#!/bin/bash

# 定义退出码
SUCCESS=0           # 成功退出
ERR_DOWNLOAD=1      # 下载失败
ERR_INSTALL=2      # 安装失败
ERR_DEPENDENCY=3    # 依赖检查失败
ERR_RUNTIME=4      # 运行时错误
ERR_SYSTEM=5       # 系统要求不满足

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 检查是否为root用户
if [ "$EUID" -ne 0 ]; then 
    echo -e "${RED}请使用root权限运行此脚本${NC}"
    exit 1
fi

# 初始化函数 - 只在首次运行时执行
initialize_environment() {
    echo -e "${GREEN}首次运行，开始配置币安抢币工具环境...${NC}"

    # 1. 安装必要的系统包
    echo -e "${YELLOW}安装系统依赖...${NC}"
    apt-get update
    apt-get install -y python3-pip python3-dev build-essential libssl-dev libffi-dev curl jq

    # 2. 安装Python依赖
    echo -e "${YELLOW}安装Python依赖...${NC}"
    pip3 install ccxt aiohttp websocket-client requests cryptography prometheus_client psutil pytz netifaces

    # 3. 创建必要的目录
    echo -e "${YELLOW}创建必要的目录...${NC}"
    mkdir -p logs config

    # 4. 设置权限
    echo -e "${YELLOW}设置权限...${NC}"
    chown -R $SUDO_USER:$SUDO_USER .
    chmod 755 *.py

    touch .initialized
    echo -e "${GREEN}初始化配置完成!${NC}"
}

# 检查是否需要初始化
if [ ! -f ".initialized" ]; then
    echo -e "${YELLOW}检测到首次运行，是否需要执行初始化配置？(y/n)${NC}"
    read -p "请选择: " init_choice
    if [[ $init_choice == "y" || $init_choice == "Y" ]]; then
        initialize_environment
    else
        echo -e "${YELLOW}跳过初始化配置${NC}"
        touch .initialized
    fi
fi

# 显示欢迎信息
echo -e "${GREEN}====== 币安现货抢币工具 ======${NC}"

# 1. 配置IPv6
echo -e "${YELLOW}配置IPv6...${NC}"

# 检查AWS实例类型
INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
if [ -z "$INSTANCE_ID" ]; then
    echo -e "${RED}不是AWS实例，跳过IPv6配置${NC}"
else
    # 获取网络接口ID
    ENI_ID=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId' --output text)
    
    if [ ! -z "$ENI_ID" ]; then
        echo "找到网络接口: $ENI_ID"
        
        # 分配IPv6地址
        echo "分配IPv6地址..."
        aws ec2 assign-ipv6-addresses --network-interface-id $ENI_ID --ipv6-address-count 4
        
        # 获取分配的IPv6地址
        IPV6_ADDRESSES=$(aws ec2 describe-network-interfaces --network-interface-ids $ENI_ID --query 'NetworkInterfaces[0].Ipv6Addresses[*].Ipv6Address' --output text)
        
        if [ ! -z "$IPV6_ADDRESSES" ]; then
            echo "成功分配IPv6地址:"
            echo "$IPV6_ADDRESSES"
            
            # 配置网络接口
            echo "配置网络接口..."
            cat > /etc/netplan/60-ipv6.yaml << EOF
network:
    version: 2
    ethernets:
        eth0:
            dhcp4: true
            dhcp6: true
            addresses:
$(echo "$IPV6_ADDRESSES" | while read -r ip; do echo "                - \"$ip/64\""; done)
EOF
            
            # 应用网络配置
            netplan apply
            
            # 验证IPv6配置
            echo "验证IPv6配置..."
            sleep 5
            ip -6 addr show eth0
            
            # 测试IPv6连通性
            echo "测试IPv6连通性..."
            for ip in $IPV6_ADDRESSES; do
                ping6 -c 1 -I $ip api.binance.com >/dev/null 2>&1
                if [ $? -eq 0 ]; then
                    echo -e "${GREEN}IPv6地址 $ip 可以访问币安${NC}"
                else
                    echo -e "${RED}IPv6地址 $ip 无法访问币安${NC}"
                fi
            done
        else
            echo -e "${RED}未能分配IPv6地址${NC}"
        fi
    else
        echo -e "${RED}未找到网络接口${NC}"
    fi
fi

# 4. 创建必要的目录
echo -e "${YELLOW}创建必要的目录...${NC}"
mkdir -p logs config

# 5. 设置权限
echo -e "${YELLOW}设置权限...${NC}"
chown -R $SUDO_USER:$SUDO_USER .
chmod 755 *.py

# 6. 完成
echo -e "${GREEN}环境配置完成!${NC}"
echo -e "${YELLOW}IPv6配置信息:${NC}"
ip -6 addr show

# 7. 显示币安API限制信息
echo -e "${YELLOW}币安API限制:${NC}"
echo "- 公共API (IP限制): 1200次/分钟"
echo "- 私有API (API Key限制): 50次/分钟"

echo "====== 币安现货抢币工具安装脚本 ======"

# 检查Python版本
check_python_version() {
    echo "检查 Python 版本..."
    # 检查 Python 版本 >= 3.9
    python3 -c "import sys; exit(0) if sys.version_info >= (3, 9) else exit(1)" || {
        echo "错误: 需要 Python 3.9 或更高版本"
        echo "当前版本: $(python3 --version)"
        return 1
    }
    echo "Python 版本检查通过 ✓"
}

# 检查系统资源
check_system_resources() {
    echo "检查系统资源..."
    local resource_warning=false
    
    # 检查内存
    total_mem=$(free -m | awk '/^Mem:/{print $2}')
    if [ $total_mem -lt 2048 ]; then  # 小于2GB
        echo -e "${YELLOW}警告: 内存可能不足 (当前: ${total_mem}MB, 建议至少2GB)${NC}"
        resource_warning=true
    else
        echo "内存检查通过 ✓ (${total_mem}MB)"
    fi
    
    # 检查CPU
    cpu_cores=$(nproc)
    if [ $cpu_cores -lt 2 ]; then
        echo -e "${YELLOW}警告: CPU核心数可能不足 (当前: $cpu_cores 核, 建议至少2核)${NC}"
        resource_warning=true
    else
        echo "CPU检查通过 ✓ ($cpu_cores 核)"
    fi
    
    # 检查磁盘空间
    free_space=$(df -m . | awk 'NR==2 {print $4}')
    if [ $free_space -lt 1024 ]; then  # 小于1GB
        echo -e "${YELLOW}警告: 磁盘空间可能不足 (当前: ${free_space}MB, 建议至少1GB)${NC}"
        resource_warning=true
    else
        echo "磁盘空间检查通过 ✓ (${free_space}MB)"
    fi
    
    if [ "$resource_warning" = true ]; then
        echo -e "\n${YELLOW}===== 警告 =====${NC}"
        echo "检测到系统资源不满足推荐配置要求。"
        echo "这可能会影响程序的性能和稳定性。"
        echo -e "建议配置：\n- 内存：至少2GB\n- CPU：至少2核\n- 磁盘空间：至少1GB"
        echo ""
        read -p "是否仍要继续安装？(y/n): " continue_choice
        if [[ $continue_choice == "y" || $continue_choice == "Y" ]]; then
            echo "继续安装..."
            return 0
        else
            echo "安装已取消"
            return 1
        fi
    fi
    
    echo -e "\n${GREEN}系统资源检查通过 ✓${NC}"
    return 0
}

# 检查网络连接
check_network() {
    echo "检查网络连接..."
    
    # 测试到币安API的连接
    curl -s -m 5 https://api.binance.com/api/v3/ping > /dev/null
    if [ $? -ne 0 ]; then
        echo "警告: 无法连接到币安API"
        return 1
    fi
    echo "网络连接检查通过 ✓"
    return 0
}

# 检查依赖函数
check_dependencies() {
    echo "正在检查依赖..."
    
    # 首先检查Python版本
    check_python_version || {
        echo -e "${YELLOW}Python版本检查失败${NC}"
        read -p "是否继续安装？(y/n): " continue_choice
        if [[ $continue_choice != "y" && $continue_choice != "Y" ]]; then
            echo "安装已取消"
        read -p "按回车键继续..."
        return $ERR_DEPENDENCY
        fi
    }
    
    # 检查系统资源
    check_system_resources || {
        read -p "按回车键继续..."
        return $ERR_SYSTEM
    }
    
    # 检查网络连接
    check_network || {
        echo -e "${YELLOW}网络连接检查失败${NC}"
        read -p "是否继续安装？(y/n): " continue_choice
        if [[ $continue_choice != "y" && $continue_choice != "Y" ]]; then
            echo "安装已取消"
        read -p "按回车键继续..."
        return $ERR_SYSTEM
        fi
    }
    
    echo "开始检查Python包..."
    
    # 使用python3 -c命令检查每个包，并在每次检查后暂停
    for package in "ccxt" "websocket" "websockets" "requests" "pytz" "aiohttp" "prometheus_client" "psutil" "numba" "numpy"; do
        echo -n "检查 $package: "
        python3 -c "import $package" 2>/dev/null && echo "已安装 ✓" || {
            echo "未安装 ✗"
            need_install=true
        }
    done

    if [ "$need_install" = true ]; then
        echo -e "\n发现未安装的依赖，是否现在安装? (y/n)"
        read -p "请选择: " install_choice
        if [[ $install_choice == "y" || $install_choice == "Y" ]]; then
            install_dependencies
            echo "依赖安装完成，重新检查..."
            check_dependencies
            return
        fi
    fi
    
    # 检查目录和文件
    echo -e "\n检查必要的目录和文件..."
    
    # 检查配置目录
    if [ ! -d "config" ]; then
        mkdir -p config
        echo "创建配置目录 ✓"
    else
        echo "配置目录已存在 ✓"
    fi
    
    # 检查日志目录
    if [ ! -d "logs" ]; then
        mkdir -p logs
        echo "创建日志目录 ✓"
    else
        echo "日志目录已存在 ✓"
    fi
    
    # 检查配置文件
    if [ -f "config/config.ini" ]; then
        echo "配置文件已存在 ✓"
    else
        echo "配置文件未创建 (首次运行时将自动创建) ✗"
    fi
    
    # 检查主程序
    if [ -f "binance_sniper.py" ]; then
        echo "主程序已存在 ✓"
    else
        echo "主程序未下载 ✗"
        echo "是否现在下载主程序? (y/n)"
        read -p "请选择: " download_choice
        if [[ $download_choice == "y" || $download_choice == "Y" ]]; then
            download_main_program
        fi
    fi
    
    echo -e "\n依赖检查完成!"
    read -p "按回车键继续..."
}

# 下载主程序函数
download_main_program() {
    echo "正在下载主程序..."
    
    # 检查是否已存在，如果存在先备份
    if [ -f "binance_sniper.py" ]; then
        echo "发现已存在的程序文件，创建备份..."
        cp binance_sniper.py binance_sniper.py.bak
    fi
    
    # 尝试使用 curl 下载
    echo "尝试使用 curl 下载..."
    curl -L -o binance_sniper.py https://raw.githubusercontent.com/fcrre26/sniper/refs/heads/main/binance_sniper.py
    
    if [ $? -ne 0 ]; then
        echo "curl 下载失败，尝试使用 wget..."
        # 备用下载方法，使用 wget
        wget --no-check-certificate -O binance_sniper.py https://raw.githubusercontent.com/fcrre26/sniper/refs/heads/main/binance_sniper.py
        
        if [ $? -ne 0 ]; then
            echo "下载失败，尝试恢复备份..."
            if [ -f "binance_sniper.py.bak" ]; then
                mv binance_sniper.py.bak binance_sniper.py
                echo "已恢复备份文件"
                return $SUCCESS
            else
                echo "错误: 下载失败且无备份可恢复"
                echo "请手动下载文件到当前目录:"
                echo "https://raw.githubusercontent.com/fcrre26/sniper/refs/heads/main/binance_sniper.py"
                return $ERR_DOWNLOAD
            fi
        fi
    fi
    
    # 检查文件是否下载成功
    if [ -f "binance_sniper.py" ]; then
        chmod +x binance_sniper.py
        echo "主程序下载成功 ✓"
        # 清理备份
        [ -f "binance_sniper.py.bak" ] && rm binance_sniper.py.bak
        return $SUCCESS
    else
        echo "错误: 文件下载失败"
        return $ERR_DOWNLOAD
    fi
}

# 安装依赖函数
install_dependencies() {
    echo "正在安装依赖..."
    
    # 检查是否安装了pip
    if ! command -v pip &> /dev/null; then
        echo "正在安装pip..."
        curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
        python3 get-pip.py
        rm get-pip.py
    fi

    # 安装依赖包
    echo "正在安装依赖包..."
    pip install -U pip setuptools wheel
    pip install ccxt                # 加密货币交所API
    pip install websocket-client    # WebSocket客户端
    pip install websockets         # WebSocket异步客户端
    pip install requests           # HTTP请求库
    pip install pytz              # 时区处理
    pip install aiohttp           # 异步HTTP客户端
    pip install prometheus-client  # 监控指标
    pip install psutil            # 系统资源监控
    pip install numba            # 性能优化
    pip install numpy            # 数学计算
    pip install cachetools       # 缓存支持
    pip install python-daemon    # 守护进程支持
    pip install lockfile         # 文件锁
    
    # 检查安装结果
    echo -e "\n====== 依赖安装完成 ======"
    pip freeze | grep -E "ccxt|websocket-client|requests|pytz|aiohttp|prometheus-client|psutil|numba|numpy|cachetools|python-daemon|lockfile"
    
    return $SUCCESS
}

# 安装systemd服务
install_service() {
    echo "正在安装系统服务..."
    
    # 创建临时服务文件
    cat > binance-sniper.service.tmp << EOF
[Unit]
Description=Binance Sniper Service
After=network.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$USER
WorkingDirectory=$(pwd)
Environment=PYTHONUNBUFFERED=1
ExecStart=$(which python3) binance_sniper.py --daemon
Restart=always
RestartSec=3
StandardOutput=append:/var/log/binance-sniper/output.log
StandardError=append:/var/log/binance-sniper/error.log

# 性能优化设置
CPUSchedulingPolicy=fifo
CPUSchedulingPriority=99
IOSchedulingClass=realtime
IOSchedulingPriority=0
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

    # 使用sudo移动服务文件
    sudo mv binance-sniper.service.tmp /etc/systemd/system/binance-sniper.service

    # 创建日志目录
    sudo mkdir -p /var/log/binance-sniper
    sudo chown $USER:$USER /var/log/binance-sniper

    # 创建临时日志轮转配置
    cat > binance-sniper.logrotate.tmp << EOF
/var/log/binance-sniper/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    create 0640 $USER $USER
}
EOF

    # 使用sudo移动日志轮转配置
    sudo mv binance-sniper.logrotate.tmp /etc/logrotate.d/binance-sniper

    # 设置正确的权限
    sudo chmod 644 /etc/systemd/system/binance-sniper.service
    sudo chmod 644 /etc/logrotate.d/binance-sniper

    # 重新加载systemd配置
    sudo systemctl daemon-reload

    # 启用服务
    sudo systemctl enable binance-sniper.service

    echo """
服务安装完成！
使用以下命令管理服务：
- 启动: sudo systemctl start binance-sniper
- 停止: sudo systemctl stop binance-sniper
- 状态: sudo systemctl status binance-sniper
- 查看日志: tail -f /var/log/binance-sniper/output.log
"""
}

# 测试币安API延迟函数
test_binance_latency() {
    echo "开始测试币安API延迟..."
    echo "测试地点: $(curl -s ipinfo.io | grep country | cut -d'"' -f4)"
    echo "本地IP: $(curl -s ipinfo.io | grep ip | cut -d'"' -f4)"
    echo "本地时间: $(date)"
    echo ""
    echo "测试中，请稍候..."
    
    # 测试主要的币安API端点
    endpoints=(
        "api.binance.com/api/v3/ping"           # API服务器检查
        "api1.binance.com/api/v3/time"          # 服务器时间
        "api2.binance.com/api/v3/ticker/price"  # 价格更新
        "api3.binance.com/api/v3/depth"         # 深度数据
        "data-api.binance.vision"               # 行情数据
    )
    
    echo "=== REST API延迟测试 ==="
    for endpoint in "${endpoints[@]}"; do
        echo -n "测试 ${endpoint%%/*}: "
        # 进行5次测试取平均值
        total_time=0
        success_count=0
        min_time=999999
        max_time=0
        
        for i in {1..5}; do
            time_result=$(curl -o /dev/null -s -w "%{time_total}\n" https://$endpoint)
            if [ $? -eq 0 ]; then
                total_time=$(echo "$total_time + $time_result" | bc)
                # 更新最小值和最大值
                time_ms=$(echo "$time_result * 1000" | bc)
                if (( $(echo "$time_ms < $min_time" | bc -l) )); then
                    min_time=$time_ms
                fi
                if (( $(echo "$time_ms > $max_time" | bc -l) )); then
                    max_time=$time_ms
                fi
                success_count=$((success_count + 1))
            fi
        done
        
        if [ $success_count -gt 0 ]; then
            avg_time=$(echo "scale=2; $total_time / $success_count * 1000" | bc)
            echo "平均: ${avg_time}ms (最小: ${min_time%.*}ms, 最大: ${max_time%.*}ms)"
        else
            echo "连接失败"
        fi
    done
    
    # 测试WebSocket延迟
    echo -e "\n=== WebSocket延迟测试 ==="
    endpoints_ws=(
        "stream.binance.com:9443/ws"     # 现货WebSocket
        "dstream.binance.com/ws"         # 合约WebSocket
    )
    
    for endpoint in "${endpoints_ws[@]}"; do
        echo -n "测试 ${endpoint%%/*}: "
        ws_time=$(python3 -c "
import websocket
import time
import sys

try:
    start = time.time()
    ws = websocket.create_connection('wss://$endpoint')
    ws.send('{\"method\": \"ping\"}')
    ws.recv()
    ws.close()
    print(f'{(time.time() - start) * 1000:.2f}ms')
except Exception as e:
    print('连接失败')
" 2>/dev/null)
        echo "$ws_time"
    done
    
    echo """
=== 延迟评估标准 ===
优秀: <50ms     (适合高频交易)
良好: 50-100ms  (适合一般交易)
一般: 100-200ms (可以使用)
较差: >200ms    (不建议使用)

注意: 实际交易时的延迟可能会因为市场情况而波动
建议在不同时段多次测试
"""
    
    read -p "按回车键继续..."
}

# 在脚本开头的常量定义后添加时间同步函数
sync_system_time() {
    echo "正在同步系统时间..."
    
    # 检查是否有root权限
    if [ "$EUID" -ne 0 ]; then
        echo "需要root权限来同步时间"
        echo "请输入sudo密码:"
        sudo -v || return 1
    fi
    
    # 设置时区为UTC+8
    echo "设置时区为Asia/Shanghai..."
    sudo timedatectl set-timezone Asia/Shanghai
    
    # 检查并安装ntpdate
    if ! command -v ntpdate &> /dev/null; then
        echo "正在安装ntpdate..."
        sudo apt-get update
        sudo apt-get install -y ntpdate
    fi
    
    # 同步时间
    echo "正在从NTP服务器同步时间..."
    sudo ntpdate ntp.aliyun.com || {
        # 如果阿里云NTP失败，尝试其他服务器
        sudo ntpdate time.windows.com || 
        sudo ntpdate time.apple.com || 
        sudo ntpdate pool.ntp.org
    }
    
    # 将时间同步写入硬件时钟
    echo "更新硬件时钟..."
    sudo hwclock --systohc
    
    # 显示当前时间
    echo "当前系统时间:"
    date
    
    # 启用NTP自动同步
    echo "启用NTP自动同步..."
    sudo timedatectl set-ntp true
    
    # 显示时间同步状态
    timedatectl status
    
    echo "时间同步完成!"
    return 0
}

# 获取网络接口名称函数
get_network_interface() {
    # 尝试获取主要网络接口
    local interface=""
    
    # 检查常见的接口名称
    for i in ens3 ens4 ens5 eth0 eth1 enp1s0 enp0s3 ens18 ens160; do
        if ip link show $i >/dev/null 2>&1; then
            interface=$i
            break
        fi
    done
    
    # 如果上述都没找到，尝试获取默认路由接口
    if [ -z "$interface" ]; then
        interface=$(ip route | grep default | awk '{print $5}' | head -n1)
    fi
    
    # 如果还是没找到，获取第一个非lo的接口
    if [ -z "$interface" ]; then
        interface=$(ip link show | grep -v lo | grep -oP '(?<=\d: )[^:@]+' | head -n1)
    fi
    
    echo "$interface"
}

# 修改detect_vps_provider函数
detect_vps_provider() {
    local provider=""
    
    # 显示菜单到标准错误输出，这样不会被 > /dev/null 重定向
    cat >&2 << EOF

====== 选择VPS提供商 ======
请选择您使用的VPS服务商：

1. AWS (亚马逊云服务)
   - 支持自动分配多个IPv6
   - 需要AWS CLI工具

2. Vultr
   - 默认支持IPv6
   - 自动DHCP配置
   - 每个实例一个IPv6地址

3. DigitalOcean
   - 支持IPv6
   - 需要在控制面板开启

4. 其他/通用
   - 适用于其他VPS提供商
   - 使用通用IPv6配置方法
   - 支持基本的IPv6功能

=========================
EOF
    
    while true; do
        # 使用标准错误输出来显示提示
        read -p $'\n请选择VPS提供商 (1-4): ' provider_choice >&2
        case $provider_choice in
            1)
                provider="aws"
                break
                ;;
            2)
                provider="vultr"
                break
                ;;
            3)
                provider="digitalocean"
                break
                ;;
            4)
                provider="generic"
                break
                ;;
            *)
                echo -e "${RED}无效选项，请输入1-4之间的数字${NC}" >&2
                ;;
        esac
    done
    
    # 保存选择到配置文件
    echo "$provider" > .vps_provider
    
    return 0
}

# 显示当前IPv6配置的函数
show_current_ipv6() {
    local interface=$1
    echo -e "${YELLOW}当前IPv6配置详情：${NC}"
    echo -e "\n1. 网络接口信息："
    ip link show $interface | grep -v "link/" | sed 's/^/   /'
    
    echo -e "\n2. IPv6地址列表："
    local count=0
    local global_count=0
    
    # 分类显示IPv6地址
    echo -e "\n   ${GREEN}全局地址：${NC}"
    while IFS= read -r line; do
        if [[ $line =~ "inet6" ]]; then
            local addr=$(echo $line | awk '{print $2}')
            local scope=$(echo $line | awk '{print $4}')
            local type=""
            if [[ $line =~ "temporary" ]]; then
                type="临时"
            elif [[ $line =~ "mngtmpaddr" ]]; then
                type="管理"
            elif [[ $line =~ "dynamic" ]]; then
                type="动态"
            else
                type="静态"
            fi
            
            if [[ $scope == "global" ]]; then
                echo -e "   - 地址: ${GREEN}$addr${NC}"
                echo -e "     类型: $type"
                echo -e "     有效期: $(echo $line | grep -o 'valid_lft [^ ]*' | cut -d' ' -f2)"
                echo -e "     首选期: $(echo $line | grep -o 'preferred_lft [^ ]*' | cut -d' ' -f2)"
                echo ""
                ((global_count++))
            fi
            ((count++))
        fi
    done < <(ip -6 addr show dev $interface)
    
    echo -e "   ${YELLOW}链路本地地址：${NC}"
    ip -6 addr show dev $interface | grep "scope link" | while read -r line; do
        local addr=$(echo $line | awk '{print $2}')
        echo -e "   - ${addr}"
    done
    
    # 显示统计信息
    echo -e "\n3. 地址统计："
    echo -e "   - 全局地址数量: ${GREEN}$global_count${NC}"
    echo -e "   - 总地址数量: $count"
    
    # 显示路由信息
    echo -e "\n4. IPv6路由信息："
    ip -6 route show dev $interface | sed 's/^/   /'
    
    # 显示网络设置
    echo -e "\n5. IPv6网络设置："
    echo -e "   - 接受路由通告: $(sysctl -n net.ipv6.conf.$interface.accept_ra)"
    echo -e "   - 自动配置: $(sysctl -n net.ipv6.conf.$interface.autoconf)"
    echo -e "   - 转发: $(sysctl -n net.ipv6.conf.$interface.forwarding)"
    
    return 0
}

# 修改manage_ipv6函数中显示IPv6配置的部分
manage_ipv6() {
    # 获取网络接口
    NETWORK_INTERFACE=$(get_network_interface)
    if [ -z "$NETWORK_INTERFACE" ]; then
        echo -e "${RED}错误: 未能找到有效的网络接口${NC}"
        read -p "按回车键继续..."
        return 1
    fi
    
    # 检查是否已有保存的VPS提供商选择
    if [ -f ".vps_provider" ]; then
        VPS_PROVIDER=$(cat .vps_provider)
    else
        detect_vps_provider
        VPS_PROVIDER=$(cat .vps_provider)
    fi
    
    while true; do
        clear
        echo """
====== IPv6 管理菜单 ======
当前网络接口: $NETWORK_INTERFACE
VPS提供商: $VPS_PROVIDER
=========================
1. 查看当前IPv6配置
2. 配置/重置IPv6地址
3. 测试所有IPv6延迟
4. 删除指定IPv6地址
5. 清理所有IPv6配置
6. 测试币安API访问
7. 重新选择VPS提供商
0. 返回主菜单
=========================
"""
        read -p "请选择操作 (0-7): " ipv6_choice
        case $ipv6_choice in
            1)
                show_current_ipv6 $NETWORK_INTERFACE
                ;;
            2)
                case $VPS_PROVIDER in
                    "aws")
                        if ! command -v aws &> /dev/null; then
                            echo -e "${RED}错误: 未安装AWS CLI工具${NC}"
                            echo "如需使用AWS特定功能，请先安装AWS CLI并配置凭证"
                            read -p "是否使用通用配置方法继续？(y/n): " use_generic
                            if [[ $use_generic == "y" || $use_generic == "Y" ]]; then
                                configure_generic_ipv6
                            fi
                        else
                            configure_aws_ipv6
                        fi
                        ;;
                    "vultr")
                        configure_vultr_ipv6
                        ;;
                    *)
                        configure_generic_ipv6
                        ;;
                esac
                read -p "按回车键继续..."
                ;;
            3)
                echo "测试所有IPv6延迟..."
                test_ipv6_latency $NETWORK_INTERFACE
                read -p "按回车键继续..."
                ;;
            4)
                delete_ipv6_address $NETWORK_INTERFACE
                read -p "按回车键继续..."
                ;;
            5)
                clean_ipv6_config $NETWORK_INTERFACE
                read -p "按回车键继续..."
                ;;
            6)
                test_binance_api_ipv6 $NETWORK_INTERFACE
                read -p "按回车键继续..."
                ;;
            7)
                detect_vps_provider
                VPS_PROVIDER=$(cat .vps_provider)
                ;;
            0)
                break
                ;;
            *)
                echo "无效选项"
                sleep 1
                ;;
        esac
    done
}

# 修改IPv6相关函数
generate_random_hex() {
    local length=$1
    head -c $((length/2)) /dev/urandom | hexdump -ve '1/1 "%.2x"'
}

get_ipv6_prefix() {
    local interface=$1
    
    # 获取非本地IPv6地址
    local current_ipv6=$(ip -6 addr show dev $interface | grep "scope global" | grep -v "temporary" | head -n1 | awk '{print $2}')
    
    if [ -z "$current_ipv6" ]; then
        echo -e "${RED}错误: 未检测到IPv6地址${NC}" >&2
        return 1
    fi
    
    # 从CIDR格式中提取地址部分
    local ipv6_addr=$(echo "$current_ipv6" | cut -d'/' -f1)
    
    # 提取前缀（前64位）
    local prefix=$(echo "$ipv6_addr" | sed -E 's/:[^:]+:[^:]+:[^:]+:[^:]+$/::/')
    
    if [ -z "$prefix" ]; then
        echo -e "${RED}错误: 无法提取IPv6前缀${NC}" >&2
        return 1
    fi
    
    echo "$prefix"
}

# Vultr特定的IPv6配置函数
configure_vultr_ipv6() {
    echo "配置Vultr IPv6..."
    
    # 检查当前IPv6配置
    echo "当前IPv6配置:"
    ip -6 addr show $NETWORK_INTERFACE
    
    echo -e "\n${YELLOW}Vultr IPv6配置选项：${NC}"
    echo "1. 启用基本IPv6（单个地址）"
    echo "2. 启用SLAAC自动配置"
    echo "3. 手动添加额外IPv6地址"
    echo "4. 返回上级菜单"
    
    read -p "请选择操作 (1-4): " vultr_choice
    case $vultr_choice in
        1|2)
            if [ "$vultr_choice" == "1" ]; then
                echo "配置基本IPv6..."
                CONFIG_CONTENT="network:
    version: 2
    ethernets:
        $NETWORK_INTERFACE:
            dhcp4: true
            dhcp6: true
            accept-ra: true"
            else
                echo "配置SLAAC自动配置..."
                CONFIG_CONTENT="network:
    version: 2
    ethernets:
        $NETWORK_INTERFACE:
            dhcp4: true
            dhcp6: true
            accept-ra: true
            ipv6-privacy: true
            ipv6-address-generation: eui64
            addresses: []  # 保留现有IPv6地址
            routes:
              - to: ::/0
                scope: global"
            fi
            
            # 创建配置文件并设置正确的权限
            echo "$CONFIG_CONTENT" | sudo tee /etc/netplan/60-ipv6.yaml > /dev/null
            sudo chmod 600 /etc/netplan/60-ipv6.yaml
            
            # 应用配置前先启用IPv6
            echo "启用IPv6..."
            sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0
            sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0
            sudo sysctl -w net.ipv6.conf.$NETWORK_INTERFACE.disable_ipv6=0
            
            # 应用配置
            echo "应用网络配置..."
            sudo netplan apply
            
            # 等待配置生效
            echo "等待配置生效..."
            sleep 10
            
            # 显示新的配置
            echo -e "\n新的IPv6配置:"
            ip -6 addr show $NETWORK_INTERFACE
            
            # 测试连接性
            echo -e "\n测试IPv6连接性..."
            # 获取所有全局IPv6地址进行测试
            local success=false
            for ipv6_addr in $(ip -6 addr show dev $NETWORK_INTERFACE | grep "scope global" | awk '{print $2}' | cut -d'/' -f1); do
                echo -n "测试 $ipv6_addr: "
                if ping6 -c 1 -I $ipv6_addr api.binance.com >/dev/null 2>&1; then
                    echo -e "${GREEN}成功${NC}"
                    success=true
                else
                    echo -e "${RED}失败${NC}"
                fi
            done
            
            if [ "$success" = true ]; then
                echo -e "${GREEN}IPv6配置成功，至少有一个地址可以访问币安${NC}"
            else
                echo -e "${RED}IPv6配置可能有问题，尝试以下解决方案：${NC}"
                echo "1. 检查防火墙设置"
                echo "2. 等待几分钟后再测试"
                echo "3. 在Vultr控制面板确认IPv6是否已启用"
                echo "4. 尝试重启网络服务"
                
                read -p "是否要重启网络服务？(y/n): " restart_choice
                if [[ $restart_choice == "y" || $restart_choice == "Y" ]]; then
                    sudo systemctl restart systemd-networkd
                    sudo netplan apply
                    sleep 5
                    echo -e "\n重启后的IPv6配置:"
                    ip -6 addr show $NETWORK_INTERFACE
                fi
            fi
            ;;
        3)
            echo "手动添加IPv6地址..."
            # 获取IPv6前缀
            local prefix=$(get_ipv6_prefix $NETWORK_INTERFACE)
            if [ $? -ne 0 ]; then
                echo -e "${RED}无法获取IPv6前缀${NC}"
                return 1
            fi
            
            read -p "请输入要添加的IPv6地址数量: " num_addresses
            if [[ ! "$num_addresses" =~ ^[0-9]+$ ]] || [ "$num_addresses" -lt 1 ]; then
                echo -e "${RED}请输入有效的数字${NC}"
                return 1
            fi
            
            # 生成并添加IPv6地址
            echo "正在添加IPv6地址..."
            for ((i=1; i<=num_addresses; i++)); do
                # 生成随机后缀
                local suffix=$(printf "%04x:%04x:%04x:%04x" \
                    $((RANDOM % 65536)) \
                    $((RANDOM % 65536)) \
                    $((RANDOM % 65536)) \
                    $((RANDOM % 65536)))
                
                local new_ip="${prefix%::}:${suffix}"
                echo "尝试添加: $new_ip"
                
                if ip -6 addr add "$new_ip/64" dev "$NETWORK_INTERFACE" 2>/dev/null; then
                    echo -e "${GREEN}成功添加: $new_ip${NC}"
                else
                    echo -e "${RED}添加失败: $new_ip${NC}"
                fi
            done
            ;;
        4)
            return 0
            ;;
        *)
            echo -e "${RED}无效选项${NC}"
            return 1
            ;;
    esac
}

# AWS特定的IPv6配置函数
configure_aws_ipv6() {
    echo "配置AWS IPv6..."
    # 获取实例ID
    INSTANCE_ID=$(curl -s http://169.254.169.254/latest/meta-data/instance-id)
    if [ -z "$INSTANCE_ID" ]; then
        echo -e "${RED}无法获取AWS实例ID${NC}"
        return 1
    fi
                
                # 获取网络接口ID
    ENI_ID=$(aws ec2 describe-instances --instance-ids $INSTANCE_ID --query 'Reservations[0].Instances[0].NetworkInterfaces[0].NetworkInterfaceId' --output text)
                
                if [ ! -z "$ENI_ID" ]; then
        echo "找到网络接口: $ENI_ID"
        
        # 分配IPv6地址
        echo "分配IPv6地址..."
        aws ec2 assign-ipv6-addresses --network-interface-id $ENI_ID --ipv6-address-count 4
        
        # 获取分配的IPv6地址
                        IPV6_ADDRESSES=$(aws ec2 describe-network-interfaces --network-interface-ids $ENI_ID --query 'NetworkInterfaces[0].Ipv6Addresses[*].Ipv6Address' --output text)
                        
        if [ ! -z "$IPV6_ADDRESSES" ]; then
            echo "成功分配IPv6地址:"
            echo "$IPV6_ADDRESSES"
            
            # 配置网络接口
            echo "配置网络接口..."
                        cat > /etc/netplan/60-ipv6.yaml << EOF
network:
    version: 2
    ethernets:
        eth0:
            dhcp4: true
            dhcp6: true
            addresses:
$(echo "$IPV6_ADDRESSES" | while read -r ip; do echo "                - \"$ip/64\""; done)
EOF
            
            # 应用网络配置
                        netplan apply
                        
            # 验证IPv6配置
            echo "验证IPv6配置..."
            sleep 5
                        ip -6 addr show eth0
            
            # 测试IPv6连通性
            echo "测试IPv6连通性..."
            for ip in $IPV6_ADDRESSES; do
                ping6 -c 1 -I $ip api.binance.com >/dev/null 2>&1
                if [ $? -eq 0 ]; then
                    echo -e "${GREEN}IPv6地址 $ip 可以访问币安${NC}"
                else
                    echo -e "${RED}IPv6地址 $ip 无法访问币安${NC}"
                fi
            done
        else
            echo -e "${RED}未能分配IPv6地址${NC}"
        fi
    else
        echo -e "${RED}未找到网络接口${NC}"
    fi
}

# 通用IPv6配置函数
configure_generic_ipv6() {
    echo "配置IPv6..."
    echo "正在使用通用配置方法"
    
    # 创建基本的netplan配置
    cat > /etc/netplan/60-ipv6.yaml << EOF
network:
    version: 2
    ethernets:
        $NETWORK_INTERFACE:
            dhcp4: true
            dhcp6: true
            accept-ra: true
EOF
    
    # 应用配置
    netplan apply
    sleep 5
    
    # 显示配置结果
    ip -6 addr show $NETWORK_INTERFACE
}

# 测试IPv6延迟函数
test_ipv6_latency() {
    local interface=$1
    for ip in $(ip -6 addr show $interface | grep "inet6" | awk '{print $2}' | cut -d'/' -f1); do
                    echo -n "测试 $ip -> api.binance.com: "
                    ping6 -c 1 -I $ip api.binance.com 2>/dev/null | grep "time=" || echo "失败"
                done
}

# 删除IPv6地址函数
delete_ipv6_address() {
    local interface=$1
                echo "当前IPv6地址:"
    ip -6 addr show $interface | grep "inet6" | awk '{print NR ") " $2}'
                read -p "请输入要删除的IPv6编号: " del_num
    ip_to_del=$(ip -6 addr show $interface | grep "inet6" | awk '{print $2}' | sed -n "${del_num}p")
                if [ ! -z "$ip_to_del" ]; then
        ip -6 addr del $ip_to_del dev $interface
                    echo "已删除 $ip_to_del"
                else
                    echo "无效的编号"
                fi
}

# 测试币安API访问函数
test_binance_api_ipv6() {
    local interface=$1
    echo -e "${YELLOW}测试币安API的IPv4/IPv6支持情况...${NC}"
    
    # 检查并安装必要工具
    echo -e "\n${YELLOW}检查必要工具...${NC}"
    local tools_to_install=()
    
    # 检查各个工具
    for tool in "traceroute6" "curl" "openssl" "host"; do
        if ! command -v $tool &>/dev/null; then
            case $tool in
                "traceroute6")
                    tools_to_install+=("iputils-tracepath")
                    ;;
                "host")
                    tools_to_install+=("bind9-host")
                    ;;
                *)
                    tools_to_install+=("$tool")
                    ;;
            esac
        fi
    done
    
    # 如果有工具需要安装
    if [ ${#tools_to_install[@]} -ne 0 ]; then
        echo -e "${YELLOW}正在安装必要工具: ${tools_to_install[*]}${NC}"
        apt-get update >/dev/null 2>&1
        apt-get install -y "${tools_to_install[@]}" >/dev/null 2>&1
    fi
    
    # IPv4测试
    echo -e "\n${YELLOW}1. 测试IPv4连接:${NC}"
    local ipv4_result=$(curl -4 -s -v https://api.binance.com/api/v3/time 2>&1)
    local ipv4_http_code=$(echo "$ipv4_result" | grep "< HTTP/2" | awk '{print $3}')
    local ipv4_response=$(echo "$ipv4_result" | grep "^{")
    
    if [ "$ipv4_http_code" = "200" ]; then
        echo -e "${GREEN}IPv4连接正常 ✓${NC}"
        echo "HTTP状态码: $ipv4_http_code"
        echo "响应: $ipv4_response"
        echo -e "\n连接详情:"
        echo "$ipv4_result" | grep "* " | sed 's/^/  /'
    else
        echo -e "${RED}IPv4连接失败 ✗${NC}"
        echo "错误信息:"
        echo "$ipv4_result" | grep "* " | sed 's/^/  /'
    fi
    
    # IPv6测试部分保持不变...
    
    # IPv6测试部分修改...
    echo -e "\n${YELLOW}2. 测试IPv6连接:${NC}"
    local ipv6_addr=$(ip -6 addr show dev $interface | grep "scope global" | awk '{print $2}' | cut -d'/' -f1 | head -n1)
    if [ ! -z "$ipv6_addr" ]; then
        echo "使用IPv6地址: $ipv6_addr"
        
        # DNS测试
        echo -e "\nDNS测试:"
        echo -n "api.binance.com AAAA记录: "
        local dns_result=$(host -t AAAA api.binance.com)
        if echo "$dns_result" | grep -q "has IPv6"; then
            echo -e "${GREEN}存在${NC}"
            echo "$dns_result" | grep "has IPv6"
        else
            echo -e "${RED}不存在${NC}"
            echo "尝试使用Google DNS..."
            if host -t AAAA api.binance.com 8.8.8.8 | grep -q "has IPv6"; then
                echo -e "${GREEN}通过Google DNS找到AAAA记录${NC}"
            fi
        fi
        
        # SSL测试改进
        echo -e "\n1) SSL/TLS测试:"
        local ssl_result=$(echo "Q" | timeout 5 openssl s_client -connect api.binance.com:443 -6 -servername api.binance.com 2>&1)
        if [ $? -eq 0 ]; then
            echo -e "   SSL连接: ${GREEN}成功${NC}"
            echo "   - TLS版本: $(echo "$ssl_result" | grep "Protocol" | awk '{print $2}')"
            echo "   - 密码套件: $(echo "$ssl_result" | grep "Cipher" | awk '{print $5}')"
            echo "   - 证书主题: $(echo "$ssl_result" | grep "subject=" | sed 's/^.*subject=//')"
        else
            echo -e "   SSL连接: ${RED}失败${NC}"
            echo "   错误信息:"
            echo "$ssl_result" | grep "error" | sed 's/^/   /'
        fi
        
        # TCP连接测试
        echo -e "\n2) TCP连接测试:"
        for port in 80 443; do
            echo -n "   端口 $port: "
            if timeout 5 bash -c "exec 3<>/dev/tcp/api.binance.com/$port" 2>/dev/null; then
                echo -e "${GREEN}可连接${NC}"
            else
                echo -e "${RED}不可连接${NC}"
            fi
        done
        
        # API测试
        echo -e "\n3) API测试:"
        local curl_result=$(curl -g -6 -s -v --interface "$ipv6_addr" \
            --connect-timeout 5 \
            -H "Accept: application/json" \
            -H "User-Agent: Mozilla/5.0" \
            https://api.binance.com/api/v3/time 2>&1)
        
        echo "连接过程:"
        echo "$curl_result" | grep "* " | sed 's/^/   /'
        
        # 路由测试
        if command -v traceroute6 &>/dev/null; then
            echo -e "\n4) 路由追踪:"
            traceroute6 -n api.binance.com 2>&1 | head -n 4 | sed 's/^/   /'
        fi
    fi
    
    # 分析结果
    echo -e "\n${YELLOW}详细分析：${NC}"
    echo "1. 网络层支持："
    echo "   - IPv4: 完全支持"
    if echo "$curl_result" | grep -q "Connection refused"; then
        echo "   - IPv6: 服务器主动拒绝连接"
        echo "     原因: 可能是服务器配置限制仅接受IPv4请求"
    elif echo "$curl_result" | grep -q "SSL handshake failure"; then
        echo "   - IPv6: SSL握手失败"
        echo "     原因: 可能是服务器SSL配置不支持IPv6"
    elif echo "$curl_result" | grep -q "Closing connection"; then
        echo "   - IPv6: 连接被服务器关闭"
        echo "     原因: 可能是应用层限制或负载均衡配置"
    else
        echo "   - IPv6: 连接异常，具体原因未知"
    fi
    
    echo -e "\n2. 建议方案："
    echo "   a) 短期解决方案："
    echo "      - 使用IPv4进行API访问"
    echo "      - 配置本地IPv6到IPv4的代理"
    echo "   b) 长期解决方案："
    echo "      - 使用支持双栈的负载均衡器"
    echo "      - 配置IPv6 NAT64网关"
    echo "      - 使用CDN提供IPv6支持"

    # 最后添加对比分析
    echo -e "\n${YELLOW}连接对比分析：${NC}"
    echo "1. IPv4连接:"
    if [ "$ipv4_http_code" = "200" ]; then
        echo -e "   - 状态: ${GREEN}正常${NC}"
        # 改进延迟计算
        local ipv4_latency=$(echo "$ipv4_result" | grep "time_total" | tail -n1 | awk '{print $3}' | cut -d: -f2)
        if [ ! -z "$ipv4_latency" ]; then
            echo "   - 延迟: ${ipv4_latency}s"
        else
            echo "   - 延迟: 未知"
        fi
    else
        echo -e "   - 状态: ${RED}异常${NC}"
    fi
    
    echo "2. IPv6连接:"
    if echo "$curl_result" | grep -q "HTTP/2 200"; then
        echo -e "   - 状态: ${GREEN}正常${NC}"
        local ipv6_latency=$(echo "$curl_result" | grep "time_total" | tail -n1 | awk '{print $3}' | cut -d: -f2)
        if [ ! -z "$ipv6_latency" ]; then
            echo "   - 延迟: ${ipv6_latency}s"
        fi
    else
        echo -e "   - 状态: ${RED}异常${NC}"
        if echo "$curl_result" | grep -q "Connection refused"; then
            echo "   - 原因: 服务器拒绝连接"
        elif echo "$curl_result" | grep -q "SSL handshake failure"; then
            echo "   - 原因: SSL握手失败"
        elif echo "$curl_result" | grep -q "Closing connection"; then
            echo "   - 原因: 服务器主动关闭连接"
        fi
    fi
    
    echo -e "\n${YELLOW}最终建议：${NC}"
    if [ "$ipv4_http_code" = "200" ]; then
        echo "1. 使用IPv4进行API访问（推荐）"
        echo "   - 连接稳定可靠"
        echo "   - 服务器完全支持"
        if [ ! -z "$ipv4_latency" ]; then
            echo "   - 当前延迟: ${ipv4_latency}s"
        fi
    fi
    echo "2. 如果必须使用IPv6，建议采用以下方案："
    echo "   a) 使用IPv6到IPv4代理"
    echo "   b) 配置NAT64网关"
    echo "   c) 使用支持双栈的负载均衡器"
    echo "   d) 或考虑使用CDN提供IPv6支持"
    
    # 添加等待用户确认
    echo -e "\n${YELLOW}测试完成，按回车键继续...${NC}"
    read
}

# 添加多IP配置函数
configure_multiple_ips() {
    echo -e "${YELLOW}配置多IP支持...${NC}"
    
    # 检测VPS提供商
    if [ -f "/sys/class/dmi/id/product_version" ]; then
        local vps_type=$(cat /sys/class/dmi/id/product_version)
    else
        local vps_type="unknown"
    fi
    
    # 获取主网卡名称
    local interface=$(ip route | grep default | awk '{print $5}' | head -n1)
    if [ -z "$interface" ]; then
        echo -e "${RED}未找到网络接口${NC}"
        return 1
    fi
    
    echo "检测到网络接口: $interface"
    echo "VPS提供商: $vps_type"
    
    # 显示当前IP配置
    echo -e "\n${YELLOW}当前IP配置:${NC}"
    ip addr show $interface | grep "inet " | awk '{print NR". "$2" ("$NF")"}'
    
    echo -e "\n${YELLOW}选择操作:${NC}"
    echo "1. 查看IP配置详情"
    echo "2. 配置新IP地址"
    echo "3. 测试所有IP延迟"
    echo "4. 查看IP使用建议"
    echo "0. 返回主菜单"
    
    read -p "请选择 (0-4): " ip_choice
    case $ip_choice in
        1)
            echo -e "\n${YELLOW}IP配置详情:${NC}"
            echo "1) 当前活跃IP:"
            ip addr show $interface | grep "inet " | while read -r line; do
                local ip=$(echo $line | awk '{print $2}')
                local status="活跃"
                echo "   - IP: $ip"
                echo "     状态: $status"
                
                # 测试到币安API的连接
                local pure_ip=$(echo $ip | cut -d'/' -f1)
                local latency=$(curl -s -w "%{time_total}\n" -o /dev/null --interface $pure_ip https://api.binance.com/api/v3/time 2>/dev/null)
                if [ $? -eq 0 ]; then
                    echo -e "     延迟: ${GREEN}${latency}s${NC}"
                else
                    echo -e "     延迟: ${RED}连接失败${NC}"
                fi
                echo ""
            done
            
            if [[ "$vps_type" == *"Vultr"* ]]; then
                echo -e "\n2) Vultr IP管理说明:"
                echo "   - 额外IPv4需要在Vultr控制面板购买"
                echo "   - 每个实例默认1个IPv4地址"
                echo "   - 可以在控制面板启用IPv6"
            elif [[ "$vps_type" == *"amazon"* ]]; then
                echo -e "\n2) AWS IP管理说明:"
                echo "   - 可以申请弹性IP (Elastic IP)"
                echo "   - 支持在VPC中分配多个私有IP"
                echo "   - 可以使用Network Interface添加IP"
            fi
            ;;
        2)
            if [[ "$vps_type" == *"Vultr"* ]]; then
                echo -e "\n${YELLOW}Vultr IP配置说明:${NC}"
                echo "1. 额外IPv4地址需要在Vultr控制面板购买"
                echo "2. 购买后，请按照以下步骤配置："
                echo "   a) 在控制面板中添加IP"
                echo "   b) 等待IP分配完成"
                echo "   c) 使用以下命令配置系统："
                echo "      ip addr add <新IP>/24 dev $interface"
                read -p "是否已在Vultr控制面板购买新IP? (y/n): " has_new_ip
                if [[ $has_new_ip == "y" || $has_new_ip == "Y" ]]; then
                    read -p "请输入新分配的IP (格式: x.x.x.x/24): " new_ip
                    if ip addr add $new_ip dev $interface 2>/dev/null; then
                        echo -e "${GREEN}IP添加成功${NC}"
                        test_ip_connection $new_ip
                    else
                        echo -e "${RED}IP添加失败，请检查IP格式或是否已被使用${NC}"
                    fi
                fi
            elif [[ "$vps_type" == *"amazon"* ]]; then
                echo -e "\n${YELLOW}AWS IP配置说明:${NC}"
                echo "1. 弹性IP配置："
                echo "   - 在AWS控制台申请弹性IP"
                echo "   - 关联到当前实例"
                echo "2. 网络接口配置："
                echo "   - 在VPC中创建新的网络接口"
                echo "   - 分配私有IP地址"
                echo "3. 负载均衡配置："
                echo "   - 使用ALB/NLB实现多IP"
            fi
            ;;
        3)
            echo -e "\n${YELLOW}测试所有IP延迟...${NC}"
            test_all_ips_latency $interface
            ;;
        4)
            echo -e "\n${YELLOW}IP使用建议:${NC}"
            echo "1. API访问策略:"
            echo "   - 使用多个API密钥轮换访问"
            echo "   - 实现请求负载均衡"
            echo "   - 监控每个IP的请求限制"
            echo ""
            echo "2. IP轮换策略:"
            echo "   - 按照延迟优先级使用IP"
            echo "   - 在达到限制前切换IP"
            echo "   - 保持IP池的动态更新"
            ;;
        0)
            return 0
            ;;
        *)
            echo -e "${RED}无效选项${NC}"
            ;;
    esac
    
    read -p "按回车键继续..."
}

# 测试IP连接性能
test_ip_connection() {
    local ip=$1
    echo -e "\n${YELLOW}测试IP: $ip${NC}"
    
    # 提取IP地址（去除CIDR）
    local pure_ip=$(echo $ip | cut -d'/' -f1)
    
    # 测试到币安API的连接
    echo "测试到币安API的连接..."
    local result=$(curl -s -m 5 --interface $pure_ip https://api.binance.com/api/v3/time)
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}连接成功${NC}"
        local latency=$(curl -s -w "%{time_total}\n" -o /dev/null --interface $pure_ip https://api.binance.com/api/v3/time)
        echo "延迟: ${latency}s"
    else
        echo -e "${RED}连接失败${NC}"
    fi
}

# 测试所有IP的延迟
test_all_ips_latency() {
    local interface=$1
    echo -e "${YELLOW}测试所有IP到币安API的延迟...${NC}"
    
    ip addr show $interface | grep "inet " | while read -r line; do
        local ip=$(echo $line | awk '{print $2}' | cut -d'/' -f1)
        echo -e "\n测试 $ip:"
        local latency=$(curl -s -w "%{time_total}\n" -o /dev/null --interface $ip https://api.binance.com/api/v3/time 2>/dev/null)
        if [ $? -eq 0 ]; then
            echo -e "${GREEN}延迟: ${latency}s${NC}"
        else
            echo -e "${RED}连接失败${NC}"
        fi
    done
}

# 添加API限流测试脚本
echo -e "\n${YELLOW}创建API限流测试脚本...${NC}"
cat > test_api_rate_limit.py << 'EOF'
import asyncio
import aiohttp
import time
from datetime import datetime
import json

class BinanceAPITester:
    def __init__(self):
        self.base_url = 'https://api.binance.com'
        self.results = {}
        self.test_duration = 60  # 测试持续60秒
        self.target_rate = 1100  # 目标请求数/分钟
        
    async def test_single_ip(self, ip: str):
        print(f"\n开始测试 IP: {ip}")
        success_count = 0
        error_count = 0
        latencies = []
        
        async with aiohttp.ClientSession() as session:
            start_time = time.time()
            request_count = 0
            
            while time.time() - start_time < self.test_duration:
                try:
                    req_start = time.time()
                    async with session.get(
                        f"{self.base_url}/api/v3/time",
                        headers={'Content-Type': 'application/json'},
                        timeout=2
                    ) as response:
                        await response.json()
                        latency = (time.time() - req_start) * 1000
                        latencies.append(latency)
                        success_count += 1
                        
                        if success_count % 50 == 0:
                            current_rate = success_count / (time.time() - start_time) * 60
                            print(f"当前速率: {current_rate:.1f} 请求/分钟")
                            
                except Exception as e:
                    error_count += 1
                    print(f"请求失败: {str(e)}")
                
                request_count += 1
                if request_count % 10 == 0:
                    await asyncio.sleep(0.5)
        
        test_duration = time.time() - start_time
        requests_per_minute = success_count / test_duration * 60
        
        result = {
            'timestamp': datetime.now().isoformat(),
            'success_count': success_count,
            'error_count': error_count,
            'requests_per_minute': requests_per_minute,
            'avg_latency': sum(latencies) / len(latencies) if latencies else 0,
            'min_latency': min(latencies) if latencies else 0,
            'max_latency': max(latencies) if latencies else 0
        }
        
        print(f"\nIP {ip} 测试结果:")
        print(f"• 成功请求: {success_count}")
        print(f"• 失败请求: {error_count}")
        print(f"• 每分钟请求: {requests_per_minute:.1f}")
        print(f"• 平均延迟: {result['avg_latency']:.1f}ms")
        
        return result

    async def run_tests(self):
        import netifaces
        ips = []
        for iface in netifaces.interfaces():
            addrs = netifaces.ifaddresses(iface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    if addr['addr'] != '127.0.0.1':
                        ips.append(addr['addr'])
        
        print(f"检测到 {len(ips)} 个IP地址")
        for ip in ips:
            self.results[ip] = await self.test_single_ip(ip)
        
        with open('api_rate_limit_results.json', 'w') as f:
            json.dump(self.results, f, indent=2)
        
        self.analyze_results()
    
    def analyze_results(self):
        print("\n=== 测试结果分析 ===")
        sorted_results = sorted(
            self.results.items(),
            key=lambda x: x[1]['requests_per_minute'],
            reverse=True
        )
        
        print("\n推荐IP配置:")
        for ip, result in sorted_results[:3]:
            print(f"\nIP: {ip}")
            print(f"• 每分钟请求: {result['requests_per_minute']:.1f}")
            print(f"• 平均延迟: {result['avg_latency']:.1f}ms")
            print(f"• 成功率: {(result['success_count']/(result['success_count']+result['error_count'])*100):.1f}%")

async def main():
    tester = BinanceAPITester()
    await tester.run_tests()

if __name__ == '__main__':
    asyncio.run(main())
EOF

# 创建测试启动脚本
echo -e "\n${YELLOW}创建测试启动脚本...${NC}"
cat > run_rate_limit_test.sh << 'EOF'
#!/bin/bash
echo "=== 币安API限流测试 ==="
echo "测试条件:"
echo "• 目标: 1100请求/分钟/IP"
echo "• 持续: 60秒"
echo "• 监控: 延迟和成功率"
echo -e "\n开始测试..."
python3 test_api_rate_limit.py
EOF

chmod +x run_rate_limit_test.sh
chmod +x test_api_rate_limit.py

# 完成安装
echo -e "\n${GREEN}环境配置完成!${NC}"
echo -e "可以运行以下命令进行API测试:"
echo -e "${YELLOW}./run_rate_limit_test.sh${NC}"

echo """
====== 币安现货抢币工具 ======
1. 检查系统环境
2. 安装/更新依赖
3. 测试API延迟
4. API限流测试(1100/分钟)
5. 安装系统服务
...
"""
    read -p "请选择操作 (0-10): " choice
    case $choice in
        ...
        4)
            echo "开始API限流测试..."
            ./run_rate_limit_test.sh
            read -p "按回车键继续..."
            ;;
        ...
    esac

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
    pip3 install aiohttp requests websocket-client websockets ccxt pytz netifaces psutil prometheus_client aiofiles

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
if ! command -v aws &> /dev/null; then
    echo -e "${YELLOW}AWS CLI未安装，跳过AWS相关配置${NC}"
else
    # 获取实例ID
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

# 在主菜单之前添加所有函数定义
analyze_ip_characteristics() {
    local ip=$1
    echo -e "\n分析 IP: $ip 特征"
    
    # 1. 查询IP信息
    echo "1. IP基本信息:"
    if ! curl -s "https://ipinfo.io/$ip" | jq . 2>/dev/null; then
        echo "无法获取IP信息"
    fi
    
    # 2. 查询ASN信息
    echo -e "\n2. ASN信息:"
    if command -v whois &> /dev/null; then
        whois $ip | grep -i "origin\|route" || echo "无ASN信息"
    else
        echo "whois命令未安装"
    fi
    
    # 3. 检查TLS指纹
    echo -e "\n3. TLS指纹:"
    if command -v openssl &> /dev/null; then
        echo | openssl s_client -connect api.binance.com:443 -servername api.binance.com 2>/dev/null | openssl x509 -text | grep "Signature Algorithm" || echo "无法获取TLS指纹"
    else
        echo "openssl未安装"
    fi
    
    # 4. 测试不同User-Agent
    echo -e "\n4. 不同User-Agent测试:"
    local agents=(
        "Mozilla/5.0"
        "curl/7.68.0"
        "Python/3.8"
        "Binance-Java-SDK"
    )
    
    for agent in "${agents[@]}"; do
        echo -n "$agent: "
        if ! curl -s -w "%{time_total}\n" -o /dev/null \
             -H "User-Agent: $agent" \
             --interface $ip \
             https://api.binance.com/api/v3/time 2>/dev/null; then
            echo "请求失败"
                fi
            done
}

test_ip_identification() {
    local ip=$1
    echo -e "\n${YELLOW}===== 测试IP识别机制: $ip =====${NC}"
    local results=()
    local detection_points=0
    
    echo "1. 基础请求测试"
    # 1. 基础请求
    echo -n "  • 普通请求: "
    local base_latency=$(curl -s -w "%{time_total}" -o /dev/null --interface $ip https://api.binance.com/api/v3/time 2>/dev/null)
    if [ $? -eq 0 ]; then
        echo "${base_latency}s"
        results+=("基础请求成功")
    else
        echo "失败"
        results+=("基础请求失败")
        detection_points=$((detection_points + 1))
    fi
    
    echo -e "\n2. 请求头测试"
    # 2. User-Agent测试
    local agents=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        "Binance/2.0"
        "curl/7.68.0"
        "Python-urllib/3.8"
        "okhttp/4.9.0"
    )
    
    echo "  • User-Agent测试:"
    for agent in "${agents[@]}"; do
        echo -n "    - $agent: "
        local ua_latency=$(curl -s -w "%{time_total}" -o /dev/null \
            -H "User-Agent: $agent" \
            --interface $ip \
            https://api.binance.com/api/v3/time 2>/dev/null)
        if [ $? -eq 0 ]; then
            echo "${ua_latency}s"
            # 检查延迟差异
            if (( $(echo "$ua_latency > $base_latency * 1.5" | bc -l) )); then
                results+=("User-Agent '$agent' 可能被标记")
                detection_points=$((detection_points + 1))
            fi
        else
            echo "被拒绝"
            results+=("User-Agent '$agent' 被拒绝")
            detection_points=$((detection_points + 2))
        fi
    done
    
    echo -e "\n3. 请求频率测试"
    # 3. 频率测试
    echo "  • 快速请求测试:"
    local success=0
    local total=10
    for ((i=1; i<=total; i++)); do
        if curl -s --interface $ip https://api.binance.com/api/v3/time >/dev/null 2>&1; then
            success=$((success + 1))
        fi
        sleep 0.1
    done
    echo "    - 成功率: $success/$total"
    if [ $success -lt $total ]; then
        results+=("检测到频率限制")
        detection_points=$((detection_points + 2))
    fi
    
    echo -e "\n4. 协议特征测试"
    # 4. TLS指纹测试
    echo -n "  • TLS指纹: "
    if openssl s_client -connect api.binance.com:443 -servername api.binance.com \
        -cipher 'ECDHE-RSA-AES128-GCM-SHA256' >/dev/null 2>&1; then
        echo "标准"
    else
        echo "异常"
        results+=("TLS指纹异常")
        detection_points=$((detection_points + 2))
    fi
    
    # 5. 代理检测测试
    echo -e "\n5. 代理特征测试"
    echo -n "  • 代理头部检测: "
    local proxy_test=$(curl -s -w "%{http_code}" -o /dev/null \
        -H "X-Forwarded-For: 8.8.8.8" \
        -H "Via: 1.1 proxy" \
        --interface $ip \
        https://api.binance.com/api/v3/time 2>/dev/null)
    if [ "$proxy_test" = "200" ]; then
        echo "未检测"
    else
        echo "被检测 (状态码: $proxy_test)"
        results+=("代理特征被检测")
        detection_points=$((detection_points + 2))
    fi
    
    # 分析结果
    echo -e "\n${YELLOW}===== 分析结果 =====${NC}"
    echo "检测分数: $detection_points (0-3:低风险 4-7:中风险 8+:高风险)"
    echo -e "\n发现的问题:"
    for result in "${results[@]}"; do
        echo "• $result"
    done
    
    # 提供建议
    echo -e "\n${GREEN}===== 伪装建议 =====${NC}"
    if [ $detection_points -ge 8 ]; then
        echo "高风险IP，建议采取以下措施："
        echo "1. 使用动态IP轮换策略"
        echo "2. 实现完整的浏览器指纹模拟"
        echo "3. 使用代理链路混淆真实IP"
        echo "4. 考虑使用住宅IP或数据中心IP混合"
    elif [ $detection_points -ge 4 ]; then
        echo "中等风险IP，建议采取以下措施："
        echo "1. 优化请求头伪装"
        echo "2. 实现基本的浏览器特征模拟"
        echo "3. 控制请求频率在安全范围"
        echo "4. 定期轮换User-Agent"
    else
        echo "低风险IP，建议采取以下措施："
        echo "1. 保持当前配置"
        echo "2. 定期监控IP状态"
        echo "3. 建立备用IP池"
    fi
    
    # 具体的伪装方案
    echo -e "\n${YELLOW}推荐伪装方案：${NC}"
    cat << EOF
1. 请求头优化：
   • User-Agent: $(echo "${agents[0]}")
   • Accept: text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8
   • Accept-Language: en-US,en;q=0.5
   • Accept-Encoding: gzip, deflate, br
   • Connection: keep-alive
   • Upgrade-Insecure-Requests: 1

2. TLS配置：
   • 使用主流密码套件
   • 启用SNI
   • 使用标准TLS 1.2/1.3协议

3. 请求策略：
   • 随机化请求间隔 ($(echo "scale=2; $base_latency * 1.5" | bc) - $(echo "scale=2; $base_latency * 2.5" | bc)秒)
   • 实现退避算法
   • 错误请求限制在5%以内

4. IP使用建议：
   • 每个IP限制在$(echo "scale=0; 1000 * $base_latency" | bc)请求/分钟
   • 超过限制自动切换IP
   • 建立IP信誉度跟踪系统
EOF
    
    echo -e "\n${YELLOW}是否需要生成伪装配置文件？(y/n)${NC}"
    read -p "请选择: " gen_config
    if [[ $gen_config == "y" || $gen_config == "Y" ]]; then
        cat > "ip_disguise_${ip}.conf" << EOF
# 币安API访问伪装配置
# 生成时间: $(date)
# IP: $ip

[请求头]
User-Agent = ${agents[0]}
Accept = text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8
Accept-Language = en-US,en;q=0.5
Accept-Encoding = gzip, deflate, br
Connection = keep-alive

[频率控制]
最大请求数/分钟 = $(echo "scale=0; 1000 * $base_latency" | bc)
基础延迟 = $base_latency
随机延迟范围 = 0.1-0.3

[TLS配置]
版本 = TLS 1.2, TLS 1.3
首选密码套件 = ECDHE-RSA-AES128-GCM-SHA256
启用SNI = true

[IP策略]
检测分数 = $detection_points
风险等级 = $([ $detection_points -ge 8 ] && echo "高" || [ $detection_points -ge 4 ] && echo "中" || echo "低")
建议轮换间隔 = $([ $detection_points -ge 8 ] && echo "1小时" || [ $detection_points -ge 4 ] && echo "4小时" || echo "12小时")
EOF
        echo -e "${GREEN}配置文件已生成: ip_disguise_${ip}.conf${NC}"
    fi
}

smart_ip_selection() {
    local test_duration=60  # 测试时长(秒)
    local test_interval=5   # 测试间隔(秒)
    declare -A results
    
    # 获取所有可用IP
    local ips=($(ip -4 addr show | grep inet | awk '{print $2}' | cut -d/ -f1 | grep -v "127.0.0.1"))
    
    echo "开始智能IP测试..."
    echo "测试持续时间: ${test_duration}秒"
    echo "测试间隔: ${test_interval}秒"
    
    # 在测试时长内循环测试所有IP
    local start_time=$(date +%s)
    while (( $(date +%s) - start_time < test_duration )); do
        for ip in "${ips[@]}"; do
            echo -n "测试 $ip: "
            if latency=$(curl -s -w "%{time_total}" -o /dev/null --interface $ip https://api.binance.com/api/v3/time 2>/dev/null); then
                echo "${latency}s"
                results[$ip]+=" $latency"
            else
                echo "失败"
            fi
        done
        sleep $test_interval
    done
    
    # 分析结果
    echo -e "\n=== IP性能分析 ==="
    for ip in "${ips[@]}"; do
        echo -e "\nIP: $ip"
        if [ ! -z "${results[$ip]}" ]; then
            # 使用awk计算平均值和标准差
            echo "${results[$ip]}" | awk '
            BEGIN {sum=0; count=0}
            {
                for(i=1;i<=NF;i++) {
                    sum+=$i; 
                    sumsq+=$i*$i;
                    count++
                }
            }
            END {
                avg=sum/count;
                stddev=sqrt(sumsq/count - (sum/count)**2);
                printf "- 平均延迟: %.3fs\n- 延迟波动: %.3fs\n- 样本数量: %d\n", 
                       avg, stddev, count
            }'
        else
            echo "- 无有效数据"
        fi
    done
}

rotate_ip_strategy() {
    local ips=("$@")
    if [ ${#ips[@]} -eq 0 ]; then
        echo "没有可用的IP地址"
        return 1
    fi
    
    local current_index=0
    local rotation_interval=300  # 5分钟轮换一次
    
    echo "开始IP轮换测试..."
    echo "轮换间隔: ${rotation_interval}秒"
    echo "可用IP数量: ${#ips[@]}"
    
    # 显示所有可用IP
    echo -e "\n可用IP列表:"
    for ((i=0; i<${#ips[@]}; i++)); do
        echo "$((i+1)). ${ips[i]}"
    done
    
    echo -e "\n按Ctrl+C停止测试\n"
    
    while true; do
        # 获取当前IP
        local current_ip=${ips[$current_index]}
        
        # 测试当前IP性能
        echo -n "使用IP: $current_ip - "
        if perf=$(curl -s -w "%{time_total}" -o /dev/null --interface $current_ip https://api.binance.com/api/v3/time 2>/dev/null); then
            echo "延迟: ${perf}s"
            
            # 如果性能较差，立即切换
            if (( $(echo "$perf > 0.1" | bc -l) )); then
                echo "性能不佳，切换IP..."
                current_index=$(( (current_index + 1) % ${#ips[@]} ))
                continue
            fi
        else
            echo "请求失败，切换到下一个IP"
            current_index=$(( (current_index + 1) % ${#ips[@]} ))
            continue
        fi
        
        echo "等待${rotation_interval}秒后轮换..."
        sleep $rotation_interval
        current_index=$(( (current_index + 1) % ${#ips[@]} ))
    done
}

# 添加一键测试函数
run_comprehensive_test() {
    echo -e "${GREEN}====== 开始全面IP测试与分析 ======${NC}"
    local report_file="ip_analysis_report_$(date +%Y%m%d_%H%M%S).txt"
    
    # 重定向所有输出到报告文件
    {
        echo "币安API访问优化报告"
        echo "生成时间: $(date)"
        echo "系统信息: $(uname -a)"
        echo "======================="
        
        # 1. 获取所有IP
        echo -e "\n[1/5] 检测可用IP..."
        local ips=($(ip -4 addr show | grep inet | awk '{print $2}' | cut -d/ -f1 | grep -v "127.0.0.1"))
        echo "发现 ${#ips[@]} 个IP地址"
        
        # 2. 快速延迟测试
        echo -e "\n[2/5] 执行延迟测试..."
        declare -A latency_results
        for ip in "${ips[@]}"; do
            local avg_latency=0
            local success=0
            for ((i=1; i<=3; i++)); do
                if latency=$(curl -s -w "%{time_total}" -o /dev/null --interface $ip https://api.binance.com/api/v3/time 2>/dev/null); then
                    avg_latency=$(echo "$avg_latency + $latency" | bc)
                    success=$((success + 1))
                fi
            done
            if [ $success -gt 0 ]; then
                latency_results[$ip]=$(echo "scale=3; $avg_latency / $success" | bc)
            else
                latency_results[$ip]="fail"
            fi
        done
        
        # 3. IP识别测试
        echo -e "\n[3/5] 执行IP识别测试..."
        declare -A detection_scores
        for ip in "${ips[@]}"; do
            if [ "${latency_results[$ip]}" != "fail" ]; then
                echo -e "\n测试 IP: $ip"
                detection_score=0
                
                # 基础请求测试
                base_latency=${latency_results[$ip]}
                
                # User-Agent测试
    local agents=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                    "Binance/2.0"
        "curl/7.68.0"
                    "Python-urllib/3.8"
    )
    
    for agent in "${agents[@]}"; do
                    if ! curl -s -H "User-Agent: $agent" --interface $ip https://api.binance.com/api/v3/time >/dev/null 2>&1; then
                        detection_score=$((detection_score + 1))
                    fi
                done
                
                # 频率测试
                local freq_success=0
                for ((i=1; i<=5; i++)); do
                    if curl -s --interface $ip https://api.binance.com/api/v3/time >/dev/null 2>&1; then
                        freq_success=$((freq_success + 1))
                    fi
                    sleep 0.1
                done
                if [ $freq_success -lt 5 ]; then
                    detection_score=$((detection_score + 2))
                fi
                
                detection_scores[$ip]=$detection_score
            fi
        done
        
        # 4. 限流测试
        echo -e "\n[4/5] 执行限流测试..."
        declare -A rate_limits
        for ip in "${ips[@]}"; do
            if [ "${latency_results[$ip]}" != "fail" ]; then
                echo "测试IP: $ip"
                
                # 创建临时Python测试脚本
                cat > "temp_rate_test.py" << 'EOF'
import asyncio
import aiohttp
import time
from datetime import datetime
import sys
import json
from collections import deque

class RateTest:
    def __init__(self, ip):
        self.ip = ip
        self.success_count = 0
        self.error_count = 0
        self.latencies = []
        self.error_types = {}
        self.rate_history = deque(maxlen=120)  # 存储2分钟的历史数据
        self.test_duration = 120  # 测试持续120秒
        self.concurrent_tasks = 50
        self.log_file = f"rate_test_{ip}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        
    def log(self, message):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log_message = f"[{timestamp}] {message}"
        print(log_message)
        with open(self.log_file, "a") as f:
            f.write(log_message + "\n")

    async def test_rate_limit(self):
        self.log(f"开始测试IP: {self.ip}")
        self.log(f"测试时长: {self.test_duration}秒")
        self.log(f"并发任务数: {self.concurrent_tasks}")
        
        start_time = time.time()
        last_report_time = start_time
        
        async with aiohttp.ClientSession() as session:
            tasks = []
            for i in range(self.concurrent_tasks):
                tasks.append(asyncio.create_task(
                    self.make_requests(session, start_time, i)
                ))
            
            # 监控任务
            tasks.append(asyncio.create_task(
                self.monitor_progress(start_time)
            ))
            
            await asyncio.gather(*tasks)
        
        # 生成最终报告
        self.generate_final_report()

    async def make_requests(self, session, start_time, task_id):
        while time.time() - start_time < self.test_duration:
            try:
                req_start = time.time()
                async with session.get(
                    'https://api.binance.com/api/v3/time',
                    headers={
                        'Content-Type': 'application/json',
                        'User-Agent': f'RateTest/1.0 Task/{task_id}',
                    },
                    timeout=2
                ) as response:
                    await response.json()
                    latency = (time.time() - req_start) * 1000
                    self.latencies.append(latency)
                    self.success_count += 1
                    
            except Exception as e:
                self.error_count += 1
                error_type = type(e).__name__
                self.error_types[error_type] = self.error_types.get(error_type, 0) + 1
            
            await asyncio.sleep(0.01)  # 避免过度请求

    async def monitor_progress(self, start_time):
        last_success = 0
        last_check_time = start_time
        
        while time.time() - start_time < self.test_duration:
            await asyncio.sleep(1)  # 每秒更新一次状态
            current_time = time.time()
            time_passed = current_time - last_check_time
            success_delta = self.success_count - last_success
            
            # 计算当前速率
            current_rate = success_delta / time_passed * 60
            self.rate_history.append(current_rate)
            
            # 计算平均延迟
            recent_latencies = self.latencies[-1000:] if self.latencies else []
            avg_latency = sum(recent_latencies) / len(recent_latencies) if recent_latencies else 0
            
            # 记录状态
            self.log(
                f"状态更新:\n"
                f"  时间: {int(current_time - start_time)}s/{self.test_duration}s\n"
                f"  当前速率: {current_rate:.1f} 请求/分钟\n"
                f"  平均延迟: {avg_latency:.2f}ms\n"
                f"  成功请求: {self.success_count}\n"
                f"  失败请求: {self.error_count}\n"
                f"  成功率: {(self.success_count/(self.success_count+self.error_count)*100):.2f}%"
            )
            
            last_success = self.success_count
            last_check_time = current_time

    def generate_final_report(self):
        test_duration = len(self.rate_history)
        avg_rate = sum(self.rate_history) / len(self.rate_history) if self.rate_history else 0
        max_rate = max(self.rate_history) if self.rate_history else 0
        min_rate = min(self.rate_history) if self.rate_history else 0
        
        report = {
            "ip": self.ip,
            "test_duration": test_duration,
            "total_requests": self.success_count + self.error_count,
            "success_count": self.success_count,
            "error_count": self.error_count,
            "success_rate": (self.success_count/(self.success_count+self.error_count)*100) if (self.success_count+self.error_count) > 0 else 0,
            "average_rate": avg_rate,
            "max_rate": max_rate,
            "min_rate": min_rate,
            "rate_stability": (max_rate - min_rate) / avg_rate if avg_rate > 0 else 0,
            "average_latency": sum(self.latencies) / len(self.latencies) if self.latencies else 0,
            "error_types": self.error_types,
            "rate_history": list(self.rate_history)
        }
        
        # 保存详细报告
        report_file = f"rate_test_report_{self.ip}.json"
        with open(report_file, "w") as f:
            json.dump(report, f, indent=2)
        
        # 输出摘要
        self.log("\n========== 测试报告 ==========")
        self.log(f"IP地址: {self.ip}")
        self.log(f"测试时长: {test_duration}秒")
        self.log(f"总请求数: {report['total_requests']}")
        self.log(f"成功请求: {report['success_count']}")
        self.log(f"失败请求: {report['error_count']}")
        self.log(f"成功率: {report['success_rate']:.2f}%")
        self.log(f"平均速率: {report['average_rate']:.1f} 请求/分钟")
        self.log(f"最大速率: {report['max_rate']:.1f} 请求/分钟")
        self.log(f"最小速率: {report['min_rate']:.1f} 请求/分钟")
        self.log(f"速率稳定性: {report['rate_stability']:.2f}")
        self.log(f"平均延迟: {report['average_latency']:.2f}ms")
        self.log("\n错误类型统计:")
        for error_type, count in report['error_types'].items():
            self.log(f"  {error_type}: {count}")
        self.log("\n详细报告已保存到: " + report_file)
        
        # 返回平均速率用于主程序
        print(f"{report['average_rate']}")

async def main():
    ip = sys.argv[1]
    tester = RateTest(ip)
    await tester.test_rate_limit()

if __name__ == '__main__':
    asyncio.run(main())
EOF
        
        # 运行Python测试脚本
        echo -e "\n开始测试IP: $ip"
        rate=$(python3 temp_rate_test.py "$ip")
        rate_limits[$ip]=${rate%.*}  # 取整数部分
        
        echo -e "\n${GREEN}IP $ip 测试完成${NC}"
        echo "平均请求率: ${rate_limits[$ip]} 请求/分钟"
        echo "详细日志已保存到 rate_test_${ip}_*.log"
        echo "完整报告已保存到 rate_test_report_${ip}.json"
        echo -e "----------------------------------------\n"
        
        # 清理临时文件
        rm temp_rate_test.py
        fi
    done
    
# ... 保持后面的代码不变 ...
} | tee "$report_file"
    
    echo -e "\n${GREEN}测试完成！详细报告已保存到: $report_file${NC}"
    echo -e "${YELLOW}建议查看报告了解详细的优化建议${NC}"
}

# 主菜单
while true; do
    echo """
====== 币安现货抢币工具 ======
1. 检查系统环境
2. 安装/更新依赖
3. 测试API延迟
4. API限流测试(1100/分钟)
5. 安装系统服务
6. 立即运行程序
7. 查看运行日志
8. 同步系统时间
9. IP特征分析与测试
0. 退出
============================
"""
    read -p "请选择操作 (0-9): " choice
    case $choice in
        1)
            echo "检查系统环境..."
            check_dependencies
            ;;
        2)
            echo "安装/更新依赖..."
            install_dependencies
            ;;
        3)
            echo "测试API延迟..."
            test_binance_latency
            ;;
        4)
            echo "开始API限流测试..."
            if [ ! -f "test_api_rate_limit.py" ]; then
                echo -e "${RED}未找到测试脚本，重新创建...${NC}"
                # 创建API限流测试脚本
                cat > test_api_rate_limit.py << 'EOF'
# ... Python脚本内容 ...
EOF
                
                # 创建启动脚本
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
            fi
            
            # 检查Python依赖
            echo "检查Python依赖..."
            pip3 install -q aiohttp netifaces || {
                echo -e "${RED}安装依赖失败${NC}"
                read -p "按回车键继续..."
                continue
            }
            
            # 运行测试
            echo -e "\n${GREEN}开始运行API限流测试...${NC}"
            ./run_rate_limit_test.sh
            
            # 显示结果
            if [ -f "api_rate_limit_results.json" ]; then
                echo -e "\n${GREEN}测试完成！详细结果已保存到 api_rate_limit_results.json${NC}"
                echo "是否查看结果摘要？(y/n)"
                read -p "请选择: " view_choice
                if [[ $view_choice == "y" || $view_choice == "Y" ]]; then
                    jq -r '.[] | "IP: \(.ip)\n请求率: \(.requests_per_minute)次/分钟\n平均延迟: \(.avg_latency)ms\n成功率: \(.success_rate)%\n"' api_rate_limit_results.json
                fi
            else
                echo -e "${RED}测试可能未完成或发生错误${NC}"
            fi
            
            read -p "按回车键继续..."
            ;;
        5)
            echo "安装系统服务..."
            install_service
            ;;
        6)
            echo "立即运行程序..."
            if [ -f "binance_sniper.py" ]; then
                python3 binance_sniper.py
            else
                echo -e "${RED}未找到主程序文件${NC}"
                echo "是否下载主程序？(y/n)"
                read -p "请选择: " download_choice
                if [[ $download_choice == "y" || $download_choice == "Y" ]]; then
                    download_main_program
                    if [ $? -eq 0 ]; then
                        python3 binance_sniper.py
                    fi
                fi
            fi
            ;;
        7)
            echo "查看运行日志..."
            if [ -d "logs" ]; then
                tail -f logs/binance_sniper.log
            else
                echo -e "${RED}未找到日志目录${NC}"
            fi
            ;;
        8)
            echo "同步系统时间..."
            sync_system_time
            ;;
        9)
            while true; do
                echo """
====== IP特征分析与测试 ======
1. 分析所有IP特征
2. 测试IP识别机制
3. 智能IP测试
4. IP轮换测试
5. 一键优化测试
6. 返回主菜单
============================
"""
                read -p "请选择测试类型 (1-6): " test_choice
                case $test_choice in
                    1)
                        echo "开始分析所有IP特征..."
                        for ip in $(ip -4 addr show | grep inet | awk '{print $2}' | cut -d/ -f1 | grep -v "127.0.0.1"); do
                            analyze_ip_characteristics "$ip"
                        done
                        ;;
                    2)
                        echo "开始测试IP识别机制..."
                        for ip in $(ip -4 addr show | grep inet | awk '{print $2}' | cut -d/ -f1 | grep -v "127.0.0.1"); do
                            test_ip_identification "$ip"
                        done
                        ;;
                    3)
                        echo "开始智能IP测试..."
                        smart_ip_selection
                        ;;
                    4)
                        echo "开始IP轮换测试..."
                        IPS=($(ip -4 addr show | grep inet | awk '{print $2}' | cut -d/ -f1 | grep -v "127.0.0.1"))
                        rotate_ip_strategy "${IPS[@]}"
                        ;;
                    5)
                        run_comprehensive_test
                        ;;
                    6)
                        break
                        ;;
                    *)
                        echo "无效选项"
                        ;;
                esac
                read -p "按回车键继续..."
            done
            ;;
        0)
            echo "退出程序"
            exit 0
            ;;
        *)
            echo -e "${RED}无效选项${NC}"
            sleep 1
            ;;
    esac
done

#!/bin/bash

# 定义退出码
SUCCESS=0           # 成功退出
ERR_DOWNLOAD=1      # 下载失败
ERR_INSTALL=2      # 安装失败
ERR_DEPENDENCY=3    # 依赖检查失败
ERR_RUNTIME=4      # 运行时错误
ERR_SYSTEM=5       # 系统要求不满足

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
    
    # 检查内存
    total_mem=$(free -m | awk '/^Mem:/{print $2}')
    if [ $total_mem -lt 2048 ]; then  # 小于2GB
        echo "警告: 内存可能不足 (建议至少2GB)"
        return 1
    else
        echo "内存检查通过 ✓ (${total_mem}MB)"
    fi
    
    # 检查CPU
    cpu_cores=$(nproc)
    if [ $cpu_cores -lt 2 ]; then
        echo "警告: CPU核心数可能不足 (建议至少2核)"
        return 1
    else
        echo "CPU检查通过 ✓ ($cpu_cores 核)"
    fi
    
    # 检查磁盘空间
    free_space=$(df -m . | awk 'NR==2 {print $4}')
    if [ $free_space -lt 1024 ]; then  # 小于1GB
        echo "警告: 磁盘空间可能不足 (建议至少1GB)"
        return 1
    else
        echo "磁盘空间检查通过 ✓ (${free_space}MB)"
    fi
    
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
    check_python_version || return $ERR_DEPENDENCY
    
    # 检查系统资源
    check_system_resources || return $ERR_SYSTEM
    
    # 检查网络连接
    check_network || return $ERR_SYSTEM
    
    local need_install=false
    
    # 检查Python依赖
    python3 -c "
try:
    import ccxt
    print('CCXT: 已安装 ✓')
except ImportError:
    print('CCXT: 未安装 ✗')
    exit(1)

try:
    import websocket
    print('WebSocket-Client: 已安装 ✓')
except ImportError:
    print('WebSocket-Client: 未安装 ✗')
    exit(2)

try:
    import websockets
    print('WebSockets: 已安装 ✓')
except ImportError:
    print('WebSockets: 未安装 ✗')
    exit(3)

try:
    import requests
    print('Requests: 已安装 ✓')
except ImportError:
    print('Requests: 未安装 ✗')
    exit(4)

try:
    import pytz
    print('PyTZ: 已安装 ✓')
except ImportError:
    print('PyTZ: 未安装 ✗')
    exit(5)

try:
    import aiohttp
    print('AIOHTTP: 已安装 ✓')
except ImportError:
    print('AIOHTTP: 未安装 ✗')
    exit(6)

try:
    import prometheus_client
    print('Prometheus-Client: 已安装 ✓')
except ImportError:
    print('Prometheus-Client: 未安装 ✗')
    exit(7)

try:
    import psutil
    print('PSUtil: 已安装 ✓')
except ImportError:
    print('PSUtil: 未安装 ✗')
    exit(8)

try:
    import numba
    print('Numba: 已安装 ✓')
except ImportError:
    print('Numba: 未安装 ✗')
    exit(9)

try:
    import numpy
    print('NumPy: 已安装 ✓')
except ImportError:
    print('NumPy: 未安装 ✗')
    exit(10)
"
    local check_result=$?
    
    if [ $check_result -ne 0 ]; then
        echo "发现未安装的依赖，是否现在安装? (y/n)"
        read -p "请选择: " install_choice
        if [[ $install_choice == "y" || $install_choice == "Y" ]]; then
            install_dependencies
            echo "依赖安装完成，重新检查..."
            check_dependencies
            return
        fi
    fi
    
    # 检查配置目录
    if [ ! -d "config" ]; then
        mkdir -p config
    fi
    
    # 检查日志目录
    if [ ! -d "logs" ]; then
        mkdir -p logs
    fi
    
    # 检查配置文件
    if [ -f "config/config.ini" ]; then
        echo "配置文件: 已存在 ✓"
    else
        echo "配置文件: 未创建 ✗"
        echo "配置文件将在首次运行时自动创建"
    fi
    
    # 检查主程序
    if [ -f "binance_sniper.py" ]; then
        echo "主程序: 已存在 ✓"
    else
        echo "主程序: 未下载 ✗"
        echo "是否现在下载主程序? (y/n)"
        read -p "请选择: " download_choice
        if [[ $download_choice == "y" || $download_choice == "Y" ]]; then
            download_main_program
        fi
    fi
    
    echo "依赖检查完成!"
    read -p "按回车键继续..."
}

# 下载主程序函数
download_main_program() {
    echo "正在下载主程序..."
    curl -o binance_sniper.py https://raw.githubusercontent.com/fcrre26/sniper/refs/heads/main/binance_sniper.py
    if [ $? -ne 0 ]; then
        echo "下载失败，请检查网络连接"
        return $ERR_DOWNLOAD
    fi
    chmod +x binance_sniper.py
    echo "主程序下载成功"
    return $SUCCESS
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
    pip install ccxt                # 加密货币交易所API
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
    
    # 创建服务文件
    sudo cat > /etc/systemd/system/binance-sniper.service << EOF
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

    # 创建日志目录
    sudo mkdir -p /var/log/binance-sniper
    sudo chown $USER:$USER /var/log/binance-sniper

    # 创建日志轮转配置
    sudo cat > /etc/logrotate.d/binance-sniper << EOF
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

    # 重新加载systemd配置
    sudo systemctl daemon-reload

    # 启用服务
    sudo systemctl enable binance-sniper.service

    echo """
=== 服务安装完成 ===
使用以下命令控制服务:
- 启动: sudo systemctl start binance-sniper
- 停止: sudo systemctl stop binance-sniper
- 状态: sudo systemctl status binance-sniper
- 查看日志: tail -f /var/log/binance-sniper/output.log
- 查看错误: tail -f /var/log/binance-sniper/error.log
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

# 主菜单循环
while true; do
    clear
    echo """
====== 币安现货抢币工具 ======
1. 检查系统环境
2. 安装/更新依赖
3. 测试API延迟
4. 安装系统服务
5. 立即运行程序
6. 后台运行程序
7. 查看运行日志
0. 退出安装脚本
============================
"""
    read -p "请选择操作 (0-7): " choice
    case $choice in
        1)
            check_dependencies
            ;;
        2)
            install_dependencies
            ;;
        3)
            test_binance_latency
            ;;
        4)
            install_service
            ;;
        5)
            if [ ! -f "binance_sniper.py" ]; then
                echo "错误: 主程序不存在"
                read -p "按回车键继续..."
                continue
            fi
            python3 binance_sniper.py
            ;;
        6)
            if [ ! -f "binance_sniper.py" ]; then
                echo "错误: 主程序不存在"
                read -p "按回车键继续..."
                continue
            fi
            echo """
=== 后台运行说明 ===
1. 程序会在前台启动，您可以进行设置
2. 即使SSH意外断开，程序也会继续在后台运行
3. 重新连接服务器后，使用 screen -r sniper 可以重新查看程序
4. 主动退出请在程序中选择0退出
"""
            read -p "按回车键开始运行..."
            screen -RR sniper python3 binance_sniper.py
            ;;
        7)
            if [ -f "/var/log/binance-sniper/output.log" ]; then
                tail -f /var/log/binance-sniper/output.log
            else
                echo "错误: 日志文件不存在"
                read -p "按回车键继续..."
            fi
            ;;
        0)
            echo "退出安装脚本"
            exit $SUCCESS
            ;;
        *)
            echo "无效选项，请重新选择"
            sleep 1
            ;;
    esac
done

exit $SUCCESS

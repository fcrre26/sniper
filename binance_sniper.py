#!/bin/bash

# 定义退出码
SUCCESS=0           # 成功退出
ERR_DOWNLOAD=1      # 下载失败
ERR_INSTALL=2      # 安装失败
ERR_DEPENDENCY=3    # 依赖检查失败
ERR_RUNTIME=4      # 运行时错误

echo "====== 币安现货抢币工具安装脚本 ======"

# 检查依赖函数
check_dependencies() {
    echo "正在检查依赖..."
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
    print('WebSocket: 已安装 ✓')
except ImportError:
    print('WebSocket: 未安装 ✗')
    exit(2)
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
    
    # 检查配置文件
    if [ -f "config.ini" ]; then
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
    pip install ccxt
    pip install websocket-client
    pip install requests
    pip install pytz

    # 检查安装结果
    echo -e "\n====== 依赖安装完成 ======"
    echo "已安装的包版本:"
    pip freeze | grep -E "ccxt|websocket-client|requests|pytz"

    echo -e "\n如果看到以上包的版本信息，说明安装成功"
    echo "====== 安装完成 ======"
    
    return $SUCCESS
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
1. 检查依赖
2. 立即运行程序
3. 测试API延迟
0. 退出安装脚本
============================
"""
    read -p "请选择操作 (0-3): " choice
    case $choice in
        1)
            check_dependencies
            ;;
        2)
            if [ ! -f "binance_sniper.py" ]; then
                echo "主程序不存在，正在安装依赖..."
                install_dependencies
                if [ $? -ne 0 ]; then
                    echo "依赖安装失败，请重试"
                    exit $ERR_INSTALL
                fi
            fi
            echo "启动币安抢币工具..."
            python3 binance_sniper.py
            if [ $? -ne 0 ]; then
                echo "程序运行失败"
                exit $ERR_RUNTIME
            fi
            exit $SUCCESS
            ;;
        3)
            test_binance_latency
            ;;
        0)
            echo "退出安装脚本"
            echo "您可以稍后通过运行 'python3 binance_sniper.py' 启动程序"
            exit $SUCCESS
            ;;
        *)
            echo "无效选项，请重新选择"
            sleep 1
            ;;
    esac
done

exit $SUCCESS

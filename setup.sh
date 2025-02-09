#!/bin/bash

echo "====== 币安现货抢币工具安装脚本 ======"

# 下载主程序
echo "正在下载主程序..."
curl -o binance_sniper.py https://raw.githubusercontent.com/fcrre26/sniper/refs/heads/main/binance_sniper.py
if [ $? -ne 0 ]; then
    echo "下载失败，请检查网络连接"
    exit 1
fi
echo "主程序下载成功"

# 检查pip是否安装
if ! command -v pip &> /dev/null; then
    echo "正在安装pip..."
    curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
    python get-pip.py
    rm get-pip.py
fi

# 安装基本依赖
echo "安装Python依赖..."
pip install ccxt pandas numpy

# 安装TA-Lib
echo "安装TA-Lib..."
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    # Linux
    sudo apt-get update
    sudo apt-get install -y build-essential
    sudo apt-get install -y python3-dev
    sudo apt-get install -y ta-lib
    pip install TA-Lib
elif [[ "$OSTYPE" == "darwin"* ]]; then
    # Mac OS
    brew install ta-lib
    pip install TA-Lib
else
    echo "请手动安装TA-Lib，参考: https://github.com/mrjbq7/ta-lib"
fi

# 检查安装结果
echo "检查依赖安装..."
python3 -c "
try:
    import ccxt
    import pandas
    import numpy
    import talib
    print('所有依赖安装成功!')
except ImportError as e:
    print(f'依赖安装失败: {str(e)}')
"

# 设置执行权限
chmod +x binance_sniper.py

echo """
====== 安装完成 ======
使用方法:
1. 运行程序: python3 binance_sniper.py
2. 首次运行需要设置API密钥
3. 设置抢购策略后即可开始使用

注意事项:
- 请确保API密钥有现货交易权限
- 建议先小额测试
- 使用前请仔细阅读说明
"""

# 询问是否立即运行
while true; do
    echo """
请选择操作:
1. 立即运行程序
2. 退出安装脚本
"""
    read -p "请输入选项 (1-2): " choice
    case $choice in
        1)
            echo "启动币安抢币工具..."
            python3 binance_sniper.py
            break
            ;;
        2)
            echo "安装完成，退出脚本"
            echo "您可以稍后通过运行 'python3 binance_sniper.py' 启动程序"
            break
            ;;
        *)
            echo "无效选项，请重新选择"
            ;;
    esac
done

exit 0 

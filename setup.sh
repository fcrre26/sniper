#!/bin/bash

echo "开始安装依赖..."

# 检查pip是否安装
if ! command -v pip &> /dev/null; then
    echo "正在安装pip..."
    curl https://bootstrap.pypa.io/get-pip.py -o get-pip.py
    python get-pip.py
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

echo "安装完成!"

# 创建虚拟环境
python -m venv venv
source venv/bin/activate

# 安装依赖
pip install --upgrade pip
pip install -r requirements.txt 

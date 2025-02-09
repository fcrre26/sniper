#!/bin/bash

# 安装系统依赖
if [ "$(uname)" == "Darwin" ]; then
    # MacOS
    brew install ta-lib
elif [ "$(expr substr $(uname -s) 1 5)" == "Linux" ]; then
    # Linux
    wget http://prdownloads.sourceforge.net/ta-lib/ta-lib-0.4.0-src.tar.gz
    tar -xvf ta-lib-0.4.0-src.tar.gz
    cd ta-lib/
    ./configure --prefix=/usr
    make
    sudo make install
    cd ..
    rm -rf ta-lib-0.4.0-src.tar.gz ta-lib/
fi

# 创建虚拟环境
python -m venv venv
source venv/bin/activate

# 安装依赖
pip install --upgrade pip
pip install -r requirements.txt 

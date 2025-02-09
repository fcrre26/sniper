import ccxt
import time
from datetime import datetime
import logging
import json
import os
import pandas as pd
import numpy as np
import talib
import threading
from binance_sniper_config import ConfigManager
import sys
from logging.handlers import RotatingFileHandler

def setup_logger():
    """配置日志"""
    # 创建logger
    logger = logging.getLogger('BinanceSniper')
    logger.setLevel(logging.DEBUG)
    
    # 创建格式化器
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
    )
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    # 文件处理器 (按大小轮转)
    file_handler = RotatingFileHandler(
        'binance_sniper.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    # 添加处理器
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger

# 创建logger实例
logger = setup_logger()

# 添加依赖检查
try:
    import talib
except ImportError:
    logger.error("请安装 TA-Lib: pip install ta-lib")
    raise

try:
    import pandas as pd
    import numpy as np
except ImportError:
    logger.error("请安装 pandas 和 numpy: pip install pandas numpy")
    raise

class BinanceSniper:
    def __init__(self, config: ConfigManager):
        """初始化币安抢币工具"""
        # 获取两组API密钥
        trade_api_key, trade_api_secret = config.get_trade_api_keys()
        query_api_key, query_api_secret = config.get_query_api_keys()
        
        # 交易专用客户端
        self.trade_client = ccxt.binance({
            'apiKey': trade_api_key,
            'secret': trade_api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'adjustForTimeDifference': True
            }
        })
        
        # 查询专用客户端
        self.query_client = ccxt.binance({
            'apiKey': query_api_key,
            'secret': query_api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'adjustForTimeDifference': True
            }
        })
        
        self.config = config
        self.symbol = None
        self.amount = None
        self.price = None
        self.history = []
        self.stop_loss = 0.02
        self.take_profit = 0.05
        self.max_price_deviation = 0.05
        self.max_price_limit = None  # 最高可接受价格
        self.reference_price = None  # 参考价格
        self.max_slippage = 0.1         # 最大允许滑点 (10%)
        self.min_depth_requirement = 5   # 最小深度要求（至少5个卖单）
        self.max_price_gap = 0.2        # 最大价格差距（相邻卖单间）
        self.ignore_outliers = True     # 是否忽略异常值
        
        # 添加API请求限制
        self.query_rate_limit = 1200  # 每分钟请求数
        self.last_query_time = {}     # 记录每个查询接口的最后请求时间
        self.min_query_interval = 60 / self.query_rate_limit  # 最小查询间隔

    def _check_rate_limit(self, endpoint: str) -> None:
        """检查并控制请求频率"""
        current_time = time.time()
        if endpoint in self.last_query_time:
            elapsed = current_time - self.last_query_time[endpoint]
            if elapsed < self.min_query_interval:
                sleep_time = self.min_query_interval - elapsed
                time.sleep(sleep_time)
        self.last_query_time[endpoint] = current_time

    def setup_trading_pair(self, symbol: str, amount: float, price: float = None) -> None:
        """设置交易参数"""
        self.symbol = symbol
        self.amount = amount
        self.price = price
        logger.info(f"设置交易对: {symbol}, 数量: {amount}, 价格: {price}")

    def check_balance(self) -> bool:
        """检查账户余额是否足够 - 使用查询客户端"""
        self._check_rate_limit('fetch_balance')
        try:
            balance = self.query_client.fetch_balance()
            quote_currency = self.symbol.split('/')[1]
            available = balance[quote_currency]['free']
            required = self.amount * (self.price or self.get_market_price())
            
            if available < required:
                logger.error(f"余额不足: 需要 {required} {quote_currency}，实际可用 {available} {quote_currency}")
                return False
            return True
        except Exception as e:
            logger.error(f"检查余额失败: {str(e)}")
            return False

    def get_market_price(self) -> float:
        """获取当前市场价格 - 使用查询客户端"""
        self._check_rate_limit('fetch_ticker')
        try:
            ticker = self.query_client.fetch_ticker(self.symbol)
            return ticker['last']
        except Exception as e:
            logger.error(f"获取市场价格失败: {str(e)}")
            return None

    def is_price_safe(self, current_price: float) -> bool:
        """检查价格是否在安全范围内"""
        if not self.price:  # 如果是市价单，跳过检查
            return True
            
        deviation = abs(current_price - self.price) / self.price
        if deviation > self.max_price_deviation:
            logger.warning(f"价格偏差过大: 目标价格 {self.price}, 当前价格 {current_price}, 偏差 {deviation*100}%")
            return False
        return True

    def get_technical_indicators(self) -> dict:
        """获取技术指标 - 使用查询客户端"""
        self._check_rate_limit('fetch_ohlcv')
        try:
            ohlcv = self.query_client.fetch_ohlcv(self.symbol, '1m', limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            rsi = talib.RSI(df['close'].values, timeperiod=14)[-1]
            ma5 = talib.MA(df['close'].values, timeperiod=5)[-1]
            ma10 = talib.MA(df['close'].values, timeperiod=10)[-1]
            
            return {
                'rsi': rsi,
                'ma5': ma5,
                'ma10': ma10
            }
        except Exception as e:
            logger.error(f"获取技术指标失败: {str(e)}")
            return None

    def check_trading_status(self) -> bool:
        """检查交易对是否可以交易 - 使用查询客户端"""
        self._check_rate_limit('load_markets')
        try:
            # 直接获取交易对信息，而不是加载所有市场
            market_info = self.query_client.fetch_ticker(self.symbol)
            
            # 检查是否有最新成交价，如果有说明已经开始交易
            if market_info.get('last'):
                return True
            
            # 检查买卖盘深度
            orderbook = self.query_client.fetch_order_book(self.symbol, limit=5)
            if orderbook['asks'] and orderbook['bids']:
                return True
            
            return False
        except Exception as e:
            logger.error(f"检查交易状态出错: {str(e)}")
            return False

    def set_price_protection(self, max_price: float = None, reference_price: float = None, 
                           max_slippage: float = None, min_depth: int = None, 
                           max_gap: float = None):
        """设置价格保护参数"""
        if max_price is not None:
            self.max_price_limit = max_price
            logger.info(f"设置最高价格保护: {max_price}")
        
        if reference_price is not None:
            self.reference_price = reference_price
            logger.info(f"设置参考价格: {reference_price}")
            
        if max_slippage is not None:
            self.max_slippage = max_slippage
            logger.info(f"设置最大滑点: {max_slippage*100}%")
            
        if min_depth is not None:
            self.min_depth_requirement = min_depth
            logger.info(f"设置最小深度要求: {min_depth}个订单")
            
        if max_gap is not None:
            self.max_price_gap = max_gap
            logger.info(f"设置最大价格差距: {max_gap*100}%")

    def place_market_order(self, max_retries: int = 3):
        """下市价单，带有严格的价格保护"""
        for attempt in range(max_retries):
            try:
                # 分析市场深度
                depth_analysis = self.analyze_market_depth()
                if not depth_analysis:
                    logger.error("无法获取有效的市场深度分析")
                    continue

                if depth_analysis['valid_orders'] < self.min_depth_requirement:
                    logger.error(f"有效订单数量不足: {depth_analysis['valid_orders']} < {self.min_depth_requirement}")
                    continue

                # 计算安全的最高价格
                safe_price = min(
                    depth_analysis['min_price'] * (1 + self.max_slippage),  # 基于最低价的滑点限制
                    self.max_price_limit if self.max_price_limit else float('inf'),  # 绝对价格限制
                    self.reference_price * 1.1 if self.reference_price else float('inf')  # 参考价格限制
                )

                # 使用限价单模拟市价单
                order = self.trade_client.create_limit_buy_order(
                    symbol=self.symbol,
                    amount=self.amount,
                    price=safe_price,
                    params={
                        'timeInForce': 'IOC',  # 立即成交或取消
                    }
                )

                logger.info(f"下单成功: {order}")
                self.save_trade_history(order)
                return order

            except Exception as e:
                logger.error(f"下单失败 (尝试 {attempt + 1}/{max_retries}): {str(e)}")
                if attempt < max_retries - 1:
                    time.sleep(0.1)
                else:
                    return None

    def place_limit_order(self):
        """下限价单 - 使用交易客户端"""
        try:
            order = self.trade_client.create_limit_buy_order(
                symbol=self.symbol,
                amount=self.amount,
                price=self.price
            )
            logger.info(f"限价单下单成功: {order}")
            self.save_trade_history(order)
            return order
        except Exception as e:
            logger.error(f"下单失败: {str(e)}")
            return None

    def track_order(self, order_id: str, timeout: int = 60) -> dict:
        """跟踪订单状态 - 使用查询客户端"""
        start_time = time.time()
        while time.time() - start_time < timeout:
            self._check_rate_limit('fetch_order')
            try:
                order = self.query_client.fetch_order(order_id, self.symbol)
                if order['status'] in ['closed', 'canceled', 'expired']:
                    return order
                time.sleep(0.5)
            except Exception as e:
                logger.error(f"获取订单状态失败: {str(e)}")
                return None
        return None

    def save_trade_history(self, order: dict):
        """保存交易历史"""
        trade_info = {
            'timestamp': datetime.now().isoformat(),
            'symbol': self.symbol,
            'type': order['type'],
            'side': order['side'],
            'price': order['price'],
            'amount': order['amount'],
            'cost': order.get('cost'),
            'status': order['status']
        }
        self.history.append(trade_info)
        self.save_history_to_file()

    def save_history_to_file(self):
        """将交易历史保存到文件"""
        try:
            with open('trade_history.json', 'w') as f:
                json.dump(self.history, f, indent=4)
        except Exception as e:
            logger.error(f"保存交易历史失败: {str(e)}")

    def monitor_position(self, order_id: str):
        """监控持仓位置"""
        try:
            order = self.query_client.fetch_order(order_id, self.symbol)
            if order['status'] != 'closed':
                return
                
            entry_price = float(order['average'])
            while True:
                current_price = self.get_market_price()
                if not current_price:
                    continue
                    
                profit_ratio = (current_price - entry_price) / entry_price
                
                # 止损
                if profit_ratio <= -self.stop_loss:
                    self.close_position("触发止损")
                    break
                    
                # 止盈
                if profit_ratio >= self.take_profit:
                    self.close_position("触发止盈")
                    break
                    
                time.sleep(1)
                
        except Exception as e:
            logger.error(f"监控持仓失败: {str(e)}")

    def close_position(self, reason: str):
        """平仓"""
        try:
            order = self.trade_client.create_market_sell_order(
                symbol=self.symbol,
                amount=self.amount
            )
            logger.info(f"{reason}, 平仓成功: {order}")
            self.save_trade_history(order)
            return order
        except Exception as e:
            logger.error(f"平仓失败: {str(e)}")
            return None

    def analyze_market_depth(self) -> dict:
        """分析市场深度，识别异常订单"""
        try:
            orderbook = self.query_client.fetch_order_book(self.symbol, limit=20)
            if not orderbook or not orderbook['asks']:
                return None

            asks = orderbook['asks']
            if len(asks) < self.min_depth_requirement:
                logger.warning(f"深度不足: 只有 {len(asks)} 个卖单，要求至少 {self.min_depth_requirement} 个")
                return None

            # 计算正常价格范围
            prices = [price for price, _ in asks[:10]]
            volumes = [volume for _, volume in asks[:10]]
            
            # 移除异常值
            if self.ignore_outliers:
                filtered_prices = []
                filtered_volumes = []
                median_price = sorted(prices)[len(prices)//2]
                
                for i, price in enumerate(prices):
                    # 如果价格偏离中位数太多，认为是异常值
                    if abs(price - median_price) / median_price <= self.max_price_gap:
                        filtered_prices.append(price)
                        filtered_volumes.append(volumes[i])
                
                prices = filtered_prices
                volumes = filtered_volumes

            if not prices:
                logger.warning("过滤后没有有效订单")
                return None

            avg_price = sum(p * v for p, v in zip(prices, volumes)) / sum(volumes)
            min_price = min(prices)
            max_price = max(prices)
            
            # 检查相邻订单价格差距
            for i in range(len(prices)-1):
                price_gap = (prices[i+1] - prices[i]) / prices[i]
                if price_gap > self.max_price_gap:
                    logger.warning(f"发现异常价格差距: {prices[i]} -> {prices[i+1]}, 差距: {price_gap*100}%")

            return {
                'avg_price': avg_price,
                'min_price': min_price,
                'max_price': max_price,
                'valid_orders': len(prices),
                'total_volume': sum(volumes),
                'price_distribution': prices
            }
        except Exception as e:
            logger.error(f"分析市场深度失败: {str(e)}")
            return None

    def print_status(self, status: str, clear: bool = True):
        """打印状态"""
        if clear and os.name == 'posix':  # Linux/Mac
            os.system('clear')
        elif clear and os.name == 'nt':   # Windows
            os.system('cls')
        
        print("\n====== 币安抢币状态 ======")
        print(f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{status}")
        print("========================\n")

    def analyze_opening_price(self) -> float:
        """分析开盘价格"""
        try:
            # 获取订单簿深度
            orderbook = self.query_client.fetch_order_book(self.symbol, limit=20)
            if not orderbook or not orderbook['asks']:
                return None
            
            asks = orderbook['asks'][:5]  # 取前5个卖单
            
            # 计算加权平均价格
            total_volume = sum(vol for _, vol in asks)
            weighted_price = sum(price * vol for price, vol in asks) / total_volume
            
            # 获取最低卖价
            min_price = asks[0][0]
            
            logger.info(f"分析开盘价格: 最低卖价={min_price}, 加权均价={weighted_price}")
            return min_price
        
        except Exception as e:
            logger.error(f"分析开盘价格失败: {str(e)}")
            return None

    def auto_adjust_price_protection(self, opening_price: float):
        """根据开盘价自动调整价格保护"""
        if not opening_price:
            return
        
        # 自动设置价格保护参数
        self.reference_price = opening_price
        self.max_price_limit = opening_price * 1.5  # 最高接受1.5倍开盘价
        self.max_slippage = 0.1  # 10%滑点
        
        logger.info(
            f"价格保护自动调整:\n"
            f"开盘价: {opening_price}\n"
            f"最高接受价格: {self.max_price_limit}\n"
            f"参考价格: {self.reference_price}\n"
            f"最大滑点: {self.max_slippage*100}%"
        )

    def snipe(self):
        """开始抢购"""
        # 显示初始策略确认
        strategy_info = (
            "====== 抢购策略设置 ======\n"
            f"交易对: {self.symbol}\n"
            f"买入数量: {self.amount}\n"
            f"买入方式: {'限价单' if self.price else '市价单(带保护)'}\n"
            "\n--- 价格保护 ---\n"
            "· 开盘自动获取价格并调整保护参数\n"
            "· 最高价格 = 开盘价 * 1.5\n"
            "· 最大滑点: 10%\n"
            f"· 最小深度: {self.min_depth_requirement}个订单\n"
            "\n--- 卖出策略 ---\n"
        )

        if self.sell_ladders:
            for i, (profit, percent) in enumerate(self.sell_ladders, 1):
                strategy_info += f"  梯队{i}: +{profit*100}% → {percent*100}%\n"
        
        strategy_info += f"\n止损: -{self.stop_loss*100}%\n"
        strategy_info += "\n确认开始抢购? (yes/no): "
        
        self.print_status(strategy_info)
        if input().lower() != 'yes':
            return None

        logger.info(f"开始抢购 {self.symbol}")
        max_attempts, check_interval = self.config.get_snipe_settings()
        attempt = 0

        if not self.check_balance():
            return None

        while attempt < max_attempts:
            # 状态显示
            current_status = (
                f"正在监控 {self.symbol}\n"
                f"尝试次数: {attempt + 1}/{max_attempts}\n"
                f"按 Ctrl+C 终止\n"
            )
            self.print_status(current_status)

            if self.check_trading_status():
                # 检测到开盘，立即获取价格并下单
                opening_price = self.analyze_opening_price()
                if opening_price:
                    self.auto_adjust_price_protection(opening_price)
                    depth_info = self.analyze_market_depth()
                    
                    if depth_info and depth_info['total_volume'] >= self.amount:
                        # 直接下单，不再提示确认
                        if self.price:
                            order = self.place_limit_order()
                        else:
                            order = self.place_market_order()

                        if order:
                            status = (
                                f"下单成功!\n"
                                f"交易对: {self.symbol}\n"
                                f"开盘价: {opening_price}\n"
                                f"成交价: {order.get('price', '市价')}\n"
                                f"数量: {order['amount']}\n"
                                f"订单ID: {order['id']}\n"
                                "\n正在启动自动卖出监控..."
                            )
                            self.print_status(status)
                            
                            # 启动订单监控和自动卖出
                            threading.Thread(target=self.track_order, args=(order['id'],)).start()
                            return order

            attempt += 1
            time.sleep(0.1)  # 100ms

        self.print_status("达到最大尝试次数，抢购失败")
        return None

    def warmup(self):
        """预热连接和缓存"""
        try:
            # 预加载市场信息
            self.query_client.load_markets()
            
            # 预检查账户余额
            self.check_balance()
            
            # 预热 WebSocket 连接（如果使用）
            self.query_client.fetch_ticker(self.symbol)
            
            logger.info("系统预热完成")
            return True
        except Exception as e:
            logger.error(f"系统预热失败: {str(e)}")
            return False

    def get_position(self) -> dict:
        """获取当前持仓"""
        try:
            balance = self.query_client.fetch_balance()
            if not self.symbol:
                return None
            
            base_currency = self.symbol.split('/')[0]  # 如 BTC/USDT 中的 BTC
            position = {
                'symbol': self.symbol,
                'amount': balance[base_currency]['free'],
                'current_price': self.get_market_price(),
                'value': 0
            }
            
            if position['current_price']:
                position['value'] = position['amount'] * position['current_price']
            
            return position
        except Exception as e:
            logger.error(f"获取持仓失败: {str(e)}")
            return None

    def place_market_sell_order(self, amount: float):
        """下市价卖单"""
        try:
            order = self.trade_client.create_market_sell_order(
                symbol=self.symbol,
                amount=amount
            )
            logger.info(f"市价卖单下单成功: {order}")
            self.save_trade_history(order)
            return order
        except Exception as e:
            logger.error(f"卖出失败: {str(e)}")
            return None

    def place_limit_sell_order(self, amount: float, price: float):
        """下限价卖单"""
        try:
            order = self.trade_client.create_limit_sell_order(
                symbol=self.symbol,
                amount=amount,
                price=price
            )
            logger.info(f"限价卖单下单成功: {order}")
            self.save_trade_history(order)
            return order
        except Exception as e:
            logger.error(f"卖出失败: {str(e)}")
            return None

def print_menu():
    """打印主菜单"""
    print("\n=== 币安现货抢币工具 ===")
    print("1. 设置API密钥")
    print("2. 抢开盘策略设置")  # 新的集中设置选项
    print("3. 查看当前策略")
    print("4. 开始抢购")
    print("5. 手动卖出")
    print("6. 查看持仓")
    print("0. 退出")
    print("=====================")

def get_valid_input(prompt: str, input_type=float, allow_empty: bool = False):
    """获取有效输入"""
    while True:
        try:
            value = input(prompt)
            if allow_empty and not value.strip():
                return None
            if input_type == str and value.strip():
                return value.strip()
            return input_type(value)
        except ValueError:
            print("输入无效，请重试")

def check_dependencies():
    """检查依赖"""
    try:
        import ccxt
        logger.info(f"CCXT版本: {ccxt.__version__}")
        
        import pandas as pd
        logger.info(f"Pandas版本: {pd.__version__}")
        
        import numpy as np
        logger.info(f"Numpy版本: {np.__version__}")
        
        import talib
        logger.info(f"TA-Lib已安装")
        
        return True
    except ImportError as e:
        logger.error(f"依赖检查失败: {str(e)}")
        logger.error("请运行 setup.sh 安装所需依赖")
        return False

def setup_snipe_strategy(sniper: BinanceSniper):
    """抢开盘策略设置"""
    print("\n=== 抢开盘策略设置 ===")
    
    # 1. 基础设置
    print("\n>>> 基础参数设置")
    symbol = get_valid_input("请输入交易对 (例如 BTC/USDT): ", str)
    amount = get_valid_input("请输入购买数量: ")
    
    # 2. 买入策略
    print("\n>>> 买入策略设置")
    buy_type = input("选择买入方式 (1: 市价单, 2: 限价单): ")
    price = None
    if buy_type == "2":
        price = get_valid_input("请输入限价单价格: ")
    
    # 3. 价格保护设置
    print("\n>>> 价格保护设置")
    print("价格保护机制说明:")
    print("1. 最高可接受价格: 绝对价格上限，高于此价格的订单会被拒绝")
    print("2. 参考价格: 预期的开盘价，用于计算价格偏离度")
    print("3. 最大滑点: 允许的最大价格偏离百分比")
    print("4. 最小深度: 要求订单簿中至少有多少个卖单才会下单")
    print("\n注意: 以上保护措施对市价单和限价单都生效")
    print("------------------------")

    max_price = get_valid_input("设置最高可接受价格: ")
    reference_price = get_valid_input("设置参考价格 (预期开盘价): ")
    max_slippage = get_valid_input("设置最大滑点 (例如 0.1 表示 10%): ")
    min_depth = get_valid_input("设置最小深度要求 (建议5个以上): ", int)
    
    # 显示保护设置确认
    print("\n当前价格保护设置:")
    print(f"1. 不会以高于 {max_price} 的价格成交")
    print(f"2. 参考价格设为 {reference_price}，允许最高偏离 10%")
    print(f"3. 允许的最大滑点为 {max_slippage*100}%")
    print(f"4. 要求至少有 {min_depth} 个卖单才会下单")
    print("\n示例: 如果最低卖价是 0.01:")
    print(f"- 最高接受价格限制: {max_price}")
    print(f"- 参考价格限制: {reference_price * 1.1}")
    print(f"- 滑点限制: {0.01 * (1 + max_slippage)}")
    print("系统将使用以上三个限制中的最小值作为实际价格上限")
    
    confirm = input("\n确认以上价格保护设置? (yes/no): ")
    if confirm.lower() != 'yes':
        print("取消设置")
        return

    # 4. 卖出策略
    print("\n>>> 卖出策略设置")
    take_profit = get_valid_input("设置止盈比例 (例如 0.1 表示 10%): ")
    stop_loss = get_valid_input("设置止损比例 (例如 0.05 表示 5%): ")
    
    # 5. 执行参数
    print("\n>>> 执行参数设置")
    max_attempts = get_valid_input("设置最大尝试次数 [10]: ", int, True) or 10
    check_interval = get_valid_input("设置检查间隔(秒) [0.2]: ", float, True) or 0.2

    # 应用设置
    sniper.setup_trading_pair(symbol, amount, price)
    sniper.set_price_protection(max_price, reference_price, max_slippage, min_depth)
    sniper.stop_loss = stop_loss
    sniper.take_profit = take_profit
    sniper.config.set_snipe_settings(max_attempts, check_interval)

    # 显示设置确认
    print("\n=== 策略设置完成 ===")
    print_strategy(sniper)
    input("\n按回车返回主菜单...")

def print_strategy(sniper: BinanceSniper):
    """打印当前策略"""
    print("\n====== 当前抢币策略 ======")
    print(f"交易对: {sniper.symbol}")
    print(f"买入数量: {sniper.amount}")
    print(f"买入方式: {'限价单' if sniper.price else '市价单'}")
    if sniper.price:
        print(f"买入价格: {sniper.price}")
    
    print("\n价格保护:")
    print(f"最高接受价格: {sniper.max_price_limit}")
    print(f"参考价格: {sniper.reference_price}")
    print(f"最大滑点: {sniper.max_slippage*100}%")
    print(f"最小深度: {sniper.min_depth_requirement}个订单")
    
    print("\n卖出策略:")
    print(f"止盈比例: {sniper.take_profit*100}%")
    print(f"止损比例: {sniper.stop_loss*100}%")
    
    max_attempts, check_interval = sniper.config.get_snipe_settings()
    print("\n执行参数:")
    print(f"最大尝试次数: {max_attempts}")
    print(f"检查间隔: {check_interval}秒")
    print("========================")

def main():
    """主函数"""
    try:
        if not check_dependencies():
            return
            
        logger.info("=== 币安现货抢币工具启动 ===")
        config = ConfigManager()
        sniper = BinanceSniper(config)

        while True:
            print_menu()
            choice = input("请选择操作 (0-6): ")

            if choice == "1":
                api_key = get_valid_input("请输入API Key: ", str)
                api_secret = get_valid_input("请输入API Secret: ", str)
                config.set_api_keys(api_key, api_secret)
                print("API密钥设置成功!")

            elif choice == "2":
                setup_snipe_strategy(sniper)

            elif choice == "3":
                print_strategy(sniper)
                input("\n按回车继续...")

            elif choice == "4":
                if not all([sniper.symbol, sniper.amount]):
                    print("请先完成抢购策略设置")
                    continue
                # 开始抢购...
                pass

            elif choice == "5":
                if not sniper.symbol:
                    print("请先设置交易对")
                    continue
                    
                position = sniper.get_position()
                if not position or position['amount'] <= 0:
                    print("当前没有持仓")
                    continue
                    
                print("\n当前持仓:")
                print(f"交易对: {position['symbol']}")
                print(f"持仓数量: {position['amount']}")
                print(f"当前价格: {position['current_price']}")
                print(f"持仓价值: {position['value']} USDT")
                
                sell_amount = get_valid_input("请输入卖出数量 (全部卖出请输入 all): ", str)
                if sell_amount.lower() == 'all':
                    sell_amount = position['amount']
                else:
                    try:
                        sell_amount = float(sell_amount)
                        if sell_amount > position['amount']:
                            print("卖出数量不能大于持仓数量")
                            continue
                    except ValueError:
                        print("输入无效")
                        continue
                
                sell_type = input("请选择卖出方式 (1: 市价卖出, 2: 限价卖出): ")
                
                if sell_type == "1":
                    confirm = input(f"\n确认以市价卖出 {sell_amount} {sniper.symbol}? (yes/no): ")
                    if confirm.lower() == 'yes':
                        result = sniper.place_market_sell_order(sell_amount)
                        if result:
                            print("\n卖出成功!")
                            print("订单信息:", result)
                        else:
                            print("\n卖出失败!")
                
                elif sell_type == "2":
                    price = get_valid_input("请输入卖出价格: ")
                    confirm = input(f"\n确认以 {price} 的价格卖出 {sell_amount} {sniper.symbol}? (yes/no): ")
                    if confirm.lower() == 'yes':
                        result = sniper.place_limit_sell_order(sell_amount, price)
                        if result:
                            print("\n卖出订单提交成功!")
                            print("订单信息:", result)
                        else:
                            print("\n卖出失败!")
                
                else:
                    print("选择无效")
                
                input("\n按回车继续...")

            elif choice == "6":
                if not sniper.symbol:
                    print("请先设置交易对")
                    continue
                    
                position = sniper.get_position()
                if not position:
                    print("获取持仓信息失败")
                    continue
                    
                print("\n当前持仓:")
                print(f"交易对: {position['symbol']}")
                print(f"持仓数量: {position['amount']}")
                print(f"当前价格: {position['current_price']}")
                print(f"持仓价值: {position['value']} USDT")
                
                input("\n按回车继续...")

            elif choice == "0":
                print("感谢使用，再见!")
                break

            else:
                print("无效选择，请重试")

    except KeyboardInterrupt:
        print("\n\n程序已终止")
    except Exception as e:
        print(f"\n发生错误: {str(e)}")
        logger.error(f"程序错误: {str(e)}")

if __name__ == "__main__":
    main()

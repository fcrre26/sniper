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

    def snipe(self):
        """开始抢购"""
        logger.info(f"开始抢购 {self.symbol}")
        max_attempts, check_interval = self.config.get_snipe_settings()
        attempt = 0

        if not self.check_balance():
            return None

        while attempt < max_attempts:
            # 更新状态显示
            status = (
                f"交易对: {self.symbol}\n"
                f"当前进度: 第 {attempt + 1} 次尝试 (共 {max_attempts} 次)\n"
                f"当前状态: 正在等待开盘\n"
                f"按 Ctrl+C 可以终止程序"
            )
            self.print_status(status)

            if self.check_trading_status():
                self.print_status("检测到交易已开盘，准备下单...")
                depth_info = self.analyze_market_depth()
                if depth_info:
                    logger.debug(f"深度分析: {json.dumps(depth_info, indent=2)}")
                    
                    if depth_info['total_volume'] < self.amount:
                        status = (
                            f"交易对: {self.symbol}\n"
                            f"当前状态: 市场深度不足\n"
                            f"需要数量: {self.amount}\n"
                            f"可用数量: {depth_info['total_volume']}\n"
                            f"继续等待中..."
                        )
                        self.print_status(status)
                        time.sleep(0.1)
                        continue

                    if self.reference_price and depth_info['avg_price'] > self.reference_price * 1.1:
                        status = (
                            f"交易对: {self.symbol}\n"
                            f"当前状态: 价格过高\n"
                            f"当前均价: {depth_info['avg_price']}\n"
                            f"参考价格: {self.reference_price}\n"
                            f"继续等待中..."
                        )
                        self.print_status(status)
                        time.sleep(0.1)
                        continue

                # 下单逻辑
                self.print_status("正在提交订单...")
                if self.price:
                    order = self.place_limit_order()
                else:
                    order = self.place_market_order()

                if order:
                    status = (
                        f"交易对: {self.symbol}\n"
                        f"订单状态: 下单成功!\n"
                        f"订单编号: {order['id']}\n"
                        f"成交价格: {order.get('price', '市价')}\n"
                        f"成交数量: {order['amount']}\n"
                        f"正在确认成交..."
                    )
                    self.print_status(status)
                    
                    # 启动订单状态监控
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

def print_menu():
    """打印主菜单"""
    print("\n=== 币安现货抢币工具 ===")
    print("1. 设置API密钥")
    print("2. 设置交易对")
    print("3. 设置购买数量")
    print("4. 设置价格 (可选)")
    print("5. 设置价格保护")
    print("6. 设置抢购参数")
    print("7. 设置止盈止损")
    print("8. 显示当前设置")
    print("9. 开始抢购")
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
            choice = input("请选择操作 (0-9): ")

            if choice == "1":
                api_key = get_valid_input("请输入API Key: ", str)
                api_secret = get_valid_input("请输入API Secret: ", str)
                config.set_api_keys(api_key, api_secret)
                print("API密钥设置成功!")

            elif choice == "2":
                symbol = get_valid_input("请输入交易对 (例如 BTC/USDT): ", str)
                amount = sniper.amount
                price = sniper.price
                sniper.setup_trading_pair(symbol, amount, price)
                config.add_trading_pair(symbol)
                print(f"交易对设置成功: {symbol}")

            elif choice == "3":
                amount = get_valid_input("请输入购买数量: ")
                if sniper.symbol:
                    sniper.setup_trading_pair(sniper.symbol, amount, sniper.price)
                    config.add_trading_pair(sniper.symbol, amount)
                    print(f"购买数量设置成功: {amount}")
                else:
                    print("请先设置交易对")

            elif choice == "4":
                price = get_valid_input("请输入价格 (直接回车则为市价单): ", allow_empty=True)
                if sniper.symbol and sniper.amount:
                    sniper.setup_trading_pair(sniper.symbol, sniper.amount, price)
                    print(f"价格设置成功: {price or '市价'}")
                else:
                    print("请先设置交易对和数量")

            elif choice == "5":
                max_price = get_valid_input("请输入最高可接受价格 (直接回车跳过): ", allow_empty=True)
                reference_price = get_valid_input("请输入参考价格 (直接回车跳过): ", allow_empty=True)
                max_slippage = get_valid_input("请输入最大滑点 (直接回车跳过): ", allow_empty=True)
                min_depth = get_valid_input("请输入最小深度要求 (直接回车跳过): ", allow_empty=True)
                max_gap = get_valid_input("请输入最大价格差距 (直接回车跳过): ", allow_empty=True)
                sniper.set_price_protection(max_price, reference_price, max_slippage, min_depth, max_gap)
                print("价格保护设置成功!")

            elif choice == "6":
                max_attempts = get_valid_input("请输入最大尝试次数 [10]: ", int, True) or 10
                check_interval = get_valid_input("请输入检查间隔(秒) [0.2]: ", float, True) or 0.2
                config.set_snipe_settings(max_attempts, check_interval)
                print(f"抢购参数设置成功! 最大尝试次数: {max_attempts}, 检查间隔: {check_interval}秒")

            elif choice == "7":
                print("\n当前设置:")
                api_key, api_secret = config.get_api_keys()
                print(f"API Key: {'*' * len(api_key) if api_key else '未设置'}")
                print(f"API Secret: {'*' * len(api_secret) if api_secret else '未设置'}")
                print(f"交易对: {sniper.symbol or '未设置'}")
                print(f"数量: {sniper.amount or '未设置'}")
                print(f"价格: {sniper.price or '市价'}")
                print(f"止损: {sniper.stop_loss*100}%")
                print(f"止盈: {sniper.take_profit*100}%")
                max_attempts, check_interval = config.get_snipe_settings()
                print(f"最大尝试次数: {max_attempts}")
                print(f"检查间隔: {check_interval}秒")
                input("\n按回车继续...")

            elif choice == "8":
                if not all([sniper.symbol, sniper.amount]):
                    print("请先完成必要设置 (交易对和数量)")
                    continue

                print("\n开始预热系统...")
                if not sniper.warmup():
                    print("系统预热失败，请重试")
                    continue

                confirm = input(f"\n确认开始抢开盘 {sniper.symbol}?\n"
                              f"数量: {sniper.amount}\n"
                              f"价格: {sniper.price or '市价'}\n"
                              f"确认请输入 'yes': ")

                if confirm.lower() == 'yes':
                    print("\n等待开盘...")
                    result = sniper.snipe()

                    if result:
                        print("\n抢购成功!")
                        print("订单信息:", result)
                        
                        # 跟踪订单状态
                        final_order = sniper.track_order(result['id'])
                        if final_order:
                            print(f"最终订单状态: {final_order['status']}")
                            print(f"成交均价: {final_order.get('average', '未知')}")
                    else:
                        print("\n抢购失败!")

                    input("\n按回车继续...")
                else:
                    print("已取消抢购")

            elif choice == "9":
                if os.path.exists('trade_history.json'):
                    with open('trade_history.json', 'r') as f:
                        history = json.load(f)
                        print("\n交易历史:")
                        for trade in history:
                            print(f"时间: {trade['timestamp']}")
                            print(f"交易对: {trade['symbol']}")
                            print(f"类型: {trade['type']}")
                            print(f"价格: {trade['price']}")
                            print(f"数量: {trade['amount']}")
                            print(f"状态: {trade['status']}")
                            print("---------------")
                else:
                    print("暂无交易历史")
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

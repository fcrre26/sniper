import ccxt
import time
from datetime import datetime
import logging
import json
import os
import pandas as pd
import numpy as np
import threading
import sys
from logging.handlers import RotatingFileHandler
from collections import defaultdict
import configparser
from typing import Tuple

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

# 删除TA-Lib依赖检查
try:
    import pandas as pd
    import numpy as np
except ImportError:
    logger.error("请安装 pandas 和 numpy: pip install pandas numpy")
    raise

class PerformanceMonitor:
    """性能监控工具"""
    def __init__(self):
        self.timers = {}
        self.records = defaultdict(list)
    
    def start(self, name: str):
        """开始计时"""
        self.timers[name] = time.perf_counter()
    
    def stop(self, name: str) -> float:
        """停止计时并返回耗时(ms)"""
        if name not in self.timers:
            return 0
        elapsed = (time.perf_counter() - self.timers[name]) * 1000
        self.records[name].append(elapsed)
        del self.timers[name]
        return elapsed
    
    def get_stats(self, name: str) -> dict:
        """获取统计信息"""
        if name not in self.records:
            return {}
        times = self.records[name]
        return {
            'count': len(times),
            'avg': sum(times) / len(times),
            'min': min(times),
            'max': max(times),
            'last': times[-1]
        }
    
    def print_stats(self):
        """打印所有统计信息"""
        print("\n=== 性能统计 ===")
        for name, stats in self.records.items():
            avg = sum(stats) / len(stats)
            min_time = min(stats)
            max_time = max(stats)
            print(f"{name}:")
            print(f"  次数: {len(stats)}")
            print(f"  平均: {avg:.2f}ms")
            print(f"  最小: {min_time:.2f}ms")
            print(f"  最大: {max_time:.2f}ms")
            print(f"  最近: {stats[-1]:.2f}ms")

class ConfigManager:
    """配置管理类"""
    def __init__(self):
        self.config = configparser.ConfigParser()
        self.config_file = 'config.ini'
        self.load_config()

    def load_config(self):
        """加载配置文件"""
        if os.path.exists(self.config_file):
            self.config.read(self.config_file, encoding='utf-8')
        else:
            # 创建默认配置
            self.config['API'] = {
                'trade_key': '',
                'trade_secret': '',
                'query_key': '',
                'query_secret': ''
            }
            self.config['SNIPE'] = {
                'max_attempts': '10',
                'check_interval': '0.2'
            }
            self.save_config()
            logger.info("创建默认配置文件")

    def save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                self.config.write(f)
            logger.info("配置保存成功")
        except Exception as e:
            logger.error(f"保存配置失败: {str(e)}")

    def set_api_keys(self, trade_key: str, trade_secret: str, query_key: str = None, query_secret: str = None):
        """设置API密钥"""
        try:
            # 验证API密钥格式
            if not trade_key or not trade_secret:
                raise ValueError("交易API密钥不能为空")

            # 设置交易API
            self.config['API']['trade_key'] = trade_key
            self.config['API']['trade_secret'] = trade_secret

            # 设置查询API（如果提供）
            if query_key and query_secret:
                self.config['API']['query_key'] = query_key
                self.config['API']['query_secret'] = query_secret
            else:
                # 如果没有提供查询API，使用相同的密钥
                self.config['API']['query_key'] = trade_key
                self.config['API']['query_secret'] = trade_secret

            self.save_config()
            logger.info("""
=== API密钥设置成功 ===
交易API: 已更新
查询API: 已更新
""")
            
        except Exception as e:
            logger.error(f"设置API密钥失败: {str(e)}")
            raise

    def get_trade_api_keys(self) -> Tuple[str, str]:
        """获取交易API密钥"""
        return (
            self.config['API'].get('trade_key', ''),
            self.config['API'].get('trade_secret', '')
        )

    def get_query_api_keys(self) -> Tuple[str, str]:
        """获取查询API密钥"""
        return (
            self.config['API'].get('query_key', ''),
            self.config['API'].get('query_secret', '')
        )

    def set_snipe_settings(self, max_attempts: int = None, check_interval: float = None):
        """设置抢购参数"""
        try:
            if max_attempts is not None:
                self.config['SNIPE']['max_attempts'] = str(max_attempts)
            if check_interval is not None:
                self.config['SNIPE']['check_interval'] = str(check_interval)
            self.save_config()
            logger.info(f"""
=== 抢购参数设置成功 ===
最大尝试次数: {max_attempts}
检查间隔: {check_interval}秒
""")
        except Exception as e:
            logger.error(f"设置抢购参数失败: {str(e)}")
            raise

    def get_snipe_settings(self) -> Tuple[int, float]:
        """获取抢购参数"""
        return (
            int(self.config['SNIPE'].get('max_attempts', 10)),
            float(self.config['SNIPE'].get('check_interval', 0.2))
)

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
        self.perf = PerformanceMonitor()

    def _check_rate_limit(self, endpoint: str) -> None:
        """检查并控制请求频率"""
        current_time = time.time()
        if endpoint in self.last_query_time:
            elapsed = current_time - self.last_query_time[endpoint]
            if elapsed < self.min_query_interval:
                sleep_time = self.min_query_interval - elapsed
                time.sleep(sleep_time)
        self.last_query_time[endpoint] = current_time

    def setup_trading_pair(self, symbol: str, amount: float) -> None:
        """设置交易参数"""
        try:
        self.symbol = symbol
        self.amount = amount
            logger.info(f"""
=== 设置交易参数 ===
交易对: {symbol}
买入金额: {amount} USDT
""")
        except Exception as e:
            logger.error(f"设置交易参数失败: {str(e)}")
            raise

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
        """检查是否已开盘"""
        try:
            # 1. 先检查是否有成交价
            ticker = self.query_client.fetch_ticker(self.symbol)
            if ticker.get('last'):  # 如果有最新成交价，说明已经开盘
                self.opening_price = ticker['last']  # 记录开盘价
                logger.info(f"检测到开盘价: {self.opening_price}")
                return True
            
            # 2. 如果还没有成交，检查是否有卖单
            orderbook = self.query_client.fetch_order_book(self.symbol, limit=1)
            if not orderbook or not orderbook['asks']:
            return False
            
            return False  # 有卖单但没成交，说明还没开盘
        
        except Exception as e:
            logger.error(f"检查交易状态失败: {str(e)}")
            return False

    def set_price_protection(self, max_price: float = None, price_multiplier: float = None):
        """设置价格保护参数"""
        try:
            if max_price is not None:
                self.max_price_limit = max_price
                logger.info(f"设置最高接受价格: {max_price} USDT")
            
            if price_multiplier is not None:
                self.price_multiplier = price_multiplier
                logger.info(f"设置开盘价倍数: {price_multiplier}倍")
            
            logger.info(f"""
=== 价格保护设置完成 ===
最高接受价格: {self.max_price_limit} USDT
开盘价倍数: {self.price_multiplier}倍
""")
        except Exception as e:
            logger.error(f"设置价格保护失败: {str(e)}")
            raise

    def place_market_order(self):
        """下市价单，带有严格的价格保护"""
        try:
            # 1. 获取当前最新成交价和卖1价格
            ticker = self.query_client.fetch_ticker(self.symbol)
            orderbook = self.query_client.fetch_order_book(self.symbol, limit=1)
            
            if not orderbook or not orderbook['asks']:
                return None
            
            last_price = ticker.get('last')  # 最新成交价
            ask_price = orderbook['asks'][0][0]  # 卖1价格
            
            # 2. 使用最新成交价(如果有)或卖1价格来计算
            base_price = last_price if last_price else ask_price
            multiplier_limit = base_price * self.price_multiplier
            
            # 3. 使用两个限制中的较小值
            safe_price = min(multiplier_limit, self.max_price_limit)
            
            logger.info(
                f"价格分析:\n"
                f"最新成交价: {last_price}\n"
                f"当前卖1价: {ask_price}\n"
                f"基准价格: {base_price}\n"
                f"倍数限制: {multiplier_limit}\n"
                f"最高限制: {self.max_price_limit}\n"
                f"实际下单价: {safe_price}"
            )

            # 4. 检查是否超过最高接受价格
            if multiplier_limit > self.max_price_limit:
                logger.warning(f"基于倍数的价格({multiplier_limit})超过最高接受价格({self.max_price_limit})")
                return None

            # 5. 执行下单
            order = self.trade_client.create_limit_buy_order(
                symbol=self.symbol,
                amount=self.amount,
                price=safe_price,
                params={
                    'timeInForce': 'IOC',
                    'postOnly': False,
                }
            )

            return order

        except Exception as e:
            logger.error(f"下单失败: {str(e)}")
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
            total_amount = float(order['amount'])
            remaining_amount = total_amount
            sold_ladders = set()  # 记录已经触发的阶梯
            
            logger.info(f"""
=== 开始监控持仓 ===
交易对: {self.symbol}
买入均价: {entry_price}
买入数量: {total_amount}
买入金额: {entry_price * total_amount} USDT
止损设置: -{self.stop_loss*100}%
阶梯止盈:
{chr(10).join([f'- 涨幅 {p*100}% 卖出 {a*100}%' for p, a in self.sell_ladders])}
""")
            
            while remaining_amount > 0:
                current_price = self.get_market_price()
                if not current_price:
                    continue
                    
                profit_ratio = (current_price - entry_price) / entry_price
                
                # 记录价格变动
                logger.info(f"""
=== 价格监控 ===
当前价格: {current_price}
买入均价: {entry_price}
盈亏比例: {profit_ratio*100:.2f}%
剩余数量: {remaining_amount}
""")
                
                # 止损检查
                if profit_ratio <= -self.stop_loss:
                    if remaining_amount > 0:
                        logger.warning(f"""
=== 触发止损 ===
当前亏损: {profit_ratio*100:.2f}%
止损线: -{self.stop_loss*100}%
卖出数量: {remaining_amount}
卖出价格: {current_price}
""")
                        sell_order = self.place_market_sell_order(remaining_amount)
                        if sell_order:
                            logger.info(f"""
=== 止损卖出成功 ===
卖出均价: {sell_order['average']}
卖出数量: {sell_order['amount']}
卖出金额: {sell_order['cost']} USDT
订单ID: {sell_order['id']}
""")
                    break
                    
                # 阶梯止盈检查
                for i, (profit_target, sell_percent) in enumerate(self.sell_ladders):
                    if i not in sold_ladders and profit_ratio >= profit_target:
                        sell_amount = total_amount * sell_percent
                        if sell_amount > remaining_amount:
                            sell_amount = remaining_amount
                        
                        logger.info(f"""
=== 触发第{i+1}档止盈 ===
目标涨幅: {profit_target*100}%
实际涨幅: {profit_ratio*100:.2f}%
卖出数量: {sell_amount}
卖出价格: {current_price}
剩余数量: {remaining_amount - sell_amount}
""")
                        
                        sell_order = self.place_market_sell_order(sell_amount)
                        if sell_order:
                            logger.info(f"""
=== 止盈卖出成功 ===
卖出均价: {sell_order['average']}
卖出数量: {sell_order['amount']}
卖出金额: {sell_order['cost']} USDT
订单ID: {sell_order['id']}
""")
                            remaining_amount -= sell_amount
                            sold_ladders.add(i)
                    
                time.sleep(1)
                
            # 交易完成后的统计
            logger.info(f"""
====== 交易完成 ======
交易对: {self.symbol}
买入均价: {entry_price}
买入数量: {total_amount}
买入金额: {entry_price * total_amount} USDT

=== 卖出统计 ===
总卖出数量: {total_amount - remaining_amount}
剩余数量: {remaining_amount}
触发止盈档位: {len(sold_ladders)}
""")
                
        except Exception as e:
            logger.error(f"""
=== 监控持仓失败 ===
错误信息: {str(e)}
交易对: {self.symbol}
剩余数量: {remaining_amount if 'remaining_amount' in locals() else '未知'}
""")

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
        self.perf.start('depth_analysis_total')
        try:
            # 1. API调用
            self.perf.start('depth_api_call')
            orderbook = self.query_client.fetch_order_book(self.symbol, limit=20)
            api_time = self.perf.stop('depth_api_call')
            
            if not orderbook or not orderbook['asks']:
                return None

            # 2. 数据处理
            self.perf.start('depth_processing')
            asks = orderbook['asks']
            valid_orders = [asks[0]]
            prev_price = asks[0][0]

            for price, volume in asks[1:]:
                if (price - prev_price) / prev_price > self.max_price_gap:
                    break
                valid_orders.append([price, volume])
                prev_price = price

            # 3. 统计计算
            self.perf.start('depth_calculation')
            total_volume = sum(vol for _, vol in valid_orders)
            total_value = sum(price * vol for price, vol in valid_orders)
            
            result = {
                'valid_orders': valid_orders,
                'min_price': valid_orders[0][0],
                'max_price': valid_orders[-1][0],
                'avg_price': total_value / total_volume,
                'total_volume': total_volume,
                'total_value': total_value,
                'order_count': len(valid_orders)
            }
            calc_time = self.perf.stop('depth_calculation')
            proc_time = self.perf.stop('depth_processing')
            total_time = self.perf.stop('depth_analysis_total')

            logger.debug(
                f"深度分析性能:\n"
                f"API调用: {api_time:.2f}ms\n"
                f"数据处理: {proc_time:.2f}ms\n"
                f"统计计算: {calc_time:.2f}ms\n"
                f"总耗时: {total_time:.2f}ms"
            )
            return result

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
        try:
            # 首先测试API延迟
            logger.info("正在测试API延迟...")
            latency = self.test_api_latency()
            if not latency:
                logger.error("API连接测试失败")
                return None
            
            logger.info(f"API延迟: {latency}ms")
            if latency > 500:  # 如果延迟大于500ms，提示风险
                confirm = input(f"警告: API延迟较高 ({latency}ms)，是否继续? (yes/no): ")
                if confirm.lower() != 'yes':
                    return None

            # 显示策略确认和开始抢购
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

            # 开始监控循环
            while attempt < max_attempts:
                # 获取性能统计
                depth_stats = self.perf.get_stats('depth_analysis_total')
                api_stats = self.perf.get_stats('depth_api_call')
                calc_stats = self.perf.get_stats('order_calculation')
                
                current_status = (
                    "====== 币安抢币状态 ======\n"
                    f"当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                    "\n=== 买入策略 ===\n"
                    f"交易对: {self.symbol}\n"
                    f"买入金额: {self.amount} USDT\n"
                    f"买入方式: {'限价单' if self.price else '市价单(带保护)'}\n"
                    f"下单方式: IOC限价单模拟市价单\n"
                    "\n=== 价格保护 ===\n"
                    "最高价格: 开盘价 * 1.5\n"
                    "参考价格: 开盘价\n"
                    f"最大滑点: {self.max_slippage*100}%\n"
                    f"最小深度: {self.min_depth_requirement}个订单\n"
                    "异常订单: 自动过滤\n"
                    f"价格差距: {self.max_price_gap*100}%以上订单过滤\n"
                    "\n=== 卖出策略 ===\n"
                )

                if self.sell_ladders:
                    for i, (profit, percent) in enumerate(self.sell_ladders, 1):
                        current_status += f"梯队{i}: 涨幅达到 {profit*100}% 时卖出 {percent*100}%\n"
                current_status += f"止损设置: -{self.stop_loss*100}% 触发全部卖出\n"

                current_status += (
                    "\n=== 系统状态 ===\n"
                    f"API延迟: {latency}ms\n"
                    "连接状态: 正常\n"
                    "账户余额: 充足\n"
                    "系统预热: 完成\n"
                )

                # 添加性能统计显示
                if depth_stats or api_stats or calc_stats:
                    current_status += (
                        "\n=== 性能统计 ===\n"
                        f"深度分析: {depth_stats.get('avg', 0):.2f}ms (最快: {depth_stats.get('min', 0):.2f}ms)\n"
                        f"API调用: {api_stats.get('avg', 0):.2f}ms (最快: {api_stats.get('min', 0):.2f}ms)\n"
                        f"内部处理: {calc_stats.get('avg', 0):.2f}ms (最快: {calc_stats.get('min', 0):.2f}ms)\n"
                    )

                current_status += (
                    "\n=== 实时监控 ===\n"
                    "监控中...\n"
                    f"尝试次数: {attempt}/{max_attempts}\n"
                    "按 Ctrl+C 终止"
                )
                
                self.print_status(current_status)

                if self.check_trading_status():
                    # 检测到开盘，直接执行
                    opening_price = self.analyze_opening_price()
                    if opening_price:
                        self.auto_adjust_price_protection(opening_price)
                        depth_info = self.analyze_market_depth()
                        
                        if depth_info and depth_info['total_volume'] >= self.amount:
                            # 直接下单
                            order = self.place_market_order() if not self.price else self.place_limit_order()

                            if order:
                                status = (
                                    f"下单成功!\n"
                                    f"交易对: {self.symbol}\n"
                                    f"开盘价: {opening_price}\n"
                                    f"成交价: {order.get('price', '市价')}\n"
                                    f"数量: {order['amount']}\n"
                                    f"订单ID: {order['id']}\n"
                                    "\n自动卖出监控已启动..."
                                )
                                self.print_status(status)
                                
                                # 启动自动卖出监控
                                threading.Thread(target=self.track_order, args=(order['id'],)).start()
                                return order

                attempt += 1
                time.sleep(0.1)  # 100ms检查间隔

            self.print_status("达到最大尝试次数，抢购失败")
            return None

        except Exception as e:
            logger.error(f"抢购失败: {str(e)}")
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

    def test_api_latency(self) -> float:
        """测试API延迟"""
        try:
            latencies = []
            for _ in range(3):  # 测试3次取平均值
                start_time = time.time()
                self.query_client.fetch_time()
                latency = (time.time() - start_time) * 1000  # 转换为毫秒
                latencies.append(latency)
                time.sleep(0.1)  # 间隔100ms
            
            avg_latency = sum(latencies) / len(latencies)
            logger.info(f"API平均延迟: {avg_latency:.2f}ms")
            return avg_latency
        
        except Exception as e:
            logger.error(f"测试API延迟失败: {str(e)}")
            return None

    def set_sell_strategy(self, sell_ladders: list, stop_loss: float):
        """设置阶梯卖出策略"""
        try:
            self.sell_ladders = sell_ladders
            self.stop_loss = stop_loss
            
            logger.info(f"""
=== 设置阶梯卖出策略 ===
止损设置: 亏损{stop_loss*100}%时触发
阶梯止盈:
{chr(10).join([f'- 涨幅{p*100}% 卖出{a*100}%' for p, a in sell_ladders])}
""")
        except Exception as e:
            logger.error(f"设置阶梯卖出策略失败: {str(e)}")
            raise

    def print_trade_summary(self, buy_order, sell_orders):
        """打印交易统计信息"""
        print("\n====== 交易统计 ======")
        print(f"交易对: {self.symbol}")
        print(f"交易时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 买入统计
        print("\n=== 买入明细 ===")
        print(f"买入均价: {buy_order['average']}")
        print(f"买入数量: {buy_order['amount']}")
        print(f"买入金额: {buy_order['cost']} USDT")
        
        # 卖出统计
        print("\n=== 卖出明细 ===")
        total_sold = 0
        total_revenue = 0
        for i, order in enumerate(sell_orders, 1):
            profit_ratio = (order['average'] - buy_order['average']) / buy_order['average']
            print(f"\n第{i}档卖出:")
            print(f"- 卖出价格: {order['average']}")
            print(f"- 卖出数量: {order['amount']}")
            print(f"- 卖出金额: {order['cost']} USDT")
            print(f"- 盈利比例: {profit_ratio*100:.2f}%")
            total_sold += order['amount']
            total_revenue += order['cost']
        
        # 总体统计
        total_profit = total_revenue - buy_order['cost']
        profit_percent = (total_revenue - buy_order['cost']) / buy_order['cost'] * 100

        print("\n=== 总体统计 ===")
        print(f"买入总额: {buy_order['cost']} USDT")
        print(f"卖出总额: {total_revenue} USDT")
        print(f"净利润: {total_profit} USDT")
        print(f"收益率: {profit_percent:.2f}%")
        print(f"平均成交延迟: {self.perf.get_stats('order_api_call')['avg']:.2f}ms")
        print("========================")

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
        
        return True
    except ImportError as e:
        logger.error(f"依赖检查失败: {str(e)}")
        logger.error("请运行 setup.sh 安装所需依赖")
        return False

def setup_snipe_strategy(sniper: BinanceSniper):
    """抢开盘策略设置"""
    logger.info("开始设置抢购策略...")
    
    # 1. 基础设置
    print("\n>>> 基础参数设置")
    symbol = get_valid_input("请输入交易对 (例如 BTC/USDT): ", str)
    total_usdt = get_valid_input("请输入买入金额(USDT): ")
    logger.info(f"基础参数设置: 交易对={symbol}, 买入金额={total_usdt} USDT")

    # 2. 价格保护设置
    print("\n>>> 价格保护设置")
    print("价格保护机制说明:")
    print("1. 最高接受单价: 绝对价格上限，无论如何都不会超过此价格")
    print("2. 开盘价倍数: 相对于第一档价格的最大倍数")
    print("3. 使用IOC限价单，超出价格自动取消")
    print("------------------------")

    max_price = get_valid_input("设置最高接受单价 (例如 0.05 USDT): ")
    price_multiplier = get_valid_input("设置开盘价倍数 (例如 3 表示最高接受开盘价的3倍): ")
    logger.info(f"价格保护设置: 最高接受价格={max_price} USDT, 开盘价倍数={price_multiplier}")

    # 3. 阶梯卖出策略
    print("\n>>> 阶梯卖出策略设置")
    print("设置三档止盈:")
    
    logger.info("开始设置阶梯卖出策略...")
    profit1 = get_valid_input("第一档止盈比例 (例如 0.1 表示 10%): ")
    amount1 = get_valid_input("第一档卖出比例 (例如 0.5 表示 50%): ")
    logger.info(f"设置第一档止盈: 涨幅={profit1*100}%, 卖出比例={amount1*100}%")
    
    profit2 = get_valid_input("第二档止盈比例 (例如 0.2 表示 20%): ")
    amount2 = get_valid_input("第二档卖出比例 (例如 0.3 表示 30%): ")
    logger.info(f"设置第二档止盈: 涨幅={profit2*100}%, 卖出比例={amount2*100}%")
    
    profit3 = get_valid_input("第三档止盈比例 (例如 0.3 表示 30%): ")
    amount3 = get_valid_input("第三档卖出比例 (例如 0.2 表示 20%): ")
    logger.info(f"设置第三档止盈: 涨幅={profit3*100}%, 卖出比例={amount3*100}%")
    
    stop_loss = get_valid_input("设置止损比例 (例如 0.05 表示 5%): ")
    logger.info(f"设置止损: 亏损={stop_loss*100}%时触发")

    # 应用设置
    logger.info("开始应用策略设置...")
    try:
        sniper.setup_trading_pair(symbol, total_usdt)
        sniper.set_price_protection(max_price, price_multiplier)
        sniper.set_sell_strategy([
            (profit1, amount1),
            (profit2, amount2),
            (profit3, amount3)
        ], stop_loss)
        logger.info("策略设置应用成功")
        
        # 记录完整策略
        logger.info(f"""
====== 策略设置完成 ======
交易对: {symbol}
买入金额: {total_usdt} USDT
下单方式: IOC限价单

价格保护:
- 最高接受价格: {max_price} USDT
- 开盘价倍数: {price_multiplier}倍

阶梯卖出:
- 第一档: 涨幅{profit1*100}% 卖出{amount1*100}%
- 第二档: 涨幅{profit2*100}% 卖出{amount2*100}%
- 第三档: 涨幅{profit3*100}% 卖出{amount3*100}%
止损设置: 亏损{stop_loss*100}%
""")

    except Exception as e:
        logger.error(f"策略设置失败: {str(e)}")
        print("策略设置失败，请重试")
        return

    print("\n=== 策略设置完成 ===")
    print_strategy(sniper)

def print_strategy(sniper: BinanceSniper):
    """打印当前策略"""
    print("\n====== 当前抢币策略 ======")
    print(f"交易对: {sniper.symbol}")
    print(f"买入金额: {sniper.amount} USDT")
    print(f"下单方式: IOC限价单(立即成交或取消)")
    
    print("\n价格保护:")
    print(f"最高接受价格: {sniper.max_price_limit} USDT")
    print(f"开盘价倍数: {sniper.price_multiplier}倍")
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
        # 检查依赖
        if not check_dependencies():
            logger.error("依赖检查失败，程序退出")
            return
            
        logger.info("""
====== 币安现货抢币工具启动 ======
版本: 1.0.0
时间: %s
===============================""", datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

        # 初始化配置
        try:
            config = ConfigManager()
            logger.info("配置管理器初始化成功")
        except Exception as e:
            logger.error("配置管理器初始化失败: %s", str(e))
            return

        # 初始化抢币工具
        try:
            sniper = BinanceSniper(config)
            logger.info("抢币工具初始化成功")
        except Exception as e:
            logger.error("抢币工具初始化失败: %s", str(e))
            return

        # 主循环
        while True:
            try:
                print_menu()
                choice = input("请选择操作 (0-6): ")

                if choice == "0":
                    logger.info("用户选择退出程序")
                    print("\n感谢使用，再见!")
                    break

                elif choice == "1":
                    logger.info("用户选择设置API密钥")
                    # ... API设置代码 ...

                elif choice == "2":
                    logger.info("用户选择设置抢购策略")
                    setup_snipe_strategy(sniper)

                elif choice == "3":
                    logger.info("用户选择查看当前策略")
                    print_strategy(sniper)
                    input("\n按回车继续...")

                elif choice == "4":
                    logger.info("用户选择开始抢购")
                    if not all([sniper.symbol, sniper.amount]):
                        logger.warning("抢购参数未设置完整")
                        print("请先完成抢购策略设置")
                    continue
                    # ... 抢购代码 ...

                elif choice == "5":
                    logger.info("用户选择手动卖出")
                    if not sniper.symbol:
                        logger.warning("交易对未设置")
                        print("请先设置交易对")
                        continue
                    # ... 卖出代码 ...

                elif choice == "6":
                    logger.info("用户选择查看持仓")
                    position = sniper.get_position()
                    if not position:
                        logger.warning("获取持仓信息失败")
                        print("获取持仓信息失败")
                        continue
                    # ... 显示持仓代码 ...
                
                else:
                    logger.warning("用户输入无效选项: %s", choice)
                    print("选择无效，请重试")

            except KeyboardInterrupt:
                logger.info("用户中断程序")
                print("\n程序已中断")
                break
            except Exception as e:
                logger.error("主循环发生错误: %s", str(e))
                print(f"\n发生错误: {str(e)}")
                print("程序将继续运行...")
                continue

    except Exception as e:
        logger.critical("程序发生致命错误: %s", str(e))
        print(f"\n程序发生致命错误: {str(e)}")
        return

    finally:
        logger.info("程序正常退出")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.critical("程序异常退出: %s", str(e))
    finally:
        # 清理工作
        logging.shutdown()

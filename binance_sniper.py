import ccxt
import time
from datetime import datetime, timezone, timedelta
import logging
import json
import os
import threading
import sys
from logging.handlers import RotatingFileHandler
from collections import defaultdict
import configparser
from typing import Tuple
import websocket  # 添加 websocket 导入
import requests
import pytz
from requests.adapters import HTTPAdapter, Retry
from urllib3.util.retry import Retry
from functools import wraps
import glob
import aiohttp
import asyncio
import argparse
import daemon
import signal
import lockfile

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

def retry_on_error(max_retries=3, delay=0.1, backoff=2, exceptions=(Exception,)):
    """重试装饰器
    
    Args:
        max_retries: 最大重试次数
        delay: 初始延迟时间
        backoff: 延迟时间的增长因子
        exceptions: 需要重试的异常类型
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            retries = 0
            current_delay = delay
            
            while retries < max_retries:
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    retries += 1
                    if retries == max_retries:
                        logger.error(f"{func.__name__} 达到最大重试次数 {max_retries}")
                        raise
                    
                    logger.warning(
                        f"{func.__name__} 失败, 正在重试 ({retries}/{max_retries})\n"
                        f"错误: {str(e)}\n"
                        f"等待 {current_delay:.2f}s 后重试"
                    )
                    
                    time.sleep(current_delay)
                    current_delay *= backoff
            
            return None
        return wrapper
    return decorator

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
        
        # 设置时区
        self.timezone = pytz.timezone('Asia/Shanghai')  # 东八区
        self.time_offset = None  # 与币安服务器的时间差
        self.network_latency = None  # 网络延迟
        self.optimal_advance_time = None  # 最优提前下单时间
        
        # 初始化时同步时间
        self.sync_time()
        
        self.concurrent_orders = 3  # 并发订单数量
        self.advance_time = 100  # 默认提前100ms
        
        # 初始化连接池
        self._init_connection_pools()

        self.perf_timer = time.perf_counter  # 使用性能计数器
        self._resources = set()  # 跟踪需要清理的资源
        self.performance_stats = defaultdict(list)  # 性能统计

    def _init_connection_pools(self):
        """初始化优化的连接池"""
        try:
            # 创建 retry 策略
            retry_strategy = Retry(
                total=3,
                backoff_factor=0.1,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=frozenset(['GET', 'POST'])
            )
            
            # 为交易和查询客户端分别创建会话
            self.trade_session = self._create_optimized_session(retry_strategy)
            self.query_session = self._create_optimized_session(retry_strategy)
            
            # 替换ccxt客户端的session
            if hasattr(self.trade_client, 'session'):
                self.trade_client.session = self.trade_session
            if hasattr(self.query_client, 'session'):
                self.query_client.session = self.query_session
            
            # 创建异步会话
            self.async_session = None
            
            logger.info("""
=== 连接池优化完成 ===
- 交易客户端连接池: 20个连接
- 查询客户端连接池: 20个连接
- 重试策略: 3次重试
- 超时设置: 连接3s, 读取27s
- Keep-Alive: 启用
""")
            
        except Exception as e:
            logger.error(f"初始化连接池失败: {str(e)}")
            raise
    
    def _create_optimized_session(self, retry_strategy):
        """创建优化的会话"""
        session = requests.Session()
        
        # 创建连接适配器
        adapter = HTTPAdapter(
            pool_connections=20,    # 连接池大小
            pool_maxsize=20,        # 最大连接数
            max_retries=retry_strategy,
            pool_block=False
        )
        
        # 应用适配器
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        
        # 优化会话设置
        session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': '300',
            'User-Agent': 'BinanceSniper/1.0'
        })
        
        # 设置超时
        session.timeout = (3.05, 27)
        
        return session
    
    async def _init_async_session(self):
        """初始化异步会话"""
        if self.async_session is None:
            timeout = aiohttp.ClientTimeout(total=30)
            connector = aiohttp.TCPConnector(
                limit=100,              # 最大并发连接数
                ttl_dns_cache=300,      # DNS缓存时间
                use_dns_cache=True,     # 启用DNS缓存
                force_close=False       # 保持连接
            )
            self.async_session = aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                headers={
                    'User-Agent': 'BinanceSniper/1.0',
                    'Connection': 'keep-alive'
                }
            )
    
    def sync_time(self) -> bool:
        """同步币安服务器时间（优化版 - 单次请求获取延迟和时间）"""
        try:
            measurements = []
            
            # 进行10次测试以获得更准确的数据
            for _ in range(10):
                # 使用性能计数器获取更精确的时间
                t1 = self.perf_timer() * 1000  # 发送前的本地时间
                
                # 单次请求获取服务器时间
                server_time = self.query_client.fetch_time()
                
                t2 = self.perf_timer() * 1000  # 接收到响应的本地时间
                
                # 计算这次请求的数据
                rtt = t2 - t1  # 往返总时间
                latency = rtt / 2  # 假设网络延迟是对称的
                
                # 估算服务器处理时间为t1 + latency时的时间
                estimated_request_arrive = t1 + latency
                time_offset = server_time - estimated_request_arrive
                
                measurements.append({
                    'rtt': rtt,
                    'latency': latency,
                    'offset': time_offset,
                    'server_time': server_time,
                    'local_send': t1,
                    'local_receive': t2
                })
                
                time.sleep(0.1)  # 避免API限制
            
            # 按RTT排序，取中间的几个样本（去除异常值）
            measurements.sort(key=lambda x: x['rtt'])
            valid_measurements = measurements[2:-2]  # 去除最高和最低的2个
            
            # 使用最稳定的样本计算平均值
            best_measurement = valid_measurements[0]  # RTT最小的样本
            self.network_latency = best_measurement['latency']
            self.time_offset = best_measurement['offset']
            
            # 计算标准差以评估稳定性
            latencies = [m['latency'] for m in valid_measurements]
            offsets = [m['offset'] for m in valid_measurements]
            
            latency_std = (sum((l - self.network_latency) ** 2 for l in latencies) / len(latencies)) ** 0.5
            offset_std = (sum((o - self.time_offset) ** 2 for o in offsets) / len(offsets)) ** 0.5
            
            # 使用最后一次测量的服务器时间显示当前时间
            current_measurement = measurements[-1]
            local_time = datetime.fromtimestamp(current_measurement['local_receive']/1000, self.timezone)
            server_time = datetime.fromtimestamp(current_measurement['server_time']/1000, self.timezone)
            
            logger.info(f"""
=== 时间同步详情 ===
本地时间: {local_time.strftime('%Y-%m-%d %H:%M:%S.%f')}
服务器时间: {server_time.strftime('%Y-%m-%d %H:%M:%S.%f')}
网络延迟: {self.network_latency:.2f}ms (标准差: {latency_std:.2f}ms)
时间偏移: {self.time_offset:.2f}ms (标准差: {offset_std:.2f}ms)
时间状态: {'正常' if abs(self.time_offset) < 1000 else '需要同步'}
样本数量: {len(valid_measurements)}
测量稳定性: {'良好' if latency_std < 10 and offset_std < 10 else '一般'}

详细分析:
- 往返时间(RTT): {best_measurement['rtt']:.2f}ms
- 请求发送时间: {datetime.fromtimestamp(best_measurement['local_send']/1000, self.timezone).strftime('%H:%M:%S.%f')}
- 请求接收时间: {datetime.fromtimestamp(best_measurement['local_receive']/1000, self.timezone).strftime('%H:%M:%S.%f')}
- 服务器时间点: {datetime.fromtimestamp(best_measurement['server_time']/1000, self.timezone).strftime('%H:%M:%S.%f')}
""")
            
            return True
            
        except Exception as e:
            logger.error(f"同步时间失败: {str(e)}")
            return False
    
    def get_server_time(self) -> datetime:
        """获取当前币安服务器时间"""
        if self.time_offset is None:
            self.sync_time()
        
        current_time = int(time.time() * 1000) + (self.time_offset or 0)
        return datetime.fromtimestamp(current_time/1000, self.timezone)

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

    @retry_on_error(max_retries=3, delay=0.1)
    def place_market_order(self):
        """下市价单(使用优化的连接池)"""
        try:
            # 使用优化的trade_session
            endpoint = f"{self.trade_client.urls['api']}/v3/order"
            params = {
                'symbol': self.symbol.replace('/', ''),
                'side': 'BUY',
                'type': 'MARKET',
                'quantity': self.amount,
                'timestamp': int(time.time() * 1000)
            }
            
            # 添加签名
            signature = self._generate_signature(params)
            params['signature'] = signature
            
            response = self._make_request(
                self.trade_session,
                'POST',
                endpoint,
                params=params,
                headers={'X-MBX-APIKEY': self.trade_client.apiKey}
            )
            
            return self._format_order_response(response)
            
        except Exception as e:
            logger.error(f"市价下单失败: {str(e)}")
            raise

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

    def calculate_optimal_advance_time(self):
        """计算最优提前下单时间(使用高精度计时器)"""
        try:
            latencies = []
            offsets = []
            
            for _ in range(10):
                request_start = self.perf_timer() * 1000
                server_time = self.query_client.fetch_time()
                response_end = self.perf_timer() * 1000
                
                latency = (response_end - request_start) / 2
                latencies.append(latency)
                
                offset = server_time - (request_start + latency)
                offsets.append(offset)
                
                time.sleep(0.1)
            
            # 使用中位数而不是平均值，避免异常值影响
            latencies.sort()
            offsets.sort()
            mid = len(latencies) // 2
            
            self.network_latency = latencies[mid]
            self.time_offset = offsets[mid]
            
            logger.info(f"""
=== 时间同步优化 ===
网络延迟(中位数): {self.network_latency:.3f}ms
时间偏移(中位数): {self.time_offset:.3f}ms
样本数量: {len(latencies)}
延迟范围: {min(latencies):.3f}ms - {max(latencies):.3f}ms
""")
            
            return True
        except Exception as e:
            logger.error(f"计算最优提前时间失败: {str(e)}")
            return False

    def set_snipe_params(self, advance_time: int = 100, concurrent_orders: int = 3):
        """设置抢购参数"""
        self.advance_time = advance_time
        self.concurrent_orders = concurrent_orders
        logger.info(f"""
=== 抢购参数设置 ===
提前下单时间: {advance_time}ms
并发订单数量: {concurrent_orders}
""")

    def snipe(self):
        """开始抢购（包含自动预热和准备）"""
        try:
            if not self.check_balance():
                return None

            # 计算距离开盘的时间
            current_time = datetime.now(self.timezone)
            time_to_open = (self.opening_time - current_time).total_seconds()

            logger.info(f"""
=== 开始自动抢购流程 ===
目标开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S')}
当前时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')}
距离开盘: {time_to_open:.1f}秒
""")

            # 预热阶段（如果时间充足）
            if time_to_open > 300:  # 如果距离开盘超过5分钟
                logger.info("等待进入预热阶段...")
                time.sleep(time_to_open - 300)  # 等待到开盘前5分钟
                
            # 开始系统预热
            logger.info("开始系统预热...")
            self.warmup()
            
            # 准备阶段：定期同步时间直到接近开盘
            last_sync = 0
            while True:
                current_time = datetime.now(self.timezone)
                time_to_open = (self.opening_time - current_time).total_seconds()
                
                # 进入最终执行阶段
                if time_to_open <= 10:
                    logger.info("进入最终执行阶段...")
                    break
                    
                # 每30秒同步一次时间
                if time.time() - last_sync >= 30:
                    logger.info(f"距离开盘还有: {time_to_open:.1f} 秒")
                    self.sync_time()
                    last_sync = time.time()
                    
                # 开盘前2分钟显示详细状态
                if time_to_open <= 120:
                    logger.info(f"""
=== 抢购准备状态 ===
距离开盘: {time_to_open:.1f}秒
网络延迟: {self.network_latency:.2f}ms
时间偏移: {self.time_offset:.2f}ms
系统状态: {'正常' if abs(self.time_offset) < 1000 else '需要同步'}
""")
                
                time.sleep(1)

            # 最后一次时间同步
            self.sync_time()
            
            # 计算发送时间
            target_server_time = self.opening_time.timestamp() * 1000
            send_advance_time = (
                self.network_latency +  # 网络延迟补偿
                abs(self.time_offset) + # 时间偏移补偿
                10  # 额外安全余量
            )
            local_send_time = target_server_time - send_advance_time
            
            logger.info(f"""
=== 最终执行计划 ===
目标服务器时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S.%f')}
本地发送时间: {datetime.fromtimestamp(local_send_time/1000, self.timezone).strftime('%Y-%m-%d %H:%M:%S.%f')}
提前量分析:
- 网络延迟补偿: {self.network_latency:.2f}ms
- 时间偏移补偿: {abs(self.time_offset):.2f}ms
- 安全余量: 10ms
总提前量: {send_advance_time:.2f}ms
并发订单数: {self.concurrent_orders}
""")

            # 等待并发送订单
            while True:
                current_local_time = time.time() * 1000
                if current_local_time >= local_send_time:
                    # 并发发送订单
                    orders = []
                    threads = []
                    order_ids = set()
                    
                    start_time = time.time() * 1000
                    
                    # 创建并启动所有下单线程
                    for _ in range(self.concurrent_orders):
                        thread = threading.Thread(
                            target=lambda: self._place_order_and_collect(orders, order_ids)
                        )
                        threads.append(thread)
                        thread.start()
                    
                    # 等待所有订单完成
                    for thread in threads:
                        thread.join(timeout=1.0)
                    
                    # 处理订单结果
                    success_order = self._handle_orders(order_ids)
                    
                    end_time = time.time() * 1000
                    
                    logger.info(f"""
=== 抢购执行结果 ===
发送订单数量: {len(order_ids)}
成功成交订单: {'是' if success_order else '否'}
总执行时间: {end_time - start_time:.2f}ms
撤单情况: 已撤销{len(order_ids) - (1 if success_order else 0)}个订单
""")
                    
                    return success_order
                
                time.sleep(0.0001)  # 0.1ms的检查间隔

        except Exception as e:
            logger.error(f"抢购过程发生错误: {str(e)}")
            return None

    def _place_order_and_collect(self, orders: list, order_ids: set):
        """下单并收集订单ID"""
        try:
            order = self.place_market_order() if not self.price else self.place_limit_order()
            if order and order.get('id'):
                orders.append(order)
                order_ids.add(order['id'])
        except Exception as e:
            logger.error(f"下单失败: {str(e)}")

    def _handle_orders(self, order_ids: set) -> dict:
        """处理订单结果"""
        try:
            # 先快速检查是否有已成交的订单
            success_order = None
            for order_id in order_ids:
                try:
                    order_status = self.query_client.fetch_order(order_id, self.symbol)
                    if order_status['status'] == 'closed':
                        success_order = order_status
                        logger.info(f"""
=== 订单成交 ===
订单ID: {order_id}
成交价格: {order_status['average']}
成交数量: {order_status['filled']}
成交金额: {order_status['cost']} USDT
""")
                        break
                except Exception as e:
                    logger.error(f"检查订单 {order_id} 状态失败: {str(e)}")
                    continue

            # 撤销多余订单
            cancel_threads = []
            for order_id in order_ids:
                if not success_order or order_id != success_order['id']:
                    thread = threading.Thread(
                        target=self.cancel_order,
                        args=(order_id,)
                    )
                    cancel_threads.append(thread)
                    thread.start()

            # 等待所有撤单完成
            for thread in cancel_threads:
                thread.join(timeout=1.0)

            if success_order:
                logger.info("已撤销所有多余订单")
            else:
                logger.info("没有订单成交，已撤销所有订单")

            return success_order

        except Exception as e:
            logger.error(f"处理订单状态时出错: {str(e)}")
            # 发生错误时尝试撤销所有订单
            self.cancel_all_orders()
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
            
            # 添加延迟评估
            if avg_latency < 50:
                status = "极佳 ✨"
            elif avg_latency < 200:
                status = "良好 ✅"
            elif avg_latency < 500:
                status = "一般 ⚠️"
            else:
                status = "较差 ❌"
            
            logger.info(f"API平均延迟: {avg_latency:.2f}ms")
            return avg_latency, status
        
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

    def optimize_network_connection(self):
        """优化网络连接"""
        # 使用keep-alive
        self.query_client.session.headers.update({
            'Connection': 'keep-alive',
            'Keep-Alive': '300'
        })
        
        # 启用HTTP/2
        self.query_client.session.mount('https://', HTTP20Adapter())
        
        # 预连接池
        self.query_client.session.mount('https://', HTTPAdapter(
            pool_connections=20,
            pool_maxsize=20,
            max_retries=2,
            pool_block=False
        ))

    def test_server_latency(self):
        """测试不同服务器延迟"""
        endpoints = {
            'api1': 'api.binance.com',
            'api2': 'api1.binance.com',
            'api3': 'api2.binance.com',
            'api4': 'api3.binance.com'
        }
        
        results = {}
        for name, endpoint in endpoints.items():
            latencies = []
            for _ in range(5):
                start = time.perf_counter()
                try:
                    requests.get(f'https://{endpoint}/api/v3/time', 
                               timeout=1)
                    latency = (time.perf_counter() - start) * 1000
                    latencies.append(latency)
                except:
                    continue
            if latencies:
                results[name] = min(latencies)
        
        # 使用延迟最低的endpoint
        best_endpoint = min(results.items(), key=lambda x: x[1])
        logger.info(f"最佳endpoint: {best_endpoint[0]} ({best_endpoint[1]:.2f}ms)")
        return best_endpoint

    def set_opening_time(self, opening_time: datetime):
        """设置开盘时间"""
        # 确保时间是东八区的时间
        if opening_time.tzinfo is None:
            opening_time = self.timezone.localize(opening_time)
        
        self.opening_time = opening_time
        
        # 计算与服务器时间的差距
        server_time = self.get_server_time()
        time_diff = opening_time - server_time
        
        logger.info(f"""
=== 开盘时间设置 ===
开盘时间: {opening_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)
当前服务器时间: {server_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)
距离开盘: {time_diff.total_seconds()/3600:.1f}小时
""")

    def cleanup(self):
        """清理资源"""
        try:
            # 关闭会话
            if hasattr(self, 'trade_session'):
                self.trade_session.close()
            if hasattr(self, 'query_session'):
                self.query_session.close()
            
            # 关闭异步会话
            if self.async_session:
                asyncio.run(self.async_session.close())
            
            # 关闭API客户端
            for client in [self.trade_client, self.query_client]:
                if hasattr(client, 'close'):
                    client.close()
            
            # 关闭所有活跃的WebSocket连接
            for resource in self._resources:
                if hasattr(resource, 'close'):
                    resource.close()
            
            # 保存性能统计
            self._save_performance_stats()
            
            # 清理临时文件
            self._cleanup_temp_files()
            
            logger.info("资源清理完成")
            
        except Exception as e:
            logger.error(f"清理资源失败: {str(e)}")
        finally:
            self._resources.clear()
    
    def _save_performance_stats(self):
        """保存性能统计数据"""
        try:
            stats = {
                'api_latency': {
                    'min': min(self.performance_stats['api_latency']),
                    'max': max(self.performance_stats['api_latency']),
                    'avg': sum(self.performance_stats['api_latency']) / len(self.performance_stats['api_latency'])
                },
                'order_execution': {
                    'min': min(self.performance_stats['order_execution']),
                    'max': max(self.performance_stats['order_execution']),
                    'avg': sum(self.performance_stats['order_execution']) / len(self.performance_stats['order_execution'])
                }
            }
            
            with open('performance_stats.json', 'w') as f:
                json.dump(stats, f, indent=2)
            
            logger.info("性能统计数据已保存")
            
        except Exception as e:
            logger.error(f"保存性能统计失败: {str(e)}")
    
    def _cleanup_temp_files(self):
        """清理临时文件"""
        temp_files = ['*.tmp', '*.log.?', 'performance_stats_*.json']
        for pattern in temp_files:
            try:
                for file in glob.glob(pattern):
                    if os.path.isfile(file):
                        os.remove(file)
            except Exception as e:
                logger.error(f"清理临时文件失败: {str(e)}")
    
    def __enter__(self):
        """上下文管理器支持"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """确保资源被正确清理"""
        self.cleanup()

    def prepare_and_snipe(self):
        """准备并执行抢购"""
        try:
            if not self.opening_time:
                logger.error("未设置开盘时间")
                return None

            # 计算距离开盘的时间
            current_time = datetime.now(self.timezone)
            time_to_open = (self.opening_time - current_time).total_seconds()

            logger.info(f"""
=== 抢购准备开始 ===
目标开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S')}
当前时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')}
距离开盘: {time_to_open:.1f}秒
""")

            # 如果距离开盘超过5分钟，等待到提前5分钟
            if time_to_open > 300:
                wait_time = time_to_open - 300
                logger.info(f"等待 {wait_time:.1f} 秒后开始系统预热...")
                time.sleep(wait_time)

            # 系统预热
            logger.info("开始系统预热...")
            self.warmup()

            # 开始定期同步时间
            last_sync = 0
            while True:
                current_time = datetime.now(self.timezone)
                time_to_open = (self.opening_time - current_time).total_seconds()

                # 如果距离开盘小于10秒，进入最终准备阶段
                if time_to_open <= 10:
                    logger.info("""
=== 进入最终准备阶段 ===
- 执行最后一次时间同步
- 准备订单参数
- 等待精确时间发送
""")
                    # 最后一次时间同步
                    self.sync_time()
                    # 开始抢购
                    return self.snipe()

                # 每30秒同步一次时间
                if time.time() - last_sync >= 30:
                    logger.info(f"距离开盘还有: {time_to_open:.1f} 秒")
                    self.sync_time()
                    last_sync = time.time()

                # 在开盘前2分钟，显示更详细的信息
                if time_to_open <= 120:
                    logger.info(f"""
=== 抢购准备状态 ===
距离开盘: {time_to_open:.1f}秒
网络延迟: {self.network_latency:.2f}ms
时间偏移: {self.time_offset:.2f}ms
系统状态: {'正常' if abs(self.time_offset) < 1000 else '需要同步'}
""")

                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("程序被用户中断")
            return None
        except Exception as e:
            logger.error(f"准备过程发生错误: {str(e)}")
            return None

    def setup_snipe_strategy(self):
        """设置抢购策略"""
        try:
            logger.info("开始设置抢购策略...")
            
            # 1. 基础设置
            print("\n>>> 基础参数设置")
            symbol = get_valid_input("请输入交易对 (例如 BTC/USDT): ", str)
            total_usdt = get_valid_input("请输入买入金额(USDT): ", float)
            
            # 2. 价格保护设置
            print("\n>>> 价格保护设置")
            print("价格保护机制说明:")
            print("1. 最高接受单价: 绝对价格上限，无论如何都不会超过此价格")
            print("2. 开盘价倍数: 相对于第一档价格的最大倍数")
            print("3. 使用IOC限价单，超出价格自动取消")
            
            max_price = get_valid_input("设置最高接受单价 (例如 0.05 USDT): ", float)
            price_multiplier = get_valid_input("设置开盘价倍数 (例如 3 表示最高接受开盘价的3倍): ", float)
            
            # 3. 并发和时间设置
            print("\n>>> 执行参数设置")
            concurrent_orders = get_valid_input("并发订单数量 (建议1-5): ", int)
            advance_time = get_valid_input("提前下单时间(ms) (建议50-200): ", int)
            
            # 4. 止盈止损设置
            print("\n>>> 止盈止损设置")
            stop_loss = get_valid_input("止损比例 (例如 0.05 表示 5%): ", float)
            take_profit = get_valid_input("止盈比例 (例如 0.1 表示 10%): ", float)
            
            # 应用设置
            self.setup_trading_pair(symbol, total_usdt)
            self.set_price_protection(max_price, price_multiplier)
            self.set_snipe_params(advance_time, concurrent_orders)
            self.set_profit_strategy(take_profit, stop_loss)
            
            # 显示设置结果
            logger.info(f"""
=== 抢购策略设置完成 ===
基础参数:
- 交易对: {symbol}
- 买入金额: {total_usdt} USDT

价格保护:
- 最高接受价格: {max_price} USDT
- 开盘价倍数: {price_multiplier}倍

执行参数:
- 并发订单数: {concurrent_orders}
- 提前下单时间: {advance_time}ms

止盈止损:
- 止损比例: {stop_loss*100}%
- 止盈比例: {take_profit*100}%
""")
            
            return True
            
        except Exception as e:
            logger.error(f"设置抢购策略失败: {str(e)}")
            return False

    def set_profit_strategy(self, take_profit: float, stop_loss: float):
        """设置止盈止损策略"""
        self.take_profit = take_profit
        self.stop_loss = stop_loss
        logger.info(f"""
=== 止盈止损策略设置 ===
止盈比例: {take_profit*100}%
止损比例: {stop_loss*100}%
""")

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
    
    # 添加开盘时间设置
    print("\n>>> 开盘时间设置")
    print("请输入开盘时间 (东八区/北京时间)")
    
    # 先同步时间显示当前时间作为参考
    server_time = sniper.get_server_time()
    print(f"\n当前币安服务器时间: {server_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
    
    while True:
        try:
            # 只做基本的格式验证
            year = get_valid_input("年 (例如 2024): ", int)
            if year < 2000 or year > 2100:  # 简单的年份范围检查
                print("年份格式无效，请输入2000-2100之间的年份")
                continue
                
            month = get_valid_input("月 (1-12): ", int)
            if month < 1 or month > 12:
                print("月份必须在1-12之间")
                continue
                
            day = get_valid_input("日 (1-31): ", int)
            if day < 1 or day > 31:
                print("日期必须在1-31之间")
                continue
                
            hour = get_valid_input("时 (0-23): ", int)
            if hour < 0 or hour > 23:
                print("小时必须在0-23之间")
                continue
                
            minute = get_valid_input("分 (0-59): ", int)
            if minute < 0 or minute > 59:
                print("分钟必须在0-59之间")
                continue
                
            second = get_valid_input("秒 (0-59): ", int)
            if second < 0 or second > 59:
                print("秒数必须在0-59之间")
                continue
            
            # 创建东八区的时间
            tz = pytz.timezone('Asia/Shanghai')
            opening_time = tz.localize(datetime(year, month, day, hour, minute, second))
            
            # 显示设置的时间信息
            print(f"\n=== 时间信息 ===")
            print(f"设置开盘时间: {opening_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
            print(f"当前服务器时间: {server_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
            
            # 确认设置
            confirm = input("\n确认使用这个开盘时间? (yes/no): ")
            if confirm.lower() == 'yes':
                break
            else:
                continue
            
        except ValueError as e:
            print(f"时间格式无效，请重新输入")
            continue

    logger.info(f"基础参数设置: 交易对={symbol}, 买入金额={total_usdt} USDT, 开盘时间={opening_time}")

    # 2. 价格保护设置
    print("\n>>> 价格保护设置")
    print("价格保护机制说明:")
    print("1. 最高接受单价: 绝对价格上限，无论如何都不会超过此价格")
    print("2. 开盘价倍数: 相对于第一档价格的最大倍数")
    print("3. 使用IOC限价单，超出价格自动取消")
    print("------------------------")

    max_price = get_valid_input("设置最高接受单价 (例如 0.05 USDT): ")
    price_multiplier = get_valid_input("设置开盘价倍数 (例如 3 表示最高接受开盘价的3倍): ")

    # 3. 阶梯卖出策略
    print("\n>>> 阶梯卖出策略设置")
    print("设置三档止盈:")
    
    profit1 = get_valid_input("第一档止盈比例 (例如 0.1 表示 10%): ")
    amount1 = get_valid_input("第一档卖出比例 (例如 0.5 表示 50%): ")
    
    profit2 = get_valid_input("第二档止盈比例 (例如 0.2 表示 20%): ")
    amount2 = get_valid_input("第二档卖出比例 (例如 0.3 表示 30%): ")
    
    profit3 = get_valid_input("第三档止盈比例 (例如 0.3 表示 30%): ")
    amount3 = get_valid_input("第三档卖出比例 (例如 0.2 表示 20%): ")
    
    stop_loss = get_valid_input("设置止损比例 (例如 0.05 表示 5%): ")

    # 应用设置
    try:
        sniper.setup_trading_pair(symbol, total_usdt)
        sniper.set_price_protection(max_price, price_multiplier)
        sniper.set_sell_strategy([
            (profit1, amount1),
            (profit2, amount2),
            (profit3, amount3)
        ], stop_loss)
        sniper.set_opening_time(opening_time)  # 设置开盘时间
        
        # 显示完整策略
        print(f"""
====== 策略设置完成 ======
交易对: {symbol}
买入金额: {total_usdt} USDT
开盘时间: {opening_time.strftime('%Y-%m-%d %H:%M:%S')}
距离开盘: {hours:.1f}小时

价格保护:
- 最高接受价格: {max_price} USDT
- 开盘价倍数: {price_multiplier}倍

阶梯卖出:
- 第一档: 涨幅{profit1*100}% 卖出{amount1*100}%
- 第二档: 涨幅{profit2*100}% 卖出{amount2*100}%
- 第三档: 涨幅{profit3*100}% 卖出{amount3*100}%
止损设置: 亏损{stop_loss*100}%

系统将在开盘前2分钟自动启动监控
""")

    except Exception as e:
        logger.error(f"策略设置失败: {str(e)}")
        print("策略设置失败，请重试")
        return

    input("\n按回车继续...")

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

def run_as_daemon():
    """以守护进程方式运行"""
    try:
        # 获取当前工作目录
        work_dir = os.getcwd()
        
        # 创建PID文件目录
        pid_dir = '/var/run/binance-sniper'
        if not os.path.exists(pid_dir):
            os.makedirs(pid_dir, mode=0o755)
        
        # 配置守护进程上下文
        context = daemon.DaemonContext(
            working_directory=work_dir,  # 使用当前目录
            umask=0o002,
            pidfile=lockfile.FileLock('/var/run/binance-sniper/sniper.pid'),
            signal_map={
                signal.SIGTERM: lambda signo, frame: cleanup_and_exit(),
                signal.SIGINT: lambda signo, frame: cleanup_and_exit(),
                signal.SIGHUP: lambda signo, frame: None,  # 忽略SIGHUP信号
            }
        )
        
        # 确保日志目录存在
        log_dir = '/var/log/binance-sniper'
        if not os.path.exists(log_dir):
            os.makedirs(log_dir, mode=0o755)
        
        # 启动守护进程
        with context:
            logger.info("Binance Sniper守护进程已启动")
            logger.info(f"工作目录: {work_dir}")
            logger.info(f"PID文件: {pid_dir}/sniper.pid")
            main_loop()
            
    except Exception as e:
        logger.error(f"守护进程启动失败: {str(e)}")
        sys.exit(1)

def cleanup_and_exit():
    """清理资源并退出"""
    logger.info("正在关闭守护进程...")
    # 执行清理操作
    sys.exit(0)

def main_loop():
    """主循环"""
    try:
        config = ConfigManager()
        with BinanceSniper(config) as sniper:
            # 从配置文件读取策略
            sniper.load_strategy()
            
            # 等待开盘时间
            while True:
                if sniper.is_ready_to_snipe():
                    result = sniper.snipe()
                    if result:
                        logger.info(f"""
=== 抢购成功 ===
订单ID: {result['id']}
成交价格: {result['average']}
成交数量: {result['filled']}
成交金额: {result['cost']} USDT
""")
                    else:
                        logger.error("抢购失败")
                        
                time.sleep(1)
                
    except Exception as e:
        logger.error(f"主循环发生错误: {str(e)}")
        return

def main():
    try:
        config = ConfigManager()
        with BinanceSniper(config) as sniper:
            while True:
                print_menu()
                choice = input("请选择操作 (0-6): ")

                if choice == "2":  # 抢购策略设置
                    logger.info("用户选择设置抢购策略")
                    if sniper.setup_snipe_strategy():
                        print("\n策略设置成功!")
                        
                        # 测试API延迟
                        print("\n正在测试API延迟...")
                        latency, status = sniper.test_api_latency()
                        print(f"API延迟: {latency:.2f}ms [{status}]")
                        
                        if latency > 200:
                            print("\n警告: 当前网络延迟较高，可能影响抢购成功率")
                            print("建议:")
                            print("1. 检查网络连接")
                            print("2. 使用更好的网络环境")
                            print("3. 增加提前下单时间")
                    else:
                        print("\n策略设置失败，请重试")
                    
                    input("\n按回车继续...")
                    continue

                elif choice == "4":  # 开始抢购
                    if not sniper.symbol or not sniper.amount:
                        print("请先完成抢购策略设置（选项2）")
                        continue
                    
                    if not sniper.opening_time:
                        print("\n请设置开盘时间（东八区）")
                        try:
                            year = get_valid_input("年 (例如 2024): ", int)
                            month = get_valid_input("月 (1-12): ", int)
                            day = get_valid_input("日 (1-31): ", int)
                            hour = get_valid_input("时 (0-23): ", int)
                            minute = get_valid_input("分 (0-59): ", int)
                            second = get_valid_input("秒 (0-59): ", int)
                            
                            opening_time = datetime(year, month, day, hour, minute, second)
                            sniper.set_opening_time(opening_time)
                        except ValueError as e:
                            print(f"时间设置错误: {str(e)}")
                            continue
                    
                    print("\n开始执行抢购...")
                    result = sniper.snipe()  # 这里会自动执行预热和准备流程
                    
                    if result:
                        print(f"""
=== 抢购成功! ===
订单ID: {result['id']}
成交价格: {result['average']}
成交数量: {result['filled']}
成交金额: {result['cost']} USDT
""")
                    else:
                        print("\n抢购失败或被中断")
                    
                    input("\n按回车继续...")
                    continue

                elif choice == "0":
                    logger.info("用户选择退出程序")
                    print("\n感谢使用，再见!")
                    break

                elif choice == "1":
                    logger.info("用户选择设置API密钥")
                    print("\n=== API密钥设置 ===")
                    print("请输入API密钥信息 (直接回车跳过则使用相同的密钥)")
                    print("\n提示: API密钥需要开启现货交易权限")
                    print("建议创建两组API密钥，分别用于交易和查询，以提高性能和安全性")
                    
                    # 获取交易API
                    print("\n>>> 交易API设置 (用于下单)")
                    trade_key = get_valid_input("交易API Key: ", str)
                    if not trade_key:
                        print("交易API Key不能为空")
                        input("\n按回车继续...")
                        continue
                        
                    trade_secret = get_valid_input("交易API Secret: ", str)
                    if not trade_secret:
                        print("交易API Secret不能为空")
                        input("\n按回车继续...")
                        continue
                    
                    # 获取查询API（可选）
                    print("\n>>> 查询API设置")
                    print("说明: 在币安API管理中创建API密钥时:")
                    print("1. 交易API需要开启: [读取] + [现货交易] 权限")
                    print("2. 查询API只需要开启: [读取] 权限")
                    print("\n建议: 使用单独的查询API可以:")
                    print("1. 提高查询性能")
                    print("2. 降低触发API限制的风险")
                    print("3. 提高安全性（查询API无需开启交易权限）")
                    use_separate = input("\n是否使用单独的查询API? (yes/no): ").lower() == 'yes'

                    if use_separate:
                        print("\n>>> 请在币安创建一个只有[读取]权限的新API")
                        print("创建步骤:")
                        print("1. 访问币安API管理页面")
                        print("2. 点击[创建API]")
                        print("3. 只勾选[读取]权限")
                        print("4. 完成验证后获取API密钥")
                        print("\n请输入新创建的只读API信息:")
                        query_key = get_valid_input("查询API Key: ", str)
                        query_secret = get_valid_input("查询API Secret: ", str)
                        if not (query_key and query_secret):
                            print("\n查询API信息不完整，将使用交易API进行查询")
                            print("(这样也可以正常使用，只是安全性和性能可能略低)")
                            query_key = None
                            query_secret = None
                        else:
                            print("\n已设置独立的查询API")
                    else:
                        print("\n将使用交易API进行查询操作")
                        print("(这样也可以正常使用，只是安全性和性能可能略低)")
                        query_key = None
                        query_secret = None
                    
                    try:
                        config.set_api_keys(
                            trade_key=trade_key,
                            trade_secret=trade_secret,
                            query_key=query_key,
                            query_secret=query_secret
                        )
                        print("\n=== API密钥设置成功! ===")
                        print("交易API: 已更新")
                        if query_key and query_secret:
                            print("查询API: 已设置独立查询API")
                        else:
                            print("查询API: 使用交易API")
                        
                        # 测试API连接
                        print("\n正在测试API连接...")
                        sniper = BinanceSniper(config)
                        test_result, status = sniper.test_api_latency()
                        
                        if test_result:
                            print(f"API连接测试成功! 延迟: {test_result:.2f}ms [{status}]")
                            if test_result > 500:
                                print("警告: API延迟较高，可能影响抢购性能")
                        else:
                            print("警告: API连接测试失败，请检查密钥是否正确")
                        
                        logger.info("抢币工具已使用新的API密钥重新初始化")
                        
                    except Exception as e:
                        logger.error(f"设置API密钥失败: {str(e)}")
                        print(f"\n设置失败: {str(e)}")
                        print("请检查:")
                        print("1. API密钥格式是否正确")
                        print("2. API是否有正确的权限")
                        print("3. 网络连接是否正常")
                    
                    input("\n按回车继续...")

                elif choice == "3":
                    logger.info("用户选择查看当前策略")
                    print_strategy(sniper)
                    input("\n按回车继续...")

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
        return
    except Exception as e:
        logger.error("主循环发生错误: %s", str(e))
        print(f"\n发生错误: {str(e)}")
        print("程序将继续运行...")
        return

    finally:
        logger.info("程序正常退出")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Binance Sniper')
    parser.add_argument('--daemon', action='store_true', help='以守护进程方式运行')
    args = parser.parse_args()
    
    if args.daemon:
        run_as_daemon()
    else:
        main()

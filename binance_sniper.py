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
from typing import Tuple, Optional, Dict, List, Any
import websocket  # 添加 websocket 导入
import requests
import pytz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import wraps
import argparse
import daemon
import signal
import lockfile

def setup_logger():
    """配置日志系统"""
    logger = logging.getLogger('BinanceSniper')
    logger.setLevel(logging.DEBUG)
    
    # 格式化器
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    # 文件处理器 (按大小轮转)
    log_file = 'logs/binance_sniper.log'
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    
    # 错误日志处理器
    error_handler = RotatingFileHandler(
        'logs/error.log',
        maxBytes=5*1024*1024,  # 5MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    
    # 添加处理器
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.addHandler(error_handler)
    
    return logger

# 创建logger实例
logger = setup_logger()

class PerformanceAnalyzer:
    """性能分析器"""
    
    def __init__(self):
        self.stats: Dict[str, List[float]] = defaultdict(list)
        self.current_operations: Dict[str, float] = {}
        self.start_times: Dict[str, float] = {}
        
    def start(self, operation: str):
        """开始计时"""
        self.start_times[operation] = time.perf_counter()
        
    def stop(self, operation: str) -> Optional[float]:
        """停止计时并返回耗时(ms)"""
        if operation not in self.start_times:
            return None
            
        elapsed = (time.perf_counter() - self.start_times[operation]) * 1000
        self.stats[operation].append(elapsed)
        del self.start_times[operation]
        return elapsed
        
    def get_stats(self, operation: str) -> Dict[str, float]:
        """获取操作的统计信息"""
        if operation not in self.stats or not self.stats[operation]:
            return {}
            
        values = self.stats[operation]
        return {
            'min': min(values),
            'max': max(values),
            'avg': sum(values) / len(values),
            'count': len(values),
            'total': sum(values)
        }
        
    def print_summary(self):
        """打印性能统计摘要"""
        print("\n====== 性能统计摘要 ======")
        categories = {
            'network': '网络操作',
            'api': 'API调用',
            'time_sync': '时间同步',
            'order': '订单操作',
            'preparation': '准备阶段',
            'execution': '执行阶段'
        }
        
        for category, desc in categories.items():
            stats = self.get_stats(category)
            if stats:
                print(f"\n{desc}:")
                print(f"- 最小耗时: {stats['min']:.2f}ms")
                print(f"- 最大耗时: {stats['max']:.2f}ms")
                print(f"- 平均耗时: {stats['avg']:.2f}ms")
                print(f"- 总计耗时: {stats['total']:.2f}ms")
                print(f"- 执行次数: {stats['count']}")

def timing_decorator(category: str):
    """性能计时装饰器"""
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            start_time = time.perf_counter()
            try:
                result = func(self, *args, **kwargs)
                elapsed = (time.perf_counter() - start_time) * 1000
                
                # 记录性能数据
                if hasattr(self, 'perf'):
                    self.perf.stats[category].append(elapsed)
                    self.perf.stats[f'{func.__name__}'].append(elapsed)
                
                # 记录详细日志
                logger.debug(f"{category} - {func.__name__}: {elapsed:.2f}ms")
                
                return result
            except Exception as e:
                elapsed = (time.perf_counter() - start_time) * 1000
                logger.error(f"{category} - {func.__name__} 失败: {elapsed:.2f}ms, 错误: {str(e)}")
                raise
        return wrapper
    return decorator

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
    """币安抢币工具核心类"""
    
    # 网络相关常量
    DEFAULT_TIMEOUT = (3.05, 27)  # (连接超时, 读取超时)
    DEFAULT_POOL_SIZE = 20
    MAX_POOL_SIZE = 50
    RETRY_ATTEMPTS = 3
    RETRY_BACKOFF = 0.5
    
    # API相关常量
    API_STATUS_CODES = [429, 500, 502, 503, 504]
    API_TEST_COUNT = 10
    API_TEST_INTERVAL = 0.1
    
    # 地理区域映射
    REGION_MAPPING = {
        'ASIA': ['CN', 'JP', 'KR', 'SG', 'IN', 'VN', 'TH'],
        'EU': ['GB', 'DE', 'FR', 'IT', 'ES', 'NL', 'CH'],
        'US': ['US', 'CA', 'MX']
    }
    
    # 延迟评估标准
    LATENCY_LEVELS = {
        'EXCELLENT': 50,   # 极佳: <50ms
        'GOOD': 100,      # 良好: 50-100ms
        'FAIR': 200,      # 一般: 100-200ms
        'POOR': 500       # 较差: >200ms
    }
    
    def __init__(self, config: 'ConfigManager'):
        """初始化币安抢币工具
        
        Args:
            config: 配置管理器实例
        """
        self.config = config
        self.performance_stats: Dict[str, List[float]] = defaultdict(list)
        self.is_test_mode: bool = False
        self._setup_clients()

    def _setup_clients(self):
        """设置交易和查询客户端"""
        # 获取两组API密钥
        trade_api_key, trade_api_secret = self.config.get_trade_api_keys()
        query_api_key, query_api_secret = self.config.get_query_api_keys()
        
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
        self.perf = PerformanceAnalyzer()
        
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
            return bool(orderbook and orderbook['asks'])  # 有卖单返回True，否则False
            
        except Exception as e:
            logger.error(f"检查交易状态失败: {str(e)}")
            return False

    def set_price_protection(self, max_price: float = None, price_multiplier: float = None):
        """设置价格保护参数"""
        try:
            if max_price is not None:
                self.max_price_limit = max_price
            
            if price_multiplier is not None:
                self.price_multiplier = price_multiplier
            
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
        """下市价单"""
        if self.is_test_mode:
            logger.info(f"""
=== 测试模式: 模拟下单 ===
交易对: {self.symbol}
类型: 市价单
数量: {self.amount}
""")
            return {
                'id': 'test_order_' + str(int(time.time())),
                'status': 'closed',
                'filled': self.amount,
                'average': self.get_market_price(),
                'cost': self.amount * self.get_market_price()
            }
            
        # 实际下单逻辑
        return super().place_market_order()

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

    def _continuous_market_data_fetch(self, start_time: float, duration_ms: int = 1000) -> List[Dict]:
        """持续获取市场数据
        Args:
            start_time: 开始时间戳(ms)
            duration_ms: 持续时间(ms)
        Returns:
            List[Dict]: 获取到的市场数据列表
        """
        market_data_list = []
        end_time = start_time + duration_ms
        
        def fetch_worker():
            while time.perf_counter() * 1000 < end_time:
                try:
                    depth = self.query_client.fetch_order_book(self.symbol, limit=5)
                    if depth and depth.get('asks'):
                        market_data = {
                            'first_ask': depth['asks'][0][0],
                            'timestamp': depth['timestamp'],
                            'local_time': time.perf_counter() * 1000
                        }
                        market_data_list.append(market_data)
                except Exception as e:
                    logger.error(f"获取市场数据失败: {str(e)}")
                
                # 尽可能快速地继续请求
                time.sleep(0.001)  # 1ms的最小间隔
        
        # 创建多个获取线程
        fetch_threads = []
        for _ in range(3):  # 使用3个线程并发获取
            thread = threading.Thread(target=fetch_worker)
            thread.daemon = True
            fetch_threads.append(thread)
            thread.start()
        
        # 等待所有线程完成
        for thread in fetch_threads:
            thread.join()
        
        return market_data_list

    def _execute_snipe_orders(self, market_data: Dict):
        """执行抢购订单
        Args:
            market_data: 市场数据
        """
        orders = []
        threads = []
        order_ids = set()
        
        def order_worker():
            try:
                order = self.place_market_order()
                if order and order.get('id'):
                    with threading.Lock():
                        orders.append(order)
                        order_ids.add(order['id'])
                    logger.info(f"订单已提交: {order['id']}")
            except Exception as e:
                logger.error(f"下单失败: {str(e)}")
        
        # 并发发送订单
        for _ in range(self.concurrent_orders):
            thread = threading.Thread(target=order_worker)
            thread.daemon = True
            threads.append(thread)
            thread.start()
        
        # 等待所有订单完成
        for thread in threads:
            thread.join(timeout=0.5)  # 设置较短的超时时间
        
        return orders, order_ids

    def snipe(self):
        """执行抢购"""
        try:
            # 1. 计算关键时间点
            target_time = self.opening_time.timestamp() * 1000
            network_latency = self.network_latency
            
            # 2. 计算市场数据获取时间
            # 提前 network_latency + 20ms(处理时间) 开始获取数据
            fetch_start_time = target_time - (network_latency + 20)
            
            # 3. 持续获取市场数据
            logger.info(f"开始获取市场数据, 提前量: {network_latency + 20:.2f}ms")
            market_data_list = self._continuous_market_data_fetch(
                fetch_start_time,
                duration_ms=network_latency + 100  # 持续时间比提前量多一点
            )
            
            if not market_data_list:
                logger.error("未能获取到市场数据")
                return None
            
            # 4. 选择最新的市场数据
            latest_data = max(market_data_list, key=lambda x: x['timestamp'])
            
            # 5. 检查价格
            if latest_data['first_ask'] > self.max_price:
                logger.warning(f"卖一价({latest_data['first_ask']})超过最高接受价格({self.max_price})")
                return None
            
            # 6. 执行订单
            logger.info("开始执行订单")
            orders, order_ids = self._execute_snipe_orders(latest_data)
            
            # 7. 处理订单结果
            success_order = self._handle_orders(order_ids)
            
            # 8. 记录性能数据
            data_fetch_times = [data['local_time'] - fetch_start_time for data in market_data_list]
            logger.info(f"""
=== 性能统计 ===
市场数据获取:
- 获取次数: {len(market_data_list)}
- 最小延迟: {min(data_fetch_times):.2f}ms
- 最大延迟: {max(data_fetch_times):.2f}ms
- 平均延迟: {sum(data_fetch_times)/len(data_fetch_times):.2f}ms

订单执行:
- 发送订单数: {len(order_ids)}
- 成功订单数: {1 if success_order else 0}
""")
            
            return success_order
            
        except Exception as e:
            logger.exception("抢购执行失败")
            return None

    def _place_order_and_collect(self, orders: list, order_ids: set):
        """下单并收集订单ID"""
        self.perf.start('single_order')
        try:
            order = self.place_market_order()
            if order and order.get('id'):
                orders.append(order)
                order_ids.add(order['id'])
                logger.info(f"订单已提交: {order['id']}")
        except Exception as e:
            logger.error(f"下单失败: {str(e)}")
        finally:
            self.perf.stop('single_order')

    def _handle_orders(self, order_ids: set) -> dict:
        """处理订单结果"""
        self.perf.start('order_handling')
        try:
            # 快速检查是否有已成交的订单
            success_order = None
            for order_id in order_ids:
                try:
                    self.perf.start(f'check_order_{order_id}')
                    order_status = self.query_client.fetch_order(order_id, self.symbol)
                    self.perf.stop(f'check_order_{order_id}')
                    
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
            self.perf.start('cancel_orders')
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
            self.perf.stop('cancel_orders')

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
        finally:
            self.perf.stop('order_handling')

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
            
            # 关闭API客户端
            for client in [self.trade_client, self.query_client]:
                if hasattr(client, 'close'):
                    client.close()
            
            # 保存性能统计
            if self.performance_stats:  # 只在有统计数据时保存
                self._save_performance_stats()
            
            logger.info("资源清理完成")
            
        except Exception as e:
            logger.error(f"清理资源失败: {str(e)}")

    def _save_performance_stats(self):
        """保存性能统计数据"""
        try:
            stats = {}
            for metric, values in self.performance_stats.items():
                if values:  # 只处理非空列表
                    stats[metric] = {
                        'min': min(values),
                        'max': max(values),
                        'avg': sum(values) / len(values),
                        'count': len(values)
                    }
            
            if stats:  # 只在有统计数据时保存
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

    def _warm_up_system(self) -> bool:
        """系统预热
        Returns:
            bool: 预热是否成功
        """
        self.perf.start('warm_up')
        try:
            logger.info("开始系统预热...")
            
            # 1. 检查账户余额
            if not self.check_balance():
                raise ValueError("账户余额不足")
                
            # 2. 预加载市场信息
            self.query_client.load_markets()
            
            # 3. 预热API连接
            self.query_client.fetch_ticker(self.symbol)
            self.trade_client.fetch_balance()
            
            # 4. 预创建WebSocket连接
            self._init_websocket_connection()
            
            warm_up_time = self.perf.stop('warm_up')
            logger.info(f"系统预热完成: {warm_up_time:.2f}ms")
            return True
            
        except Exception as e:
            logger.error(f"系统预热失败: {str(e)}")
            return False

    def _prepare_sessions(self) -> bool:
        """准备API会话
        Returns:
            bool: 会话准备是否成功
        """
        self.perf.start('prepare_sessions')
        try:
            # 1. 优化连接池设置
            self.trade_session.mount('https://', HTTPAdapter(
                pool_connections=50,
                pool_maxsize=50,
                max_retries=self.RETRY_ATTEMPTS,
                pool_block=False
            ))
            
            # 2. 设置会话参数
            self.trade_session.headers.update({
                'Content-Type': 'application/json',
                'User-Agent': 'BinanceSniper/1.0',
                'X-MBX-APIKEY': self.trade_client.apiKey
            })
            
            # 3. 预创建查询会话
            self.query_session.headers.update({
                'Content-Type': 'application/json',
                'User-Agent': 'BinanceSniper/1.0',
                'X-MBX-APIKEY': self.query_client.apiKey
            })
            
            prep_time = self.perf.stop('prepare_sessions')
            logger.info(f"会话准备完成: {prep_time:.2f}ms")
            return True
            
        except Exception as e:
            logger.error(f"会话准备失败: {str(e)}")
            return False

    def _prepare_order_params(self) -> dict:
        """准备订单参数
        Returns:
            dict: 预准备的订单参数
        """
        self.perf.start('prepare_params')
        try:
            # 1. 基础订单参数
            params = {
                'symbol': self.symbol.replace('/', ''),
                'side': 'BUY',
                'type': 'MARKET',
                'quantity': self.amount,
            }
            
            # 2. 准备签名参数
            timestamp = int(time.time() * 1000)
            params['timestamp'] = timestamp
            signature = self._generate_signature(params)
            params['signature'] = signature
            
            prep_time = self.perf.stop('prepare_params')
            logger.info(f"参数准备完成: {prep_time:.2f}ms")
            return params
            
        except Exception as e:
            logger.error(f"参数准备失败: {str(e)}")
            return None

    def _final_time_sync(self) -> Tuple[float, float]:
        """最终时间同步
        Returns:
            Tuple[float, float]: (网络延迟, 时间偏移)
        """
        self.perf.start('final_sync')
        try:
            latencies = []
            offsets = []
            
            # 进行多次同步取最优值
            for _ in range(5):
                start = time.perf_counter()
                server_time = self.query_client.fetch_time()
                end = time.perf_counter()
                
                latency = (end - start) * 1000 / 2  # 单向延迟
                offset = server_time - (start * 1000 + latency)
                
                latencies.append(latency)
                offsets.append(offset)
                
                time.sleep(0.1)  # 间隔100ms
                
            # 使用最优值
            best_latency = min(latencies)
            avg_offset = sum(offsets) / len(offsets)
            
            sync_time = self.perf.stop('final_sync')
            logger.info(f"""
最终时间同步完成: {sync_time:.2f}ms
- 网络延迟: {best_latency:.2f}ms
- 时间偏移: {avg_offset:.2f}ms
""")
            return best_latency, avg_offset
            
        except Exception as e:
            logger.error(f"最终时间同步失败: {str(e)}")
            return None

    def _calculate_send_time(self, sync_results: Tuple[float, float]) -> float:
        """计算订单发送时间
        Args:
            sync_results: (网络延迟, 时间偏移)
        Returns:
            float: 发送时间戳(ms)
        """
        if not sync_results:
            return None
            
        network_latency, time_offset = sync_results
        
        # 计算发送时间
        target_time = self.opening_time.timestamp() * 1000  # 目标服务器时间
        send_advance = (
            network_latency +  # 网络延迟补偿
            abs(time_offset) + # 时间偏移补偿
            5  # 安全余量(5ms)
        )
        
        send_time = target_time - send_advance
        
        logger.info(f"""
=== 订单发送时间计划 ===
目标服务器时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S.%f')}
计划发送时间: {datetime.fromtimestamp(send_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')}
提前量分析:
- 网络延迟补偿: {network_latency:.2f}ms
- 时间偏移补偿: {abs(time_offset):.2f}ms
- 安全余量: 5ms
总提前量: {send_advance:.2f}ms
""")
        
        return send_time

    def prepare_and_snipe(self):
        """准备并执行抢购"""
        try:
            current_time = datetime.now(self.timezone)
            time_to_open = (self.opening_time - current_time).total_seconds()
            
            # T-5分钟：系统初始化
            if time_to_open > 300:
                logger.info(f"等待进入5分钟准备阶段... 剩余{time_to_open-300:.1f}秒")
                time.sleep(time_to_open - 300)
            
            # 开始5分钟准备
            self.perf.start('preparation')
            
            # 1. 系统预热
            if not self._warm_up_system():
                return None
                
            # 2. 网络优化
            if not self.optimize_network_connection():
                logger.warning("网络优化失败，继续执行")
                
            # 3. 服务器选择
            server_results = self.test_server_latency()
            if not server_results:
                logger.warning("服务器选择失败，使用默认服务器")
                
            # T-2分钟：准备阶段
            while (time_to_open := self._get_time_to_open()) > 120:
                time.sleep(1)
                
            logger.info("进入2分钟准备阶段...")
            
            # 4. 预创建会话和参数
            if not self._prepare_sessions():
                return None
                
            # 5. 准备订单参数
            order_params = self._prepare_order_params()
            if not order_params:
                return None
                
            # T-30秒：最终准备
            while (time_to_open := self._get_time_to_open()) > 30:
                time.sleep(1)
                
            logger.info("进入30秒最终准备阶段...")
            
            # 6. 最后时间同步和参数准备
            sync_results = self._final_time_sync()
            if not sync_results:
                return None
                
            send_time = self._calculate_send_time(sync_results)
            if not send_time:
                return None
                
            # T-1秒：就绪状态
            while (time_to_open := self._get_time_to_open()) > 1:
                time.sleep(0.1)
                
            logger.info("进入发送准备状态...")
            
            # 7. 高精度等待
            current_time = time.perf_counter() * 1000
            while current_time < send_time:
                if send_time - current_time > 1:
                    time.sleep(0.0001)  # 100微秒的自旋等待
                current_time = time.perf_counter() * 1000
                
            # 8. 执行下单
            self.perf.start('order_execution')
            
            # 并发发送订单
            orders = []
            threads = []
            order_ids = set()
            
            execution_start = time.perf_counter()
            
            for _ in range(self.concurrent_orders):
                thread = threading.Thread(
                    target=lambda: self._place_order_and_collect(orders, order_ids, order_params)
                )
                threads.append(thread)
                thread.start()
                
            # 等待所有订单完成
            for thread in threads:
                thread.join(timeout=1.0)
                
            # 处理订单结果
            success_order = self._handle_orders(order_ids)
            
            execution_time = self.perf.stop('order_execution')
            total_time = time.perf_counter() - execution_start
            
            logger.info(f"""
=== 抢购执行完成 ===
总耗时: {total_time*1000:.2f}ms
订单执行: {execution_time:.2f}ms
发送订单数: {len(order_ids)}
成功订单数: {1 if success_order else 0}
订单延迟: {(time.perf_counter() - execution_start) * 1000:.2f}ms
""")
            
            return success_order
            
        except Exception as e:
            logger.exception("抢购过程发生错误")
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

    def optimize_network_connection(self):
        """优化网络连接配置"""
        try:
            # 1. 设置基础连接参数
            self.query_client.session.headers.update({
                'Connection': 'keep-alive',
                'Keep-Alive': '300',
                'User-Agent': 'BinanceSniper/1.0'
            })
            
            # 2. 配置重试策略
            retry_strategy = Retry(
                total=3,                     # 总重试次数
                backoff_factor=0.5,          # 重试等待时间的增长因子
                status_forcelist=[429, 500, 502, 503, 504],  # 需要重试的HTTP状态码
                allowed_methods=frozenset(['GET', 'POST']),   # 允许重试的HTTP方法
            )
            
            # 3. 配置连接池
            adapter = HTTPAdapter(
                pool_connections=20,    # 连接池基础连接数
                pool_maxsize=50,        # 最大连接数
                max_retries=retry_strategy,
                pool_block=False
            )
            
            # 4. 应用配置
            self.query_client.session.mount('https://', adapter)
            
            # 5. 设置超时
            self.query_client.session.timeout = (3.05, 27)  # (连接超时, 读取超时)
            
            logger.info("""
=== 网络连接优化完成 ===
- 连接池大小: 20-50
- 重试策略: 3次重试
- Keep-Alive: 300秒
- 超时设置: 连接3s, 读取27s
""")
            
            return True
            
        except Exception as e:
            logger.error(f"网络连接优化失败: {str(e)}")
            return False

    def test_server_latency(self) -> dict:
        """测试不同服务器延迟并自动选择最佳服务器"""
        try:
            # 1. 获取本地地理信息
            try:
                geo_info = requests.get('https://ipinfo.io/json', timeout=5).json()
                local_country = geo_info.get('country', 'Unknown')
                local_region = geo_info.get('region', 'Unknown')
                logger.info(f"当前位置: {local_country}-{local_region}")
            except Exception as e:
                logger.warning(f"获取地理位置失败: {str(e)}")
                local_country = 'Unknown'
            
            # 2. 定义服务器列表（按地区分组）
            endpoints = {
                'ASIA': {  # 亚洲服务器
                    'api1.ap': 'api1.binance.com',
                    'api2.ap': 'api2.binance.com',
                    'api3.ap': 'api3.binance.com'
                },
                'EU': {    # 欧洲服务器
                    'api1.eu': 'api1.binance.com',
                    'api2.eu': 'api2.binance.com'
                },
                'US': {    # 美国服务器
                    'api1.us': 'api1.binance.com',
                    'api2.us': 'api2.binance.com'
                }
            }
            
            # 3. 根据地理位置选择优先测试的服务器组
            if local_country in ['CN', 'JP', 'KR', 'SG', 'IN']:
                priority_endpoints = endpoints['ASIA']
            elif local_country in ['GB', 'DE', 'FR', 'IT', 'ES']:
                priority_endpoints = endpoints['EU']
            elif local_country in ['US', 'CA']:
                priority_endpoints = endpoints['US']
            else:
                priority_endpoints = {k: v for d in endpoints.values() for k, v in d.items()}
            
            # 4. 测试延迟
            results = {}
            test_count = 10  # 增加测试次数到10次
            
            for name, endpoint in priority_endpoints.items():
                latencies = []
                errors = 0
                
                logger.info(f"测试服务器 {name} ({endpoint})...")
                
                for i in range(test_count):
                    try:
                        # 使用 /api/v3/time 接口测试延迟
                        start = time.perf_counter()
                        response = requests.get(
                            f'https://{endpoint}/api/v3/time',
                            timeout=2,
                            headers={'User-Agent': 'BinanceSniper/1.0'}
                        )
                        
                        if response.status_code == 200:
                            latency = (time.perf_counter() - start) * 1000
                            latencies.append(latency)
                            logger.debug(f"测试 {i+1}/{test_count}: {latency:.2f}ms")
                        else:
                            errors += 1
                            logger.warning(f"测试 {i+1}/{test_count}: 状态码 {response.status_code}")
                            
                    except requests.exceptions.Timeout:
                        errors += 1
                        logger.warning(f"测试 {i+1}/{test_count}: 超时")
                    except requests.exceptions.RequestException as e:
                        errors += 1
                        logger.warning(f"测试 {i+1}/{test_count}: {str(e)}")
                    
                    time.sleep(0.1)  # 间隔100ms，避免请求过于频繁
                
                # 计算服务器得分
                if latencies:
                    # 去掉最高和最低延迟，计算平均值
                    latencies.sort()
                    valid_latencies = latencies[1:-1] if len(latencies) > 2 else latencies
                    avg_latency = sum(valid_latencies) / len(valid_latencies)
                    
                    # 计算得分（考虑延迟和错误率）
                    error_rate = errors / test_count
                    score = avg_latency * (1 + error_rate * 2)  # 错误率权重为2
                    
                    results[name] = {
                        'endpoint': endpoint,
                        'avg_latency': avg_latency,
                        'min_latency': min(latencies),
                        'max_latency': max(latencies),
                        'error_rate': error_rate,
                        'score': score
                    }
                    
                    logger.info(f"""
服务器 {name} 测试结果:
- 平均延迟: {avg_latency:.2f}ms
- 最低延迟: {min(latencies):.2f}ms
- 最高延迟: {max(latencies):.2f}ms
- 错误率: {error_rate*100:.1f}%
- 综合得分: {score:.2f}
""")
            
            # 5. 选择最佳服务器
            if not results:
                raise Exception("没有可用的服务器")
            
            best_server = min(results.items(), key=lambda x: x[1]['score'])
            best_name, best_stats = best_server
            
            logger.info(f"""
=== 最佳服务器选择 ===
服务器: {best_name}
地址: {best_stats['endpoint']}
平均延迟: {best_stats['avg_latency']:.2f}ms
错误率: {best_stats['error_rate']*100:.1f}%
综合得分: {best_stats['score']:.2f}

延迟评估:
- 极佳: <50ms
- 良好: 50-100ms
- 一般: 100-200ms
- 较差: >200ms
""")
            
            # 6. 自动应用最佳服务器设置
            try:
                self.query_client.urls['api'] = f"https://{best_stats['endpoint']}"
                logger.info(f"已切换到最佳服务器: {best_stats['endpoint']}")
            except Exception as e:
                logger.error(f"切换服务器失败: {str(e)}")
            
            return results
            
        except Exception as e:
            logger.error(f"服务器延迟测试失败: {str(e)}")
            return {}

    def check_system_health(self) -> Tuple[bool, Dict[str, bool]]:
        """系统健康检查
        
        Returns:
            Tuple[bool, Dict[str, bool]]: (总体健康状态, 各项检查结果)
        """
        checks = {
            'network': self._check_network(),
            'api_access': self._check_api_access(),
            'time_sync': self._check_time_sync(),
            'balance': self.check_balance()
        }
        
        all_passed = all(checks.values())
        
        if all_passed:
            logger.info("系统健康检查通过 ✅")
        else:
            failed_checks = [k for k, v in checks.items() if not v]
            logger.warning(f"系统健康检查失败 ❌ 失败项: {', '.join(failed_checks)}")
        
        return all_passed, checks
    
    def _check_network(self) -> bool:
        """检查网络连接"""
        try:
            response = requests.get('https://api.binance.com/api/v3/ping', 
                                  timeout=self.DEFAULT_TIMEOUT[0])
            return response.status_code == 200
        except Exception as e:
            logger.error(f"网络检查失败: {str(e)}")
            return False
    
    def _check_api_access(self) -> bool:
        """检查API访问权限"""
        try:
            self.query_client.fetch_time()
            return True
        except Exception as e:
            logger.error(f"API访问检查失败: {str(e)}")
            return False
    
    def _check_time_sync(self) -> bool:
        """检查时间同步"""
        try:
            server_time = self.get_server_time()
            local_time = datetime.now(self.timezone)
            diff = abs((server_time - local_time).total_seconds())
            return diff < 1  # 允许1秒的误差
        except Exception as e:
            logger.error(f"时间同步检查失败: {str(e)}")
            return False

    def enable_test_mode(self):
        """启用测试模式"""
        self.is_test_mode = True
        logger.info("""
=== 测试模式已启用 ===
- 订单不会真实成交
- API调用将模拟执行
- 所有操作将记录到日志
""")
    
    def place_market_order(self):
        """下市价单"""
        if self.is_test_mode:
            logger.info(f"""
=== 测试模式: 模拟下单 ===
交易对: {self.symbol}
类型: 市价单
数量: {self.amount}
""")
            return {
                'id': 'test_order_' + str(int(time.time())),
                'status': 'closed',
                'filled': self.amount,
                'average': self.get_market_price(),
                'cost': self.amount * self.get_market_price()
            }
            
        # 实际下单逻辑
        return super().place_market_order()

    def _prepare_market_data_stream(self):
        """准备市场数据流"""
        try:
            # 1. 准备WebSocket连接
            self.ws = websocket.WebSocketApp(
                f"wss://stream.binance.com:9443/ws/{self.symbol.lower().replace('/', '')}@depth5@100ms",
                on_message=self._on_depth_message,
                on_error=self._on_ws_error,
                on_close=self._on_ws_close
            )
            
            # 2. 启动WebSocket线程
            self.ws_thread = threading.Thread(target=self.ws.run_forever)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
            logger.info("市场数据流准备就绪")
            return True
            
        except Exception as e:
            logger.error(f"准备市场数据流失败: {str(e)}")
            return False

    def _calculate_fetch_time(self, sync_results: Tuple[float, float]) -> float:
        """计算市场数据获取时间
        Args:
            sync_results: (网络延迟, 时间偏移)
        Returns:
            float: 数据获取时间戳(ms)
        """
        network_latency, time_offset = sync_results
        
        # 开盘时间
        target_time = self.opening_time.timestamp() * 1000
        
        # 计算提前量
        # 1. 网络延迟补偿
        # 2. API处理时间(预估20ms)
        # 3. 数据处理时间(预估5ms)
        fetch_advance = (
            network_latency +  # 网络延迟
            20 +              # API处理时间
            5                 # 数据处理时间
        )
        
        # 数据获取时间 = 开盘时间 - 提前量
        fetch_time = target_time - fetch_advance
        
        logger.info(f"""
=== 市场数据获取计划 ===
目标开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S.%f')}
计划获取时间: {datetime.fromtimestamp(fetch_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')}
提前量分析:
- 网络延迟补偿: {network_latency:.2f}ms
- API处理时间: 20ms
- 数据处理时间: 5ms
总提前量: {fetch_advance:.2f}ms
""")
        
        return fetch_time

    def _fetch_market_data(self) -> Dict:
        """获取市场数据"""
        try:
            # 1. 获取深度数据
            depth = self.query_client.fetch_order_book(self.symbol, limit=5)
            
            # 2. 提取关键数据
            first_ask = depth['asks'][0] if depth['asks'] else None
            first_bid = depth['bids'][0] if depth['bids'] else None
            
            if not first_ask or not first_bid:
                raise ValueError("无法获取市场深度数据")
                
            market_data = {
                'first_ask': first_ask[0],  # 卖一价
                'first_bid': first_bid[0],  # 买一价
                'spread': first_ask[0] - first_bid[0],  # 价差
                'timestamp': depth['timestamp']
            }
            
            logger.debug(f"市场数据: {market_data}")
            return market_data
            
        except Exception as e:
            logger.error(f"获取市场数据失败: {str(e)}")
            return None

    def prepare_and_snipe(self):
        """准备并执行抢购"""
        try:
            # ... 前面的代码保持不变 ...
            
            # T-1秒：最终准备
            while (time_to_open := self._get_time_to_open()) > 1:
                time.sleep(0.1)
                
            logger.info("进入最终准备阶段...")
            
            # 计算市场数据获取时间
            fetch_time = self._calculate_fetch_time(sync_results)
            
            # 等待获取市场数据的时间点
            current_time = time.perf_counter() * 1000
            while current_time < fetch_time:
                if fetch_time - current_time > 1:
                    time.sleep(0.0001)
                current_time = time.perf_counter() * 1000
                
            # 获取市场数据
            market_data = self._fetch_market_data()
            if not market_data:
                logger.error("无法获取市场数据，终止抢购")
                return None
                
            # 根据市场数据调整订单参数
            first_ask = market_data['first_ask']
            if first_ask > self.max_price:
                logger.warning(f"卖一价({first_ask})超过最高接受价格({self.max_price})")
                return None
                
            # 更新订单参数
            order_params['price'] = first_ask
            
            # 继续等待发送订单的时间点
            while current_time < send_time:
                if send_time - current_time > 1:
                    time.sleep(0.0001)
                current_time = time.perf_counter() * 1000
                
            # 执行下单
            # ... 后面的代码保持不变 ...

def print_menu():
    """打印主菜单"""
    print("\n=== 币安现货抢币工具 ===")
    print("1. 设置API密钥")
    print("2. 抢开盘策略设置")
    print("3. 查看当前策略")
    print("4. 开始抢购")
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
    """主程序入口"""
    try:
        config = ConfigManager()
        
        # 验证配置
        is_valid, errors = config.validate_config()
        if not is_valid:
            logger.error("配置验证失败:")
            for error in errors:
                logger.error(f"- {error}")
            return
        
        with BinanceSniper(config) as sniper:
            # 系统健康检查
            is_healthy, health_checks = sniper.check_system_health()
            if not is_healthy:
                logger.warning("系统健康检查未通过，请确认是否继续")
                if not get_valid_input("是否继续? (y/n): ", str).lower().startswith('y'):
                    return
            
            # 命令映射
            commands = {
                "1": ("设置API密钥", sniper.setup_api_keys),
                "2": ("抢开盘策略设置", sniper.setup_snipe_strategy),
                "3": ("查看当前策略", sniper.print_strategy),
                "4": ("开始抢购", sniper.prepare_and_snipe),
                "0": ("退出程序", lambda: "exit")
            }
            
            while True:
                try:
                    print_menu()
                    choice = input("请选择操作 (0-4): ").strip()
                    
                    if choice not in commands:
                        print("选择无效，请重试")
                        continue
                    
                    name, action = commands[choice]
                    logger.info(f"用户选择: {name}")
                    
                    result = action()
                    if result == "exit":
                        break
                    
                    input("\n按回车继续...")
                    
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    logger.exception(f"操作执行失败: {str(e)}")
                    print(f"\n操作失败: {str(e)}")
                    print("请检查日志获取详细信息")
                    input("\n按回车继续...")
                    
    except KeyboardInterrupt:
        print("\n程序已中断")
    except Exception as e:
        logger.exception("程序运行错误")
        print(f"\n程序错误: {str(e)}")
    finally:
        logger.info("程序退出")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Binance Sniper')
    parser.add_argument('--daemon', action='store_true', help='以守护进程方式运行')
    args = parser.parse_args()
    
    if args.daemon:
        run_as_daemon()
    else:
        main()

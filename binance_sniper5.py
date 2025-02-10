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
from typing import (
    Tuple, Optional, Dict, List, Any, Set, 
    Callable, Union, TypeVar, Type
)
from types import TracebackType
from decimal import Decimal
import websocket
import requests
import pytz
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import wraps
import argparse
import daemon
import signal
import lockfile
from abc import ABC, abstractmethod
import asyncio
import aiohttp
import numpy as np
import numba
from concurrent.futures import ThreadPoolExecutor
from cachetools import TTLCache
import mmap
import io
import prometheus_client as prom
import logging.handlers
import websockets
import psutil
import socket
import gc
import struct
import statistics
import glob

# 统一异常处理
class SniperError(Exception):
    """基础异常类"""
    pass

class NetworkError(SniperError):
    """网络相关异常"""
    pass

class ExecutionError(SniperError):
    """执行相关异常"""
    pass

class MarketError(SniperError):
    """市场相关异常"""
    pass

class ConfigError(SniperError):
    """配置相关异常"""
    pass

class TimeError(SniperError):
    """时间同步相关异常"""
    pass

class PreciseWaiter:
    """精确等待器，用于高精度时间控制"""
    
    def __init__(self):
        """初始化等待器"""
        self.last_sleep_error = 0  # 记录上次休眠误差
        self._min_sleep = 0.000001  # 最小休眠时间(1微秒)
        
    async def wait_until(self, target_time: float):
        """等待到指定时间
        
        Args:
            target_time: 目标时间戳(毫秒)
        """
        while True:
            current = time.time() * 1000
            if current >= target_time:
                break
                
            remaining = target_time - current
            if remaining > 1:
                # 长等待使用asyncio.sleep
                await asyncio.sleep(remaining / 2000)  # 转换为秒并减半
            else:
                # 短等待使用自旋等待
                while time.time() * 1000 < target_time:
                    await asyncio.sleep(0)  # 让出CPU时间片
    
    async def sleep(self, duration: float):
        """精确休眠指定时间
        
        Args:
            duration: 休眠时间(毫秒)
        """
        if duration <= 0:
            return
            
        target = time.time() * 1000 + duration
        await self.wait_until(target)
    
    def busy_wait(self, target_time: float):
        """自旋等待（非异步）
        
        Args:
            target_time: 目标时间戳(毫秒)
        """
        while time.time() * 1000 < target_time:
            pass

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

# 执行策略管理器
class ExecutionStrategyManager:
    def __init__(self):
        self.strategies = {
            'aggressive': {
                'concurrent_orders': 5,
                'time_advance': 50,  # ms
                'retry_attempts': 3
            },
            'balanced': {
                'concurrent_orders': 3,
                'time_advance': 100,  # ms
                'retry_attempts': 2
            },
            'conservative': {
                'concurrent_orders': 1,
                'time_advance': 200,  # ms
                'retry_attempts': 1
            }
        }
        
    def get_strategy(self, risk_level: str) -> Dict:
        """获取执行策略"""
        return self.strategies.get(risk_level, self.strategies['balanced'])
        
    def optimize_strategy(self, 
                         network_latency: float,
                         success_rate: float) -> Dict:
        """根据网络条件优化策略"""
        if network_latency < 50 and success_rate > 0.8:
            return self.strategies['aggressive']
        elif network_latency > 200 or success_rate < 0.5:
            return self.strategies['conservative']
        else:
            return self.strategies['balanced']

# 在 SniperError 类定义之后,ExecutionController 类之前添加

class TimeSync:
    """时间同步管理器"""
    def __init__(self):
        self.network_latency = None  # 网络延迟
        self.time_offset = None      # 时间偏移
        self.last_sync_time = 0      # 上次同步时间
        self.sync_interval = 60      # 同步间隔(秒)
        self.min_samples = 5         # 最小样本数
        self.max_samples = 10        # 最大样本数
        self.sync_timeout = 5        # 同步超时时间(秒)
        
    async def sync(self, client) -> bool:
        """同步时间
        Args:
            client: API客户端
        Returns:
            bool: 同步是否成功
        """
        try:
            measurements = []
            
            # 收集多个样本
            for _ in range(self.max_samples):
                start = time.perf_counter_ns()
                server_time = await client.fetch_time()
                end = time.perf_counter_ns()
                
                rtt = (end - start) / 1e6  # 转换为毫秒
                latency = rtt / 2
                offset = server_time - (start/1e6 + latency)
                
                measurements.append({
                    'latency': latency,
                    'offset': offset,
                    'rtt': rtt
                })
                
                await asyncio.sleep(0.1)  # 间隔100ms
            
            # 过滤异常值
            filtered = self._filter_outliers(measurements)
            if len(filtered) < self.min_samples:
                logger.error("有效样本数不足")
                return False
            
            # 使用中位数作为最终值
            self.network_latency = statistics.median(m['latency'] for m in filtered)
            self.time_offset = statistics.median(m['offset'] for m in filtered)
            self.last_sync_time = time.time()
            
            logger.info(f"""
=== 时间同步完成 ===
网络延迟: {self.network_latency:.2f}ms
时间偏移: {self.time_offset:.2f}ms
样本数量: {len(filtered)}
""")
            return True
            
        except Exception as e:
            logger.error(f"时间同步失败: {str(e)}")
            return False
    
    def _filter_outliers(self, measurements: List[Dict]) -> List[Dict]:
        """过滤异常值"""
        if len(measurements) < 4:
            return measurements
            
        # 计算RTT的四分位数
        rtts = sorted(m['rtt'] for m in measurements)
        q1 = rtts[len(rtts)//4]
        q3 = rtts[3*len(rtts)//4]
        iqr = q3 - q1
        
        # 过滤掉RTT异常的样本
        return [m for m in measurements if q1 - 1.5*iqr <= m['rtt'] <= q3 + 1.5*iqr]
    
    def needs_sync(self) -> bool:
        """检查是否需要同步"""
        return (
            self.network_latency is None or
            self.time_offset is None or
            time.time() - self.last_sync_time > self.sync_interval
        )
    
    def get_network_latency(self) -> Optional[float]:
        """获取网络延迟"""
        return self.network_latency
    
    def get_time_offset(self) -> Optional[float]:
        """获取时间偏移"""
        return self.time_offset

class MarketDepthAnalyzer:
    """市场深度分析器"""
    
    def __init__(self, client):
        self.client = client
        self.logger = logging.getLogger(self.__class__.__name__)
        
    async def analyze_depth(self, client, symbol: str, limit: int = 20) -> Optional[Dict]:
        """分析市场深度
        
        Args:
            client: API客户端
            symbol: 交易对
            limit: 深度档数
            
        Returns:
            Optional[Dict]: 深度分析结果
        """
        try:
            # 获取订单簿数据
            orderbook = await client.fetch_order_book(symbol, limit)
            if not orderbook or not orderbook['asks']:
                return None
                
            asks = orderbook['asks']
            bids = orderbook['bids']
            
            # 基本价格信息
            best_ask = asks[0][0]
            best_bid = bids[0][0] if bids else 0
            spread = best_ask - best_bid
            
            # 深度统计
            ask_volume = sum(ask[1] for ask in asks[:5])  # 前5档卖单量
            bid_volume = sum(bid[1] for bid in bids[:5])  # 前5档买单量
            
            # 检查深度异常
            price_gaps = [asks[i+1][0] - asks[i][0] for i in range(min(4, len(asks)-1))]
            max_gap = max(price_gaps) if price_gaps else 0
            avg_gap = sum(price_gaps) / len(price_gaps) if price_gaps else 0
            
            return {
                'ask': best_ask,
                'bid': best_bid,
                'spread': spread,
                'spread_ratio': spread / best_bid if best_bid else 0,
                'ask_volume': ask_volume,
                'bid_volume': bid_volume,
                'max_gap': max_gap,
                'avg_gap': avg_gap,
                'timestamp': orderbook['timestamp'],
                'asks': asks[:5],  # 前5档卖单
                'bids': bids[:5]   # 前5档买单
            }
            
        except Exception as e:
            self.logger.error(f"分析市场深度失败: {str(e)}")
            return None
    
    def is_depth_normal(self, depth_data: Dict) -> bool:
        """检查深度是否正常
        
        Args:
            depth_data: 深度数据
            
        Returns:
            bool: 深度是否正常
        """
        if not depth_data:
            return False
            
        try:
            # 1. 检查买卖价差
            if depth_data['spread_ratio'] > 0.05:  # 价差超过5%
                self.logger.warning(f"价差过大: {depth_data['spread_ratio']*100:.2f}%")
                return False
                
            # 2. 检查深度断层
            if depth_data['max_gap'] > depth_data['avg_gap'] * 3:
                self.logger.warning("检测到深度断层")
                return False
                
            # 3. 检查深度充足性
            min_volume = 10  # 最小深度要求
            if depth_data['ask_volume'] < min_volume:
                self.logger.warning(f"卖单深度不足: {depth_data['ask_volume']:.2f}")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"深度检查失败: {str(e)}")
            return False

class OrderExecutor:
    """订单执行器"""
    
    def __init__(self):
        self.logger = logging.getLogger(self.__class__.__name__)
        self._order_buffer = bytearray(1024 * 10)  # 10KB缓冲区
        
    async def execute_orders(self, 
                           client,
                           symbol: str,
                           amount: float,
                           price: float,
                           concurrent_orders: int = 3) -> Optional[Dict]:
        """执行订单
        
        Args:
            client: API客户端
            symbol: 交易对
            amount: 数量
            price: 价格
            concurrent_orders: 并发订单数
            
        Returns:
            Optional[Dict]: 成功的订单信息
        """
        try:
            # 1. 准备订单参数
            base_params = {
                'symbol': symbol.replace('/', ''),
                'side': 'BUY',
                'type': 'LIMIT',
                'timeInForce': 'IOC',
                'quantity': amount,
                'price': price
            }
            
            # 2. 创建多个订单任务
            order_tasks = []
            for _ in range(concurrent_orders):
                params = base_params.copy()
                params['timestamp'] = int(time.time() * 1000)
                task = self._execute_single_order(client, params)
                order_tasks.append(task)
            
            # 3. 并发执行订单
            orders = await asyncio.gather(*order_tasks, return_exceptions=True)
            
            # 4. 处理结果
            success_order = None
            for order in orders:
                if isinstance(order, dict) and order.get('status') == 'FILLED':
                    success_order = order
                    break
            
            return success_order
            
        except Exception as e:
            self.logger.error(f"订单执行失败: {str(e)}")
            return None
            
    async def _execute_single_order(self, client, params: Dict) -> Optional[Dict]:
        """执行单个订单"""
        try:
            return await client.create_order(**params)
        except Exception as e:
            self.logger.error(f"执行订单失败: {str(e)}")
            return None

# 执行流程控制器
class ExecutionController:
    def __init__(self, 
                 time_sync: TimeSync,
                 order_executor: 'OrderExecutor',
                 depth_analyzer: 'MarketDepthAnalyzer',
                 monitor: 'RealTimeMonitor'):
        self.time_sync = time_sync
        self.order_executor = order_executor
        self.depth_analyzer = depth_analyzer
        self.monitor = monitor
        self.waiter = PreciseWaiter()
        
    async def execute_snipe(self,
                          client,
                          symbol: str,
                          amount: float,
                          target_time: float,
                          price: Optional[float] = None,
                          concurrent_orders: int = 3) -> Optional[Dict]:
        """执行抢购
        Args:
            client: API客户端
            symbol: 交易对
            amount: 数量
            target_time: 目标时间戳(毫秒)
            price: 限价单价格(可选)
            concurrent_orders: 并发订单数
        Returns:
            Optional[Dict]: 成功的订单信息
        """
        try:
            # 1. 确保时间同步
            if self.time_sync.needs_sync():
                await self.time_sync.sync(client)
                
            # 2. 计算关键时间点
            network_latency = self.time_sync.network_latency
            fetch_time = target_time - (network_latency + 25)  # 提前25ms获取数据
            send_time = target_time - (network_latency + 5)   # 提前5ms发送订单
            
            logger.info(f"""
=== 执行计划 ===
目标时间: {datetime.fromtimestamp(target_time/1000).strftime('%H:%M:%S.%f')}
数据获取: {datetime.fromtimestamp(fetch_time/1000).strftime('%H:%M:%S.%f')}
订单发送: {datetime.fromtimestamp(send_time/1000).strftime('%H:%M:%S.%f')}
网络延迟: {network_latency:.2f}ms
""")
            
            # 3. 等待并获取市场数据
            await self.waiter.wait_until(fetch_time)
            market_data = await self.depth_analyzer.analyze_depth(client, symbol)
            if not market_data:
                logger.error("无法获取市场数据")
                return None
                
            # 4. 检查价格
            if price and not self._is_price_safe(market_data['ask'], price):
                logger.warning("价格超出安全范围")
                return None
                
            # 5. 等待并执行订单
            await self.waiter.wait_until(send_time)
            success_order = await self.order_executor.execute_orders(
                client=client,
                symbol=symbol,
                amount=amount,
                price=price or market_data['ask'],  # 如果没有指定价格,使用市场价
                concurrent_orders=concurrent_orders
            )
            
            # 6. 记录结果
            if success_order:
                self.monitor.record_success(True)
                logger.info(f"""
=== 抢购成功 ===
订单ID: {success_order['id']}
成交价格: {success_order['average']}
成交数量: {success_order['filled']}
成交金额: {success_order['cost']} USDT
""")
            else:
                self.monitor.record_success(False)
                logger.warning("抢购未成功")
                
            return success_order
            
        except Exception as e:
            logger.error(f"执行抢购失败: {str(e)}")
            self.monitor.record_error()
            return None

    def _is_price_safe(self, current_price: float, target_price: float) -> bool:
        """检查价格是否在安全范围内"""
        deviation = abs(current_price - target_price) / target_price
        if deviation > self.max_price_deviation:
            logger.warning(f"价格偏差过大: 目标 {target_price}, 当前 {current_price}, 偏差 {deviation*100:.2f}%")
            return False
        return True

# 网络优化器
class NetworkOptimizer:
    def __init__(self):
        self.connection_pool = aiohttp.TCPConnector(
            limit=100,
            ttl_dns_cache=300,
            use_dns_cache=True,
            force_close=False
        )
        self.session = None
        self.endpoints = []
        self.best_endpoint = None
        self.latency_stats = defaultdict(list)
        
    async def initialize(self):
        """初始化网络连接"""
        self.session = aiohttp.ClientSession(
            connector=self.connection_pool,
            timeout=aiohttp.ClientTimeout(total=5)
        )
        
    async def optimize_connection(self, url: str):
        """优化到特定URL的连接"""
        try:
            # 预连接
            async with self.session.get(url) as response:
                await response.read()
                
            # 测试延迟
            start = time.perf_counter()
            async with self.session.get(url) as response:
                await response.read()
            latency = (time.perf_counter() - start) * 1000
            
            return latency
            
        except Exception as e:
            logger.error(f"连接优化失败: {str(e)}")
            return None
            
    async def test_endpoints(self, endpoints: List[str]) -> Dict[str, float]:
        """测试多个端点的延迟"""
        results = {}
        for endpoint in endpoints:
            latency = await self.optimize_connection(endpoint)
            if latency:
                results[endpoint] = latency
                self.latency_stats[endpoint].append(latency)
        
        # 更新最佳端点
        if results:
            self.best_endpoint = min(results.items(), key=lambda x: x[1])[0]
            
        return results
        
    def get_best_endpoint(self) -> Optional[str]:
        """获取最佳端点"""
        return self.best_endpoint
        
    def get_latency_stats(self, endpoint: str) -> Dict:
        """获取端点延迟统计"""
        if not self.latency_stats[endpoint]:
            return {}
            
        latencies = self.latency_stats[endpoint]
        return {
            'min': min(latencies),
            'max': max(latencies),
            'avg': sum(latencies) / len(latencies),
            'count': len(latencies)
        }
        
    async def cleanup(self):
        """清理资源"""
        if self.session:
            await self.session.close()
        self.latency_stats.clear()

# 实时监控系统
class RealTimeMonitor:
    def __init__(self):
        self.metrics = {
            'order_latency': [],
            'market_data_latency': [],
            'network_latency': [],
            'success_rate': 0,
            'error_count': 0
        }
        
        # Prometheus指标
        self.prom_metrics = {
            'order_latency': prom.Histogram(
                'order_latency_seconds',
                'Order execution latency',
                buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25)
            ),
            'market_data_latency': prom.Histogram(
                'market_data_latency_seconds',
                'Market data fetch latency',
                buckets=(0.001, 0.005, 0.01, 0.025, 0.05)
            ),
            'success_rate': prom.Gauge(
                'order_success_rate',
                'Order success rate'
            ),
            'error_count': prom.Counter(
                'error_total',
                'Total number of errors'
            ),
            'memory_usage': prom.Gauge(
                'memory_usage_bytes',
                'Memory usage in bytes'
            ),
            'cpu_usage': prom.Gauge(
                'cpu_usage_percent',
                'CPU usage percentage'
            )
        }
        
    def record_latency(self, category: str, latency: float):
        """记录延迟"""
        self.metrics[f'{category}_latency'].append(latency)
        self.prom_metrics[f'{category}_latency'].observe(latency)
        
    def record_success(self, success: bool):
        """记录成功率"""
        if success:
            self.metrics['success_rate'] = (
                self.metrics['success_rate'] * 0.9 + 0.1
            )  # 指数移动平均
        else:
            self.metrics['success_rate'] = self.metrics['success_rate'] * 0.9
            
        self.prom_metrics['success_rate'].set(self.metrics['success_rate'])
        
    def record_error(self):
        """记录错误"""
        self.metrics['error_count'] += 1
        self.prom_metrics['error_count'].inc()
        
    def update_system_metrics(self):
        """更新系统指标"""
        # 内存使用
        memory = psutil.Process().memory_info()
        self.prom_metrics['memory_usage'].set(memory.rss)
        
        # CPU使用
        cpu_percent = psutil.Process().cpu_percent()
        self.prom_metrics['cpu_usage'].set(cpu_percent)
        
    def get_statistics(self) -> Dict:
        """获取统计数据"""
        stats = {}
        for category in ['order', 'market_data', 'network']:
            latencies = self.metrics[f'{category}_latency']
            if latencies:
                stats[category] = {
                    'min': min(latencies),
                    'max': max(latencies),
                    'avg': sum(latencies) / len(latencies),
                    'p95': sorted(latencies)[int(len(latencies) * 0.95)],
                    'count': len(latencies)
                }
                
        stats['success_rate'] = self.metrics['success_rate']
        stats['error_count'] = self.metrics['error_count']
        
        return stats
        
    def print_report(self):
        """打印监控报告"""
        stats = self.get_statistics()
        print("\n====== 性能监控报告 ======")
        
        for category in ['order', 'market_data', 'network']:
            if category in stats:
                print(f"\n{category.upper()} 延迟统计:")
                print(f"最小延迟: {stats[category]['min']:.2f}ms")
                print(f"最大延迟: {stats[category]['max']:.2f}ms")
                print(f"平均延迟: {stats[category]['avg']:.2f}ms")
                print(f"95分位数: {stats[category]['p95']:.2f}ms")
                print(f"样本数量: {stats[category]['count']}")
                
        print(f"\n成功率: {stats['success_rate']*100:.2f}%")
        print(f"错误数: {stats['error_count']}")
        print("==========================")

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
    """配置管理器"""
    def __init__(self):
        # 设置配置目录和文件路径
        self.config_dir = 'config'
        self.config_file = os.path.join(self.config_dir, 'api_config.ini')
        
        # 确保配置目录存在
        if not os.path.exists(self.config_dir):
            os.makedirs(self.config_dir)
        
        # 初始化配置解析器
        self.config = configparser.ConfigParser()
        
        # 加载现有配置或创建新配置
        if os.path.exists(self.config_file):
            self.config.read(self.config_file)
        else:
            self._create_default_config()
    
    def _create_default_config(self):
        """创建默认配置"""
        self.config['API'] = {
            'trade_api_key': '',
            'trade_api_secret': '',
            'query_api_key': '',
            'query_api_secret': ''
        }
        self.save_config()
    
    def save_config(self):
        """保存配置到文件"""
        try:
            with open(self.config_file, 'w') as f:
                self.config.write(f)
            logger.info("配置已保存")
        except Exception as e:
            logger.error(f"保存配置失败: {str(e)}")
    
    def set_trade_api_keys(self, api_key: str, api_secret: str):
        """设置交易API密钥"""
        if 'API' not in self.config:
            self.config['API'] = {}
        self.config['API']['trade_api_key'] = api_key
        self.config['API']['trade_api_secret'] = api_secret
        self.save_config()
    
    def set_query_api_keys(self, api_key: str, api_secret: str):
        """设置查询API密钥"""
        if 'API' not in self.config:
            self.config['API'] = {}
        self.config['API']['query_api_key'] = api_key
        self.config['API']['query_api_secret'] = api_secret
        self.save_config()
    
    def get_trade_api_keys(self) -> Tuple[str, str]:
        """获取交易API密钥"""
        if 'API' in self.config:
            return (
                self.config['API'].get('trade_api_key', ''),
                self.config['API'].get('trade_api_secret', '')
            )
        return ('', '')
    
    def get_query_api_keys(self) -> Tuple[str, str]:
        """获取查询API密钥"""
        if 'API' in self.config:
            return (
                self.config['API'].get('query_api_key', ''),
                self.config['API'].get('query_api_secret', '')
            )
        return ('', '')
    
    def has_api_keys(self) -> bool:
        """检查是否已设置API密钥"""
        if 'API' not in self.config:
            return False
        
        api_config = self.config['API']
        return all([
            api_config.get('trade_api_key'),
            api_config.get('trade_api_secret'),
            api_config.get('query_api_key'),
            api_config.get('query_api_secret')
        ])

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

# 定义一些类型别名
OrderType = Dict[str, Any]
OrderID = str
MarketData = Dict[str, Union[float, str]]
Timestamp = float
T = TypeVar('T')  # 用于泛型方法

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
    
    def __init__(self, config: ConfigManager):
        self.config = config
        self._resources = set()
        self.performance_stats = defaultdict(list)
        self.is_test_mode = False
        
        # 基础配置
        self.trade_client = None
        self.query_client = None
        self.ws_client = None
        self.symbol = None
        self.amount = None
        self.price = None
        self.opening_time = None
        
        # 设置时区
        self.timezone = pytz.timezone('Asia/Shanghai')
        
        # 尝试加载API配置
        self.load_api_keys()
        
    def _init_clients(self) -> bool:
        """初始化交易和查询客户端"""
        try:
            # 1. 检查API密钥
            if not self.trade_client or not self.query_client:
                logger.error("API客户端未初始化，请先设置API密钥")
                return False

            # 2. 测试交易API连接
            try:
                balance = self.trade_client.fetch_balance()
                self.balance = balance.get('USDT', {}).get('free', 0)
                logger.info(f"交易API连接正常，USDT余额: {self.balance}")
            except Exception as e:
                logger.error(f"交易API测试失败: {str(e)}")
                return False

            # 3. 测试查询API连接
            try:
                start_time = time.time()
                server_time = self.query_client.fetch_time()
                latency = (time.time() - start_time) * 1000
                logger.info(f"查询API连接正常，延迟: {latency:.2f}ms")
            except Exception as e:
                logger.error(f"查询API测试失败: {str(e)}")
                return False

            # 4. 检查交易对是否存在
            try:
                ticker = self.query_client.fetch_ticker(self.symbol)
                logger.info(f"交易对 {self.symbol} 当前价格: {ticker['last']}")
            except Exception as e:
                logger.error(f"交易对 {self.symbol} 不可用: {str(e)}")
                return False

            # 5. 检查账户权限
            try:
                permissions = balance.get('info', {}).get('permissions', [])
                if 'SPOT' not in permissions:
                    logger.error("API缺少现货交易权限")
                    return False
                logger.info(f"账户权限: {', '.join(permissions)}")
            except Exception as e:
                logger.error(f"检查账户权限失败: {str(e)}")
                return False

            # 6. 优化网络连接
            try:
                # 设置重试策略
                retry_strategy = Retry(
                    total=3,
                    backoff_factor=0.5,
                    status_forcelist=[429, 500, 502, 503, 504]
                )

                # 创建连接适配器
                adapter = HTTPAdapter(
                    pool_connections=20,
                    pool_maxsize=20,
                    max_retries=retry_strategy,
                    pool_block=False
                )

                # 应用到两个客户端
                if hasattr(self.trade_client, 'session'):
                    self.trade_client.session.mount('https://', adapter)
                if hasattr(self.query_client, 'session'):
                    self.query_client.session.mount('https://', adapter)

                logger.info("网络连接已优化")
            except Exception as e:
                logger.warning(f"网络优化失败: {str(e)}")
                # 网络优化失败不影响主功能，继续执行

            logger.info("客户端初始化完成")
            return True

        except Exception as e:
            logger.error(f"初始化客户端失败: {str(e)}")
            return False

    def setup_api_keys(self):
        """设置API密钥"""
        print("\n=== API密钥设置 ===")
        
        # 检查是否已有API配置
        has_existing_api = False
        if self.trade_client and self.query_client:
            try:
                # 测试现有API
                print("\n正在测试现有API连接...")
                
                # 获取交易API信息
                trade_balance = self.trade_client.fetch_balance()
                trade_key = self.trade_client.apiKey
                masked_trade_key = f"{trade_key[:6]}{'*' * (len(trade_key)-10)}{trade_key[-4:]}"
                
                # 获取查询API信息
                query_start = time.time()
                server_time = self.query_client.fetch_time()
                query_latency = (time.time() - query_start) * 1000
                query_key = self.query_client.apiKey
                masked_query_key = f"{query_key[:6]}{'*' * (len(query_key)-10)}{query_key[-4:]}"
                
                # 获取账户权限和余额信息
                permissions = trade_balance.get('info', {}).get('permissions', ['unknown'])
                usdt_balance = trade_balance.get('USDT', {}).get('free', 0)
                total_balance = trade_balance.get('USDT', {}).get('total', 0)
                
                # 获取账户API状态信息
                trade_info = trade_balance.get('info', {})
                
                # 更准确地检查IP白名单状态
                ip_restrict = trade_info.get('ipRestrict', False)
                ip_list = trade_info.get('ipList', [])
                ip_status = '已配置' if ip_restrict or ip_list else '未配置'
                
                # 检查2FA状态
                enable_withdrawals = trade_info.get('enableWithdraw', False)
                enable_internal_transfer = trade_info.get('enableInternalTransfer', False)
                enable_futures = trade_info.get('enableFutures', False)
                
                # 综合判断2FA状态
                security_status = '已开启' if (enable_withdrawals or enable_internal_transfer or enable_futures) else '未开启'
                
                # 计算时间偏移
                local_time = int(time.time() * 1000)
                time_offset = server_time - local_time
                
                print(f"""
====== 当前API状态 ======

交易API信息:
- API Key: {masked_trade_key}
- 交易权限: {', '.join(permissions)}
- USDT余额: {usdt_balance:.2f} (可用)
- USDT总额: {total_balance:.2f} (总计)

查询API信息:
- API Key: {masked_query_key}
- 网络延迟: {query_latency:.2f}ms
- 时间偏移: {time_offset}ms

系统状态:
- 服务器时间: {datetime.fromtimestamp(server_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
- 本地时间: {datetime.fromtimestamp(local_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
- API限额: {self.trade_client.rateLimit}/分钟
- IP白名单: {ip_status} {f'({len(ip_list)}个IP)' if ip_list else ''}
- 2FA状态: {security_status}
- API权限: {'已限制' if ip_restrict else '未限制'}

======================
""")
                
                has_existing_api = True
                change = input("\n是否要更换API密钥? (y/n): ").strip().lower()
                if change != 'y':
                    print("\nAPI设置未变更")
                    return True
                    
            except Exception as e:
                print(f"\n⚠️ 现有API测试失败: {str(e)}")
                print("需要重新设置API")
                
        print("\n请输入币安API密钥信息:")
        trade_api_key = input("交易API Key: ").strip()
        trade_api_secret = input("交易API Secret: ").strip()
        
        use_separate = input("\n是否使用独立的查询API? (建议:是) (y/n): ").strip().lower()
        if use_separate == 'y':
            print("\n请输入查询API (可以使用现货API):")
            query_api_key = input("查询API Key: ").strip()
            query_api_secret = input("查询API Secret: ").strip()
        else:
            query_api_key = trade_api_key
            query_api_secret = trade_api_secret
        
        try:
            print("\n正在测试API连接...")
            
            # 初始化并测试API客户端
            trade_client = self._create_client(trade_api_key, trade_api_secret)
            query_client = self._create_client(query_api_key, query_api_secret)
            
            # 测试交易API
            print("测试交易API...")
            balance = trade_client.fetch_balance()
            usdt_balance = balance.get('USDT', {}).get('free', 0)
            print(f"✅ 交易API正常 (USDT余额: {usdt_balance:.2f})")
            
            # 测试查询API
            print("测试查询API...")
            start_time = time.time()
            query_client.fetch_time()
            latency = (time.time() - start_time) * 1000
            print(f"✅ 查询API正常 (延迟: {latency:.2f}ms)")
            
            # 获取API权限信息
            permissions = balance.get('info', {}).get('permissions', ['unknown'])
            print(f"✅ API权限: {', '.join(permissions)}")
            
            # 测试成功后保存配置
            self.config.set_trade_api_keys(trade_api_key, trade_api_secret)
            self.config.set_query_api_keys(query_api_key, query_api_secret)
            
            # 设置客户端
            self.trade_client = trade_client
            self.query_client = query_client
            
            print("\n✅ API设置成功!")
            print(f"- 交易权限: {', '.join(permissions)}")
            print(f"- USDT余额: {usdt_balance:.2f}")
            print(f"- API延迟: {latency:.2f}ms")
            
            logger.info("API密钥设置成功")
            return True
            
        except Exception as e:
            logger.error(f"API设置失败: {str(e)}")
            print(f"\n❌ API设置失败: {str(e)}")
            print("请检查:")
            print("1. API密钥是否正确")
            print("2. 是否开启了交易权限")
            print("3. 是否绑定了IP白名单")
            return False

    def _create_client(self, api_key: str, api_secret: str):
        """创建CCXT客户端"""
        return ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'adjustForTimeDifference': True
            }
        })

    def load_api_keys(self) -> bool:
        """从配置加载API密钥（同步方法）"""
        try:
            trade_api_key, trade_api_secret = self.config.get_trade_api_keys()
            query_api_key, query_api_secret = self.config.get_query_api_keys()
            
            if not all([trade_api_key, trade_api_secret, query_api_key, query_api_secret]):
                return False
            
            # 初始化客户端
            self.trade_client = self._create_client(trade_api_key, trade_api_secret)
            self.query_client = self._create_client(query_api_key, query_api_secret)
            
            # 测试连接
            self.trade_client.fetch_balance()
            self.query_client.fetch_time()
            
            logger.info("已成功加载API配置")
            return True
            
        except Exception as e:
            logger.error(f"加载API配置失败: {str(e)}")
            self.trade_client = None
            self.query_client = None
            return False

    async def initialize_trading_system(self):
        """初始化交易系统"""
        try:
            logger.info("开始初始化交易系统...")
            
            # 1. 检查策略文件
            logger.info("检查策略文件: config/strategy.ini")
            if not os.path.exists('config/strategy.ini'):
                logger.warning("未找到策略文件")
                return False
                
            logger.info("找到策略文件，尝试加载...")
            if not self.load_strategy():
                logger.error("加载策略失败")
                return False
                
            # 2. 初始化基础组件
            try:
                # 初始化市场深度分析器
                self.depth_analyzer = MarketDepthAnalyzer(self.query_client)
                
                # 初始化订单执行器
                self.order_executor = OrderExecutor()
                
                # 初始化精确等待器
                self.waiter = PreciseWaiter()
                
            except Exception as e:
                logger.error(f"初始化基本组件失败: {str(e)}")
                return False
                
            # 3. 检查API连接
            try:
                # 检查交易API
                balance = await self.trade_client.fetch_balance()
                usdt_balance = float(balance.get('USDT', {}).get('free', 0))
                logger.info(f"交易API连接正常，USDT余额: {usdt_balance}")
                
                # 检查查询API
                start_time = time.time()
                server_time = await self.query_client.fetch_time()
                latency = (time.time() - start_time) * 1000
                logger.info(f"查询API连接正常，延迟: {latency:.2f}ms")
                
                # 获取当前价格
                ticker = await self.query_client.fetch_ticker(self.symbol)
                current_price = float(ticker['last'])
                logger.info(f"交易对 {self.symbol} 当前价格: {current_price}")
                
            except Exception as e:
                logger.error(f"API连接检查失败: {str(e)}")
                return False
                
            logger.info("交易系统初始化完成")
            return True
            
        except Exception as e:
            logger.error(f"初始化交易系统失败: {str(e)}")
            return False

    async def prepare_and_snipe(self):
        """准备并执行抢购"""
        try:
            # 创建精确等待器
            self.waiter = PreciseWaiter()
            
            # 加载API配置
            if not self.load_api_keys():  # 改为同步方法调用
                print("API配置加载失败，请检查配置")
                return
            
            # 1. 初始化交易系统
            if not self._init_clients():  # 使用同步的初始化方法
                print("交易系统初始化失败，请检查配置")
                return
            
            # 2. 验证开盘时间
            if not hasattr(self, 'opening_time') or not self.opening_time:
                print("请先设置开盘时间")
                return
                
            current_time = datetime.now(self.timezone)
            time_to_open = (self.opening_time - current_time).total_seconds()
            
            if time_to_open <= 0:
                print("开盘时间已过")
                return
            
            # 3. 测试网络状态
            print("\n正在测试网络状态...")
            
            # 创建事件循环用于网络测试和执行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                network_stats = loop.run_until_complete(self.measure_network_stats())
                if not network_stats:
                    print("网络状态测试失败，请检查网络连接")
                    return

                print("\n=== 开始网络测试（持续5分钟）===")
                test_results = []
                test_start = time.time()
                
                while time.time() - test_start < 300:  # 测试5分钟
                    stats = loop.run_until_complete(self.measure_network_stats(5))
                    if stats:
                        test_results.append(stats)
                        print(f"\033[2K\r当前网络状态: 延迟 {stats['latency']:.2f}ms (波动: ±{stats['jitter']/2:.2f}ms) 偏移: {stats['offset']:.2f}ms")
                        sys.stdout.flush()
                    time.sleep(30)  # 每30秒测试一次

                # 4. 执行抢购
                print("\n=== 准备开始执行 ===")
                result = loop.run_until_complete(self.execute_snipe())
                
                # 5. 显示结果
                if result:
                    print(f"""
=== 抢购成功 ===
订单ID: {result['id']}
成交价格: {result['average']}
成交数量: {result['filled']}
成交金额: {result['cost']} USDT
""")
                else:
                    print("抢购未成功")
                    
                return result
                
            finally:
                # 关闭事件循环
                try:
                    # 取消所有待处理的任务
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    
                    # 等待所有任务完成
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    
                    # 关闭循环
                    loop.close()
                except Exception as e:
                    logger.error(f"关闭事件循环失败: {str(e)}")
                    
        except Exception as e:
            logger.error(f"抢购任务执行失败: {str(e)}")
            print(f"抢购任务执行失败: {str(e)}")
            return None

    def _init_basic_components(self):
        """初始化基本组件"""
        try:
            # 获取API密钥
            trade_api_key, trade_api_secret = self.config.get_trade_api_keys()
            query_api_key, query_api_secret = self.config.get_query_api_keys()
            
            # 如果有API密钥，则初始化客户端
            if trade_api_key and trade_api_secret:
                self.trade_client = ccxt.binance({
                    'apiKey': trade_api_key,
                    'secret': trade_api_secret,
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'spot',
                        'adjustForTimeDifference': True
                    }
                })
                
                self.query_client = ccxt.binance({
                    'apiKey': query_api_key,
                    'secret': query_api_secret,
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'spot',
                        'adjustForTimeDifference': True
                    }
                })
        except Exception as e:
            logger.warning(f"初始化基本组件时出错: {str(e)}")
    
    def _init_trading_components(self):
        """初始化交易相关组件"""
        if not self.trade_client or not self.query_client:
            raise ValueError("请先设置API密钥")
            
        # 初始化其他组件
        self.time_sync = TimeSync()
        self.execution_controller = ExecutionController(
            time_sync=self.time_sync,
            order_executor=self.order_executor,
            depth_analyzer=self.depth_analyzer,
            monitor=self.monitor
        )
        self.network_optimizer = NetworkOptimizer()
        self.monitor = RealTimeMonitor()
        
        # 设置时区
        self.timezone = pytz.timezone('Asia/Shanghai')
        
        # 初始化连接池
        self._init_connection_pools()

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
    
    async def sync_time(self, final_sync: bool = False) -> Optional[Tuple[float, float]]:
        """高精度时间同步
        Args:
            final_sync: 是否为最终同步
        Returns:
            Tuple[float, float]: (网络延迟, 时间偏移)
        """
        try:
            measurements = []
            samples = 10 if final_sync else 5
            
            for _ in range(samples):
                t1 = time.perf_counter_ns()  # 使用纳秒级计时器
                server_time = await self.query_client.fetch_time()
                t2 = time.perf_counter_ns()
                
                rtt_ns = t2 - t1
                latency = rtt_ns / 2e6  # 转换为毫秒
                offset = server_time - (t1/1e6 + latency)
                
                measurements.append({
                    'latency': latency,
                    'offset': offset,
                    'rtt': rtt_ns / 1e6
                })
                
                await asyncio.sleep(0.1)
                
            # 过滤异常值
            filtered = self._filter_measurements(measurements)
            
            # 使用最优值
            best_measurement = min(filtered, key=lambda x: x['rtt'])
            self.network_latency = best_measurement['latency']
            self.time_offset = best_measurement['offset']
            
            logger.info(f"""
=== 时间同步完成 ===
网络延迟: {self.network_latency:.3f}ms
时间偏移: {self.time_offset:.3f}ms
样本数量: {len(filtered)}
""")
            
            return self.network_latency, self.time_offset
            
        except Exception as e:
            logger.error(f"时间同步失败: {str(e)}")
            return None

    def _filter_measurements(self, measurements: List[Dict]) -> List[Dict]:
        """过滤时间同步异常值"""
        if len(measurements) < 4:
            return measurements
            
        # 计算RTT的四分位数
        rtts = sorted(m['rtt'] for m in measurements)
        q1 = rtts[len(rtts)//4]
        q3 = rtts[3*len(rtts)//4]
        iqr = q3 - q1
        
        # 过滤掉RTT异常的样本
        return [m for m in measurements if q1 - 1.5*iqr <= m['rtt'] <= q3 + 1.5*iqr]

    async def get_server_time(self) -> datetime:
        """获取当前币安服务器时间"""
        if self.time_offset is None:
            await self.sync_time()
        
        current_time = int(time.time() * 1000) + (self.time_offset or 0)
        return datetime.fromtimestamp(current_time/1000, self.timezone)

    async def _check_rate_limit(self, endpoint: str) -> None:
        """检查并控制请求频率"""
        current_time = time.time()
        if endpoint in self.last_query_time:
            elapsed = current_time - self.last_query_time[endpoint]
            if elapsed < self.min_query_interval:
                sleep_time = self.min_query_interval - elapsed
                await asyncio.sleep(sleep_time)
        self.last_query_time[endpoint] = current_time

    def setup_trading_pair(self, symbol: str, amount: float) -> None:
        """设置交易参数"""
        self.symbol = symbol
        self.amount = amount
        logger.info(f"设置交易参数: {symbol}, {amount} USDT")

    def check_balance(self) -> bool:
        """检查账户余额（同步方法）"""
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
        """获取当前市场价格（同步方法）"""
        try:
            ticker = self.query_client.fetch_ticker(self.symbol)
            return float(ticker['last'])
        except Exception as e:
            logger.error(f"获取市场价格失败: {str(e)}")
            return 0.0

    def is_price_safe(self, current_price: float) -> bool:
        """检查价格是否在安全范围内"""
        if not self.price:  # 市价单跳过检查
            return True
            
        deviation = abs(current_price - self.price) / self.price
        if deviation > self.max_price_deviation:
            logger.warning(f"价格偏差过大: 目标 {self.price}, 当前 {current_price}, 偏差 {deviation*100:.2f}%")
            return False
        return True

    async def check_trading_status(self) -> bool:
        """检查是否已开盘"""
        try:
            # 优先使用WebSocket数据
            if self.ws_client and self.ws_client.is_connected():
                market_data = await self.ws_client.get_market_data()
                if market_data:
                    self.opening_price = float(market_data['last'])
                    logger.info(f"检测到开盘价: {self.opening_price}")
                    return True
            
            # 回退到REST API
            ticker = await self.query_client.fetch_ticker(self.symbol)
            if ticker.get('last'):
                self.opening_price = float(ticker['last'])
                logger.info(f"检测到开盘价: {self.opening_price}")
                return True
            
            # 检查订单簿
            orderbook = await self.query_client.fetch_order_book(self.symbol, limit=1)
            return bool(orderbook and orderbook['asks'])
        
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
{chr(10).join([f'- 涨幅 {p*100}% 卖出 {a*100}%' for p, a in self.sell_strategy])}
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
                for i, (profit_target, sell_percent) in enumerate(self.sell_strategy):
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

    def set_snipe_params(self, concurrent_orders: int):
        """设置抢购参数"""
        self.concurrent_orders = concurrent_orders
        
        # 计算最优提前时间
        try:
            # 0. 检查并初始化客户端
            if not self.query_client:
                # 获取API密钥
                query_api_key, query_api_secret = self.config.get_query_api_keys()
                if not query_api_key or not query_api_secret:
                    raise ValueError("请先设置API密钥")
                    
                self.query_client = ccxt.binance({
                    'apiKey': query_api_key,
                    'secret': query_api_secret,
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'spot',
                        'adjustForTimeDifference': True
                    }
                })
            
            # 1. 测试网络延迟
            latencies = []
            for _ in range(10):  # 测试10次
                start = time.perf_counter()
                self.query_client.fetch_time()
                latency = (time.perf_counter() - start) * 1000
                latencies.append(latency)
                time.sleep(0.1)  # 间隔100ms
            
            # 2. 计算网络延迟
            latencies.sort()  # 排序后去掉最高和最低值
            avg_latency = sum(latencies[1:-1]) / len(latencies[1:-1])
            min_latency = min(latencies[1:-1])
            max_latency = max(latencies[1:-1])
            
            # 3. 获取服务器时间偏移
            server_time = self.query_client.fetch_time()
            local_time = int(time.time() * 1000)
            time_offset = abs(server_time - local_time)
            
            # 4. 计算最优提前时间
            # 基础延迟计算
            api_process_time = 20  # API处理时间
            safety_margin = 5      # 基础安全余量
            base_advance = (
                avg_latency +      # 平均网络延迟
                time_offset +      # 时间偏移
                api_process_time + # API处理时间
                safety_margin      # 安全余量
            )
            
            # 根据延迟稳定性调整
            latency_variance = max_latency - min_latency
            extra_margin = 0
            if latency_variance > 50:  # 延迟波动大于50ms
                extra_margin = latency_variance / 2  # 增加一半的波动值作为额外安全余量
                base_advance += extra_margin
            
            self.advance_time = int(base_advance)
            
            logger.info(f"""
=== 抢购参数优化完成 ===
并发订单数: {concurrent_orders}

网络延迟分析:
- 最小延迟: {min_latency:.2f}ms
- 平均延迟: {avg_latency:.2f}ms
- 最大延迟: {max_latency:.2f}ms
- 延迟波动: {latency_variance:.2f}ms
- 时间偏移: {time_offset}ms

提前时间计算:
1. 基础延迟组成:
   - 平均网络延迟:  {avg_latency:.2f}ms
   - 时间偏移补偿:  {time_offset:.2f}ms
   - API处理时间:   {api_process_time}ms
   - 基础安全余量:  {safety_margin}ms
   小计: {avg_latency + time_offset + api_process_time + safety_margin:.2f}ms

2. 波动补偿:
   - 延迟波动范围:  {latency_variance:.2f}ms
   - 额外安全余量:  {extra_margin:.2f}ms
   {f"   (由于延迟波动>{50}ms，增加50%波动值作为额外安全余量)" if latency_variance > 50 else "   (延迟稳定，无需额外安全余量)"}

最终结果:
- 总提前时间: {self.advance_time}ms
- 执行评估: {"极佳 ✨" if self.advance_time < 100 else "良好 ✅" if self.advance_time < 200 else "一般 ⚠️" if self.advance_time < 500 else "较差 ❌"}
""")
            
        except Exception as e:
            logger.error(f"计算最优提前时间失败: {str(e)}")
            # 使用保守的默认值
            self.advance_time = 100
            logger.warning("使用默认提前时间: 100ms")

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

    async def _execute_snipe_orders(self, market_data: Dict):
        """执行抢购订单（优化版）
        Args:
            market_data: 市场数据
        Returns:
            Tuple[List, Set]: (订单列表, 订单ID集合)
        """
        orders = []
        order_ids = set()
        
        # 预分配内存缓冲区
        order_buffer = memoryview(bytearray(1024 * 1024))  # 1MB缓冲区
        
        # 预生成订单基础参数
        base_params = {
            'symbol': self.symbol.replace('/', ''),
            'side': 'BUY',
            'type': 'MARKET',
            'quantity': self.amount,
            'newOrderRespType': 'RESULT'  # 使用精简的响应类型
        }
        
        # 批量生成时间戳
        timestamps = [int(time.time() * 1000) + i for i in range(self.concurrent_orders)]
        
        # 创建订单任务
        async def place_single_order(index: int):
            try:
                # 使用缓冲区的一部分
                buffer_slice = order_buffer[index*1024:(index+1)*1024]
                
                # 更新时间戳和签名
                params = base_params.copy()
                params['timestamp'] = timestamps[index]
                params['signature'] = self._generate_signature(params)
                
                # 执行订单
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        'https://api.binance.com/api/v3/order',
                        headers={
                            'X-MBX-APIKEY': self.trade_client.apiKey,
                            'Content-Type': 'application/json'
                        },
                        json=params,
                        timeout=1  # 1秒超时
                    ) as response:
                        if response.status == 200:
                            order = await response.json()
                            if order and order.get('orderId'):
                                async with self._lock:
                                    orders.append(order)
                                    order_ids.add(str(order['orderId']))
                                logger.info(f"订单已提交: {order['orderId']}")
            except Exception as e:
                logger.error(f"下单失败: {str(e)}")
        
        # 并发执行所有订单
        tasks = [place_single_order(i) for i in range(self.concurrent_orders)]
        await asyncio.gather(*tasks)
        
        return orders, order_ids

    async def snipe(self) -> Optional[OrderType]:
        """执行抢购"""
        try:
            # 1. 系统初始化
            if not await self.initialize():
                logger.error("系统初始化失败")
                return None
            
            # 2. 委托给执行控制器
            result = await self.execution_controller.execute_snipe(
                client=self.trade_client,
                symbol=self.symbol,
                amount=self.amount,
                target_time=self.opening_time.timestamp() * 1000,
                price=self.price,
                concurrent_orders=self.concurrent_orders
            )
            
            if result:
                logger.info("抢购执行成功")
                return result
            else:
                logger.warning("抢购执行失败")
                return None
            
        except asyncio.CancelledError:
            logger.warning("抢购任务被取消")
            return None
        except Exception as e:
            logger.error(f"抢购执行失败: {str(e)}")
            return None
        finally:
            try:
                await self.cleanup()
            except Exception as e:
                logger.error(f"清理资源失败: {str(e)}")

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
{chr(10).join([f'- 涨幅 {p*100}% 卖出 {a*100}%' for p, a in sell_ladders])}
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

    async def cleanup(self) -> None:
        """清理资源"""
        try:
            logger.info("开始清理资源...")
            
            # 关闭WebSocket连接
            if hasattr(self, 'ws_client') and self.ws_client:
                try:
                    self.ws_client.close()
                    logger.info("WebSocket连接已关闭")
                except Exception as e:
                    logger.error(f"关闭WebSocket连接失败: {str(e)}")
            
            # 关闭CCXT客户端
            if hasattr(self, 'trade_client') and self.trade_client:
                try:
                    # CCXT客户端不需要显式关闭
                    self.trade_client = None
                    logger.info("交易客户端已清理")
                except Exception as e:
                    logger.error(f"清理交易客户端失败: {str(e)}")
                
            if hasattr(self, 'query_client') and self.query_client:
                try:
                    # CCXT客户端不需要显式关闭
                    self.query_client = None
                    logger.info("查询客户端已清理")
                except Exception as e:
                    logger.error(f"清理查询客户端失败: {str(e)}")
            
            # 关闭事件循环
            if hasattr(self, 'event_loop') and self.event_loop:
                try:
                    self.event_loop.close()
                    logger.info("事件循环已关闭")
                except Exception as e:
                    logger.error(f"关闭事件循环失败: {str(e)}")
            
            # 保存配置
            if hasattr(self, 'config'):
                try:
                    self.config.save()
                    logger.info("配置已保存")
                except Exception as e:
                    logger.error(f"保存配置失败: {str(e)}")
            
            logger.info("资源清理完成")
            
        except Exception as e:
            logger.error(f"清理资源失败: {str(e)}")

    async def _cleanup_temp_files(self) -> None:
        """清理临时文件"""
        patterns = ['*.tmp', '*.log.?', 'performance_stats_*.json']
        for pattern in patterns:
            for file in glob.glob(pattern):
                if os.path.isfile(file):
                    try:
                        os.remove(file)
                        logger.debug(f"已删除临时文件: {file}")
                    except Exception as e:
                        logger.warning(f"删除文件 {file} 失败: {str(e)}")

    def __enter__(self: T) -> T:
        """上下文管理器入口"""
        return self

    def __exit__(self, exc_type: Optional[Type[BaseException]], 
                exc_val: Optional[BaseException], 
                exc_tb: Optional[TracebackType]) -> None:
        """上下文管理器退出"""
        try:
            # 清理资源
            if hasattr(self, 'ws_client') and self.ws_client:
                self.ws_client.close()
            
            if hasattr(self, 'trade_session') and self.trade_session:
                self.trade_session.close()
                
            if hasattr(self, 'query_session') and self.query_session:
                self.query_session.close()
                
            if hasattr(self, 'async_session') and self.async_session:
                if not self.async_session.closed:
                    asyncio.run(self.async_session.close())
            
            # 清理其他资源
            for resource in self._resources:
                if hasattr(resource, 'close'):
                    resource.close()
                elif hasattr(resource, '__exit__'):
                    resource.__exit__(None, None, None)
                    
            logger.info("资源清理完成")
            
        except Exception as e:
            logger.error(f"清理资源时发生错误: {str(e)}")

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
            return None  # 修复这里的缩进

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
            return None    # 这里的缩进有问题
        
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
            # 创建精确等待器
            self.waiter = PreciseWaiter()
            
            # 加载API配置
            if not self.load_api_keys():  # 改为同步方法调用
                print("API配置加载失败，请检查配置")
                return
            
            # 1. 初始化交易系统
            if not self._init_clients():  # 使用同步的初始化方法
                print("交易系统初始化失败，请检查配置")
                return
            
            # 2. 验证开盘时间
            if not hasattr(self, 'opening_time') or not self.opening_time:
                print("请先设置开盘时间")
                return
                
            current_time = datetime.now(self.timezone)
            time_to_open = (self.opening_time - current_time).total_seconds()
            
            if time_to_open <= 0:
                print("开盘时间已过")
                return
            
            # 3. 测试网络状态
            print("\n正在测试网络状态...")
            
            # 创建事件循环用于网络测试和执行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                network_stats = loop.run_until_complete(self.measure_network_stats())
                if not network_stats:
                    print("网络状态测试失败，请检查网络连接")
                    return

                print("\n=== 开始网络测试（持续5分钟）===")
                test_results = []
                test_start = time.time()
                
                while time.time() - test_start < 300:  # 测试5分钟
                    stats = loop.run_until_complete(self.measure_network_stats(5))
                    if stats:
                        test_results.append(stats)
                        print(f"\033[2K\r当前网络状态: 延迟 {stats['latency']:.2f}ms (波动: ±{stats['jitter']/2:.2f}ms) 偏移: {stats['offset']:.2f}ms")
                        sys.stdout.flush()
                    time.sleep(30)  # 每30秒测试一次

                # 4. 执行抢购
                print("\n=== 准备开始执行 ===")
                result = loop.run_until_complete(self.execute_snipe())
                
                # 5. 显示结果
                if result:
                    print(f"""
=== 抢购成功 ===
订单ID: {result['id']}
成交价格: {result['average']}
成交数量: {result['filled']}
成交金额: {result['cost']} USDT
""")
                else:
                    print("抢购未成功")
                    
                return result
                
            finally:
                # 关闭事件循环
                try:
                    # 取消所有待处理的任务
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    
                    # 等待所有任务完成
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    
                    # 关闭循环
                    loop.close()
                except Exception as e:
                    logger.error(f"关闭事件循环失败: {str(e)}")
                    
        except Exception as e:
            logger.error(f"抢购任务执行失败: {str(e)}")
            print(f"抢购任务执行失败: {str(e)}")
            return None

    def setup_snipe_strategy(self):
        """设置抢购策略（同步方法）"""
        try:
            logger.info("开始设置抢购策略...")
            
            # 获取当前服务器时间作为参考
            server_time = self.query_client.fetch_time()
            current_time = datetime.fromtimestamp(server_time/1000, self.timezone)
            
            # 1. 基础设置
            print("\n>>> 基础参数设置")
            coin = get_valid_input("请输入要买的币种 (例如 BTC): ", str).upper()
            symbol = f"{coin}/USDT"  # 自动添加USDT交易对
            total_usdt = get_valid_input("请输入买入金额(USDT): ", float)
            
            # 2. 价格保护设置
            print("\n>>> 价格保护设置")
            print("价格保护机制说明:")
            print("1. 最高接受单价: 绝对价格上限，无论如何都不会超过此价格")
            print("2. 开盘价倍数: 相对于第一档价格的最大倍数")
            
            max_price = get_valid_input("设置最高接受单价 (USDT): ", float)
            price_multiplier = get_valid_input("设置开盘价倍数 (例如 3 表示最高接受开盘价的3倍): ", float)
            
            # 3. 执行参数设置
            print("\n>>> 执行参数设置")
            concurrent_orders = get_valid_input("并发订单数量 (建议1-5): ", int)
            advance_time = 100  # 默认提前100ms
            
            # 4. 止盈止损设置
            print("\n>>> 止盈止损设置")
            print("请输入止损百分比 (例如: 输入5表示5%)")
            stop_loss_percent = get_valid_input("止损百分比: ", float)
            stop_loss = stop_loss_percent / 100  # 转换为小数
            
            # 设置三档止盈
            print("\n设置三档止盈:")
            sell_strategy = []
            total_sell_percent = 0  # 记录总卖出比例
            
            for i in range(3):
                # 检查剩余可卖出比例
                remaining_percent = 100 - total_sell_percent
                if remaining_percent <= 0:
                    print("\n已设置完所有卖出比例")
                    break
                    
                print(f"\n第{i+1}档止盈设置:")
                print(f"当前已设置卖出比例: {total_sell_percent}%")
                print(f"剩余可设置比例: {remaining_percent}%")
                
                print(f"请输入涨幅百分比 (例如: 输入20表示20%)")
                profit_percent = get_valid_input(f"涨幅百分比: ", float)
                print(f"请输入卖出百分比 (最多可设置 {remaining_percent}%)")
                while True:
                    sell_percent = get_valid_input(f"卖出百分比: ", float)
                    if sell_percent <= 0:
                        print("卖出比例必须大于0%")
                        continue
                    if sell_percent > remaining_percent:
                        print(f"错误: 超出可设置比例，最多还可设置 {remaining_percent}%")
                        continue
                    break
                
                # 转换为小数
                profit_ratio = profit_percent / 100
                sell_ratio = sell_percent / 100
                
                sell_strategy.append((profit_ratio, sell_ratio))
                total_sell_percent += sell_percent
                
                # 显示当前设置情况
                print("\n当前止盈设置:")
                for idx, (p, a) in enumerate(sell_strategy):
                    print(f"- 第{idx+1}档: 涨幅{p*100:.0f}% 卖出{a*100:.0f}%")
                print(f"总计已设置: {total_sell_percent}%")

            # 5. 时间设置
            print("\n>>> 开盘时间设置")
            print("请输入开盘时间 (东八区/北京时间)")
            
            # 获取当前服务器时间作为参考
            server_time = self.query_client.fetch_time()
            current_time = datetime.fromtimestamp(server_time/1000, self.timezone)
            print(f"\n当前币安服务器时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
            
            while True:
                try:
                    year = get_valid_input("年 (例如 2024): ", int)
                    if year < 2024 or year > 2100:
                        print("年份无效，请输入2024-2100之间的年份")
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
                    
                    # 创建开盘时间
                    opening_time = self.timezone.localize(datetime(year, month, day, hour, minute, second))
                    
                    # 检查是否早于当前时间
                    if opening_time <= current_time:
                        print("错误: 开盘时间不能早于当前时间")
                        continue
                    
                    # 显示时间信息
                    time_diff = opening_time - current_time
                    hours = time_diff.total_seconds() / 3600
                    
                    print(f"\n=== 时间信息 ===")
                    print(f"设置开盘时间: {opening_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
                    print(f"当前服务器时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
                    print(f"距离开盘还有: {hours:.1f}小时")
                    
                    confirm = input("\n确认使用这个开盘时间? (yes/no): ")
                    if confirm.lower() == 'yes':
                        self.opening_time = opening_time
                        break
                    
                except ValueError as e:
                    print(f"时间格式无效，请重新输入")
                    continue

            # 6. 应用所有设置
            self.setup_trading_pair(symbol, total_usdt)
            self.max_price_limit = max_price
            self.price_multiplier = price_multiplier
            self.concurrent_orders = concurrent_orders
            self.advance_time = advance_time
            self.stop_loss = stop_loss
            self.sell_strategy = sell_strategy
            
            # 保存策略
            if self.save_strategy():
                print("\n策略已保存到配置文件")
            else:
                print("\n警告: 策略保存失败")
            
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

    def _fetch_market_data(self) -> Optional[Dict]:
        """获取市场数据
        Returns:
            Optional[Dict]: 市场数据或None
        """
        try:
            depth = self.query_client.fetch_order_book(self.symbol, limit=5)
            if not depth or not depth.get('asks') or not depth.get('bids'):
                return None
                
            market_data = {
                'first_ask': depth['asks'][0][0],
                'first_bid': depth['bids'][0][0],
                'spread': depth['asks'][0][0] - depth['bids'][0][0],
                'timestamp': depth['timestamp'],
                'depth': depth
            }
            return market_data
            
        except Exception as e:
            logger.error(f"获取市场数据失败: {str(e)}")
            return None

    def prepare_and_snipe(self):
        """准备并执行抢购"""
        try:
            # 创建精确等待器
            self.waiter = PreciseWaiter()
            
            # 加载API配置
            if not self.load_api_keys():  # 改为同步方法调用
                print("API配置加载失败，请检查配置")
                return
            
            # 1. 初始化交易系统
            if not self._init_clients():  # 使用同步的初始化方法
                print("交易系统初始化失败，请检查配置")
                return
            
            # 2. 验证开盘时间
            if not hasattr(self, 'opening_time') or not self.opening_time:
                print("请先设置开盘时间")
                return
                
            current_time = datetime.now(self.timezone)
            time_to_open = (self.opening_time - current_time).total_seconds()
            
            if time_to_open <= 0:
                print("开盘时间已过")
                return
            
            # 3. 测试网络状态
            print("\n正在测试网络状态...")
            
            # 创建事件循环用于网络测试和执行
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                network_stats = loop.run_until_complete(self.measure_network_stats())
                if not network_stats:
                    print("网络状态测试失败，请检查网络连接")
                    return

                print("\n=== 开始网络测试（持续5分钟）===")
                test_results = []
                test_start = time.time()
                
                while time.time() - test_start < 300:  # 测试5分钟
                    stats = loop.run_until_complete(self.measure_network_stats(5))
                    if stats:
                        test_results.append(stats)
                        print(f"\033[2K\r当前网络状态: 延迟 {stats['latency']:.2f}ms (波动: ±{stats['jitter']/2:.2f}ms) 偏移: {stats['offset']:.2f}ms")
                        sys.stdout.flush()
                    time.sleep(30)  # 每30秒测试一次

                # 4. 执行抢购
                print("\n=== 准备开始执行 ===")
                result = loop.run_until_complete(self.execute_snipe())
                
                # 5. 显示结果
                if result:
                    print(f"""
=== 抢购成功 ===
订单ID: {result['id']}
成交价格: {result['average']}
成交数量: {result['filled']}
成交金额: {result['cost']} USDT
""")
                else:
                    print("抢购未成功")
                    
                return result
                
            finally:
                # 关闭事件循环
                try:
                    # 取消所有待处理的任务
                    pending = asyncio.all_tasks(loop)
                    for task in pending:
                        task.cancel()
                    
                    # 等待所有任务完成
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    
                    # 关闭循环
                    loop.close()
                except Exception as e:
                    logger.error(f"关闭事件循环失败: {str(e)}")
                    
        except Exception as e:
            logger.error(f"抢购任务执行失败: {str(e)}")
            print(f"抢购任务执行失败: {str(e)}")
            return None

    async def init_market_stream(self):
        """初始化市场数据流"""
        self.ws_client = BinanceWebSocket(self.symbol)
        return await self.ws_client.connect()
    
    async def _fetch_market_data_ws(self) -> Optional[Dict]:
        """通过WebSocket获取市场数据"""
        if not self.ws_client:
            return None
            
        data = await self.ws_client.receive_data()
        if not data:
            return None
            
        return {
            'first_ask': float(data.asks[0][0]),
            'first_bid': float(data.bids[0][0]),
            'spread': float(data.asks[0][0]) - float(data.bids[0][0]),
            'timestamp': data.timestamp,
            'depth': {
                'asks': data.asks,
                'bids': data.bids
            }
        }
    
    async def _execute_order_async(self, params: Dict) -> Optional[Dict]:
        """异步执行订单"""
        try:
            async with aiohttp.ClientSession() as session:
                # 使用预签名的参数
                async with session.post(
                    'https://api.binance.com/api/v3/order',
                    headers={'X-MBX-APIKEY': self.trade_client.apiKey},
                    json=params,
                    timeout=1  # 1秒超时
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        logger.error(f"订单执行失败: {response.status}")
                        return None
        except Exception as e:
            logger.error(f"订单执行异常: {str(e)}")
            return None

    async def _execute_snipe_orders_async(self, market_data: Dict) -> Tuple[List, Set]:
        """异步执行抢购订单"""
        orders = []
        order_ids = set()
        
        # 预生成订单参数
        base_params = self._prepare_order_params()
        if not base_params:
            return [], set()
        
        # 创建多个订单任务
        order_tasks = []
        for _ in range(self.concurrent_orders):
            params = base_params.copy()
            # 添加时间戳和签名
            params['timestamp'] = int(time.time() * 1000)
            params['signature'] = self._generate_signature(params)
            
            task = self._execute_order_async(params)
            order_tasks.append(task)
        
        # 并发执行所有订单
        completed_orders = await asyncio.gather(*order_tasks, return_exceptions=True)
        
        # 处理结果
        for order in completed_orders:
            if isinstance(order, dict) and order.get('orderId'):
                orders.append(order)
                order_ids.add(str(order['orderId']))
        
        return orders, order_ids

    async def execute_snipe(self) -> Optional[OrderType]:
        """执行抢购"""
        try:
            # 1. 系统初始化
            if not await self.initialize():
                return None
                
            # 2. 计算关键时间点
            target_time = self.opening_time.timestamp() * 1000
            fetch_time = target_time - (self.network_latency + 25)  # 提前25ms获取数据
            send_time = target_time - (self.network_latency + 5)   # 提前5ms发送订单
            
            logger.info(f"""
=== 执行计划 ===
目标时间: {datetime.fromtimestamp(target_time/1000).strftime('%H:%M:%S.%f')}
数据获取: {datetime.fromtimestamp(fetch_time/1000).strftime('%H:%M:%S.%f')}
订单发送: {datetime.fromtimestamp(send_time/1000).strftime('%H:%M:%S.%f')}
网络延迟: {self.network_latency:.2f}ms
""")
            
            # 3. 等待并获取市场数据
            await self._precise_wait(fetch_time)
            market_data = await self.ws_client.get_market_data()
            if not market_data:
                return None
                
            # 4. 检查价格
            if not self.is_price_safe(market_data['ask']):
                return None
                
            # 5. 等待并执行订单
            await self._precise_wait(send_time)
            orders, order_ids = await self._execute_orders_optimized(market_data)
            
            # 6. 处理订单结果
            return await self._handle_orders(order_ids)
            
        except Exception as e:
            logger.error(f"抢购执行失败: {str(e)}")
            return None
        finally:
            try:
                await self.cleanup()
            except Exception as e:
                logger.error(f"清理资源失败: {str(e)}")

    async def _precise_wait(self, target_time: float):
        """高精度等待
        Args:
            target_time: 目标时间戳(毫秒)
        """
        while True:
            current = time.time() * 1000
            if current >= target_time:
                break
                
            remaining = target_time - current
            if remaining > 1:
                await asyncio.sleep(remaining / 2000)  # 转换为秒并减半
            else:
                await asyncio.sleep(0)  # 让出CPU时间片

    # 删除其他执行订单的方法,只保留优化版本
    async def _execute_orders_optimized(self, market_data: MarketData) -> Tuple[List[OrderType], Set[OrderID]]:
        """优化的订单执行"""
        try:
            orders = []
            order_ids = set()
            
            # 预分配内存和参数
            params_buffer = memoryview(self._order_buffer)
            base_params = {
                'symbol': self.symbol.replace('/', ''),
                'side': 'BUY',
                'type': 'LIMIT_IOC',
                'quantity': self.amount,
                'price': market_data['ask'],
                'newOrderRespType': 'RESULT'
            }
            
            # 批量生成时间戳和签名
            timestamps = [int(time.time() * 1000) + i for i in range(self.concurrent_orders)]
            signatures = await self._batch_generate_signatures(base_params, timestamps)
            
            # 创建并发任务
            async with aiohttp.ClientSession() as session:
                tasks = [
                    self._execute_single_order(
                        {**base_params, 'timestamp': ts, 'signature': sig},
                        params_buffer[i*1024:(i+1)*1024],
                        session
                    )
                    for i, (ts, sig) in enumerate(zip(timestamps, signatures))
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果
            for result in results:
                if isinstance(result, dict) and result.get('orderId'):
                    orders.append(result)
                    order_ids.add(str(result['orderId']))
            
            return orders, order_ids
            
        except asyncio.CancelledError:
            logger.warning("订单执行被取消")
            return [], set()
        except Exception as e:
            logger.error(f"订单执行失败: {str(e)}")
            return [], set()

    # 添加单个订单执行方法
    async def _execute_single_order(self, params: Dict, buffer: memoryview, session: aiohttp.ClientSession) -> Optional[OrderType]:
        """执行单个订单"""
        try:
            async with session.post(
                'https://api.binance.com/api/v3/order',
                headers={'X-MBX-APIKEY': self.trade_client.apiKey},
                json=params,
                timeout=1
            ) as response:
                if response.status == 200:
                    return await response.json()
                return None
        except Exception as e:
            logger.error(f"执行订单失败: {str(e)}")
            return None

    def show_api_status(self):
        """显示并测试当前API配置"""
        print("\n=== 当前API配置状态 ===")
        
        # 获取当前配置
        trade_key, _ = self.config.get_trade_api_keys()
        query_key, _ = self.config.get_query_api_keys()
        
        # 显示配置信息（只显示前6位和后4位，中间用*代替）
        def mask_key(key: str) -> str:
            if not key:
                return "未设置"
            return f"{key[:6]}{'*' * (len(key)-10)}{key[-4:]}"
        
        print(f"""
交易API Key: {mask_key(trade_key)}
查询API Key: {mask_key(query_key)}
""")
        
        # 测试API连接
        print("\n正在测试API连接...")
        
        success = True
        try:
            # 测试交易API
            print("\n1. 测试交易API:")
            balance = self.trade_client.fetch_balance()
            usdt_balance = balance.get('USDT', {}).get('free', 0)
            print(f"✅ 连接成功")
            print(f"   可用USDT余额: {usdt_balance:.2f}")
            
            # 测试查询API
            print("\n2. 测试查询API:")
            server_time = self.query_client.fetch_time()
            local_time = int(time.time() * 1000)
            time_diff = abs(server_time - local_time)
            print(f"✅ 连接成功")
            print(f"   服务器时间: {datetime.fromtimestamp(server_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}")
            print(f"   时间偏差: {time_diff}ms")
            
            # 测试市场数据访问
            print("\n3. 测试市场数据访问:")
            btc_ticker = self.query_client.fetch_ticker('BTC/USDT')
            print(f"✅ 数据访问正常")
            print(f"   BTC当前价格: {btc_ticker['last']:.2f} USDT")
            
            print("\n=== 测试结论 ===")
            print("API配置状态: 正常 ✨")
            print(f"建议: 可以开始设置抢购策略")
            
        except Exception as e:
            success = False
            print(f"\n❌ 测试失败: {str(e)}")
            print("\n=== 测试结论 ===")
            print("API配置状态: 异常 ⚠️")
            print("建议: 请重新设置API密钥")
        
        return success

    def setup_api_keys(self):
        """设置API密钥"""
        print("\n=== API密钥设置 ===")
        
        # 检查是否已有API配置
        has_existing_api = False
        if self.trade_client and self.query_client:
            try:
                # 测试现有API
                print("\n正在测试现有API连接...")
                
                # 获取交易API信息
                trade_balance = self.trade_client.fetch_balance()
                trade_key = self.trade_client.apiKey
                masked_trade_key = f"{trade_key[:6]}{'*' * (len(trade_key)-10)}{trade_key[-4:]}"
                
                # 获取查询API信息
                query_start = time.time()
                server_time = self.query_client.fetch_time()
                query_latency = (time.time() - query_start) * 1000
                query_key = self.query_client.apiKey
                masked_query_key = f"{query_key[:6]}{'*' * (len(query_key)-10)}{query_key[-4:]}"
                
                # 获取账户权限和余额信息
                permissions = trade_balance.get('info', {}).get('permissions', ['unknown'])
                usdt_balance = trade_balance.get('USDT', {}).get('free', 0)
                total_balance = trade_balance.get('USDT', {}).get('total', 0)
                
                # 获取账户API状态信息
                trade_info = trade_balance.get('info', {})
                
                # 更准确地检查IP白名单状态
                ip_restrict = trade_info.get('ipRestrict', False)
                ip_list = trade_info.get('ipList', [])
                ip_status = '已配置' if ip_restrict or ip_list else '未配置'
                
                # 检查2FA状态
                enable_withdrawals = trade_info.get('enableWithdraw', False)
                enable_internal_transfer = trade_info.get('enableInternalTransfer', False)
                enable_futures = trade_info.get('enableFutures', False)
                
                # 综合判断2FA状态
                security_status = '已开启' if (enable_withdrawals or enable_internal_transfer or enable_futures) else '未开启'
                
                # 计算时间偏移
                local_time = int(time.time() * 1000)
                time_offset = server_time - local_time
                
                print(f"""
====== 当前API状态 ======

交易API信息:
- API Key: {masked_trade_key}
- 交易权限: {', '.join(permissions)}
- USDT余额: {usdt_balance:.2f} (可用)
- USDT总额: {total_balance:.2f} (总计)

查询API信息:
- API Key: {masked_query_key}
- 网络延迟: {query_latency:.2f}ms
- 时间偏移: {time_offset}ms

系统状态:
- 服务器时间: {datetime.fromtimestamp(server_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
- 本地时间: {datetime.fromtimestamp(local_time/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
- API限额: {self.trade_client.rateLimit}/分钟
- IP白名单: {ip_status} {f'({len(ip_list)}个IP)' if ip_list else ''}
- 2FA状态: {security_status}
- API权限: {'已限制' if ip_restrict else '未限制'}

======================
""")
                
                has_existing_api = True
                change = input("\n是否要更换API密钥? (y/n): ").strip().lower()
                if change != 'y':
                    print("\nAPI设置未变更")
                    return True
                    
            except Exception as e:
                print(f"\n⚠️ 现有API测试失败: {str(e)}")
                print("需要重新设置API")
                
        print("\n请输入币安API密钥信息:")
        trade_api_key = input("交易API Key: ").strip()
        trade_api_secret = input("交易API Secret: ").strip()
        
        use_separate = input("\n是否使用独立的查询API? (建议:是) (y/n): ").strip().lower()
        if use_separate == 'y':
            print("\n请输入查询API (可以使用现货API):")
            query_api_key = input("查询API Key: ").strip()
            query_api_secret = input("查询API Secret: ").strip()
        else:
            query_api_key = trade_api_key
            query_api_secret = trade_api_secret
        
        try:
            print("\n正在测试API连接...")
            
            # 初始化并测试API客户端
            trade_client = self._create_client(trade_api_key, trade_api_secret)
            query_client = self._create_client(query_api_key, query_api_secret)
            
            # 测试交易API
            print("测试交易API...")
            balance = trade_client.fetch_balance()
            usdt_balance = balance.get('USDT', {}).get('free', 0)
            print(f"✅ 交易API正常 (USDT余额: {usdt_balance:.2f})")
            
            # 测试查询API
            print("测试查询API...")
            start_time = time.time()
            query_client.fetch_time()
            latency = (time.time() - start_time) * 1000
            print(f"✅ 查询API正常 (延迟: {latency:.2f}ms)")
            
            # 获取API权限信息
            permissions = balance.get('info', {}).get('permissions', ['unknown'])
            print(f"✅ API权限: {', '.join(permissions)}")
            
            # 测试成功后保存配置
            self.config.set_trade_api_keys(trade_api_key, trade_api_secret)
            self.config.set_query_api_keys(query_api_key, query_api_secret)
            
            # 设置客户端
            self.trade_client = trade_client
            self.query_client = query_client
            
            print("\n✅ API设置成功!")
            print(f"- 交易权限: {', '.join(permissions)}")
            print(f"- USDT余额: {usdt_balance:.2f}")
            print(f"- API延迟: {latency:.2f}ms")
            
            logger.info("API密钥设置成功")
            return True
            
        except Exception as e:
            logger.error(f"API设置失败: {str(e)}")
            print(f"\n❌ API设置失败: {str(e)}")
            print("请检查:")
            print("1. API密钥是否正确")
            print("2. 是否开启了交易权限")
            print("3. 是否绑定了IP白名单")
            return False

    def print_strategy(self):
        """显示当前策略设置"""
        try:
            logger.info("开始打印策略...")
            
            # 1. 加载策略文件
            if not hasattr(self, 'symbol') or not self.symbol:
                if not self.load_strategy():
                    print("\n⚠️ 未找到已保存的策略，请先设置策略")
                    return False
            
            # 2. 获取实时市场数据
            try:
                ticker = self.query_client.fetch_ticker(self.symbol)
                current_price = ticker['last']
                price_24h_change = ticker['percentage']
                volume_24h = ticker['quoteVolume']
            except Exception as e:
                logger.error(f"获取市场数据失败: {str(e)}")
                current_price = 0
                price_24h_change = 0
                volume_24h = 0

            # 3. 获取账户余额
            try:
                balance = self.trade_client.fetch_balance()
                self.balance = balance.get('USDT', {}).get('free', 0)
            except Exception as e:
                logger.error(f"获取余额失败: {str(e)}")
                self.balance = 0

            # 4. 计算时间相关信息
            now = datetime.now(self.timezone)
            time_diff = self.opening_time - now
            hours_remaining = time_diff.total_seconds() / 3600
            
            # 5. 打印完整策略信息
            print(f"""
====== 当前抢购策略详情 ======

📌 基础参数
- 交易对: {self.symbol}
- 买入金额: {self.amount} USDT
- 买入数量: {self.amount/current_price:.8f} {self.symbol.split('/')[0] if current_price > 0 else 'Unknown'}

📊 市场信息
- 当前价格: {current_price} USDT
- 24h涨跌: {price_24h_change:.2f}%
- 24h成交: {volume_24h:.2f} USDT

⏰ 时间信息
- 开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S')}
- 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}
- 剩余时间: {int(hours_remaining)}小时{int((hours_remaining % 1) * 60)}分钟

💰 价格保护
- 最高限价: {self.max_price_limit} USDT
- 价格倍数: {self.price_multiplier}倍

⚡ 执行策略
- 并发订单: {self.concurrent_orders}个
- 提前时间: {self.advance_time}ms

📈 止盈止损
- 止损线: -{self.stop_loss*100:.1f}%
阶梯止盈:""")

            # 打印止盈策略
            for idx, (profit, amount) in enumerate(self.sell_strategy, 1):
                print(f"- 第{idx}档: 涨幅{profit*100:.0f}% 卖出{amount*100:.0f}%")

            print(f"""
💰 账户状态
- 可用USDT: {self.balance:.2f}
- 所需USDT: {self.amount:.2f}
- 状态: {'✅ 余额充足' if self.balance >= self.amount else '❌ 余额不足'}

⚠️ 风险提示
1. 已启用最高价格保护: {self.max_price_limit} USDT
2. 已启用价格倍数限制: {self.price_multiplier}倍
3. 使用IOC限价单模式
""")
            
            logger.info("策略信息打印完成")
            return True
            
        except Exception as e:
            logger.error(f"打印策略失败: {str(e)}", exc_info=True)
            print(f"\n⚠️ 打印策略失败: {str(e)}")
            print("请检查是否已正确设置策略")
            return False

    def save_strategy(self):
        """保存当前策略到配置文件"""
        try:
            if not hasattr(self, 'opening_time') or not self.opening_time:
                logger.error("未设置开盘时间")
                return False
                
            if not os.path.exists(self.config.config_dir):
                os.makedirs(self.config.config_dir)
                
            strategy_file = os.path.join(self.config.config_dir, 'strategy.ini')
            config = configparser.ConfigParser()
            
            # 保存基础参数
            config['Basic'] = {
                'symbol': self.symbol,
                'amount': str(self.amount),
                'opening_time': self.opening_time.strftime('%Y-%m-%d %H:%M:%S')
            }
            
            # 保存价格保护参数
            config['PriceProtection'] = {
                'max_price_limit': str(self.max_price_limit),
                'price_multiplier': str(self.price_multiplier)
            }
            
            # 保存执行参数
            config['Execution'] = {
                'concurrent_orders': str(self.concurrent_orders),
                'advance_time': str(self.advance_time)
            }
            
            # 保存止盈止损策略
            config['StopStrategy'] = {
                'stop_loss': str(self.stop_loss),
                'sell_strategy': json.dumps(self.sell_strategy)  # 将列表转换为JSON字符串
            }
            
            with open(strategy_file, 'w') as f:
                config.write(f)
                
            logger.info("策略已保存到配置文件")
            return True
            
        except Exception as e:
            logger.error(f"保存策略失败: {str(e)}")
            return False

    def load_strategy(self) -> bool:
        """从配置文件加载策略"""
        try:
            # 使用 config_dir 从 ConfigManager 获取正确的路径
            strategy_file = os.path.join(self.config.config_dir, 'strategy.ini')
            if not os.path.exists(strategy_file):
                logger.info(f"未找到策略配置文件: {strategy_file}")
                return False
                
            config = configparser.ConfigParser()
            config.read(strategy_file)
            
            try:
                # 加载基础参数
                self.symbol = config['Basic']['symbol']
                self.amount = float(config['Basic']['amount'])
                self.opening_time = datetime.strptime(
                    config['Basic']['opening_time'],
                    '%Y-%m-%d %H:%M:%S'
                ).replace(tzinfo=self.timezone)
                
                # 加载价格保护参数
                self.max_price_limit = float(config['PriceProtection']['max_price_limit'])
                self.price_multiplier = float(config['PriceProtection']['price_multiplier'])
                
                # 加载执行参数
                self.concurrent_orders = int(config['Execution']['concurrent_orders'])
                self.advance_time = int(config['Execution']['advance_time'])
                
                # 加载止盈止损策略
                self.stop_loss = float(config['StopStrategy']['stop_loss'])
                self.sell_strategy = json.loads(config['StopStrategy']['sell_strategy'])
                
                logger.info(f"""
=== 策略加载成功 ===
交易对: {self.symbol}
买入金额: {self.amount} USDT
开盘时间: {self.opening_time}

价格保护:
- 最高价格: {self.max_price_limit} USDT
- 价格倍数: {self.price_multiplier}倍

执行参数:
- 并发订单: {self.concurrent_orders}
- 提前时间: {self.advance_time}ms

止盈止损:
- 止损线: {self.stop_loss*100}%
- 止盈策略:
{chr(10).join([f'  - 涨幅{p*100}% 卖出{a*100}%' for p, a in self.sell_strategy])}
""")
                return True
                
            except KeyError as e:
                logger.error(f"配置文件缺少必要参数: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"加载策略失败: {str(e)}")
            return False

    def setup_snipe_strategy(self):
        """设置抢购策略（同步方法）"""
        try:
            logger.info("开始设置抢购策略...")
            
            # 获取当前服务器时间作为参考
            server_time = self.query_client.fetch_time()
            current_time = datetime.fromtimestamp(server_time/1000, self.timezone)
            
            # 1. 基础设置
            print("\n>>> 基础参数设置")
            coin = get_valid_input("请输入要买的币种 (例如 BTC): ", str).upper()
            symbol = f"{coin}/USDT"  # 自动添加USDT交易对
            total_usdt = get_valid_input("请输入买入金额(USDT): ", float)
            
            # 2. 价格保护设置
            print("\n>>> 价格保护设置")
            print("价格保护机制说明:")
            print("1. 最高接受单价: 绝对价格上限，无论如何都不会超过此价格")
            print("2. 开盘价倍数: 相对于第一档价格的最大倍数")
            
            max_price = get_valid_input("设置最高接受单价 (USDT): ", float)
            price_multiplier = get_valid_input("设置开盘价倍数 (例如 3 表示最高接受开盘价的3倍): ", float)
            
            # 3. 执行参数设置
            print("\n>>> 执行参数设置")
            concurrent_orders = get_valid_input("并发订单数量 (建议1-5): ", int)
            advance_time = 100  # 默认提前100ms
            
            # 4. 止盈止损设置
            print("\n>>> 止盈止损设置")
            print("请输入止损百分比 (例如: 输入5表示5%)")
            stop_loss_percent = get_valid_input("止损百分比: ", float)
            stop_loss = stop_loss_percent / 100  # 转换为小数
            
            # 设置三档止盈
            print("\n设置三档止盈:")
            sell_strategy = []
            total_sell_percent = 0  # 记录总卖出比例
            
            for i in range(3):
                # 检查剩余可卖出比例
                remaining_percent = 100 - total_sell_percent
                if remaining_percent <= 0:
                    print("\n已设置完所有卖出比例")
                    break
                    
                print(f"\n第{i+1}档止盈设置:")
                print(f"当前已设置卖出比例: {total_sell_percent}%")
                print(f"剩余可设置比例: {remaining_percent}%")
                
                print(f"请输入涨幅百分比 (例如: 输入20表示20%)")
                profit_percent = get_valid_input(f"涨幅百分比: ", float)
                print(f"请输入卖出百分比 (最多可设置 {remaining_percent}%)")
                while True:
                    sell_percent = get_valid_input(f"卖出百分比: ", float)
                    if sell_percent <= 0:
                        print("卖出比例必须大于0%")
                        continue
                    if sell_percent > remaining_percent:
                        print(f"错误: 超出可设置比例，最多还可设置 {remaining_percent}%")
                        continue
                    break
                
                # 转换为小数
                profit_ratio = profit_percent / 100
                sell_ratio = sell_percent / 100
                
                sell_strategy.append((profit_ratio, sell_ratio))
                total_sell_percent += sell_percent
                
                # 显示当前设置情况
                print("\n当前止盈设置:")
                for idx, (p, a) in enumerate(sell_strategy):
                    print(f"- 第{idx+1}档: 涨幅{p*100:.0f}% 卖出{a*100:.0f}%")
                print(f"总计已设置: {total_sell_percent}%")

            # 5. 时间设置
            print("\n>>> 开盘时间设置")
            print("请输入开盘时间 (东八区/北京时间)")
            
            # 获取当前服务器时间作为参考
            server_time = self.query_client.fetch_time()
            current_time = datetime.fromtimestamp(server_time/1000, self.timezone)
            print(f"\n当前币安服务器时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
            
            while True:
                try:
                    year = get_valid_input("年 (例如 2024): ", int)
                    if year < 2024 or year > 2100:
                        print("年份无效，请输入2024-2100之间的年份")
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
                    
                    # 创建开盘时间
                    opening_time = self.timezone.localize(datetime(year, month, day, hour, minute, second))
                    
                    # 检查是否早于当前时间
                    if opening_time <= current_time:
                        print("错误: 开盘时间不能早于当前时间")
                        continue
                    
                    # 显示时间信息
                    time_diff = opening_time - current_time
                    hours = time_diff.total_seconds() / 3600
                    
                    print(f"\n=== 时间信息 ===")
                    print(f"设置开盘时间: {opening_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
                    print(f"当前服务器时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
                    print(f"距离开盘还有: {hours:.1f}小时")
                    
                    confirm = input("\n确认使用这个开盘时间? (yes/no): ")
                    if confirm.lower() == 'yes':
                        self.opening_time = opening_time
                        break
                    
                except ValueError as e:
                    print(f"时间格式无效，请重新输入")
                    continue

            # 6. 应用所有设置
            self.setup_trading_pair(symbol, total_usdt)
            self.max_price_limit = max_price
            self.price_multiplier = price_multiplier
            self.concurrent_orders = concurrent_orders
            self.advance_time = advance_time
            self.stop_loss = stop_loss
            self.sell_strategy = sell_strategy
            
            # 保存策略
            if self.save_strategy():
                print("\n策略已保存到配置文件")
            else:
                print("\n警告: 策略保存失败")
            
            return True
            
        except Exception as e:
            logger.error(f"设置抢购策略失败: {str(e)}")
            return False

    def update_balance(self):
        """更新账户余额"""
        try:
            balance = self.trade_client.fetch_balance()
            self.balance = float(balance['USDT']['free'])
        except Exception as e:
            logger.error(f"更新余额失败: {str(e)}")

    def _init_clients(self) -> bool:
        """初始化交易和查询客户端"""
        try:
            # 1. 检查API密钥
            if not self.trade_client or not self.query_client:
                logger.error("API客户端未初始化，请先设置API密钥")
                return False

            # 2. 测试交易API连接
            try:
                balance = self.trade_client.fetch_balance()
                self.balance = balance.get('USDT', {}).get('free', 0)
                logger.info(f"交易API连接正常，USDT余额: {self.balance}")
            except Exception as e:
                logger.error(f"交易API测试失败: {str(e)}")
                return False

            # 3. 测试查询API连接
            try:
                start_time = time.time()
                server_time = self.query_client.fetch_time()
                latency = (time.time() - start_time) * 1000
                logger.info(f"查询API连接正常，延迟: {latency:.2f}ms")
            except Exception as e:
                logger.error(f"查询API测试失败: {str(e)}")
                return False

            # 4. 检查交易对是否存在
            try:
                ticker = self.query_client.fetch_ticker(self.symbol)
                logger.info(f"交易对 {self.symbol} 当前价格: {ticker['last']}")
            except Exception as e:
                logger.error(f"交易对 {self.symbol} 不可用: {str(e)}")
                return False

            # 5. 检查账户权限
            try:
                permissions = balance.get('info', {}).get('permissions', [])
                if 'SPOT' not in permissions:
                    logger.error("API缺少现货交易权限")
                    return False
                logger.info(f"账户权限: {', '.join(permissions)}")
            except Exception as e:
                logger.error(f"检查账户权限失败: {str(e)}")
                return False

            # 6. 优化网络连接
            try:
                # 设置重试策略
                retry_strategy = Retry(
                    total=3,
                    backoff_factor=0.5,
                    status_forcelist=[429, 500, 502, 503, 504]
                )

                # 创建连接适配器
                adapter = HTTPAdapter(
                    pool_connections=20,
                    pool_maxsize=20,
                    max_retries=retry_strategy,
                    pool_block=False
                )

                # 应用到两个客户端
                if hasattr(self.trade_client, 'session'):
                    self.trade_client.session.mount('https://', adapter)
                if hasattr(self.query_client, 'session'):
                    self.query_client.session.mount('https://', adapter)

                logger.info("网络连接已优化")
            except Exception as e:
                logger.warning(f"网络优化失败: {str(e)}")
                # 网络优化失败不影响主功能，继续执行

            logger.info("客户端初始化完成")
            return True

        except Exception as e:
            logger.error(f"初始化客户端失败: {str(e)}")
            return False

    async def measure_network_stats(self, samples: int = 5) -> Optional[Dict[str, float]]:
        """测量网络状态"""
        try:
            latencies = []
            offsets = []
            
            for _ in range(samples):
                start_time = time.time() * 1000
                server_time = await self.query_client.fetch_time()
                end_time = time.time() * 1000
                
                latency = end_time - start_time
                offset = server_time - start_time
                
                latencies.append(latency)
                offsets.append(offset)
                
                await asyncio.sleep(0.1)  # 100ms间隔
                
            avg_latency = statistics.mean(latencies)
            jitter = statistics.stdev(latencies) if len(latencies) > 1 else 0
            avg_offset = statistics.mean(offsets)
            
            return {
                'latency': avg_latency,
                'jitter': jitter,
                'offset': avg_offset
            }
            
        except Exception as e:
            logger.error(f"网络状态测量失败: {str(e)}")
            return None

    def _filter_outliers(self, data: List[float]) -> List[float]:
        """过滤异常值"""
        if len(data) < 4:
            return data
            
        # 计算四分位数
        sorted_data = sorted(data)
        q1 = sorted_data[len(sorted_data)//4]
        q3 = sorted_data[3*len(sorted_data)//4]
        iqr = q3 - q1
        
        # 过滤异常值
        return [x for x in data if q1 - 1.5*iqr <= x <= q3 + 1.5*iqr]

class ErrorHandler:
    """统一错误处理"""
    def __init__(self):
        self._error_counts = defaultdict(int)
        self._last_errors = defaultdict(list)
        self._max_retries = 3
        self._backoff_factor = 0.1
        self._lock = asyncio.Lock()
        
        # 错误恢复策略
        self._recovery_strategies = {
            NetworkError: self._handle_network_error,
            TimeError: self._handle_time_error,
            MarketError: self._handle_market_error,
            ExecutionError: self._handle_execution_error
        }
    
    async def handle_error(self, error: Exception, context: str, retry_func=None) -> bool:
        """处理错误"""
        async with self._lock:
            try:
                # 1. 记录错误
                error_key = f"{context}:{type(error).__name__}"
                self._error_counts[error_key] += 1
                self._last_errors[error_key].append(time.time())
                
                # 2. 清理旧记录
                self._cleanup_old_errors(error_key)
                
                # 3. 检查是否严重错误
                if self._is_critical_error(error_key):
                    logger.critical(f"严重错误 - {context}: {str(error)}")
                    return False
                
                # 4. 执行对应的恢复策略
                error_type = type(error)
                if error_type in self._recovery_strategies:
                    return await self._recovery_strategies[error_type](error, retry_func)
                
                # 5. 默认重试策略
                if retry_func:
                    return await self._retry_with_backoff(retry_func)
                
                return False
                
            except Exception as e:
                logger.error(f"错误处理失败: {str(e)}")
                return False
    
    async def _handle_network_error(self, error: NetworkError, retry_func) -> bool:
        """处理网络错误"""
        try:
            # 1. 检查网络连接
            if not await self._check_network():
                return False
            
            # 2. 重新初始化连接
            await self._reinit_connection()
            
            # 3. 执行重试
            if retry_func:
                return await self._retry_with_backoff(retry_func)
            
            return False
        except Exception as e:
            logger.error(f"网络错误恢复失败: {str(e)}")
            return False
    
    async def _handle_time_error(self, error: TimeError, retry_func) -> bool:
        """处理时间同步错误"""
        try:
            # 1. 重新同步时间
            if not await self._resync_time():
                return False
            
            # 2. 执行重试
            if retry_func:
                return await self._retry_with_backoff(retry_func)
            
            return False
        except Exception as e:
            logger.error(f"时间同步错误恢复失败: {str(e)}")
            return False
    
    async def _handle_market_error(self, error: MarketError, retry_func) -> bool:
        """处理市场错误"""
        try:
            # 1. 检查市场状态
            if not await self._check_market_status():
                return False
            
            # 2. 更新市场数据
            await self._update_market_data()
            
            # 3. 执行重试
            if retry_func:
                return await self._retry_with_backoff(retry_func)
            
            return False
        except Exception as e:
            logger.error(f"市场错误恢复失败: {str(e)}")
            return False
    
    async def _handle_execution_error(self, error: ExecutionError, retry_func) -> bool:
        """处理执行错误"""
        try:
            # 1. 检查订单状态
            if not await self._check_order_status():
                return False
            
            # 2. 清理失败订单
            await self._cleanup_failed_orders()
            
            # 3. 执行重试
            if retry_func:
                return await self._retry_with_backoff(retry_func)
            
            return False
        except Exception as e:
            logger.error(f"执行错误恢复失败: {str(e)}")
            return False
    
    def _cleanup_old_errors(self, error_key: str):
        """清理旧的错误记录"""
        current_time = time.time()
        self._last_errors[error_key] = [
            t for t in self._last_errors[error_key]
            if current_time - t < 300  # 保留5分钟内的错误
        ]
    
    def _is_critical_error(self, error_key: str) -> bool:
        """判断是否是严重错误"""
        recent_errors = [
            t for t in self._last_errors[error_key]
            if time.time() - t < 60  # 1分钟内的错误
        ]
        return len(recent_errors) >= 5  # 1分钟内5次以上同类错误
    
    async def _retry_with_backoff(self, func) -> bool:
        """带退避的重试机制"""
        retry_count = 0
        while retry_count < self._max_retries:
            try:
                await asyncio.sleep(self._backoff_factor * (2 ** retry_count))
                if asyncio.iscoroutinefunction(func):
                    result = await func()
                else:
                    result = func()
                return True if result else False
            except Exception:
                retry_count += 1
        return False

def print_menu(clear=True):
    """打印主菜单"""
    # 删除清屏代码,只保留菜单打印
    print("\n=== 币安现货抢币工具 ===")
    print("1. 设置API密钥")
    print("2. 抢开盘策略设置") 
    print("3. 查看当前策略")
    print("4. 开始抢购")
    print("0. 退出")
    print("=====================")

def get_valid_input(prompt: str, input_type: Type = str, allow_empty: bool = False) -> Any:
    """获取有效输入
    Args:
        prompt: 提示信息
        input_type: 输入类型
        allow_empty: 是否允许空输入
    Returns:
        Any: 转换后的输入值
    """
    while True:
        try:
            value = input(prompt).strip()
            if allow_empty and not value:
                return None
            if input_type == str:
                if value:
                    return value
                raise ValueError("输入不能为空")
            return input_type(value)
        except ValueError:
            print(f"输入无效，请输入{input_type.__name__}类型的值")

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
    try:
        logger.info("开始设置抢购策略...")
        
        # 1. 基础设置
        print("\n>>> 基础参数设置")
        coin = get_valid_input("请输入要买的币种 (例如 BTC): ", str).upper()
        symbol = f"{coin}/USDT"  # 自动添加USDT交易对
        total_usdt = get_valid_input("请输入买入金额(USDT): ", float)
        
        # 添加开盘时间设置
        print("\n>>> 开盘时间设置")
        print("请输入开盘时间 (东八区/北京时间)")
        
        # 先同步时间显示当前时间作为参考
        server_time = sniper.get_server_time()
        print(f"\n当前币安服务器时间: {server_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
        
        while True:
            try:
                # 只做基本的格式验证
                year = get_valid_input("年 (例如 2025): ", int)
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
                    sniper.opening_time = opening_time
                    break
                else:
                    print("请重新设置开盘时间")
                    continue
                    
            except ValueError as e:
                print(f"时间格式无效，请重新输入: {str(e)}")
                continue
        
        # 设置其他参数...
        # ... (其他代码保持不变)
        
        return True
        
    except Exception as e:
        logger.error(f"设置抢购策略失败: {str(e)}")
        return False

def main():
    """主程序入口"""
    try:
        # 检查依赖
        if not check_dependencies():
            print("依赖检查失败，请安装必要的依赖包")
            return

        # 初始化配置管理器
        config = ConfigManager()
        
        # 初始化币安抢币工具
        sniper = BinanceSniper(config)

        while True:
            print_menu()
            choice = input("请选择操作 (0-4): ").strip()

            if choice == '0':
                print("感谢使用，再见!")
                break
            elif choice == '1':
                sniper.setup_api_keys()
            elif choice == '2':
                sniper.setup_snipe_strategy()
            elif choice == '3':
                sniper.print_strategy()
            elif choice == '4':
                # 创建新的事件循环来执行异步操作
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(sniper.prepare_and_snipe())
                except Exception as e:
                    print(f"执行抢购失败: {str(e)}")
                finally:
                    loop.close()
            else:
                print("无效的选择，请重新输入")

    except KeyboardInterrupt:
        print("\n程序已被用户中断")
    except Exception as e:
        print(f"程序运行出错: {str(e)}")
        logger.error(f"程序运行出错: {str(e)}", exc_info=True)

if __name__ == '__main__':
    main()

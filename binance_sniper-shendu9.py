#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Binance Sniper - 币安现货抢币工具
"""

# =============================== 
# 模块：导入和全局设置
# ===============================

# 标准库
import os
import sys
import json
import time
import logging
import asyncio
import threading
import signal
import statistics
import gc
import contextlib
import weakref
import inspect
import socket
import subprocess
import random
import base64
import uuid
from collections import deque, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from functools import wraps, partial
from logging.handlers import RotatingFileHandler
from threading import Lock, Event
from types import TracebackType
from typing import (
    Dict, Optional, List, Tuple, TypeVar, Union, Any, 
    Type, Protocol, runtime_checkable,
    AsyncIterator, Callable, Awaitable, AsyncGenerator,
    NoReturn, Iterator, Mapping, cast, Final,
    Coroutine, Set, DefaultDict
)

# 第三方库
# 网络相关
import aiohttp
import requests
import websocket
import websockets
from aiohttp import ClientSession, ClientTimeout, TCPConnector
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout
from urllib3.util.retry import Retry
import aiofiles  # 这个包缺失

# 加密和安全
import hmac
import hashlib
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_pem_private_key

# 其他工具
import ccxt
import pytz
import netifaces
import psutil
import prometheus_client as prom
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

# 配置和工具
import configparser
from decimal import Decimal
import aiofiles

# 获取当前脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 设置相对于脚本目录的路径
LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
CONFIG_DIR = os.path.join(SCRIPT_DIR, 'config')

# 确保目录存在
try:
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(CONFIG_DIR, exist_ok=True)
except Exception as e:
    print(f"初始化目录失败: {str(e)}")
    sys.exit(1)

# =============================== 
# 模块：目录和文件操作
# ===============================

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
    """精确等待器"""
    def __init__(self):
        self.last_sleep_error = 0
        self._min_sleep = 0.000001
        self.logger = logging.getLogger(__name__)
        
    async def wait_until(self, target_time: float):
        """等待到指定时间"""
        while True:
            current = time.time() * 1000
            if current >= target_time:
                break
                
            remaining = target_time - current
            if remaining > 1:
                await asyncio.sleep(remaining / 2000)
            else:
                while time.time() * 1000 < target_time:
                    await asyncio.sleep(0)
    
    async def sleep(self, duration: float):
        """精确休眠指定时间"""
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

# =============================== 
# 模块：IP 管理器
# ===============================
# 在 SniperError 相关类之后，PreciseWaiter 类之前添加 IPManager 类
class IPManager:
    """IP管理器 - 处理多IP轮换、监控和故障切换"""
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.interface = self._get_default_interface()
        self.ips = self._get_interface_ips()
        
        # IP状态跟踪
        self.ip_stats = {}
        for ip in self.ips:
            self.ip_stats[ip] = {
                'requests': 0,          # 请求计数
                'errors': 0,            # 错误计数
                'last_request': 0,      # 最后请求时间
                'latency': float('inf'), # 当前延迟
                'ws_connected': False,   # WebSocket连接状态
                'last_ws_msg': 0,       # 最后WS消息时间
            }
        
        self.lock = asyncio.Lock()
        self.data_pool = DataPool()         # 创建数据池实例
        self.scheduler = RequestScheduler(data_pool=self.data_pool)  # 传入数据池

    def _get_default_interface(self) -> str:
        """获取默认网络接口"""
        try:
            gateways = netifaces.gateways()
            default = gateways.get('default', {}).get(netifaces.AF_INET)
            if default:
                return default[1]
            for iface in netifaces.interfaces():
                if iface != 'lo':
                    return iface
        except Exception as e:
            self.logger.error(f"获取网络接口失败: {e}")
        return "eth0"

    def _get_interface_ips(self) -> List[str]:
        """获取接口上配置的所有IPv4地址"""
        ips = []
        try:
            addrs = netifaces.ifaddresses(self.interface)
            if netifaces.AF_INET in addrs:
                for addr in addrs[netifaces.AF_INET]:
                    if 'addr' in addr and addr['addr'] != '127.0.0.1':
                        ips.append(addr['addr'])
            self.logger.info(f"检测到IP地址: {ips}")
        except Exception as e:
            self.logger.error(f"获取IP地址失败: {e}")
        return ips

    async def update_ip_status(self, ip: str, latency: float, error: bool = False):
        """更新IP状态"""
        async with self.lock:
            stats = self.ip_stats[ip]
            stats['requests'] += 1
            stats['last_request'] = time.time() * 1000
            stats['latency'] = latency
            if error:
                stats['errors'] += 1

# =============================== 
# 模块：数据缓存池
# ===============================
class DataPool:
    """市场数据缓存池 - 管理所有市场数据"""
    def __init__(self):
        self.lock = asyncio.Lock()
        
        # 价格数据缓存
        self.price_cache = {
            'ws_data': deque(maxlen=1000),    # WebSocket价格数据
            'rest_data': deque(maxlen=1000),   # REST价格数据
            'last_update': 0,                  # 最后更新时间
        }
        
        # 深度数据缓存
        self.depth_cache = {
            'ws_data': {
                'bids': {},  # 买单深度
                'asks': {}   # 卖单深度
            },
            'rest_data': {
                'bids': {},
                'asks': {}
            },
            'last_update': 0
        }
        
        # 交易数据缓存
        self.trade_cache = {
            'recent_trades': deque(maxlen=1000),  # 最近成交
            'last_update': 0
        }
        
        # 统计数据
        self.stats = {
            'ws_messages': 0,
            'rest_updates': 0,
            'cache_hits': 0,
            'cache_misses': 0
        }

    async def update_price(self, data: dict, source: str = 'ws'):
        """更新价格数据"""
        async with self.lock:
            if source == 'ws':
                self.price_cache['ws_data'].append(data)
                self.stats['ws_messages'] += 1
            else:
                self.price_cache['rest_data'].append(data)
                self.stats['rest_updates'] += 1
            
            self.price_cache['last_update'] = time.time() * 1000

    async def update_depth(self, data: dict, source: str = 'ws'):
        """更新深度数据"""
        async with self.lock:
            target = self.depth_cache['ws_data' if source == 'ws' else 'rest_data']
            
            # 更新买单深度
            for price, amount in data.get('bids', []):
                if float(amount) > 0:
                    target['bids'][price] = amount
                else:
                    target['bids'].pop(price, None)
                    
            # 更新卖单深度
            for price, amount in data.get('asks', []):
                if float(amount) > 0:
                    target['asks'][price] = amount
                else:
                    target['asks'].pop(price, None)
                    
            self.depth_cache['last_update'] = time.time() * 1000
            
            if source == 'ws':
                self.stats['ws_messages'] += 1
            else:
                self.stats['rest_updates'] += 1

    async def update_trades(self, trades: list):
        """更新最近成交"""
        async with self.lock:
            for trade in trades:
                self.trade_cache['recent_trades'].append(trade)
            self.trade_cache['last_update'] = time.time() * 1000
            self.stats['rest_updates'] += 1

    def get_latest_price(self) -> Optional[float]:
        """获取最新价格"""
        if self.price_cache['ws_data']:
            self.stats['cache_hits'] += 1
            return float(self.price_cache['ws_data'][-1]['price'])
        elif self.price_cache['rest_data']:
            self.stats['cache_hits'] += 1
            return float(self.price_cache['rest_data'][-1]['price'])
        self.stats['cache_misses'] += 1
        return None

    def get_depth(self, level: int = 20) -> dict:
        """获取市场深度"""
        bids = sorted(self.depth_cache['ws_data']['bids'].items(), 
                     key=lambda x: float(x[0]), reverse=True)[:level]
        asks = sorted(self.depth_cache['ws_data']['asks'].items(), 
                     key=lambda x: float(x[0]))[:level]
        
        return {
            'bids': bids,
            'asks': asks,
            'timestamp': self.depth_cache['last_update']
        }

    def get_recent_trades(self, limit: int = 100) -> list:
        """获取最近成交"""
        return list(self.trade_cache['recent_trades'])[-limit:]

    def get_pool_stats(self) -> dict:
        """获取数据池统计信息"""
        return {
            'ws_messages': self.stats['ws_messages'],
            'rest_updates': self.stats['rest_updates'],
            'cache_hits': self.stats['cache_hits'],
            'cache_misses': self.stats['cache_misses'],
            'price_cache_size': len(self.price_cache['ws_data']),
            'depth_cache_size': len(self.depth_cache['ws_data']['bids']) + 
                              len(self.depth_cache['ws_data']['asks']),
            'trade_cache_size': len(self.trade_cache['recent_trades'])
        }

# =============================== 
# 模块：请求调度器
# ===============================
class RequestScheduler:
    """请求调度器 - 实现精确的请求控制"""
    def __init__(self, data_pool: DataPool = None):
        self.data_pool = data_pool
        self.cycle_ms = 54        # 54毫秒完整周期
        self.ip_window_ms = 18    # 每IP 18毫秒窗口
        self.last_request_time = 0
        self.response_queue = asyncio.Queue()
        self.running = True
        
        # 请求统计
        self.request_stats = {
            'total': 0,
            'success': 0,
            'errors': 0,
            'latencies': []
        }

    async def start(self):
        """启动调度器"""
        asyncio.create_task(self._request_sender())
        asyncio.create_task(self._response_handler())

    async def _request_sender(self):
        """固定频率发送请求"""
        while self.running:
            current_ms = time.time() * 1000
            
            if current_ms - self.last_request_time >= self.cycle_ms:
                # 获取当前可用IP
                active_ip = self.get_active_ip(current_ms)
                if active_ip:
                    # 异步发送请求
                    asyncio.create_task(self.send_request(active_ip))
                    self.last_request_time = current_ms
            
            await asyncio.sleep(0.001)  # 1ms检查间隔

    async def _response_handler(self):
        """处理响应数据"""
        while self.running:
            try:
                response = await self.response_queue.get()
                asyncio.create_task(self.process_response(response))
            except Exception as e:
                logger.error(f"响应处理失败: {str(e)}")
            await asyncio.sleep(0)

    def get_active_ip(self, current_ms: float) -> Optional[str]:
        """获取当前时间窗口的活动IP"""
        cycle_position = int(current_ms % self.cycle_ms)
        window = cycle_position // self.ip_window_ms
        
        if window < len(self.ips):
            return self.ips[window]
        return None

    async def send_request(self, ip: str):
        """发送请求(不等待响应)"""
        try:
            start_time = time.time() * 1000
            
            # 创建请求任务
            request_task = asyncio.create_task(
                self._do_request(ip)
            )
            
            # 设置超时
            try:
                response = await asyncio.wait_for(
                    request_task,
                    timeout=0.5  # 500ms超时
                )
                
                # 计算延迟
                latency = time.time() * 1000 - start_time
                
                # 更新统计
                self.request_stats['total'] += 1
                self.request_stats['success'] += 1
                self.request_stats['latencies'].append(latency)
                
                # 放入响应队列
                await self.response_queue.put({
                    'ip': ip,
                    'data': response,
                    'latency': latency,
                    'timestamp': start_time
                })
                
            except asyncio.TimeoutError:
                self.request_stats['errors'] += 1
                logger.warning(f"请求超时 (IP: {ip})")
                
        except Exception as e:
            self.request_stats['errors'] += 1
            logger.error(f"请求失败 (IP: {ip}): {str(e)}")

    async def process_response(self, response: dict):
        """处理响应数据"""
        try:
            # 提取数据
            ip = response['ip']
            data = response['data']
            latency = response['latency']
            
            # 更新IP状态
            await self.ip_manager.update_ip_status(
                ip=ip,
                latency=latency,
                error=False
            )
            
            # 更新数据池
            await self.data_pool.update(data)
            
        except Exception as e:
            logger.error(f"响应处理失败: {str(e)}")

    async def _do_request(self, ip: str):
        """执行实际的请求"""
        try:
            # 获取市场数据
            response = await self.query_client.fetch_order_book(
                self.symbol,
                limit=5,
                params={'ip': ip}
            )
            
            return response
            
        except Exception as e:
            logger.error(f"请求执行失败 (IP: {ip}): {str(e)}")
            raise e



# =============================== 
# 模块：日志系统配置
# ===============================
def setup_logger():
    """设置日志记录器"""
    # 检查是否已经存在logger
    logger = logging.getLogger('BinanceSniper')
    if logger.handlers:  # 如果已经有handler，直接返回
        return logger
        
    logger.setLevel(logging.DEBUG)

    # 创建格式化器
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # 控制台处理器
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件处理器
    log_file = os.path.join(LOG_DIR, 'binance_sniper.log')
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

def format_number(num: float, decimals: int = 2) -> str:
    """格式化数字，添加千位分隔符"""
    return f"{num:,.{decimals}f}"

def mask_api_key(key: str) -> str:
    """遮蔽API密钥"""
    if not key:
        return "未设置"
    return f"{key[:6]}{'*' * (len(key)-10)}{key[-4:]}"

# 创建logger实例
logger = setup_logger()

# =============================== 
# 模块：全局 Logger 实例
# ===============================
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

# =============================== 
# 模块：执行策略管理器
# ===============================
class TimeSync:
    """时间同步管理器"""
    def __init__(self):
        self.network_latency = None
        self.time_offset = None
        self.last_sync_time = 0
        self.sync_interval = 30  # 改为30秒同步一次
        self.min_samples = 5
        self.max_samples = 10
        self._server_time = 0
        
    def _filter_outliers(self, measurements: List[Dict]) -> List[Dict]:
        """过滤异常值
        Args:
            measurements: 包含测量数据的字典列表
        Returns:
            List[Dict]: 过滤后的测量结果
        """
        if len(measurements) < 3:
            return measurements
            
        # 按延迟排序
        sorted_measurements = sorted(measurements, key=lambda x: x['latency'])
        
        # 计算四分位数
        n = len(sorted_measurements)
        q1_idx = n // 4
        q3_idx = (n * 3) // 4
        
        q1 = sorted_measurements[q1_idx]['latency']
        q3 = sorted_measurements[q3_idx]['latency']
        iqr = q3 - q1
        
        # 定义异常值界限
        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        
        # 过滤异常值
        filtered = [m for m in sorted_measurements 
                   if lower_bound <= m['latency'] <= upper_bound]
        
        return filtered if filtered else sorted_measurements[:3]

    async def sync(self) -> Optional[Tuple[float, float, int]]:
        """异步同步时间，获取网络延迟和服务器时间"""
        try:
            measurements = []
            logger.info("开始时间同步测量...")
            
            # 1. 快速预检
            initial_time = await self.get_server_time()
            if not initial_time:
                logger.error("预检失败：无法连接到币安服务器")
                return None
                
            # 2. 批量收集样本
            successful_samples = 0
            for i in range(self.max_samples):
                try:
                    # 精确计时
                    start = time.time() * 1000  # 改用 time.time() 获取当前时间戳
                    server_time = await self.get_server_time()
                    end = time.time() * 1000
                    
                    if not server_time:
                        logger.warning(f"样本{i+1}/{self.max_samples}: 获取服务器时间失败")
                        continue
                    
                    # 计算延迟
                    rtt = end - start  # 直接计算往返时间
                    latency = rtt / 2
                    
                    # 计算时间偏移 (修正后的逻辑)
                    time_offset = server_time - (start + latency)  # 服务器时间与本地时间的差值
                    
                    measurements.append({
                        'server_time': server_time,
                        'latency': latency,
                        'rtt': rtt,
                        'offset': time_offset
                    })
                    
                    successful_samples += 1
                    logger.debug(
                        f"样本{i+1}: 延迟={latency:.2f}ms, "
                        f"RTT={rtt:.2f}ms, "
                        f"偏移={time_offset:.2f}ms"
                    )
                    
                    # 如果已经收集到足够的样本，可以提前结束
                    if successful_samples >= self.min_samples:
                        await asyncio.sleep(0.1)  # 最后一次等待确保数据稳定
                        break
                        
                    await asyncio.sleep(0.1)  # 采样间隔
                    
                except Exception as e:
                    logger.error(f"样本{i+1}采集异常: {str(e)}")
                    continue
            
            # 3. 样本分析
            if len(measurements) < self.min_samples:
                logger.error(
                    f"样本不足: 获得{len(measurements)}个, "
                    f"需要{self.min_samples}个"
                )
                return None
                
            # 4. 过滤异常值
            filtered = self._filter_outliers(measurements)
            logger.info(
                f"样本统计: 总计={len(measurements)}, "
                f"有效={len(filtered)}, "
                f"过滤={len(measurements)-len(filtered)}"
            )
            
            if len(filtered) < self.min_samples:
                logger.error(f"过滤后样本不足: {len(filtered)}/{self.min_samples}")
                return None
            
            # 5. 计算最终结果
            self.network_latency = statistics.median(m['latency'] for m in filtered)
            self._server_time = statistics.median(m['server_time'] for m in filtered)
            time_offset = statistics.median(m['offset'] for m in filtered)
            self.last_sync_time = self._server_time
            
            logger.info(
                f"同步完成: 延迟={self.network_latency:.2f}ms, "
                f"偏移={time_offset:.2f}ms, "
                f"样本数={len(filtered)}"
            )
            
            return (self.network_latency, time_offset, int(self._server_time))
            
        except Exception as e:
            logger.error(f"时间同步过程异常: {str(e)}")
            return None

    async def get_server_time(self) -> Optional[int]:
        """异步获取服务器时间"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.binance.com/api/v3/time', timeout=2) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result['serverTime']
        except Exception as e:
            logger.error(f"获取服务器时间失败: {str(e)}")
            return None

    def get_current_time(self) -> int:
        """获取当前服务器时间(毫秒)"""
        if not self._server_time:
            return 0
        # 基于最后同步的服务器时间计算当前时间
        elapsed = time.perf_counter_ns() / 1e6  # 转换为毫秒
        return int(self._server_time + elapsed)
    
    def needs_sync(self) -> bool:
        """检查是否需要同步"""
        current = self.get_current_time()
        return (
            self._server_time == 0 or
            current - self.last_sync_time > self.sync_interval * 1000  # 转换为毫秒
        )

class MarketDepthAnalyzer:
    """市场深度分析器"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
    async def analyze_depth(self, orderbook: dict) -> dict:
        try:
            # 4. 优化请求
            async with self.session.get(
                f"{self.base_url}{self.depth_path}",
                params=params,
                skip_auto_headers={'Accept-Encoding'},  # 跳过自动添加的头
                ssl=False,  # 禁用SSL验证
                allow_redirects=False  # 禁用重定向
            ) as response:
                if response.status != 200:
                    raise MarketError(f"获取深度数据失败: {response.status}")
                
                # 5. 优化数据处理
                orderbook = await response.json(content_type=None)  # 跳过content-type检查
                
                if not orderbook or 'asks' not in orderbook:
                    return None
                
                # 6. 使用列表推导优化数据处理
                asks = [[float(p), float(q)] for p, q in orderbook['asks'][:5]]
                bids = [[float(p), float(q)] for p, q in orderbook['bids'][:5]]
                
                best_ask = asks[0][0]
                best_bid = bids[0][0] if bids else 0
                
                # 7. 使用sum+generator优化计算
                ask_volume = sum(ask[1] for ask in asks)
                bid_volume = sum(bid[1] for bid in bids)
                
                # 8. 优化价格差计算
                price_gaps = [asks[i+1][0] - asks[i][0] for i in range(len(asks)-1)]
                max_gap = max(price_gaps, default=0)
                avg_gap = sum(price_gaps) / len(price_gaps) if price_gaps else 0
                
                return {
                    'ask': best_ask,
                    'bid': best_bid,
                    'spread': best_ask - best_bid,
                    'spread_ratio': (best_ask - best_bid) / best_bid if best_bid else 0,
                    'ask_volume': ask_volume,
                    'bid_volume': bid_volume,
                    'max_gap': max_gap,
                    'avg_gap': avg_gap,
                    'timestamp': orderbook.get('T', int(time.time() * 1000))
                }
                
        except Exception as e:
            self.logger.error(f"深度分析失败: {str(e)}")
            return None

    async def close(self):
        """关闭资源"""
        if self.session:
            await self.session.close()
            self.session = None
        if self._connector:
            await self._connector.close()
            self._connector = None

    def is_depth_normal(self, depth_data: Dict) -> bool:
        """检查深度是否正常"""
        if not depth_data:
            return False
            
        try:
            if depth_data['spread_ratio'] > 0.05:
                logger.warning(f"价差过大: {depth_data['spread_ratio']*100:.2f}%")
                return False
                
            if depth_data['max_gap'] > depth_data['avg_gap'] * 3:
                logger.warning("检测到深度断层")
                return False
                
            min_volume = 10
            if depth_data['ask_volume'] < min_volume:
                logger.warning(f"卖单深度不足: {depth_data['ask_volume']:.2f}")
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"深度检查失败: {str(e)}")
            return False

class OrderExecutor:
    """订单执行器"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self._order_buffer = bytearray(1024 * 10)  # 10KB缓冲区
        self._executor = ThreadPoolExecutor(max_workers=4)
        
    async def execute_orders(self,
                           client,
                           symbol: str,
                           amount: float,
                           price: float,
                           concurrent_orders: int = 3) -> Optional[Dict]:
        """执行订单"""
        try:
            # 基础参数
            base_params = {
                'symbol': symbol,
                'side': 'BUY',
                'type': 'LIMIT',
                'timeInForce': 'IOC',
                'quantity': amount / concurrent_orders,
                'price': price
            }
            
            # 创建并发任务
            tasks = []
            for _ in range(concurrent_orders):
                task = self._execute_single_order(client, base_params.copy())
                tasks.append(task)
                
            # 等待所有任务完成
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果
            successful_orders = []
            for result in results:
                if isinstance(result, dict) and result.get('status') == 'closed':
                    successful_orders.append(result)
                    
            return successful_orders[0] if successful_orders else None
            
        except Exception as e:
            self.logger.error(f"订单执行失败: {str(e)}")
            return None
            
    async def _execute_single_order(self, client, params: Dict) -> Optional[Dict]:
        """执行单个订单"""
        try:
            # 使用线程池执行同步API调用
            return await asyncio.get_event_loop().run_in_executor(
                self._executor,
                client.create_order,
                **params
            )
        except Exception as e:
            self.logger.error(f"执行订单失败: {str(e)}")
            return None

class RealTimeMonitor:
    """实时监控系统"""
    def __init__(self):
        self.metrics = {
            'order_latency': [],
            'market_data_latency': [],
            'network_latency': [],
            'success_rate': 0,
            'error_count': 0
        }
        
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
            )
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
    """配置管理类"""
    def __init__(self, config_file: str = 'config.json'):
        self.config_file = config_file
        self.default_config = {
            'trade_api_key': '',    
            'trade_api_secret': '', 
            'query_api_key': '',    
            'query_api_secret': '',
            'trading_pairs': [],  
            'default_amounts': {},  
            'max_attempts': 10,
            'check_interval': 0.2
        }
        self.config = self.load_config()
        self.config_dir = os.path.dirname(config_file)

    def load_config(self) -> Dict[str, Any]:
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return self.default_config.copy()
        except Exception as e:
            print(f"加载配置文件出错: {str(e)}")
            return self.default_config.copy()

    def save_config(self) -> bool:
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            return True
        except Exception as e:
            print(f"保存配置文件出错: {str(e)}")
            return False

    def save(self):
        """兼容旧的保存方法"""
        return self.save_config()

    def set_trade_api_keys(self, api_key: str, api_secret: str):
        """设置交易API密钥"""
        self.config['trade_api_key'] = api_key
        self.config['trade_api_secret'] = api_secret
        self.save_config()
    
    def set_query_api_keys(self, api_key: str, api_secret: str):
        """设置查询API密钥"""
        self.config['query_api_key'] = api_key
        self.config['query_api_secret'] = api_secret
        self.save_config()

    def get_trade_api_keys(self) -> Tuple[str, str]:
        return (
            self.config.get('trade_api_key', ''),
            self.config.get('trade_api_secret', '')
        )

    def get_query_api_keys(self) -> Tuple[str, str]:
        return (
            self.config.get('query_api_key', ''),
            self.config.get('query_api_secret', '')
        )
    
    def has_api_keys(self) -> bool:
        """检查是否已设置API密钥"""
        return all([
            self.config.get('trade_api_key'),
            self.config.get('trade_api_secret'),
            self.config.get('query_api_key'),
            self.config.get('query_api_secret')
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

# 在文件开头添加时区设置
timezone = pytz.timezone('Asia/Shanghai')

def format_local_time(timestamp_ms: int) -> str:
    """将毫秒时间戳转换为本地时间字符串"""
    dt = datetime.fromtimestamp(timestamp_ms/1000, timezone)
    return dt.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

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
        """初始化币安抢币工具"""
        self.config = config
        self.logger = logging.getLogger(__name__)
        self.trade_client = None
        self.query_client = None
        self.timezone = pytz.timezone('Asia/Shanghai')
        
        # 添加 time_sync 初始化
        self.time_sync = TimeSync()
        
        # 添加 market_data 初始化
        self.market_data = {
            'network_latency': 0,
            'time_offset': 0,
            'server_time': 0,
            'current_price': 0,
            'price_24h_change': 0,
            'volume_24h': 0,
            'market_status': '未知'
        }
        
        # 修改这行: 使用 PerformanceAnalyzer 替代 PerformanceTimer
        self.perf = PerformanceAnalyzer()
        
        # 添加网络和时间相关属性初始化
        self.network_latency = 0
        self.server_time_offset = 0
        
        # 初始化其他属性
        self.symbol = None
        self.amount = None
        self.max_price_limit = None
        self.price_multiplier = None
        self.concurrent_orders = None
        self.advance_time = None
        self.stop_loss = None
        self.sell_strategy = None
        self.opening_time = None
        
        # 确保 DataPool 和 OrderProcessor 在初始化时就创建
        self.data_pool = DataPool()
        self.order_processor = OrderProcessor(config, self.data_pool, self)
        
        # 初始化基本组件
        self._init_basic_components()
        
    async def update_market_data(self):
        """更新市场数据"""
        try:
            # 更新价格信息
            if self.query_client:
                try:
                    # 使用异步会话获取数据
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            f'https://api.binance.com/api/v3/ticker/24hr?symbol={self.symbol.replace("/", "")}'
                        ) as response:
                            if response.status == 200:
                                ticker = await response.json()
                                self.market_data.update({
                                    'current_price': float(ticker['lastPrice']),
                                    'price_24h_change': float(ticker['priceChangePercent']),
                                    'volume_24h': float(ticker['quoteVolume']),
                                    'market_status': '正常交易'
                                })
                            else:
                                raise Exception(f"获取行情失败: {response.status}")
                except Exception as e:
                    self.logger.error(f"获取价格信息失败: {str(e)}")
                    self.market_data.update({
                        'market_status': '未知',
                        'current_price': 0,
                        'price_24h_change': 0,
                        'volume_24h': 0
                    })
            
            # 更新网络和时间信息
            time_sync_result = await self.time_sync.sync()
            if time_sync_result:
                self.market_data.update({
                    'network_latency': time_sync_result[0],
                    'time_offset': time_sync_result[1],
                    'server_time': time_sync_result[2]
                })
                
            return True
        except Exception as e:
            self.logger.error(f"更新市场数据失败: {str(e)}")
            return False
        
    async def print_strategy_async(self):
        """代理到 OrderProcessor 的打印方法"""
        try:
            # 更新市场数据
            await self.update_market_data()
            
            # 同步必要的属性到 OrderProcessor
            self.order_processor.query_client = self.query_client
            self.order_processor.trade_client = self.trade_client
            self.order_processor.symbol = self.symbol
            self.order_processor.amount = self.amount
            self.order_processor.max_price_limit = self.max_price_limit
            self.order_processor.price_multiplier = self.price_multiplier
            self.order_processor.concurrent_orders = self.concurrent_orders
            self.order_processor.advance_time = self.advance_time
            self.order_processor.stop_loss = self.stop_loss
            self.order_processor.sell_strategy = self.sell_strategy
            self.order_processor.opening_time = self.opening_time
            
            # 调用 OrderProcessor 的实现
            return await self.order_processor.print_strategy_async()
            
        except Exception as e:
            self.logger.error(f"打印策略失败: {str(e)}")
            return False

    async def load_strategy_async(self) -> bool:
        """代理到 OrderProcessor 的加载策略方法"""
        try:
            result = await self.order_processor.load_strategy_async()
            if result:  # 如果加载成功，同步属性回来
                # 从 OrderProcessor 同步属性到 BinanceSniper
                self.symbol = self.order_processor.symbol
                self.amount = self.order_processor.amount
                self.max_price_limit = self.order_processor.max_price_limit
                self.price_multiplier = self.order_processor.price_multiplier
                self.concurrent_orders = self.order_processor.concurrent_orders
                self.advance_time = self.order_processor.advance_time
                self.stop_loss = self.order_processor.stop_loss
                self.sell_strategy = self.order_processor.sell_strategy
                self.opening_time = self.order_processor.opening_time
            return result
        except Exception as e:
            self.logger.error(f"加载策略失败: {str(e)}")
            return False

    async def _init_snipe(self) -> bool:
        """初始化抢购环境"""
        try:
            # 1. 检查API密钥
            if not await self._check_api_keys():
                return False
                
            # 2. 加载策略配置 - 改为异步
            if not await self.load_strategy_async():  # 原来的 load_strategy() 改为异步
                return False
                
            # 3. 准备IP资源
            if not await self.prepare_ips():
                return False
                
            # 4. 验证IP可用性
            if not await self.validate_ips():
                return False
                
            # 5. 启动数据池
            await self.data_pool.start(self.symbol)
            
            # 6. 同步时间
            if not await self.sync_server_time():
                return False
                
            self.logger.info("初始化完成")
            return True
            
        except Exception as e:
            self.logger.error(f"初始化失败: {str(e)}")
            return False

    async def cleanup(self):
        """清理资源"""
        try:
            # 检查是否已经清理过
            if hasattr(self, '_cleaned') and self._cleaned:
                return
            
            # 停止数据池
            if hasattr(self, 'data_pool'):
                await self.data_pool.stop()
            
            # 清理其他资源...
            
            self.logger.info("资源清理完成")
            self._cleaned = True  # 标记为已清理
        except Exception as e:
            self.logger.error(f"资源清理失败: {str(e)}")

    def __del__(self):
        """析构函数"""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self.cleanup())
        except Exception:
            pass  # 忽略清理过程中的错误

    def _init_basic_components(self):
        """初始化基本组件，包括API客户端和连接池"""
        try:
            # 获取API密钥
            trade_api_key, trade_api_secret = self.config.get_trade_api_keys()
            query_api_key, query_api_secret = self.config.get_query_api_keys()

            # 初始化客户端 (如果密钥存在)
            if trade_api_key and trade_api_secret:
                self.trade_client = self._create_ccxt_client(trade_api_key, trade_api_secret)
                self.query_client = self._create_ccxt_client(query_api_key, query_api_secret)
                self.logger.info("API客户端初始化成功")
            else:
                self.logger.warning("未配置API密钥，交易功能将受限")

            # 初始化连接池
            self._init_connection_pools()

        except Exception as e:
            self.logger.warning(f"初始化基本组件时出错: {str(e)}")

    def _create_ccxt_client(self, api_key, secret):
        """创建ccxt客户端实例"""
        return ccxt.binance({
            'apiKey': api_key,
            'secret': secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'spot',
                'adjustForTimeDifference': True
            }
        })

    async def start_market_cache(self):
        """启动市场数据缓存"""
        if not self.market_cache:
            self.market_cache = MarketDataCache(self.query_client)
            # 使用已分配的IP
            if hasattr(self, 'execution_ips'):
                self.market_cache.set_ip(self.execution_ips['primary'])
            await self.market_cache.start(self.symbol)
            self.logger.info(f"市场数据缓存已启动: {self.symbol}")

    async def stop_market_cache(self):
        """停止市场数据缓存"""
        if self.market_cache:
            await self.market_cache.stop()
            self.market_cache = None
        
    async def monitor_position(self, order_id: str):
        """监控持仓位置"""
        try:
            # 启动市场数据缓存
            await self.start_market_cache()
            
            # 创建异常行情监控器
            opportunity_monitor = MarketOpportunityMonitor(
                self.market_cache,
                self.entry_price,
                self.total_amount
            )
            
            while True:
                # 检查异常行情机会
                opportunity = await opportunity_monitor.check_opportunity()
                if opportunity:
                    # 计算预期收益
                    expected_profit = opportunity['expected_profit']
                    total_profit = sum(profit for _, profit in self.profits)
                    
                    if expected_profit > total_profit:
                        # 执行抢卖
                        sell_price = opportunity['sell_price']
                        await self.execute_sell(sell_price)
                        break
                
                await asyncio.sleep(0.001)  # 1ms检查间隔
            
        except Exception as e:
            logger.error(f"监控持仓失败: {str(e)}")
            print(f"\n❌ 监控持仓失败: {str(e)}")
        finally:
            # 确保停止数据缓存
            await self.stop_market_cache()
        
    async def _init_clients(self) -> bool:
        """初始化API客户端"""
        try:
            # 获取API密钥
            trade_api_key, trade_api_secret = self.config.get_trade_api_keys()
            query_api_key, query_api_secret = self.config.get_query_api_keys()
            
            if not (trade_api_key and trade_api_secret and query_api_key and query_api_secret):
                self.logger.warning("API密钥未配置")
                return False
                
            # 创建客户端
            self.trade_client = self._create_ccxt_client(trade_api_key, trade_api_secret)
            self.query_client = self._create_ccxt_client(query_api_key, query_api_secret)
            
            return True
            
        except Exception as e:
            self.logger.error(f"初始化客户端失败: {str(e)}")
            return False
    async def setup_api_keys_async(self):
        """异步设置API密钥"""
        try:
            print("\n=== API密钥设置 ===")
            
            # 检查是否已有API配置
            if self.config.has_api_keys():
                print("\n检测到已有API配置,正在测试连接...")
                try:
                    # 测试交易API
                    await asyncio.to_thread(self.trade_client.fetch_balance)
                    print("✅ 交易API连接正常")
                    
                    # 测试查询API
                    start_time = time.time()
                    await asyncio.to_thread(self.query_client.fetch_time)
                    latency = (time.time() - start_time) * 1000
                    print(f"✅ 查询API连接正常 (延迟: {latency:.2f}ms)")
                    
                    # 询问是否要更换
                    change = input("\n是否要更换API密钥? (y/n): ").strip().lower()
                    if change != 'y':
                        print("\nAPI设置未变更")
                        return True
                except Exception as e:
                    print(f"\n⚠️ 现有API测试失败: {str(e)}")
            
            # 设置新的API密钥
            print("\n请输入新的API密钥:")
            trade_key = input("交易API Key: ").strip()
            trade_secret = input("交易API Secret: ").strip()
            query_key = input("查询API Key: ").strip()
            query_secret = input("查询API Secret: ").strip()
            
            # 保存API密钥
            self.config.set_trade_api_keys(trade_key, trade_secret)
            self.config.set_query_api_keys(query_key, query_secret)
            
            print("\n✅ API密钥已保存")
            
            # 重新初始化客户端并测试
            if await self._init_clients():
                print("\n正在测试新API连接...")
                try:
                    # 测试交易API
                    await asyncio.to_thread(self.trade_client.fetch_balance)
                    print("✅ 交易API连接正常")
                    
                    # 测试查询API
                    start_time = time.time()
                    await asyncio.to_thread(self.query_client.fetch_time)
                    latency = (time.time() - start_time) * 1000
                    print(f"✅ 查询API连接正常 (延迟: {latency:.2f}ms)")
                except Exception as e:
                    print(f"❌ API测试失败: {str(e)}")
            else:
                print("❌ API客户端初始化失败")
            
            return True
            
        except Exception as e:
            self.logger.error(f"设置API密钥失败: {str(e)}")
            print(f"\n❌ 设置失败: {str(e)}")
            return False

    def _init_trading_components(self):
        """初始化交易相关组件"""
        if not self.trade_client or not self.query_client:
            raise ValueError("请先设置API密钥")

        # 初始化其他组件
        self.time_sync = TimeSync()  # 已定义的时间同步类
        self.ip_manager = IPManager()  # 替代 NetworkOptimizer
        self.data_pool = DataPool()    # 数据池
        self.order_processor = OrderProcessor(self.config, self.data_pool)  # 订单处理器
        self.monitor = RealTimeMonitor()  # 监控器
        
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

            # 创建异步会话 (如果需要，可以考虑初始化 aiohttp.ClientSession，但目前代码中没有使用)
            # self.async_session = aiohttp.ClientSession() # 如果后续需要异步会话，可以初始化在这里

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
            
            # 修改日志格式
            self.logger.info(f"""
=== 时间同步完成 ===
• 总耗时: {total_time:.2f}ms
• 网络延迟: {self.network_latency:.2f}ms
• 时间偏移: {self.server_time_offset:.2f}ms
""")
            
            return self.network_latency, self.time_offset
            
        except Exception as e:
            self.logger.error(f"时间同步失败: {str(e)}")
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
            elapsed = current_time - self.last_query_time[endpoint]  # 修复括号闭合
            if elapsed < self.min_query_interval:
                sleep_time = self.min_query_interval - elapsed
                await asyncio.sleep(sleep_time)
        self.last_query_time[endpoint] = current_time

    def setup_trading_pair(self, symbol: str, amount: float):
        """设置交易对和金额"""
        self.symbol = symbol  # ✅ 确保这里被调用
        self.amount = amount
        logger.info(f"已设置交易对: {symbol}, 买入金额: {amount} USDT")

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

    async def start_market_cache(self):
        """启动市场数据缓存"""
        if not self.market_cache:
            self.market_cache = MarketDataCache(self.query_client)
            # 使用已分配的IP
            if hasattr(self, 'execution_ips'):
                self.market_cache.set_ip(self.execution_ips['primary'])
            await self.market_cache.start(self.symbol)
            self.logger.info(f"市场数据缓存已启动: {self.symbol}")
            
    async def stop_market_cache(self):
        """停止市场数据缓存"""
        if self.market_cache:
            await self.market_cache.stop()
            self.market_cache = None
            
    async def monitor_position(self, order_id: str):
        """监控持仓位置"""
        try:
            # 启动市场数据缓存
            await self.start_market_cache()
            
            # 创建异常行情监控器
            opportunity_monitor = MarketOpportunityMonitor(
                self.market_cache,
                self.entry_price,
                self.total_amount
            )
            
            while True:
                # 检查异常行情机会
                opportunity = await opportunity_monitor.check_opportunity()
                if opportunity:
                    # 计算预期收益
                    expected_profit = opportunity['expected_profit']
                    total_profit = sum(profit for _, profit in self.profits)
                    
                    if expected_profit > total_profit:
                        # 执行抢卖
                        sell_price = opportunity['sell_price']
                        await self.execute_sell(sell_price)
                        break
                
                await asyncio.sleep(0.001)  # 1ms检查间隔
            
        except Exception as e:
            logger.error(f"监控持仓失败: {str(e)}")
            print(f"\n❌ 监控持仓失败: {str(e)}")
        finally:
            # 确保停止数据缓存
            await self.stop_market_cache()
        
    def _is_price_safe(self, current_price: float, target_price: float) -> bool:
        """检查价格是否在安全范围内"""
        deviation = abs(current_price - target_price) / target_price
        if deviation > self.max_price_deviation:
            logger.warning(f"价格偏差过大: 目标 {target_price}, 当前 {current_price}, 偏差 {deviation*100:.2f}%")
            return False
        return True

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
                    continue
                
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

        
    async def snipe(self):
        """执行抢购"""
        try:
            # 1. 初始化准备
            if not await self._init_snipe():
                return None
            
            # 2. 同步时间
            sync_results = await self.sync_time()
            if not sync_results:
                logger.error("时间同步失败")
                return None
                
            # 3. 计算发送时间
            send_time = self._calculate_send_time(sync_results)
            if not send_time:
                logger.error("发送时间计算失败")
                return None
                
            # 4. 准备订单参数
            params = {
                'symbol': self.symbol.replace('/', ''),
                'side': 'BUY',
                'type': 'LIMIT',
                'timeInForce': 'IOC',
                'quantity': self.amount,
                'price': self.max_price_limit,
                'timestamp': int(time.time() * 1000)
            }
            
            # 5. 等待直到发送时间
            current_ms = time.time() * 1000
            if current_ms < send_time:
                await asyncio.sleep((send_time - current_ms) / 1000)
                
            # 6. 执行订单
            result = await self._execute_order_async(params)
            if result and result.get('orderId'):
                logger.info(f"订单执行成功: {result['orderId']}")
                return result
            else:
                logger.error("订单执行失败")
                return None
            
        except Exception as e:
            logger.error(f"抢购执行失败: {str(e)}")
            return None

    async def _init_snipe(self):
        """初始化抢购准备"""
        try:
            # 1. 检查市场状态
            status = await self.check_symbol_status()
            if not status['active']:
                logger.error(f"交易对状态异常: {status['msg']}")
                return False

            # 2. 准备IP资源
            if not await self.prepare_ips():
                logger.error("IP资源准备失败")
                return False

            # 3. 验证IP可用性
            if not await self.validate_ips():
                logger.error("IP验证失败")
                return False

            # 4. 建立WebSocket连接
            if not await self._setup_websocket():
                logger.error("WebSocket连接失败")
                return False

            return True

        except Exception as e:
            logger.error(f"初始化失败: {str(e)}")
            return False

    async def _execute_order_async(self, params):
        """异步执行订单"""
        try:
            return await asyncio.to_thread(
                self.trade_client.create_order,
                **params
            )
        except Exception as e:
            logger.error(f"订单执行失败: {str(e)}")
            return None

    async def sync_time(self) -> bool:  # ✅ 正确声明为异步方法
        """时间同步"""
        return await self.time_sync.sync()  # 确保调用异步方法

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
f"最终时间同步完成: {sync_time:.2f}ms"  # 正确：整个字符串在引号内
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

    async def prepare_and_snipe(self):
        """全异步准备并执行抢购"""
        try:
            logger.info("异步准备流程开始...")
            
            # 异步初始化链
            init_tasks = [
                self._async_load_api_keys(),
                self._async_load_strategy(),
                self._async_init_clients(),
                self._async_prepare_ips()
            ]
            
            # 并行执行初始化任务
            results = await asyncio.gather(*init_tasks, return_exceptions=True)
            
            # 检查初始化结果
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"初始化阶段 {i+1} 失败: {str(result)}")
                    return False
                if not result:
                    logger.error(f"初始化阶段 {i+1} 返回失败")
                    return False
            
            # 执行核心抢购逻辑
            return await self._async_snipe_core()
            
        except Exception as e:
            logger.error(f"异步流程异常: {str(e)}")
            return False

    # 异步加载API密钥
    async def _async_load_api_keys(self) -> bool:
        """使用aiofiles异步加载API密钥"""
        try:
            async with aiofiles.open(self.config.config_file, mode='r') as f:
                content = await f.read()
                config = json.loads(content)
                
            self.api_key = config.get('trade_api_key')
            self.api_secret = config.get('trade_api_secret')
            
            if not all([self.api_key, self.api_secret]):
                raise ConfigError("API密钥不完整")
                
            logger.info("API密钥异步加载成功")
            return True
            
        except Exception as e:
            logger.error(f"密钥加载失败: {str(e)}")
            return False

    # 异步加载策略
    async def _async_load_strategy(self) -> bool:
        """使用aiofiles异步加载策略"""
        try:
            strategy_file = os.path.join(CONFIG_DIR, 'strategy.ini')
            async with aiofiles.open(strategy_file, mode='r') as f:
                content = await f.read()
                
            config = configparser.ConfigParser()
            config.read_string(content)
            
            # 异步解析策略参数
            self.symbol = await asyncio.to_thread(config.get, 'Basic', 'symbol')
            self.amount = await asyncio.to_thread(config.getfloat, 'Basic', 'amount')
            
            # 解析时间并转换时区
            time_str = await asyncio.to_thread(config.get, 'Basic', 'opening_time')
            self.opening_time = await asyncio.to_thread(
                lambda: datetime.strptime(time_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=self.timezone)
            )
            
            logger.info("策略异步加载成功")
            return True
            
        except Exception as e:
            logger.error(f"策略加载失败: {str(e)}")
            return False

    # 完全异步化客户端初始化
    async def _async_init_clients(self) -> bool:
        """使用线程池执行同步CCXT操作"""
        try:
            loop = asyncio.get_running_loop()
            
            # 在线程池中执行同步CCXT初始化
            self.trade_client = await loop.run_in_executor(
                None, 
                lambda: ccxt.binance({
                    'apiKey': self.api_key,
                    'secret': self.api_secret,
                    'enableRateLimit': True
                })
            )
            
            # 异步测试连接
            await loop.run_in_executor(None, self.trade_client.fetch_balance)
            logger.info("交易客户端异步初始化成功")
            return True
            
        except Exception as e:
            logger.error(f"客户端初始化失败: {str(e)}")
            return False

    # 核心抢购逻辑
    async def _async_snipe_core(self) -> bool:
        """全异步抢购核心逻辑"""
        try:
            # 创建异步监控任务
            monitor_task = asyncio.create_task(self._async_monitor_market())
            
            # 精确时间同步
            server_time = await self._async_get_server_time()
            target_time = self.opening_time.timestamp() * 1000
            time_diff = target_time - server_time
            
            # 时间校准
            if time_diff > 1000:
                logger.warning(f"时间偏差过大: {time_diff}ms")
                return False
                
            # 精确等待
            waiter = PreciseWaiter()
            await waiter.wait_until(target_time - 50)  # 提前50ms
            
            # 并发下单
            order_tasks = [
                self._async_send_order(i) 
                for i in range(self.concurrent_orders)
            ]
            results = await asyncio.gather(*order_tasks)
            
            # 处理结果
            success_orders = [r for r in results if r]
            logger.info(f"成功订单数: {len(success_orders)}/{self.concurrent_orders}")
            return len(success_orders) > 0
            
        except Exception as e:
            logger.error(f"核心抢购失败: {str(e)}")
            return False
        finally:
            monitor_task.cancel()

    # 异步发送订单
    async def _async_send_order(self, order_id: int) -> bool:
        """使用aiohttp异步发送订单"""
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    'symbol': self.symbol.replace('/', ''),
                    'side': 'BUY',
                    'type': 'LIMIT',
                    'timeInForce': 'IOC',
                    'quantity': self.amount / self.concurrent_orders,
                    'price': self.price,
                    'timestamp': int(time.time() * 1000)
                }
                
                # 生成签名
                query = '&'.join([f"{k}={v}" for k,v in params.items()])
                signature = hmac.new(
                    self.api_secret.encode(), 
                    query.encode(), 
                    hashlib.sha256
                ).hexdigest()
                params['signature'] = signature
                
                # 发送异步请求
                async with session.post(
                    'https://api.binance.com/api/v3/order',
                    headers={'X-MBX-APIKEY': self.api_key},
                    params=params
                ) as response:
                    if response.status == 200:
                        return True
                    logger.error(f"订单{order_id}失败: {await response.text()}")
                    return False
                
        except Exception as e:
            logger.error(f"订单{order_id}异常: {str(e)}")
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
    
    async def _fetch_market_data(self) -> Optional[Dict]:
        """获取市场数据"""
        try:
            # 使用 await 正确获取订单簿数据
            depth = await asyncio.to_thread(
                self.query_client.fetch_order_book,
                self.symbol,
                limit=5
            )
            
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
            self.logger.error(f"获取市场数据失败: {str(e)}")
            return None

    async def _execute_order_async(self, params: Dict) -> Optional[Dict]:
        """异步执行订单"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    'https://api.binance.com/api/v3/order',
                    headers={'X-MBX-APIKEY': self.trade_client.apiKey},
                    json=params,
                    timeout=1  # 1秒超时
                ) as response:
                    if response.status == 200:
                        return await response.json()
                    else:
                        self.logger.error(f"订单执行失败: {response.status}")  # 修改这里
                        return None
        except Exception as e:
            self.logger.error(f"订单执行异常: {str(e)}")  # 修改这里
            return None

    async def execute_snipe(self):
        """执行抢购主逻辑"""
        try:
            return await self.order_processor.execute_orders(
                symbol=self.symbol,
                amount=self.amount,
                price=self.max_price_limit
            )
        except Exception as e:
            self.logger.error(f"抢购执行失败: {str(e)}")
            return None

    # 添加这个代理方法
    async def setup_snipe_strategy_async(self):
        """代理到 OrderProcessor 的策略设置方法"""
        try:
            # 确保设置必要的客户端
            self.order_processor.query_client = self.query_client
            self.order_processor.trade_client = self.trade_client
            
            # 调用 OrderProcessor 的实现
            return await self.order_processor.setup_snipe_strategy_async()
            
        except Exception as e:
            self.logger.error(f"设置抢购策略失败: {str(e)}")
            return False

# =============================== 
# 模块：订单处理
# ===============================
class OrderProcessor:
    """订单处理核心类"""
    
    def __init__(self, config: ConfigManager, data_pool: DataPool, sniper=None):
        """初始化订单处理器
        Args:
            config: 配置管理器实例
            data_pool: 市场数据池实例
            sniper: BinanceSniper实例引用
        """
        # 基础组件
        self.config = config
        self.data_pool = data_pool
        self.logger = logging.getLogger(__name__)
        self._executor = ThreadPoolExecutor(max_workers=4)
        self.sniper = sniper  # 保存 BinanceSniper 引用
        
        # 执行配置
        self.execution_ips = {}
        self.ip_roles_locked = False
        self.server_time_offset = 0
        self.timeout = 5.0
        self.retry_count = 3
        self.network_latency = 0
        
        # API客户端
        self.trade_client = None
        self.query_client = None
        
        # 缓存设置
        self.market_cache = {}
        self.cache_ttl = 0.5  # 500ms缓存时间
        
        # 交易参数
        self.symbol = None
        self.amount = 0
        self.max_price_limit = 0
        self.advance_time = 0
        self.concurrent_orders = 0
        
        # 添加时区设置
        self.timezone = pytz.timezone('Asia/Shanghai')  # 添加这一行

    def _sign_request(self, params: dict) -> dict:
        """生成请求签名"""
        api_key, api_secret = self.config.get_trade_api_keys()
        data = params.copy()
        data.update({'timestamp': int(time.time() * 1000)})
        query = '&'.join([f"{k}={v}" for k, v in sorted(data.items())])
        data['signature'] = hmac.new(
            api_secret.encode(), 
            query.encode(), 
            hashlib.sha256
        ).hexdigest()
        return data

    def build_order_params(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: float,
        price: float,
        time_in_force: str = 'IOC'
    ) -> dict:
        """构建订单参数"""
        return self._sign_request({
            'symbol': symbol.replace('/', ''),
            'side': side.upper(),
            'type': order_type.upper(),
            'timeInForce': time_in_force,
            'quantity': round(amount, 6),
            'price': str(round(price, 4))
        })

    async def execute_orders_with_strategy(self) -> List[dict]:
        """使用分批策略执行订单"""
        orders = []
        execution_start = self.get_server_time()
        
        try:
            # 1. 获取最新市场数据
            market_data = await self.data_pool.get_latest_data()
            if not market_data:
                raise MarketError("无法获取市场数据")

            # 2. 检查价格
            current_price = market_data['price']
            if current_price > self.max_price_limit:
                raise MarketError(f"当前价格 {current_price} 超过限制 {self.max_price_limit}")

            # 3. 三批次执行策略
            batches = [
                (2, 0, 'primary'),   # 第1批: 2个订单, 0ms延迟
                (2, 5, 'secondary'), # 第2批: 2个订单, 5ms延迟
                (1, 10, 'fallback')  # 第3批: 1个订单, 10ms延迟
            ]
            
            for batch_num, (size, offset, ip_role) in enumerate(batches, 1):
                self.logger.info(f"执行第{batch_num}批订单...")
                current_price = await self.data_pool.get_latest_price()
                
                batch_result = await self._execute_batch(
                    batch_size=size,
                    time_offset=offset,
                    ip=self.execution_ips[ip_role],
                    price=current_price
                )
                
                if batch_result:
                    await self.print_batch_result(batch_num, batch_result)
                    return batch_result

            total_time = self.get_server_time() - execution_start
            self.logger.warning(f"所有批次执行完成，总耗时: {total_time}ms，无成功订单")
            return []

        except Exception as e:
            self.logger.error(f"订单执行策略失败: {str(e)}")
            return orders

    async def _execute_batch(self, batch_size: int, time_offset: int, ip: str, price: float) -> List[dict]:
        """执行一批订单"""
        orders = []
        try:
            base_time = self.get_server_time()
            execution_time = base_time + time_offset

            tasks = [
                self._place_single_order(
                    ip=ip,
                    execution_time=execution_time,
                    price=price
                )
                for _ in range(batch_size)
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            for result in results:
                if isinstance(result, Exception):
                    self.logger.error(f"订单执行失败: {str(result)}")
                    continue
                if result and result.get('status') == 'filled':
                    orders.append(result)

            return orders

        except Exception as e:
            self.logger.error(f"批次执行失败: {str(e)}")
            return orders

    async def _place_single_order(self, ip: str, execution_time: float, price: float) -> Optional[dict]:
        """执行单个订单"""
        try:
            current_time = self.get_server_time()
            if current_time < execution_time:
                wait_time = (execution_time - current_time) / 1000
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

            order_params = self.build_order_params(
                symbol=self.symbol,
                side='BUY',
                order_type='LIMIT',
                amount=self.amount,
                price=price,
                time_in_force='GTC'
            )
            order_params['timestamp'] = int(execution_time)

            start_time = self.get_server_time()
            order = await self.trade_client.create_order(**order_params)
            execution_latency = self.get_server_time() - start_time

            self.logger.info(f"订单执行完成 - IP: {ip}, 延迟: {execution_latency}ms")
            
            return order

        except Exception as e:
            await self.ip_manager.report_error(ip)
            raise ExecutionError(f"订单执行失败: {str(e)}")

    def get_server_time(self) -> float:
        """获取当前币安服务器时间(毫秒)"""
        return time.time() * 1000 + (self.server_time_offset or 0)

    # ... (其他方法保持不变,包括之前的打印方法)
    async def print_order_status(self, order: dict):
        """打印订单状态信息"""
        try:
            print(f"""
====== 订单状态 ======
订单ID: {order['id']}
状态: {order['status']}
价格: {order['price']} USDT
数量: {order['amount']}
成交量: {order['filled']}
剩余量: {order['remaining']}
成交金额: {order['cost']} USDT
时间: {datetime.fromtimestamp(order['timestamp']/1000).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}
==================
""")
        except Exception as e:
            self.logger.error(f"打印订单状态失败: {str(e)}")

    async def print_execution_summary(self, results: List[dict]):
        """打印执行汇总信息"""
        try:
            successful = [r for r in results if r.get('status') == 'filled']
            total_filled = sum(order['filled'] for order in successful)
            total_cost = sum(order['cost'] for order in successful)
            
            print(f"""
====== 执行汇总 ======
成功订单: {len(successful)}/{len(results)}
成交数量: {total_filled}
成交金额: {total_cost} USDT
平均价格: {total_cost/total_filled if total_filled else 0} USDT
==================
""")
        except Exception as e:
            self.logger.error(f"打印执行汇总失败: {str(e)}")

    async def print_batch_result(self, batch_num: int, results: List[dict]):
        """打印批次执行结果"""
        try:
            successful = [r for r in results if r.get('status') == 'filled']
            print(f"""
====== 第{batch_num}批执行结果 ======
订单数量: {len(results)}
成功数量: {len(successful)}
成交详情:""")
            
            for order in successful:
                print(f"""
• 订单ID: {order['id']}
  价格: {order['price']} USDT
  数量: {order['filled']}
  金额: {order['cost']} USDT
  延迟: {order.get('execution_latency', 0):.2f}ms""")
            
            print("==================")
            
        except Exception as e:
            self.logger.error(f"打印批次结果失败: {str(e)}")

    def show_execution_stats(self):
        """显示执行统计信息"""
        print(f"""
====== 执行统计 ======
IP分配:
• 主要IP: {self.execution_ips.get('primary', 'N/A')}
• 备用IP: {self.execution_ips.get('secondary', 'N/A')}
• 故障转移IP: {self.execution_ips.get('fallback', 'N/A')}

执行配置:
• 批次数量: 3
• 总订单数: 5
• 时间间隔: 5ms
• 超时设置: {self._executor._max_workers}线程/{self.timeout}秒

网络状态:
• 服务器延迟: {self.network_latency:.2f}ms
• 时间偏差: {self.server_time_offset:.2f}ms
==================
""")

    async def print_market_status(self):
        """打印市场状态"""
        try:
            market_data = await self.data_pool.get_latest_data()
            if not market_data:
                print("\n⚠️ 无法获取市场数据")
                return

            print(f"""
====== 市场状态 ======
交易对: {self.symbol}
当前价格: {market_data['price']} USDT
买一价: {market_data['bid']} USDT
卖一价: {market_data['ask']} USDT
24h成交量: {market_data['volume']} 
深度更新: {datetime.fromtimestamp(market_data['timestamp']/1000).strftime('%H:%M:%S.%f')[:-3]}
==================
""")
        except Exception as e:
            self.logger.error(f"打印市场状态失败: {str(e)}")

    async def print_order_book(self, depth: int = 5):
        """打印订单簿"""
        try:
            order_book = await self.data_pool.get_order_book()
            
            print(f"\n====== {self.symbol} 订单簿 ======")
            print("\n卖单:")
            for price, amount in reversed(order_book['asks'][:depth]):
                print(f"  {price:10.8f} | {amount:10.8f}")
                
            print("\n买单:")
            for price, amount in order_book['bids'][:depth]:
                print(f"  {price:10.8f} | {amount:10.8f}")
                
            print("\n==================")

        except Exception as e:
            self.logger.error(f"打印订单簿失败: {str(e)}")

    async def print_strategy_status(self):
        """打印策略状态"""
        try:
            market_data = await self.data_pool.get_latest_data()
            balance = await self.trade_client.fetch_balance()
            usdt_balance = balance.get('USDT', {}).get('free', 0)

            print(f"""
====== 策略状态 ======
基本信息:
• 交易对: {self.symbol}
• 当前价格: {market_data['price']} USDT
• 可用USDT: {usdt_balance:.2f}

执行设置:
• 最大价格: {self.max_price_limit} USDT
• 买入金额: {self.amount} USDT
• 分批执行: {self.concurrent_orders}个订单
• 时间提前: {self.advance_time}ms

风险控制:
• 价格保护: 最高{self.max_price_limit} USDT
• 订单超时: {self.timeout}秒
• 重试次数: {self.retry_count}次

网络状态:
• 主IP延迟: {self.network_latency:.2f}ms
• 备用IP数: {len(self.execution_ips)-1}个
• 时间偏差: {self.server_time_offset:.2f}ms
==================
""")
        except Exception as e:
            self.logger.error(f"打印策略状态失败: {str(e)}")

    async def print_execution_progress(self, current: int, total: int, status: str):
        """打印执行进度"""
        try:
            progress = current / total * 100
            print(f"""
执行进度: [{current}/{total}] {progress:.1f}%
状态: {status}
时间: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}
""")
        except Exception as e:
            self.logger.error(f"打印进度失败: {str(e)}")

    async def monitor_order_execution(self, order_id: str) -> dict:
        """监控订单执行状态"""
        try:
            while True:
                order = await self.trade_client.fetch_order(order_id, self.symbol)
                if order['status'] in ['filled', 'canceled', 'rejected']:
                    await self.print_order_status(order)
                    return order
                await asyncio.sleep(0.1)
        except Exception as e:
            self.logger.error(f"监控订单状态失败: {str(e)}")
            raise

    async def print_strategy_async(self):
        """打印当前策略配置"""
        try:
            # 确保市场数据是最新的
            await self.sniper.update_market_data()
            market_data = self.sniper.market_data
            
            # 获取当前时间和剩余时间
            now = datetime.now(self.timezone)
            if hasattr(self, 'opening_time'):
                time_diff = self.opening_time - now
                hours_remaining = time_diff.total_seconds() / 3600
            else:
                hours_remaining = 0
            
            # 计算实际的提前时间
            advance_time = (
                market_data['network_latency'] +  # 网络延迟
                abs(market_data['time_offset']) +  # 时间偏移
                5  # 安全冗余
            )

            print(f"""
====== 当前抢购策略详情 ======

📌 基础参数
- 交易对: {self.symbol or '未设置'}
- 买入金额: {self.amount or 0:,.2f} USDT
- 买入数量: {"待定" if market_data['current_price'] == 0 else f"{self.amount/market_data['current_price']:.8f} {self.symbol.split('/')[0] if self.symbol else ''}"}

📊 市场信息
- 币种状态: {market_data['market_status']}
- 当前价格: {"未上市" if market_data['current_price'] == 0 else f"{market_data['current_price']:,.2f} USDT"}
- 24h涨跌: {"未上市" if market_data['current_price'] == 0 else f"{market_data['price_24h_change']:.2f}%"}
- 24h成交: {"未上市" if market_data['current_price'] == 0 else f"{market_data['volume_24h']:,.2f} USDT"}

⏰ 时间信息
- 开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S') if hasattr(self, 'opening_time') else '未设置'}
- 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}
- 剩余时间: {int(hours_remaining)}小时{int((hours_remaining % 1) * 60)}分钟

💰 价格保护
- 最高限价: {self.max_price_limit or 0:,.2f} USDT
- 价格倍数: {self.price_multiplier or 0}倍

⚡ 执行策略
- 并发订单: {self.concurrent_orders or 0}个
- 提前时间: {advance_time:.2f}ms
- 网络延迟: {market_data['network_latency']:.2f}ms
- 时间偏移: {market_data['time_offset']:.2f}ms
- 安全冗余: 5ms

📈 止盈止损
- 止损线: -{self.stop_loss*100:.1f}%
阶梯止盈:""")

            # 打印止盈策略
            if hasattr(self, 'sell_strategy'):
                for idx, (profit, amount) in enumerate(self.sell_strategy, 1):
                    print(f"- 第{idx}档: 涨幅{profit*100:.0f}% 卖出{amount*100:.0f}%")

            # 尝试获取账户余额
            try:
                balance = await self.trade_client.fetch_balance() if self.trade_client else None
                usdt_balance = balance['USDT']['free'] if balance and 'USDT' in balance else 0
            except:
                usdt_balance = 0

            print(f"""
💰 账户状态
- 可用USDT: {usdt_balance:,.2f}
- 所需USDT: {self.amount or 0:,.2f}
- 状态: {'✅ 余额充足' if usdt_balance >= (self.amount or 0) else '❌ 余额不足'}

⚠️ 风险提示
1. 已启用最高价格保护: {self.max_price_limit or 0:,.2f} USDT
2. 已启用价格倍数限制: {self.price_multiplier or 0}倍
3. 使用IOC限价单模式
=============================""")
            
            return True
            
        except Exception as e:
            self.logger.error(f"打印策略失败: {str(e)}")
            return False
# =============================== 
# 模块：测试功能
# ===============================
    async def test_center(self):
        """测试中心"""
        try:
            while True:
                print("\n=== 性能测试选项 ===")
                print("1. 快速测试 (30秒)")
                print("2. 标准测试 (120秒)")
                print("3. 长时间测试 (300秒)")
                print("4. 自定义时长")
                print("5. 币安测试网测试")
                print("0. 返回主菜单")
                print("===================")
                
                choice = input("请选择测试时长 (0-5): ").strip()
                
                if choice == '0':
                    break
                elif choice == '1':
                    await self.run_performance_test(30)
                elif choice == '2':
                    await self.run_performance_test(120)
                elif choice == '3':
                    await self.run_performance_test(300)
                elif choice == '4':
                    try:
                        duration = int(input("请输入测试时长(秒): "))
                        if duration > 0:
                            await self.run_performance_test(duration)
                        else:
                            print("测试时长必须大于0")
                    except ValueError:
                        print("请输入有效的数字")
                elif choice == '5':
                    await self.run_testnet_test()
                else:
                    print("无效的选择，请重新输入")
        except Exception as e:
            self.logger.error(f"测试中心运行失败: {str(e)}")
            print(f"测试失败: {str(e)}")

    async def _setup_websocket(self) -> bool:
        """建立WebSocket连接"""
        try:
            # 初始化WebSocket连接参数
            self.ws_connections = []
            base_url = "wss://stream.binance.com:9443/ws"
            
            # 创建多个WebSocket连接用于不同用途
            streams = [
                f"{self.symbol.lower().replace('/', '')}@depth",  # 深度数据
                f"{self.symbol.lower().replace('/', '')}@trade",  # 交易数据
                f"{self.symbol.lower().replace('/', '')}@ticker"  # 24小时行情
            ]
            
            # 建立连接
            for stream in streams:
                try:
                    ws = await websockets.connect(f"{base_url}/{stream}")
                    self.ws_connections.append(ws)
                    logger.debug(f"WebSocket连接成功: {stream}")
                except Exception as e:
                    logger.error(f"WebSocket连接失败 ({stream}): {str(e)}")
                    # 关闭已建立的连接
                    for ws in self.ws_connections:
                        await ws.close()
                    return False
            
            logger.info(f"WebSocket连接已建立 ({len(self.ws_connections)}个)")
            return True
            
        except Exception as e:
            logger.error(f"建立WebSocket连接失败: {str(e)}")
            return False

    async def _cleanup_websocket(self):
        """清理WebSocket连接"""
        try:
            # 关闭所有WebSocket连接
            for ws in self.ws_connections:
                await ws.close()
            self.ws_connections = []
            logger.info("所有WebSocket连接已关闭")
        except Exception as e:
            logger.error(f"清理WebSocket连接失败: {str(e)}")

    async def prepare_and_snipe_async(self):
        """异步执行抢购准备和执行"""
        try:
            # 1. 检查开盘时间
            current_time = datetime.now(self.timezone)
            time_to_open = (self.opening_time - current_time).total_seconds()
            
            if time_to_open <= 0:
                logger.error("开盘时间已过")
                return None
                
            logger.info(f"""
=== 抢购配置确认 ===
交易对: {self.symbol}
买入金额: {self.amount} USDT
开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S')}
距离开盘: {time_to_open/3600:.1f}小时
""")

            # 2. 同步时间和网络延迟
            sync_results = await self.sync_time()
            if not sync_results:
                logger.error("时间同步失败")
                return None
                
            # 3. 计算发送时间
            send_time = self._calculate_send_time(sync_results)
            if not send_time:
                logger.error("发送时间计算失败")
                return None
                
            # 4. 准备订单参数
            params = {
                'symbol': self.symbol.replace('/', ''),
                'side': 'BUY',
                'type': 'LIMIT',
                'timeInForce': 'IOC',
                'quantity': self.amount,
                'price': self.max_price_limit,
                'timestamp': int(time.time() * 1000)
            }
            
            # 5. 等待直到发送时间
            current_ms = time.time() * 1000
            if current_ms < send_time:
                await asyncio.sleep((send_time - current_ms) / 1000)
                
            # 6. 执行订单
            result = await self._execute_order_async(params)
            if result and result.get('orderId'):
                logger.info(f"订单执行成功: {result['orderId']}")
                return result
            else:
                logger.error("订单执行失败")
                return None
                
        except Exception as e:
            logger.error(f"抢购执行失败: {str(e)}")
            return None

    async def run_performance_test(self, duration: int):
        """运行性能测试"""
        try:
            print(f"\n开始执行 {duration} 秒性能测试...")
            
            # 确保数据池已启动
            if not self.data_pool:
                print("数据池未初始化")
                return
            
            await self.data_pool.start(self.symbol)
            
            test_start = time.time()
            stats = {
                'price_updates': 0,
                'depth_updates': 0,
                'trade_updates': 0,
                'latencies': []
            }
            
            print("\n=== 测试进行中 ===")
            while time.time() - test_start < duration:
                # 使用数据池获取数据
                price = await self.data_pool.get_latest_price()
                depth = await self.data_pool.get_depth()
                trades = await self.data_pool.get_recent_trades()
                
                if price:
                    stats['price_updates'] += 1
                if depth:
                    stats['depth_updates'] += 1
                if trades:
                    stats['trade_updates'] += 1
                    
                await asyncio.sleep(0.1)  # 100ms采样间隔
                
            # 获取数据池统计信息
            pool_stats = self.data_pool.get_pool_stats()
            
            print("\n=== 测试结果 ===")
            print(f"• 价格更新次数: {stats['price_updates']}")
            print(f"• 深度更新次数: {stats['depth_updates']}")
            print(f"• 成交更新次数: {stats['trade_updates']}")
            print(f"• WebSocket消息总数: {pool_stats['ws_messages']}")
            print(f"• REST更新总数: {pool_stats['rest_updates']}")
            print(f"• 缓存命中率: {pool_stats['cache_hits']/(pool_stats['cache_hits']+pool_stats['cache_misses'])*100:.1f}%")
            
        except Exception as e:
            logger.error(f"性能测试失败: {str(e)}")
            print(f"测试失败: {str(e)}")
        finally:
            # 确保清理资源
            await self.cleanup()

    async def run_testnet_test(self):
        """运行测试网测试"""
        try:
            print("\n=== 开始币安测试网测试 ===")
            
            # 切换到测试网
            self.trade_client.set_sandbox_mode(True)
            self.query_client.set_sandbox_mode(True)
            
            # 启动数据池
            await self.data_pool.start(self.symbol)
            
            print("已切换到测试网环境")
            print("开始执行测试流程...")
            
            # 执行模拟交易
            test_order = await self.execute_test_order()
            if test_order:
                print(f"测试订单执行成功: {test_order['orderId']}")
                
                # 使用数据池监控价格变化
                initial_price = await self.data_pool.get_latest_price()
                print(f"初始价格: {initial_price}")
                
                # 监控一段时间的价格变化
                for _ in range(10):
                    await asyncio.sleep(1)
                    current_price = await self.data_pool.get_latest_price()
                    print(f"当前价格: {current_price}")
            
            # 切回主网
            self.trade_client.set_sandbox_mode(False)
            self.query_client.set_sandbox_mode(False)
            
            print("\n测试完成，已切回主网")
            
        except Exception as e:
            logger.error(f"测试网测试失败: {str(e)}")
            print(f"测试失败: {str(e)}")
        finally:
            await self.cleanup()

    async def load_strategy_async(self) -> bool:
        """异步加载策略"""
        try:
            # 使用正确的配置文件路径
            strategy_file = '/root/config/strategy.ini'
            if not os.path.exists(strategy_file):
                self.logger.warning(f"未找到策略配置文件: {strategy_file}")
                return False
            
            # 使用 asyncio.to_thread 包装文件读取操作
            config = configparser.ConfigParser()
            await asyncio.to_thread(config.read, strategy_file)
            
            # 加载基础参数
            self.symbol = config['Basic']['symbol']
            self.amount = float(config['Basic']['amount'])
            
            # 解析时间并设置时区
            opening_time_str = config['Basic']['opening_time']
            self.opening_time = datetime.strptime(
                opening_time_str,
                '%Y-%m-%d %H:%M:%S'
            )
            # 确保设置东八区时区
            if self.timezone is None:
                self.timezone = pytz.timezone('Asia/Shanghai')
            self.opening_time = self.timezone.localize(self.opening_time)
            
            # 加载价格保护参数
            self.max_price_limit = float(config['PriceProtection']['max_price_limit'])
            self.price_multiplier = float(config['PriceProtection']['price_multiplier'])
            
            # 加载执行参数
            self.concurrent_orders = int(config['Execution']['concurrent_orders'])
            self.advance_time = int(config['Execution']['advance_time'])
            
            # 加载止盈止损策略
            self.stop_loss = float(config['StopStrategy']['stop_loss'])
            self.sell_strategy = json.loads(config['StopStrategy']['sell_strategy'])
            
            self.logger.info(f"""
[策略加载] 
✅ 策略配置成功
• 交易对: {self.symbol}
• 买入金额: {format_number(self.amount)} USDT
• 开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S %Z')}
• 价格保护: 最高 {format_number(self.max_price_limit)} USDT ({self.price_multiplier}倍)
• 执行参数: {self.concurrent_orders}并发, 提前{self.advance_time}ms
• 止盈止损: 
  - 止损: -{self.stop_loss*100:.1f}%
{chr(10).join([f'  - 止盈{i+1}: +{p*100:.1f}% 卖出{a*100:.1f}%' for i, (p, a) in enumerate(self.sell_strategy)])}
""")
            return True            
        except Exception as e:
            self.logger.error(f"加载策略失败: {str(e)}")
            return False

    async def save_strategy_async(self):
        """异步保存策略"""
        try:
            if not hasattr(self, 'opening_time') or not self.opening_time:
                self.logger.error("未设置开盘时间")
                return False
                
            # 使用固定的配置目录路径
            config_dir = '/root/config'
            if not os.path.exists(config_dir):
                await asyncio.to_thread(os.makedirs, config_dir)
                
            strategy_file = os.path.join(config_dir, 'strategy.ini')
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
                'sell_strategy': json.dumps(self.sell_strategy)
            }
            
            # 直接写入文件
            with open(strategy_file, 'w') as f:
                config.write(f)
                
            self.logger.info(f"策略已保存到: {strategy_file}")
            return True
            
        except Exception as e:
            self.logger.error(f"保存策略失败: {str(e)}")
            return False

    
    async def setup_snipe_strategy_async(self):
        """设置抢购策略"""
        try:
            self.logger.info("开始设置抢购策略...")  # 修改这里
            
            # 1. 基础设置
            print("\n>>> 基础参数设置")
            coin = input("请输入要买的币种 (例如 BTC): ").strip().upper()
            symbol = f"{coin}/USDT"  # 自动添加USDT交易对
            total_usdt = float(input("请输入买入金额(USDT): ").strip())
            
            # 2. 价格保护设置
            print("\n>>> 价格保护设置")
            print("价格保护机制说明:")
            print("1. 最高接受单价: 绝对价格上限，无论如何都不会超过此价格")
            print("2. 开盘价倍数: 相对于第一档价格的最大倍数")
            
            max_price = float(input("设置最高接受单价 (USDT): ").strip())
            price_multiplier = float(input("设置开盘价倍数 (例如 3 表示最高接受开盘价的3倍): ").strip())
            
            # 3. 执行参数设置
            print("\n>>> 执行参数设置")
            concurrent_orders = int(input("并发订单数量 (建议1-5): ").strip())
            advance_time = 100  # 默认提前100ms
            
            # 4. 止盈止损设置
            print("\n>>> 止盈止损设置")
            
            # 4.1 设置止损
            while True:
                try:
                    print("\n请设置止损百分比 (例如: 输入2表示-2%)")
                    stop_loss_input = float(input("止损百分比: ").strip())
                    if 0 < stop_loss_input <= 100:
                        stop_loss = -stop_loss_input / 100  # 转换为负数的小数形式
                        break
                    else:
                        print("止损百分比必须大于0且小于等于100")
                except ValueError:
                    print("请输入有效的数字")

            # 4.2 设置止盈
            print("\n请设置5档止盈,每档设置涨幅百分比和卖出比例")
            print("例如: 第1档 涨幅20% 卖出30%")
            print("     第2档 涨幅40% 卖出30%")
            print("     第3档 涨幅60% 卖出20%")
            print("     第4档 涨幅80% 卖出10%")
            print("     第5档 涨幅100% 卖出10%")
            print("总计卖出比例需要等于100%\n")

            sell_strategy = []
            total_sell_percent = 0  # 记录总卖出比例
            
            for i in range(5):
                # 检查剩余可卖出比例
                remaining_percent = 100 - total_sell_percent
                if remaining_percent <= 0:
                    print("\n已设置完所有卖出比例")
                    break
                    
                print(f"\n第{i+1}档止盈设置:")
                print(f"当前已设置卖出比例: {total_sell_percent}%")
                print(f"剩余可设置比例: {remaining_percent}%")
                
                print(f"请输入涨幅百分比 (例如: 输入20表示20%)")
                profit_percent = float(input(f"涨幅百分比: ").strip())
                print(f"请输入卖出百分比 (最多可设置 {remaining_percent}%)")
                while True:
                    sell_percent = float(input(f"卖出百分比: ").strip())
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
            server_time = await asyncio.to_thread(self.query_client.fetch_time)
            current_time = datetime.fromtimestamp(server_time/1000, self.timezone)
            print(f"\n当前币安服务器时间: {current_time.strftime('%Y-%m-%d %H:%M:%S')} (东八区)")
            
            # 在 "# ... 时间设置部分保持不变 ..." 处添加以下代码
            while True:
                try:
                    year = int(input("年 (例如 2025): ").strip())
                    if year < 2025 or year > 2100:
                        print("年份无效，请输入2024-2100之间的年份")
                        continue
                        
                    month = int(input("月 (1-12): ").strip())
                    if month < 1 or month > 12:
                        print("月份必须在1-12之间")
                        continue
                        
                    day = int(input("日 (1-31): ").strip())
                    if day < 1 or day > 31:
                        print("日期必须在1-31之间")
                        continue
                        
                    hour = int(input("时 (0-23): ").strip())
                    if hour < 0 or hour > 23:
                        print("小时必须在0-23之间")
                        continue
                        
                    minute = int(input("分 (0-59): ").strip())
                    if minute < 0 or minute > 59:
                        print("分钟必须在0-59之间")
                        continue
                        
                    second = int(input("秒 (0-59): ").strip())
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
            if await self.save_strategy_async():
                print("\n策略已保存到配置文件")
            else:
                print("\n警告: 策略保存失败")
            
            return True
            
        except Exception as e:
            self.logger.error(f"设置抢购策略失败: {str(e)}")
            return False

    async def cancel_all_orders(self, symbol: str) -> int:
        """撤销所有当前订单"""
        try:
            orders = await self.trade_client.fetch_open_orders(symbol)
            canceled = 0
            for order in orders:
                await self.trade_client.cancel_order(order['id'], symbol)
                canceled += 1
                self.logger.info(f"已撤销订单: {order['id']}")
            return canceled
        except Exception as e:
            self.logger.error(f"撤单失败: {str(e)}")
            return 0

    def setup_trading_pair(self, symbol: str, amount: float):
        """设置交易对和数量"""
        self.symbol = symbol
        self.amount = amount
        self.logger.info(f"设置交易对: {symbol}, 数量: {amount}")

# =============================== 
# 模块：main 主程序
# ===============================

async def main():
    """主程序入口"""
    try:
        # 设置配置文件路径
        config_file = os.path.join(CONFIG_DIR, 'config.json')
        
        # 创建 ConfigManager 实例时传入 config_file 参数
        config = ConfigManager(config_file)
        sniper = BinanceSniper(config)
        
        while True:
            print("\n=== 币安现货抢币工具 ===")
            print("1. 设置API密钥")
            print("2. 抢开盘策略设置")
            print("3. 查看当前策略")
            print("4. 开始抢购")
            print("5. 测试中心") 
            print("0. 退出")
            print("=====================")
            
            choice = input("\n请选择操作 (0-5): ").strip()
            
            if choice == '0':
                print("感谢使用，再见!")
                break
            elif choice == '1':
                # 修改这里:使用异步方法
                await sniper.setup_api_keys_async()  
            elif choice == '2':
                await sniper.setup_snipe_strategy_async()
            elif choice == '3':
                # 先加载策略
                if await sniper.load_strategy_async():  # 添加这行
                    await sniper.print_strategy_async()
                else:
                    print("加载策略失败，请先设置策略")
            elif choice == '4':
                if not await sniper._init_clients():
                    print("请先配置API密钥")
                    continue
                    
                if not await sniper._init_snipe():
                    print("初始化失败")
                    continue
                    
                result = await sniper.execute_snipe()
                if result:
                    print("\n✅ 抢购成功!")
                    print(f"订单ID: {result['id']}")
                    print(f"成交价格: {result['price']}")
                    print(f"成交数量: {result['filled']}")
                else:
                    print("\n❌ 抢购失败")
                    
            elif choice == '5':
                await sniper.test_center()
            else:
                print("无效的选择，请重新输入")
                
    except Exception as e:
        logger.error(f"程序运行出错: {str(e)}")
    finally:
        if 'sniper' in locals():
            await sniper.cleanup()

# 在文件开头的导入部分添加或修改
class DepthAnalyzer:
    """深度分析器"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)

if __name__ == '__main__':
    try:
        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 注册信号处理
        def signal_handler():
            print("\n收到退出信号，正在清理...")
            tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            [task.cancel() for task in tasks]
            
            loop.stop()
            loop.close()
            print("程序已安全退出")
            sys.exit(0)
            
        # 注册CTRL+C信号处理
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, signal_handler)
            
        # 运行主程序
        loop.run_until_complete(main())
        
    except Exception as e:
        print(f"程序异常退出: {str(e)}")
    finally:
        # 清理事件循环
        if loop.is_running():
            loop.close()

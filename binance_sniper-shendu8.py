import time
import logging
from logging.handlers import RotatingFileHandler  # 添加这个导入
import asyncio
from typing import (
    Dict, Optional, List, TypeVar, Tuple, Any, Union, Type,
    Callable, Awaitable, Coroutine, Set, DefaultDict
)  # 补充完整的 typing 导入
from types import TracebackType  # 添加 TracebackType 导入
from concurrent.futures import ThreadPoolExecutor
import statistics
import ccxt
import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector
import prometheus_client as prom
from collections import defaultdict, deque
import psutil
import pytz
from datetime import datetime, timedelta, timezone  # 补充 timedelta 和 timezone
import sys
import os
import json  # 添加 json 模块导入
import configparser  # 添加 configparser 模块导入
import base64  # 添加 base64 模块导入
from functools import wraps, partial  # 添加装饰器支持
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import RequestException, Timeout
from urllib3.util.retry import Retry  # 添加 Retry
import websocket  # 添加 websocket 支持
import threading  # 添加 threading 支持
from threading import Lock, Event
from cryptography.hazmat.primitives.serialization import load_pem_private_key  # 添加加密支持
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from decimal import Decimal  # 用于精确计算
import uuid  # 用于生成唯一ID
import signal  # 用于信号处理
import gc  # 用于垃圾回收控制
import contextlib  # 用于上下文管理
import weakref  # 用于弱引用
import inspect  # 用于运行时检查
import socket
import subprocess
import netifaces
import websockets

# 获取当前脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 设置相对于脚本目录的路径
LOG_DIR = os.path.join(SCRIPT_DIR, 'logs')
CONFIG_DIR = os.path.join(SCRIPT_DIR, 'config')

# 确保目录存在
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

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

# 在 SniperError 相关类之后，PreciseWaiter 类之前添加 IPManager 类
class IPManager:
    """IP管理器 - 处理多IP轮换、监控和故障切换"""
    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.interface = self._get_default_interface()
        self.ips = self._get_interface_ips()
        self.ip_stats = {}
        for ip in self.ips:
            self.ip_stats[ip] = {
                "requests": 0,          # 请求计数
                "errors": 0,            # 错误计数
                "last_used": 0,         # 最后使用时间
                "latency": float('inf'), # 当前延迟
                "ws_connected": False,   # WebSocket连接状态
                "last_ws_msg": 0,       # 最后WS消息时间
                "window_start": 0,      # 时间窗口开始
                "window_end": 0         # 时间窗口结束
            }
        
        # 为每个IP分配时间窗口
        for i, ip in enumerate(self.ips):
            window_start = i * 20  # 0, 20, 40
            window_end = window_start + 20  # 20, 40, 60
            self.ip_stats[ip]['window_start'] = window_start
            self.ip_stats[ip]['window_end'] = window_end

        self.current_ip_index = 0
        self.latency_threshold = 1000  # 延迟阈值(ms)
        self.error_threshold = 5       # 错误阈值
        self.lock = asyncio.Lock()     # 用于并发控制
        self.scheduler = RequestScheduler()  # 请求调度器

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

    async def get_best_ip(self) -> str:
        """获取当前最佳IP"""
        current_ms = int(time.time() * 1000) % 60
        return await self.get_ip_for_window(current_ms)

    async def report_error(self, ip: str):
        """报告IP错误"""
        await self.update_ip_status(ip, float('inf'), error=True)

    async def get_ip_for_window(self, current_ms: int) -> str:
        """获取当前时间窗口的最佳IP"""
        async with self.lock:
            for ip in self.ips:
                stats = self.ip_stats[ip]
                if (stats['window_start'] <= current_ms < stats['window_end'] and 
                    stats['errors'] < self.error_threshold):
                    return ip
            # 如果没有合适的IP，返回延迟最低的
            return min(self.ips, key=lambda x: self.ip_stats[x]['latency'])

    async def update_ip_status(self, ip: str, latency: float, error: bool = False):
        """更新IP状态"""
        async with self.lock:
            stats = self.ip_stats[ip]
            stats['latency'] = latency
            stats['last_used'] = time.time()
            if error:
                stats['errors'] += 1
                if stats['errors'] >= self.error_threshold:
                    self.logger.warning(f"IP {ip} 错误次数过多，将被临时禁用")

    async def get_next_ip(self):
        """获取下一个可用IP"""
        async with self.lock:
            # 按时间窗口选择IP
            current_second = int(time.time()) % 60
            for ip in self.ips:
                if (self.ip_stats[ip]['window_start'] <= current_second < 
                    self.ip_stats[ip]['window_end']):
                    return ip
            
            # 如果没有找到合适的IP，使用轮询方式
            self.current_ip_index = (self.current_ip_index + 1) % len(self.ips)
            return self.ips[self.current_ip_index]

    async def get_best_ip(self):
        """获取当前最佳IP"""
        best_ip = None
        best_score = float('inf')
        
        for ip in self.ips:
            stats = self.ip_stats[ip]
            # 计算综合得分 (越低越好)
            score = (
                stats['latency'] * 0.4 +  # 延迟权重
                (stats['errors'] / max(stats['requests'], 1)) * 100 * 0.3 +  # 错误率权重
                (time.time() - stats['last_used']) * 0.3  # 负载均衡权重
            )
            
            if score < best_score:
                best_score = score
                best_ip = ip
        
        return best_ip or self.ips[0]

class RequestScheduler:
    """请求调度器 - 实现精确的时间窗口和请求控制"""
    def __init__(self):
        self.last_request_time = {}
        
        # 优化请求间隔
        self.request_intervals = {
            'depth': 5,      # 降低到5ms (原来10ms)
            'price': 7,      # 降低到7ms (原来15ms)
            'book': 3        # 保持3ms
        }
        
        # 优化时间窗口，缩小窗口提高并发
        self.request_windows = {
            'ip1': (0, 15),     # 0-15ms窗口  (原来0-20ms)
            'ip2': (15, 30),    # 15-30ms窗口 (原来20-40ms)
            'ip3': (30, 45)     # 30-45ms窗口 (原来40-60ms)
        }
        
        # 增加配额限制，避免触发币安限制
        self.request_quotas = {
            'ip1': {
                'used': 0,
                'limit': 1100,  # 留100个请求的安全边际
                'depth_count': 0,
                'price_count': 0,
                'book_count': 0,
                'last_reset': time.time()
            },
            'ip2': {
                'used': 0,
                'limit': 1100,
                'depth_count': 0,
                'price_count': 0,
                'book_count': 0,
                'last_reset': time.time()
            },
            'ip3': {
                'used': 0,
                'limit': 1100,
                'depth_count': 0,
                'price_count': 0,
                'book_count': 0,
                'last_reset': time.time()
            }
        }
        
        self.lock = asyncio.Lock()
        self.last_reset_time = time.time()
        
        # 添加高频模式标志
        self.high_freq_mode = False
        self.high_freq_start_time = 0
        self.HIGH_FREQ_DURATION = 10  # 高频模式持续10秒

    async def enable_high_freq_mode(self):
        """启用高频模式 - 用于开盘前后的关键时期"""
        self.high_freq_mode = True
        self.high_freq_start_time = time.time()
        
        # 临时调整请求间隔
        self.request_intervals = {
            'depth': 3,    # 极限3ms
            'price': 4,    # 极限4ms
            'book': 3      # 保持3ms
        }
        
        self.logger.info("已启用高频模式")

    async def disable_high_freq_mode(self):
        """禁用高频模式 - 恢复正常请求频率"""
        self.high_freq_mode = False
        
        # 恢复正常请求间隔
        self.request_intervals = {
            'depth': 5,    # 正常5ms
            'price': 7,    # 正常7ms
            'book': 3      # 保持3ms
        }
        
        self.logger.info("已恢复正常模式")

    async def can_make_request(self, ip: str, request_type: str) -> bool:
        """检查是否可以发送请求"""
        async with self.lock:
            # 检查是否需要退出高频模式
            if (self.high_freq_mode and 
                time.time() - self.high_freq_start_time > self.HIGH_FREQ_DURATION):
                await self.disable_high_freq_mode()
            
            # 检查配额
            quota = self.request_quotas[ip]
            if quota['used'] >= quota['limit']:
                return False
            
            # 获取当前毫秒时间
            current_ms = int(time.time() * 1000) % 45  # 改为45ms循环
            window_start, window_end = self.request_windows[ip]
            
            # 检查是否在时间窗口内
            if not (window_start <= current_ms < window_end):
                return False
            
            # 检查请求间隔
            last_time = self.last_request_time.get(f"{ip}_{request_type}", 0)
            interval = self.request_intervals[request_type]
            
            if (time.time() * 1000 - last_time) < interval:
                return False
                
            return True

    async def track_request(self, ip: str, request_type: str):
        """记录请求"""
        async with self.lock:
            # 更新请求计数
            self.request_quotas[ip]['used'] += 1
            self.request_quotas[ip][f'{request_type}_count'] += 1
            self.last_request_time[f"{ip}_{request_type}"] = time.time() * 1000
            
            # 检查是否需要重置计数器
            current_time = time.time()
            if current_time - self.last_reset_time >= 60:
                await self.reset_quotas()

    async def reset_quotas(self):
        """重置请求配额"""
        async with self.lock:
            for ip in self.request_quotas:
                self.request_quotas[ip] = {
                    'used': 0,
                    'limit': 1100,
                    'depth_count': 0,
                    'price_count': 0,
                    'book_count': 0
                }
            self.last_reset_time = time.time()

    async def get_quota_status(self, ip: str) -> dict:
        """获取配额状态"""
        async with self.lock:
            return {
                'total_used': self.request_quotas[ip]['used'],
                'remaining': self.request_quotas[ip]['limit'] - self.request_quotas[ip]['used'],
                'depth_requests': self.request_quotas[ip]['depth_count'],
                'price_requests': self.request_quotas[ip]['price_count'],
                'book_requests': self.request_quotas[ip]['book_count']
            }

class BinanceWebSocketManager:
    """优化后的WebSocket管理器"""
    def __init__(self, symbol: str, logger=None):
        self.symbol = symbol.lower().replace('/', '').lower()  # 格式化交易对
        self.logger = logger or logging.getLogger(__name__)
        self.connections = {}
        self.data_callbacks = []
        self.running = True
        self.lock = asyncio.Lock()
        self.last_data = {}
        self.scheduler = RequestScheduler()
        self.latency_stats = {}
        self.LATENCY_WINDOW = 1000
        
        # 添加高频数据缓存
        self.realtime_data = {
            'bookTicker': None,
            'trade': None,
            'depth': None
        }
        
        # 添加性能统计
        self.stats = {
            'bookTicker_latency': deque(maxlen=1000),
            'trade_latency': deque(maxlen=1000),
            'message_count': 0
        }

    async def add_connection(self, ip: str):
        """为指定IP添加WebSocket连接"""
        try:
            # 正确格式化streams
            streams = [
                f"{self.symbol}@trade",         # 逐笔交易
                f"{self.symbol}@depth@100ms",   # 增量深度信息
                f"{self.symbol}@bookTicker"     # 最优挂单信息
            ]
            
            # 使用_connect_websocket建立连接
            ws = await self._connect_websocket(ip, streams)
            
            # 连接成功后的处理
            if ws:
                self.logger.info(f"WebSocket连接已建立 (IP: {ip})")
                return True
            return False
            
        except Exception as e:
            self.logger.error(f"WebSocket连接失败 (IP: {ip}): {e}")
            return False

    async def _handle_messages(self, ip: str):
        """处理WebSocket消息"""
        ws_conn = self.connections[ip]
        
        while self.running and ws_conn['active']:
            try:
                message = await ws_conn['ws'].recv()
                data = json.loads(message)
                
                # 更新最后消息时间
                ws_conn['last_msg'] = time.time()
                
                # 计算延迟
                if 'E' in data:  # 事件时间
                    receive_time = time.time() * 1000
                    event_time = data['E']
                    latency = receive_time - event_time
                    ws_conn['latency'].append(latency)
                
                # 更新消息计数
                self.stats['message_count'] += 1
                
                # 根据消息类型更新数据
                if 'e' in data:
                    stream_type = data['e']
                    if stream_type == 'trade':
                        self.realtime_data['trade'] = data
                    elif stream_type == 'depthUpdate':
                        self.realtime_data['depth'] = data
                    elif stream_type == 'bookTicker':
                        self.realtime_data['bookTicker'] = data
                
                # 回调处理
                for callback in self.data_callbacks:
                    try:
                        await callback(data)
                    except Exception as e:
                        self.logger.error(f"回调处理错误: {e}")
                
            except Exception as e:
                self.logger.error(f"WebSocket消息处理错误 (IP: {ip}): {str(e)}")
                ws_conn['active'] = False
                break

    async def _monitor_connection(self, ip: str):
        """监控连接状态"""
        while self.running:
            try:
                conn = self.connections.get(ip)
                if not conn:
                    break
                    
                current_time = time.time()
                if current_time - conn['last_msg'] > 30:
                    self.logger.warning(f"WebSocket连接超时 (IP: {ip})")
                    conn['active'] = False
                    await self._attempt_reconnect(ip)
                    
                if conn['latency']:
                    avg_latency = sum(conn['latency'])/len(conn['latency'])
                    if avg_latency > 1000:
                        self.logger.warning(f"WebSocket延迟过高 (IP: {ip}): {avg_latency:.2f}ms")
                        
                await asyncio.sleep(0.001)
                
            except Exception as e:
                self.logger.error(f"连接监控错误 (IP: {ip}): {e}")
                await asyncio.sleep(0.001)

    async def get_best_connection(self) -> Optional[str]:
        """获取最佳连接"""
        best_ip = None
        min_latency = float('inf')
        
        async with self.lock:
            for ip, conn in self.connections.items():
                if not conn['active'] or not conn['latency']:
                    continue
                    
                avg_latency = sum(conn['latency'])/len(conn['latency'])
                if avg_latency < min_latency:
                    min_latency = avg_latency
                    best_ip = ip
                    
        return best_ip

    async def add_callback(self, callback: Callable):
        """添加数据处理回调"""
        if callback not in self.data_callbacks:
            self.data_callbacks.append(callback)

    async def remove_callback(self, callback: Callable):
        """移除数据处理回调"""
        if callback in self.data_callbacks:
            self.data_callbacks.remove(callback)

    async def close_connection(self, ip: str):
        """关闭指定IP的连接"""
        if ip in self.connections:
            try:
                conn = self.connections[ip]
                conn['active'] = False
                await conn['ws'].close()
                del self.connections[ip]
            except Exception as e:
                self.logger.error(f"关闭WebSocket连接失败 (IP: {ip}): {e}")

    async def close(self):
        """关闭所有连接"""
        self.running = False
        tasks = []
        for ip in list(self.connections.keys()):
            tasks.append(self.close_connection(ip))
        if tasks:
            await asyncio.gather(*tasks)
        self.connections.clear()
        self.data_callbacks.clear()
        self.last_data.clear()

    async def _attempt_reconnect(self, ip: str):
        """尝试重新连接"""
        try:
            self.logger.info(f"尝试重新连接 WebSocket (IP: {ip})")
            await self.close_connection(ip)
            await asyncio.sleep(1)  # 等待1秒
            await self.add_connection(ip)
            return True
        except Exception as e:
            self.logger.error(f"WebSocket重连失败 (IP: {ip}): {e}")
            return False

    async def _connect_websocket(self, ip, streams):
        try:
            # 修改 streams 格式
            formatted_streams = []
            for stream in streams:
                if '@' not in stream:  # 如果stream中没有@符号，说明需要格式化
                    formatted_streams.append(f"{stream.lower()}@trade")
                else:
                    formatted_streams.append(stream.lower())
                
            url = f"wss://stream.binance.com:9443/ws"  # 修改为基础URL
            
            # 建立 WebSocket 连接
            ws = await websockets.connect(
                url,
                origin="https://stream.binance.com",
                host="stream.binance.com",
                local_addr=(ip, 0),
                ssl=True,
                ping_interval=20,
                ping_timeout=60,
                compression=None  # 禁用压缩以避免潜在问题
            )
            
            self.connections[ip] = {
                'ws': ws,
                'last_msg': time.time(),
                'active': True,
                'latency': deque(maxlen=self.LATENCY_WINDOW)
            }
            
            # 修改订阅消息格式
            subscribe_msg = {
                "method": "SUBSCRIBE",
                "params": formatted_streams,
                "id": int(time.time() * 1000)
            }
            
            # 发送订阅消息
            await ws.send(json.dumps(subscribe_msg))
            
            # 等待并验证订阅确认
            while True:
                response = await ws.recv()
                response_data = json.loads(response)
                
                # 检查是否是订阅确认消息
                if 'id' in response_data:
                    if response_data.get('result') is None:
                        asyncio.create_task(self._handle_messages(ip))
                        asyncio.create_task(self._monitor_connection(ip))
                        self.logger.info(f"WebSocket连接已建立并订阅成功 (IP: {ip})")
                        break
                    else:
                        error_msg = response_data.get('error', {}).get('msg', '未知错误')
                        raise Exception(f"订阅失败: {error_msg}")
                else:
                    # 如果收到的是数据消息而不是订阅确认，继续等待
                    continue

        except Exception as e:
            self.logger.error(f"WebSocket连接或订阅失败 (IP: {ip}): {str(e)}")
            if ip in self.connections:
                self.connections[ip]['active'] = False
                if 'ws' in self.connections[ip]:
                    try:
                        await self.connections[ip]['ws'].close()
                    except:
                        pass
                del self.connections[ip]  # 删除失败的连接
            raise

        return ws

def setup_logger():
    """配置日志系统"""
    logger = logging.getLogger('BinanceSniper')
    logger.setLevel(logging.DEBUG)
    
    # 自定义格式化器
    class CustomFormatter(logging.Formatter):
        def format(self, record):
            # 设置时间格式
            record.asctime = self.formatTime(record, datefmt='%Y-%m-%d %H:%M:%S')
            # 自定义格式
            return f"[{record.asctime}] [{record.levelname}] {record.getMessage()}"
    
    # 格式化器
    formatter = CustomFormatter()
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    
    # 文件处理器 (按大小轮转)
    log_file = os.path.join(LOG_DIR, 'binance_sniper.log')
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
        self.network_latency = None
        self.time_offset = None
        self.last_sync_time = 0
        self.sync_interval = 30  # 改为30秒同步一次
        self.min_samples = 5
        self.max_samples = 10
        self._server_time = 0  # 添加服务器时间缓存
        
    async def get_server_time(self) -> int:
        """获取币安服务器时间"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.binance.com/api/v3/time', timeout=2) as response:
                    if response.status == 200:
                        result = await response.json()
                        return result['serverTime']  # 毫秒时间戳
        except Exception as e:
            logger.error(f"获取服务器时间失败: {str(e)}")
            return None

    async def sync(self) -> bool:
        """同步时间"""
        try:
            measurements = []
            
            # 收集多个样本
            for _ in range(self.max_samples):
                start = time.perf_counter_ns()
                server_time = await self.get_server_time()
                if not server_time:
                    continue
                    
                end = time.perf_counter_ns()
                
                rtt = (end - start) / 1e6  # 转换为毫秒
                latency = rtt / 2
                
                measurements.append({
                    'server_time': server_time,
                    'latency': latency,
                    'rtt': rtt
                })
                
                await asyncio.sleep(0.1)
            
            # 过滤异常值
            filtered = self._filter_outliers(measurements)
            if len(filtered) < self.min_samples:
                return False
                
            # 使用中位数
            self.network_latency = statistics.median(m['latency'] for m in filtered)
            self._server_time = statistics.median(m['server_time'] for m in filtered)
            self.last_sync_time = self._server_time
            
            # 修改日志输出，使用本地时间
            logger.info(f"""
[时间同步] 
• 本地时间: {format_local_time(self._server_time)}
• 网络延迟: {self.network_latency:.2f}ms
• 样本数量: {len(filtered)}
""")
            
            return True
            
        except Exception as e:
            logger.error(f"时间同步失败: {str(e)}")
            return False
    
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
        self.session = None
        self.base_url = 'https://api.binance.com'
        self.depth_path = '/api/v3/depth'
        self._cached_params = {}
        self._connector = None
        
    async def _init_session(self):
        """初始化会话，优化连接设置"""
        if self.session is None:
            # 1. 优化连接设置
            self._connector = aiohttp.TCPConnector(
                force_close=False,  # 改为保持连接
                ttl_dns_cache=300,  # DNS缓存5分钟
                use_dns_cache=True,
                limit=1,  # 限制并发连接数为1
                enable_cleanup_closed=True,  # 自动清理关闭的连接
                tcp_nodelay=True,  # 启用 TCP_NODELAY
                keepalive_timeout=30  # 连接保持30秒
            )
            
            # 2. 优化超时设置
            timeout = aiohttp.ClientTimeout(
                total=0.5,      # 总超时500ms
                connect=0.1,    # 连接超时100ms
                sock_read=0.2   # 读取超时200ms
            )
            
            # 3. 创建会话
            self.session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=timeout,
                headers={
                    'Accept': 'application/json',
                    'User-Agent': 'aiohttp/3.8.1',
                    'Connection': 'keep-alive'
                }
            )
            
    def _prepare_params(self, symbol: str, limit: int = 5):
        """预处理请求参数"""
        cache_key = f"{symbol}:{limit}"
        if cache_key not in self._cached_params:
            self._cached_params[cache_key] = {
                'symbol': symbol.replace('/', ''),
                'limit': limit
            }
        return self._cached_params[cache_key]

    async def analyze_depth(self, client, symbol: str, limit: int = 5):
        """分析市场深度"""
        try:
            await self._init_session()
            params = self._prepare_params(symbol, limit)
            
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
            logger.error(f"分析市场深度失败: {str(e)}")
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
    """配置管理器"""
    def __init__(self):
        # 设置配置目录
        self.config_dir = CONFIG_DIR  # 使用全局定义的 CONFIG_DIR
        self.config_file = os.path.join(self.config_dir, 'config.json')
        
        # 初始化配置解析器
        self.config = configparser.ConfigParser()
        
        # 加载现有配置或创建新配置
        if os.path.exists(self.config_file):
            self.config.read(self.config_file)
        else:
            self._create_default_config()
            
        # 确保配置目录存在
        os.makedirs(self.config_dir, exist_ok=True)
    
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
        """初始化币安抢币工具
        Args:
            config: 配置管理器实例
        """
        # 1. 首先初始化logger
        self.logger = setup_logger()
        
        # 2. 基础配置
        self.config = config
        self.trade_client = None
        self.query_client = None
        
        # 3. 设置时区
        self.timezone = pytz.timezone('Asia/Shanghai')
        
        # 4. 基础变量
        self.symbol = None
        self.amount = None
        self.price = None
        self.sell_strategy = None
        self.market_cache = None
        
        # 5. 初始化基本组件
        self._init_basic_components()
        
        # 6. 初始化依赖logger的组件
        self.ip_manager = IPManager(self.logger)
        self.market_data_cache = {}
        self.cache_ttl = 0.02  # 20ms缓存
        
        # 7. WebSocket相关
        self.websocket = None
        self.order_update_queue = asyncio.Queue()
        self.price_update_queue = asyncio.Queue()
        self.ws_connections = {}
        self.last_rest_request = {}
        self.ws_manager = None
        
        # 8. 性能监控
        self.perf = PerformanceAnalyzer()
        
        # 9. 资源跟踪
        self._resources = set()
        
        # 10. 添加性能测试相关的基础配置
        self.request_interval = 0.05  # 50ms 默认请求间隔
        self.max_retries = 3         # 默认重试次数
        self.timeout = 5.0           # 默认超时时间(秒)
        
        # 11. 请求限制配置
        self.rate_limits = {
            'weight': 1200,          # 每分钟权重限制
            'orders': 50,            # 每10秒订单限制
            'request_window': 60     # 时间窗口(秒)
        }
        
        self.logger.info("BinanceSniper初始化完成")

        # 添加时间同步相关属性
        self.server_time_offset = 0  # 与币安服务器的时间差(ms)
        self.last_time_sync = 0      # 上次同步时间
        self.time_sync_interval = 1   # 同步间隔(秒)
        self.time_samples = deque(maxlen=100)  # 保存最近100次时间同步样本

        # 添加市场数据监控相关的属性
        self.market_status = {
            'symbol_active': False,
            'has_orderbook': False,
            'has_trades': False,
            'last_update': 0
        }

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
                
                self.logger.info("API客户端初始化成功")
        except Exception as e:
            self.logger.warning(f"初始化基本组件时出错: {str(e)}")
        
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
                permissions = balance.get('info', {}).get('permissions', ['unknown'])
                
                logger.info(f"""
[API测试] 
✅ 交易API
• 连接状态: 正常
• USDT余额: {format_number(self.balance, 6)}
• API限额: {self.trade_client.rateLimit}次/分钟
• 账户权限: {', '.join(permissions)}
""")
            except Exception as e:
                logger.error(f"交易API测试失败: {str(e)}")
                return False

            # 3. 测试查询API连接
            try:
                start_time = time.time()
                server_time = self.query_client.fetch_time()
                latency = (time.time() - start_time) * 1000
                time_offset = server_time - int(time.time() * 1000)
                
                logger.info(f"""
✅ 查询API
• 连接状态: 正常
• 网络延迟: {format_number(latency, 2)}ms
• 时间偏移: {format_number(time_offset, 2)}ms
""")
            except Exception as e:
                logger.error(f"查询API测试失败: {str(e)}")
                return False

            # 4. 检查市场状态
            try:
                logger.info(f"""
ℹ️ 市场状态
• 交易对: {self.symbol} (未上线)
• 状态: 等待开盘
• 备注: 新币抢购模式
""")
            except Exception as e:
                logger.warning(f"市场状态检查失败: {str(e)}")

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

    async def prepare_and_snipe_async(self):
        """异步抢购实现"""
        try:
            # 异步网络测试
            print("\n正在测试网络状态...")
            network_stats = await self._measure_network_stats_async()
            if not network_stats:
                print("网络状态测试失败，请检查网络连接")
                return

            print("\n=== 开始网络测试（持续5分钟）===")
            test_results = []
            test_start = time.time()
            
            # 第一阶段：常规网络测试（5分钟）
            while time.time() - test_start < 300:  # 测试5分钟
                stats = await self._measure_network_stats_async(5)
                if stats:
                    test_results.append(stats)
                    print(f"\r当前网络状态: 延迟 {stats['latency']:.2f}ms (波动: ±{stats['jitter']/2:.2f}ms) 偏移: {stats['offset']:.2f}ms", end='', flush=True)
                await asyncio.sleep(30)  # 每30秒测试一次

            print("\n=== 初始网络测试完成 ===")
            
            # 开始执行抢购
            print("\n=== 开始执行抢购任务 ===")
            result = await self.execute_snipe()
            
            # 显示结果
            if result:
                print(f"""
=== 抢购成功 ===
订单ID: {result['id']}
成交价格: {format_number(result['average'])} USDT
成交数量: {format_number(result['filled'])}
成交金额: {format_number(result['cost'])} USDT
""")
            else:
                print("\n抢购未成功")
                
            return result
            
        except Exception as e:
            logger.error(f"抢购任务执行失败: {str(e)}")
            print(f"\n抢购任务执行失败: {str(e)}")
            return None
        finally:
            # 确保资源被正确清理
            await self.cleanup()

    async def cleanup(self):
        """异步清理资源"""
        try:
            logger.info("开始清理资源...")
            
            # 清理交易客户端
            if hasattr(self, 'trade_client') and self.trade_client:
                await asyncio.to_thread(self.trade_client.close)
                logger.info("交易客户端已清理")
            
            # 清理查询客户端
            if hasattr(self, 'query_client') and self.query_client:
                await asyncio.to_thread(self.query_client.close)
                logger.info("查询客户端已清理")
            
            logger.info("资源清理完成")
            
        except Exception as e:
            logger.error(f"清理资源失败: {str(e)}")

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
            
            # 加载API配置(同步方法)
            if not self.load_api_keys():
                print("API配置加载失败，请检查配置")
                return
            
            # 0. 加载策略配置
            if not self.load_strategy():
                print("请先设置抢购策略（选项2）")
                return
            
            # 1. 初始化交易系统(同步方法)
            if not self._init_clients():
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

            print(f"""
=== 抢购配置确认 ===
交易对: {self.symbol}
买入金额: {self.amount} USDT
开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S')}
距离开盘: {time_to_open/3600:.1f}小时
""")

            confirm = input("确认开始抢购? (yes/no): ")
            if confirm.lower() != 'yes':
                print("已取消抢购")
                return

            # 3. 创建事件循环用于异步操作
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                # 4. 执行异步抢购
                return loop.run_until_complete(self.prepare_and_snipe_async())
            finally:
                # 清理资源
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
            
            # 加载API配置(同步方法)
            if not self.load_api_keys():
                print("API配置加载失败，请检查配置")
                return
            
            # 0. 加载策略配置
            if not self.load_strategy():
                print("请先设置抢购策略（选项2）")
                return
            
            # 1. 初始化交易系统(同步方法)
            if not self._init_clients():
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

            print(f"""
=== 抢购配置确认 ===
交易对: {self.symbol}
买入金额: {self.amount} USDT
开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S')}
距离开盘: {time_to_open/3600:.1f}小时
""")

            confirm = input("确认开始抢购? (yes/no): ")
            if confirm.lower() != 'yes':
                print("已取消抢购")
                return

            # 3. 创建事件循环用于异步操作
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                # 4. 执行异步抢购
                return loop.run_until_complete(self.prepare_and_snipe_async())
            finally:
                # 清理资源
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
        if not self.ws_manager:
            self.ws_manager = BinanceWebSocketManager(self.symbol, self.logger)
            # 为每个可用IP添加连接
            for ip in self.ip_manager.get_available_ips():
                await self.ws_manager.add_connection(ip)
        return True
    
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
            logger.info("开始执行抢购任务...")
            
            while True:  # 持续运行
                current_time = datetime.now(self.timezone)
                time_to_open = (self.opening_time - current_time).total_seconds()
                
                if time_to_open <= 0:
                    logger.info("已到达开盘时间，准备执行抢购")
                    break
                    
                # 显示等待状态
                hours = int(time_to_open // 3600)
                minutes = int((time_to_open % 3600) // 60)
                seconds = int(time_to_open % 60)
                
                print(f"\r等待开盘中... 剩余时间: {hours:02d}:{minutes:02d}:{seconds:02d}", end='', flush=True)
                
                # 每秒更新一次状态
                await asyncio.sleep(1)
                
                # 如果剩余时间小于5分钟，进入准备阶段
                if time_to_open <= 300:
                    print("\n\n=== 进入最后5分钟准备阶段 ===")
                    logger.info("进入最后5分钟准备阶段")
                    
                    # 最后5分钟的网络测试
                    print("\n开始最后网络状态检测...")
                    final_test_results = []
                    test_start = time.time()
                    
                    # 密集测试30秒
                    while time.time() - test_start < 30:
                        stats = await self._measure_network_stats_async(3)  # 每次测3个样本
                        if stats:
                            final_test_results.append(stats)
                            print(f"\r最终网络状态: 延迟 {stats['latency']:.2f}ms (波动: ±{stats['jitter']/2:.2f}ms) 偏移: {stats['offset']:.2f}ms", end='', flush=True)
                        await asyncio.sleep(1)  # 每秒测一次
                    
                    # 计算最终网络状态平均值
                    if final_test_results:
                        avg_latency = statistics.mean(d['latency'] for d in final_test_results)
                        avg_jitter = statistics.mean(d['jitter'] for d in final_test_results)
                        avg_offset = statistics.mean(d['offset'] for d in final_test_results)
                        
                        print(f"\n\n最终网络状态汇总:")
                        print(f"平均延迟: {avg_latency:.2f}ms")
                        print(f"平均波动: ±{avg_jitter/2:.2f}ms")
                        print(f"平均偏移: {avg_offset:.2f}ms")
                        
                        # 如果网络状态不好，给出警告
                        if avg_latency > 50 or abs(avg_offset) > 100:
                            print("\n⚠️ 警告: 网络状态不佳，可能影响抢购成功率")
                    
                    break
            
            # 最后5分钟的精确等待
            if time_to_open > 0:
                logger.info("开始精确等待...")
                target_time = self.opening_time.timestamp() * 1000 - self.advance_time
                await self.waiter.wait_until(target_time)
            
            # 执行抢购订单
            logger.info("开始执行抢购订单...")
            orders = []
            for i in range(self.concurrent_orders):
                try:
                    order = await asyncio.to_thread(
                        self.trade_client.create_market_buy_order,
                        symbol=self.symbol,
                        amount=self.amount/self.concurrent_orders
                    )
                    orders.append(order)
                    logger.info(f"订单 {i+1} 执行成功: {order['id']}")
                except Exception as e:
                    logger.error(f"订单 {i+1} 执行失败: {str(e)}")
            
            # 检查订单结果
            successful_orders = [order for order in orders if order and order.get('status') == 'closed']
            if successful_orders:
                total_filled = sum(order['filled'] for order in successful_orders)
                avg_price = sum(order['cost'] for order in successful_orders) / total_filled if total_filled else 0
                
                result = {
                    'id': successful_orders[0]['id'],
                    'average': avg_price,
                    'filled': total_filled,
                    'cost': total_filled * avg_price
                }
                logger.info(f"抢购成功: {result}")
                return result
            else:
                logger.error("所有订单均执行失败")
                return None
                
        except Exception as e:
            logger.error(f"抢购执行失败: {str(e)}")
            return None

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
            
            # 2. 获取实时市场数据(如果可用)
            current_price = 0
            price_24h_change = 0
            volume_24h = 0
            market_status = "未上市"
            
            try:
                ticker = self.query_client.fetch_ticker(self.symbol)
                current_price = ticker['last']
                price_24h_change = ticker['percentage']
                volume_24h = ticker['quoteVolume']
                market_status = "已上市"
            except Exception as e:
                logger.info(f"获取市场数据失败(币种未上市): {str(e)}")

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
- 买入数量: {"待定" if current_price == 0 else f"{self.amount/current_price:.8f} {self.symbol.split('/')[0]}"}

📊 市场信息
- 币种状态: {market_status}
- 当前价格: {"未上市" if current_price == 0 else f"{current_price} USDT"}
- 24h涨跌: {"未上市" if current_price == 0 else f"{price_24h_change:.2f}%"}
- 24h成交: {"未上市" if current_price == 0 else f"{volume_24h:.2f} USDT"}

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
                
                logger.info(f"""
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
                permissions = balance.get('info', {}).get('permissions', ['unknown'])
                
                logger.info(f"""
[API测试] 
✅ 交易API
• 连接状态: 正常
• USDT余额: {format_number(self.balance, 6)}
• API限额: {self.trade_client.rateLimit}次/分钟
• 账户权限: {', '.join(permissions)}
""")
            except Exception as e:
                logger.error(f"交易API测试失败: {str(e)}")
                return False

            # 3. 测试查询API连接
            try:
                start_time = time.time()
                server_time = self.query_client.fetch_time()
                latency = (time.time() - start_time) * 1000
                time_offset = server_time - int(time.time() * 1000)
                
                logger.info(f"""
✅ 查询API
• 连接状态: 正常
• 网络延迟: {format_number(latency, 2)}ms
• 时间偏移: {format_number(time_offset, 2)}ms
""")
            except Exception as e:
                logger.error(f"查询API测试失败: {str(e)}")
                return False

            # 4. 检查市场状态
            try:
                logger.info(f"""
ℹ️ 市场状态
• 交易对: {self.symbol} (未上线)
• 状态: 等待开盘
• 备注: 新币抢购模式
""")
            except Exception as e:
                logger.warning(f"市场状态检查失败: {str(e)}")

            return True
        
        except Exception as e:
            logger.error(f"初始化客户端失败: {str(e)}")
            return False

    async def _measure_network_stats_async(self, samples: int = 5) -> Optional[Dict[str, float]]:
        """异步测量网络状态"""
        try:
            latencies = []
            offsets = []
            
            for _ in range(samples):
                start_time = time.time() * 1000
                # 使用 asyncio.to_thread 包装同步调用
                server_time = await asyncio.to_thread(self.query_client.fetch_time)
                end_time = time.time() * 1000
                
                latency = end_time - start_time
                offset = server_time - start_time
                
                latencies.append(latency)
                offsets.append(offset)
                
                await asyncio.sleep(0.1)
                
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

    async def _make_request(self, url: str, method: str = "GET", **kwargs):
        """优化的API请求方法"""
        cache_key = f"{url}_{kwargs.get('params', '')}"
        current_time = time.time()
        
        # 检查缓存
        if cache_key in self.market_data_cache:
            cache_data = self.market_data_cache[cache_key]
            if current_time - cache_data['timestamp'] < self.cache_ttl:
                return cache_data['data']

        # 获取最佳IP并检查请求限制
        ip = await self.ip_manager.get_best_ip()
        if ip not in self.last_rest_request:
            self.last_rest_request[ip] = []
            
        # 清理旧的请求记录
        self.last_rest_request[ip] = [t for t in self.last_rest_request[ip] 
                                    if current_time - t < 60]
                                    
        # 检查是否超过限制
        if len(self.last_rest_request[ip]) >= 1100:
            self.logger.warning(f"IP {ip} 接近请求限制，等待重置")
            await asyncio.sleep(0.1)
            
        try:
            kwargs['source_address'] = (ip, 0)
            async with aiohttp.ClientSession() as session:
                async with getattr(session, method.lower())(url, **kwargs) as response:
                    data = await response.json()
                    
                    # 更新缓存和请求计数
                    self.market_data_cache[cache_key] = {
                        'data': data,
                        'timestamp': current_time
                    }
                    self.last_rest_request[ip].append(current_time)
                    return data
                    
        except Exception as e:
            await self.ip_manager.report_error(ip)
            self.logger.error(f"请求失败 (IP: {ip}): {e}")
            raise

    async def get_market_data(self, symbol: str) -> Dict:
        """优化的市场数据获取"""
        try:
            # 1. 先检查缓存
            cache_key = f"{symbol}_market_data"
            current_time = time.time()
            
            if (cache_key in self.market_data_cache and 
                current_time - self.market_data_cache[cache_key]['timestamp'] < self.cache_ttl):
                return self.market_data_cache[cache_key]

            # 2. 使用已分配的IP获取数据
            if hasattr(self, 'execution_ips'):
                ip = self.execution_ips['primary']  # 使用主IP
            else:
                ip = await self.ip_manager.get_best_ip()

            # 3. 获取数据
            params = {'symbol': symbol}
            ticker = await self.query_client.fetch_ticker(symbol)
            orderbook = await self.query_client.fetch_order_book(symbol)
            
            # 4. 整合数据
            market_data = {
                'ticker': ticker,
                'orderbook': orderbook,
                'timestamp': current_time,
                'price': float(ticker['last']),
                'bid': float(ticker['bid']),
                'ask': float(ticker['ask']),
                'volume': float(ticker['baseVolume'])
            }
            
            # 5. 更新缓存和状态
            self.market_data_cache[cache_key] = market_data
            self._update_market_status(market_data)
            
            return market_data

        except Exception as e:
            self.logger.error(f"获取市场数据失败: {e}")
            raise

    def _update_market_status(self, market_data: Dict):
        """更新市场状态"""
        self.market_status = {
            'symbol_active': True,
            'has_orderbook': bool(market_data['orderbook']['bids'] or market_data['orderbook']['asks']),
            'has_trades': market_data['volume'] > 0,
            'last_update': time.time()
        }

    async def monitor_market_status(self):
        """持续监控市场状态"""
        while True:
            try:
                # 使用已分配的IP
                if hasattr(self, 'execution_ips'):
                    ip = self.execution_ips['primary']
                else:
                    ip = await self.ip_manager.get_best_ip()

                # 获取最新市场数据
                market_data = await self.get_market_data(self.symbol)
                
                # 分析价格变动
                if 'last_price' in self.market_status:
                    price_change = abs(market_data['price'] - self.market_status['last_price'])
                    if price_change > self.price_change_threshold:
                        self.logger.warning(f"价格剧烈波动: {price_change}")

                # 分析深度变化
                if market_data['orderbook']['bids'] and market_data['orderbook']['asks']:
                    spread = float(market_data['orderbook']['asks'][0][0]) - float(market_data['orderbook']['bids'][0][0])
                    if spread > self.spread_threshold:
                        self.logger.warning(f"买卖价差过大: {spread}")

                # 更新状态
                self.market_status['last_price'] = market_data['price']
                
                await asyncio.sleep(0.1)  # 100ms检查间隔
                
            except Exception as e:
                self.logger.error(f"市场状态监控失败: {str(e)}")
                await asyncio.sleep(1)  # 出错后等待1秒

    async def batch_market_data(self, symbols: List[str]) -> Dict:
        """批量获取多个交易对的市场数据"""
        try:
            # 使用已分配的IP
            if hasattr(self, 'execution_ips'):
                tasks = [
                    self.get_market_data(symbol) 
                    for symbol in symbols
                ]
            else:
                # 如果IP未分配，使用多IP并发
                tasks = []
                for i, symbol in enumerate(symbols):
                    ip_index = i % len(self.ip_manager.ips)
                    tasks.append(
                        self.get_market_data(
                            symbol, 
                            ip=self.ip_manager.ips[ip_index]
                        )
                    )
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            market_data = {}
            for symbol, result in zip(symbols, results):
                if isinstance(result, Exception):
                    self.logger.warning(f"{symbol} 数据获取失败: {result}")
                    continue
                market_data[symbol] = result
                
            return market_data
            
        except Exception as e:
            self.logger.error(f"批量获取市场数据失败: {e}")
            raise

    async def sync_server_time(self) -> float:
        """同步币安服务器时间"""
        try:
            local_send_time = time.time() * 1000
            server_time = await self.query_client.fetch_time()
            local_recv_time = time.time() * 1000
            
            # 计算网络延迟
            network_latency = (local_recv_time - local_send_time) / 2
            
            # 计算时间偏差
            server_time_ms = server_time['serverTime']
            time_offset = server_time_ms - (local_send_time + network_latency)
            
            # 保存样本
            self.time_samples.append({
                'offset': time_offset,
                'latency': network_latency,
                'timestamp': local_send_time
            })
            
            # 计算稳定的时间偏差(使用中位数)
            recent_offsets = [sample['offset'] for sample in self.time_samples]
            self.server_time_offset = statistics.median(recent_offsets)
            
            self.last_time_sync = local_send_time
            self.logger.debug(f"时间同步完成: 偏差 {self.server_time_offset}ms, 延迟 {network_latency}ms")
            
            return network_latency
            
        except Exception as e:
            self.logger.error(f"同步服务器时间失败: {str(e)}")
            raise TimeError("无法同步服务器时间")

    def get_server_time(self) -> float:
        """获取当前币安服务器时间(毫秒)"""
        return time.time() * 1000 + self.server_time_offset

    async def maintain_time_sync(self):
        """维护时间同步"""
        while True:
            try:
                current_time = time.time() * 1000
                if current_time - self.last_time_sync >= self.time_sync_interval * 1000:
                    await self.sync_server_time()
                await asyncio.sleep(0.1)  # 100ms检查一次
            except Exception as e:
                self.logger.error(f"时间同步维护失败: {str(e)}")
                await asyncio.sleep(1)  # 出错后等待1秒再试

    async def calculate_execution_time(self, target_time: float) -> float:
        """计算实际执行时间
        Args:
            target_time: 目标执行时间(毫秒)
        Returns:
            实际应该执行的时间(毫秒)
        """
        # 确保时间同步是最新的
        if time.time() * 1000 - self.last_time_sync >= self.time_sync_interval * 1000:
            await self.sync_server_time()
        
        # 获取最近的网络延迟样本
        recent_latencies = [sample['latency'] for sample in self.time_samples]
        if not recent_latencies:
            raise TimeError("没有足够的网络延迟样本")
        
        # 使用95分位数作为网络延迟估计
        network_latency = statistics.quantiles(recent_latencies, n=20)[-1]
        
        # 计算完整提前量
        advance_time = network_latency + 50  # 网络延迟 + 50ms安全边际
        
        # 返回实际执行时间(考虑服务器时间偏差)
        return target_time - advance_time

    async def check_symbol_status(self) -> dict:
        """检查交易对状态
        Returns:
            dict: {
                'active': bool,  # 交易对是否可用
                'status': str,   # 交易对状态
                'msg': str       # 详细信息
            }
        """
        try:
            # 使用3个IP并发检查
            tasks = []
            for ip in list(self.ip_manager.ips)[:3]:
                if await self.ip_manager.scheduler.can_make_request(ip, 'market'):
                    tasks.append(self._check_symbol_with_ip(ip))
            
            if not tasks:
                raise MarketError("没有可用的IP进行检查")
            
            results = await asyncio.gather(*tasks, return_exceptions=True)
            valid_results = [r for r in results if not isinstance(r, Exception)]
            
            if not valid_results:
                return {
                    'active': False,
                    'status': 'error',
                    'msg': '所有IP检查失败'
                }
            
            # 任一IP检查成功即可
            for result in valid_results:
                if result['active']:
                    return result
            
            return valid_results[0]  # 返回第一个结果
            
        except Exception as e:
            self.logger.error(f"检查交易对状态失败: {str(e)}")
            return {
                'active': False,
                'status': 'error',
                'msg': str(e)
            }

    async def _check_symbol_with_ip(self, ip: str) -> dict:
        """使用指定IP检查交易对状态"""
        try:
            # 1. 检查交易对信息
            exchange_info = await self.query_client.fetch_markets()
            symbol_info = None
            
            for market in exchange_info:
                if market['symbol'] == self.symbol:
                    symbol_info = market
                    break
            
            if not symbol_info:
                return {
                    'active': False,
                    'status': 'not_found',
                    'msg': f'交易对 {self.symbol} 不存在'
                }
            
            # 2. 检查订单簿
            orderbook = await self.query_client.fetch_order_book(self.symbol)
            has_orderbook = bool(orderbook['bids'] or orderbook['asks'])
            
            # 3. 检查24小时统计
            ticker = await self.query_client.fetch_ticker(self.symbol)
            
            # 4. 综合判断
            is_active = (
                symbol_info.get('active', False) and
                has_orderbook and
                ticker.get('baseVolume', 0) > 0
            )
            
            return {
                'active': is_active,
                'status': 'ready' if is_active else 'not_ready',
                'msg': '交易对可用' if is_active else '交易对未准备就绪',
                'orderbook': has_orderbook,
                'info': symbol_info
            }
            
        except Exception as e:
            await self.ip_manager.report_error(ip)
            raise MarketError(f"IP {ip} 检查失败: {str(e)}")

    async def execute_orders_with_strategy(self) -> List[dict]:
        """使用分批策略执行订单
        Returns:
            List[dict]: 成功执行的订单列表
        """
        orders = []
        execution_start = self.get_server_time()
        
        try:
            # 确保交易对可用
            status = await self.check_symbol_status()
            if not status['active']:
                raise MarketError(f"交易对不可用: {status['msg']}")

            # 第1批: T+0ms (2个订单)
            self.logger.info("执行第1批订单...")
            first_batch = await self._execute_batch(
                batch_size=2,
                time_offset=0,
                ip=self.execution_ips['primary']
            )
            if first_batch:
                self.logger.info(f"第1批订单成功, 数量: {len(first_batch)}")
                return first_batch

            # 第2批: T+5ms (2个订单)
            self.logger.info("执行第2批订单...")
            second_batch = await self._execute_batch(
                batch_size=2,
                time_offset=5,
                ip=self.execution_ips['secondary']
            )
            if second_batch:
                self.logger.info(f"第2批订单成功, 数量: {len(second_batch)}")
                return second_batch

            # 第3批: T+10ms (1个订单)
            self.logger.info("执行第3批订单...")
            final_batch = await self._execute_batch(
                batch_size=1,
                time_offset=10,
                ip=self.execution_ips['fallback']
            )
            if final_batch:
                self.logger.info(f"第3批订单成功, 数量: {len(final_batch)}")
                return final_batch

            total_time = self.get_server_time() - execution_start
            self.logger.warning(f"所有批次执行完成，总耗时: {total_time}ms，无成功订单")
            return []

        except Exception as e:
            self.logger.error(f"订单执行策略失败: {str(e)}")
            return orders

    async def _execute_batch(self, batch_size: int, time_offset: int, ip: str) -> List[dict]:
        """执行一批订单
        Args:
            batch_size: 订单数量
            time_offset: 相对于基准时间的偏移(ms)
            ip: 使用的IP地址
        Returns:
            List[dict]: 成功的订单列表
        """
        orders = []
        try:
            # 计算执行时间
            base_time = self.get_server_time()
            execution_time = base_time + time_offset

            # 创建订单任务
            tasks = []
            for _ in range(batch_size):
                tasks.append(self._place_single_order(
                    ip=ip,
                    execution_time=execution_time
                ))

            # 并发执行订单
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 处理结果
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

    async def _place_single_order(self, ip: str, execution_time: float) -> Optional[dict]:
        """执行单个订单
        Args:
            ip: 使用的IP
            execution_time: 目标执行时间(ms)
        Returns:
            Optional[dict]: 订单结果
        """
        try:
            # 等待直到执行时间
            current_time = self.get_server_time()
            if current_time < execution_time:
                wait_time = (execution_time - current_time) / 1000
                if wait_time > 0:
                    await asyncio.sleep(wait_time)

            # 准备订单参数
            order_params = {
                'symbol': self.symbol,
                'type': 'LIMIT',
                'side': 'BUY',
                'price': self.price,
                'amount': self.amount,
                'timeInForce': 'GTC',
                'timestamp': int(execution_time)
            }

            # 执行订单
            start_time = self.get_server_time()
            order = await self.trade_client.create_order(**order_params)
            execution_latency = self.get_server_time() - start_time

            # 记录执行延迟
            self.logger.info(f"订单执行完成 - IP: {ip}, 延迟: {execution_latency}ms")
            
            return order

        except Exception as e:
            await self.ip_manager.report_error(ip)
            raise ExecutionError(f"订单执行失败: {str(e)}")

    async def test_performance(self, duration=120):
        """测试当前配置的性能"""
        print(f"\n=== 开始性能测试 (持续{duration}秒) ===")
        
        # 确保使用实盘模式
        self.trade_client.set_sandbox_mode(False)
        self.query_client.set_sandbox_mode(False)
        
        # 如果没有设置交易对，使用默认的 BTC/USDT
        if not self.symbol:
            self.symbol = 'BTC/USDT'
            print(f"未设置交易对，使用默认交易对: {self.symbol}")
        
        # 初始化 WebSocket 连接
        if not hasattr(self, 'ws_manager') or not self.ws_manager:
            print("初始化 WebSocket 连接...")
            self.ws_manager = BinanceWebSocketManager(self.symbol, self.logger)
            # 等待连接建立
            for ip in self.ip_manager.ips:
                try:
                    await self.ws_manager.add_connection(ip)
                    print(f"✓ WebSocket 连接已建立 (IP: {ip})")
                    # 添加短暂延迟确保连接完全建立
                    await asyncio.sleep(1)
                except Exception as e:
                    print(f"✗ WebSocket 连接失败 (IP: {ip}): {e}")
                    continue
        
        # 显示当前配置
        print("\n当前系统配置:")
        print(f"• 交易对: {self.symbol}")
        print(f"• API类型: 实盘模式")
        print(f"• 可用IP: {len(self.ip_manager.ips)}个")
        for ip in self.ip_manager.ips:
            print(f"  - {ip}")
        print(f"• WebSocket连接: {'已启用' if hasattr(self, 'ws_manager') else '未启用'}")
        print("\n开始测试...\n")

        # 初始化统计数据
        stats = {
            'latencies': [],
            'success_count': 0,
            'error_count': 0,
            'errors': defaultdict(int),
            'min_latency': float('inf'),
            'max_latency': 0,
            'last_report': time.time(),
            'last_success': 0,
            'requests_per_second': [],
            'ws_messages': 0
        }

        start_time = time.time()
        
        # 创建IP请求队列
        ip_queues = {ip: asyncio.Queue() for ip in self.ip_manager.ips}
        
        # 修改 ip_worker 函数
        async def ip_worker(ip: str, queue: asyncio.Queue, offset_ms: int):
            await asyncio.sleep(offset_ms / 1000)  # 初始错开等待
            while time.time() - start_time < duration:
                try:
                    req_start = time.time()
                    # 直接使用 REST API 而不是 CCXT
                    async with aiohttp.ClientSession() as session:
                        async with session.get(
                            'https://api.binance.com/api/v3/ticker/24hr',
                            params={'symbol': self.symbol.replace('/', '')},
                            timeout=2,
                            headers={'User-Agent': 'Mozilla/5.0'}
                        ) as response:
                            if response.status == 200:
                                data = await response.json()
                                latency = (time.time() - req_start) * 1000
                                await queue.put(('success', latency))
                            else:
                                await queue.put(('error', f'HTTP {response.status}'))
                                
                except asyncio.TimeoutError:
                    await queue.put(('error', 'Timeout'))
                except Exception as e:
                    await queue.put(('error', str(e)))
                
                await asyncio.sleep(0.054)  # 54ms间隔

        # 添加结果处理函数
        async def process_results():
            while time.time() - start_time < duration:
                for ip, queue in ip_queues.items():
                    while not queue.empty():
                        status, data = await queue.get()
                        
                        if status == 'success':
                            latency = data
                            stats['latencies'].append(latency)
                            stats['success_count'] += 1
                            stats['min_latency'] = min(stats['min_latency'], latency)
                            stats['max_latency'] = max(stats['max_latency'], latency)
                        else:
                            stats['error_count'] += 1
                            error_type = str(data)
                            stats['errors'][error_type] = stats['errors'].get(error_type, 0) + 1
                            
                # 更新显示
                now = time.time()
                if now - stats['last_report'] >= 1:
                    time_passed = now - stats['last_report']
                    current_rate = (stats['success_count'] - stats['last_success']) / time_passed
                    stats['requests_per_second'].append(current_rate)
                    
                    # 计算最近的平均延迟
                    recent_latencies = stats['latencies'][-1000:]
                    avg_latency = sum(recent_latencies) / len(recent_latencies) if recent_latencies else 0
                    
                    # 获取WebSocket消息计数
                    if hasattr(self, 'ws_manager') and self.ws_manager:
                        stats['ws_messages'] = getattr(self.ws_manager, 'message_count', 0)
                    
                    print(f"\r状态: "
                          f"运行时间: {int(now - start_time)}s/{duration}s | "
                          f"当前速率: {current_rate*60:.1f} 请求/分钟 | "
                          f"平均延迟: {avg_latency:.2f}ms | "
                          f"成功: {stats['success_count']} | "
                          f"错误: {stats['error_count']} | "
                          f"WS消息: {stats['ws_messages']}", end='')
                    
                    stats['last_report'] = now
                    stats['last_success'] = stats['success_count']
                
                await asyncio.sleep(0.001)  # 避免CPU过载

        try:
            # 创建IP工作任务
            workers = []
            for i, ip in enumerate(self.ip_manager.ips):
                offset = i * 18  # 错开18ms启动
                workers.append(asyncio.create_task(ip_worker(ip, ip_queues[ip], offset)))
            
            # 添加结果处理任务
            processor = asyncio.create_task(process_results())
            
            # 等待所有任务完成或超时
            await asyncio.wait(
                [*workers, processor],
                timeout=duration,
                return_when=asyncio.FIRST_COMPLETED
            )
            
        except asyncio.CancelledError:
            print("\n测试被取消")
        except KeyboardInterrupt:
            print("\n测试被用户中断")
        except Exception as e:
            print(f"\n测试出错: {str(e)}")
        finally:
            # 清理任务
            for task in [*workers, processor]:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            
            # 关闭 WebSocket 连接
            if hasattr(self, 'ws_manager'):
                await self.ws_manager.close()

            print("\n测试结束")

        # 生成详细报告
        total_time = time.time() - start_time
        total_requests = stats['success_count'] + stats['error_count']
        avg_rate = stats['success_count'] / total_time * 60
        
        print("\n\n========== 详细测试报告 ==========")
        print(f"\n基本信息:")
        print(f"• 测试时长: {total_time:.1f}秒")
        print(f"• 总请求数: {total_requests}")
        print(f"• 成功请求: {stats['success_count']}")
        print(f"• 失败请求: {stats['error_count']}")
        
        print(f"\n性能指标:")
        print(f"• 平均速率: {avg_rate:.1f} 请求/分钟")
        if stats['latencies']:
            print(f"• 延迟统计:")
            print(f"  - 最小延迟: {stats['min_latency']:.2f}ms")
            print(f"  - 最大延迟: {stats['max_latency']:.2f}ms")
            print(f"  - 平均延迟: {sum(stats['latencies'])/len(stats['latencies']):.2f}ms")
            
            # 计算延迟分位数
            sorted_latencies = sorted(stats['latencies'])
            p50 = sorted_latencies[len(sorted_latencies)//2]
            p95 = sorted_latencies[int(len(sorted_latencies)*0.95)]
            p99 = sorted_latencies[int(len(sorted_latencies)*0.99)]
            print(f"  - 延迟分位数:")
            print(f"    * P50: {p50:.2f}ms")
            print(f"    * P95: {p95:.2f}ms")
            print(f"    * P99: {p99:.2f}ms")
        
        print(f"\n稳定性指标:")
        print(f"• 成功率: {(stats['success_count']/total_requests*100):.2f}%")
        if stats['errors']:
            print("• 错误分布:")
            for error_type, count in stats['errors'].items():
                print(f"  - {error_type}: {count}次 ({count/stats['error_count']*100:.1f}%)")
        
        if stats['requests_per_second']:
            avg_rps = sum(stats['requests_per_second'])/len(stats['requests_per_second'])
            print(f"\n吞吐量分析:")
            print(f"• 平均每秒请求: {avg_rps:.1f}")
            print(f"• 每分钟请求: {avg_rps*60:.1f}")
        
        print("\n系统配置:")
        print(f"• 交易对: {self.symbol}")
        print(f"• 可用IP数: {len(self.ip_manager.ips)}")
        print(f"• API类型: 实盘模式")
        print("================================")
        print(f"\n额外统计:")
        print(f"• WebSocket消息总数: {stats['ws_messages']}")
        print(f"• 总数据点: {total_requests + stats['ws_messages']}")
        print(f"• 综合每秒处理: {(total_requests + stats['ws_messages'])/total_time:.1f}")
        print("================================")

        return {
            'duration': total_time,
            'total_requests': total_requests,
            'success_count': stats['success_count'],
            'error_count': stats['error_count'],
            'avg_latency': sum(stats['latencies'])/len(stats['latencies']) if stats['latencies'] else 0,
            'min_latency': stats['min_latency'],
            'max_latency': stats['max_latency'],
            'success_rate': stats['success_count']/total_requests if total_requests > 0 else 0,
            'requests_per_minute': avg_rate,
            'ws_messages': stats['ws_messages']
        }

    async def run_test(self):
        """统一测试入口"""
        while True:
            print("\n=== 币安测试中心 ===")
            print("1. API性能测试")
            print("2. 网络模拟运行")
            print("3. WebSocket测试")
            print("4. 全面压力测试")
            print("0. 返回")
            print("==================")
            
            try:
                choice = input("请选择测试类型 (0-4): ").strip()
                
                if choice == '0':
                    break
                    
                elif choice == '1':  # API性能测试
                    print("\n--- API性能测试 ---")
                    print("1. 快速测试 (30秒)")
                    print("2. 标准测试 (120秒)")
                    print("3. 长时间测试 (300秒)")
                    print("4. 自定义时长")
                    print("5. 返回")
                    
                    test_choice = input("请选择测试时长 (1-5): ").strip()
                    if test_choice == '5':
                        continue
                        
                    # 修改这里的时长选择逻辑
                    duration = None
                    if test_choice == '1':
                        duration = 30
                    elif test_choice == '2':
                        duration = 120
                    elif test_choice == '3':
                        duration = 300
                    elif test_choice == '4':
                        try:
                            duration = int(input("请输入测试时长(秒): "))
                        except ValueError:
                            print("无效的时长输入")
                            continue

                    if duration:
                        if self._init_clients():
                            await self.test_performance(duration)
                        else:
                            print("初始化客户端失败")
                        
                elif choice == '2':  # 网络模拟运行
                    await self.test_network()
                    
                elif choice == '3':  # WebSocket测试
                    await self.test_websocket()
                    
                elif choice == '4':  # 全面压力测试
                    # 修改这里：不使用 await
                    if self._init_clients():  # 确保客户端已初始化
                        await self.test_performance(300)  # 5分钟完整测试
                    else:
                        print("初始化客户端失败")
                    
                else:
                    print("无效选择")
                    
                input("\n按Enter继续...")
                
            except KeyboardInterrupt:
                print("\n测试被用户中断")
                break
            except Exception as e:
                print(f"测试执行失败: {str(e)}")
                self.logger.error(f"测试执行失败: {str(e)}", exc_info=True)
                input("\n按Enter继续...")

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
        except Exception as e:  # 修复缩进
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

    def show_menu(self):
        """显示主菜单"""
        print("""
=== 币安现货抢币工具 ===
1. 设置API密钥
2. 抢开盘策略设置
3. 查看当前策略
4. 开始抢购
5. 测试网络模拟运行
6. 性能测试
0. 退出
=====================""")

    def run(self):
        """运行主程序"""
        while True:
            self.show_menu()
            choice = input("请选择操作 (0-6): ").strip()
            
            if choice == '6':
                print("""
=== 性能测试选项 ===
1. 快速测试 (30秒)
2. 标准测试 (120秒)
3. 长时间测试 (300秒)
4. 自定义时长
5. 返回主菜单
===================""")
                test_choice = input("请选择测试类型 (1-5): ").strip()
                
                if test_choice == '1':
                    duration = 30
                elif test_choice == '2':
                    duration = 120
                elif test_choice == '3':
                    duration = 300
                elif test_choice == '4':
                    duration = int(input("请输入测试时长(秒): "))
                elif test_choice == '5':
                    continue
                else:
                    print("无效选择")
                    continue
                
                # 创建新的事件循环来执行性能测试
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(sniper.test_current_performance(duration))
                except Exception as e:
                    print(f"性能测试失败: {str(e)}")
                finally:
                    loop.close()
            
            elif choice == '0':
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
            elif choice == '5':
                # 创建新的事件循环来执行测试
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(test_network())
                except Exception as e:
                    print(f"测试网络运行失败: {str(e)}")
                finally:
                    loop.close()
            else:
                print("无效的选择，请重新输入")

    async def test_network_run(self):
        """测试网络模拟运行"""
        try:
            print("\n=== 开始测试网络模拟运行 ===")
            
            # 1. 切换到测试网络
            self.trade_client.set_sandbox_mode(True)
            self.query_client.set_sandbox_mode(True)
            
            # 2. 设置更短的测试时间（5分钟后开盘）
            original_opening_time = self.opening_time
            self.opening_time = datetime.now(self.timezone) + timedelta(minutes=5)
            
            print(f"""
测试模式配置:
• 环境: 测试网络
• 模拟开盘时间: {self.opening_time.strftime('%Y-%m-%d %H:%M:%S')}
• 交易对: {self.symbol}
• 买入金额: {format_number(self.amount)} USDT
• 并发订单数: {self.concurrent_orders}
""")

            # 3. 执行完整流程
            result = await self.prepare_and_snipe_async()
            
            # 4. 如果下单成功，测试止盈止损
            if result and result.get('filled', 0) > 0:
                entry_price = result['average']
                filled_amount = result['filled']
                
                print("\n=== 开始测试止盈止损 ===")
                
                # 设置止损单
                stop_loss_price = entry_price * (1 - self.stop_loss)
                try:
                    stop_loss_order = await asyncio.to_thread(
                        self.trade_client.create_order,
                        symbol=self.symbol,
                        type='STOP_LOSS_LIMIT',
                        side='SELL',
                        amount=filled_amount,
                        price=stop_loss_price,
                        params={'stopPrice': stop_loss_price}
                    )
                    print(f"• 止损单已设置: 价格 {format_number(stop_loss_price)} USDT")
                except Exception as e:
                    print(f"• 止损单设置失败: {str(e)}")
                
                # 设置止盈单
                for i, (price_level, amount_ratio) in enumerate(self.sell_strategy):
                    target_price = entry_price * (1 + price_level)
                    sell_amount = filled_amount * amount_ratio
                    try:
                        take_profit_order = await asyncio.to_thread(
                            self.trade_client.create_order,
                            symbol=self.symbol,
                            type='LIMIT',
                            side='SELL',
                            amount=sell_amount,
                            price=target_price
                        )
                        print(f"• 止盈单{i+1}已设置: 价格 {format_number(target_price)} USDT, 数量 {format_number(sell_amount)}")
                    except Exception as e:
                        print(f"• 止盈单{i+1}设置失败: {str(e)}")
                
                # 查询当前订单状态
                try:
                    open_orders = await asyncio.to_thread(
                        self.trade_client.fetch_open_orders,
                        symbol=self.symbol
                    )
                    print(f"\n当前挂单数量: {len(open_orders)}")
                    for order in open_orders:
                        print(f"• 订单ID: {order['id']}, 类型: {order['type']}, 价格: {order['price']}, 数量: {order['amount']}")
                except Exception as e:
                    print(f"查询订单失败: {str(e)}")
                
            print("\n=== 测试运行完成 ===")
            
            # 恢复原始设置
            self.opening_time = original_opening_time
            self.trade_client.set_sandbox_mode(False)
            self.query_client.set_sandbox_mode(False)
            
        except Exception as e:
            logger.error(f"测试运行失败: {str(e)}")
            print(f"测试运行失败: {str(e)}")
        finally:
            # 确保清理所有测试订单
            try:
                await asyncio.to_thread(
                    self.trade_client.cancel_all_orders,
                    symbol=self.symbol
                )
            except Exception as e:
                logger.error(f"清理测试订单失败: {str(e)}")

    async def monitor_symbol_status(self):
        """持续监控交易对状态"""
        while True:
            try:
                status = await self.check_symbol_status()
                if status['active']:
                    self.logger.info(f"交易对 {self.symbol} 状态: {status['msg']}")
                    return True
                else:
                    self.logger.debug(f"交易对 {self.symbol} 状态: {status['msg']}")
                await asyncio.sleep(0.1)  # 100ms检查一次
            except Exception as e:
                self.logger.error(f"监控交易对状态失败: {str(e)}")
                await asyncio.sleep(1)  # 出错后等待1秒

    async def prepare_ips(self):
        """IP准备和角色分配
        Returns:
            bool: 是否成功
        """
        try:
            available_ips = list(self.ip_manager.ips)
            if len(available_ips) < 3:
                raise RuntimeError(f"可用IP不足，当前只有 {len(available_ips)} 个IP")

            # 简单分配IP角色
            self.execution_ips = {
                'primary': available_ips[0],    # 第一个IP作为主要IP
                'secondary': available_ips[1],   # 第二个IP作为次要IP
                'fallback': available_ips[2]     # 第三个IP作为备用IP
            }

            self.logger.info(f"IP角色分配完成: {self.execution_ips}")
            self.ip_roles_locked = True
            return True

        except Exception as e:
            self.logger.error(f"IP准备失败: {str(e)}")
            return False

    async def validate_ips(self):
        """验证所有IP可用性"""
        try:
            for role, ip in self.execution_ips.items():
                # 简单的连接测试
                response = await self.query_client.ping(ip=ip)
                if not response:
                    raise NetworkError(f"IP {ip} ({role}) 连接测试失败")
                
            self.logger.info("所有IP验证通过")
            return True

        except Exception as e:
            self.logger.error(f"IP验证失败: {str(e)}")
            return False

    async def create_ws_connection(self, ip):
        """创建WebSocket连接"""
        try:
            # 使用IP绑定的WebSocket连接
            ws_url = f"wss://stream.binance.com:9443/ws"
            ws = await websockets.connect(
                ws_url,
                extra_headers={
                    'Host': 'stream.binance.com',
                    'User-Agent': f'BinanceSniper/1.0 IP/{ip}'
                },
                ssl=True,
                timeout=5
            )
            
            # 订阅行情
            await ws.send(json.dumps({
                "method": "SUBSCRIBE",
                "params": [
                    "btcusdt@ticker"
                ],
                "id": int(time.time() * 1000)
            }))
            
            # 等待订阅确认
            response = await ws.recv()
            if json.loads(response).get('result') is None:
                return ws
            else:
                await ws.close()
                return None
                
        except Exception as e:
            logger.error(f"WebSocket连接失败 (IP: {ip}): {str(e)}")
            return None

# 在文件末尾添加
async def main():
    """主程序入口"""
    try:
        # 初始化配置和实例
        config = ConfigManager()
        sniper = BinanceSniper(config)
        
        # 加载API配置
        if not sniper.load_api_keys():
            print("请先配置API密钥")
            return
            
        print("\n=== 币安现货抢币工具 ===")
        print("1. 设置API密钥")
        print("2. 抢开盘策略设置")
        print("3. 查看当前策略")
        print("4. 开始抢购")
        print("5. 测试中心")
        print("0. 退出")
        print("=====================")
        
        while True:
            choice = input("\n请选择操作 (0-5): ").strip()
            
            if choice == '0':
                print("感谢使用，再见!")
                break
            elif choice == '1':
                await sniper.setup_api_keys()
            elif choice == '2':
                await sniper.setup_snipe_strategy()
            elif choice == '3':
                sniper.print_strategy()
            elif choice == '4':
                await sniper.prepare_and_snipe()
            elif choice == '5':
                # 修改这里,直接使用已有的异步 run_test 方法
                await sniper.run_test()  # 使用已有的异步测试方法
            else:
                print("无效的选择，请重新输入")
                
    except KeyboardInterrupt:
        print("\n程序已被用户中断")
    except Exception as e:
        print(f"程序运行出错: {str(e)}")
        logger.error(f"程序运行出错: {str(e)}", exc_info=True)

if __name__ == '__main__':
    try:
        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 设置信号处理
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(cleanup()))
            
        # 运行主程序
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        print("\n程序被用户中断")
    except Exception as e:
        print(f"程序运行出错: {str(e)}")
        logger.error(f"程序运行出错: {str(e)}", exc_info=True)
    finally:
        # 清理并关闭事件循环
        try:
            tasks = asyncio.all_tasks(loop)
            for task in tasks:
                task.cancel()
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()

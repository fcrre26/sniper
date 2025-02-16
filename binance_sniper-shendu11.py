# =============================== 
# 模块：导入和常量
# ===============================
import asyncio
import logging
import time
import json
import os
import sys
import signal
from collections import defaultdict, deque
from typing import Dict, List, Optional, Set, Tuple, Any
import ccxt
import pytz
from logging.handlers import RotatingFileHandler
import aiohttp
from datetime import datetime, timedelta
import psutil
import numpy as np

# 常量定义
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

# =============================== 
# 模块：日志配置
# ===============================
def setup_logger():
    """设置日志记录器"""
    logger = logging.getLogger('BinanceSniper')
    if logger.handlers:
        return logger
        
    logger.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_file = os.path.join(LOG_DIR, 'binance_sniper.log')
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

logger = setup_logger()

# =============================== 
# 模块：工具函数
# ===============================
def format_number(num: float, decimals: int = 2) -> str:
    """格式化数字"""
    return f"{num:,.{decimals}f}"

def mask_api_key(key: str) -> str:
    """遮蔽API密钥"""
    if not key:
        return "未设置"
    return f"{key[:6]}{'*' * (len(key)-10)}{key[-4:]}"

# =============================== 
# 模块：数据管理
# ===============================
class MarketDataCache:
    """市场数据缓存 - 专注于数据存储和基础操作"""
    def __init__(self):
        self.lock = asyncio.Lock()
        
        # 最新市场数据
        self._latest_data = {
            'current_price': 0,
            'price_24h_change': 0,
            'volume_24h': 0,
            'market_status': 'unknown',
            'timestamp': 0,
            'network_latency': 0,
            'time_offset': 0,
            'server_time': 0,
            'depth': {
                'bids': [],
                'asks': []
            }
        }
        
        # 价格缓存
        self.price_cache = {
            'ws_data': deque(maxlen=1000),  # WebSocket数据
            'rest_data': deque(maxlen=1000), # REST数据
            'last_update': 0,
            'last_price': 0
        }
        
        # 深度缓存
        self.depth_cache = {
            'bids': {},  # 买单深度
            'asks': {},  # 卖单深度
            'last_update': 0,
            'snapshot': None  # 深度快照
        }
        
        # 成交缓存
        self.trade_cache = {
            'recent_trades': deque(maxlen=1000),
            'last_update': 0
        }

    async def update_price(self, data: dict, source: str = 'ws'):
        """更新价格数据"""
        async with self.lock:
            if source == 'ws':
                self.price_cache['ws_data'].append(data)
            else:
                self.price_cache['rest_data'].append(data)
            self.price_cache['last_price'] = float(data['price'])
            self.price_cache['last_update'] = time.time() * 1000
            self._latest_data['current_price'] = float(data['price'])

    async def update_depth(self, data: dict, source: str = 'ws'):
        """更新深度数据"""
        async with self.lock:
            if source == 'ws':
                self._update_incremental_depth(data)
            else:
                self._update_snapshot_depth(data)
            self._latest_data['depth'] = {
                'bids': list(self.depth_cache['bids'].items())[:10],
                'asks': list(self.depth_cache['asks'].items())[:10]
            }
            self.depth_cache['last_update'] = time.time() * 1000

    def _update_incremental_depth(self, data: dict):
        """处理增量深度更新"""
        if not self.depth_cache['snapshot']:
            return
            
        # 更新买单深度
        for price, amount in data.get('bids', []):
            if float(amount) > 0:
                self.depth_cache['bids'][price] = amount
            else:
                self.depth_cache['bids'].pop(price, None)
                
        # 更新卖单深度
        for price, amount in data.get('asks', []):
            if float(amount) > 0:
                self.depth_cache['asks'][price] = amount
            else:
                self.depth_cache['asks'].pop(price, None)

    def _update_snapshot_depth(self, data: dict):
        """处理深度快照数据"""
        self.depth_cache['snapshot'] = data
        self.depth_cache['bids'].clear()
        self.depth_cache['asks'].clear()
        
        for price, amount in data.get('bids', []):
            self.depth_cache['bids'][price] = amount
        for price, amount in data.get('asks', []):
            self.depth_cache['asks'][price] = amount

    async def update_trades(self, trade: dict):
        """更新成交数据"""
        async with self.lock:
            self.trade_cache['recent_trades'].append(trade)
            self.trade_cache['last_update'] = time.time() * 1000

    async def get_latest_data(self) -> Optional[Dict]:
        """获取最新市场数据"""
        async with self.lock:
            return self._latest_data.copy()

    async def get_depth(self, limit: int = 20) -> Dict:
        """获取市场深度"""
        async with self.lock:
            bids = sorted(
                self.depth_cache['bids'].items(),
                key=lambda x: float(x[0]),
                reverse=True
            )[:limit]
            asks = sorted(
                self.depth_cache['asks'].items(),
                key=lambda x: float(x[0])
            )[:limit]
            
            return {
                'bids': bids,
                'asks': asks,
                'timestamp': self.depth_cache['last_update']
            }

    async def get_recent_trades(self, limit: int = 100) -> List[dict]:
        """获取最近成交"""
        async with self.lock:
            return list(self.trade_cache['recent_trades'])[-limit:]

    async def clear_cache(self):
        """清理缓存数据"""
        async with self.lock:
            self.price_cache['ws_data'].clear()
            self.price_cache['rest_data'].clear()
            self.depth_cache['bids'].clear()
            self.depth_cache['asks'].clear()
            self.trade_cache['recent_trades'].clear()

# =============================== 
# 模块：市场监控
# ===============================
class MarketMonitor:
    """市场监控 - 专注于市场状态监控和异常检测"""
    def __init__(self, data_cache: MarketDataCache):
        self.logger = logging.getLogger(__name__)
        self.data_cache = data_cache
        
        # 监控配置
        self.monitor_config = {
            'check_interval': 1.0,     # 检查间隔(秒)
            'price_delay_threshold': 5000,  # 价格延迟阈值(ms)
            'depth_delay_threshold': 2000,  # 深度延迟阈值(ms)
            'warning_threshold': 3,     # 异常警告阈值
            'critical_threshold': 5     # 严重警告阈值
        }
        
        # 市场状态
        self.market_status = {
            'is_trading': False,        # 是否正在交易
            'last_price_update': 0,     # 最后价格更新时间
            'last_depth_update': 0,     # 最后深度更新时间
            'abnormal_count': 0,        # 异常计数
            'last_warning_time': 0      # 最后警告时间
        }
        
        # 监控统计
        self.monitor_stats = {
            'checks_total': 0,          # 总检查次数
            'warnings_total': 0,        # 总警告次数
            'errors_total': 0,          # 总错误次数
            'status_history': deque(maxlen=100)  # 状态历史
        }
        
        # 运行状态
        self.is_running = False
        self.monitor_task = None

        # 深度监控配置
        self.depth_config = {
            'check_interval': 0.1,      # 检查间隔(秒)
            'depth_levels': 5,          # 检查深度层数
            'anomaly_threshold': 5.0,   # 单笔异常阈值(BTC)
            'ratio_threshold': 3.0,     # 深度比例阈值
            'opportunity_threshold': 3.0 # 机会比例阈值
        }

    async def start_monitoring(self):
        """启动市场监控"""
        if self.is_running:
            return
            
        self.is_running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        self.logger.info("市场监控已启动")

    async def stop_monitoring(self):
        """停止市场监控"""
        if not self.is_running:
            return
            
        self.is_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        self.logger.info("市场监控已停止")

    async def _monitor_loop(self):
        """监控循环"""
        while self.is_running:
            try:
                await asyncio.sleep(self.monitor_config['check_interval'])
                
                # 分析市场状态
                status = await self.analyze_market_status()
                self.monitor_stats['checks_total'] += 1
                
                # 记录状态历史
                self.monitor_stats['status_history'].append({
                    'timestamp': time.time(),
                    'status': status
                })
                
                # 处理异常状态
                if status['status'] == 'warning':
                    await self._handle_warning(status)
                elif status['status'] == 'critical':
                    await self._handle_critical(status)
                    
            except Exception as e:
                self.logger.error(f"市场监控异常: {str(e)}")
                self.monitor_stats['errors_total'] += 1
                await asyncio.sleep(5)  # 错误后等待

    async def analyze_market_status(self) -> Dict:
        """分析市场状态"""
        try:
            current_time = time.time() * 1000
            latest_data = await self.data_cache.get_latest_data()
            
            # 检查价格延迟
            price_delay = current_time - latest_data['timestamp']
            
            # 检查深度延迟
            depth_delay = current_time - latest_data['depth'].get('timestamp', 0)
            
            # 检查异常
            is_abnormal = (
                price_delay > self.monitor_config['price_delay_threshold'] or
                depth_delay > self.monitor_config['depth_delay_threshold'] or
                not latest_data['depth']['bids'] or
                not latest_data['depth']['asks']
            )
            
            if is_abnormal:
                self.market_status['abnormal_count'] += 1
            else:
                self.market_status['abnormal_count'] = 0
            
            # 确定状态级别
            status = 'normal'
            if self.market_status['abnormal_count'] >= self.monitor_config['critical_threshold']:
                status = 'critical'
            elif self.market_status['abnormal_count'] >= self.monitor_config['warning_threshold']:
                status = 'warning'
            
            return {
                'status': status,
                'is_trading': self.market_status['is_trading'],
                'price_delay': price_delay,
                'depth_delay': depth_delay,
                'abnormal_count': self.market_status['abnormal_count'],
                'timestamp': current_time
            }
            
        except Exception as e:
            self.logger.error(f"状态分析失败: {str(e)}")
            return {
                'status': 'error',
                'error': str(e),
                'timestamp': time.time() * 1000
            }

    async def _handle_warning(self, status: Dict):
        """处理警告状态"""
        try:
            current_time = time.time()
            # 控制警告频率
            if current_time - self.market_status['last_warning_time'] < 60:
                return
                
            self.monitor_stats['warnings_total'] += 1
            self.market_status['last_warning_time'] = current_time
            
            self.logger.warning(f"""
市场异常警告:
- 价格延迟: {status['price_delay']:.0f}ms
- 深度延迟: {status['depth_delay']:.0f}ms
- 异常计数: {status['abnormal_count']}
- 状态级别: {status['status']}
""")
            
        except Exception as e:
            self.logger.error(f"处理警告失败: {str(e)}")

    async def _handle_critical(self, status: Dict):
        """处理严重异常状态"""
        try:
            self.market_status['is_trading'] = False
            self.logger.error(f"""
市场严重异常:
- 价格延迟: {status['price_delay']:.0f}ms
- 深度延迟: {status['depth_delay']:.0f}ms
- 异常计数: {status['abnormal_count']}
- 状态级别: {status['status']}
建议暂停交易!
""")
            
        except Exception as e:
            self.logger.error(f"处理严重异常失败: {str(e)}")

    def get_status(self) -> Dict:
        """获取监控状态"""
        return {
            'is_running': self.is_running,
            'market_status': self.market_status,
            'statistics': {
                'checks': self.monitor_stats['checks_total'],
                'warnings': self.monitor_stats['warnings_total'],
                'errors': self.monitor_stats['errors_total']
            },
            'config': self.monitor_config
        }

    async def monitor_depth(self):
        """监控深度异常"""
        while self.is_running:
            try:
                depth = await self.data_cache.get_depth(self.depth_config['depth_levels'])
                
                # 计算深度
                sell_depth = sum(amount for price, amount in depth['asks'])
                buy_depth = sum(amount for price, amount in depth['bids'])
                
                # 计算比例
                sell_ratio = sell_depth / buy_depth if buy_depth > 0 else float('inf')
                buy_ratio = buy_depth / sell_depth if sell_depth > 0 else float('inf')
                
                # 检查异常卖单(风险)
                if sell_ratio > self.depth_config['ratio_threshold']:
                    await self._handle_sell_anomaly(depth, sell_ratio)
                    
                # 检查异常买单(机会)
                elif buy_ratio > self.depth_config['opportunity_threshold']:
                    await self._handle_buy_anomaly(depth, buy_ratio)
                    
                await asyncio.sleep(self.depth_config['check_interval'])
                
            except Exception as e:
                self.logger.error(f"深度监控异常: {str(e)}")
                await asyncio.sleep(1)

    async def _handle_sell_anomaly(self, depth: Dict, ratio: float):
        """处理异常卖单"""
        try:
            depth_data = {
                'symbol': self.symbol,
                'current_price': await self.data_cache.get_latest_price(),
                'asks': depth['asks'],
                'bids': depth['bids'],
                'sell_depth': sum(amount for price, amount in depth['asks']),
                'buy_depth': sum(amount for price, amount in depth['bids']),
                'depth_ratio': ratio,
                'threshold': self.depth_config['ratio_threshold'],
                'anomaly_threshold': self.depth_config['anomaly_threshold'],
                'trade_amount': self.position_size,
                'trade_price': depth['bids'][0][0]  # 最高买价
            }
            
            # 打印异常
            self.printer.print_depth_anomaly(depth_data, is_sell_anomaly=True)
            
            # 执行紧急卖出
            result = await self.order_executor.execute_emergency_sell(
                amount=depth_data['trade_amount'],
                price=depth_data['trade_price']
            )
            
            # 打印结果
            self.printer.print_anomaly_result({
                'success': result['status'] == 'filled',
                'is_sell_anomaly': True,
                'order_id': result['id'],
                'price': result['price'],
                'amount': result['filled'],
                'value': result['price'] * result['filled'],
                'entry_price': self.entry_price,
                'profit_amount': (result['price'] - self.entry_price) * result['filled'],
                'profit_percent': (result['price'] / self.entry_price - 1) * 100
            })
            
        except Exception as e:
            self.logger.error(f"处理异常卖单失败: {str(e)}")

    async def _handle_buy_anomaly(self, depth: Dict, ratio: float):
        """处理异常买单"""
        # 类似 _handle_sell_anomaly 的逻辑，但是针对买单机会
        # ... 实现代码 ...

# =============================== 
# 模块：缓存管理
# ===============================
class CacheManager:
    """缓存管理器 - 专注于缓存维护和统计"""
    def __init__(self, data_cache: MarketDataCache):
        self.logger = logging.getLogger(__name__)
        self.data_cache = data_cache
        
        # 缓存配置
        self.cache_config = {
            'price': {
                'ttl': 0.5,          # 价格缓存有效期(秒)
                'max_size': 1000     # 最大缓存条目数
            },
            'depth': {
                'ttl': 0.2,          # 深度缓存有效期(秒)
                'max_size': 100      # 最大缓存条目数
            },
            'trade': {
                'ttl': 1.0,          # 成交缓存有效期(秒)
                'max_size': 1000     # 最大缓存条目数
            },
            'cleanup_interval': 60    # 清理间隔(秒)
        }
        
        # 缓存统计
        self.stats = {
            'hits': defaultdict(int),      # 命中统计
            'misses': defaultdict(int),    # 未命中统计
            'evictions': defaultdict(int), # 清除统计
            'size': defaultdict(int),      # 大小统计
            'errors': defaultdict(int)     # 错误统计
        }
        
        # 运行状态
        self.is_running = False
        self.cleanup_task = None

    async def start(self):
        """启动缓存管理"""
        if self.is_running:
            return
            
        self.is_running = True
        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        self.logger.info("缓存管理器已启动")

    async def stop(self):
        """停止缓存管理"""
        if not self.is_running:
            return
            
        self.is_running = False
        if self.cleanup_task:
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        self.logger.info("缓存管理器已停止")

    async def _cleanup_loop(self):
        """清理循环"""
        while self.is_running:
            try:
                await asyncio.sleep(self.cache_config['cleanup_interval'])
                await self._cleanup_expired()
                await self._enforce_size_limits()
                self.logger.debug("缓存清理完成")
                
            except Exception as e:
                self.logger.error(f"缓存清理异常: {str(e)}")
                self.stats['errors']['cleanup'] += 1

    async def _cleanup_expired(self):
        """清理过期数据"""
        try:
            current_time = time.time()
            
            # 清理价格缓存
            await self._cleanup_cache(
                'price',
                self.data_cache.price_cache,
                current_time
            )
            
            # 清理深度缓存
            await self._cleanup_cache(
                'depth',
                self.data_cache.depth_cache,
                current_time
            )
            
            # 清理成交缓存
            await self._cleanup_cache(
                'trade',
                self.data_cache.trade_cache,
                current_time
            )
            
        except Exception as e:
            self.logger.error(f"清理过期数据失败: {str(e)}")
            self.stats['errors']['expired_cleanup'] += 1

    async def _cleanup_cache(self, cache_type: str, cache: Dict, current_time: float):
        """清理指定缓存"""
        try:
            ttl = self.cache_config[cache_type]['ttl']
            
            # 清理过期数据
            expired = []
            for key, value in cache.items():
                if isinstance(value, dict) and 'timestamp' in value:
                    if current_time - value['timestamp']/1000 > ttl:
                        expired.append(key)
                        
            # 删除过期数据
            for key in expired:
                cache.pop(key)
                self.stats['evictions'][cache_type] += 1
                
        except Exception as e:
            self.logger.error(f"清理{cache_type}缓存失败: {str(e)}")
            self.stats['errors'][f'{cache_type}_cleanup'] += 1

    async def _enforce_size_limits(self):
        """强制执行大小限制"""
        try:
            # 检查价格缓存
            while len(self.data_cache.price_cache['ws_data']) > self.cache_config['price']['max_size']:
                self.data_cache.price_cache['ws_data'].popleft()
                self.stats['evictions']['price'] += 1
                
            # 检查深度缓存
            depth_size = len(self.data_cache.depth_cache['bids']) + len(self.data_cache.depth_cache['asks'])
            if depth_size > self.cache_config['depth']['max_size']:
                self.data_cache.depth_cache['bids'].clear()
                self.data_cache.depth_cache['asks'].clear()
                self.stats['evictions']['depth'] += 1
                
            # 检查成交缓存
            while len(self.data_cache.trade_cache['recent_trades']) > self.cache_config['trade']['max_size']:
                self.data_cache.trade_cache['recent_trades'].popleft()
                self.stats['evictions']['trade'] += 1
                
        except Exception as e:
            self.logger.error(f"强制大小限制失败: {str(e)}")
            self.stats['errors']['size_limit'] += 1

    def get_stats(self) -> Dict:
        """获取缓存统计"""
        try:
            return {
                'hits': dict(self.stats['hits']),
                'misses': dict(self.stats['misses']),
                'hit_rates': {
                    cache_type: (
                        self.stats['hits'][cache_type] / 
                        (self.stats['hits'][cache_type] + self.stats['misses'][cache_type])
                        if (self.stats['hits'][cache_type] + self.stats['misses'][cache_type]) > 0
                        else 0
                    )
                    for cache_type in ['price', 'depth', 'trade']
                },
                'evictions': dict(self.stats['evictions']),
                'errors': dict(self.stats['errors']),
                'current_sizes': {
                    'price': len(self.data_cache.price_cache['ws_data']),
                    'depth': (
                        len(self.data_cache.depth_cache['bids']) + 
                        len(self.data_cache.depth_cache['asks'])
                    ),
                    'trade': len(self.data_cache.trade_cache['recent_trades'])
                }
            }
            
        except Exception as e:
            self.logger.error(f"获取缓存统计失败: {str(e)}")
            return {}

    def record_cache_access(self, cache_type: str, hit: bool):
        """记录缓存访问"""
        try:
            if hit:
                self.stats['hits'][cache_type] += 1
            else:
                self.stats['misses'][cache_type] += 1
                
        except Exception as e:
            self.logger.error(f"记录缓存访问失败: {str(e)}")
            self.stats['errors']['access_record'] += 1

# =============================== 
# 模块：网络管理
# ===============================
class IPManager:
    """IP管理器 - 管理多IP负载均衡和故障切换"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # IP池配置
        self.ip_pool = {}  # IP地址 -> 角色映射
        self.active_ips = set()  # 当前可用IP集合
        
        # IP状态统计
        self.ip_stats = defaultdict(lambda: {
            'success': 0,        # 成功请求数
            'errors': 0,         # 错误请求数
            'latency': [],       # 延迟历史
            'last_used': 0,      # 最后使用时间
            'last_error': 0      # 最后错误时间
        })
        
        # IP管理配置
        self.ip_config = {
            'max_errors': 3,        # 最大错误次数
            'recovery_time': 30,    # 恢复等待时间(秒)
            'latency_threshold': 200 # 延迟阈值(ms)
        }

    async def add_ip(self, ip: str, role: str = 'general'):
        """添加IP"""
        self.ip_pool[ip] = role
        self.active_ips.add(ip)
        self.logger.info(f"添加IP: {ip} (角色: {role})")

    async def remove_ip(self, ip: str):
        """移除IP"""
        self.ip_pool.pop(ip, None)
        self.active_ips.discard(ip)
        self.ip_stats.pop(ip, None)
        self.logger.info(f"移除IP: {ip}")

    async def get_best_ip(self) -> Optional[str]:
        """获取最佳IP"""
        try:
            current_time = time.time()
            available_ips = []
            
            for ip in self.active_ips:
                stats = self.ip_stats[ip]
                
                # 检查错误次数
                if stats['errors'] >= self.ip_config['max_errors']:
                    if current_time - stats['last_error'] > self.ip_config['recovery_time']:
                        # 重置错误计数
                        stats['errors'] = 0
                    else:
                        continue
                
                # 检查延迟
                avg_latency = (
                    sum(stats['latency'][-10:]) / len(stats['latency'][-10:])
                    if stats['latency'] else float('inf')
                )
                if avg_latency < self.ip_config['latency_threshold']:
                    available_ips.append((ip, avg_latency))
            
            if not available_ips:
                return None
                
            # 按延迟排序并返回最佳IP
            return min(available_ips, key=lambda x: x[1])[0]
            
        except Exception as e:
            self.logger.error(f"获取最佳IP失败: {str(e)}")
            return None

    async def update_ip_status(self, ip: str, latency: float, error: bool = False):
        """更新IP状态"""
        try:
            stats = self.ip_stats[ip]
            current_time = time.time()
            
            if error:
                stats['errors'] += 1
                stats['last_error'] = current_time
                if stats['errors'] >= self.ip_config['max_errors']:
                    self.active_ips.discard(ip)
                    self.logger.warning(f"IP {ip} 已暂时禁用 (错误次数: {stats['errors']})")
            else:
                stats['success'] += 1
                stats['latency'].append(latency)
                stats['latency'] = stats['latency'][-100:]  # 保留最近100次延迟
                stats['last_used'] = current_time
                
        except Exception as e:
            self.logger.error(f"更新IP状态失败: {str(e)}")

    def get_ip_stats(self) -> Dict:
        """获取IP统计信息"""
        return {
            ip: {
                'success_rate': (
                    stats['success'] / (stats['success'] + stats['errors'])
                    if (stats['success'] + stats['errors']) > 0 else 0
                ),
                'avg_latency': (
                    sum(stats['latency'][-10:]) / len(stats['latency'][-10:])
                    if stats['latency'] else 0
                ),
                'errors': stats['errors'],
                'active': ip in self.active_ips
            }
            for ip, stats in self.ip_stats.items()
        }

class RateLimiter:
    """频率限制器 - 管理API请求频率"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # 频率限制配置
        self.limits = {
            'default': {
                'rate': 1200,      # 每分钟请求数
                'burst': 100       # 突发请求数
            },
            'order': {
                'rate': 100,       # 每10秒订单数
                'burst': 20
            },
            'market': {
                'rate': 2400,      # 每分钟行情请求数
                'burst': 200
            }
        }
        
        # 请求计数器
        self.counters = defaultdict(lambda: {
            'count': 0,
            'reset_time': 0,
            'last_request': 0
        })
        
        # 统计信息
        self.stats = {
            'total_requests': 0,
            'limited_requests': 0,
            'burst_requests': 0,
            'reset_count': 0
        }
        
        # 启动清理任务
        asyncio.create_task(self._cleanup_task())

    async def check_limit(self, endpoint: str) -> bool:
        """检查是否超过限制"""
        try:
            current_time = time.time()
            limit_key = self._get_limit_key(endpoint)
            counter = self.counters[limit_key]
            limit = self.limits.get(limit_key, self.limits['default'])
            
            # 检查是否需要重置计数器
            if current_time >= counter['reset_time']:
                counter['count'] = 0
                counter['reset_time'] = current_time + 60  # 1分钟重置
                self.stats['reset_count'] += 1
            
            # 检查是否超过限制
            if counter['count'] >= limit['rate']:
                self.logger.warning(f"达到频率限制: {limit_key}")
                self.stats['limited_requests'] += 1
                return False
            
            # 检查突发请求
            if (current_time - counter['last_request'] < 0.1 and  # 100ms内
                counter['count'] >= limit['burst']):
                self.logger.warning(f"达到突发限制: {limit_key}")
                self.stats['burst_requests'] += 1
                return False
            
            self.stats['total_requests'] += 1
            return True
            
        except Exception as e:
            self.logger.error(f"检查频率限制失败: {str(e)}")
            return False

    async def record_request(self, endpoint: str):
        """记录请求"""
        try:
            current_time = time.time()
            limit_key = self._get_limit_key(endpoint)
            counter = self.counters[limit_key]
            
            counter['count'] += 1
            counter['last_request'] = current_time
            
        except Exception as e:
            self.logger.error(f"记录请求失败: {str(e)}")

    def _get_limit_key(self, endpoint: str) -> str:
        """获取限制类型"""
        if 'order' in endpoint:
            return 'order'
        elif 'market' in endpoint or 'ticker' in endpoint:
            return 'market'
        return 'default'

    async def _cleanup_task(self):
        """清理过期计数器"""
        while True:
            try:
                await asyncio.sleep(60)  # 每分钟清理一次
                current_time = time.time()
                
                # 清理过期计数器
                expired = [
                    key for key, counter in self.counters.items()
                    if current_time >= counter['reset_time']
                ]
                for key in expired:
                    self.counters.pop(key)
                    
            except Exception as e:
                self.logger.error(f"清理计数器失败: {str(e)}")

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        return {
            'requests': {
                'total': self.stats['total_requests'],
                'limited': self.stats['limited_requests'],
                'burst': self.stats['burst_requests']
            },
            'resets': self.stats['reset_count'],
            'current_counters': {
                key: counter['count']
                for key, counter in self.counters.items()
            }
        }

# =============================== 
# 模块：响应处理
# ===============================
class ResponseHandler:
    """响应处理器 - 处理API响应"""
    def __init__(self, data_cache: MarketDataCache):
        self.logger = logging.getLogger(__name__)
        self.data_cache = data_cache
        
        # 响应处理器映射
        self.processors = {
            'market': self._process_market_data,
            'depth': self._process_depth_data,
            'trade': self._process_trade_data,
            'order': self._process_order_data,
            'account': self._process_account_data,
            'system': self._process_system_data
        }
        
        # 错误处理器
        self.error_handlers = {
            'timeout': self._handle_timeout,
            'rate_limit': self._handle_rate_limit,
            'invalid_key': self._handle_invalid_key,
            'server_error': self._handle_server_error,
            'network_error': self._handle_network_error,
            'invalid_request': self._handle_invalid_request
        }
        
        # 响应统计
        self.stats = {
            'total': 0,
            'success': 0,
            'errors': defaultdict(int),
            'latencies': [],
            'response_times': defaultdict(list),
            'error_rates': defaultdict(float)
        }

    async def process_response(self, response_data: Dict):
        """处理响应数据"""
        try:
            self.stats['total'] += 1
            start_time = time.perf_counter_ns()
            
            # 提取响应信息
            request = response_data['request']
            response = response_data['response']
            latency = response_data['latency']
            
            # 记录延迟
            self.stats['latencies'].append(latency)
            
            # 检查错误
            if 'error' in response:
                await self._handle_error(response['error'], request)
                return

            # 处理成功响应
            processor = self.processors.get(request['type'])
            if processor:
                await processor(response)
                self.stats['success'] += 1
                
                # 记录处理时间
                process_time = (time.perf_counter_ns() - start_time) / 1e6
                self.stats['response_times'][request['type']].append(process_time)
            else:
                self.logger.warning(f"未知的响应类型: {request['type']}")
            
            # 更新错误率
            self._update_error_rates()

        except Exception as e:
            self.logger.error(f"处理响应失败: {str(e)}")
            self.stats['errors']['process_error'] += 1

    async def _process_market_data(self, data: Dict):
        """处理市场数据"""
        try:
            await self.data_cache.update_price(data, source='rest')
        except Exception as e:
            self.logger.error(f"处理市场数据失败: {str(e)}")
            self.stats['errors']['market_process'] += 1

    async def _process_depth_data(self, data: Dict):
        """处理深度数据"""
        try:
            await self.data_cache.update_depth(data, source='rest')
        except Exception as e:
            self.logger.error(f"处理深度数据失败: {str(e)}")
            self.stats['errors']['depth_process'] += 1

    async def _process_trade_data(self, data: Dict):
        """处理成交数据"""
        try:
            await self.data_cache.update_trades(data)
        except Exception as e:
            self.logger.error(f"处理成交数据失败: {str(e)}")
            self.stats['errors']['trade_process'] += 1

    async def _process_order_data(self, data: Dict):
        """处理订单数据"""
        try:
            # TODO: 实现订单数据处理逻辑
            pass
        except Exception as e:
            self.logger.error(f"处理订单数据失败: {str(e)}")
            self.stats['errors']['order_process'] += 1

    async def _process_account_data(self, data: Dict):
        """处理账户数据"""
        try:
            # TODO: 实现账户数据处理逻辑
            pass
        except Exception as e:
            self.logger.error(f"处理账户数据失败: {str(e)}")
            self.stats['errors']['account_process'] += 1

    async def _process_system_data(self, data: Dict):
        """处理系统数据"""
        try:
            # TODO: 实现系统数据处理逻辑
            pass
        except Exception as e:
            self.logger.error(f"处理系统数据失败: {str(e)}")
            self.stats['errors']['system_process'] += 1

    async def _handle_error(self, error: Dict, request: Dict):
        """处理错误"""
        try:
            error_type = error.get('type', 'unknown')
            self.stats['errors'][error_type] += 1
            
            handler = self.error_handlers.get(error_type)
            if handler:
                await handler(error, request)
            else:
                self.logger.error(f"未处理的错误类型: {error_type}")
                
        except Exception as e:
            self.logger.error(f"处理错误失败: {str(e)}")
            self.stats['errors']['error_handle'] += 1

    async def _handle_timeout(self, error: Dict, request: Dict):
        """处理超时错误"""
        self.logger.warning(f"请求超时: {request['endpoint']}")
        # TODO: 实现重试逻辑

    async def _handle_rate_limit(self, error: Dict, request: Dict):
        """处理频率限制错误"""
        self.logger.warning(f"触发频率限制: {request['endpoint']}")
        # TODO: 实现延迟重试逻辑

    async def _handle_invalid_key(self, error: Dict, request: Dict):
        """处理无效密钥错误"""
        self.logger.error("API密钥无效或过期")
        # TODO: 实现通知逻辑

    async def _handle_server_error(self, error: Dict, request: Dict):
        """处理服务器错误"""
        self.logger.error(f"服务器错误: {error.get('message')}")
        # TODO: 实现故障转移逻辑

    async def _handle_network_error(self, error: Dict, request: Dict):
        """处理网络错误"""
        self.logger.error(f"网络错误: {error.get('message')}")
        # TODO: 实现网络重试逻辑

    async def _handle_invalid_request(self, error: Dict, request: Dict):
        """处理无效请求错误"""
        self.logger.error(f"无效请求: {error.get('message')}")
        # TODO: 实现请求验证逻辑

    def _update_error_rates(self):
        """更新错误率统计"""
        try:
            for error_type, count in self.stats['errors'].items():
                self.stats['error_rates'][error_type] = (
                    count / self.stats['total'] if self.stats['total'] > 0 else 0
                )
        except Exception as e:
            self.logger.error(f"更新错误率失败: {str(e)}")

    def get_statistics(self) -> Dict:
        """获取统计信息"""
        try:
            return {
                'requests': {
                    'total': self.stats['total'],
                    'success': self.stats['success'],
                    'success_rate': self.stats['success'] / self.stats['total'] if self.stats['total'] > 0 else 0
                },
                'errors': {
                    'counts': dict(self.stats['errors']),
                    'rates': dict(self.stats['error_rates'])
                },
                'latency': {
                    'average': sum(self.stats['latencies']) / len(self.stats['latencies']) if self.stats['latencies'] else 0,
                    'p95': sorted(self.stats['latencies'])[int(len(self.stats['latencies']) * 0.95)] if self.stats['latencies'] else 0
                },
                'processing_times': {
                    req_type: {
                        'average': sum(times) / len(times) if times else 0,
                        'p95': sorted(times)[int(len(times) * 0.95)] if times else 0
                    }
                    for req_type, times in self.stats['response_times'].items()
                }
            }
        except Exception as e:
            self.logger.error(f"获取统计信息失败: {str(e)}")
            return {}

# =============================== 
# 模块：执行管理
# ===============================
class OrderExecutor:
    """订单执行器 - 负责订单执行策略和风险控制"""
    def __init__(self, data_cache: MarketDataCache, config: ConfigManager):
        self.logger = logging.getLogger(__name__)
        self.data_cache = data_cache
        self.config = config
        self.printer = PrintManager()
        
        # 执行配置
        self.exec_config = {
            'max_retries': 3,          # 最大重试次数
            'retry_delay': 0.1,        # 重试延迟(秒)
            'timeout': 1.0,            # 超时时间(秒)
            'batch_size': 5,           # 批次大小
            'concurrent_orders': 3      # 并发订单数
        }
        
        # 风险控制配置
        self.risk_config = {
            'max_price_deviation': 0.05,  # 最大价格偏差
            'min_depth_ratio': 3.0,       # 最小深度比例
            'max_amount_ratio': 0.2,      # 最大数量比例
            'min_balance_ratio': 1.2      # 最小余额比例
        }
        
        # 订单跟踪
        self.order_tracker = {
            'active_orders': {},       # 活动订单
            'completed_orders': [],    # 已完成订单
            'failed_orders': [],       # 失败订单
            'last_order_time': 0,      # 最后下单时间
            'position': {              # 持仓信息
                'amount': 0,
                'avg_price': 0,
                'unrealized_pnl': 0
            }
        }
        
        # 执行统计
        self.stats = {
            'total_orders': 0,
            'successful_orders': 0,
            'failed_orders': 0,
            'retry_count': 0,
            'avg_latency': 0,
            'execution_times': []
        }

    async def execute_orders(self, symbol: str, amount: float, price: float) -> List[Dict]:
        """执行订单"""
        try:
            # 风险检查
            if not await self._check_risk(symbol, amount, price):
                raise ExecutionError("风险检查未通过")

            # 获取执行策略
            strategy = await self._get_execution_strategy(
                symbol, amount, price
            )

            # 执行订单
            orders = []
            for batch in strategy['batches']:
                batch_orders = await self._execute_batch(
                    symbol=symbol,
                    amount=batch['amount'],
                    price=batch['price'],
                    delay=batch['delay']
                )
                orders.extend(batch_orders)

            # 更新统计
            self._update_stats(orders)
            
            return orders

        except Exception as e:
            self.logger.error(f"订单执行失败: {str(e)}")
            return []

    async def _check_risk(self, symbol: str, amount: float, price: float) -> bool:
        """风险检查"""
        try:
            # 检查价格偏差
            market_price = await self.data_cache.get_latest_price(symbol)
            if market_price > 0:
                deviation = abs(price - market_price) / market_price
                if deviation > self.risk_config['max_price_deviation']:
                    self.logger.warning(f"价格偏差过大: {deviation:.2%}")
                    return False

            # 检查深度
            depth = await self.data_cache.get_depth(symbol)
            total_asks = sum(float(amount) for _, amount in depth['asks'])
            total_bids = sum(float(amount) for _, amount in depth['bids'])
            depth_ratio = min(total_asks, total_bids) / amount
            if depth_ratio < self.risk_config['min_depth_ratio']:
                self.logger.warning(f"市场深度不足: {depth_ratio:.2f}")
                return False

            # 检查余额
            balance = await self._get_balance()
            if balance < amount * self.risk_config['min_balance_ratio']:
                self.logger.warning("余额不足")
                return False

            return True

        except Exception as e:
            self.logger.error(f"风险检查失败: {str(e)}")
            return False

    async def _get_execution_strategy(self, symbol: str, amount: float, price: float) -> Dict:
        """获取执行策略"""
        try:
            # 基于市场状态选择策略
            market_status = await self.data_cache.get_latest_data()
            
            # 计算批次
            batch_size = amount / self.exec_config['concurrent_orders']
            batches = []
            
            for i in range(self.exec_config['concurrent_orders']):
                batch = {
                    'amount': batch_size,
                    'price': price,
                    'delay': i * 0.1  # 100ms间隔
                }
                batches.append(batch)

            return {
                'type': 'concurrent',
                'batches': batches
            }

        except Exception as e:
            self.logger.error(f"获取执行策略失败: {str(e)}")
            return {'type': 'simple', 'batches': [{'amount': amount, 'price': price, 'delay': 0}]}

    async def _execute_batch(self, symbol: str, amount: float, price: float, delay: float) -> List[Dict]:
        """执行订单批次"""
        try:
            # 创建订单任务
            tasks = []
            for _ in range(self.exec_config['concurrent_orders']):
                task = self._place_single_order(
                    symbol=symbol,
                    amount=amount/self.exec_config['concurrent_orders'],
                    price=price,
                    delay=delay
                )
                tasks.append(task)

            # 并发执行订单
            orders = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 过滤成功订单
            return [
                order for order in orders 
                if isinstance(order, dict) and order.get('status') == 'filled'
            ]

        except Exception as e:
            self.logger.error(f"批次执行失败: {str(e)}")
            return []

    async def _place_single_order(self, symbol: str, amount: float, price: float, delay: float) -> Optional[Dict]:
        """执行单个订单"""
        try:
            # 等待指定延迟
            if delay > 0:
                await asyncio.sleep(delay)

            # 构建订单参数
            order_params = {
                'symbol': symbol,
                'side': 'BUY',
                'type': 'LIMIT',
                'timeInForce': 'IOC',
                'quantity': amount,
                'price': price
            }

            # 执行订单
            start_time = time.perf_counter_ns()
            order = await self.config.trade_client.create_order(**order_params)
            latency = (time.perf_counter_ns() - start_time) / 1e6

            # 更新订单跟踪
            self.order_tracker['active_orders'][order['id']] = {
                'order': order,
                'start_time': time.time(),
                'latency': latency
            }

            # 更新统计
            self.stats['execution_times'].append(latency)
            self.stats['avg_latency'] = sum(self.stats['execution_times'][-100:]) / len(self.stats['execution_times'][-100:])

            return order

        except Exception as e:
            self.logger.error(f"订单执行失败: {str(e)}")
            return None

    async def track_orders(self):
        """跟踪订单状态"""
        while True:
            try:
                # 更新活动订单状态
                for order_id, order_info in list(self.order_tracker['active_orders'].items()):
                    order = await self.config.trade_client.fetch_order(order_id)
                    
                    # 更新订单状态
                    if order['status'] in ['filled', 'canceled', 'rejected']:
                        if order['status'] == 'filled':
                            self.order_tracker['completed_orders'].append(order)
                            self.stats['successful_orders'] += 1
                        else:
                            self.order_tracker['failed_orders'].append(order)
                            self.stats['failed_orders'] += 1
                            
                        del self.order_tracker['active_orders'][order_id]
                
                await asyncio.sleep(1)  # 每秒检查一次
                
            except Exception as e:
                self.logger.error(f"订单跟踪失败: {str(e)}")
                await asyncio.sleep(5)

    def get_order_stats(self) -> Dict:
        """获取订单统计"""
        return {
            'active_orders': len(self.order_tracker['active_orders']),
            'completed_orders': len(self.order_tracker['completed_orders']),
            'failed_orders': len(self.order_tracker['failed_orders']),
            'success_rate': (
                self.stats['successful_orders'] / self.stats['total_orders']
                if self.stats['total_orders'] > 0 else 0
            ),
            'avg_latency': self.stats['avg_latency'],
            'position': self.order_tracker['position']
        }

    def _update_stats(self, orders: List[Dict]):
        """更新执行统计"""
        try:
            self.stats['total_orders'] += len(orders)
            self.stats['successful_orders'] += len([
                o for o in orders if o.get('status') == 'filled'
            ])
            self.stats['failed_orders'] += len([
                o for o in orders if o.get('status') != 'filled'
            ])
            
            # 更新延迟统计
            latencies = [o.get('latency', 0) for o in orders if 'latency' in o]
            if latencies:
                self.stats['execution_times'].extend(latencies)
                self.stats['avg_latency'] = sum(latencies) / len(latencies)

        except Exception as e:
            self.logger.error(f"更新统计失败: {str(e)}")

    async def _get_balance(self):
        """获取账户余额"""
        try:
            # 获取账户信息
            account = await self.config.trade_client.fetch_account()
            balances = account.get('balances', [])
            usdt_balance = sum(float(balance['free']) for balance in balances if balance['asset'] == 'USDT')
            return usdt_balance

        except Exception as e:
            self.logger.error(f"获取账户余额失败: {str(e)}")
            return 0

class ExecutionStrategy:
    """执行策略 - 负责订单执行策略"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # 策略配置
        self.strategy_config = {
            'aggressive': {  # 激进策略
                'batch_count': 3,      # 分3批
                'time_advance': 50,    # 提前50ms
                'retry_count': 3,      # 重试3次
                'price_adjust': 0.002  # 价格调整0.2%
            },
            'balanced': {    # 均衡策略
                'batch_count': 2,      # 分2批
                'time_advance': 100,   # 提前100ms
                'retry_count': 2,      # 重试2次
                'price_adjust': 0.001  # 价格调整0.1%
            },
            'conservative': { # 保守策略
                'batch_count': 1,      # 一次性
                'time_advance': 200,   # 提前200ms
                'retry_count': 1,      # 重试1次
                'price_adjust': 0      # 不调整价格
            }
        }
        
        # 策略统计
        self.strategy_stats = {
            'strategy_used': defaultdict(int),
            'success_rate': defaultdict(list),
            'execution_time': defaultdict(list)
        }

    async def analyze_market(self, symbol: str) -> Dict:
        """分析市场状态"""
        try:
            # 获取深度数据
            depth = await self.data_cache.get_depth(symbol)
            
            # 计算买卖压力
            sell_pressure = sum(float(amount) for _, amount in depth['asks'][:5])
            buy_pressure = sum(float(amount) for _, amount in depth['bids'][:5])
            
            # 计算价格波动
            trades = await self.data_cache.get_recent_trades(symbol)
            prices = [float(trade['price']) for trade in trades[-100:]]
            volatility = np.std(prices) / np.mean(prices) if prices else 0
            
            return {
                'sell_pressure': sell_pressure,
                'buy_pressure': buy_pressure,
                'pressure_ratio': sell_pressure / buy_pressure if buy_pressure > 0 else float('inf'),
                'volatility': volatility,
                'depth_quality': len(depth['asks']) + len(depth['bids']),
                'market_activity': len(trades)
            }
            
        except Exception as e:
            self.logger.error(f"市场分析失败: {str(e)}")
            return {}

    def select_strategy(self, market_analysis: Dict) -> str:
        """选择执行策略"""
        try:
            # 基于市场分析选择策略
            if market_analysis.get('volatility', 0) > 0.02 or \
               market_analysis.get('pressure_ratio', 1) > 2:
                return 'conservative'  # 高波动或高卖压时保守
                
            elif market_analysis.get('depth_quality', 0) > 100 and \
                 market_analysis.get('market_activity', 0) > 1000:
                return 'aggressive'   # 深度好且活跃时激进
                
            else:
                return 'balanced'     # 其他情况均衡
                
        except Exception as e:
            self.logger.error(f"策略选择失败: {str(e)}")
            return 'conservative'  # 出错时使用保守策略

    def calculate_batches(self, amount: float, price: float, 
                         batch_count: int) -> List[Dict]:
        """计算订单批次"""
        try:
            base_amount = amount / batch_count
            batches = []
            
            for i in range(batch_count):
                # 计算每批次的数量和价格调整
                batch_amount = base_amount * (1 + 0.1 * (i - batch_count//2))
                batch_price = price * (1 + 0.001 * (i - batch_count//2))
                
                batch = {
                    'amount': batch_amount,
                    'price': batch_price,
                    'delay': i * 0.1  # 100ms间隔
                }
                batches.append(batch)
                
            return batches
            
        except Exception as e:
            self.logger.error(f"批次计算失败: {str(e)}")
            return [{'amount': amount, 'price': price, 'delay': 0}]

    async def get_strategy(self, symbol: str, amount: float, price: float) -> Dict:
        """获取执行策略"""
        try:
            # 分析市场状态
            market_analysis = await self.analyze_market(symbol)
            
            # 选择策略类型
            strategy_type = self.select_strategy(market_analysis)
            self.strategy_stats['strategy_used'][strategy_type] += 1
            
            # 获取策略配置
            config = self.strategy_config[strategy_type]
            
            # 计算批次
            batches = self.calculate_batches(
                amount=amount,
                price=price,
                batch_count=config['batch_count']
            )
            
            return {
                'type': strategy_type,
                'config': config,
                'batches': batches,
                'market_analysis': market_analysis
            }
            
        except Exception as e:
            self.logger.error(f"获取策略失败: {str(e)}")
            return {
                'type': 'conservative',
                'config': self.strategy_config['conservative'],
                'batches': [{'amount': amount, 'price': price, 'delay': 0}],
                'market_analysis': {}
            }

    def update_stats(self, strategy_type: str, success: bool, execution_time: float):
        """更新策略统计"""
        try:
            self.strategy_stats['success_rate'][strategy_type].append(1 if success else 0)
            self.strategy_stats['execution_time'][strategy_type].append(execution_time)
            
            # 保持最近1000条记录
            if len(self.strategy_stats['success_rate'][strategy_type]) > 1000:
                self.strategy_stats['success_rate'][strategy_type].pop(0)
            if len(self.strategy_stats['execution_time'][strategy_type]) > 1000:
                self.strategy_stats['execution_time'][strategy_type].pop(0)
                
        except Exception as e:
            self.logger.error(f"更新统计失败: {str(e)}")

    def get_stats(self) -> Dict:
        """获取策略统计"""
        try:
            stats = {
                'usage': dict(self.strategy_stats['strategy_used']),
                'success_rate': {},
                'avg_execution_time': {}
            }
            
            # 计算成功率
            for strategy in self.strategy_stats['success_rate']:
                rates = self.strategy_stats['success_rate'][strategy]
                stats['success_rate'][strategy] = sum(rates) / len(rates) if rates else 0
                
            # 计算平均执行时间
            for strategy in self.strategy_stats['execution_time']:
                times = self.strategy_stats['execution_time'][strategy]
                stats['avg_execution_time'][strategy] = sum(times) / len(times) if times else 0
                
            return stats
            
        except Exception as e:
            self.logger.error(f"获取统计失败: {str(e)}")

# =============================== 
# 模块：配置管理
# ===============================
class ConfigManager:
    """配置管理器 - 管理程序配置"""
    def __init__(self, config_file: str):
        self.logger = logging.getLogger(__name__)
        self.config_file = config_file
        self.api_manager = APIKeyManager()
        
        # 默认配置
        self.default_config = {
            'trading': {
                'max_slippage': 0.01,    # 最大滑点
                'min_profit': 0.005,     # 最小利润
                'max_amount': 1000,      # 最大交易量
                'default_timeout': 5      # 默认超时(秒)
            },
            'network': {
                'max_retries': 3,        # 最大重试次数
                'retry_delay': 0.5,      # 重试延迟(秒)
                'connection_timeout': 5,  # 连接超时(秒)
                'read_timeout': 30        # 读取超时(秒)
            },
            'risk': {
                'max_price_deviation': 0.05,  # 最大价格偏差
                'min_depth_ratio': 3.0,       # 最小深度比例
                'max_amount_ratio': 0.2,      # 最大数量比例
                'min_balance_ratio': 1.2      # 最小余额比例
            }
        }
        
        # 加载配置
        self.config = self.load_config()

    def load_config(self) -> Dict:
        """加载配置"""
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                    
                # 合并默认配置
                return self._merge_config(self.default_config, config)
            return self.default_config.copy()
            
        except Exception as e:
            self.logger.error(f"加载配置失败: {str(e)}")
            return self.default_config.copy()

    def save_config(self) -> bool:
        """保存配置"""
        try:
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            return True
            
        except Exception as e:
            self.logger.error(f"保存配置失败: {str(e)}")
            return False

    def _merge_config(self, default: Dict, custom: Dict) -> Dict:
        """合并配置"""
        result = default.copy()
        
        for key, value in custom.items():
            if key in result and isinstance(result[key], dict):
                if isinstance(value, dict):
                    result[key] = self._merge_config(result[key], value)
            else:
                result[key] = value
                
        return result

    def get_config(self, section: str = None) -> Dict:
        """获取配置"""
        if section:
            return self.config.get(section, {})
        return self.config

    def update_config(self, section: str, key: str, value: Any) -> bool:
        """更新配置"""
        try:
            if section not in self.config:
                self.config[section] = {}
            self.config[section][key] = value
            return self.save_config()
            
        except Exception as e:
            self.logger.error(f"更新配置失败: {str(e)}")
            return False

    def validate_config(self) -> bool:
        """验证配置"""
        try:
            # 验证必需的配置项
            required_sections = {'trading', 'network', 'risk'}
            if not all(section in self.config for section in required_sections):
                return False
                
            # 验证配置值
            trading = self.config['trading']
            if not (0 < trading['max_slippage'] < 1 and 
                   0 < trading['min_profit'] < 1 and
                   trading['max_amount'] > 0 and
                   trading['default_timeout'] > 0):
                return False
                
            network = self.config['network']
            if not (network['max_retries'] > 0 and
                   network['retry_delay'] > 0 and
                   network['connection_timeout'] > 0 and
                   network['read_timeout'] > 0):
                return False
                
            risk = self.config['risk']
            if not (0 < risk['max_price_deviation'] < 1 and
                   risk['min_depth_ratio'] > 0 and
                   0 < risk['max_amount_ratio'] < 1 and
                   risk['min_balance_ratio'] > 1):
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"验证配置失败: {str(e)}")
            return False

class APIKeyManager:
    """API密钥管理器 - 管理API密钥和权限"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # API密钥配置
        self.keys = {
            'trade': {
                'api_key': '',
                'api_secret': '',
                'permissions': set()
            },
            'query': {
                'api_key': '',
                'api_secret': '',
                'permissions': set()
            }
        }
        
        # 权限配置
        self.required_permissions = {
            'trade': {'SPOT_TRADE', 'USER_DATA'},
            'query': {'MARKET_DATA', 'USER_STREAM'}
        }
        
        # 密钥统计
        self.key_stats = {
            'last_update': defaultdict(float),
            'validation_count': defaultdict(int),
            'error_count': defaultdict(int)
        }

    async def set_api_keys(self, key_type: str, api_key: str, api_secret: str) -> bool:
        """设置API密钥"""
        try:
            if key_type not in self.keys:
                raise ValueError(f"无效的密钥类型: {key_type}")
                
            # 验证密钥格式
            if not self._validate_key_format(api_key, api_secret):
                raise ValueError("密钥格式无效")
                
            # 验证密钥权限
            permissions = await self._check_permissions(api_key, api_secret)
            required = self.required_permissions[key_type]
            if not required.issubset(permissions):
                missing = required - permissions
                raise ValueError(f"缺少所需权限: {missing}")
                
            # 保存密钥
            self.keys[key_type].update({
                'api_key': api_key,
                'api_secret': api_secret,
                'permissions': permissions
            })
            
            # 更新统计
            self.key_stats['last_update'][key_type] = time.time()
            self.key_stats['validation_count'][key_type] += 1
            
            self.logger.info(f"已更新 {key_type} API密钥")
            return True
            
        except Exception as e:
            self.logger.error(f"设置API密钥失败: {str(e)}")
            self.key_stats['error_count'][key_type] += 1
            return False

    def get_api_keys(self, key_type: str) -> Tuple[str, str]:
        """获取API密钥"""
        if key_type not in self.keys:
            raise ValueError(f"无效的密钥类型: {key_type}")
            
        keys = self.keys[key_type]
        return keys['api_key'], keys['api_secret']

    def _validate_key_format(self, api_key: str, api_secret: str) -> bool:
        """验证密钥格式"""
        try:
            # 检查长度
            if len(api_key) != 64 or len(api_secret) != 64:
                return False
                
            # 检查字符
            valid_chars = set('0123456789abcdefABCDEF')
            if not all(c in valid_chars for c in api_key + api_secret):
                return False
                
            return True
            
        except Exception:
            return False

    async def _check_permissions(self, api_key: str, api_secret: str) -> Set[str]:
        """检查API权限"""
        try:
            # 创建临时客户端
            client = ccxt.binance({
                'apiKey': api_key,
                'secret': api_secret
            })
            
            # 获取账户信息
            account = await client.fetch_account()
            
            # 解析权限
            permissions = set(account.get('permissions', []))
            
            return permissions
            
        except Exception as e:
            self.logger.error(f"检查API权限失败: {str(e)}")
            return set()

    def get_key_stats(self) -> Dict:
        """获取密钥统计"""
        return {
            key_type: {
                'last_update': self.key_stats['last_update'][key_type],
                'validation_count': self.key_stats['validation_count'][key_type],
                'error_count': self.key_stats['error_count'][key_type],
                'has_required_permissions': (
                    self.required_permissions[key_type].issubset(
                        self.keys[key_type]['permissions']
                    )
                )
            }
            for key_type in self.keys
        }

# =============================== 
# 模块：请求调度
# ===============================
class RequestScheduler:
    """请求调度器 - 实现精确的请求控制"""
    def __init__(self, data_cache: MarketDataCache):
        self.logger = logging.getLogger(__name__)
        self.data_cache = data_cache
        
        # 初始化组件
        self.ip_manager = IPManager()
        self.rate_limiter = RateLimiter()
        self.response_handler = ResponseHandler(data_cache)
        
        # 调度配置
        self.schedule_config = {
            'cycle_ms': 54,        # 54毫秒完整周期
            'ip_window_ms': 18,    # 每IP 18毫秒窗口
            'batch_size': 5,       # 批次大小
            'retry_count': 3,      # 重试次数
            'timeout': 5.0         # 超时时间(秒)
        }
        
        # 运行状态
        self.is_running = False
        self.last_request_time = 0
        self.request_queue = asyncio.Queue()
        self.response_queue = asyncio.Queue()
        
        # 统计信息
        self.stats = {
            'total_requests': 0,
            'successful_requests': 0,
            'failed_requests': 0,
            'retry_count': 0,
            'avg_latency': 0,
            'request_times': []
        }

    async def start(self):
        """启动调度器"""
        if self.is_running:
            return
            
        self.is_running = True
        asyncio.create_task(self._request_sender())
        asyncio.create_task(self._response_handler())
        self.logger.info("请求调度器已启动")

    async def stop(self):
        """停止调度器"""
        self.is_running = False
        # 等待队列处理完成
        await self.request_queue.join()
        await self.response_queue.join()
        self.logger.info("请求调度器已停止")

    async def schedule_request(self, request: Dict):
        """调度请求"""
        try:
            self.stats['total_requests'] += 1
            await self.request_queue.put(request)
            
        except Exception as e:
            self.logger.error(f"请求调度失败: {str(e)}")
            self.stats['failed_requests'] += 1

    async def _request_sender(self):
        """请求发送循环"""
        while self.is_running:
            try:
                current_ms = time.time() * 1000
                if current_ms - self.last_request_time >= self.schedule_config['cycle_ms']:
                    # 获取当前可用IP
                    active_ip = await self.ip_manager.get_best_ip()
                    if active_ip and not self.request_queue.empty():
                        request = await self.request_queue.get()
                        asyncio.create_task(self._execute_request(request, active_ip))
                        self.last_request_time = current_ms
                await asyncio.sleep(0.001)  # 1ms休眠
                
            except Exception as e:
                self.logger.error(f"请求发送异常: {str(e)}")
                await asyncio.sleep(1)  # 错误后等待1秒

    async def _execute_request(self, request: Dict, ip: str):
        """执行请求"""
        try:
            # 检查频率限制
            if not await self.rate_limiter.check_limit(request['endpoint']):
                await self.request_queue.put(request)
                self.stats['retry_count'] += 1
                return

            # 执行请求
            start_time = time.perf_counter_ns()
            response = await self._do_request(request, ip)
            latency = (time.perf_counter_ns() - start_time) / 1e6

            # 更新统计
            self.stats['request_times'].append(latency)
            self.stats['avg_latency'] = (
                sum(self.stats['request_times'][-100:]) / 
                len(self.stats['request_times'][-100:])
            )

            # 更新IP状态
            await self.ip_manager.update_ip_status(ip, latency, error=False)
            
            # 处理响应
            await self.response_queue.put({
                'request': request,
                'response': response,
                'latency': latency,
                'ip': ip
            })
            
            self.stats['successful_requests'] += 1

        except Exception as e:
            self.logger.error(f"请求执行失败: {str(e)}")
            self.stats['failed_requests'] += 1
            await self.ip_manager.update_ip_status(ip, 0, error=True)
            
        finally:
            self.request_queue.task_done()

    async def _do_request(self, request: Dict, ip: str) -> Dict:
        """执行实际的请求"""
        try:
            # 构建请求参数
            params = request.get('params', {})
            params['ip'] = ip
            
            # 执行请求
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method=request['method'],
                    url=request['url'],
                    params=params,
                    headers=request.get('headers'),
                    timeout=self.schedule_config['timeout']
                ) as response:
                    return await response.json()
                    
        except Exception as e:
            self.logger.error(f"请求执行失败: {str(e)}")
            raise

    def get_stats(self) -> Dict:
        """获取调度器统计信息"""
        return {
            'queue_size': {
                'request': self.request_queue.qsize(),
                'response': self.response_queue.qsize()
            },
            'requests': {
                'total': self.stats['total_requests'],
                'successful': self.stats['successful_requests'],
                'failed': self.stats['failed_requests'],
                'retry_count': self.stats['retry_count']
            },
            'latency': {
                'average': self.stats['avg_latency'],
                'p95': sorted(self.stats['request_times'])[-50] if self.stats['request_times'] else 0
            }
        }

# =============================== 
# 模块：风险控制
# ===============================
class RiskController:
    """风险控制器 - 负责交易风险管理"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # 风险控制配置
        self.risk_config = {
            'price': {
                'max_deviation': 0.05,    # 最大价格偏差
                'min_depth_ratio': 3.0,   # 最小深度比例
                'max_impact': 0.02        # 最大市场影响
            },
            'amount': {
                'max_ratio': 0.2,         # 最大数量比例
                'min_balance': 1.2,       # 最小余额比例
                'max_exposure': 0.5       # 最大风险敞口
            },
            'time': {
                'max_delay': 500,         # 最大延迟(ms)
                'min_update_interval': 100 # 最小更新间隔(ms)
            }
        }
        
        # 风险统计
        self.risk_stats = {
            'checks': defaultdict(int),
            'violations': defaultdict(int),
            'last_check': defaultdict(float),
            'risk_levels': defaultdict(list)
        }

    async def check_risk(self, symbol: str, amount: float, price: float) -> bool:
        """检查交易风险"""
        try:
            self.risk_stats['checks']['total'] += 1
            current_time = time.time()
            
            # 检查价格风险
            if not await self._check_price_risk(symbol, price):
                self.risk_stats['violations']['price'] += 1
                return False
                
            # 检查数量风险
            if not await self._check_amount_risk(symbol, amount):
                self.risk_stats['violations']['amount'] += 1
                return False
                
            # 检查时间风险
            if not await self._check_time_risk(symbol):
                self.risk_stats['violations']['time'] += 1
                return False
                
            # 更新统计
            self.risk_stats['last_check'][symbol] = current_time
            return True
            
        except Exception as e:
            self.logger.error(f"风险检查失败: {str(e)}")
            self.risk_stats['violations']['check_error'] += 1
            return False

    async def _check_price_risk(self, symbol: str, price: float) -> bool:
        """检查价格风险"""
        try:
            # 获取市场价格
            market_price = await self._get_market_price(symbol)
            
            # 计算价格偏差
            deviation = abs(price - market_price) / market_price
            
            # 检查价格偏差
            if deviation > self.risk_config['price']['max_deviation']:
                self.logger.warning(f"价格偏差过大: {deviation:.2%}")
                return False
                
            # 检查深度比例
            depth_ratio = await self._calculate_depth_ratio(symbol, price)
            if depth_ratio < self.risk_config['price']['min_depth_ratio']:
                self.logger.warning(f"深度比例不足: {depth_ratio:.2f}")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"价格风险检查失败: {str(e)}")
            return False

    async def _check_amount_risk(self, symbol: str, amount: float) -> bool:
        """检查数量风险"""
        try:
            # 获取账户余额
            balance = await self._get_account_balance(symbol)
            
            # 计算数量比例
            amount_ratio = amount / balance
            
            # 检查数量比例
            if amount_ratio > self.risk_config['amount']['max_ratio']:
                self.logger.warning(f"数量比例过大: {amount_ratio:.2%}")
                return False
                
            # 检查余额比例
            if balance < amount * self.risk_config['amount']['min_balance']:
                self.logger.warning("余额不足")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"数量风险检查失败: {str(e)}")
            return False

    async def _check_time_risk(self, symbol: str) -> bool:
        """检查时间风险"""
        try:
            current_time = time.time() * 1000
            
            # 检查更新延迟
            last_update = await self._get_last_update_time(symbol)
            if current_time - last_update > self.risk_config['time']['max_delay']:
                self.logger.warning(f"数据更新延迟过大: {current_time - last_update}ms")
                return False
                
            # 检查更新间隔
            last_check = self.risk_stats['last_check'][symbol]
            if current_time - last_check * 1000 < self.risk_config['time']['min_update_interval']:
                self.logger.warning("检查间隔过短")
                return False
                
            return True
            
        except Exception as e:
            self.logger.error(f"时间风险检查失败: {str(e)}")
            return False

    def get_risk_stats(self) -> Dict:
        """获取风险统计"""
        return {
            'checks': dict(self.risk_stats['checks']),
            'violations': dict(self.risk_stats['violations']),
            'violation_rates': {
                risk_type: (
                    self.risk_stats['violations'][risk_type] / 
                    self.risk_stats['checks']['total']
                    if self.risk_stats['checks']['total'] > 0 else 0
                )
                for risk_type in self.risk_stats['violations']
            }
        }

# =============================== 
# 模块：时间同步
# ===============================
class TimeSync:
    """时间同步器 - 维护与服务器的时间同步"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # 同步配置
        self.sync_config = {
            'sync_interval': 30,    # 同步间隔(秒)
            'max_offset': 1000,     # 最大偏移(ms)
            'min_samples': 5,       # 最小样本数
            'max_retry': 3          # 最大重试次数
        }
        
        # 时间统计
        self.time_stats = {
            'offset_samples': [],   # 偏移样本
            'last_sync': 0,         # 最后同步时间
            'sync_count': 0,        # 同步次数
            'error_count': 0        # 错误次数
        }
        
        # 时间偏移
        self.time_offset = 0

    async def sync(self):
        """执行时间同步"""
        try:
            self.time_stats['sync_count'] += 1
            
            # 获取服务器时间
            server_time = await self._get_server_time()
            
            # 计算时间偏移
            local_time = time.time() * 1000
            offset = server_time - local_time
            
            # 更新偏移样本
            self.time_stats['offset_samples'].append(offset)
            self.time_stats['offset_samples'] = self.time_stats['offset_samples'][-100:]
            
            # 计算平均偏移
            if len(self.time_stats['offset_samples']) >= self.sync_config['min_samples']:
                self.time_offset = sum(self.time_stats['offset_samples']) / len(self.time_stats['offset_samples'])
            
            # 检查偏移
            if abs(self.time_offset) > self.sync_config['max_offset']:
                self.logger.warning(f"时间偏移过大: {self.time_offset:.2f}ms")
            
            self.time_stats['last_sync'] = local_time
            
        except Exception as e:
            self.logger.error(f"时间同步失败: {str(e)}")
            self.time_stats['error_count'] += 1

    async def _get_server_time(self) -> int:
        """获取服务器时间"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.binance.com/api/v3/time') as response:
                    data = await response.json()
                    return data['serverTime']
                    
        except Exception as e:
            self.logger.error(f"获取服务器时间失败: {str(e)}")
            raise

    def get_time_stats(self) -> Dict:
        """获取时间统计"""
        return {
            'offset': {
                'current': self.time_offset,
                'average': sum(self.time_stats['offset_samples']) / len(self.time_stats['offset_samples'])
                if self.time_stats['offset_samples'] else 0,
                'max': max(self.time_stats['offset_samples']) if self.time_stats['offset_samples'] else 0,
                'min': min(self.time_stats['offset_samples']) if self.time_stats['offset_samples'] else 0
            },
            'sync': {
                'last_sync': self.time_stats['last_sync'],
                'total_count': self.time_stats['sync_count'],
                'error_count': self.time_stats['error_count']
            }
        }

    def get_adjusted_time(self) -> float:
        """获取校正后的时间"""
        return time.time() * 1000 + self.time_offset

# =============================== 
# 模块：主控制类
# ===============================
class BinanceSniper:
    """币安抢币工具主控制类"""
    def __init__(self, config_file: str):
        self.logger = logging.getLogger(__name__)
        
        # 初始化配置管理
        self.config_manager = ConfigManager(config_file)
        self.api_manager = APIKeyManager()
        
        # 初始化数据组件
        self.data_cache = MarketDataCache()
        self.market_monitor = MarketMonitor(self.data_cache)
        self.cache_manager = CacheManager(self.data_cache)
        
        # 初始化网络组件
        self.request_scheduler = None  # 延迟初始化
        self.time_sync = None  # 延迟初始化
        
        # 初始化执行组件
        self.order_executor = None  # 延迟初始化
        
        # 运行状态
        self.is_running = False
        self.start_time = 0
        self.cleanup_tasks = []

    async def initialize(self) -> bool:
        """初始化系统"""
        try:
            self.logger.info("开始初始化系统...")
            
            # 验证配置
            if not self.config_manager.validate_config():
                raise ValueError("配置验证失败")
                
            # 设置API密钥
            trade_config = self.config_manager.get_config('trading')
            await self.api_manager.set_api_keys(
                'trade',
                trade_config['api_key'],
                trade_config['api_secret']
            )
            
            # 初始化网络组件
            self.request_scheduler = RequestScheduler(self.data_cache)
            
            # 初始化执行组件
            self.order_executor = OrderExecutor(
                self.data_cache,
                self.config_manager
            )
            
            # 启动监控和清理任务
            await self._start_background_tasks()
            
            self.is_running = True
            self.start_time = time.time()
            
            self.logger.info("系统初始化完成")
            return True
            
        except Exception as e:
            self.logger.error(f"系统初始化失败: {str(e)}")
            return False

    async def _start_background_tasks(self):
        """启动后台任务"""
        try:
            # 启动市场监控
            await self.market_monitor.start_monitoring()
            
            # 启动缓存管理
            await self.cache_manager.start()
            
            # 启动请求调度
            await self.request_scheduler.start()
            
            # 记录任务以便清理
            self.cleanup_tasks = [
                self.market_monitor.monitor_task,
                self.cache_manager.cleanup_task,
                self.request_scheduler
            ]
            
        except Exception as e:
            self.logger.error(f"启动后台任务失败: {str(e)}")
            raise

    async def cleanup(self):
        """清理资源"""
        try:
            self.logger.info("开始清理资源...")
            self.is_running = False
            
            # 停止市场监控
            await self.market_monitor.stop_monitoring()
            
            # 停止缓存管理
            await self.cache_manager.stop()
            
            # 停止请求调度
            await self.request_scheduler.stop()
            
            # 保存配置
            self.config_manager.save_config()
            
            self.logger.info("资源清理完成")
            
        except Exception as e:
            self.logger.error(f"资源清理失败: {str(e)}")

    async def execute_snipe(self, symbol: str, amount: float, 
                          price: float) -> Optional[Dict]:
        """执行抢币"""
        try:
            if not self.is_running:
                raise RuntimeError("系统未初始化")
                
            self.logger.info(f"""
开始执行抢币:
- 交易对: {symbol}
- 数量: {amount}
- 价格: {price}
""")
            
            # 检查市场状态
            market_status = await self.market_monitor.analyze_market_status()
            if market_status['status'] != 'normal':
                raise RuntimeError(f"市场状态异常: {market_status['status']}")
            
            # 执行订单
            orders = await self.order_executor.execute_orders(
                symbol=symbol,
                amount=amount,
                price=price
            )
            
            if not orders:
                self.logger.warning("未成功执行任何订单")
                return None
                
            # 返回最优订单
            best_order = max(
                orders,
                key=lambda x: x.get('filled', 0) * x.get('price', 0)
            )
            
            self.logger.info(f"""
抢币执行完成:
- 订单ID: {best_order['id']}
- 成交价: {best_order['price']}
- 成交量: {best_order['filled']}
- 状态: {best_order['status']}
""")
            
            return best_order
            
        except Exception as e:
            self.logger.error(f"抢币执行失败: {str(e)}")
            return None

    def get_status(self) -> Dict:
        """获取系统状态"""
        return {
            'is_running': self.is_running,
            'uptime': time.time() - self.start_time if self.is_running else 0,
            'market_status': self.market_monitor.get_status(),
            'cache_stats': self.cache_manager.get_stats(),
            'execution_stats': self.order_executor.stats if self.order_executor else {},
            'request_stats': self.request_scheduler.get_stats() if self.request_scheduler else {}
        }

    async def show_menu(self):
        """显示主菜单"""
        self.printer.print_header()
        while True:
            try:
                menu = """
=== 币安现货抢币工具 ===
1. 设置API密钥
2. 抢开盘策略设置
3. 查看当前策略
4. 开始抢购
5. 测试中心
0. 退出
=====================

请选择操作 (0-5): """
                choice = input(menu)
                
                if choice == '1':
                    await self._set_api_keys()
                elif choice == '2':
                    await self._set_strategy()
                elif choice == '3':
                    await self._show_current_strategy()
                elif choice == '4':
                    await self._start_snipe()
                elif choice == '5':
                    await self._run_tests()
                elif choice == '0':
                    print("\n正在退出程序...")
                    await self.stop()
                    break
                else:
                    print("\n无效的选择，请重试")
                    
            except Exception as e:
                self.logger.error(f"菜单操作异常: {str(e)}")
                print("\n操作出现错误，请重试")

    async def _set_api_keys(self):
        """设置API密钥"""
        try:
            print("\n=== API密钥设置 ===")
            api_key = input("请输入API Key: ").strip()
            api_secret = input("请输入API Secret: ").strip()
            
            if await self.config.set_api_keys(api_key, api_secret):
                self.printer.print_success("API密钥设置成功!")
            else:
                self.printer.print_error("API密钥设置失败!")
                
        except Exception as e:
            self.logger.error(f"设置API密钥失败: {str(e)}")
            self.printer.print_error("设置过程出现错误")

    async def _set_strategy(self):
        """设置抢购策略"""
        try:
            print("\n=== 抢购策略设置 ===")
            
            # 交易对设置
            symbol = input("请输入交易对 (例如 BTCUSDT): ").strip().upper()
            
            # 金额设置
            amount = float(input("请输入买入金额 (USDT): ").strip())
            
            # 价格限制
            max_price = float(input("请输入最高限价 (USDT, 0表示不限制): ").strip())
            
            # 止盈设置
            take_profits = []
            print("\n设置阶梯止盈 (最多5档):")
            for i in range(5):
                profit = input(f"第{i+1}档止盈比例 (%, 直接回车结束): ").strip()
                if not profit:
                    break
                amount = input(f"第{i+1}档卖出比例 (%): ").strip()
                take_profits.append((float(profit)/100, float(amount)/100))
                
            # 止损设置
            stop_loss = float(input("\n请输入止损比例 (%): ").strip()) / 100
            
            # 保存策略
            strategy = {
                'symbol': symbol,
                'amount': amount,
                'max_price': max_price,
                'take_profits': take_profits,
                'stop_loss': stop_loss
            }
            
            if await self.config.save_strategy(strategy):
                self.printer.print_success("策略设置成功!")
            else:
                self.printer.print_error("策略设置失败!")
                
        except Exception as e:
            self.logger.error(f"设置策略失败: {str(e)}")
            self.printer.print_error("设置过程出现错误")

    async def _show_current_strategy(self):
        """显示当前策略"""
        try:
            strategy = await self.config.get_strategy()
            if not strategy:
                self.printer.print_warning("当前没有设置策略")
                return
                
            # 获取市场数据
            market_data = await self.market_monitor.get_market_data(strategy['symbol'])
            
            # 显示策略详情
            self.printer.print_strategy_status({
                'symbol': strategy['symbol'],
                'amount': strategy['amount'],
                'max_price': strategy['max_price'],
                'take_profits': strategy['take_profits'],
                'stop_loss': strategy['stop_loss'],
                'market_data': market_data,
                'opening_time': await self.market_monitor.get_opening_time(strategy['symbol']),
                'usdt_balance': await self.order_executor._get_balance()
            })
            
        except Exception as e:
            self.logger.error(f"显示策略失败: {str(e)}")
            self.printer.print_error("显示策略出现错误")

    async def _start_snipe(self):
        """开始抢购"""
        try:
            # 获取当前策略
            strategy = await self.config.get_strategy()
            if not strategy:
                self.printer.print_error("请先设置抢购策略!")
                return
                
            # 显示当前策略
            await self._show_current_strategy()
            
            # 确认执行
            confirm = input("\n确认开始执行抢购策略? (yes/no): ").strip().lower()
            if confirm != 'yes':
                print("\n已取消执行")
                return
                
            # 开始执行
            result = await self.execute_snipe(
                symbol=strategy['symbol'],
                amount=strategy['amount'],
                price=strategy['max_price']
            )
            
            if result:
                self.printer.print_execution_result(result)
            else:
                self.printer.print_error("抢购执行失败!")
                
        except Exception as e:
            self.logger.error(f"抢购执行失败: {str(e)}")
            self.printer.print_error("执行过程出现错误")

    async def _run_tests(self):
        """运行测试"""
        try:
            print("\n=== 测试中心 ===")
            print("1. 网络测试")
            print("2. API测试")
            print("3. 订单测试")
            print("4. 性能测试")
            print("5. 全面测试")
            print("0. 返回主菜单")
            
            choice = input("\n请选择测试类型 (0-5): ").strip()
            
            if choice == '1':
                result = await self.test_center.test_network()
                self.printer.print_test_result('network', result)
            elif choice == '2':
                result = await self.test_center.test_api()
                self.printer.print_test_result('api', result)
            elif choice == '3':
                result = await self.test_center.test_orders()
                self.printer.print_test_result('order', result)
            elif choice == '4':
                result = await self.test_center.test_performance()
                self.printer.print_test_result('performance', result)
            elif choice == '5':
                await self.test_center.run_all_tests()
            elif choice == '0':
                return
            else:
                print("\n无效的选择")
                
        except Exception as e:
            self.logger.error(f"运行测试失败: {str(e)}")
            self.printer.print_error("测试过程出现错误")

# =============================== 
# 模块：打印输出
# ===============================
class PrintManager:
    """打印管理器 - 处理所有输出打印"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.colors = {
            'red': '\033[91m',
            'green': '\033[92m',
            'yellow': '\033[93m',
            'blue': '\033[94m',
            'purple': '\033[95m',
            'cyan': '\033[96m',
            'white': '\033[97m',
            'end': '\033[0m'
        }

    def print_header(self):
        """打印程序头部"""
        header = f"""
{self.colors['cyan']}
====================================
    Binance Sniper v1.0
    币安现货抢币工具
====================================
{self.colors['end']}
"""
        print(header)

    def print_market_status(self, status: Dict):
        """打印市场状态"""
        output = f"""
{self.colors['yellow']}市场状态:{self.colors['end']}
- 当前价格: {status['current_price']}
- 24h涨跌: {status['price_24h_change']}%
- 24h成交: {status['volume_24h']}
- 市场状态: {status['market_status']}
- 网络延迟: {status['network_latency']}ms
"""
        print(output)

    def print_execution_result(self, order: Dict):
        """打印执行结果"""
        if not order:
            print(f"\n{self.colors['red']}订单执行失败!{self.colors['end']}")
            return

        output = f"""
{self.colors['green']}订单执行成功:{self.colors['end']}
- 订单ID: {order['id']}
- 成交价: {order['price']}
- 成交量: {order['filled']}
- 状态: {order['status']}
- 手续费: {order.get('fee', '未知')}
"""
        print(output)

    def print_system_status(self, status: Dict):
        """打印系统状态"""
        output = f"""
{self.colors['blue']}系统状态:{self.colors['end']}
- 运行时间: {status['uptime']:.1f}秒
- 市场状态: {status['market_status']['status']}
- 缓存命中率: {status['cache_stats'].get('hit_rate', 0):.2%}
- 请求成功率: {status['request_stats'].get('success_rate', 0):.2%}
- 平均延迟: {status['request_stats'].get('avg_latency', 0):.2f}ms
"""
        print(output)

    def print_error(self, error: str):
        """打印错误信息"""
        print(f"\n{self.colors['red']}错误: {error}{self.colors['end']}")

    def print_warning(self, warning: str):
        """打印警告信息"""
        print(f"\n{self.colors['yellow']}警告: {warning}{self.colors['end']}")

    def print_success(self, message: str):
        """打印成功信息"""
        print(f"\n{self.colors['green']}{message}{self.colors['end']}")

    def print_progress(self, message: str):
        """打印进度信息"""
        print(f"{self.colors['cyan']}{message}{self.colors['end']}")

    def print_debug(self, message: str):
        """打印调试信息"""
        if logging.getLogger().getEffectiveLevel() <= logging.DEBUG:
            print(f"{self.colors['purple']}[DEBUG] {message}{self.colors['end']}")

    def print_strategy_status(self, strategy_data: Dict):
        """打印当前策略配置"""
        try:
            market_data = strategy_data['market_data']
            now = datetime.now(pytz.UTC)
            opening_time = strategy_data.get('opening_time')
            time_diff = opening_time - now if opening_time else timedelta(0)
            hours_remaining = time_diff.total_seconds() / 3600

            # 计算实际的提前时间
            advance_time = (
                market_data['network_latency'] +  # 网络延迟
                abs(market_data['time_offset']) +  # 时间偏移
                5  # 安全冗余
            )

            # 买入数量显示逻辑
            current_price = market_data['current_price']
            estimated_amount = (
                f"{strategy_data['amount']/current_price:.8f} BTC (估算)" 
                if current_price > 0 
                else "待定 (以实际开盘价为准)"
            )

            output = f"""
{self.colors['cyan']}====== 当前抢购策略详情 ======{self.colors['end']}

📌 基础参数
- 交易对: {strategy_data['symbol'] or '未设置'}
- 买入金额: {strategy_data['amount'] or 0:,.2f} USDT
- 买入数量: {estimated_amount}
- 说明: 实际买入数量将根据开盘价动态计算

📊 市场信息
- 币种状态: {market_data['market_status']}
- 当前价格: {"未上市" if market_data['current_price'] == 0 else f"{market_data['current_price']:,.2f} USDT"}
- 24h涨跌: {"未上市" if market_data['current_price'] == 0 else f"{market_data['price_24h_change']:.2f}%"}
- 24h成交: {"未上市" if market_data['current_price'] == 0 else f"{market_data['volume_24h']:,.2f} USDT"}

⏰ 时间信息
- 开盘时间: {opening_time.strftime('%Y-%m-%d %H:%M:%S') if opening_time else '未设置'}
- 当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}
- 剩余时间: {int(hours_remaining)}小时{int((hours_remaining % 1) * 60)}分钟

💰 价格保护
- 最高限价: {strategy_data['max_price_limit'] or 0:,.2f} USDT
- 价格倍数: {strategy_data['price_multiplier'] or 0}倍

⚡ 执行策略
- 并发订单: {strategy_data['concurrent_orders'] or 0}个
- 提前时间: {advance_time:.2f}ms
- 网络延迟: {market_data['network_latency']:.2f}ms
- 时间偏移: {market_data['time_offset']:.2f}ms
- 安全冗余: 5ms

📈 止盈止损
- 止损线: -{strategy_data['stop_loss']*100:.1f}%
阶梯止盈:"""
            print(output)

            # 打印止盈策略
            if 'sell_strategy' in strategy_data:
                for idx, (profit, amount) in enumerate(strategy_data['sell_strategy'], 1):
                    print(f"- 第{idx}档: 涨幅{profit*100:.0f}% 卖出{amount*100:.0f}%")

            # 账户状态
            balance_info = f"""
💰 账户状态
- 可用USDT: {strategy_data['usdt_balance']:,.2f}
- 所需USDT: {strategy_data['amount'] or 0:,.2f}
- 状态: {'✅ 余额充足' if strategy_data['usdt_balance'] >= (strategy_data['amount'] or 0) else '❌ 余额不足'}

⚠️ 风险提示
1. 已启用最高价格保护: {strategy_data['max_price_limit'] or 0:,.2f} USDT
2. 已启用价格倍数限制: {strategy_data['price_multiplier'] or 0}倍
3. 使用IOC限价单模式
{self.colors['cyan']}============================={self.colors['end']}"""
            print(balance_info)
            
            return True
            
        except Exception as e:
            self.logger.error(f"打印策略失败: {str(e)}")
            return False

    def print_order_book(self, order_book: Dict, symbol: str, depth: int = 5):
        """打印订单簿"""
        try:
            header = f"\n{self.colors['cyan']}====== {symbol} 订单簿 ======{self.colors['end']}"
            print(header)
            
            print(f"\n{self.colors['red']}卖单:{self.colors['end']}")
            for price, amount in reversed(order_book['asks'][:depth]):
                print(f"  {price:10.8f} | {amount:10.8f}")
                
            print(f"\n{self.colors['green']}买单:{self.colors['end']}")
            for price, amount in order_book['bids'][:depth]:
                print(f"  {price:10.8f} | {amount:10.8f}")
                
            print(f"\n{self.colors['cyan']}=================={self.colors['end']}")

        except Exception as e:
            self.logger.error(f"打印订单簿失败: {str(e)}")

    def print_execution_progress(self, current: int, total: int, status: str):
        """打印执行进度"""
        try:
            progress = current / total * 100
            output = f"""
{self.colors['cyan']}执行进度: [{current}/{total}] {progress:.1f}%
状态: {status}
时间: {datetime.now().strftime('%H:%M:%S.%f')[:-3]}{self.colors['end']}
"""
            print(output)
        except Exception as e:
            self.logger.error(f"打印进度失败: {str(e)}")

    def print_depth_anomaly(self, depth_data: Dict, is_sell_anomaly: bool):
        """打印深度异常"""
        try:
            anomaly_type = "卖单" if is_sell_anomaly else "买单"
            output = f"""
{self.colors['red'] if is_sell_anomaly else self.colors['green']}
⚠️ 检测到异常{anomaly_type}!
当前价格: {depth_data['current_price']:.2f}

====== {depth_data['symbol']} 深度异常 ======{self.colors['end']}

{self.colors['red']}卖单:{self.colors['end']}"""
            print(output)

            # 打印卖单
            for price, amount in reversed(depth_data['asks']):
                mark = "  ⚠️ 大卖单" if amount >= depth_data['anomaly_threshold'] else ""
                print(f"  {price:10.8f} | {amount:10.8f}{mark}")

            output = f"""
{self.colors['green']}买单:{self.colors['end']}"""
            print(output)

            # 打印买单
            for price, amount in depth_data['bids']:
                mark = "  ⚠️ 大买单" if amount >= depth_data['anomaly_threshold'] else ""
                print(f"  {price:10.8f} | {amount:10.8f}{mark}")

            # 打印深度分析
            output = f"""
📊 深度分析:
- 买单深度: {depth_data['buy_depth']:.8f} BTC
- 卖单深度: {depth_data['sell_depth']:.8f} BTC
- 深度比例: {depth_data['depth_ratio']:.2f}
- 阈值: {depth_data['threshold']:.1f}

{'🚨 执行紧急卖出:' if is_sell_anomaly else '🚨 执行快速卖出:'}
- 数量: {depth_data['trade_amount']:.8f} BTC
- 目标价格: {depth_data['trade_price']:.2f} USDT
- 订单类型: IOC
"""
            print(output)

        except Exception as e:
            self.logger.error(f"打印深度异常失败: {str(e)}")

    def print_anomaly_result(self, result: Dict):
        """打印异常交易结果"""
        try:
            if not result['success']:
                print(f"\n{self.colors['red']}交易执行失败!{self.colors['end']}")
                return

            output = f"""
✅ {'紧急' if result['is_sell_anomaly'] else '快速'}卖出成功:
订单ID: {result['order_id']}
成交价格: {result['price']:.2f}
成交数量: {result['amount']:.8f}
成交金额: {result['value']:.2f} USDT

💰 交易统计:
- 买入价格: {result['entry_price']:.2f} USDT
- 卖出价格: {result['price']:.2f} USDT
- 价差收益: {result['profit_amount']:.2f} USDT ({result['profit_percent']:.2f}%)
"""
            print(output)

        except Exception as e:
            self.logger.error(f"打印交易结果失败: {str(e)}")

# =============================== 
# 模块：数据池
# ===============================
class DataPool:
    """数据池 - 统一处理币安数据"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.lock = asyncio.Lock()
        
        # 市场数据缓存
        self.market_data = {
            'price': defaultdict(float),      # 价格数据
            'depth': defaultdict(dict),       # 深度数据
            'trades': defaultdict(list),      # 成交数据
            'klines': defaultdict(list),      # K线数据
            'tickers': defaultdict(dict),     # 24h行情
            'book_tickers': defaultdict(dict) # 最优挂单
        }
        
        # WebSocket数据缓存
        self.ws_data = {
            'trades': deque(maxlen=1000),     # 最近成交
            'depth': deque(maxlen=1000),      # 深度更新
            'klines': deque(maxlen=1000),     # K线更新
            'book_tickers': deque(maxlen=100) # 最优挂单更新
        }
        
        # 统计信息
        self.stats = {
            'updates': defaultdict(int),      # 更新次数
            'ws_messages': defaultdict(int),  # WS消息数
            'cache_hits': defaultdict(int),   # 缓存命中
            'cache_misses': defaultdict(int), # 缓存未命中
            'errors': defaultdict(int)        # 错误计数
        }

    async def update_price(self, symbol: str, price: float, source: str = 'rest'):
        """更新价格数据"""
        try:
            async with self.lock:
                self.market_data['price'][symbol] = price
                self.stats['updates']['price'] += 1
                if source == 'ws':
                    self.stats['ws_messages']['price'] += 1
                    
        except Exception as e:
            self.logger.error(f"更新价格失败: {str(e)}")
            self.stats['errors']['price_update'] += 1

    async def update_depth(self, symbol: str, depth_data: Dict, source: str = 'rest'):
        """更新深度数据"""
        try:
            async with self.lock:
                if source == 'ws':
                    # 增量更新
                    current_depth = self.market_data['depth'][symbol]
                    self._update_depth_incrementally(current_depth, depth_data)
                    self.ws_data['depth'].append(depth_data)
                    self.stats['ws_messages']['depth'] += 1
                else:
                    # 全量更新
                    self.market_data['depth'][symbol] = depth_data
                    
                self.stats['updates']['depth'] += 1
                
        except Exception as e:
            self.logger.error(f"更新深度失败: {str(e)}")
            self.stats['errors']['depth_update'] += 1

    def _update_depth_incrementally(self, current: Dict, update: Dict):
        """增量更新深度数据"""
        # 更新买单
        for price, amount in update.get('bids', []):
            if float(amount) > 0:
                current['bids'][price] = amount
            else:
                current['bids'].pop(price, None)
                
        # 更新卖单
        for price, amount in update.get('asks', []):
            if float(amount) > 0:
                current['asks'][price] = amount
            else:
                current['asks'].pop(price, None)

    async def update_trades(self, symbol: str, trade: Dict, source: str = 'ws'):
        """更新成交数据"""
        try:
            async with self.lock:
                self.market_data['trades'][symbol].append(trade)
                if source == 'ws':
                    self.ws_data['trades'].append(trade)
                    self.stats['ws_messages']['trades'] += 1
                self.stats['updates']['trades'] += 1
                
        except Exception as e:
            self.logger.error(f"更新成交失败: {str(e)}")
            self.stats['errors']['trade_update'] += 1

    async def get_latest_price(self, symbol: str) -> float:
        """获取最新价格"""
        try:
            async with self.lock:
                price = self.market_data['price'].get(symbol, 0)
                if price > 0:
                    self.stats['cache_hits']['price'] += 1
                else:
                    self.stats['cache_misses']['price'] += 1
                return price
                
        except Exception as e:
            self.logger.error(f"获取价格失败: {str(e)}")
            self.stats['errors']['price_get'] += 1
            return 0

    async def get_depth(self, symbol: str, limit: int = 20) -> Dict:
        """获取深度数据"""
        try:
            async with self.lock:
                depth = self.market_data['depth'].get(symbol, {})
                if depth:
                    self.stats['cache_hits']['depth'] += 1
                else:
                    self.stats['cache_misses']['depth'] += 1
                    
                return {
                    'bids': sorted(
                        depth.get('bids', {}).items(),
                        key=lambda x: float(x[0]),
                        reverse=True
                    )[:limit],
                    'asks': sorted(
                        depth.get('asks', {}).items(),
                        key=lambda x: float(x[0])
                    )[:limit]
                }
                
        except Exception as e:
            self.logger.error(f"获取深度失败: {str(e)}")
            self.stats['errors']['depth_get'] += 1
            return {'bids': [], 'asks': []}

    async def get_recent_trades(self, symbol: str, limit: int = 100) -> List[Dict]:
        """获取最近成交"""
        try:
            async with self.lock:
                trades = self.market_data['trades'].get(symbol, [])
                if trades:
                    self.stats['cache_hits']['trades'] += 1
                else:
                    self.stats['cache_misses']['trades'] += 1
                return trades[-limit:]
                
        except Exception as e:
            self.logger.error(f"获取成交失败: {str(e)}")
            self.stats['errors']['trades_get'] += 1
            return []

    def get_stats(self) -> Dict:
        """获取统计信息"""
        return {
            'updates': dict(self.stats['updates']),
            'ws_messages': dict(self.stats['ws_messages']),
            'cache': {
                'hits': dict(self.stats['cache_hits']),
                'misses': dict(self.stats['cache_misses'])
            },
            'errors': dict(self.stats['errors'])
        }

    async def clear_cache(self):
        """清理缓存数据"""
        try:
            async with self.lock:
                # 清理市场数据
                for key in self.market_data:
                    if isinstance(self.market_data[key], defaultdict):
                        self.market_data[key].clear()
                    elif isinstance(self.market_data[key], dict):
                        self.market_data[key] = {}
                        
                # 清理WS数据
                for key in self.ws_data:
                    self.ws_data[key].clear()
                    
                # 重置统计
                for key in self.stats:
                    if isinstance(self.stats[key], defaultdict):
                        self.stats[key].clear()
                        
            self.logger.info("缓存已清理")
            
        except Exception as e:
            self.logger.error(f"清理缓存失败: {str(e)}")

# =============================== 
# 模块：测试中心
# ===============================
class TestCenter:
    """测试中心 - 用于测试各项功能"""
    def __init__(self, sniper: 'BinanceSniper'):
        self.logger = logging.getLogger(__name__)
        self.sniper = sniper
        self.printer = PrintManager()
        
        # 测试配置
        self.test_config = {
            'network': {
                'timeout': 5,
                'retry_count': 3,
                'endpoints': [
                    '/api/v3/ping',
                    '/api/v3/time',
                    '/api/v3/exchangeInfo'
                ]
            },
            'api': {
                'symbols': ['BTCUSDT', 'ETHUSDT'],
                'order_amount': 0.001,
                'test_mode': True
            },
            'performance': {
                'concurrent_requests': 10,
                'request_interval': 0.1,
                'test_duration': 60
            }
        }
        
        # 测试结果
        self.test_results = {
            'network': defaultdict(list),
            'api': defaultdict(list),
            'order': defaultdict(list),
            'performance': defaultdict(list)
        }

    async def run_all_tests(self):
        """运行所有测试"""
        try:
            self.printer.print_progress("开始全面测试...")
            
            # 网络测试
            network_result = await self.test_network()
            self.printer.print_test_result('network', network_result)
            
            # API测试
            api_result = await self.test_api()
            self.printer.print_test_result('api', api_result)
            
            # 订单测试
            order_result = await self.test_orders()
            self.printer.print_test_result('order', order_result)
            
            # 性能测试
            perf_result = await self.test_performance()
            self.printer.print_test_result('performance', perf_result)
            
            return True
            
        except Exception as e:
            self.logger.error(f"测试执行失败: {str(e)}")
            return False

    async def test_network(self) -> Dict:
        """测试网络连接"""
        results = {
            'latency': [],
            'success_rate': 0,
            'errors': defaultdict(int)
        }
        
        try:
            for endpoint in self.test_config['network']['endpoints']:
                for _ in range(self.test_config['network']['retry_count']):
                    try:
                        start_time = time.perf_counter()
                        async with aiohttp.ClientSession() as session:
                            async with session.get(
                                f"https://api.binance.com{endpoint}",
                                timeout=self.test_config['network']['timeout']
                            ) as response:
                                await response.json()
                                latency = (time.perf_counter() - start_time) * 1000
                                results['latency'].append(latency)
                                
                    except Exception as e:
                        results['errors'][str(e)] += 1
                        
            total_requests = len(self.test_config['network']['endpoints']) * self.test_config['network']['retry_count']
            success_count = len(results['latency'])
            results['success_rate'] = success_count / total_requests if total_requests > 0 else 0
            
            if results['latency']:
                results['avg_latency'] = sum(results['latency']) / len(results['latency'])
                results['min_latency'] = min(results['latency'])
                results['max_latency'] = max(results['latency'])
                
            return results
            
        except Exception as e:
            self.logger.error(f"网络测试失败: {str(e)}")
            return results

    async def test_api(self) -> Dict:
        """测试API功能"""
        results = {
            'endpoints': defaultdict(dict),
            'errors': defaultdict(int)
        }
        
        try:
            # 测试市场数据API
            for symbol in self.test_config['api']['symbols']:
                # 测试价格接口
                try:
                    price = await self.sniper.data_cache.get_latest_price(symbol)
                    results['endpoints']['price'][symbol] = price
                except Exception as e:
                    results['errors']['price'] += 1
                    
                # 测试深度接口
                try:
                    depth = await self.sniper.data_cache.get_depth(symbol)
                    results['endpoints']['depth'][symbol] = len(depth['bids']) + len(depth['asks'])
                except Exception as e:
                    results['errors']['depth'] += 1
                    
            return results
            
        except Exception as e:
            self.logger.error(f"API测试失败: {str(e)}")
            return results

    async def test_orders(self) -> Dict:
        """测试订单功能"""
        results = {
            'test_orders': [],
            'errors': defaultdict(int)
        }
        
        try:
            if not self.test_config['api']['test_mode']:
                self.logger.warning("订单测试需要在测试模式下进行")
                return results
                
            # 测试下单
            for symbol in self.test_config['api']['symbols']:
                try:
                    test_order = await self.sniper.order_executor._place_single_order(
                        symbol=symbol,
                        amount=self.test_config['api']['order_amount'],
                        price=await self.sniper.data_cache.get_latest_price(symbol),
                        delay=0
                    )
                    if test_order:
                        results['test_orders'].append(test_order)
                except Exception as e:
                    results['errors']['order'] += 1
                    
            return results
            
        except Exception as e:
            self.logger.error(f"订单测试失败: {str(e)}")
            return results

    async def test_performance(self) -> Dict:
        """测试性能"""
        results = {
            'requests_per_second': 0,
            'avg_latency': 0,
            'error_rate': 0,
            'memory_usage': 0
        }
        
        try:
            start_time = time.time()
            request_count = 0
            error_count = 0
            latencies = []
            
            # 创建并发请求
            async def make_request():
                nonlocal request_count, error_count
                try:
                    start = time.perf_counter()
                    await self.sniper.data_cache.get_latest_price('BTCUSDT')
                    latency = (time.perf_counter() - start) * 1000
                    latencies.append(latency)
                    request_count += 1
                except:
                    error_count += 1
                    
            # 执行并发测试
            tasks = []
            while time.time() - start_time < self.test_config['performance']['test_duration']:
                tasks.extend([
                    make_request() 
                    for _ in range(self.test_config['performance']['concurrent_requests'])
                ])
                await asyncio.sleep(self.test_config['performance']['request_interval'])
                
            await asyncio.gather(*tasks)
            
            # 计算结果
            test_duration = time.time() - start_time
            results['requests_per_second'] = request_count / test_duration
            results['avg_latency'] = sum(latencies) / len(latencies) if latencies else 0
            results['error_rate'] = error_count / (request_count + error_count) if (request_count + error_count) > 0 else 0
            results['memory_usage'] = psutil.Process().memory_info().rss / 1024 / 1024  # MB
            
            return results
            
        except Exception as e:
            self.logger.error(f"性能测试失败: {str(e)}")
            return results

    def get_test_results(self) -> Dict:
        """获取测试结果"""
        return {
            'network': dict(self.test_results['network']),
            'api': dict(self.test_results['api']),
            'order': dict(self.test_results['order']),
            'performance': dict(self.test_results['performance'])
        }

# =============================== 
# 模块：WebSocket管理
# ===============================
class WebSocketManager:
    """WebSocket管理器 - 处理实时数据连接"""
    def __init__(self, data_pool: DataPool):
        self.logger = logging.getLogger(__name__)
        self.data_pool = data_pool
        
        # WebSocket配置
        self.ws_config = {
            'base_url': 'wss://stream.binance.com:9443/ws',
            'keepalive': 30,           # 心跳间隔(秒)
            'reconnect_delay': 5,      # 重连延迟(秒)
            'max_reconnects': 5,       # 最大重连次数
            'ping_timeout': 10,        # ping超时(秒)
            'close_timeout': 5         # 关闭超时(秒)
        }
        
        # 连接状态
        self.connections = {
            'market': None,    # 市场数据连接
            'trade': None,     # 交易数据连接
            'depth': None      # 深度数据连接
        }
        
        # 订阅状态
        self.subscriptions = {
            'market': set(),   # 市场数据订阅
            'trade': set(),    # 交易数据订阅
            'depth': set()     # 深度数据订阅
        }
        
        # 统计信息
        self.stats = {
            'messages': defaultdict(int),      # 消息计数
            'reconnects': defaultdict(int),    # 重连计数
            'errors': defaultdict(int),        # 错误计数
            'latency': defaultdict(list)       # 延迟统计
        }
        
        self.is_running = False
        self.tasks = []

    async def start(self):
        """启动WebSocket连接"""
        if self.is_running:
            return
            
        self.is_running = True
        
        # 创建连接任务
        self.tasks = [
            asyncio.create_task(self._maintain_connection('market')),
            asyncio.create_task(self._maintain_connection('trade')),
            asyncio.create_task(self._maintain_connection('depth'))
        ]
        
        self.logger.info("WebSocket管理器已启动")

    async def stop(self):
        """停止WebSocket连接"""
        if not self.is_running:
            return
            
        self.is_running = False
        
        # 取消所有任务
        for task in self.tasks:
            task.cancel()
            
        # 关闭所有连接
        for conn in self.connections.values():
            if conn:
                await conn.close()
                
        self.logger.info("WebSocket管理器已停止")

    async def _maintain_connection(self, conn_type: str):
        """维护WebSocket连接"""
        reconnect_count = 0
        
        while self.is_running:
            try:
                if not self.connections[conn_type]:
                    # 创建新连接
                    self.connections[conn_type] = await self._create_connection(conn_type)
                    reconnect_count = 0
                    
                # 处理消息
                async for message in self.connections[conn_type]:
                    await self._handle_message(conn_type, message)
                    
            except Exception as e:
                self.logger.error(f"WebSocket连接异常 [{conn_type}]: {str(e)}")
                self.stats['errors'][conn_type] += 1
                
                # 处理重连
                if reconnect_count < self.ws_config['max_reconnects']:
                    reconnect_count += 1
                    self.stats['reconnects'][conn_type] += 1
                    await asyncio.sleep(self.ws_config['reconnect_delay'])
                    continue
                else:
                    self.logger.error(f"WebSocket重连失败 [{conn_type}], 达到最大重试次数")
                    break

    async def _create_connection(self, conn_type: str):
        """创建WebSocket连接"""
        try:
            # 构建订阅消息
            subscribe_msg = {
                'method': 'SUBSCRIBE',
                'params': list(self.subscriptions[conn_type]),
                'id': int(time.time() * 1000)
            }
            
            # 创建连接
            session = aiohttp.ClientSession()
            ws = await session.ws_connect(
                self.ws_config['base_url'],
                heartbeat=self.ws_config['keepalive'],
                timeout=self.ws_config['ping_timeout']
            )
            
            # 发送订阅消息
            await ws.send_json(subscribe_msg)
            
            return ws
            
        except Exception as e:
            self.logger.error(f"创建WebSocket连接失败 [{conn_type}]: {str(e)}")
            raise

    async def _handle_message(self, conn_type: str, message: aiohttp.WSMessage):
        """处理WebSocket消息"""
        try:
            if message.type == aiohttp.WSMsgType.TEXT:
                data = json.loads(message.data)
                
                # 更新统计
                self.stats['messages'][conn_type] += 1
                
                # 处理不同类型的消息
                if conn_type == 'market':
                    await self._handle_market_message(data)
                elif conn_type == 'trade':
                    await self._handle_trade_message(data)
                elif conn_type == 'depth':
                    await self._handle_depth_message(data)
                    
            elif message.type == aiohttp.WSMsgType.CLOSED:
                self.logger.warning(f"WebSocket连接关闭 [{conn_type}]")
                self.connections[conn_type] = None
                
            elif message.type == aiohttp.WSMsgType.ERROR:
                self.logger.error(f"WebSocket连接错误 [{conn_type}]")
                self.stats['errors'][conn_type] += 1
                
        except Exception as e:
            self.logger.error(f"处理WebSocket消息失败: {str(e)}")
            self.stats['errors']['message_handling'] += 1

    async def _handle_market_message(self, data: Dict):
        """处理市场数据消息"""
        try:
            if 'e' not in data:  # 不是事件消息
                return
                
            event_type = data['e']
            symbol = data['s']
            
            if event_type == '24hrTicker':
                await self.data_pool.update_price(
                    symbol=symbol,
                    price=float(data['c']),
                    source='ws'
                )
                
        except Exception as e:
            self.logger.error(f"处理市场数据消息失败: {str(e)}")

    async def _handle_trade_message(self, data: Dict):
        """处理交易数据消息"""
        try:
            if 'e' not in data:
                return
                
            event_type = data['e']
            symbol = data['s']
            
            if event_type == 'trade':
                await self.data_pool.update_trades(
                    symbol=symbol,
                    trade=data,
                    source='ws'
                )
                
        except Exception as e:
            self.logger.error(f"处理交易数据消息失败: {str(e)}")

    async def _handle_depth_message(self, data: Dict):
        """处理深度数据消息"""
        try:
            if 'e' not in data:
                return
                
            event_type = data['e']
            symbol = data['s']
            
            if event_type == 'depthUpdate':
                await self.data_pool.update_depth(
                    symbol=symbol,
                    depth_data={
                        'bids': data['b'],
                        'asks': data['a']
                    },
                    source='ws'
                )
                
        except Exception as e:
            self.logger.error(f"处理深度数据消息失败: {str(e)}")

    def get_stats(self) -> Dict:
        """获取WebSocket统计信息"""
        return {
            'messages': dict(self.stats['messages']),
            'reconnects': dict(self.stats['reconnects']),
            'errors': dict(self.stats['errors']),
            'connections': {
                k: 'connected' if v else 'disconnected'
                for k, v in self.connections.items()
            }
        }

# =============================== 
# 模块：性能监控
# ===============================
class PerformanceMonitor:
    """性能监控器 - 监控系统资源和性能"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        
        # 监控配置
        self.monitor_config = {
            'check_interval': 1.0,        # 检查间隔(秒)
            'history_size': 3600,         # 历史数据保留时长(秒)
            'cpu_threshold': 80,          # CPU告警阈值(%)
            'memory_threshold': 85,       # 内存告警阈值(%)
            'latency_threshold': 1000,    # 延迟告警阈值(ms)
            'error_rate_threshold': 0.05  # 错误率告警阈值(5%)
        }
        
        # 性能数据
        self.performance_data = {
            'cpu_usage': deque(maxlen=3600),      # CPU使用率历史
            'memory_usage': deque(maxlen=3600),   # 内存使用率历史
            'network_latency': deque(maxlen=3600), # 网络延迟历史
            'request_stats': deque(maxlen=3600),   # 请求统计历史
            'error_stats': deque(maxlen=3600)      # 错误统计历史
        }
        
        # 告警状态
        self.alert_status = {
            'cpu_alert': False,
            'memory_alert': False,
            'latency_alert': False,
            'error_alert': False,
            'last_alert_time': defaultdict(float)
        }
        
        # 统计数据
        self.stats = {
            'total_alerts': defaultdict(int),
            'peak_values': {
                'cpu': 0,
                'memory': 0,
                'latency': 0,
                'error_rate': 0
            }
        }
        
        self.is_running = False
        self.monitor_task = None

    async def start(self):
        """启动性能监控"""
        if self.is_running:
            return
            
        self.is_running = True
        self.monitor_task = asyncio.create_task(self._monitor_loop())
        self.logger.info("性能监控已启动")

    async def stop(self):
        """停止性能监控"""
        if not self.is_running:
            return
            
        self.is_running = False
        if self.monitor_task:
            self.monitor_task.cancel()
            try:
                await self.monitor_task
            except asyncio.CancelledError:
                pass
        self.logger.info("性能监控已停止")

    async def _monitor_loop(self):
        """监控循环"""
        while self.is_running:
            try:
                # 收集性能数据
                await self._collect_performance_data()
                
                # 检查告警条件
                await self._check_alerts()
                
                # 更新统计信息
                self._update_stats()
                
                await asyncio.sleep(self.monitor_config['check_interval'])
                
            except Exception as e:
                self.logger.error(f"性能监控异常: {str(e)}")
                await asyncio.sleep(5)

    async def _collect_performance_data(self):
        """收集性能数据"""
        try:
            # CPU使用率
            cpu_percent = psutil.cpu_percent(interval=None)
            self.performance_data['cpu_usage'].append({
                'timestamp': time.time(),
                'value': cpu_percent
            })
            
            # 内存使用率
            memory = psutil.Process().memory_info()
            memory_percent = memory.rss / psutil.virtual_memory().total * 100
            self.performance_data['memory_usage'].append({
                'timestamp': time.time(),
                'value': memory_percent
            })
            
            # 网络延迟
            start_time = time.perf_counter()
            async with aiohttp.ClientSession() as session:
                async with session.get('https://api.binance.com/api/v3/time') as response:
                    await response.json()
                    latency = (time.perf_counter() - start_time) * 1000
                    self.performance_data['network_latency'].append({
                        'timestamp': time.time(),
                        'value': latency
                    })
                    
        except Exception as e:
            self.logger.error(f"收集性能数据失败: {str(e)}")

    async def _check_alerts(self):
        """检查告警条件"""
        try:
            current_time = time.time()
            
            # CPU告警
            if self.performance_data['cpu_usage']:
                cpu_usage = self.performance_data['cpu_usage'][-1]['value']
                if cpu_usage > self.monitor_config['cpu_threshold']:
                    await self._handle_alert('cpu', cpu_usage, current_time)
                    
            # 内存告警
            if self.performance_data['memory_usage']:
                memory_usage = self.performance_data['memory_usage'][-1]['value']
                if memory_usage > self.monitor_config['memory_threshold']:
                    await self._handle_alert('memory', memory_usage, current_time)
                    
            # 延迟告警
            if self.performance_data['network_latency']:
                latency = self.performance_data['network_latency'][-1]['value']
                if latency > self.monitor_config['latency_threshold']:
                    await self._handle_alert('latency', latency, current_time)
                    
            # 错误率告警
            if self.performance_data['error_stats']:
                error_rate = self.performance_data['error_stats'][-1]['value']
                if error_rate > self.monitor_config['error_rate_threshold']:
                    await self._handle_alert('error_rate', error_rate, current_time)
                    
        except Exception as e:
            self.logger.error(f"检查告警失败: {str(e)}")

    async def _handle_alert(self, alert_type: str, value: float, current_time: float):
        """处理告警"""
        try:
            # 控制告警频率(每类告警最少间隔60秒)
            if current_time - self.alert_status['last_alert_time'][alert_type] < 60:
                return
                
            self.alert_status['last_alert_time'][alert_type] = current_time
            self.stats['total_alerts'][alert_type] += 1
            
            # 更新峰值
            if value > self.stats['peak_values'][alert_type]:
                self.stats['peak_values'][alert_type] = value
                
            # 记录告警日志
            self.logger.warning(f"""
性能告警:
- 类型: {alert_type}
- 当前值: {value:.2f}
- 阈值: {self.monitor_config[f'{alert_type}_threshold']}
- 告警次数: {self.stats['total_alerts'][alert_type]}
""")
            
        except Exception as e:
            self.logger.error(f"处理告警失败: {str(e)}")

    def _update_stats(self):
        """更新统计信息"""
        try:
            # 计算各项指标的平均值
            for metric in ['cpu_usage', 'memory_usage', 'network_latency']:
                if self.performance_data[metric]:
                    values = [d['value'] for d in self.performance_data[metric]]
                    self.stats[f'avg_{metric}'] = sum(values) / len(values)
                    
            # 更新错误率统计
            if self.performance_data['error_stats']:
                error_rates = [d['value'] for d in self.performance_data['error_stats']]
                self.stats['avg_error_rate'] = sum(error_rates) / len(error_rates)
                
        except Exception as e:
            self.logger.error(f"更新统计失败: {str(e)}")

    def get_performance_stats(self) -> Dict:
        """获取性能统计"""
        return {
            'current': {
                'cpu': self.performance_data['cpu_usage'][-1]['value'] if self.performance_data['cpu_usage'] else 0,
                'memory': self.performance_data['memory_usage'][-1]['value'] if self.performance_data['memory_usage'] else 0,
                'latency': self.performance_data['network_latency'][-1]['value'] if self.performance_data['network_latency'] else 0
            },
            'averages': {
                'cpu': self.stats.get('avg_cpu_usage', 0),
                'memory': self.stats.get('avg_memory_usage', 0),
                'latency': self.stats.get('avg_network_latency', 0),
                'error_rate': self.stats.get('avg_error_rate', 0)
            },
            'peaks': self.stats['peak_values'],
            'alerts': dict(self.stats['total_alerts'])
        }

    def get_historical_data(self, metric: str, duration: int = 3600) -> List[Dict]:
        """获取历史数据"""
        try:
            if metric not in self.performance_data:
                return []
                
            current_time = time.time()
            return [
                data for data in self.performance_data[metric]
                if current_time - data['timestamp'] <= duration
            ]
            
        except Exception as e:
            self.logger.error(f"获取历史数据失败: {str(e)}")
            return []

if __name__ == '__main__':
    try:
        # 创建事件循环
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # 创建并初始化Sniper
        sniper = BinanceSniper('config.json')
        loop.run_until_complete(sniper.initialize())
        
        # 运行菜单
        loop.run_until_complete(sniper.show_menu())
        
    except Exception as e:
        print(f"程序异常退出: {str(e)}")
    finally:
        # 清理事件循环
        if loop.is_running():
            loop.close()

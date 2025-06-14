import platform
import psutil
import distro
from typing import Dict, List, Generator, Union


class SystemProbe:
    """系统探测工具，支持服务、系统、磁盘、网络、硬件资源等信息采集"""

    @property
    def system_info(self) -> Dict[str, str]:
        """获取基础系统信息"""
        info = {
            'os': platform.system(),
            'release': platform.release(),
            'kernel': platform.version(),
            'machine': platform.machine(),
            'arch': platform.architecture()[0],
            'hostname': platform.node()
        }

        if info['os'] == 'Linux':
            info.update({
                'distro': distro.name(pretty=True),
                'distro_version': distro.version(),
                'libc': ' '.join(platform.libc_ver())
            })

        return info

    @staticmethod
    def disk_usage() -> Generator[Dict[str, Union[str, float]], None, None]:
        """获取每个挂载点的磁盘使用情况"""
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                yield {
                    'device': part.device,
                    'mount': part.mountpoint,
                    'fstype': part.fstype,
                    'total_gb': round(usage.total / (1024 ** 3), 2),
                    'used_gb': round(usage.used / (1024 ** 3), 2),
                    'free_gb': round(usage.free / (1024 ** 3), 2),
                    'usage_percent': usage.percent
                }
            except PermissionError:
                continue

    @staticmethod
    def network_interfaces() -> Generator[Dict[str, Union[str, Dict[str, str], Dict[str, float]]], None, None]:
        """采集网络接口和流量信息"""
        net_stats = psutil.net_io_counters(pernic=True)

        # 安全可靠的默认值处理
        default_stats = {
            'bytes_sent': 0,
            'bytes_recv': 0,
            'packets_sent': 0,
            'packets_recv': 0,
            'errin': 0,
            'errout': 0,
            'dropin': 0,
            'dropout': 0
        }

        for name, addrs in psutil.net_if_addrs().items():
            stats = net_stats.get(name)
            # 使用getattr安全访问属性，兼容不同psutil版本
            bytes_sent = getattr(stats, 'bytes_sent', default_stats['bytes_sent']) if stats else default_stats[
                'bytes_sent']
            bytes_recv = getattr(stats, 'bytes_recv', default_stats['bytes_recv']) if stats else default_stats[
                'bytes_recv']

            yield {
                'interface': name,
                'addresses': {
                    addr.family.name: addr.address for addr in addrs if addr.address
                },
                'traffic_mb': {
                    'sent': round(bytes_sent / 1024 ** 2, 2),
                    'recv': round(bytes_recv / 1024 ** 2, 2)
                }
            }

    @staticmethod
    def hardware_resources() -> Dict[str, Union[int, float]]:
        """获取 CPU、内存、交换区使用情况"""
        cpu_usage = psutil.cpu_percent(interval=0.2)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()

        return {
            'cpu_cores': psutil.cpu_count(logical=False),
            'cpu_threads': psutil.cpu_count(logical=True),
            'cpu_usage_percent': cpu_usage,
            'memory_total_gb': round(mem.total / (1024 ** 3), 2),
            'memory_available_gb': round(mem.available / (1024 ** 3), 2),
            'memory_usage_percent': mem.percent,
            'swap_total_gb': round(swap.total / (1024 ** 3), 2),
            'swap_used_gb': round(swap.used / (1024 ** 3), 2),
            'swap_free_gb': round(swap.free / (1024 ** 3), 2)
        }

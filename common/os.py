import platform
import psutil
import distro
import paramiko
import subprocess
import getpass
from .logger import setup_logger
from typing import Dict, Generator, Union, List, Optional, Tuple
from pathlib import Path

logger = setup_logger(__name__)


class SystemProbe:
    """handle system probe or execute command"""

    def __init__(self):
        self.executor = Executor()

    @property
    def system_info(self) -> Dict[str, str]:
        """get system info"""
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
        """get each mount point disk usage"""
        for part in psutil.disk_partitions(all=False):
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

    @staticmethod
    def network_interfaces() -> Generator[Dict[str, Union[str, Dict[str, str], Dict[str, float]]], None, None]:
        """get network interfaces info"""
        net_stats = psutil.net_io_counters(pernic=True)

        # default value format
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
            # use getattr to access attributes，compatible for old psutil version
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
        """get cpu mem swap info"""
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

    def ssh_keys_distribution(self,
                              host_ips: List[str],
                              username: str,
                              password: Optional[str] = None,
                              port: int = 22,
                              timeout: int = 5
                              ) -> dict[str, str] | None:
        """
        Distribute SSH keys to multiple hosts similar to ssh-copy-id.

        Args:
            host_ips: List of host IP addresses to copy keys to
            username: SSH username for all hosts
            password: Optional common password for all hosts (will prompt if None and needed)
            port: SSH port
            timeout: Connection timeout in seconds

        Returns:
            Dictionary with host IPs as keys and status messages as values
        """
        # Generate or load public key
        private_key_path = Path.home() / ".ssh" / "id_rsa"
        if not private_key_path.exists():
            logger.info("There is no SSH key pair, generating new pair...", extra={"to_stdout": True})
            self.executor.execute(f"ssh-keygen -t rsa -b 2048 -N '' -f {private_key_path}")

        public_key, _, _ = self.executor.execute(f"ssh-keygen -y -f {private_key_path}")
        public_key = public_key.strip()

        results = {}
        password = password

        for host_ip in host_ips:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            try:
                # Try key-based auth first
                try:
                    client.connect(host_ip, port=port, username=username, timeout=timeout)
                    logger.info(f"[Connected] The key to {username}@{host_ip} already exists!", extra={"to_stdout": True})
                    results[host_ip] = "[Connected] keys set up already"
                    continue
                except paramiko.AuthenticationException:
                    pass

                # Password auth flow
                while True:
                    if password is None:
                        password = getpass.getpass(f"Enter SSH password for {username}@{host_ip}: ")

                    try:
                        client.connect(host_ip, port=port, username=username, password=password, timeout=timeout)
                        break
                    except paramiko.AuthenticationException:
                        logger.info(f"The password of {username}@{host_ip} is not correct, please try again.",
                                    extra={"to_stdout": True})
                        password = None
                        continue

                # Deploy key
                commands = [
                    "mkdir -p ~/.ssh",
                    "chmod 700 ~/.ssh",
                    f"echo '{public_key}' >> ~/.ssh/authorized_keys",
                    "chmod 600 ~/.ssh/authorized_keys"
                ]

                for cmd in commands:
                    stdin, stdout, stderr = client.exec_command(cmd)
                    if stderr.read():
                        raise RuntimeError(f"Command failed: {cmd}")

                logger.info(f"[Deployed] The key to {username}@{host_ip} has been deployed successfully!")
                results[host_ip] = "[Deployed] keys set up successfully"

            except Exception as e:
                results[host_ip] = f"[Failed] The reason is {str(e)}"
            finally:
                client.close()
        logger.info(f"The results of distributing ssh key is {results}.", extra={"to_stdout": True})
        return results


class Executor:
    @staticmethod
    def execute(script: str, timeout: int = 15) -> Tuple[str, str, int]:
        """return (stdout, stderr, returncode)"""
        result = subprocess.run(
            script,
            shell=True,
            check=False,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8"
        )
        return result.stdout, result.stderr, result.returncode

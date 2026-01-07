import platform
import psutil
import distro
import paramiko
import socket
import subprocess
import getpass
from .logger import setup_logger
from typing import Dict, Generator, Union, List, Optional, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

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

    # ------------------------------------------------------------
    # Public entry
    # ------------------------------------------------------------
    def ssh_keys_distribution(
        self,
        host_ips: List[str],
        username: str,
        password: Optional[str] = None,
        port: int = 22,
        timeout: int = 5,
        ask_pass: bool = False,
        dry_run: bool = False,
        max_workers: int = 10,
    ) -> Dict[str, str]:
        """
        Distribute SSH keys to hosts (ssh-copy-id compatible)

        Features:
        - multi-key
        - concurrency
        - dry-run
        - sshd capability detection
        """

        public_keys = self._load_all_public_keys()
        if not public_keys:
            raise RuntimeError("No SSH public keys available")

        results: Dict[str, str] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(
                    self._handle_single_host,
                    host,
                    username,
                    password,
                    port,
                    timeout,
                    ask_pass,
                    dry_run,
                    public_keys,
                ): host
                for host in host_ips
            }

            for future in as_completed(futures):
                host = futures[future]
                try:
                    results[host] = future.result()
                except Exception as e:
                    results[host] = f"[FAILED] {e}"

        return results

    # ------------------------------------------------------------
    # Single host handler
    # ------------------------------------------------------------
    def _handle_single_host(
        self,
        host: str,
        username: str,
        password: Optional[str],
        port: int,
        timeout: int,
        ask_pass: bool,
        dry_run: bool,
        public_keys: Dict[str, str],
    ):

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        host_password = password

        try:
            # 1. try key auth
            try:
                client.connect(
                    hostname=host,
                    port=port,
                    username=username,
                    timeout=timeout,
                    allow_agent=True,
                    look_for_keys=True,
                )
                return "[SKIPPED] Key already works"
            except paramiko.AuthenticationException:
                pass

            # 2. password auth
            while True:
                if host_password is None:
                    if not ask_pass:
                        raise RuntimeError("Password required")
                    host_password = getpass.getpass(
                        f"Password for {username}@{host}: "
                    )
                try:
                    client.connect(
                        hostname=host,
                        port=port,
                        username=username,
                        password=host_password,
                        timeout=timeout,
                        look_for_keys=False,
                        allow_agent=False,
                    )
                    break
                except paramiko.AuthenticationException:
                    host_password = None

            # 3. detect remote sshd supported key types
            supported_types = self._detect_remote_key_types(client)

            # 4. select keys to deploy
            deploy_keys = {
                name: key
                for name, key in public_keys.items()
                if self._key_type(name) in supported_types
            }

            if not deploy_keys:
                return "[FAILED] No compatible SSH key type"

            if dry_run:
                return f"[CHECK] Would install keys: {', '.join(deploy_keys)}"

            # 5. deploy
            self._deploy_keys(client, deploy_keys)

            return "[SUCCESS] Keys installed"

        except (socket.timeout, socket.error) as e:
            return f"[FAILED] Network error: {e}"
        finally:
            client.close()

    # ------------------------------------------------------------
    # Key loading
    # ------------------------------------------------------------
    def _load_all_public_keys(self) -> Dict[str, str]:
        # ssh-copy-id key priority
        keys_priority = [
            "id_ed25519.pub",
            "id_ecdsa.pub",
            "id_rsa.pub",
        ]

        ssh_dir = Path.home() / ".ssh"
        keys: Dict[str, str] = {}

        for name in keys_priority:
            path = ssh_dir / name
            if path.exists():
                keys[name] = path.read_text().strip()

        if keys:
            return keys

        # generate default ed25519
        private_key = ssh_dir / "id_ed25519"
        logger.info(
            "No SSH keys found, generating ed25519 key...",
            extra={"to_stdout": True}
        )
        self.executor.execute(
            f"ssh-keygen -t ed25519 -N '' -f {private_key}"
        )
        pub = private_key.with_suffix(".pub")
        if not pub.exists():
            raise RuntimeError("SSH key generation failed")

        return {"id_ed25519.pub": pub.read_text().strip()}

    # ------------------------------------------------------------
    # Remote capability detection
    # ------------------------------------------------------------
    def _detect_remote_key_types(self, client: paramiko.SSHClient) -> List[str]:
        """
        Detect sshd supported PubkeyAcceptedAlgorithms
        """
        cmd = (
            "sshd -T 2>/dev/null | "
            "grep -i pubkeyacceptedalgorithms | awk '{print $2}'"
        )
        stdin, stdout, _ = client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()

        if exit_code != 0:
            # fallback: assume common defaults
            return ["ssh-ed25519", "rsa-sha2-256", "rsa-sha2-512"]

        algos = stdout.read().decode().strip().split(",")
        return algos

    # ------------------------------------------------------------
    # Deployment
    # ------------------------------------------------------------
    def _deploy_keys(self, client: paramiko.SSHClient, keys: Dict[str, str]) -> None:
        commands = [
            "install -d -m 700 ~/.ssh",
            "touch ~/.ssh/authorized_keys",
            "chmod 600 ~/.ssh/authorized_keys",
        ]

        for cmd in commands:
            self._exec(client, cmd)

        for key in keys.values():
            self._exec(
                client,
                (
                    f"grep -qxF '{key}' ~/.ssh/authorized_keys "
                    f"|| echo '{key}' >> ~/.ssh/authorized_keys"
                ),
            )

        self._exec(client, "restorecon -Rv ~/.ssh 2>/dev/null || true")

    # ------------------------------------------------------------
    # Utils
    # ------------------------------------------------------------
    def _exec(self, client: paramiko.SSHClient, cmd: str) -> None:
        stdin, stdout, stderr = client.exec_command(cmd)
        exit_code = stdout.channel.recv_exit_status()
        if exit_code != 0:
            raise RuntimeError(
                f"{cmd} failed: {stderr.read().decode().strip()}"
            )

    def _key_type(self, key_name: str) -> str:
        if "ed25519" in key_name:
            return "ssh-ed25519"
        if "ecdsa" in key_name:
            return "ecdsa-sha2-nistp256"
        if "rsa" in key_name:
            return "rsa-sha2-256"
        return ""


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

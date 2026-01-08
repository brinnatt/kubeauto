import platform
import psutil
import distro
import paramiko
import socket
import sys
import time
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

    def ssh_keys_distribution(
            self,
            host_ips: List[str],
            username: str,
            password: Optional[str] = None,
            pw_file: Optional[str] = None,
            port: int = 22,
            timeout: int = 5,
            ask_pass: bool = False,
            dry_run: bool = False,
            max_workers: int = 10,
    ) -> Dict[str, str]:
        """
        Distribute SSH public keys to remote hosts reliably.

        Password resolution priority (highest → lowest):
          1. --pw-file <JSON_FILE>
          2. --password <STR>
          3. --ask-pass (interactive per-host)
          4. Key-only auth (no password)

        Password file format (pw.json):
        {
          "host1": "pass1",
          "host2": "pass2",
          "groupA": ["host3", "host4"],
          "groupA_password": "passA"
        }
        Hosts not listed default to key-only or --password fallback.
        """
        public_keys = self._load_all_public_keys()
        if not public_keys:
            raise RuntimeError("No SSH public keys found. Tried: id_ed25519.pub, id_ecdsa.pub, id_rsa.pub")

        # Resolve password map — ALL in main thread (thread-safe)
        pw_map: Dict[str, Optional[str]] = {}

        # Step 1: Load from --pw-file
        if pw_file:
            import json
            try:
                with open(pw_file, "r") as f:
                    pw_data = json.load(f)
            except Exception as e:
                raise ValueError(f"Failed to load --pw-file '{pw_file}': {e}")

            # Expand group definitions
            host_to_pw: Dict[str, str] = {}
            for k, v in pw_data.items():
                if k.endswith("_password") and isinstance(v, str):
                    group_name = k[:-9]  # remove '_password'
                    group_hosts = pw_data.get(group_name)
                    if isinstance(group_hosts, list):
                        for h in group_hosts:
                            if isinstance(h, str):
                                host_to_pw[h] = v
                elif isinstance(v, str) and not k.endswith("_password"):
                    # direct host mapping
                    host_to_pw[k] = v

            for host in host_ips:
                pw_map[host] = host_to_pw.get(host)

        # Step 2: --password fallback
        if password is not None:
            for host in host_ips:
                if host not in pw_map or pw_map[host] is None:
                    pw_map[host] = password

        # Step 3: --ask-pass (interactive, per-host, main thread only)
        if ask_pass:
            logger.info("", extra={"to_stdout": True})
            logger.info("Interactive password input (Enter for key-only):", extra={"to_stdout": True})
            for i, host in enumerate(host_ips, 1):
                if host in pw_map and pw_map[host] is not None:
                    continue
                try:
                    pw = getpass.getpass(f"[{i}/{len(host_ips)}] Password for {username}@{host}: ")
                    pw_map[host] = pw if pw.strip() else None
                except (KeyboardInterrupt, EOFError):
                    logger.error("User interrupted password input.", extra={"to_stdout": True})
                    sys.exit(1)

        # Default: key-only
        for host in host_ips:
            pw_map.setdefault(host, None)

        # Run in parallel
        results: Dict[str, str] = {}

        def _worker(host: str) -> str:
            host_pw = pw_map[host]
            for attempt in range(1, 4):  # up to 3 attempts
                try:
                    return self._handle_single_host(
                        host=host,
                        username=username,
                        host_password=host_pw,
                        port=port,
                        timeout=timeout,
                        dry_run=dry_run,
                        public_keys=public_keys,
                    )
                except (paramiko.AuthenticationException, paramiko.SSHException, socket.error) as e:
                    if attempt < 3:
                        time.sleep(0.5 * attempt)
                        continue
                    return f"[FAILED] Auth/SSH error after {attempt} attempts: {e}"
                except Exception as e:
                    return f"[CRASH] {type(e).__name__}: {e}"
            return "[FAILED] Unexpected retry exit"

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_worker, h): h for h in host_ips}
            for future in as_completed(futures):
                host = futures[future]
                results[host] = future.result()

        return results

    def _handle_single_host(
            self,
            host: str,
            username: str,
            host_password: Optional[str],
            port: int,
            timeout: int,
            dry_run: bool,
            public_keys: Dict[str, str],
    ) -> str:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            # Phase 1: Try key auth
            try:
                client.connect(
                    hostname=host,
                    port=port,
                    username=username,
                    timeout=timeout,
                    look_for_keys=True,
                    allow_agent=True,
                    compress=True,
                )
                return "[SKIPPED] Key auth already works"
            except (paramiko.AuthenticationException, paramiko.SSHException, socket.error):
                pass

            # Phase 2: Password auth (if provided)
            if host_password is not None:
                try:
                    client.connect(
                        hostname=host,
                        port=port,
                        username=username,
                        password=host_password,
                        timeout=timeout,
                        look_for_keys=False,
                        allow_agent=False,
                        compress=True,
                    )
                except paramiko.AuthenticationException:
                    raise
                except Exception as e:
                    raise RuntimeError(f"Connection failed: {e}")

            # Final key retry (in case password was wrong)
            if host_password is None:
                try:
                    client.connect(
                        hostname=host,
                        port=port,
                        username=username,
                        timeout=timeout,
                        look_for_keys=True,
                        allow_agent=True,
                        compress=True,
                    )
                    return "[SKIPPED] Key auth succeeded on retry"
                except Exception:
                    raise RuntimeError("Auth failed: no password and key auth unavailable")

            # Phase 3: Deploy keys
            if dry_run:
                return f"[DRY-RUN] Install {len(public_keys)} keys: {', '.join(public_keys.keys())}"

            import base64
            keys_combined = "\n".join(public_keys.values()) + "\n"
            keys_b64 = base64.b64encode(keys_combined.encode()).decode()

            script = f"""set -euo pipefail
install -d -m 700 ~/.ssh
touch ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

echo '{keys_b64}' | base64 -d | while IFS= read -r key; do
    [[ -n "$key" ]] || continue
    grep -qxF "$key" ~/.ssh/authorized_keys || echo "$key" >> ~/.ssh/authorized_keys
done

restorecon -Rv ~/.ssh 2>/dev/null || true
"""
            stdin, stdout, stderr = client.exec_command(script)
            rc = stdout.channel.recv_exit_status()
            stderr_text = stderr.read().decode().strip()

            if rc == 0:
                return "[SUCCESS] Keys installed"
            else:
                err = stderr_text or stdout.read().decode().strip()
                return f"[FAILED] Deploy failed (rc={rc}): {err[:256]}"

        finally:
            client.close()

    def _load_all_public_keys(self) -> Dict[str, str]:
        ssh_dir = Path.home() / ".ssh"
        keys_priority = ["id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"]
        keys: Dict[str, str] = {}
        for name in keys_priority:
            path = ssh_dir / name
            if path.exists():
                content = path.read_text().strip()
                if content and "ssh-" in content and " " in content:
                    keys[name] = content
        if keys:
            return keys

        # Generate ed25519
        private_key = ssh_dir / "id_ed25519"
        logger.info("No SSH keys found, generating ed25519...", extra={"to_stdout": True})
        stdout, stderr, rc = self.executor.execute(
            f"ssh-keygen -t ed25519 -N '' -f {private_key} -q"
        )
        if rc != 0:
            raise RuntimeError(f"Keygen failed: {stderr.strip()}")
        pub = private_key.with_suffix(".pub")
        if not pub.exists():
            raise RuntimeError("Public key missing after generation")
        content = pub.read_text().strip()
        if not (content.startswith("ssh-") and " " in content):
            raise RuntimeError("Invalid public key format")
        return {"id_ed25519.pub": content}


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

import platform
import psutil
import distro
import paramiko
import base64
import json
import socket
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import sys
import subprocess
import getpass
import threading
import os
from .logger import setup_logger, LOG_STDOUT
from .utils import expand_host_targets, parse_pw_file_host_passwords
from typing import Dict, Generator, Union, List, Optional, Tuple
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = setup_logger(__name__)

# Thread-safe lock for known_hosts file operations
_known_hosts_lock = threading.Lock()


class _DeferredAutoAddPolicy(paramiko.MissingHostKeyPolicy):
    """Accept an unknown key; the caller persists it through the locked path."""

    def missing_host_key(self, client, hostname, key):
        # Paramiko AutoAddPolicy calls client.save_host_keys() here.  With
        # concurrent system -a workers that causes unsynchronised in-place
        # rewrites.  Keep AutoAddPolicy's acceptance semantics, but defer the
        # write to _save_host_key_thread_safe() after connect succeeds.
        client._host_keys.add(hostname, key.get_name(), key)


class SystemProbe:
    """handle system probe or execute command"""

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

        Host tokens support IPv4 last-octet ranges, e.g. 192.168.139.129-134.
        """
        host_ips = expand_host_targets(host_ips)
        public_keys = self._load_all_public_keys()
        if not public_keys:
            raise RuntimeError("No SSH public keys found. Tried: id_ed25519.pub, id_ecdsa.pub, id_rsa.pub")

        # Resolve password map — ALL in main thread (thread-safe)
        pw_map: Dict[str, Optional[str]] = {}

        # Step 1: Load from --pw-file
        if pw_file:
            try:
                with open(pw_file, "r") as f:
                    pw_data = json.load(f)
            except Exception as e:
                raise ValueError(f"Failed to load --pw-file '{pw_file}': {e}")

            host_to_pw = parse_pw_file_host_passwords(pw_data)
            pw_map.update({host: host_to_pw.get(host) for host in host_ips})

        # Step 2: --password fallback
        if password is not None:
            for host in host_ips:
                if host not in pw_map or pw_map[host] is None:
                    pw_map[host] = password

        # Step 3: --ask-pass (interactive, per-host, main thread only)
        if ask_pass:
            logger.info("", extra=LOG_STDOUT)
            logger.info("Interactive password input (Enter for key-only):", extra=LOG_STDOUT)
            for i, host in enumerate(host_ips, 1):
                if host in pw_map and pw_map[host] is not None:
                    continue
                try:
                    pw = getpass.getpass(f"[{i}/{len(host_ips)}] Password for {username}@{host}: ")
                    pw_map[host] = pw if pw.strip() else None
                except (KeyboardInterrupt, EOFError):
                    logger.error("User interrupted password input.", extra=LOG_STDOUT)
                    sys.exit(1)

        # Default: key-only
        for host in host_ips:
            pw_map.setdefault(host, None)

        # Run in parallel
        results: Dict[str, str] = {}

        def _worker(host_ip: str):
            host_pw = pw_map[host_ip]
            try:
                return self._handle_single_host(
                    host=host_ip,
                    username=username,
                    host_password=host_pw,
                    port=port,
                    timeout=timeout,
                    dry_run=dry_run,
                    public_keys=public_keys,
                )
            except (paramiko.AuthenticationException, paramiko.SSHException, socket.error) as ex:
                return f"[FAILED] Auth/SSH error: {ex}"
            except Exception as ex:
                return f"[CRASH] {type(ex).__name__}: {ex}"

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
    ):
        # Prepare known_hosts file path
        ssh_dir = Path.home() / ".ssh"
        known_hosts_path = ssh_dir / "known_hosts"
        
        # Ensure .ssh directory exists
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        
        client = paramiko.SSHClient()
        
        if known_hosts_path.exists():
            self._load_host_keys_thread_safe(client, known_hosts_path)
        
        # Accept unknown host keys as before, but serialize persistence after
        # connect instead of letting Paramiko AutoAddPolicy write concurrently.
        client.set_missing_host_key_policy(_DeferredAutoAddPolicy())
        
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
                # Save host key to known_hosts (thread-safe)
                self._save_host_key_thread_safe(client, host, port, known_hosts_path)
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
                    # Save host key to known_hosts (thread-safe)
                    self._save_host_key_thread_safe(client, host, port, known_hosts_path)
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
                    # Save host key to known_hosts (thread-safe)
                    self._save_host_key_thread_safe(client, host, port, known_hosts_path)
                    return "[SKIPPED] Key auth succeeded on retry"
                except Exception:
                    raise RuntimeError("Auth failed: no password and key auth unavailable")

            # Phase 3: Deploy keys
            if dry_run:
                return f"[DRY-RUN] Install {len(public_keys)} keys: {', '.join(public_keys.keys())}"

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

    @staticmethod
    def _load_host_keys_thread_safe(
            client: paramiko.SSHClient,
            known_hosts_path: Path,
    ) -> None:
        """Load host keys under the save lock and repair only invalid lines."""
        with _known_hosts_lock:
            try:
                client.load_host_keys(str(known_hosts_path))
                return
            except paramiko.hostkeys.InvalidHostKey as ex:
                logger.warning(
                    "Invalid SSH host-key record in %s; removing only invalid records.",
                    known_hosts_path,
                )
                SystemProbe._repair_invalid_host_key_lines(known_hosts_path)
                try:
                    client.load_host_keys(str(known_hosts_path))
                    return
                except Exception as retry_ex:
                    logger.debug(
                        f"Failed to load repaired known_hosts: {retry_ex}"
                    )
            except Exception as ex:
                logger.debug(f"Failed to load known_hosts: {ex}")

    @staticmethod
    def _repair_invalid_host_key_lines(known_hosts_path: Path) -> None:
        """Atomically discard records whose key data Paramiko cannot decode."""
        original_mode = known_hosts_path.stat().st_mode & 0o777
        valid_lines = []
        for lineno, line in enumerate(
                known_hosts_path.read_text(errors="surrogateescape").splitlines(True),
                1,
        ):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                valid_lines.append(line)
                continue
            try:
                paramiko.hostkeys.HostKeyEntry.from_line(stripped, lineno)
            except paramiko.hostkeys.InvalidHostKey:
                continue
            valid_lines.append(line)

        temporary_path = known_hosts_path.with_name(
            f".{known_hosts_path.name}.repair-{os.getpid()}-{threading.get_ident()}"
        )
        try:
            temporary_path.write_text(
                "".join(valid_lines), errors="surrogateescape"
            )
            os.chmod(temporary_path, original_mode)
            os.replace(temporary_path, known_hosts_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _save_host_key_thread_safe(
            client: paramiko.SSHClient,
            host: str,
            port: int,
            known_hosts_path: Path
    ) -> None:
        """
        Thread-safely save host key to known_hosts file.
        
        Based on paramiko official best practices:
        1. Get remote server key from transport after connection
        2. Load existing known_hosts to preserve entries from other threads
        3. Add new host key to HostKeys object
        4. Save merged HostKeys to file
        
        Reference: paramiko documentation for get_transport().get_remote_server_key()
        """
        with _known_hosts_lock:
            try:
                # Get the remote server's host key from the transport
                # This is the official way to get host key after connection
                transport = client.get_transport()
                if transport is None:
                    logger.debug(f"No transport available for {host}:{port}")
                    return
                
                remote_server_key = transport.get_remote_server_key()
                if remote_server_key is None:
                    logger.debug(f"No remote server key for {host}:{port}")
                    return
                
                # Create or load HostKeys object
                host_keys = paramiko.HostKeys()
                
                # Load existing known_hosts file (if exists) to preserve existing entries
                # This ensures we don't lose entries added by other threads
                if known_hosts_path.exists():
                    try:
                        host_keys.load(str(known_hosts_path))
                    except Exception as ex:
                        logger.debug(f"Failed to load {known_hosts_path}: {ex}, but ignored")
                        pass
                
                # Add host key with both formats for compatibility
                # Format 1: hostname only (for default port 22)
                # Format 2: [hostname]:port (for non-default ports)
                key_type = remote_server_key.get_name()
                
                # Add with hostname format
                host_keys.add(host, key_type, remote_server_key)
                
                # Also add with port format if port is not 22
                if port != 22:
                    host_with_port = f"[{host}]:{port}"
                    host_keys.add(host_with_port, key_type, remote_server_key)
                
                # Save merged host keys to file
                host_keys.save(str(known_hosts_path))
                
                # Ensure file has correct permissions (SSH standard: 644)
                if known_hosts_path.exists():
                    os.chmod(known_hosts_path, 0o644)
                    
            except Exception as e:
                # Log but don't fail the operation if saving host key fails
                logger.debug(f"Failed to save host key for {host}:{port}: {e}")

    @staticmethod
    def _generate_ed25519_keypair(ssh_dir: Path) -> Tuple[Path, str]:
        """
        Generate ed25519 key pair in OpenSSH format.

        [fix] In-process generation avoids ssh-keygen OpenSSL symbol errors
              (e.g. EVP_sm4_ctr) when Python pollutes LD_LIBRARY_PATH.
        [optimize] Uses project dependency cryptography; no shell subprocess.
        """
        ssh_dir.mkdir(mode=0o700, exist_ok=True)
        private_key_path = ssh_dir / "id_ed25519"
        public_key_path = private_key_path.with_suffix(".pub")

        key = Ed25519PrivateKey.generate()
        private_key_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.OpenSSH,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        comment = f"{getpass.getuser()}@{socket.gethostname()}"
        public_line = (
            key.public_key()
            .public_bytes(
                encoding=serialization.Encoding.OpenSSH,
                format=serialization.PublicFormat.OpenSSH,
            )
            .decode()
            + f" {comment}"
        )
        public_key_path.write_text(public_line + "\n", encoding="utf-8")
        os.chmod(private_key_path, 0o600)
        os.chmod(public_key_path, 0o644)
        return public_key_path, public_line

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

        # [fix] 无本地密钥时改用 cryptography 生成，规避 ssh-keygen 与系统 OpenSSL 链接冲突
        logger.info("No SSH keys found, generating ed25519...", extra=LOG_STDOUT)
        _, content = self._generate_ed25519_keypair(ssh_dir)
        if not (content.startswith("ssh-") and " " in content):
            raise RuntimeError("Invalid public key format")
        return {"id_ed25519.pub": content}

    # Stdlib-only probe script for remote hosts (no psutil on targets).
    _REMOTE_PROBE_PY = r"""
import json, os

def disk_rows():
    skip_fs = {
        "proc", "sysfs", "devtmpfs", "tmpfs", "squashfs", "overlay", "cgroup2",
        "cgroup", "pstore", "bpf", "tracefs", "debugfs", "securityfs", "configfs",
        "fusectl", "mqueue", "hugetlbfs", "devpts", "autofs", "binfmt_misc",
    }
    rows = []
    for line in open("/proc/mounts", encoding="utf-8", errors="replace"):
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fstype = parts[0], parts[1], parts[2]
        if fstype in skip_fs or mnt.startswith(("/proc", "/sys", "/run", "/dev")):
            continue
        try:
            st = os.statvfs(mnt)
            total = st.f_frsize * st.f_blocks
            free = st.f_frsize * st.f_bavail
            used = total - free
            pct = (used / total * 100) if total else 0
            rows.append({
                "device": dev, "mount": mnt, "fstype": fstype,
                "total_gb": round(total / 1024 ** 3, 2),
                "used_gb": round(used / 1024 ** 3, 2),
                "free_gb": round(free / 1024 ** 3, 2),
                "usage_percent": round(pct, 1),
            })
        except OSError:
            pass
    return rows

def resources():
    mem = {}
    for line in open("/proc/meminfo", encoding="utf-8", errors="replace"):
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        mem[k.strip()] = int(v.split()[0])
    total_kb = mem.get("MemTotal", 0)
    avail_kb = mem.get("MemAvailable", mem.get("MemFree", 0))
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)
    load = open("/proc/loadavg", encoding="utf-8").read().split()
    cpus = os.cpu_count() or 1
    cpuinfo = open("/proc/cpuinfo", encoding="utf-8", errors="replace").read()
    threads = max(cpuinfo.count("processor"), cpus)
    usage = round(float(load[0]) / cpus * 100, 1) if cpus else 0.0
    return {
        "cpu_cores": cpus,
        "cpu_threads": threads,
        "cpu_usage_percent": usage,
        "memory_total_gb": round(total_kb / 1024 / 1024, 2),
        "memory_available_gb": round(avail_kb / 1024 / 1024, 2),
        "memory_usage_percent": round((1 - avail_kb / total_kb) * 100, 1) if total_kb else 0,
        "swap_total_gb": round(swap_total / 1024 / 1024, 2),
        "swap_used_gb": round((swap_total - swap_free) / 1024 / 1024, 2),
        "swap_free_gb": round(swap_free / 1024 / 1024, 2),
    }

def networks():
    rows = []
    with open("/proc/net/dev", encoding="utf-8", errors="replace") as f:
        next(f)
        next(f)
        for line in f:
            if ":" not in line:
                continue
            name, data = line.split(":", 1)
            name = name.strip()
            if name == "lo":
                continue
            nums = data.split()
            rx, tx = int(nums[0]), int(nums[8])
            rows.append({
                "interface": name,
                "traffic_mb": {"sent": round(tx / 1024 / 1024, 2), "recv": round(rx / 1024 / 1024, 2)},
            })
    return rows

print(json.dumps({"disks": disk_rows(), "resources": resources(), "networks": networks()}))
"""

    def _ssh_exec(
            self,
            host: str,
            username: str,
            command: str,
            port: int = 22,
            timeout: int = 10,
    ) -> Tuple[str, str, int]:
        """Run command on remote host via SSH key auth."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=port,
            username=username,
            timeout=timeout,
            look_for_keys=True,
            allow_agent=True,
            compress=True,
        )
        try:
            _, stdout, stderr = client.exec_command(command, timeout=timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            rc = stdout.channel.recv_exit_status()
            return out, err, rc
        finally:
            client.close()

    def remote_probe(
            self,
            host_ips: List[str],
            username: str = "root",
            port: int = 22,
            timeout: int = 10,
            max_workers: int = 10,
    ) -> Dict[str, Union[Dict, str]]:
        """Collect disk/cpu/network stats from remote hosts over SSH."""
        host_ips = expand_host_targets(host_ips)
        script_b64 = base64.b64encode(self._REMOTE_PROBE_PY.encode()).decode()
        py_code = f"import base64; exec(base64.b64decode('{script_b64}').decode())"
        py_arg = json.dumps(py_code)
        remote_cmd = (
            "for py in /usr/bin/python3 /usr/libexec/platform-python python3; do "
            f'[ -x "$py" ] || continue; "$py" -c {py_arg} && exit 0; done; exit 127'
        )

        results: Dict[str, Union[Dict, str]] = {}

        def _worker(host: str):
            try:
                out, err, rc = self._ssh_exec(host, username, remote_cmd, port=port, timeout=timeout)
                if rc != 0:
                    return f"[FAILED] exit {rc}: {err.strip() or out.strip()}"
                return json.loads(out)
            except json.JSONDecodeError as ex:
                return f"[FAILED] invalid probe JSON: {ex}"
            except (paramiko.AuthenticationException, paramiko.SSHException, socket.error) as ex:
                return f"[FAILED] SSH error: {ex}"
            except Exception as ex:
                return f"[CRASH] {type(ex).__name__}: {ex}"

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_worker, h): h for h in host_ips}
            for future in as_completed(futures):
                host = futures[future]
                results[host] = future.result()

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

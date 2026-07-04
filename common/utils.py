"""
Utility functions for kubeauto
"""
import os
import sys
import site
import subprocess
import shutil
import ipaddress
import re
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union
from enum import Enum
from pathlib import Path
from .logger import setup_logger, LOG_STDOUT
from .exceptions import CommandExecutionError, InstallPrereqError

logger = setup_logger(__name__)


def ensure_kubeauto_clusters_dir(base_path: Union[Path, str]) -> Path:
    """Ensure /usr/local/kubeauto/clusters exists and is writable by the invoking user."""
    clusters_dir = Path(base_path) / "clusters"
    try:
        clusters_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise InstallPrereqError(
            f"Cannot create cluster directory {clusters_dir}: permission denied. "
            f"Run once as root: mkdir -p {clusters_dir} && chown $USER:$USER {clusters_dir}"
        ) from exc

    if os.getuid() == 0:
        owner = os.environ.get("SUDO_USER") or os.environ.get("USER")
        if owner and owner != "root":
            try:
                import pwd
                pw = pwd.getpwnam(owner)
                os.chown(clusters_dir, pw.pw_uid, pw.pw_gid)
            except (ImportError, KeyError, OSError):
                pass
    return clusters_dir


def copy_file_to_remote(
    local_path: Union[Path, str],
    remote_path: str,
    host: str,
    port: int = 22,
    username: str = "root",
    mode: int = 0o400,
    timeout: int = 30,
    *,
    password: Optional[str] = None,
    key_filename: Optional[Union[str, Path, Sequence[Union[str, Path]]]] = None,
    look_for_keys: bool = True,
    allow_agent: bool = True,
) -> None:
    """
    Copy a local file to a remote host via SFTP (paramiko).

    Auth (try in order when not specified): key_filename -> agent/key scan -> password.
    Remote parent directory is created if missing.

    Args:
        local_path: Local file path.
        remote_path: Remote path (e.g. /root/.kube/config).
        host: Remote host IP or hostname.
        port: SSH port (default 22; production may use non-default).
        username: SSH user (default root).
        mode: Remote file mode (default 0o400).
        timeout: Connect timeout in seconds.
        password: Optional password auth (e.g. when no key).
        key_filename: Optional key file path(s) (str, Path, or list).
        look_for_keys: Whether to try default key files (default True).
        allow_agent: Whether to use SSH agent (default True).
    """
    import paramiko

    local_path = Path(local_path)
    if not local_path.is_file():
        raise CommandExecutionError(f"Local file not found: {local_path}")

    connect_kwargs: dict = {
        "hostname": host,
        "port": port,
        "username": username,
        "timeout": timeout,
        "compress": True,
        "look_for_keys": look_for_keys,
        "allow_agent": allow_agent,
    }
    if password is not None:
        connect_kwargs["password"] = password
        connect_kwargs["look_for_keys"] = look_for_keys  # caller can still allow key try
        connect_kwargs["allow_agent"] = allow_agent
    if key_filename is not None:
        if isinstance(key_filename, (str, Path)):
            connect_kwargs["key_filename"] = str(key_filename)
        else:
            connect_kwargs["key_filename"] = [str(p) for p in key_filename]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(**connect_kwargs)
        remote_dir = remote_path.rsplit("/", 1)[0] if "/" in remote_path else "."
        stdin, stdout, stderr = client.exec_command(f"mkdir -p {remote_dir}")
        if stdout.channel.recv_exit_status() != 0:
            err = stderr.read().decode().strip()
            raise CommandExecutionError(f"mkdir on {host} failed: {err}")

        sftp = client.open_sftp()
        try:
            sftp.put(str(local_path), remote_path)
            sftp.chmod(remote_path, mode)
        finally:
            sftp.close()
    except paramiko.SSHException as e:
        raise CommandExecutionError(f"SSH/SFTP to {host} failed: {e}")
    finally:
        client.close()


def run_command(cmd: List[str] | Tuple[str] | str,
                check: bool = True,
                capture_output=True,
                allowed_exit_codes: List[int] = None,
                **kwargs):
    """Run a shell command with error handling"""
    logger.debug(f"Executing command: {' '.join(cmd) if isinstance(cmd, (list, tuple)) else cmd}")

    # [fix subprocess grammar] if SHELL enabled, CMD must be string, because LIST takes no effect in this case.
    if isinstance(cmd, (list, tuple)):
        if kwargs.get("shell"):
            cmd = " ".join(cmd)
    elif isinstance(cmd, str):
        if not kwargs.get("shell"):
            cmd = cmd.split()
    else:
        raise CommandExecutionError(
            f"Command must be either a list or a tuple or a string, please check you command {cmd}"
        )

    # [fix subprocess grammar] Handle stdout/stderr and capture_output conflict.
    # Disable capture_output if stdout/stderr is provided
    if capture_output and ("stdout" in kwargs or "stderr" in kwargs):
        capture_output = False

    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True, **kwargs)
        return result
    except subprocess.CalledProcessError as e:
        if allowed_exit_codes and e.returncode in allowed_exit_codes:
            return e
        # Build detailed error message
        error_msg = (
            f"Command failed with exit code {e.returncode}: {' '.join(e.cmd) if isinstance(e.cmd, (list, tuple)) else cmd}\n"
            f"Error output: {e.stderr.strip() if e.stderr else '(empty)'}\n"
            f"Standard output: {e.stdout.strip() if e.stdout else '(empty)'}"
        )
        raise CommandExecutionError(error_msg)
    except Exception as e:
        raise CommandExecutionError(f"Command failed: {e}")


def rmrf(path: Path) -> None:
    try:
        if not path.exists():
            return
        elif path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    except Exception as e:
        raise CommandExecutionError(f"Failed to remove {path}: {e}")


def validate_ip(ip: str) -> bool:
    """Validate an IP address"""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


_RE_IPV4_LAST_OCTET_RANGE = re.compile(
    r"^(?P<prefix>(?:\d{1,3}\.){3})(?P<start>\d{1,3})-(?P<end>\d{1,3})$"
)

DEFAULT_MAX_HOST_EXPAND = 256


def expand_host_target(token: str, *, max_hosts: int = DEFAULT_MAX_HOST_EXPAND) -> List[str]:
    """
    Expand one host token into concrete addresses.

    Supported forms:
      - Single host: 192.168.139.129 or hostname
      - IPv4 last-octet range: 192.168.139.129-134
    """
    token = token.strip()
    if not token:
        return []

    match = _RE_IPV4_LAST_OCTET_RANGE.match(token)
    if not match:
        return [token]

    prefix = match.group("prefix")
    start, end = int(match.group("start")), int(match.group("end"))
    if start > end:
        raise ValueError(f"Invalid host range '{token}': start octet > end octet")
    count = end - start + 1
    if count > max_hosts:
        raise ValueError(
            f"Host range '{token}' expands to {count} addresses (limit {max_hosts})"
        )

    ips = [f"{prefix}{octet}" for octet in range(start, end + 1)]
    try:
        for ip in ips:
            ipaddress.IPv4Address(ip)
    except ipaddress.AddressValueError as e:
        raise ValueError(f"Invalid host range '{token}': {e}") from e
    return ips


def expand_host_targets(
        tokens: Iterable[str],
        *,
        max_hosts: int = DEFAULT_MAX_HOST_EXPAND,
) -> List[str]:
    """Expand host tokens and deduplicate while preserving order."""
    return list(dict.fromkeys(
        host
        for token in tokens
        for host in expand_host_target(token, max_hosts=max_hosts)
    ))


def iter_pw_file_bindings(pw_data: Mapping[str, object]) -> Iterator[tuple[str, str]]:
    """Yield (host_token, password) pairs from pw.json; tokens may include ranges."""
    for key, value in pw_data.items():
        if key.endswith("_password"):
            if not isinstance(value, str):
                continue
            group = pw_data.get(key[:-9])
            if not isinstance(group, list):
                continue
            yield from ((token, value) for token in group if isinstance(token, str))
        elif isinstance(value, str):
            yield key, value


def parse_pw_file_host_passwords(
        pw_data: Mapping[str, object],
        *,
        max_hosts: int = DEFAULT_MAX_HOST_EXPAND,
) -> Dict[str, str]:
    """Map each expanded host to its password from pw.json."""
    return {
        host: password
        for token, password in iter_pw_file_bindings(pw_data)
        for host in expand_host_target(token, max_hosts=max_hosts)
    }


def parse_pw_file_hosts(
        pw_data: Mapping[str, object],
        *,
        max_hosts: int = DEFAULT_MAX_HOST_EXPAND,
) -> List[str]:
    """All hosts referenced in pw.json, expanded and deduped in order."""
    tokens = (token for token, _ in iter_pw_file_bindings(pw_data))
    return expand_host_targets(tokens, max_hosts=max_hosts)


def get_host_ip() -> str:
    """Get host's primary IP address"""
    try:
        # Try using ip command
        result = run_command(["ip", "route", "get", "1"])
        interface = result.stdout.split("dev ")[1].split(" ")[0]
        result = run_command(["ip", "addr", "show", interface])
        ip_line = [line for line in result.stdout.split('\n') if "inet " in line][0]
        ip = ip_line.split("inet ")[1].split("/")[0]
        return ip
    except Exception as e:
        logger.warning(f"Failed to get host IP: {e}")
        return "127.0.0.1"


def ssh_localhost() -> None:
    """Setup SSH keys if they don't exist"""
    ssh_dir = Path.home() / ".ssh"
    ssh_dir.mkdir(exist_ok=True, mode=0o700)

    private_key = ssh_dir / "id_rsa"
    if not private_key.exists():
        logger.info("Generating SSH key pair", extra=LOG_STDOUT)
        run_command(f"ssh-keygen -t rsa -b 2048 -N '' -f {private_key}", shell=True)

    authorized_keys = ssh_dir / "authorized_keys"
    authorized_keys.touch(mode=0o600)

    public_key = ssh_dir / "id_rsa.pub"
    if public_key.exists() and authorized_keys.exists():
        with open(public_key) as f:
            pub_key_content = f.read().strip()
        with open(authorized_keys) as f:
            auth_keys_content = f.read()
        if pub_key_content not in auth_keys_content:
            with open(authorized_keys, "a") as f:
                f.write(f"\n{pub_key_content}\n")

    # Add host to known_hosts
    host_ip = get_host_ip()
    known_hosts = ssh_dir / "known_hosts"
    run_command(["ssh-keyscan", "-t", "ecdsa", "-H", host_ip], stdout=known_hosts.open("a"))


def confirm_action(prompt: str, timeout: int = 5) -> bool:
    """Ask for confirmation with timeout. Non-interactive sessions auto-proceed."""
    import select
    import sys
    import time

    logger.warning(f"{prompt} (timeout: {timeout}s)")
    if not sys.stdin.isatty():
        logger.info("Non-interactive session: proceeding automatically.")
        return True

    sys.stdout.write("Press any key to abort...")
    sys.stdout.flush()

    rlist, _, _ = select.select([sys.stdin], [], [], timeout)
    if rlist:
        sys.stdin.read(1)
        logger.warning("Action aborted by user")
        return False
    return True


def get_resource_path(*parts):
    """返回在开发环境或被 PyInstaller 打包后的资源绝对路径（拼接 parts）
    Usage: get_resource_path('playbooks', 'my.yml')
    """
    if getattr(sys, 'frozen', False):
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).resolve().parent.parent  # 项目根目录
    return str(base.joinpath(*parts))


def get_pkg_dir(package_name: str, extra_paths: Optional[List[str]] = None) -> str:
    """
    查找指定 package 在当前 Python 环境下的路径。
    1. 默认只查当前解释器的 site-packages 和 sys.path。
    2. 允许调用方通过 extra_paths 显式传递搜索目录。

    Args:
        package_name (str): 包名，例如 'ansible_runner'
        extra_paths (List[str], optional): 额外的搜索路径

    Returns:
        str: 包所在的绝对路径

    Raises:
        RuntimeError: 如果找不到该包
    """

    search_paths: List[str] = []

    # 1. 额外路径优先
    if extra_paths:
        search_paths.extend(extra_paths)

    # 2. site-packages
    search_paths.extend(site.getsitepackages())

    # 3. sys.path（兼容 venv）
    search_paths.extend(sys.path)

    # 去重 & 转 Path
    seen = set()
    for p in search_paths:
        if not p:
            continue
        if p in seen:
            continue
        seen.add(p)
        candidate = Path(p) / package_name
        if candidate.exists() and candidate.is_dir():
            return str(candidate)

    raise RuntimeError(
        f"Cannot find '{package_name}'. "
        f"Checked paths: {search_paths}"
    )


class AnsiColor(Enum):
    RED = "\033[91m"
    YELLOW = "\033[93m"
    GREEN = "\033[92m"
    RESET = "\033[0m"

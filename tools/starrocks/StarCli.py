#!/usr/bin/env python3
"""
StarRocks 自动化部署工具
支持 FE/BE/CN 节点部署、高可用集群、systemd 服务管理
Python 3.9+ 兼容
"""

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional, Union

import distro

# 日志配置常量
LOG_DIR = os.getenv("STARROCKS_LOG_DIR", os.path.join(os.getcwd(), "logs"))
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, "starrocks_deploy.log")
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _env_for_system_subprocess():
    """Return envvars so subprocess (ssh/scp) use system libs, not the PyInstaller bundle.

    Used when this script is run as a PyInstaller one-file binary on Linux (e.g. Kylin). Without this, ssh
    can load libcrypto from the unpacked bundle and fail with "OPENSSL_1_1_1f not found".

    Background
    ----------
    PyInstaller bundles the Python interpreter and shared-library dependencies (e.g. libcrypto, libssl)
    from the build machine (the host where pyinstaller is run) into the executable. At runtime the bootloader extracts them to a temporary directory
    (sys._MEIPASS, e.g. /tmp/_MEIxxxxxx) and prepends that path to LD_LIBRARY_PATH so the frozen process
    can load those .so files. The original LD_LIBRARY_PATH is saved in LD_LIBRARY_PATH_ORIG.

    References:
    - What PyInstaller bundles and one-file extraction:
      https://pyinstaller.org/en/stable/operating-mode.html
    - Bootstrap: LD_LIBRARY_PATH_ORIG and prepend to LD_LIBRARY_PATH (GNU/Linux):
      https://pyinstaller.org/en/stable/advanced-topics.html#the-bootstrap-process-in-detail

    Why ssh sees the bundle's libcrypto
    -----------------------------------
    Subprocesses inherit the parent's environment. So ssh runs with LD_LIBRARY_PATH still pointing at _MEIPASS. The dynamic linker then loads
    libcrypto from the bundle instead of the system. Host ssh (e.g. on Kylin) is built against the host's
    OpenSSL and expects symbols like OPENSSL_1_1_1f from the host's libcrypto; the bundled libcrypto from
    the build machine does not provide that symbol version, so ssh fails with "OPENSSL_1_1_1f not found".

    Reference:
    - Launching external programs and inherited library path:
      https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application

    Solution
    --------
    Before spawning ssh/scp, pass env that restores LD_LIBRARY_PATH from
    LD_LIBRARY_PATH_ORIG (or set it to empty). Then the child processes use system libraries only; host ssh
    and host libcrypto remain ABI-compatible.

    Reference (official recipe):
    - LD_LIBRARY_PATH / LIBPATH considerations:
      https://pyinstaller.org/en/stable/runtime-information.html#ld-library-path-libpath-considerations
    """
    env = os.environ.copy()
    if sys.platform.startswith("linux"):
        lp_orig = os.environ.get("LD_LIBRARY_PATH_ORIG")
        env["LD_LIBRARY_PATH"] = lp_orig if lp_orig is not None else ""
    return env


class CommandExecutionError(Exception):
    """命令执行异常"""
    pass


def setup_logger(
        name: str = "starrocks_deploy",
        log_file: Optional[str] = None,
        level: int = DEFAULT_LOG_LEVEL,
        fmt: str = DEFAULT_FORMAT,
        datefmt: str = DEFAULT_DATEFMT,
        handlers: Optional[List[logging.Handler]] = None
) -> logging.Logger:
    """设置日志记录器"""
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)

    log = logging.getLogger(name)
    log.setLevel(level)

    if log.hasHandlers():
        return log  # 避免重复添加 handler

    # 如果没有指定 handlers，则默认使用文件 handler 或标准输出
    if handlers is None:
        log_file = log_file or DEFAULT_LOG_FILE
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        formatter = logging.Formatter(fmt, datefmt)
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        # 默认输入到文件，传 extra={'skip_file': True} 不输入到文件
        file_handler.addFilter(lambda record: not getattr(record, 'skip_file', False))
        log.addHandler(file_handler)

        # 添加一个默认的 stdout handler 但不启用
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(level)
        # 传 extra={'to_stdout': True} 才可以标准输出
        stdout_handler.addFilter(lambda record: getattr(record, 'to_stdout', False))
        log.addHandler(stdout_handler)
    else:
        for handler in handlers:
            handler.setLevel(level)
            if not handler.formatter:
                handler.setFormatter(logging.Formatter(fmt, datefmt))
            log.addHandler(handler)

    return log


logger = setup_logger(__name__)

# 配置常量
MIN_PORT = 1
MAX_PORT = 65535
MAX_HOSTNAME_LENGTH = 253  # RFC 1123 主机名最大长度
DEFAULT_FE_PORTS = {
    'http_port': 8030,
    'rpc_port': 9020,
    'query_port': 9030,
    'edit_log_port': 9010
}
DEFAULT_BE_PORTS = {
    'be_port': 9060,
    'be_http_port': 8040,
    'heartbeat_service_port': 9050,
    'brpc_port': 8060,
    'starlet_port': 9070
}
DEFAULT_CN_PORTS = {
    'be_port': 9060,
    'be_http_port': 8040,
    'heartbeat_service_port': 9050,
    'brpc_port': 8060
}


class InputValidator:
    """输入验证器"""

    @staticmethod
    def validate_port(port: int) -> bool:
        """验证端口号范围"""
        return MIN_PORT <= port <= MAX_PORT

    @staticmethod
    def validate_ip_or_cidr(ip_cidr: str) -> bool:
        """验证IP地址或CIDR格式"""
        if not ip_cidr:
            return False
        # 支持CIDR格式: x.x.x.x/xx
        if '/' in ip_cidr:
            parts = ip_cidr.split('/')
            if len(parts) != 2:
                return False
            try:
                mask = int(parts[1])
                if not (0 <= mask <= 32):
                    return False
            except ValueError:
                return False
            ip_cidr = parts[0]

        # 验证IP地址
        parts = ip_cidr.split('.')
        if len(parts) != 4:
            return False
        try:
            return all(0 <= int(p) <= 255 for p in parts)
        except ValueError:
            return False

    @staticmethod
    def validate_hostname(hostname: str) -> bool:
        """验证主机名或IPv4格式"""
        if not hostname or len(hostname) > MAX_HOSTNAME_LENGTH:
            return False
        if re.match(r'^(\d{1,3}\.){3}\d{1,3}$', hostname):
            try:
                return all(0 <= int(p) <= 255 for p in hostname.split('.'))
            except ValueError:
                return False
        return bool(re.match(r'^[a-zA-Z0-9.-]+$', hostname))

    @staticmethod
    def validate_path(path: str) -> bool:
        """验证路径格式"""
        if not path or '..' in path:
            return False
        try:
            Path(path).resolve()
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def validate_storage_path(storage_path: str) -> bool:
        """验证存储路径格式: /path1,medium:HDD;/path2,medium:SSD;/path3"""
        if not storage_path:
            return False
        paths = storage_path.split(';')
        for path_spec in paths:
            path_spec = path_spec.strip()
            if not path_spec:
                continue
            # 检查是否包含medium指定
            if ',' in path_spec:
                parts = path_spec.split(',')
                if len(parts) != 2:
                    return False
                path_part = parts[0].strip()
                medium_part = parts[1].strip()
                if not medium_part.startswith('medium:'):
                    return False
                medium_type = medium_part.split(':')[1]
                if medium_type not in ['HDD', 'SSD']:
                    return False
            else:
                path_part = path_spec

            if not InputValidator.validate_path(path_part):
                return False
        return True


def run_command(
        cmd: Union[List[str], Tuple[str, ...], str],
        check: bool = True,
        capture_output: bool = True,
        allowed_exit_codes: Optional[List[int]] = None,
        **kwargs
):
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
    except subprocess.TimeoutExpired as e:
        logger.error("命令执行超时: {}".format(e), extra={"to_stdout": True})
        raise CommandExecutionError(f"Command timeout: {' '.join(e.cmd) if isinstance(e.cmd, (list, tuple)) else cmd}")
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
        logger.error("命令执行异常: {}".format(e), extra={"to_stdout": True})
        raise CommandExecutionError(f"Command failed: {e}")


class EnvironmentChecker:
    """环境检查器"""

    @staticmethod
    def check_port_available(port: int, host: str = '0.0.0.0') -> bool:
        """检查端口是否可用"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result != 0
        except Exception as e:
            logger.error("检查端口可用性失败: {}".format(e), extra={"to_stdout": True})
            return False

    @staticmethod
    def _get_os_info() -> Tuple[str, Optional[str]]:
        """获取操作系统信息"""
        return distro.id(), distro.like()

    @staticmethod
    def _find_java_home_from_path(java_path: Path) -> Optional[str]:
        """从Java可执行文件路径推导JAVA_HOME"""
        # 使用Path.resolve()解析符号链接（Python 3.6+支持）
        try:
            resolved_path = java_path.resolve()
        except Exception as e:
            logger.debug("解析Java路径符号链接失败: {}".format(e))
            resolved_path = java_path

        # 向上查找JAVA_HOME（检查lib目录特征）
        for parent in resolved_path.parents:
            if (parent / 'lib').exists() or (parent / 'jre' / 'lib').exists():
                # 验证bin/java存在
                if (parent / 'bin' / 'java').exists() or (parent / 'java').exists():
                    return str(parent)
        return None

    @staticmethod
    def _find_java_home_by_distro() -> Optional[str]:
        """根据Linux发行版查找JAVA_HOME"""
        os_id, os_like = EnvironmentChecker._get_os_info()

        # Debian/Ubuntu系列
        if os_id in ('debian', 'ubuntu') or (os_like and 'debian' in os_like):
            # 使用update-alternatives
            try:
                result = run_command(
                    ['update-alternatives', '--list', 'java'],
                    capture_output=True,
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=5
                )
                if result.returncode == 0 and result.stdout:
                    java_path = Path(result.stdout.strip().split('\n')[0])
                    return EnvironmentChecker._find_java_home_from_path(java_path)
            except Exception as e:
                logger.debug("通过update-alternatives查找Java失败: {}".format(e))

            # 检查常见路径
            common_paths = [
                '/usr/lib/jvm/default-java',
                '/usr/lib/jvm/java-17-openjdk-amd64',
                '/usr/lib/jvm/java-17-openjdk',
            ]
            for path in common_paths:
                path_obj = Path(path)
                if path_obj.exists() and (path_obj / 'bin' / 'java').exists():
                    return str(path_obj)

        # RHEL/CentOS/Rocky系列
        elif os_id in ('rhel', 'centos', 'rocky', 'fedora') or (os_like and 'rhel' in os_like):
            # 使用alternatives
            try:
                result = run_command(
                    ['alternatives', '--display', 'java'],
                    capture_output=True,
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=5
                )
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.split('\n'):
                        if 'currently points to' in line or 'link currently points to' in line:
                            java_path_str = line.split()[-1]
                            java_path = Path(java_path_str)
                            java_home = EnvironmentChecker._find_java_home_from_path(java_path)
                            if java_home:
                                return java_home
            except Exception as e:
                logger.debug("通过alternatives查找Java失败: {}".format(e))

            # 检查常见路径
            common_paths = [
                '/usr/lib/jvm/java-17-openjdk',
                '/usr/lib/jvm/java-17',
                '/usr/lib/jvm/java-1.17.0-openjdk',
            ]
            for path in common_paths:
                path_obj = Path(path)
                if path_obj.exists() and (path_obj / 'bin' / 'java').exists():
                    return str(path_obj)

        # 通用路径检查
        common_paths = [
            '/usr/lib/jvm/default-java',
            '/usr/lib/jvm/default',
            '/usr/lib/jvm/java-17-openjdk',
            '/usr/lib/jvm/java-17',
        ]
        for path in common_paths:
            path_obj = Path(path)
            if path_obj.exists() and (path_obj / 'bin' / 'java').exists():
                return str(path_obj)

        return None

    @staticmethod
    def check_java() -> Tuple[bool, Optional[str], Optional[str]]:
        """检查Java环境 - StarRocks 3.5+需要Java 17+"""
        try:
            # 检查Java是否可用
            result = run_command(
                ['java', '-version'],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=10
            )

            if result.returncode != 0 and not result.stderr:
                return False, None, None

            # 解析Java版本
            version_output = result.stderr if result.stderr else result.stdout
            java_version = None
            if version_output:
                version_match = re.search(r'version\s+"?(\d+)\.?(\d+)?', version_output)
                if version_match:
                    major = int(version_match.group(1))
                    minor = int(version_match.group(2)) if version_match.group(2) else 0
                    # Java 9+ 使用单版本号，Java 8及以下使用1.x格式
                    if major == 1 and version_match.group(2):
                        major = int(version_match.group(2))
                    java_version = f"{major}.{minor}" if minor > 0 else str(major)

                    # StarRocks 3.5+需要Java 17+
                    if major < 17:
                        logger.error("Java版本过低: {}，StarRocks 3.5+需要Java 17或更高版本".format(java_version),
                                     extra={"to_stdout": True})
                        return False, None, java_version

            # 获取JAVA_HOME（使用优化的检测逻辑）
            java_home = os.environ.get('JAVA_HOME')

            if not java_home:
                # 方法1: 使用shutil.which()找到java路径（Python标准库）
                java_bin = shutil.which('java')
                if java_bin:
                    java_path = Path(java_bin)
                    java_home = EnvironmentChecker._find_java_home_from_path(java_path)
                    if java_home:
                        logger.debug(f"通过java路径推导JAVA_HOME: {java_home}")

                # 方法2: 根据发行版查找（使用distro库）
                if not java_home:
                    java_home = EnvironmentChecker._find_java_home_by_distro()
                    if java_home:
                        logger.debug(f"通过发行版检测找到JAVA_HOME: {java_home}")

            # 验证JAVA_HOME有效性
            if java_home:
                java_home_path = Path(java_home)
                if not (java_home_path / 'bin' / 'java').exists():
                    # 可能找到的是jre目录，尝试向上查找
                    if (java_home_path.parent / 'bin' / 'java').exists():
                        java_home = str(java_home_path.parent)
                        logger.debug(f"修正JAVA_HOME为: {java_home}")

            return True, java_home, java_version
        except Exception as e:
            logger.error("检查Java环境失败: {}".format(e), extra={"to_stdout": True})
            return False, None, None

    @staticmethod
    def check_directory_writable(path: str) -> bool:
        """检查目录是否可写"""
        try:
            path_obj = Path(path)
            if not path_obj.exists():
                path_obj.mkdir(parents=True, exist_ok=True)
            test_file = path_obj / '.starrocks_write_test'
            test_file.write_text('test')
            test_file.unlink()
            return True
        except Exception as e:
            logger.error("检查目录可写性失败: {}".format(e), extra={"to_stdout": True})
            return False

    @staticmethod
    def check_user_group_exists(user: str, group: str) -> Tuple[bool, bool]:
        """检查用户和组是否存在"""
        user_exists = False
        group_exists = False
        try:
            # 检查用户
            result = run_command(
                ['id', user],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=5
            )
            user_exists = result.returncode == 0
        except Exception as e:
            logger.error("检查用户是否存在失败: {}".format(e), extra={"to_stdout": True})

        try:
            # 检查组
            result = run_command(
                ['getent', 'group', group],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=5
            )
            group_exists = result.returncode == 0
        except Exception as e:
            logger.error("检查组是否存在失败: {}".format(e), extra={"to_stdout": True})

        return user_exists, group_exists

    @staticmethod
    def check_directory_independent(base_dir: Path, target_dir: str) -> bool:
        """检查目标目录是否独立于基础目录"""
        try:
            base_resolved = base_dir.resolve()
            target_resolved = Path(target_dir).resolve()
            # 检查target_dir是否是base_dir的子目录
            try:
                target_resolved.relative_to(base_resolved)
                logger.warning(f"目录 {target_dir} 位于安装目录 {base_dir} 内，建议使用独立目录",
                               extra={"to_stdout": True})
                return False
            except ValueError:
                # 不在子目录中
                return True
        except Exception as e:
            logger.error("检查目录独立性失败: {}".format(e), extra={"to_stdout": True})
            return False


class SecurityChecker:
    """安全检查器"""

    @staticmethod
    def check_ssh_key_permissions(key_path: str) -> bool:
        """检查SSH私钥文件权限"""
        try:
            file_mode = stat.S_IMODE(os.stat(key_path).st_mode)
            if file_mode != 0o600:
                logger.warning(f"SSH私钥权限不安全: {key_path} (应为600)", extra={"to_stdout": True})
                return False
            return True
        except OSError:
            return False


class SSHManager:
    """SSH管理器 - 用于远程部署"""

    def __init__(
            self,
            host: str,
            port: int,
            username: str = "root",
            key_path: str = "~/.ssh/id_rsa",
            strict_host_key_checking: bool = True
    ):
        if not InputValidator.validate_hostname(host):
            raise ValueError(f"无效的主机名: {host}")
        if not InputValidator.validate_port(port):
            raise ValueError(f"无效的端口号: {port}")

        self.host = host
        self.port = port
        self.username = username
        self.key_path = os.path.expanduser(key_path)
        self.strict_host_key_checking = strict_host_key_checking

        if not os.path.exists(self.key_path):
            raise FileNotFoundError(f"SSH密钥文件不存在: {self.key_path}")
        SecurityChecker.check_ssh_key_permissions(self.key_path)

    def _build_ssh_options(self) -> List[str]:
        ssh_options = [
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3",
            "-o", "BatchMode=yes",  # 禁用交互式密码认证，密钥认证失败时直接失败
            "-p", str(self.port),
            "-i", self.key_path,
        ]
        if self.strict_host_key_checking:
            known_hosts_file = os.path.expanduser("~/.ssh/known_hosts")
            ssh_options.extend([
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts_file}",
            ])
        else:
            logger.warning("警告: 已禁用SSH主机密钥检查", extra={"to_stdout": True})
            ssh_options.extend(["-o", "StrictHostKeyChecking=no"])
        return ssh_options

    def run_command(self, cmd: str, timeout: Optional[int] = None) -> Tuple[bool, str]:
        """执行远程命令"""
        if not cmd:
            return False, "命令为空"
        # 构建 SSH 命令：ssh [options] user@host "command"
        # 直接传递命令字符串给 SSH，让远程 shell 执行
        ssh_cmd = ["ssh"] + self._build_ssh_options() + [f"{self.username}@{self.host}", cmd]
        try:
            result = run_command(ssh_cmd, check=False, capture_output=True, timeout=timeout or 3600, env=_env_for_system_subprocess())
            if result.returncode == 0:
                return True, result.stdout if result.stdout else ""
            else:
                # 合并 stdout 和 stderr，确保完整返回所有错误信息
                # 注意：如果远程命令使用了 2>&1，所有输出都在 stdout 中，stderr 可能为空
                output_parts = []
                if result.stderr:
                    output_parts.append(f"[stderr] {result.stderr}")
                if result.stdout:
                    output_parts.append(f"[stdout] {result.stdout}")
                if output_parts:
                    error_msg = "\n".join(output_parts)
                else:
                    error_msg = f"命令执行失败，返回码: {result.returncode}"
                return False, error_msg
        except Exception as e:
            logger.error(f"SSH命令执行失败: {e}", extra={"to_stdout": True})
            return False, str(e)

    def copy_file(self, src: str, dst: str) -> bool:
        """复制文件到远程"""
        # 规范化路径并检查
        src_path = Path(src).resolve()
        if not src_path.exists():
            logger.error(f"源文件不存在: {src}", extra={"to_stdout": True})
            return False

        if not dst or '..' in dst:
            logger.error("目标路径无效", extra={"to_stdout": True})
            return False

        # SCP使用-P而不是-p指定端口
        scp_options = [
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",  # 禁用交互式密码认证，密钥认证失败时直接失败
            "-P", str(self.port),
            "-i", self.key_path,
        ]
        if self.strict_host_key_checking:
            known_hosts_file = os.path.expanduser("~/.ssh/known_hosts")
            scp_options.extend([
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts_file}",
            ])
        else:
            scp_options.extend(["-o", "StrictHostKeyChecking=no"])

        scp_cmd = ["scp"] + scp_options + [str(src_path), f"{self.username}@{self.host}:{dst}"]
        try:
            result = run_command(scp_cmd, check=False, capture_output=False, timeout=1800, env=_env_for_system_subprocess())
            return result.returncode == 0
        except Exception as e:
            logger.error("复制文件到远程失败 {} -> {}: {}".format(src, dst, e), extra={"to_stdout": True})
            return False


class ConfigGenerator:
    """配置文件生成器"""

    @staticmethod
    def generate_fe_config(
            meta_dir: str,
            http_port: int = 8030,
            rpc_port: int = 9020,
            query_port: int = 9030,
            edit_log_port: int = 9010,
            priority_networks: Optional[str] = None,
            java_home: Optional[str] = None,
            default_replication_num: Optional[int] = None,
            java_opts: Optional[str] = None,
            extra_config: Optional[Dict[str, Any]] = None
    ) -> str:
        """生成FE配置文件内容"""
        config_lines = [
            "# StarRocks FE Configuration",
            "# Generated by starrocks_deploy.py",
            "",
            f"meta_dir = {meta_dir}",
            "",
            "# FE Ports",
            f"http_port = {http_port}",
            f"rpc_port = {rpc_port}",
            f"query_port = {query_port}",
            f"edit_log_port = {edit_log_port}",
            ""
        ]

        if priority_networks:
            config_lines.append(f"priority_networks = {priority_networks}")
            config_lines.append("")

        if java_home:
            config_lines.append(f"JAVA_HOME = {java_home}")
            config_lines.append("")

        if java_opts:
            config_lines.append("# JVM Options for JDK17+ compatibility")
            config_lines.append(f'JAVA_OPTS = "{java_opts}"')
            config_lines.append("")
        else:
            config_lines.append("# JVM Options for JDK17+ compatibility")
            config_lines.append(
                'JAVA_OPTS = "--add-opens=java.base/java.util=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED --add-opens=java.base/sun.nio.ch=ALL-UNNAMED"')
            config_lines.append("")

        if default_replication_num is not None:
            config_lines.append(f"default_replication_num = {default_replication_num}")
            config_lines.append("")

        if extra_config:
            config_lines.append("# Extra Configuration")
            for key, value in extra_config.items():
                config_lines.append(f"{key} = {value}")
            config_lines.append("")

        return "\n".join(config_lines)

    @staticmethod
    def generate_be_config(
            storage_root_path: str,
            be_port: int = 9060,
            be_http_port: int = 8040,
            heartbeat_service_port: int = 9050,
            brpc_port: int = 8060,
            starlet_port: int = 9070,
            priority_networks: Optional[str] = None,
            java_home: Optional[str] = None,
            sys_log_level: str = "INFO",
            java_opts: Optional[str] = None,
            extra_config: Optional[Dict[str, Any]] = None
    ) -> str:
        """生成BE配置文件内容"""
        config_lines = [
            "# StarRocks BE Configuration",
            "# Generated by starrocks_deploy.py",
            "",
            f"sys_log_level = {sys_log_level}",
            "",
            "# BE Ports",
            f"be_port = {be_port}",
            f"be_http_port = {be_http_port}",
            f"heartbeat_service_port = {heartbeat_service_port}",
            f"brpc_port = {brpc_port}",
            f"starlet_port = {starlet_port}",
            ""
        ]

        if priority_networks:
            config_lines.append(f"priority_networks = {priority_networks}")
            config_lines.append("")

        # 多磁盘存储路径配置
        config_lines.append("# Data Storage Paths (Multi-disk support)")
        config_lines.append("# Format: /path1,medium:HDD;/path2,medium:SSD;/path3")
        config_lines.append(f"storage_root_path = {storage_root_path}")
        config_lines.append("")

        if java_home:
            config_lines.append(f"JAVA_HOME = {java_home}")
            config_lines.append("")

        if java_opts:
            config_lines.append("# JVM Options for JDK17+ compatibility")
            config_lines.append(f'JAVA_OPTS = "{java_opts}"')
            config_lines.append("")
        else:
            # 默认JDK17兼容选项
            config_lines.append("# JVM Options for JDK17+ compatibility")
            config_lines.append(
                'JAVA_OPTS = "--add-opens=java.base/java.util=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED --add-opens=java.base/sun.nio.ch=ALL-UNNAMED"')
            config_lines.append("")

        if extra_config:
            config_lines.append("# Extra Configuration")
            for key, value in extra_config.items():
                config_lines.append(f"{key} = {value}")
            config_lines.append("")

        return "\n".join(config_lines)

    @staticmethod
    def generate_cn_config(
            be_port: int = 9060,
            be_http_port: int = 8040,
            heartbeat_service_port: int = 9050,
            brpc_port: int = 8060,
            priority_networks: Optional[str] = None,
            java_home: Optional[str] = None,
            sys_log_level: str = "INFO",
            java_opts: Optional[str] = None,
            extra_config: Optional[Dict[str, Any]] = None
    ) -> str:
        """生成CN配置文件内容"""
        config_lines = [
            "# StarRocks CN Configuration",
            "# Generated by starrocks_deploy.py",
            "",
            f"sys_log_level = {sys_log_level}",
            "",
            "# CN Ports",
            f"be_port = {be_port}",
            f"be_http_port = {be_http_port}",
            f"heartbeat_service_port = {heartbeat_service_port}",
            f"brpc_port = {brpc_port}",
            ""
        ]

        if priority_networks:
            config_lines.append(f"priority_networks = {priority_networks}")
            config_lines.append("")

        if java_home:
            config_lines.append(f"JAVA_HOME = {java_home}")
            config_lines.append("")

        if java_opts:
            config_lines.append("# JVM Options for JDK17+ compatibility")
            config_lines.append(f'JAVA_OPTS = "{java_opts}"')
            config_lines.append("")
        else:
            # 默认JDK17兼容选项
            config_lines.append("# JVM Options for JDK17+ compatibility")
            config_lines.append(
                'JAVA_OPTS = "--add-opens=java.base/java.util=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED --add-opens=java.base/sun.nio.ch=ALL-UNNAMED"')
            config_lines.append("")

        if extra_config:
            config_lines.append("# Extra Configuration")
            for key, value in extra_config.items():
                config_lines.append(f"{key} = {value}")
            config_lines.append("")

        return "\n".join(config_lines)


class SystemdServiceGenerator:
    """Systemd服务文件生成器"""

    @staticmethod
    def generate_fe_service(
            starrocks_home: str,
            user: str = "starrocks",
            group: str = "starrocks",
            description: str = "StarRocks FE Service",
            java_home: Optional[str] = None,
            helper_address: Optional[str] = None,
            use_fqdn: bool = False
    ) -> str:
        """生成FE systemd服务文件"""
        # 校验 helper_address 格式（如果提供）
        if helper_address:
            if ':' not in helper_address:
                raise ValueError(
                    f"无效的helper_address格式: {helper_address}，格式应为: host:port (例如: 192.168.1.100:9010)")
            helper_parts = helper_address.split(':', 1)
            if len(helper_parts) != 2:
                raise ValueError(
                    f"无效的helper_address格式: {helper_address}，格式应为: host:port (例如: 192.168.1.100:9010)")
            helper_host, helper_port_str = helper_parts
            if not InputValidator.validate_hostname(helper_host):
                raise ValueError(f"无效的helper_address主机地址: {helper_host}，必须是有效的IPv4地址或主机名")
            try:
                helper_port = int(helper_port_str)
                if not InputValidator.validate_port(helper_port):
                    raise ValueError(f"无效的helper_address端口号: {helper_port}，端口号必须在1-65535之间")
            except ValueError as e:
                if "无效的helper_address端口号" in str(e):
                    raise
                raise ValueError(f"无效的helper_address端口号: {helper_port_str}，必须是数字")

        env_lines = []
        final_java_home = java_home
        if not final_java_home:
            final_java_home = os.environ.get('JAVA_HOME')
        if final_java_home:
            env_lines.append(f"Environment=\"JAVA_HOME={final_java_home}\"")
        else:
            logger.warning("未设置JAVA_HOME，StarRocks可能无法正常启动", extra={"to_stdout": True})
        env_lines.append(f"Environment=\"STARROCKS_HOME={starrocks_home}\"")
        env_section = "\n".join(env_lines) if env_lines else ""

        # 构建启动命令
        # 注意：helper_address 已经是 host:port 格式，不需要再拼接 edit_log_port
        # 根据 StarRocks 官方文档，--helper 参数格式为: --helper "host:edit_log_port"
        if helper_address:
            exec_start = f"{starrocks_home}/fe/bin/start_fe.sh --helper {helper_address}"
        elif use_fqdn:
            exec_start = f"{starrocks_home}/fe/bin/start_fe.sh --host_type FQDN"
        else:
            exec_start = f"{starrocks_home}/fe/bin/start_fe.sh"

        service_content = f"""[Unit]
Description={description}
After=network.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={starrocks_home}
UMask=0022
ExecStart={exec_start}
ExecStop={starrocks_home}/fe/bin/stop_fe.sh
Restart=on-failure
RestartSec=10
TimeoutStartSec=600
TimeoutStopSec=120
KillMode=process
KillSignal=SIGTERM
{env_section}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=starrocks-fe

LimitNOFILE=65536
LimitNPROC=40960
LimitCORE=infinity

NoNewPrivileges=true
PrivateTmp=false

[Install]
WantedBy=multi-user.target
"""
        return service_content

    @staticmethod
    def generate_be_service(
            starrocks_home: str,
            user: str = "starrocks",
            group: str = "starrocks",
            description: str = "StarRocks BE Service",
            java_home: Optional[str] = None
    ) -> str:
        """生成BE systemd服务文件"""
        env_lines = []
        final_java_home = java_home
        if not final_java_home:
            final_java_home = os.environ.get('JAVA_HOME')
        if final_java_home:
            env_lines.append(f"Environment=\"JAVA_HOME={final_java_home}\"")
        else:
            logger.warning("未设置JAVA_HOME，StarRocks可能无法正常启动", extra={"to_stdout": True})
        env_lines.append(f"Environment=\"STARROCKS_HOME={starrocks_home}\"")
        env_section = "\n".join(env_lines) if env_lines else ""

        service_content = f"""[Unit]
Description={description}
After=network.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={starrocks_home}
UMask=0022
ExecStart={starrocks_home}/be/bin/start_be.sh
ExecStop={starrocks_home}/be/bin/stop_be.sh
Restart=on-failure
RestartSec=10
TimeoutStartSec=600
TimeoutStopSec=120
KillMode=process
KillSignal=SIGTERM
{env_section}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=starrocks-be

LimitNOFILE=65536
LimitNPROC=40960
LimitCORE=infinity

NoNewPrivileges=true
PrivateTmp=false

[Install]
WantedBy=multi-user.target
"""
        return service_content

    @staticmethod
    def generate_cn_service(
            starrocks_home: str,
            user: str = "starrocks",
            group: str = "starrocks",
            description: str = "StarRocks CN Service",
            java_home: Optional[str] = None
    ) -> str:
        """生成CN systemd服务文件"""
        env_lines = []
        final_java_home = java_home
        if not final_java_home:
            final_java_home = os.environ.get('JAVA_HOME')
        if final_java_home:
            env_lines.append(f"Environment=\"JAVA_HOME={final_java_home}\"")
        else:
            logger.warning("未设置JAVA_HOME，StarRocks可能无法正常启动", extra={"to_stdout": True})
        env_lines.append(f"Environment=\"STARROCKS_HOME={starrocks_home}\"")
        env_section = "\n".join(env_lines) if env_lines else ""

        service_content = f"""[Unit]
Description={description}
After=network.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={starrocks_home}
UMask=0022
ExecStart={starrocks_home}/be/bin/start_cn.sh
ExecStop={starrocks_home}/be/bin/stop_cn.sh
Restart=on-failure
RestartSec=10
TimeoutStartSec=600
TimeoutStopSec=120
KillMode=process
KillSignal=SIGTERM
{env_section}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=starrocks-cn

LimitNOFILE=65536
LimitNPROC=40960
LimitCORE=infinity

NoNewPrivileges=true
PrivateTmp=false

[Install]
WantedBy=multi-user.target
"""
        return service_content


class StarRocksDeployer:
    """StarRocks部署器"""

    # 节点类型到服务名称的映射
    SERVICE_NAMES = {
        'fe': 'starrocks-fe',
        'be': 'starrocks-be',
        'cn': 'starrocks-cn'
    }

    # 节点类型到配置文件路径的映射
    CONFIG_PATHS = {
        'fe': lambda home: home / "fe" / "conf" / "fe.conf",
        'be': lambda home: home / "be" / "conf" / "be.conf",
        'cn': lambda home: home / "be" / "conf" / "cn.conf"
    }

    # 节点类型到PID文件路径的映射
    PID_FILES = {
        'fe': lambda home: home / "fe" / "fe.pid",
        'be': lambda home: home / "be" / "be.pid",
        'cn': lambda home: home / "be" / "cn.pid"
    }

    # 节点类型到日志目录路径的映射
    LOG_DIRS = {
        'fe': lambda home: home / "fe" / "log",
        'be': lambda home: home / "be" / "log",
        'cn': lambda home: home / "be" / "log"
    }

    def __init__(self, starrocks_home: str, user: str = "starrocks", group: str = "starrocks"):
        self.starrocks_home = Path(starrocks_home).resolve()
        self.user = user
        self.group = group

        if not self.starrocks_home.exists():
            raise ValueError(f"StarRocks安装目录不存在: {starrocks_home}")

    def check_environment(self) -> Tuple[bool, List[str]]:
        """检查部署环境"""
        errors = []

        # 检查Java（StarRocks 3.5+需要Java 17+）
        java_ok, java_home, java_version = EnvironmentChecker.check_java()
        if not java_ok:
            if java_version:
                errors.append(f"Java版本过低: {java_version}，StarRocks 3.5+需要Java 17或更高版本")
            else:
                errors.append("未找到Java环境，请先安装JDK 17或更高版本")
        elif java_version:
            logger.info(f"检测到Java版本: {java_version}", extra={"to_stdout": True})

        # 检查目录权限
        if not EnvironmentChecker.check_directory_writable(str(self.starrocks_home)):
            errors.append(f"StarRocks目录不可写: {self.starrocks_home}")

        # 检查用户和组是否存在
        user_exists, group_exists = EnvironmentChecker.check_user_group_exists(self.user, self.group)
        if not user_exists:
            errors.append(f"用户不存在: {self.user}，请先创建用户或使用现有用户")
        if not group_exists:
            errors.append(f"组不存在: {self.group}，请先创建组或使用现有组")

        # 检查必要的目录结构
        fe_dir = self.starrocks_home / "fe"
        be_dir = self.starrocks_home / "be"
        if not fe_dir.exists():
            errors.append(f"FE目录不存在: {fe_dir}，请确保StarRocks已正确安装")
        if not be_dir.exists():
            errors.append(f"BE目录不存在: {be_dir}，请确保StarRocks已正确安装")

        return len(errors) == 0, errors

    def _check_service_exists(self, service_name: str) -> bool:
        """检查systemd服务是否存在（跨发行版更可靠）"""
        try:
            # 优先使用 systemctl show 的 LoadState，跨发行版一致
            show_result = run_command(
                ['systemctl', 'show', service_name, '-p', 'LoadState', '--value'],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 3, 4],
                timeout=10
            )
            load_state = (show_result.stdout or "").strip().lower()
            if load_state == "not-found":
                return False
            if load_state in ("loaded", "masked", "stub", "generated", "bad"):
                return True

            # 回退：使用 systemctl status 检查服务是否存在（即使服务未运行也会返回信息）
            result = run_command(
                ['systemctl', 'status', service_name, '--no-pager'],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 2, 3, 4],  # 0=运行中, 1=未运行, 2=未找到, 3=已停止, 4=其他错误
                timeout=10
            )
            # systemctl status: 2/4 常见为“未找到”/“不存在”
            combined_output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            if result.returncode in (2, 4) or "not-found" in combined_output or "could not be found" in combined_output:
                return False
            # 其他返回码说明服务文件存在（无论是否运行）
            return True
        except Exception as e:
            logger.debug(f"检查systemd服务是否存在失败: {e}")
            # 如果检查失败，尝试检查服务文件是否存在
            service_path = Path(f"/etc/systemd/system/{service_name}.service")
            return service_path.exists()

    def _is_service_active(self, service_name: str) -> bool:
        """检查systemd服务是否正在运行"""
        try:
            result = run_command(
                ['systemctl', 'is-active', service_name],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 3],
                timeout=10
            )
            return result.returncode == 0 and result.stdout.strip() == 'active'
        except Exception as e:
            logger.debug(f"检查systemd服务状态失败: {e}")
            return False

    def _is_service_loaded(self, service_name: str) -> bool:
        """检查systemd服务是否已加载（服务文件存在且已加载到systemd）"""
        try:
            # 优先使用 systemctl show 的 LoadState
            show_result = run_command(
                ['systemctl', 'show', service_name, '-p', 'LoadState', '--value'],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 3, 4],
                timeout=10
            )
            load_state = (show_result.stdout or "").strip().lower()
            if load_state == "not-found":
                return False
            if load_state in ("loaded", "masked", "stub", "generated", "bad"):
                return True

            result = run_command(
                ['systemctl', 'is-enabled', service_name],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1],
                timeout=10
            )
            # 返回码0表示已启用
            if result.returncode == 0:
                return True
            combined_output = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            # systemctl is-enabled 对不存在的服务可能返回 "not-found"/"disabled"
            if "not-found" in combined_output or "could not be found" in combined_output:
                return False
            # 如果返回"disabled"或"static"，说明服务文件存在但未启用
            if "disabled" in combined_output or "static" in combined_output:
                return True
            return False
        except Exception as e:
            logger.debug(f"检查systemd服务是否已加载失败: {e}")
            return False

    def _stop_and_disable_service(self, service_name: str, force: bool = True) -> bool:
        """
        停止并禁用systemd服务
        
        Args:
            service_name: 服务名称
            force: 如果正常停止失败，是否强制kill进程
        
        Returns:
            bool: 成功返回True
        """
        try:
            # 检查服务是否存在
            service_exists = self._check_service_exists(service_name)
            service_loaded = self._is_service_loaded(service_name)

            if not service_exists and not service_loaded:
                logger.debug(f"服务 {service_name} 不存在，跳过停止操作")
                return True

            # 1. 停止服务（优雅停止）
            if self._is_service_active(service_name):
                logger.info(f"正在停止服务: {service_name}", extra={"to_stdout": True})
                result = run_command(
                    ['systemctl', 'stop', service_name],
                    check=False,
                    capture_output=True,
                    allowed_exit_codes=[0, 1, 5],
                    timeout=60
                )
                if result.returncode != 0:
                    logger.warning(
                        f"停止服务命令返回非0: {service_name}, code={result.returncode}, stderr={result.stderr.strip() if result.stderr else '(empty)'}",
                        extra={"to_stdout": True}
                    )

                # 等待服务停止（最多等待10秒）
                max_wait = 10
                waited = 0
                while waited < max_wait and self._is_service_active(service_name):
                    time.sleep(1)
                    waited += 1

                # 如果服务仍在运行且force=True，强制kill
                if self._is_service_active(service_name) and force:
                    logger.warning(f"服务 {service_name} 未能正常停止，尝试强制终止...", extra={"to_stdout": True})
                    self._kill_service_processes(service_name)
                    time.sleep(2)

            # 2. 禁用服务（防止开机自启）
            if service_loaded:
                logger.info(f"正在禁用服务: {service_name}", extra={"to_stdout": True})
                run_command(
                    ['systemctl', 'disable', service_name],
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=30
                )

            # 3. 重置服务状态（清除失败状态）
            run_command(
                ['systemctl', 'reset-failed', service_name],
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=10
            )

            logger.info(f"✓ 服务 {service_name} 已停止并禁用", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error(f"停止并禁用服务失败: {e}", extra={"to_stdout": True})
            # 即使失败，也尝试强制kill进程
            if force:
                logger.warning("尝试强制终止服务进程...", extra={"to_stdout": True})
                self._kill_service_processes(service_name)
            return False

    def _kill_service_processes(self, service_name: str) -> bool:
        """强制kill服务相关的所有进程"""
        try:
            # 获取服务的主进程PID
            result = run_command(
                ['systemctl', 'show', service_name, '--property=MainPID', '--value'],
                check=False,
                capture_output=True,
                timeout=10
            )

            if result.returncode == 0:
                pid_str = result.stdout.strip()
                if pid_str and pid_str.isdigit():
                    pid = int(pid_str)
                    if pid > 1:  # 确保不是init进程
                        logger.info(f"正在kill主进程 PID: {pid}", extra={"to_stdout": True})
                        # 先尝试SIGTERM
                        run_command(['kill', '-TERM', str(pid)], check=False, timeout=5)
                        time.sleep(2)
                        # 检查进程是否还存在
                        check_result = run_command(
                            ['kill', '-0', str(pid)],
                            check=False,
                            timeout=5
                        )
                        if check_result.returncode == 0:
                            # 进程仍在运行，使用SIGKILL
                            logger.warning(f"进程 {pid} 未响应SIGTERM，使用SIGKILL强制终止", extra={"to_stdout": True})
                            run_command(['kill', '-KILL', str(pid)], check=False, timeout=5)

            # 额外检查：根据服务名称查找相关进程
            process_patterns = {
                'starrocks-fe': ['start_fe.sh', 'fe.jar', 'org.apache.doris'],
                'starrocks-be': ['start_be.sh', 'starrocks_be', '--be'],
                'starrocks-cn': ['start_cn.sh', 'starrocks_be', '--cn']
            }

            patterns = process_patterns.get(service_name, [])
            for pattern in patterns:
                # 使用pgrep查找进程
                pgrep_result = run_command(
                    ['pgrep', '-f', pattern],
                    check=False,
                    capture_output=True,
                    timeout=10
                )
                if pgrep_result.returncode == 0:
                    pids = pgrep_result.stdout.strip().split('\n')
                    for pid_str in pids:
                        if pid_str.strip().isdigit():
                            pid = int(pid_str.strip())
                            if pid > 1:
                                logger.info(f"正在kill相关进程 PID: {pid} (匹配: {pattern})", extra={"to_stdout": True})
                                run_command(['kill', '-KILL', str(pid)], check=False, timeout=5)

            return True
        except Exception as e:
            logger.error(f"强制终止服务进程失败: {e}", extra={"to_stdout": True})
            return False

    def _remove_service_file(self, service_path: Path) -> bool:
        """删除systemd服务文件"""
        try:
            if service_path.exists():
                service_path.unlink()
                logger.info(f"✓ 已删除服务文件: {service_path}", extra={"to_stdout": True})
                # 重新加载systemd
                run_command(
                    ['systemctl', 'daemon-reload'],
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=30
                )
                return True
            else:
                logger.info(f"服务文件不存在: {service_path}，跳过删除", extra={"to_stdout": True})
                return True
        except Exception as e:
            logger.error("删除服务文件失败: {}".format(e), extra={"to_stdout": True})
            return False

    def _cleanup_pid_file(self, pid_file: Path) -> bool:
        """清理PID文件"""
        try:
            if pid_file.exists():
                pid_file.unlink()
                logger.info(f"✓ 已清理PID文件: {pid_file}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error("清理PID文件失败: {}".format(e), extra={"to_stdout": True})
            return False

    def clean_deployment(self, node_type: str, backup_config: bool = True, fe_host: Optional[str] = None,
                         fe_query_port: int = 9030, password: Optional[str] = None) -> bool:
        """
        清理已部署的服务和配置
        
        设计原则：
        - FE节点：先从集群移除（ALTER SYSTEM DROP FOLLOWER），然后清理本地服务
        - BE/CN节点：先检查是否在集群中，在集群中则必须提供fe_host进行移除
        
        Args:
            node_type: 节点类型 [fe|be|cn]
            backup_config: 是否备份配置文件（默认True）
            fe_host: FE主机地址（FE/BE/CN节点在集群中时必须提供）
            fe_query_port: FE查询端口（默认9030）
            password: 密码
        
        Returns:
            bool: 清理成功返回True
        
        Raises:
            ValueError: BE/CN节点在集群中但未提供fe_host时抛出
        """
        logger.info(f"=== 开始清理 {node_type.upper()} 节点部署 ===", extra={"to_stdout": True})

        node_type_lower = node_type.lower()
        node_type_upper = node_type.upper()
        service_name = self.SERVICE_NAMES.get(node_type_lower)
        if not service_name:
            logger.error(f"无效的节点类型: {node_type}", extra={"to_stdout": True})
            return False

        service_path = Path(f"/etc/systemd/system/{service_name}.service")

        # FE节点从集群移除（必须在清理本地服务前执行）
        if node_type_lower == 'fe':
            if not self._check_fe_cleanup_safety(fe_host, fe_query_port, password):
                return False
            
            # 从集群移除FE节点（如果提供fe_host）
            if fe_host:
                if not self._remove_fe_from_cluster_before_cleanup(fe_host, fe_query_port, password):
                    logger.warning(
                        "从集群移除FE节点失败，但继续清理本地服务（节点可能不在集群中）",
                        extra={"to_stdout": True}
                    )
            else:
                logger.warning(
                    "未提供 --fe-host，无法从集群移除FE节点，将只清理本地服务",
                    extra={"to_stdout": True}
                )
                logger.warning(
                    "建议：如果节点在集群中，请提供 --fe-host 参数以确保正确从集群移除",
                    extra={"to_stdout": True}
                )

        # BE/CN节点从集群移除
        if node_type_lower in ['be', 'cn']:
            if not fe_host:
                logger.warning(f"{node_type_upper} 节点清理需要 --fe-host 以确认集群状态", extra={"to_stdout": True})
                raise ValueError(f"{node_type_upper} 节点清理需要 --fe-host 以确认集群状态")

            if not self._remove_be_cn_from_cluster_before_cleanup(node_type, fe_host, fe_query_port, password):
                return False

        # 清理服务、配置文件、PID、日志和临时文件
        self._cleanup_service_and_files(node_type_lower, service_name, service_path, backup_config)

        logger.info(f"=== {node_type_upper} 节点清理完成 ===", extra={"to_stdout": True})
        return True

    def _remove_node_from_cluster(self, fe_host: str, fe_query_port: int, node_ip: str, node_port: int, node_type: str,
                                  force: bool = False, password: Optional[str] = None) -> bool:
        """从集群中移除节点
        
        Args:
            fe_host: FE主机地址
            fe_query_port: FE查询端口
            node_ip: 节点IP
            node_port: 节点端口
            node_type: 节点类型 [be|cn]
            force: 是否强制删除（遇到单副本表等错误时使用）
        
        Returns:
            bool: 移除成功返回True
        """
        try:
            mysql_client = shutil.which('mysql')
            if not mysql_client:
                logger.debug("未找到mysql客户端，跳过从集群移除节点")
                return False

            if node_type.lower() == "be":
                sql = f'ALTER SYSTEM DROP BACKEND "{node_ip}:{node_port}";'
                force_sql = f'ALTER SYSTEM DROP BACKEND "{node_ip}:{node_port}" FORCE;'
            else:  # CN
                sql = f'ALTER SYSTEM DROP COMPUTE NODE "{node_ip}:{node_port}";'
                force_sql = f'ALTER SYSTEM DROP COMPUTE NODE "{node_ip}:{node_port}" FORCE;'

            logger.info(f"正在从集群移除 {node_type.upper()} 节点: {node_ip}:{node_port}", extra={"to_stdout": True})
            cmd = [mysql_client, '-h', fe_host, '-P', str(fe_query_port), '-uroot', '-e', sql]
            if password:
                env = os.environ.copy()
                env['MYSQL_PWD'] = password
                result = run_command(cmd, check=False, capture_output=True, timeout=30, env=env)
            else:
                result = run_command(cmd, check=False, capture_output=True, timeout=30)

            if result.returncode == 0:
                logger.info(f"✓ 成功从集群移除 {node_type.upper()} 节点", extra={"to_stdout": True})
                return True
            else:
                # 节点可能不存在于集群中，不算错误
                if "does not exist" in result.stderr or "not found" in result.stderr:
                    logger.debug(f"{node_type.upper()} 节点不在集群中，跳过移除")
                    return True

                # 检查是否需要FORCE删除（单副本表等错误）
                if force or ("only one replica" in result.stderr or "FORCE" in result.stderr):
                    if not force:
                        logger.warning(f"检测到需要强制删除: {result.stderr[:200]}", extra={"to_stdout": True})
                        logger.info("尝试使用 FORCE 强制删除节点...", extra={"to_stdout": True})

                    cmd_force = [mysql_client, '-h', fe_host, '-P', str(fe_query_port), '-uroot', '-e', force_sql]
                    if password:
                        env_force = os.environ.copy()
                        env_force['MYSQL_PWD'] = password
                        result_force = run_command(cmd_force, check=False, capture_output=True, timeout=30,
                                                   env=env_force)
                    else:
                        result_force = run_command(cmd_force, check=False, capture_output=True, timeout=30)

                    if result_force.returncode == 0:
                        logger.info(f"✓ 成功强制从集群移除 {node_type.upper()} 节点", extra={"to_stdout": True})
                        return True
                    else:
                        logger.error(f"强制删除节点失败: {result_force.stderr}", extra={"to_stdout": True})
                        return False
                else:
                    logger.warning(f"从集群移除节点失败: {result.stderr}", extra={"to_stdout": True})
                    return False
        except Exception as e:
            logger.error(f"从集群移除节点时发生异常: {e}", extra={"to_stdout": True})
            return False

    def _check_fe_cleanup_safety(self, fe_host: Optional[str], fe_query_port: int, password: Optional[str]) -> bool:
        """
        检查FE清理前的安全性（确保集群中没有BE/CN节点）
        
        Returns:
            如果安全可以清理返回True，否则返回False
        """
        if not fe_host:
            logger.error(
                "清理FE节点时必须提供 --fe-host 以检查集群状态",
                extra={"to_stdout": True}
            )
            logger.error(
                "原因：清理FE前必须检查集群中是否还有BE/CN节点，避免导致集群状态不一致",
                extra={"to_stdout": True}
            )
            logger.error(
                "操作：请先清理所有BE/CN节点，或提供 --fe-host 参数以验证集群状态",
                extra={"to_stdout": True}
            )
            return False

        # 检查BE节点
        be_sql = "SHOW BACKENDS;"
        be_success, be_output = self._execute_sql(fe_host, fe_query_port, be_sql, password=password)
        if not be_success:
            logger.error(
                f"检查BE节点状态失败: {be_output}，无法确认集群状态，拒绝清理FE节点",
                extra={"to_stdout": True}
            )
            return False

        be_count = 0
        if be_output:
            be_nodes = self._parse_node_list_output(be_output, "BE")
            be_count = len(be_nodes)

        # 检查CN节点
        cn_sql = "SHOW COMPUTE NODES;"
        cn_success, cn_output = self._execute_sql(fe_host, fe_query_port, cn_sql, password=password)
        if not cn_success:
            logger.error(
                f"检查CN节点状态失败: {cn_output}，无法确认集群状态，拒绝清理FE节点",
                extra={"to_stdout": True}
            )
            return False

        cn_count = 0
        if cn_output:
            cn_nodes = self._parse_node_list_output(cn_output, "CN")
            cn_count = len(cn_nodes)

        logger.debug(
            f"BE节点检查: be_success={be_success}, be_count={be_count}, output_preview={be_output[:200] if be_output else 'None'}")
        logger.debug(
            f"CN节点检查: cn_success={cn_success}, cn_count={cn_count}, output_preview={cn_output[:200] if cn_output else 'None'}")

        if be_count > 0 or cn_count > 0:
            logger.error(
                f"检测到集群中仍有活跃节点，禁止清理FE节点（可能导致集群状态不一致）",
                extra={"to_stdout": True}
            )
            if be_count > 0:
                logger.error(f"  - 集群中还有 {be_count} 个BE节点", extra={"to_stdout": True})
            if cn_count > 0:
                logger.error(f"  - 集群中还有 {cn_count} 个CN节点", extra={"to_stdout": True})
            logger.error("建议操作顺序：", extra={"to_stdout": True})
            logger.error("  1. 先清理所有BE/CN节点: starcli --clean --deploy be/cn --fe-host <FE_HOST>",
                         extra={"to_stdout": True})
            logger.error("  2. 再清理FE节点: starcli --clean --deploy fe --fe-host <FE_HOST>",
                         extra={"to_stdout": True})
            logger.error("如需强制清理（风险自负），请先手动清理所有BE/CN节点后再执行", extra={"to_stdout": True})
            return False

        return True

    def _remove_fe_from_cluster_before_cleanup(
            self,
            fe_host: str,
            fe_query_port: int,
            password: Optional[str]
    ) -> bool:
        """
        清理FE节点前，先从集群移除
        
        Returns:
            是否成功
        """
        # 获取本机IP地址和edit_log_port
        node_ip = None
        edit_log_port = DEFAULT_FE_PORTS['edit_log_port']
        
        try:
            node_ip = self._get_local_ip(fe_host, fe_query_port)
        except Exception as e:
            logger.error(f"获取本机IP失败: {e}", extra={"to_stdout": True})

        if not node_ip or node_ip == "127.0.0.1":
            logger.error("无法获取本机有效IPv4地址，无法完成集群校验", extra={"to_stdout": True})
            return False

        # 尝试从配置文件中读取edit_log_port
        fe_conf_path = self.starrocks_home / "fe" / "conf" / "fe.conf"
        if fe_conf_path.exists():
            config_port = self._parse_port_from_config(fe_conf_path, "edit_log_port")
            if config_port:
                edit_log_port = config_port

        # 检查节点是否在集群中
        sql = "SHOW FRONTENDS;"
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password=password)
        if not success:
            logger.error(f"检查FE节点是否在集群中失败: {output}", extra={"to_stdout": True})
            return False

        node_in_cluster = False
        if output:
            for line in output.splitlines():
                line = line.strip()
                if not line or ("IP" in line and "EditLogPort" in line):
                    continue
                try:
                    tokens = re.split(r"\s+", line)
                    if len(tokens) >= 4:
                        fe_ip = tokens[2] if len(tokens) > 2 else ""
                        fe_port = tokens[3] if len(tokens) > 3 else ""
                        if fe_ip == node_ip and fe_port == str(edit_log_port):
                            node_in_cluster = True
                            break
                except Exception as e:
                    logger.debug(f"解析FE节点行失败: {e}")

        # 如果节点在集群中，先从集群移除
        if node_in_cluster:
            logger.info(f"检测到FE节点 ({node_ip}:{edit_log_port}) 在集群中，将从集群移除",
                        extra={"to_stdout": True})
            if not self._remove_fe_follower_from_cluster(fe_host, fe_query_port, node_ip, edit_log_port,
                                                          password=password):
                logger.error(f"从集群移除FE节点失败，清理操作已中止", extra={"to_stdout": True})
                return False
            logger.info(f"✓ 已从集群移除FE节点", extra={"to_stdout": True})
        else:
            logger.info(f"节点 ({node_ip}:{edit_log_port}) 不在集群中，直接清理本地服务", extra={"to_stdout": True})

        return True

    def _remove_be_cn_from_cluster_before_cleanup(
            self,
            node_type: str,
            fe_host: str,
            fe_query_port: int,
            password: Optional[str]
    ) -> bool:
        """
        清理BE/CN节点前，先从集群移除
        
        Returns:
            是否成功
        """
        node_type_upper = node_type.upper()

        # 获取本机IP地址
        node_ip = None
        try:
            node_ip = self._get_local_ip(fe_host, fe_query_port)
        except Exception as e:
            logger.error(f"获取本机IP失败: {e}", extra={"to_stdout": True})

        if not node_ip or node_ip == "127.0.0.1":
            logger.error("无法获取本机有效IPv4地址，无法完成集群校验", extra={"to_stdout": True})
            return False

        # 检查节点是否在集群中
        heartbeat_port = DEFAULT_BE_PORTS['heartbeat_service_port'] if node_type.lower() == 'be' else DEFAULT_CN_PORTS[
            'heartbeat_service_port']
        sql = "SHOW BACKENDS;" if node_type_upper == "BE" else "SHOW COMPUTE NODES;"
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password=password)
        if not success:
            logger.error(f"检查节点是否在集群中失败: {output}", extra={"to_stdout": True})
            return False

        nodes = self._parse_node_list_output(output, node_type_upper)
        node_in_cluster = any(node["ip"] == node_ip for node in nodes)

        # 如果节点在集群中，先从集群移除
        if node_in_cluster:
            logger.info(f"检测到 {node_type_upper} 节点 ({node_ip}:{heartbeat_port}) 在集群中，将从集群移除",
                        extra={"to_stdout": True})
            if not self._remove_node_from_cluster(fe_host, fe_query_port, node_ip, heartbeat_port, node_type,
                                                  force=True, password=password):
                logger.error(f"从集群移除 {node_type_upper} 节点失败，清理操作已中止", extra={"to_stdout": True})
                return False
            logger.info(f"✓ 已从集群移除 {node_type_upper} 节点", extra={"to_stdout": True})
        else:
            logger.info(f"节点 ({node_ip}:{heartbeat_port}) 不在集群中，直接清理本地服务", extra={"to_stdout": True})

        return True

    def _cleanup_service_and_files(
            self,
            node_type: str,
            service_name: str,
            service_path: Path,
            backup_config: bool
    ) -> None:
        """
        清理服务、配置文件、PID文件、日志文件和临时文件
        
        Args:
            node_type: 节点类型 ('fe', 'be', 'cn')
            service_name: systemd服务名称
            service_path: systemd服务文件路径
            backup_config: 是否备份配置文件
        """
        node_type_lower = node_type.lower()

        # 停止服务
        logger.info("正在停止服务...", extra={"to_stdout": True})
        self._stop_and_disable_service(service_name, force=True)
        self._kill_service_processes(service_name)
        time.sleep(1)

        # 删除服务文件
        logger.info("正在删除服务文件...", extra={"to_stdout": True})
        self._remove_service_file(service_path)

        # 备份并删除配置文件
        logger.info("正在处理配置文件...", extra={"to_stdout": True})
        config_path_func = self.CONFIG_PATHS.get(node_type_lower)
        config_path = config_path_func(self.starrocks_home) if config_path_func else None
        if config_path and config_path.exists():
            if backup_config:
                try:
                    backup_path = config_path.with_suffix(f".conf.backup.{int(time.time())}")
                    import shutil
                    shutil.copy2(config_path, backup_path)
                    logger.info(f"✓ 配置文件已备份: {backup_path}", extra={"to_stdout": True})
                except Exception as e:
                    logger.debug(f"备份配置文件失败: {e}")
            try:
                config_path.unlink()
                logger.info(f"✓ 已删除配置文件: {config_path}", extra={"to_stdout": True})
            except Exception as e:
                logger.debug(f"删除配置文件失败: {e}")

        # 清理PID文件
        logger.info("正在清理PID文件...", extra={"to_stdout": True})
        pid_file_func = self.PID_FILES.get(node_type_lower)
        pid_file = pid_file_func(self.starrocks_home) if pid_file_func else None
        if pid_file:
            self._cleanup_pid_file(pid_file)

        # 清理日志文件
        logger.info("正在清理日志文件...", extra={"to_stdout": True})
        log_dir_func = self.LOG_DIRS.get(node_type_lower)
        log_dir = log_dir_func(self.starrocks_home) if log_dir_func else None
        if log_dir:
            self._cleanup_log_files(log_dir, days_to_keep=0)

        # 清理临时文件
        logger.info("正在清理临时文件...", extra={"to_stdout": True})
        self._cleanup_temp_files(node_type.lower())

    def _cleanup_log_files(self, log_dir: Path, days_to_keep: int = 7) -> None:
        """清理日志文件（保留指定天数的日志）
        
        Args:
            log_dir: 日志目录路径
            days_to_keep: 保留日志的天数（默认7天，0表示删除所有日志）
        """
        try:
            if not log_dir.exists():
                return

            log_files = list(log_dir.glob("*.log*"))
            if not log_files:
                return

            cleaned_count = 0

            if days_to_keep == 0:
                # 删除所有日志文件
                for log_file in log_files:
                    try:
                        log_file.unlink()
                        cleaned_count += 1
                    except Exception as e:
                        logger.debug(f"删除日志文件 {log_file} 失败: {e}")
                if cleaned_count > 0:
                    logger.info(f"✓ 已清理 {cleaned_count} 个日志文件", extra={"to_stdout": True})
            else:
                # 基于时间清理
                import time
                current_time = time.time()
                cutoff_time = current_time - (days_to_keep * 24 * 60 * 60)

                for log_file in log_files:
                    try:
                        if log_file.stat().st_mtime < cutoff_time:
                            log_file.unlink()
                            cleaned_count += 1
                    except Exception as e:
                        logger.debug(f"删除日志文件 {log_file} 失败: {e}")

                if cleaned_count > 0:
                    logger.info(f"✓ 已清理 {cleaned_count} 个旧日志文件（保留最近{days_to_keep}天）",
                                extra={"to_stdout": True})
        except Exception as e:
            logger.debug(f"清理日志文件失败: {e}")

    def _cleanup_temp_files(self, node_type: str) -> None:
        """清理临时文件和缓存"""
        try:
            temp_dirs = {
                'fe': self.starrocks_home / "fe" / "temp",
                'be': self.starrocks_home / "be" / "temp",
                'cn': self.starrocks_home / "be" / "temp"
            }
            temp_dir = temp_dirs.get(node_type.lower())

            if temp_dir and temp_dir.exists():
                import shutil
                try:
                    shutil.rmtree(temp_dir)
                    logger.info(f"✓ 已清理临时目录: {temp_dir}", extra={"to_stdout": True})
                except Exception as e:
                    logger.debug(f"清理临时目录失败: {e}")
        except Exception as e:
            logger.debug(f"清理临时文件失败: {e}")

    def check_existing_deployment(self, node_type: str) -> Tuple[bool, List[str]]:
        """检查是否已存在部署
        
        Returns:
            Tuple[bool, List[str]]: (是否存在, 详细信息列表)
        """
        service_names = {
            'fe': 'starrocks-fe',
            'be': 'starrocks-be',
            'cn': 'starrocks-cn'
        }
        service_name = service_names.get(node_type.lower())
        if not service_name:
            return False, []

        details = []
        exists = False

        # 检查systemd服务
        if self._check_service_exists(service_name):
            exists = True
            is_active = self._is_service_active(service_name)
            status = "运行中" if is_active else "已停止"
            details.append(f"Systemd服务 {service_name} 已存在 ({status})")

        node_type_lower = node_type.lower()

        # 运行态或明确残留信号判断（FE/BE/CN统一）
        pid_paths = {
            'fe': self.starrocks_home / "fe" / "fe.pid",
            'be': self.starrocks_home / "be" / "be.pid",
            'cn': self.starrocks_home / "be" / "cn.pid"
        }
        pid_file = pid_paths.get(node_type_lower)
        if pid_file and pid_file.exists():
            exists = True
            details.append(f"PID文件已存在: {pid_file}")

        process_patterns = {
            'fe': ['start_fe.sh', 'fe.jar', 'org.apache.doris'],
            'be': ['start_be.sh', 'starrocks_be', '--be'],
            'cn': ['start_cn.sh', 'starrocks_be', '--cn']
        }
        for pattern in process_patterns.get(node_type_lower, []):
            try:
                pgrep_result = run_command(
                    ['pgrep', '-f', pattern],
                    check=False,
                    capture_output=True,
                    timeout=5
                )
                if pgrep_result.returncode == 0 and pgrep_result.stdout.strip():
                    exists = True
                    details.append(f"检测到运行进程匹配: {pattern}")
            except Exception as e:
                logger.debug(f"进程检测失败: {e}")

        return exists, details

    def _parse_port_from_config(self, config_path: Path, key: str) -> Optional[int]:
        """从配置文件中解析端口值（形如 key = 1234）"""
        if not config_path.exists():
            return None
        try:
            pattern = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(\d+)\s*$")
            for line in config_path.read_text(encoding='utf-8', errors='ignore').splitlines():
                match = pattern.match(line)
                if match:
                    return int(match.group(1))
        except Exception as e:
            logger.debug(f"解析端口配置失败: {e}")
        return None

    def _get_role_ports(self, role: str) -> List[int]:
        """获取角色使用的端口列表（优先读取配置文件，其次使用默认值）"""
        role = role.lower()
        ports: List[int] = []
        if role == "be":
            config_path = self.starrocks_home / "be" / "conf" / "be.conf"
            defaults = DEFAULT_BE_PORTS
            keys = [
                ("be_port", "be_port"),
                ("be_http_port", "be_http_port"),
                ("heartbeat_service_port", "heartbeat_service_port"),
                ("brpc_port", "brpc_port"),
                ("starlet_port", "starlet_port")
            ]
        else:
            config_path = self.starrocks_home / "be" / "conf" / "cn.conf"
            defaults = DEFAULT_CN_PORTS
            keys = [
                ("be_port", "be_port"),
                ("be_http_port", "be_http_port"),
                ("heartbeat_service_port", "heartbeat_service_port"),
                ("brpc_port", "brpc_port")
            ]

        for conf_key, default_key in keys:
            port = self._parse_port_from_config(config_path, conf_key)
            ports.append(port if port is not None else defaults[default_key])
        return ports

    def _detect_local_role_conflict(self, role: str) -> Tuple[bool, List[str], List[str]]:
        """检测本地是否已部署指定角色（多信号判断，严格禁止同机共存）
        
        注意：此方法用于检测不同角色之间的冲突（BE vs CN），
        不用于检测同一角色的重复部署（幂等性由其他逻辑处理）
        """
        role = role.lower()
        details: List[str] = []
        warnings: List[str] = []
        conflict = False

        # 检查systemd服务（最可靠的信号）
        service_name = f"starrocks-{role}"
        service_exists = self._check_service_exists(service_name)
        service_loaded = self._is_service_loaded(service_name)
        service_active = self._is_service_active(service_name)
        if service_exists or service_loaded:
            if service_active:
                # 服务正在运行，这是真正的冲突
                conflict = True
                details.append(f"Systemd服务 {service_name} 正在运行")
            else:
                # 服务存在但未运行，只是残留，添加到warnings
                warnings.append(f"Systemd服务 {service_name} 已存在但未运行")

        # 注意：不检查配置文件，因为BE和CN的配置文件（be.conf/cn.conf）都在同一个目录下
        # 官方包自带这些配置文件，配置文件存在不代表服务在运行
        # 真正的冲突判断应该基于：服务状态、进程状态、端口占用

        # 检查PID文件
        pid_file = self.starrocks_home / "be" / f"{role}.pid"
        if pid_file.exists():
            warnings.append(f"PID文件存在: {pid_file}")
            try:
                pid = int(pid_file.read_text(encoding='utf-8', errors='ignore').strip())
                if pid > 1:
                    try:
                        os.kill(pid, 0)
                        details.append(f"PID进程存在: {pid}")
                        conflict = True
                    except Exception as ex:
                        logger.debug(f"PID进程不存在或无权限: {pid}: {ex}")
                        warnings.append(f"PID进程不存在或无权限: {pid}")
            except Exception as e:
                logger.debug(f"解析PID文件失败: {e}")

        # 进程级检查（通过启动脚本区分BE和CN，避免误判）
        # BE和CN都使用starrocks_be二进制，但启动脚本不同
        if role == "be":
            # 检测CN是否在运行：查找start_cn.sh或--cn参数
            cn_patterns = ["start_cn.sh", "--cn"]
            for pattern in cn_patterns:
                try:
                    pgrep_result = run_command(
                        ['pgrep', '-f', pattern],
                        check=False,
                        capture_output=True,
                        timeout=5
                    )
                    if pgrep_result.returncode == 0 and pgrep_result.stdout.strip():
                        conflict = True
                        details.append(f"检测到CN运行进程匹配: {pattern}")
                except Exception as e:
                    logger.debug(f"进程检测失败: {e}")
        elif role == "cn":
            # 检测BE是否在运行：查找start_be.sh或--be参数
            be_patterns = ["start_be.sh", "--be"]
            for pattern in be_patterns:
                try:
                    pgrep_result = run_command(
                        ['pgrep', '-f', pattern],
                        check=False,
                        capture_output=True,
                        timeout=5
                    )
                    if pgrep_result.returncode == 0 and pgrep_result.stdout.strip():
                        conflict = True
                        details.append(f"检测到BE运行进程匹配: {pattern}")
                except Exception as e:
                    logger.debug(f"进程检测失败: {e}")

        # 端口占用检查（仅作为辅助信号，因为BE和CN端口可能相同）
        # 注意：端口占用不能单独作为冲突证据，因为可能是同一角色在运行（幂等性场景）
        port_conflicts = []
        for port in self._get_role_ports(role):
            try:
                if not EnvironmentChecker.check_port_available(port):
                    port_conflicts.append(port)
            except Exception as e:
                logger.debug(f"检查端口占用失败: {e}")

        # 只有当有其他冲突信号时，才报告端口占用（避免误判幂等性场景）
        if conflict and port_conflicts:
            for port in port_conflicts:
                details.append(f"端口已被占用: {port}")

        if details:
            conflict = True
        return conflict, details, warnings

    def _write_config_file(self, config_path: Path, config_content: str, node_type: str) -> bool:
        """写入配置文件（带备份和权限设置）"""
        # 备份原配置文件
        if config_path.exists():
            backup_path = config_path.with_suffix('.conf.backup')
            try:
                shutil.copy2(config_path, backup_path)
                logger.info(f"备份原配置文件到: {backup_path}", extra={"to_stdout": True})
            except Exception as e:
                logger.error(f"备份配置文件失败: {e}", extra={"to_stdout": True})
                return False

        # 写入配置文件
        try:
            config_path.write_text(config_content, encoding='utf-8')
            # 设置配置文件权限（644）
            try:
                os.chmod(config_path, 0o644)
            except Exception as e:
                logger.warning(f"设置配置文件权限失败: {e}", extra={"to_stdout": True})
            logger.info(f"✓ {node_type}配置文件已生成: {config_path}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error(f"写入{node_type}配置文件失败: {e}", extra={"to_stdout": True})
            return False

    def _create_systemd_service(self, service_content: str, service_path: Path) -> None:
        """创建systemd服务文件"""
        try:
            # 检查是否有root权限（Linux平台）
            is_root = os.geteuid() == 0

            if is_root:
                service_path.write_text(service_content, encoding='utf-8')
                os.chmod(service_path, 0o644)
                try:
                    os.chmod(service_path, 0o644)
                except Exception as e:
                    logger.error("设置服务文件权限失败: {}".format(e), extra={"to_stdout": True})

                logger.info(f"✓ Systemd服务文件已生成: {service_path}", extra={"to_stdout": True})

                # 重新加载systemd
                run_command(
                    ['systemctl', 'daemon-reload'],
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=30
                )
                logger.info("✓ Systemd配置已重新加载", extra={"to_stdout": True})
            else:
                logger.warning("需要root权限才能创建systemd服务文件", extra={"to_stdout": True})
                logger.info(f"请手动创建服务文件: {service_path}", extra={"to_stdout": True})
                logger.info("服务文件内容:", extra={"to_stdout": True})
                print(service_content)
        except Exception as e:
            logger.error("创建systemd服务文件失败: {}".format(e), extra={"to_stdout": True})
            logger.info("服务文件内容:", extra={"to_stdout": True})
            print(service_content)

    def _validate_ports(self, ports: List[int]) -> bool:
        """验证端口列表"""
        for port in ports:
            if not InputValidator.validate_port(port):
                logger.error(f"无效的端口号: {port}", extra={"to_stdout": True})
                return False
            if not EnvironmentChecker.check_port_available(port):
                logger.warning(f"端口 {port} 已被占用，请检查", extra={"to_stdout": True})
        return True

    def _validate_and_get_java(self, java_home: Optional[str]) -> Optional[str]:
        """获取Java路径（如果未提供则自动检测）"""
        if not java_home:
            _, java_home, _ = EnvironmentChecker.check_java()
        return java_home

    def _validate_java_home_complete(self, java_home: Optional[str]) -> Tuple[bool, Optional[str]]:
        """
        完整验证Java环境（包括JAVA_HOME路径和bin/java存在性）
        
        Returns:
            (is_valid, java_home_path): 如果有效返回(True, java_home)，否则返回(False, None)
        """
        java_home = self._validate_and_get_java(java_home)
        if not java_home:
            logger.error("无法获取JAVA_HOME", extra={"to_stdout": True})
            logger.error("请确保已安装JDK 17+，可以通过以下方式之一：", extra={"to_stdout": True})
            logger.error("  1. 设置JAVA_HOME环境变量", extra={"to_stdout": True})
            logger.error("  2. 通过包管理器安装（脚本会自动检测）:", extra={"to_stdout": True})
            logger.error("     - CentOS/RHEL: yum install java-17-openjdk java-17-openjdk-devel",
                         extra={"to_stdout": True})
            logger.error("     - Debian/Ubuntu: apt-get install openjdk-17-jdk", extra={"to_stdout": True})
            logger.error("  3. 手动指定: --java-home /path/to/jdk", extra={"to_stdout": True})
            return False, None

        java_home_path = Path(java_home)
        if not java_home_path.exists():
            logger.error(f"JAVA_HOME路径不存在: {java_home}", extra={"to_stdout": True})
            return False, None
        if not (java_home_path / "bin" / "java").exists():
            logger.error(f"JAVA_HOME路径无效（未找到bin/java）: {java_home}", extra={"to_stdout": True})
            return False, None

        logger.info(f"使用JAVA_HOME: {java_home}", extra={"to_stdout": True})
        return True, java_home

    def _check_existing_deployment_and_handle_force(
            self,
            node_type: str,
            service_name: str,
            force: bool,
            enable_systemd: bool,
            fe_host: Optional[str] = None,
            fe_query_port: int = 9030,
            password: Optional[str] = None
    ) -> Tuple[bool, bool]:
        """
        检查已存在部署并处理force和幂等性逻辑
        
        Args:
            node_type: 节点类型 ('fe', 'be', 'cn')
            service_name: systemd服务名称
            force: 是否强制重新部署
            enable_systemd: 是否启用systemd
            fe_host: FE主机地址（用于清理）
            fe_query_port: FE查询端口
            password: 密码
        
        Returns:
            (should_skip_deployment, success): 是否跳过部署步骤，操作是否成功
        """
        exists, details = self.check_existing_deployment(node_type)
        skip_deployment = False

        if exists:
            if force:
                logger.warning(f"检测到已存在的{node_type.upper()}部署，将进行清理后重新部署", extra={"to_stdout": True})
                for detail in details:
                    logger.warning(f"  - {detail}", extra={"to_stdout": True})
                try:
                    if not self.clean_deployment(node_type, backup_config=True, fe_host=fe_host,
                                                 fe_query_port=fe_query_port, password=password):
                        logger.error("清理现有部署失败，无法继续", extra={"to_stdout": True})
                        return False, False
                except ValueError as e:
                    logger.error(f"清理现有部署失败: {e}", extra={"to_stdout": True})
                    return False, False
                except Exception as e:
                    logger.error(f"清理现有部署时发生异常: {e}", extra={"to_stdout": True})
                    return False, False
                skip_deployment = False
            else:
                is_service_running = self._is_service_active(service_name) if enable_systemd else False
                if is_service_running:
                    logger.info(f"检测到{node_type.upper()}服务已部署且正在运行，跳过部署步骤（幂等性）",
                                extra={"to_stdout": True})
                    logger.info("将只执行后续操作（如加入集群等）", extra={"to_stdout": True})
                    skip_deployment = True
                else:
                    logger.warning(f"检测到已存在的{node_type.upper()}部署，但服务未运行:", extra={"to_stdout": True})
                    for detail in details:
                        logger.warning(f"  - {detail}", extra={"to_stdout": True})
                    logger.warning("将尝试重新部署配置和服务", extra={"to_stdout": True})
                    skip_deployment = False

        return skip_deployment, True

    def _add_node_to_cluster_with_wait(
            self,
            node_type: str,
            fe_host: Optional[str],
            fe_query_port: int,
            node_port: int,
            priority_networks: Optional[str],
            password: Optional[str],
            skip_deployment: bool,
            enable_systemd: bool,
            ports_to_wait: List[int]
    ) -> bool:
        """
        等待节点启动并添加到集群（BE/CN通用逻辑）
        
        Args:
            node_type: 节点类型 ("BE" 或 "CN")
            fe_host: FE主机地址
            fe_query_port: FE查询端口
            node_port: 节点心跳端口
            priority_networks: 优先网络
            password: 密码
            skip_deployment: 是否跳过了部署步骤
            enable_systemd: 是否启用systemd
            ports_to_wait: 需要等待的端口列表
        
        Returns:
            是否成功
        """
        if not fe_host:
            node_ip = self._get_local_ip("127.0.0.1", 9030, priority_networks)
            self._print_add_cluster_hint(node_type, node_ip or f"YOUR_{node_type}_IP", node_port)
            return True

        # 等待节点启动（如果刚部署）
        if not skip_deployment:
            if enable_systemd:
                logger.info(f"等待{node_type}服务启动...", extra={"to_stdout": True})
                time.sleep(10)
            else:
                logger.info(f"等待{node_type}进程启动...", extra={"to_stdout": True})
                time.sleep(5)

            if not self._wait_for_ports("127.0.0.1", ports_to_wait, timeout=60):
                logger.error(
                    f"{node_type}端口未就绪（{','.join(map(str, ports_to_wait))}），请检查{node_type}启动日志",
                    extra={"to_stdout": True}
                )
                return False

        node_ip = self._get_local_ip(fe_host, fe_query_port, priority_networks)
        if not node_ip:
            logger.error("无法确定本机IP地址，请手动添加节点到集群", extra={"to_stdout": True})
            self._print_add_cluster_hint(node_type, node_ip or f"YOUR_{node_type}_IP", node_port, fe_host,
                                         fe_query_port)
            return True

        try:
            self._add_node_to_cluster(fe_host, fe_query_port, node_ip, node_port, node_type, password)
        except ValueError as e:
            logger.error(str(e), extra={"to_stdout": True})
            return False

        return True

    def _validate_priority_networks(self, priority_networks: Optional[str]) -> bool:
        """验证priority_networks"""
        if priority_networks and not InputValidator.validate_ip_or_cidr(priority_networks):
            logger.error(f"无效的priority_networks格式: {priority_networks}", extra={"to_stdout": True})
            return False
        return True

    def _log_deployment_completion(self, node_type: str, start_command: str, service_name: str,
                                   enable_systemd: bool) -> None:
        """记录部署完成信息"""
        logger.info(f"=== {node_type}节点部署完成 ===", extra={"to_stdout": True})
        logger.info(f"配置文件: {self.starrocks_home / node_type.lower() / 'conf' / f'{node_type.lower()}.conf'}",
                    extra={"to_stdout": True})

        if enable_systemd:
            logger.info(f"Systemd服务: {service_name}", extra={"to_stdout": True})
            logger.info(f"启动服务: systemctl start {service_name}", extra={"to_stdout": True})
            logger.info(f"查看状态: systemctl status {service_name}", extra={"to_stdout": True})
            logger.info(f"查看日志: journalctl -u {service_name} -f", extra={"to_stdout": True})

            # 尝试启动服务（非阻塞，立即返回）
            try:
                logger.info(f"正在启动服务 {service_name}...", extra={"to_stdout": True})
                run_command(['systemctl', 'enable', service_name], check=False, allowed_exit_codes=[0, 1], timeout=10)
                # systemctl start 对于 forking 类型服务应该立即返回，设置超时避免阻塞
                result = run_command(['systemctl', 'start', service_name], check=False, allowed_exit_codes=[0, 1],
                                     timeout=30)
                if result.returncode == 0:
                    logger.info(f"✓ 服务 {service_name} 启动命令已执行", extra={"to_stdout": True})
                else:
                    logger.warning(f"服务启动命令执行失败，请检查状态: systemctl status {service_name}",
                                   extra={"to_stdout": True})
            except Exception as e:
                logger.warning(f"自动启动服务失败: {e}", extra={"to_stdout": True})
                logger.warning(f"请手动执行: systemctl start {service_name}", extra={"to_stdout": True})
        else:
            logger.info("启动命令:", extra={"to_stdout": True})
            logger.info(f"  {start_command}", extra={"to_stdout": True})

        # 打印后续操作提示
        if node_type == "FE":
            logger.info("\n=== 下一步操作 ===", extra={"to_stdout": True})
            logger.info("1. 确认FE启动成功:", extra={"to_stdout": True})
            logger.info(f"   cat {self.starrocks_home / 'fe' / 'log' / 'fe.log'} | grep thrift",
                        extra={"to_stdout": True})
            logger.info("   或: systemctl status starrocks-fe", extra={"to_stdout": True})
            logger.info("2. 使用MySQL客户端连接FE:", extra={"to_stdout": True})
            logger.info("   mysql -h <FE_IP> -P<QUERY_PORT> -uroot", extra={"to_stdout": True})
            logger.info("3. 添加BE/CN节点到集群(重要):", extra={"to_stdout": True})
            logger.info("   ALTER SYSTEM ADD BACKEND \"<BE_IP>:<HEARTBEAT_PORT>\";", extra={"to_stdout": True})
            logger.info("   ALTER SYSTEM ADD COMPUTE NODE \"<CN_IP>:<HEARTBEAT_PORT>\";", extra={"to_stdout": True})
            logger.info("4. 如果是单机部署(1 FE + 1 BE)，请确保设置 default_replication_num = 1",
                        extra={"to_stdout": True})
        elif node_type in ["BE", "CN"]:
            logger.info("\n=== 下一步操作 ===", extra={"to_stdout": True})
            logger.info(f"1. 确认{node_type}启动成功:", extra={"to_stdout": True})
            logger.info(f"   cat {self.starrocks_home / 'be' / 'log' / f'{node_type.lower()}.INFO'} | grep heartbeat",
                        extra={"to_stdout": True})
            logger.info(f"2. 在FE中添加此节点(如果尚未添加):", extra={"to_stdout": True})
            if node_type == "BE":
                logger.info(
                    f"   mysql -h <FE_IP> -P<QUERY_PORT> -uroot -e 'ALTER SYSTEM ADD BACKEND \"<THIS_NODE_IP>:<HEARTBEAT_PORT>\";'",
                    extra={"to_stdout": True})
            else:
                logger.info(
                    f"   mysql -h <FE_IP> -P<QUERY_PORT> -uroot -e 'ALTER SYSTEM ADD COMPUTE NODE \"<THIS_NODE_IP>:<HEARTBEAT_PORT>\";'",
                    extra={"to_stdout": True})

    def _validate_helper_address(self, helper_address: str) -> Tuple[bool, Optional[str], Optional[int]]:
        """
        验证helper_address格式
        
        Returns:
            (is_valid, helper_host, helper_port): 如果有效返回(True, host, port)，否则返回(False, None, None)
        """
        if ':' not in helper_address:
            logger.error(f"无效的helper_address格式: {helper_address}，格式应为: host:port (例如: 192.168.1.100:9010)",
                         extra={"to_stdout": True})
            return False, None, None

        helper_parts = helper_address.split(':', 1)
        if len(helper_parts) != 2:
            logger.error(f"无效的helper_address格式: {helper_address}，格式应为: host:port (例如: 192.168.1.100:9010)",
                         extra={"to_stdout": True})
            return False, None, None

        helper_host, helper_port_str = helper_parts
        if not InputValidator.validate_hostname(helper_host):
            logger.error(f"无效的helper_address主机地址: {helper_host}，必须是有效的IPv4地址或主机名",
                         extra={"to_stdout": True})
            return False, None, None

        try:
            helper_port = int(helper_port_str)
            if not InputValidator.validate_port(helper_port):
                logger.error(f"无效的helper_address端口号: {helper_port}，端口号必须在1-65535之间",
                             extra={"to_stdout": True})
                return False, None, None
            return True, helper_host, helper_port
        except ValueError:
            logger.error(f"无效的helper_address端口号: {helper_port_str}，必须是数字", extra={"to_stdout": True})
            return False, None, None

    def _check_and_handle_bdb_je_metadata(self, meta_dir: str, helper_address: Optional[str], force: bool) -> bool:
        """
        检查并处理BDB JE元数据冲突（Follower节点必须使用空元数据目录）
        
        Returns:
            是否成功处理（如果force=False且存在冲突，返回False）
        """
        if not helper_address:
            return True

        meta_path = Path(meta_dir)
        if not meta_path.exists():
            return True

        bdb_files = list(meta_path.glob("*.jdb")) + list(meta_path.glob("*.lck"))
        if not bdb_files:
            return True

        if force:
            logger.warning(f"检测到元数据目录包含BDB JE数据文件，将清理后重新部署（Follower节点必须使用空元数据目录）",
                           extra={"to_stdout": True})
            try:
                import shutil
                for item in meta_path.iterdir():
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir() and item.name != "image":
                        shutil.rmtree(item)
                logger.info(f"已清理元数据目录中的BDB JE数据", extra={"to_stdout": True})
                return True
            except Exception as e:
                logger.error(f"清理元数据目录失败: {e}", extra={"to_stdout": True})
                logger.error(f"请手动清理元数据目录: {meta_dir}，然后重新部署", extra={"to_stdout": True})
                return False
        else:
            logger.error(f"检测到元数据目录包含BDB JE数据文件，Follower节点必须使用空元数据目录",
                         extra={"to_stdout": True})
            logger.error(f"元数据目录: {meta_dir}", extra={"to_stdout": True})
            logger.error(f"检测到的BDB文件: {[str(f.name) for f in bdb_files[:5]]}", extra={"to_stdout": True})
            logger.error(f"解决方案：", extra={"to_stdout": True})
            logger.error(f"  1. 使用 --force 参数自动清理并重新部署", extra={"to_stdout": True})
            logger.error(f"  2. 或手动清理元数据目录后重新部署: rm -rf {meta_dir}/*", extra={"to_stdout": True})
            return False

    def _prepare_fe_meta_directory(self, meta_dir: str) -> bool:
        """
        准备FE元数据目录（创建目录、设置权限、验证可写性）
        
        Returns:
            是否成功
        """
        meta_path = Path(meta_dir)

        if not meta_path.exists():
            try:
                meta_path.mkdir(parents=True, exist_ok=True, mode=0o755)
                logger.info(f"创建元数据目录: {meta_dir}", extra={"to_stdout": True})
            except Exception as e:
                logger.error(f"无法创建元数据目录: {e}", extra={"to_stdout": True})
                return False

        image_dir = meta_path / "image"
        if not image_dir.exists():
            try:
                image_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
                logger.info(f"创建image子目录: {image_dir}", extra={"to_stdout": True})
            except Exception as e:
                logger.error(f"无法创建image子目录: {e}", extra={"to_stdout": True})
                return False

        if not self._ensure_directory_permissions(meta_path, recursive=True):
            return False

        if not EnvironmentChecker.check_directory_writable(meta_dir):
            logger.error(f"元数据目录不可写: {meta_dir}", extra={"to_stdout": True})
            logger.error(f"请确保运行用户 {self.user} 对目录有写权限", extra={"to_stdout": True})
            return False

        if not EnvironmentChecker.check_directory_writable(str(image_dir)):
            logger.error(f"image子目录不可写: {image_dir}", extra={"to_stdout": True})
            logger.error(f"请确保运行用户 {self.user} 对目录有写权限", extra={"to_stdout": True})
            return False

        if not EnvironmentChecker.check_directory_independent(self.starrocks_home, meta_dir):
            logger.warning(f"元数据目录 {meta_dir} 位于安装目录内，建议使用独立目录以提高性能和可靠性",
                           extra={"to_stdout": True})

        return True

    def _handle_fe_follower_cluster_join(
            self,
            helper_address: Optional[str],
            priority_networks: Optional[str],
            edit_log_port: int,
            password: Optional[str],
            skip_deployment: bool
    ) -> bool:
        """
        处理FE Follower节点加入集群的逻辑
        
        Returns:
            是否成功（如果不需要加入或已加入，返回True）
        """
        if not helper_address:
            return True

        try:
            helper_parts = helper_address.split(':', 1)
            if len(helper_parts) != 2:
                logger.warning(f"helper_address格式无效: {helper_address}，跳过自动添加到集群",
                               extra={"to_stdout": True})
                return True

            helper_host = helper_parts[0]
            helper_query_port = 9030

            node_ip = self._get_local_ip(helper_host, helper_query_port, priority_networks)
            if node_ip and node_ip != "127.0.0.1":
                if skip_deployment:
                    # 如果服务已运行，直接尝试添加到集群
                    if self._add_fe_follower_to_cluster(helper_host, helper_query_port, node_ip, edit_log_port,
                                                        password):
                        logger.info(f"✓ FE Follower节点 ({node_ip}:{edit_log_port}) 已在集群中或已添加到集群",
                                    extra={"to_stdout": True})
                    else:
                        logger.warning(
                            f"FE Follower节点 ({node_ip}:{edit_log_port}) 添加到集群失败，请手动执行: ALTER SYSTEM ADD FOLLOWER \"{node_ip}:{edit_log_port}\";",
                            extra={"to_stdout": True})
                else:
                    # 如果刚部署，等待启动后再添加
                    logger.info(f"等待FE Follower节点启动...", extra={"to_stdout": True})
                    time.sleep(10)

                    if self._add_fe_follower_to_cluster(helper_host, helper_query_port, node_ip, edit_log_port,
                                                        password):
                        logger.info(f"✓ FE Follower节点 ({node_ip}:{edit_log_port}) 已添加到集群",
                                    extra={"to_stdout": True})
                    else:
                        logger.warning(
                            f"FE Follower节点 ({node_ip}:{edit_log_port}) 添加到集群失败，请手动执行: ALTER SYSTEM ADD FOLLOWER \"{node_ip}:{edit_log_port}\";",
                            extra={"to_stdout": True})
            else:
                logger.warning("无法获取本机IP地址，无法自动添加FE Follower到集群", extra={"to_stdout": True})
                logger.warning("请手动执行: ALTER SYSTEM ADD FOLLOWER \"<本机IP>:<edit_log_port>\";",
                               extra={"to_stdout": True})
        except Exception as e:
            logger.warning(f"处理helper_address失败: {e}，跳过自动添加到集群", extra={"to_stdout": True})

        return True

    def _ensure_directory_permissions(self, path: Path, recursive: bool = False) -> bool:
        """确保目录所有者和基本权限（umask 会处理新文件的权限）"""
        if not path.exists():
            return True

        is_root = os.geteuid() == 0
        try:
            # 只设置所有者，不手动设置权限（让 umask 自然生效）
            if self.user:
                owner = f"{self.user}:{self.group}" if self.group else self.user
                cmd = ['chown', '-R', owner, str(path)] if recursive else ['chown', owner, str(path)]
                result = run_command(cmd, check=False, allowed_exit_codes=[0, 1])

                if result.returncode != 0:
                    if is_root:
                        logger.error(f"设置目录所有者失败: {path}", extra={"to_stdout": True})
                        return False
                    else:
                        logger.warning(f"无法设置目录所有者 (需要root权限): {path}", extra={"to_stdout": True})
                        logger.warning(f"请手动执行: chown -R {owner} {path}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error(f"设置目录所有者异常: {e}", extra={"to_stdout": True})
            return False

    def deploy_fe(
            self,
            meta_dir: str,
            http_port: int = 8030,
            rpc_port: int = 9020,
            query_port: int = 9030,
            edit_log_port: int = 9010,
            priority_networks: Optional[str] = None,
            java_home: Optional[str] = None,
            default_replication_num: Optional[int] = None,
            use_fqdn: bool = False,
            helper_address: Optional[str] = None,
            enable_systemd: bool = True,
            java_opts: Optional[str] = None,
            extra_config: Optional[Dict[str, Any]] = None,
            force: bool = False,
            password: Optional[str] = None,
            fe_host: Optional[str] = None,
            fe_query_port: int = 9030
    ) -> bool:
        """部署FE节点（幂等操作）"""
        logger.info("=== 开始部署FE节点 ===", extra={"to_stdout": True})

        # 校验 helper_address 格式（如果提供）
        if helper_address:
            is_valid, _, _ = self._validate_helper_address(helper_address)
            if not is_valid:
                return False

        # 检查是否已存在部署
        exists, details = self.check_existing_deployment('fe')
        service_name = 'starrocks-fe'
        skip_deployment = False  # 默认不跳过部署

        if exists:
            if force:
                logger.warning("检测到已存在的FE部署，将进行清理后重新部署", extra={"to_stdout": True})
                for detail in details:
                    logger.warning(f"  - {detail}", extra={"to_stdout": True})

                # 优先使用传入的fe_host，如果没有则从helper_address中提取
                fe_host_for_clean = fe_host
                fe_query_port_for_clean = fe_query_port
                if not fe_host_for_clean and helper_address:
                    try:
                        helper_parts = helper_address.split(':', 1)
                        if len(helper_parts) == 2:
                            fe_host_for_clean = helper_parts[0]
                    except Exception as ex:
                        logger.error(f"Failed to abstract fe_host: {ex}", extra={"to_stdout": True})
                        pass

                if not self.clean_deployment('fe', backup_config=True, fe_host=fe_host_for_clean,
                                             fe_query_port=fe_query_port_for_clean, password=password):
                    logger.error("清理现有部署失败，无法继续", extra={"to_stdout": True})
                    return False
                skip_deployment = False
            else:
                # 检查服务是否正在运行
                is_service_running = self._is_service_active(service_name) if enable_systemd else False
                # 幂等性：如果服务已部署且正常运行，跳过部署步骤
                if is_service_running:
                    logger.info("检测到FE服务已部署且正在运行，跳过部署步骤（幂等性）", extra={"to_stdout": True})
                    skip_deployment = True
                else:
                    logger.warning("检测到已存在的FE部署，但服务未运行:", extra={"to_stdout": True})
                    for detail in details:
                        logger.warning(f"  - {detail}", extra={"to_stdout": True})
                    logger.warning("将尝试重新部署配置和服务", extra={"to_stdout": True})
                    skip_deployment = False

        # 如果跳过部署，检查是否需要添加到集群（Follower节点）
        if skip_deployment:
            logger.info("FE节点部署完成（已存在且运行正常）", extra={"to_stdout": True})
            self._handle_fe_follower_cluster_join(helper_address, priority_networks, edit_log_port, password, True)
            return True

        # 验证端口
        if not self._validate_ports([http_port, rpc_port, query_port, edit_log_port]):
            return False

        # 检查并处理BDB JE元数据冲突（Follower节点必须使用空元数据目录）
        if not self._check_and_handle_bdb_je_metadata(meta_dir, helper_address, force):
            return False

        # 准备元数据目录（创建、设置权限、验证可写性）
        if not self._prepare_fe_meta_directory(meta_dir):
            return False

        # 验证priority_networks
        if not self._validate_priority_networks(priority_networks):
            return False

        is_valid, java_home = self._validate_java_home_complete(java_home)
        if not is_valid:
            return False

        # 生成配置文件
        fe_conf_path = self.starrocks_home / "fe" / "conf" / "fe.conf"
        if not fe_conf_path.parent.exists():
            logger.error(f"FE配置目录不存在: {fe_conf_path.parent}", extra={"to_stdout": True})
            return False

        config_content = ConfigGenerator.generate_fe_config(
            meta_dir=meta_dir,
            http_port=http_port,
            rpc_port=rpc_port,
            query_port=query_port,
            edit_log_port=edit_log_port,
            priority_networks=priority_networks,
            java_home=java_home,
            default_replication_num=default_replication_num,
            java_opts=java_opts,
            extra_config=extra_config
        )

        # 写入配置文件
        if not self._write_config_file(fe_conf_path, config_content, "FE"):
            return False

        # 确保PID文件目录存在且有正确权限
        pid_file_dir = self.starrocks_home / "fe"
        if not pid_file_dir.exists():
            logger.error(f"FE目录不存在: {pid_file_dir}", extra={"to_stdout": True})
            return False

        if not self._ensure_directory_permissions(pid_file_dir, recursive=True):
            logger.error(f"无法设置FE目录权限: {pid_file_dir}", extra={"to_stdout": True})
            logger.error(f"请确保运行用户 {self.user} 对 {pid_file_dir} 有写权限", extra={"to_stdout": True})
            return False

        log_dir = pid_file_dir / "log"
        if not log_dir.exists():
            try:
                log_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
                logger.info(f"创建FE日志目录: {log_dir}", extra={"to_stdout": True})
            except Exception as e:
                logger.error(f"无法创建FE日志目录: {e}", extra={"to_stdout": True})
                return False

        if not self._ensure_directory_permissions(log_dir, recursive=False):
            logger.error(f"无法设置FE日志目录权限: {log_dir}", extra={"to_stdout": True})
            return False

        # 生成systemd服务文件
        if enable_systemd:
            service_content = SystemdServiceGenerator.generate_fe_service(
                starrocks_home=str(self.starrocks_home),
                user=self.user,
                group=self.group,
                java_home=java_home,
                helper_address=helper_address,
                use_fqdn=use_fqdn
            )
            service_path = Path("/etc/systemd/system/starrocks-fe.service")
            self._create_systemd_service(service_content, service_path)

        # 生成启动命令（用于日志输出，实际启动由 systemd 服务管理）
        # 注意：helper_address 已经是 host:port 格式，不需要再拼接 edit_log_port
        if helper_address:
            start_cmd = f"{self.starrocks_home / 'fe' / 'bin' / 'start_fe.sh'} --helper {helper_address} --daemon"
        elif use_fqdn:
            start_cmd = f"{self.starrocks_home / 'fe' / 'bin' / 'start_fe.sh'} --host_type FQDN --daemon"
        else:
            start_cmd = f"{self.starrocks_home / 'fe' / 'bin' / 'start_fe.sh'} --daemon"

        self._log_deployment_completion("FE", start_cmd, "starrocks-fe", enable_systemd)

        # 如果是Follower节点（提供了helper_address），需要添加到集群
        self._handle_fe_follower_cluster_join(helper_address, priority_networks, edit_log_port, password, False)

        return True

    def _get_local_ip(self, fe_host: str, fe_query_port: int, priority_networks: Optional[str] = None) -> Optional[str]:
        """获取本机IP地址（优先使用priority_networks匹配的IP）"""
        # 方法1: 如果指定了priority_networks，尝试匹配该网段的IPv4 IP
        if priority_networks:
            try:
                import ipaddress
                network = ipaddress.ip_network(priority_networks, strict=False)
                # 只处理IPv4网络
                if network.version != 4:
                    logger.warning(f"priority_networks {priority_networks} 不是IPv4网络，将使用其他方法获取IP",
                                   extra={"to_stdout": True})
                else:
                    hostname = socket.gethostname()
                    # 只获取IPv4地址
                    for addr_info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
                        ip = addr_info[4][0]
                        try:
                            addr = ipaddress.ip_address(ip)
                            # 只接受IPv4地址，排除回环地址和链路本地地址
                            if isinstance(addr,
                                          ipaddress.IPv4Address) and not addr.is_loopback and not addr.is_link_local:
                                if addr in network:
                                    logger.info(f"使用priority_networks匹配的IPv4 IP: {ip}", extra={"to_stdout": True})
                                    return ip
                        except ValueError:
                            continue
            except Exception as e:
                logger.debug(f"通过priority_networks匹配IP失败: {e}")

        # 方法2: 通过连接FE获取本机IP
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((fe_host, fe_query_port))
            node_ip = s.getsockname()[0]
            s.close()
            if node_ip and node_ip != "127.0.0.1":
                logger.info(f"通过连接FE获取本机IP: {node_ip}", extra={"to_stdout": True})
                return node_ip
        except Exception as e:
            logger.debug(f"通过连接FE获取IP失败: {e}")

        # 方法3: 获取所有非回环IPv4地址（只返回IPv4，排除IPv6）
        try:
            import ipaddress
            hostname = socket.gethostname()
            # 只获取IPv4地址
            for addr_info in socket.getaddrinfo(hostname, None, family=socket.AF_INET):
                ip = addr_info[4][0]
                try:
                    addr = ipaddress.ip_address(ip)
                    # 只接受IPv4地址，排除回环地址和链路本地地址
                    if isinstance(addr, ipaddress.IPv4Address) and not addr.is_loopback and not addr.is_link_local:
                        logger.info(f"使用主机名解析的IPv4 IP: {ip}", extra={"to_stdout": True})
                        return ip
                except ValueError:
                    continue
        except Exception as e:
            logger.debug(f"通过主机名获取IP失败: {e}")

        logger.warning("无法自动检测本机IPv4 IP，将使用 127.0.0.1", extra={"to_stdout": True})
        return "127.0.0.1"

    def _wait_for_fe_ready(self, fe_host: str, fe_query_port: int, timeout: int = 120,
                           password: Optional[str] = None) -> bool:
        """等待FE就绪（可以接受SQL连接）"""
        logger.info(f"等待FE就绪 ({fe_host}:{fe_query_port})...", extra={"to_stdout": True})

        mysql_client = shutil.which('mysql')
        if not mysql_client:
            logger.warning("未找到 mysql 客户端，无法检查FE就绪状态", extra={"to_stdout": True})
            return False

        start_time = time.time()
        check_interval = 3

        while time.time() - start_time < timeout:
            try:
                cmd = [mysql_client, '-h', fe_host, '-P', str(fe_query_port), '-uroot', '-e', 'SELECT 1;']
                if password:
                    env = os.environ.copy()
                    env['MYSQL_PWD'] = password
                    result = run_command(cmd, check=False, capture_output=True, timeout=5, env=env)
                else:
                    result = run_command(cmd, check=False, capture_output=True, timeout=5)

                if result.returncode == 0:
                    logger.info("✓ FE已就绪，可以接受连接", extra={"to_stdout": True})
                    return True

                # 如果是连接错误，继续等待
                if "Can't connect to MySQL server" in result.stderr or "Connection refused" in result.stderr:
                    elapsed = int(time.time() - start_time)
                    logger.debug(f"FE尚未就绪，等待中... ({elapsed}/{timeout}秒)")
                    time.sleep(check_interval)
                else:
                    # 其他错误可能表示FE已启动但有问题，记录但继续等待
                    logger.debug(f"FE连接异常: {result.stderr[:100]}")
                    time.sleep(check_interval)

            except Exception as e:
                logger.debug(f"检查FE就绪状态失败: {e}")
                time.sleep(check_interval)

        logger.error(f"等待FE就绪超时 ({timeout}秒)", extra={"to_stdout": True})
        return False

    def _ensure_fe_available(self, fe_host: str, fe_query_port: int, password: Optional[str] = None) -> bool:
        """确保FE可连接（用于BE/CN部署前置检查）"""
        try:
            with socket.create_connection((fe_host, fe_query_port), timeout=5):
                pass
        except Exception as e:
            logger.error(
                f"无法连接FE {fe_host}:{fe_query_port}，请检查网络/路由/防火墙/端口配置: {e}",
                extra={"to_stdout": True}
            )
            return False

        mysql_client = shutil.which('mysql')
        if not mysql_client:
            logger.error("未找到 mysql 客户端，无法检查FE状态", extra={"to_stdout": True})
            return False
        if not self._wait_for_fe_ready(fe_host, fe_query_port, timeout=60, password=password):
            logger.error("FE不可达或未就绪，请检查 --fe-host 与端口", extra={"to_stdout": True})
            return False
        return True

    def _wait_for_ports(self, host: str, ports: List[int], timeout: int = 60) -> bool:
        """等待指定主机端口就绪（用于确认BE/CN已启动）"""
        start_time = time.time()
        pending = set(ports)
        while time.time() - start_time < timeout:
            for port in list(pending):
                try:
                    with socket.create_connection((host, port), timeout=2):
                        pending.discard(port)
                except Exception as ex:
                    logger.warning(f"Failed to create socket connection to {host}:{port}, {ex}",
                                   extra={"to_stdout": True})
                    continue
            if not pending:
                return True
            time.sleep(2)
        return False

    def _parse_node_list_output(self, output: str, node_type: str) -> List[Dict[str, Any]]:
        """
        解析SHOW BACKENDS或SHOW COMPUTE NODES的输出
        
        Args:
            output: SQL命令的输出
            node_type: 节点类型 ("BE" 或 "CN")
        
        Returns:
            节点列表，每个节点包含id和ip
        """
        nodes = []
        if not output:
            return nodes

        header_key = "BackendId" if node_type == "BE" else "ComputeNodeId"
        lines = output.splitlines()
        header_found = False

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # 识别表头行
            if header_key in line and "IP" in line and "HeartbeatPort" in line:
                header_found = True
                logger.debug(f"找到{node_type}表头行: {line}")
                continue

            # 只有在找到表头后才开始解析数据行
            if header_found:
                tokens = re.split(r"\s+", line)
                if len(tokens) >= 2:
                    try:
                        node_id = int(tokens[0])
                        ip_str = tokens[1]
                        ip_pattern = re.search(r'\d+\.\d+\.\d+\.\d+', ip_str)
                        if node_id > 0 and ip_pattern:
                            nodes.append({"id": node_id, "ip": ip_str})
                            logger.debug(f"检测到{node_type}节点: {header_key}={node_id}, IP={ip_str}")
                    except (ValueError, IndexError) as e:
                        logger.debug(f"解析{node_type}数据行失败: {line}, 错误: {e}")
                        continue

        return nodes

    def _check_node_in_cluster(
            self,
            fe_host: str,
            fe_query_port: int,
            node_ip: str,
            node_port: int,
            node_type: str,
            password: Optional[str] = None
    ) -> bool:
        """检查节点是否已在集群中（作为指定类型）"""
        sql = "SHOW BACKENDS;" if node_type == "BE" else "SHOW COMPUTE NODES;"
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password)
        if not success or not output:
            return False

        # 使用统一的解析方法
        nodes = self._parse_node_list_output(output, node_type)
        for node in nodes:
            if node["ip"] == node_ip:
                # 检查端口是否在输出中（HeartbeatPort列）
                if str(node_port) in output:
                    return True
        return False

    def _check_role_conflict(self, fe_host: str, fe_query_port: int, node_ip: str, node_port: int, node_type: str,
                             password: Optional[str] = None) -> Optional[str]:
        """
        检查节点角色冲突
        
        Returns:
            None: 无冲突
            str: 冲突的节点类型（"BE" 或 "CN"）
        """
        try:
            if node_type == "BE":
                sql = "SHOW COMPUTE NODES;"
                check_type = "CN"
            else:
                sql = "SHOW BACKENDS;"
                check_type = "BE"

            success, output = self._execute_sql(fe_host, fe_query_port, sql, password)
            if success and output:
                node_address = f"{node_ip}:{node_port}"
                if node_address in output:
                    return check_type
            return None
        except Exception as e:
            logger.debug(f"检查节点角色冲突失败: {e}")
            # 如果检查失败，返回None（不阻止部署，让后续步骤处理）
            return None

    def _add_fe_follower_to_cluster(self, fe_host: str, fe_query_port: int, node_ip: str, edit_log_port: int,
                                    password: Optional[str] = None) -> bool:
        """将FE Follower节点添加到集群（幂等操作）"""
        sql = f'SHOW FRONTENDS;'
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password)
        if success and output:
            for line in output.splitlines():
                line = line.strip()
                if not line or "IP" in line and "EditLogPort" in line:
                    continue
                try:
                    tokens = re.split(r"\s+", line)
                    if node_ip in tokens and str(edit_log_port) in tokens:
                        logger.info(f"✓ FE Follower节点 ({node_ip}:{edit_log_port}) 已在集群中",
                                    extra={"to_stdout": True})
                        return True
                except Exception as e:
                    logger.debug(f"解析FE节点行失败: {e}")

        logger.info(f"尝试将FE Follower节点 ({node_ip}:{edit_log_port}) 添加到集群 ({fe_host}:{fe_query_port})...",
                    extra={"to_stdout": True})

        if not self._wait_for_fe_ready(fe_host, fe_query_port, timeout=120, password=password):
            logger.error("FE未就绪，无法添加Follower节点", extra={"to_stdout": True})
            return False

        sql = f'ALTER SYSTEM ADD FOLLOWER "{node_ip}:{edit_log_port}";'
        success, msg = self._execute_sql(fe_host, fe_query_port, sql, password)
        if success:
            logger.info(f"✓ 成功将FE Follower节点添加到集群", extra={"to_stdout": True})
            return True
        else:
            stderr_lower = msg.lower() if msg else ""
            if "already exists" in stderr_lower or "duplicate" in stderr_lower:
                logger.info(f"FE Follower节点已存在于集群中", extra={"to_stdout": True})
                return True
            logger.error(f"添加FE Follower节点失败: {msg}", extra={"to_stdout": True})
            return False

    def _remove_fe_follower_from_cluster(self, fe_host: str, fe_query_port: int, node_ip: str, edit_log_port: int,
                                         password: Optional[str] = None) -> bool:
        """从集群中移除FE Follower节点（幂等操作）
        
        Args:
            fe_host: FE主机地址（必须是集群中其他可用的FE节点）
            fe_query_port: FE查询端口
            node_ip: 要移除的FE节点IP
            edit_log_port: 要移除的FE节点edit_log_port
            password: 密码
        
        Returns:
            bool: 移除成功返回True
        """
        # 检查节点是否在集群中
        sql = 'SHOW FRONTENDS;'
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password)
        if not success:
            logger.error(f"查询FE节点列表失败: {output}", extra={"to_stdout": True})
            return False
        
        node_in_cluster = False
        node_role = None
        total_fe_count = 0
        alive_fe_count = 0
        
        if output:
            lines = output.splitlines()
            for line in lines:
                line = line.strip()
                if not line or ("IP" in line and "EditLogPort" in line):
                    continue
                try:
                    tokens = re.split(r"\s+", line)
                    if len(tokens) >= 7:
                        fe_ip = tokens[2] if len(tokens) > 2 else ""
                        fe_port = tokens[3] if len(tokens) > 3 else ""
                        fe_role = tokens[6] if len(tokens) > 6 else ""
                        fe_alive = tokens[9] if len(tokens) > 9 else ""
                        
                        total_fe_count += 1
                        if fe_alive.lower() == "true":
                            alive_fe_count += 1
                        
                        if fe_ip == node_ip and fe_port == str(edit_log_port):
                            node_in_cluster = True
                            node_role = fe_role.upper()
                except Exception as e:
                    logger.debug(f"解析FE节点行失败: {e}")
        
        if not node_in_cluster:
            logger.info(f"FE节点 ({node_ip}:{edit_log_port}) 不在集群中，跳过移除", extra={"to_stdout": True})
            return True
        
        # 检查节点角色
        if node_role == "LEADER":
            logger.error(
                f"错误：不能直接移除LEADER节点 ({node_ip}:{edit_log_port})",
                extra={"to_stdout": True}
            )
            logger.error(
                "操作建议：",
                extra={"to_stdout": True}
            )
            logger.error(
                "  1. 如果这是最后一个节点，直接清理本地服务即可",
                extra={"to_stdout": True}
            )
            logger.error(
                "  2. 如果有其他FOLLOWER节点，请先等待自动选举新的LEADER，或手动执行 TRANSFER LEADER",
                extra={"to_stdout": True}
            )
            logger.error(
                "  3. 然后使用其他FE节点地址执行清理命令",
                extra={"to_stdout": True}
            )
            return False
        
        # 检查剩余节点数量（确保满足多数派要求）
        # StarRocks FE使用BDB JE的SIMPLE_MAJORITY策略，需要至少(n+1)/2个节点在线
        remaining_count = total_fe_count - 1
        if remaining_count > 0:
            min_required = (remaining_count + 1) // 2 + 1  # 多数派
            if alive_fe_count - 1 < min_required:
                logger.warning(
                    f"警告：移除节点后，集群将只剩下 {remaining_count} 个节点，"
                    f"需要至少 {min_required} 个节点在线才能正常工作",
                    extra={"to_stdout": True}
                )
                logger.warning(
                    f"当前在线节点数: {alive_fe_count}，移除后将只有 {alive_fe_count - 1} 个节点",
                    extra={"to_stdout": True}
                )
                logger.warning(
                    "这可能导致集群无法满足多数派要求，建议先确保有足够的节点在线",
                    extra={"to_stdout": True}
                )
                # 不阻止操作，但给出警告
        
        logger.info(f"正在从集群移除FE Follower节点: {node_ip}:{edit_log_port}", extra={"to_stdout": True})
        
        if not self._wait_for_fe_ready(fe_host, fe_query_port, timeout=120, password=password):
            logger.error("FE未就绪，无法移除Follower节点", extra={"to_stdout": True})
            return False
        
        sql = f'ALTER SYSTEM DROP FOLLOWER "{node_ip}:{edit_log_port}";'
        success, msg = self._execute_sql(fe_host, fe_query_port, sql, password)
        if success:
            logger.info(f"✓ 成功从集群移除FE Follower节点", extra={"to_stdout": True})
            return True
        else:
            stderr_lower = msg.lower() if msg else ""
            if "does not exist" in stderr_lower or "not found" in stderr_lower:
                logger.info(f"FE节点不在集群中，跳过移除", extra={"to_stdout": True})
                return True
            logger.error(f"移除FE Follower节点失败: {msg}", extra={"to_stdout": True})
            return False

    def _add_node_to_cluster(self, fe_host: str, fe_query_port: int, node_ip: str, node_port: int, node_type: str,
                             password: Optional[str] = None) -> bool:
        """将节点添加到集群（幂等操作）"""
        if self._check_node_in_cluster(fe_host, fe_query_port, node_ip, node_port, node_type, password):
            logger.info(f"✓ {node_type} 节点 ({node_ip}:{node_port}) 已在集群中", extra={"to_stdout": True})
            return True

        logger.info(f"尝试将 {node_type} 节点 ({node_ip}:{node_port}) 添加到 FE 集群 ({fe_host}:{fe_query_port})...",
                    extra={"to_stdout": True})

        if not self._wait_for_fe_ready(fe_host, fe_query_port, timeout=120, password=password):
            logger.error("FE未就绪，无法添加节点", extra={"to_stdout": True})
            return False

        mysql_client = shutil.which('mysql')
        if not mysql_client:
            logger.warning("未找到 mysql 客户端，无法自动添加节点", extra={"to_stdout": True})
            return False

        if node_type == "BE":
            sql = f'ALTER SYSTEM ADD BACKEND "{node_ip}:{node_port}";'
        else:
            sql = f'ALTER SYSTEM ADD COMPUTE NODE "{node_ip}:{node_port}";'

        cmd = [mysql_client, '-h', fe_host, '-P', str(fe_query_port), '-uroot', '-e', sql]
        if password:
            env = os.environ.copy()
            env['MYSQL_PWD'] = password
            result = run_command(cmd, check=False, capture_output=True, timeout=30, env=env)
        else:
            result = run_command(cmd, check=False, capture_output=True, timeout=30)

        if result.returncode == 0:
            logger.info(f"✓ 成功将 {node_type} 节点添加到集群", extra={"to_stdout": True})
            return True

        # 处理错误
        stderr = result.stderr.lower()

        # 角色冲突：如果添加CN时返回"Backend already exists"，说明已作为BE存在
        if node_type == "CN" and "backend already exists" in stderr:
            error_msg = (
                f"错误：节点 {node_ip}:{node_port} 已经作为 BE 节点存在于集群中，"
                f"不能同时作为 CN 节点。"
                f"\n请先移除BE节点：ALTER SYSTEM DROP BACKEND \"{node_ip}:{node_port}\";"
            )
            logger.error(error_msg, extra={"to_stdout": True})
            raise ValueError(error_msg)

        # 如果添加BE时返回"Compute node already exists"，说明已作为CN存在
        if node_type == "BE" and "compute node already exists" in stderr:
            error_msg = (
                f"错误：节点 {node_ip}:{node_port} 已经作为 CN 节点存在于集群中，"
                f"不能同时作为 BE 节点。"
                f"\n请先移除CN节点：ALTER SYSTEM DROP COMPUTE NODE \"{node_ip}:{node_port}\";"
            )
            logger.error(error_msg, extra={"to_stdout": True})
            raise ValueError(error_msg)

        # 节点已存在（相同类型）
        if "already exists" in stderr or "duplicate" in stderr:
            logger.info(f"{node_type} 节点已存在于集群中", extra={"to_stdout": True})
            return True

        # 其他错误
        logger.error(f"添加节点失败: {result.stderr}", extra={"to_stdout": True})
        return False

    def _execute_sql(self, fe_host: str, fe_query_port: int, sql: str, password: Optional[str] = None) -> Tuple[
        bool, str]:
        """执行SQL命令"""
        mysql_client = shutil.which('mysql')
        if not mysql_client:
            return False, "未找到 mysql 客户端"

        cmd = [mysql_client, '-h', fe_host, '-P', str(fe_query_port), '-uroot', '-e', sql]

        try:
            if password:
                env = os.environ.copy()
                env['MYSQL_PWD'] = password
                result = run_command(cmd, check=False, capture_output=True, timeout=30, env=env)
            else:
                result = run_command(cmd, check=False, capture_output=True, timeout=30)

            if result.returncode == 0:
                return True, result.stdout.strip() if result.stdout else ""
            else:
                return False, result.stderr.strip() if result.stderr else ""
        except Exception as e:
            logger.error(f"执行SQL失败: {e}", extra={"to_stdout": True})
            return False, str(e)

    def setup_cluster(
            self,
            fe_host: str = "127.0.0.1",
            fe_query_port: int = 9030,
            root_password: Optional[str] = None,
            enable_profile: bool = False,
            enable_pipeline_engine: bool = True,
            parallel_fragment_exec_instance_num: int = 1,
            max_user_connections: int = 1000
    ) -> bool:
        """Post-deployment setup: 设置root密码、系统变量等"""
        logger.info("=== 开始Post-deployment设置 ===", extra={"to_stdout": True})

        # 1. 设置root密码
        if root_password:
            logger.info("设置root用户密码...", extra={"to_stdout": True})
            sql = f"SET PASSWORD = PASSWORD('{root_password}');"
            success, msg = self._execute_sql(fe_host, fe_query_port, sql)
            if success:
                logger.info("✓ root密码设置成功", extra={"to_stdout": True})
            else:
                logger.error(f"设置root密码失败: {msg}", extra={"to_stdout": True})
                return False
        else:
            logger.warning("未设置root密码，建议使用 --root-password 参数设置密码", extra={"to_stdout": True})

        # 2. 设置root用户属性（max_user_connections）
        logger.info("设置root用户属性...", extra={"to_stdout": True})
        sql = f"ALTER USER 'root' SET PROPERTIES (\"max_user_connections\" = \"{max_user_connections}\");"
        success, msg = self._execute_sql(fe_host, fe_query_port, sql, root_password)
        if success:
            logger.info(f"✓ 设置 root 用户 max_user_connections = {max_user_connections}", extra={"to_stdout": True})
        else:
            logger.warning(f"设置 root 用户属性失败: {msg}", extra={"to_stdout": True})

        # 3. 设置系统变量
        logger.info("设置系统变量...", extra={"to_stdout": True})
        system_vars = [
            (f"enable_profile", str(enable_profile).lower()),
            (f"enable_pipeline_engine", str(enable_pipeline_engine).lower()),
            (f"parallel_fragment_exec_instance_num", str(parallel_fragment_exec_instance_num))
        ]

        for var_name, var_value in system_vars:
            sql = f"SET GLOBAL {var_name} = {var_value};"
            success, msg = self._execute_sql(fe_host, fe_query_port, sql, root_password)
            if success:
                logger.info(f"✓ 设置 {var_name} = {var_value}", extra={"to_stdout": True})
            else:
                logger.warning(f"设置 {var_name} 失败: {msg}", extra={"to_stdout": True})

        logger.info("=== Post-deployment设置完成 ===", extra={"to_stdout": True})
        return True

    def show_cluster_status(self, fe_host: str = "127.0.0.1", fe_query_port: int = 9030,
                            password: Optional[str] = None) -> bool:
        """显示集群状态"""
        print("=== 集群状态 ===")

        # 显示FE节点
        print("\nFE节点:")
        sql = "SHOW FRONTENDS;"
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password)
        if success:
            if output:
                print(output)
            else:
                print("(无FE节点)")
        else:
            print(f"查询FE节点失败: {output}")
            logger.warning(f"查询FE节点失败: {output}", extra={"to_stdout": True})

        # 显示BE节点
        print("\nBE节点:")
        sql = "SHOW BACKENDS;"
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password)
        if success:
            if output:
                print(output)
            else:
                print("(无BE节点)")
        else:
            print(f"查询BE节点失败: {output}")
            logger.warning(f"查询BE节点失败: {output}", extra={"to_stdout": True})

        # 显示CN节点
        print("\nCN节点:")
        sql = "SHOW COMPUTE NODES;"
        success, output = self._execute_sql(fe_host, fe_query_port, sql, password)
        if success:
            if output:
                print(output)
            else:
                print("(无CN节点)")
        else:
            print(f"查询CN节点失败: {output}")
            logger.warning(f"查询CN节点失败: {output}", extra={"to_stdout": True})

        return True

    def deploy_be(
            self,
            storage_root_path: str,
            be_port: int = 9060,
            be_http_port: int = 8040,
            heartbeat_service_port: int = 9050,
            brpc_port: int = 8060,
            starlet_port: int = 9070,
            priority_networks: Optional[str] = None,
            java_home: Optional[str] = None,
            sys_log_level: str = "INFO",
            java_opts: Optional[str] = None,
            enable_systemd: bool = True,
            extra_config: Optional[Dict[str, Any]] = None,
            force: bool = False,
            fe_host: Optional[str] = None,
            fe_query_port: int = 9030,
            password: Optional[str] = None
    ) -> bool:
        """部署BE节点（幂等操作）"""
        logger.info("=== 开始部署BE节点 ===", extra={"to_stdout": True})

        # BE部署必须指定FE地址并可连接
        if not fe_host:
            logger.error("部署BE节点必须指定 --fe-host", extra={"to_stdout": True})
            return False
        if not self._ensure_fe_available(fe_host, fe_query_port, password):
            return False

        # 检查角色冲突
        if not self._check_role_conflicts_before_deploy("BE", fe_host, fe_query_port, heartbeat_service_port,
                                                        priority_networks, password):
            return False

        # 检查是否已存在部署并处理force和幂等性
        service_name = 'starrocks-be'
        skip_deployment, success = self._check_existing_deployment_and_handle_force(
            'be', service_name, force, enable_systemd, fe_host, fe_query_port, password
        )
        if not success:
            return False

        # 如果跳过部署，直接执行后续操作
        if not skip_deployment:
            # 准备存储目录
            if not self._prepare_storage_directories(storage_root_path):
                return False

            # 验证端口
            if not self._validate_ports([be_port, be_http_port, heartbeat_service_port, brpc_port, starlet_port]):
                return False

            # 验证priority_networks
            if not self._validate_priority_networks(priority_networks):
                return False

            # 验证Java
            is_valid, java_home = self._validate_java_home_complete(java_home)
            if not is_valid:
                return False

            # 确保BE目录权限
            be_dir = self.starrocks_home / "be"
            if not self._ensure_directory_permissions(be_dir, recursive=True):
                logger.warning(f"无法设置BE目录权限: {be_dir}，可能会导致启动失败", extra={"to_stdout": True})

            # 生成配置和服务
            config_content = ConfigGenerator.generate_be_config(
                storage_root_path=storage_root_path,
                be_port=be_port,
                be_http_port=be_http_port,
                heartbeat_service_port=heartbeat_service_port,
                brpc_port=brpc_port,
                starlet_port=starlet_port,
                priority_networks=priority_networks,
                java_home=java_home,
                sys_log_level=sys_log_level,
                java_opts=java_opts,
                extra_config=extra_config
            )

            service_content = None
            if enable_systemd:
                service_content = SystemdServiceGenerator.generate_be_service(
                    starrocks_home=str(self.starrocks_home),
                    user=self.user,
                    group=self.group,
                    java_home=java_home
                )

            if not self._deploy_be_cn_config_and_service(
                    "BE", config_content, service_content, "starrocks-be", "start_be.sh",
                    self.starrocks_home / "be" / "log"
            ):
                return False

        # 自动添加到集群（幂等操作）
        if not self._add_node_to_cluster_with_wait(
                "BE", fe_host, fe_query_port, heartbeat_service_port, priority_networks,
                password, skip_deployment, enable_systemd, [be_port, heartbeat_service_port]
        ):
            return False

        return True

    def _prepare_storage_directories(self, storage_root_path: str) -> bool:
        """
        准备存储目录（创建、设置权限、验证可写性）
        
        Returns:
            是否成功
        """
        if not InputValidator.validate_storage_path(storage_root_path):
            logger.error(f"无效的存储路径格式: {storage_root_path}", extra={"to_stdout": True})
            logger.error("格式应为: /path1,medium:HDD;/path2,medium:SSD;/path3", extra={"to_stdout": True})
            return False

        paths = storage_root_path.split(';')
        for path_spec in paths:
            path_spec = path_spec.strip()
            if not path_spec:
                continue

            # 提取路径部分
            if ',' in path_spec:
                path_part = path_spec.split(',')[0].strip()
            else:
                path_part = path_spec

            storage_path = Path(path_part)
            if not storage_path.exists():
                try:
                    storage_path.mkdir(parents=True, exist_ok=True, mode=0o755)
                    logger.info(f"创建存储目录: {path_part}", extra={"to_stdout": True})
                except Exception as e:
                    logger.error(f"无法创建存储目录 {path_part}: {e}", extra={"to_stdout": True})
                    return False

            # 设置目录权限和所有者
            if not self._ensure_directory_permissions(storage_path, recursive=True):
                return False

            if not EnvironmentChecker.check_directory_writable(path_part):
                logger.error(f"存储目录不可写: {path_part}", extra={"to_stdout": True})
                return False

            # 检查目录独立性
            if not EnvironmentChecker.check_directory_independent(self.starrocks_home, path_part):
                logger.warning(f"存储目录 {path_part} 位于安装目录内，建议使用独立目录以提高性能和可靠性",
                               extra={"to_stdout": True})

        return True

    def _check_role_conflicts_before_deploy(
            self,
            node_type: str,
            fe_host: Optional[str],
            fe_query_port: int,
            heartbeat_service_port: int,
            priority_networks: Optional[str],
            password: Optional[str]
    ) -> bool:
        """
        部署前检查角色冲突（本地冲突和集群冲突）
        
        Args:
            node_type: 节点类型 ("BE" 或 "CN")
            fe_host: FE主机地址
            fe_query_port: FE查询端口
            heartbeat_service_port: 心跳端口
            priority_networks: 优先网络
            password: 密码
        
        Returns:
            如果无冲突返回True，否则返回False
        """
        # 检查本地角色冲突
        conflict_role = 'cn' if node_type == 'BE' else 'be'
        conflict, details, warnings = self._detect_local_role_conflict(conflict_role)
        if conflict:
            logger.error(f"检测到{conflict_role.upper()}已在本节点部署或运行，禁止与{node_type}共存",
                         extra={"to_stdout": True})
            for detail in details:
                logger.error(f"  - {detail}", extra={"to_stdout": True})
            return False
        if warnings:
            logger.warning(f"检测到{conflict_role.upper()}相关残留，但未发现运行实例，继续部署",
                           extra={"to_stdout": True})
            for detail in warnings:
                logger.warning(f"  - {detail}", extra={"to_stdout": True})

        # 检查集群角色冲突
        if fe_host:
            node_ip = self._get_local_ip(fe_host, fe_query_port, priority_networks)
            if node_ip and node_ip != "127.0.0.1":
                conflict_type = self._check_role_conflict(fe_host, fe_query_port, node_ip, heartbeat_service_port,
                                                          node_type, password)
                if conflict_type:
                    error_msg = (
                        f"错误：节点 {node_ip}:{heartbeat_service_port} 已经作为 {conflict_type} 节点存在于集群中，"
                        f"不能同时作为 {node_type} 节点。"
                        f"\n请先移除{conflict_type}节点："
                        f"\n  ALTER SYSTEM DROP {'BACKEND' if conflict_type == 'BE' else 'COMPUTE NODE'} \"{node_ip}:{heartbeat_service_port}\";"
                    )
                    logger.error(error_msg, extra={"to_stdout": True})
                    return False

        return True

    def _deploy_be_cn_config_and_service(
            self,
            node_type: str,
            config_content: str,
            service_content: Optional[str],
            service_name: str,
            start_script_name: str,
            log_dir: Optional[Path] = None
    ) -> bool:
        """
        BE/CN配置和服务文件部署的公共逻辑
        
        Returns:
            是否成功
        """
        # 生成配置文件
        conf_path = self.starrocks_home / "be" / "conf" / f"{node_type.lower()}.conf"
        if not conf_path.parent.exists():
            logger.error(f"{node_type}配置目录不存在: {conf_path.parent}", extra={"to_stdout": True})
            return False

        # 写入配置文件
        if not self._write_config_file(conf_path, config_content, node_type):
            return False

        # 确保日志目录权限
        if log_dir and log_dir.exists():
            if not self._ensure_directory_permissions(log_dir, recursive=False):
                logger.warning(f"无法设置{node_type}日志目录权限: {log_dir}", extra={"to_stdout": True})

        # 生成systemd服务文件
        if service_content:
            service_path = Path(f"/etc/systemd/system/{service_name}.service")
            self._create_systemd_service(service_content, service_path)

        start_cmd = f"{self.starrocks_home / 'be' / 'bin' / start_script_name} --daemon"
        self._log_deployment_completion(node_type, start_cmd, service_name, service_content is not None)

        return True

    def _print_add_cluster_hint(self, node_type: str, node_ip: str, node_port: int, fe_host: Optional[str] = None,
                                fe_query_port: int = 9030) -> None:
        """打印加入集群的友好提示"""
        logger.info("", extra={"to_stdout": True})
        logger.info("=" * 60, extra={"to_stdout": True})
        logger.info(f"提示: {node_type} 节点已部署，但尚未加入集群", extra={"to_stdout": True})
        logger.info("=" * 60, extra={"to_stdout": True})
        if fe_host:
            logger.info(f"要将此 {node_type} 节点加入集群，请执行以下命令之一:", extra={"to_stdout": True})
            logger.info("", extra={"to_stdout": True})
            logger.info("方法1: 使用 starcli 命令（推荐）:", extra={"to_stdout": True})
            logger.info(f"  starcli --deploy {node_type.lower()} \\", extra={"to_stdout": True})
            logger.info(f"    --starrocks-home {self.starrocks_home} \\", extra={"to_stdout": True})
            logger.info(f"    --fe-host {fe_host} \\", extra={"to_stdout": True})
            logger.info(f"    --fe-query-port {fe_query_port}", extra={"to_stdout": True})
            logger.info("", extra={"to_stdout": True})
            logger.info("方法2: 使用 MySQL 客户端手动添加:", extra={"to_stdout": True})
            logger.info(f"  mysql -h {fe_host} -P {fe_query_port} -uroot -e \\", extra={"to_stdout": True})
        else:
            logger.info(f"要将此 {node_type} 节点加入集群，请执行以下命令之一:", extra={"to_stdout": True})
            logger.info("", extra={"to_stdout": True})
            logger.info("方法1: 使用 starcli 命令（推荐）:", extra={"to_stdout": True})
            logger.info(f"  starcli --deploy {node_type.lower()} \\", extra={"to_stdout": True})
            logger.info(f"    --starrocks-home {self.starrocks_home} \\", extra={"to_stdout": True})
            logger.info(f"    --storage-root-path <YOUR_STORAGE_PATH> \\", extra={"to_stdout": True})
            logger.info(f"    --fe-host <FE_HOST> \\", extra={"to_stdout": True})
            logger.info(f"    --fe-query-port <FE_QUERY_PORT>", extra={"to_stdout": True})
            logger.info("", extra={"to_stdout": True})
            logger.info("方法2: 使用 MySQL 客户端手动添加:", extra={"to_stdout": True})
            logger.info(f"  mysql -h <FE_HOST> -P <FE_QUERY_PORT> -uroot -e \\", extra={"to_stdout": True})

        if node_type == "BE":
            logger.info(f"    'ALTER SYSTEM ADD BACKEND \"{node_ip}:{node_port}\";'", extra={"to_stdout": True})
        else:
            logger.info(f"    'ALTER SYSTEM ADD COMPUTE NODE \"{node_ip}:{node_port}\";'", extra={"to_stdout": True})
        logger.info("", extra={"to_stdout": True})
        logger.info("=" * 60, extra={"to_stdout": True})

    def deploy_cn(
            self,
            be_port: int = 9060,
            be_http_port: int = 8040,
            heartbeat_service_port: int = 9050,
            brpc_port: int = 8060,
            priority_networks: Optional[str] = None,
            java_home: Optional[str] = None,
            sys_log_level: str = "INFO",
            java_opts: Optional[str] = None,
            enable_systemd: bool = True,
            extra_config: Optional[Dict[str, Any]] = None,
            force: bool = False,
            fe_host: Optional[str] = None,
            fe_query_port: int = 9030,
            password: Optional[str] = None
    ) -> bool:
        """部署CN节点（幂等操作）"""
        logger.info("=== 开始部署CN节点 ===", extra={"to_stdout": True})

        # CN部署必须指定FE地址并可连接
        if not fe_host:
            logger.error("部署CN节点必须指定 --fe-host", extra={"to_stdout": True})
            return False
        if not self._ensure_fe_available(fe_host, fe_query_port, password):
            return False

        # 检查角色冲突
        if not self._check_role_conflicts_before_deploy("CN", fe_host, fe_query_port, heartbeat_service_port,
                                                        priority_networks, password):
            return False

        # 检查是否已存在部署并处理force和幂等性
        service_name = 'starrocks-cn'
        skip_deployment, success = self._check_existing_deployment_and_handle_force(
            'cn', service_name, force, enable_systemd, fe_host, fe_query_port, password
        )
        if not success:
            return False

        # 如果跳过部署，直接执行后续操作
        if not skip_deployment:
            # 验证端口
            if not self._validate_ports([be_port, be_http_port, heartbeat_service_port, brpc_port]):
                return False

            # 验证priority_networks
            if not self._validate_priority_networks(priority_networks):
                return False

            # 验证Java
            is_valid, java_home = self._validate_java_home_complete(java_home)
            if not is_valid:
                return False

            # 确保BE目录权限（CN也使用be目录）
            be_dir = self.starrocks_home / "be"
            if not self._ensure_directory_permissions(be_dir, recursive=True):
                logger.warning(f"无法设置BE目录权限: {be_dir}，可能会导致启动失败", extra={"to_stdout": True})

            # 生成配置和服务
            config_content = ConfigGenerator.generate_cn_config(
                be_port=be_port,
                be_http_port=be_http_port,
                heartbeat_service_port=heartbeat_service_port,
                brpc_port=brpc_port,
                priority_networks=priority_networks,
                java_home=java_home,
                sys_log_level=sys_log_level,
                java_opts=java_opts,
                extra_config=extra_config
            )

            service_content = None
            if enable_systemd:
                service_content = SystemdServiceGenerator.generate_cn_service(
                    starrocks_home=str(self.starrocks_home),
                    user=self.user,
                    group=self.group,
                    java_home=java_home
                )

            if not self._deploy_be_cn_config_and_service(
                    "CN", config_content, service_content, "starrocks-cn", "start_cn.sh",
                    self.starrocks_home / "be" / "log"
            ):
                return False

        # 自动添加到集群（幂等操作）
        if not self._add_node_to_cluster_with_wait(
                "CN", fe_host, fe_query_port, heartbeat_service_port, priority_networks,
                password, skip_deployment, enable_systemd, [be_port, heartbeat_service_port]
        ):
            return False

        return True

    def verify_fe_started(self, timeout: int = 60) -> bool:
        """验证FE节点是否启动成功"""
        logger.info("检查FE节点启动状态...", extra={"to_stdout": True})
        fe_log_path = self.starrocks_home / "fe" / "log" / "fe.log"

        if not fe_log_path.exists():
            logger.warning("FE日志文件不存在，可能尚未启动", extra={"to_stdout": True})
            return False

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with open(fe_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    if 'thrift server started' in content:
                        logger.info("✓ FE节点启动成功", extra={"to_stdout": True})
                        return True
            except Exception as e:
                logger.debug(f"读取日志文件失败: {e}", extra={'skip_file': True})

            time.sleep(2)

        logger.warning("FE节点启动验证超时", extra={"to_stdout": True})
        return False

    def verify_be_started(self, timeout: int = 60) -> bool:
        """验证BE节点是否启动成功"""
        logger.info("检查BE节点启动状态...", extra={"to_stdout": True})
        be_log_path = self.starrocks_home / "be" / "log" / "be.INFO"

        if not be_log_path.exists():
            logger.warning("BE日志文件不存在，可能尚未启动", extra={"to_stdout": True})
            return False

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                with open(be_log_path, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read()
                    if 'heartbeat' in content.lower():
                        logger.info("✓ BE节点启动成功", extra={"to_stdout": True})
                        return True
            except Exception as e:
                logger.debug(f"读取日志文件失败: {e}", extra={'skip_file': True})

            time.sleep(2)

        logger.warning("BE节点启动验证超时", extra={"to_stdout": True})
        return False


def load_json_config(config_file: str) -> Dict[str, Any]:
    """加载JSON配置文件"""
    config_path = Path(config_file)
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_file}", extra={"to_stdout": True})
        sys.exit(1)

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"✓ 加载配置文件: {config_file}", extra={"to_stdout": True})
        return config
    except json.JSONDecodeError as ex:
        logger.error(f"配置文件格式错误: {ex}", extra={"to_stdout": True})
        sys.exit(1)
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}", extra={"to_stdout": True})
        sys.exit(1)


def _parse_target_host(target_host: str, default_port: int) -> Tuple[str, int]:
    """解析 target_host（支持 host:port 形式）"""
    host = target_host
    port = default_port
    if target_host and ":" in target_host:
        parts = target_host.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            host = parts[0]
            port = int(parts[1])

    # 校验主机地址
    if host and not InputValidator.validate_hostname(host):
        raise ValueError(f"无效的target_host主机地址格式: {host}")

    # 校验端口号
    if not InputValidator.validate_port(port):
        raise ValueError(f"无效的target_host端口号: {port}")

    return host, port


def _is_local_host(host: str) -> bool:
    """判断是否为本机地址"""
    if not host:
        return True
    host_lower = host.lower()
    if host_lower in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        local_names = {socket.gethostname().lower(), socket.getfqdn().lower()}
        return host_lower in local_names
    except Exception as e:
        logger.error(f"failed to detect local host {e}", extra={"to_stdout": True})
        return False


def _build_remote_command(argv: List[str], remote_config_path: Optional[str],
                          remote_script: Optional[str] = None) -> str:
    """构建远程执行命令（过滤本地远程相关参数）"""
    filtered: List[str] = []
    skip_next = False
    flags_with_value = {"--target-host", "--ssh-user", "--ssh-port", "--ssh-key", "--remote-workdir", "--config"}
    flags_no_value = {"--disable-ssh-host-check"}
    for i, arg in enumerate(argv[1:]):
        if skip_next:
            skip_next = False
            continue
        if arg in flags_no_value:
            continue
        if arg in flags_with_value:
            if arg == "--config":
                skip_next = True
                if remote_config_path:
                    filtered.extend(["--config", remote_config_path])
            else:
                skip_next = True
            continue
        filtered.append(arg)

    if remote_script:
        return f"python3 {shlex.quote(remote_script)} " + " ".join(shlex.quote(a) for a in filtered)
    else:
        return "starcli " + " ".join(shlex.quote(a) for a in filtered)


def _run_remote_deploy(
        target_host: str,
        args: argparse.Namespace,
        config: Dict[str, Any],
        argv: List[str]
) -> None:
    """通过SSH在远程主机执行部署（使用标准化的starcli命令）"""
    ssh_user = config.get("ssh_user") or args.ssh_user
    ssh_port_default = config.get("ssh_port") or args.ssh_port or 22
    ssh_key = config.get("ssh_key") or args.ssh_key
    strict_host_key_checking = not (config.get("disable_ssh_host_check") or args.disable_ssh_host_check)
    remote_workdir = config.get("remote_workdir") or args.remote_workdir or "/tmp/starrocks_deploy"

    host, port = _parse_target_host(target_host, ssh_port_default)

    try:
        ssh = SSHManager(
            host=host,
            port=port,
            username=ssh_user,
            key_path=ssh_key,
            strict_host_key_checking=strict_host_key_checking
        )
    except Exception as e:
        logger.error(f"初始化SSH失败: {e}", extra={"to_stdout": True})
        sys.exit(1)

    check_cmd = "command -v starcli"
    success, _ = ssh.run_command(check_cmd, timeout=20)
    remote_script = None

    if not success:
        logger.info(f"远程主机 {host}:{port} 上未找到 starcli 命令，将复制本地脚本到远程", extra={"to_stdout": True})

        local_script_path = None
        if __file__ and Path(__file__).exists():
            local_script_path = Path(__file__).resolve()
        else:
            which_starcli = shutil.which('starcli')
            if which_starcli and Path(which_starcli).exists():
                local_script_path = Path(which_starcli).resolve()

        if not local_script_path or not local_script_path.exists():
            logger.error("无法确定本地 starcli 脚本路径", extra={"to_stdout": True})
            logger.error("请确保脚本文件存在，或使用 --target-host 前先安装 starcli 到系统 PATH",
                         extra={"to_stdout": True})
            sys.exit(1)

        success, output = ssh.run_command(f"mkdir -p {remote_workdir}", timeout=20)
        if not success:
            logger.error(f"创建远程目录失败: {output}", extra={"to_stdout": True})
            sys.exit(1)

        remote_script = f"{remote_workdir}/starrocks_deploy.py"
        if not ssh.copy_file(str(local_script_path), remote_script):
            logger.error("复制脚本到远程失败", extra={"to_stdout": True})
            sys.exit(1)

        chmod_cmd = f"chmod +x {remote_script}"
        success, _ = ssh.run_command(chmod_cmd, timeout=10)
        if not success:
            logger.warning(f"设置远程脚本执行权限失败，但继续执行", extra={"to_stdout": True})

        logger.info(f"已复制脚本到远程: {remote_script}", extra={"to_stdout": True})
    else:
        test_cmd = "starcli --help 2>&1 | head -5 || true"
        success, test_output = ssh.run_command(test_cmd, timeout=20)
        if not success:
            logger.warning(f"远程 starcli 命令测试失败: {test_output}", extra={"to_stdout": True})
        else:
            logger.debug(f"远程 starcli 命令可用", extra={"to_stdout": False})

    remote_config_path = None
    if args.config:
        if not remote_script:
            success, output = ssh.run_command(f"mkdir -p {remote_workdir}", timeout=20)
            if not success:
                logger.error(f"创建远程目录失败: {output}", extra={"to_stdout": True})
                sys.exit(1)

        remote_config_path = f"{remote_workdir}/config.json"
        if not ssh.copy_file(str(Path(args.config).resolve()), remote_config_path):
            logger.error("复制配置文件到远程失败", extra={"to_stdout": True})
            sys.exit(1)

    remote_cmd = _build_remote_command(argv, remote_config_path, remote_script)
    logger.info(f"正在远程主机 {host}:{port} 执行部署...", extra={"to_stdout": True})
    logger.info(f"远程执行命令: {remote_cmd}", extra={"to_stdout": True})
    # 使用 2>&1 确保捕获所有输出（包括错误输出）
    remote_cmd_with_redirect = f"{remote_cmd} 2>&1"
    success, output = ssh.run_command(remote_cmd_with_redirect, timeout=3600)
    if success:
        if output:
            logger.info(output.strip(), extra={"to_stdout": True})
        sys.exit(0)
    # 输出详细错误信息（完整输出，不截断）
    if output:
        # 完整输出错误信息，按行输出以确保所有内容都显示
        error_lines = output.strip().split('\n')
        logger.error("远程执行失败，完整错误信息：", extra={"to_stdout": True})
        for line in error_lines:
            logger.error(f"  {line}", extra={"to_stdout": True})
    else:
        logger.error("远程执行失败（无错误输出）", extra={"to_stdout": True})
    logger.error(f"执行的命令: {remote_cmd}", extra={"to_stdout": True})
    logger.error(f"目标主机: {host}:{port} (用户: {ssh_user})", extra={"to_stdout": True})
    # 尝试获取更详细的错误信息
    if not output:
        logger.error("提示：如果远程主机上 starcli 命令执行失败，请检查：", extra={"to_stdout": True})
        logger.error("  1. starcli 是否已正确安装到系统 PATH", extra={"to_stdout": True})
        logger.error("  2. 远程主机上执行 'starcli --help' 是否正常", extra={"to_stdout": True})
        logger.error("  3. 检查远程主机的日志或错误输出", extra={"to_stdout": True})
    sys.exit(1)


def show_examples():
    """显示使用示例"""
    examples = """
使用示例:

1. 部署单个FE节点（Leader）:
   starcli \\
        --deploy fe \\
        --starrocks-home /opt/starrocks \\
        --meta-dir /data/starrocks/fe/meta

2. 部署FE节点（Follower，加入已有集群）:
   starcli \\
        --deploy fe \\
        --starrocks-home /opt/starrocks \\
        --meta-dir /data/starrocks/fe/meta \\
        --helper-address 192.168.1.100:9010 \\
        --root-password your_password
   # 说明: --helper-address 指定已有FE集群中任意一个FE节点（通常是Leader）的地址和edit_log_port
   #       新Follower节点将通过此地址连接到集群，同步元数据并加入FE集群
   #       脚本会自动执行 ALTER SYSTEM ADD FOLLOWER 将Follower添加到集群
   #       如果FE集群已设置密码，必须提供 --root-password 参数
   #       格式: host:port (host为FE节点IP或主机名，port为edit_log_port，默认9010)
   #       仅在多FE节点集群部署Follower节点时需要，单FE节点或Leader节点部署时不需要指定

3. 部署BE节点（单磁盘，必须指定 --fe-host）:
   starcli \\
        --deploy be \\
        --starrocks-home /opt/starrocks \\
        --storage-root-path /data/starrocks/be/storage \\
        --fe-host 192.168.1.100 \\
        --root-password your_password

4. 部署BE节点（多磁盘，指定HDD/SSD）:
   starcli \\
        --deploy be \\
        --starrocks-home /opt/starrocks \\
        --storage-root-path "/data1,medium:HDD;/data2,medium:SSD;/data3,medium:HDD" \\
        --fe-host 192.168.1.100 \\
        --root-password your_password

5. 部署BE节点（指定网络和Java路径）:
   starcli \\
        --deploy be \\
        --starrocks-home /opt/starrocks \\
        --storage-root-path "/data1,medium:HDD;/data2,medium:SSD" \\
        --priority-networks 192.168.1.0/24 \\
        --java-home /usr/lib/jvm/java-17-openjdk \\
        --fe-host 192.168.1.100 \\
        --root-password your_password

6. 部署CN节点（必须指定 --fe-host）:
   starcli \\
        --deploy cn \\
        --starrocks-home /opt/starrocks \\
        --priority-networks 192.168.1.0/24 \\
        --fe-host 192.168.1.100 \\
        --root-password your_password

7. 使用JSON配置文件部署:
   # config.json 示例（支持所有参数）:
   # {
   #   "starrocks_home": "/opt/starrocks",
   #   "deploy": "fe",
   #   "meta_dir": "/data/starrocks/fe/meta",
   #   "http_port": 8030,
   #   "rpc_port": 9020,
   #   "query_port": 9030,
   #   "edit_log_port": 9010,
   #   "priority_networks": "192.168.1.0/24",
   #   "java_home": "/usr/lib/jvm/java-17-openjdk",
   #   "java_opts": "--add-opens=java.base/java.util=ALL-UNNAMED",
   #   "sys_log_level": "INFO",
   #   "enable_systemd": true,
   #   "fe_host": "192.168.1.100",
   #   "fe_query_port": 9030,
   #   "target_host": "10.0.0.20",
   #   "ssh_user": "root",
   #   "ssh_port": 22,
   #   "ssh_key": "~/.ssh/id_rsa",
   #   "disable_ssh_host_check": false,
   #   "remote_workdir": "/tmp/starrocks_deploy",
   #   "root_password": "your_password",
   #   "enable_profile": false,
   #   "enable_pipeline_engine": true,
   #   "parallel_fragment_exec_instance_num": 1,
   #   "max_user_connections": 1000
   # }
   
   starcli --config config.json

8. 禁用systemd服务管理:
   starcli \\
        --deploy be \\
        --starrocks-home /opt/starrocks \\
        --storage-root-path /data/starrocks/be/storage \\
        --fe-host 192.168.1.100 \\
        --root-password your_password \\
        --no-systemd

9. 部署FE节点（FQDN模式）:
   starcli \\
        --deploy fe \\
        --starrocks-home /opt/starrocks \\
        --meta-dir /data/starrocks/fe/meta \\
        --use-fqdn

10. 单BE节点集群部署（FE需要设置default_replication_num）:
    starcli \\
         --deploy fe \\
         --starrocks-home /opt/starrocks \\
         --meta-dir /data/starrocks/fe/meta \\
         --default-replication-num 1

11. 强制覆盖已存在的部署（自动清理后重新部署）:
    starcli \\
         --deploy fe \\
         --starrocks-home /opt/starrocks \\
         --meta-dir /data/starrocks/fe/meta \\
         --force

12. 清理已部署的服务和配置（不进行部署）:
    # 清理FE节点（必须指定 --fe-host，会先从集群移除节点，然后清理本地服务）:
    # 注意：FE节点使用BDB JE的SIMPLE_MAJORITY策略，需要满足多数派要求
    #       对于3节点集群，移除节点后需要至少2个节点在线才能正常工作
    #       不能直接移除LEADER节点，需要先等待自动选举或手动执行 TRANSFER LEADER
    starcli \\
         --deploy fe \\
         --starrocks-home /opt/starrocks \\
         --fe-host 192.168.1.100 \\
         --root-password your_password \\
         --clean
    
    # 清理BE节点（必须指定 --fe-host，如果节点在集群中会先从集群移除）:
    starcli \\
         --deploy be \\
         --starrocks-home /opt/starrocks \\
         --fe-host 192.168.1.100 \\
         --root-password your_password \\
         --clean
    
    # 清理CN节点（必须指定 --fe-host，如果节点在集群中会先从集群移除）:
    starcli \\
         --deploy cn \\
         --starrocks-home /opt/starrocks \\
         --fe-host 192.168.1.100 \\
         --root-password your_password \\
         --clean

13. 部署BE节点并自动加入集群:
    starcli \\
         --deploy be \\
         --starrocks-home /opt/starrocks \\
         --storage-root-path /data/starrocks/be/storage \\
         --fe-host 192.168.1.100 \\
         --root-password your_password

14. 部署后执行Post-deployment设置（设置root密码和系统变量）:
    starcli \\
         --deploy fe \\
         --starrocks-home /opt/starrocks \\
         --meta-dir /data/starrocks/fe/meta \\
         --setup \\
         --root-password your_password

15. 查看集群状态:
    starcli \\
         --status \\
         --fe-host 192.168.1.100 \\
         --root-password your_password

16. 幂等性使用示例（重复运行相同命令，已部署则跳过）:
    # 第一次运行：部署BE节点（必须指定 --fe-host，会自动加入集群）
    starcli \\
         --deploy be \\
         --starrocks-home /opt/starrocks \\
         --storage-root-path /data/starrocks/be/storage \\
         --fe-host 192.168.1.100 \\
         --root-password your_password
    
    # 第二次运行相同命令：检测到已部署，跳过部署步骤，但会检查并确保节点在集群中
    # 如果节点已在集群中则跳过，如果不在集群中则自动加入（幂等性保证）
    starcli \\
         --deploy be \\
         --starrocks-home /opt/starrocks \\
         --storage-root-path /data/starrocks/be/storage \\
         --fe-host 192.168.1.100 \\
         --root-password your_password

17. 远程部署BE节点（默认本地，指定远程主机）:
   # 注意：远程主机必须已安装 starcli 到系统 PATH（如 /usr/local/bin/starcli）
   starcli \\
        --deploy be \\
        --starrocks-home /opt/starrocks \\
        --storage-root-path /data/starrocks/be/storage \\
        --fe-host 192.168.1.100 \\
        --target-host 10.0.0.20 \\
        --ssh-user root \\
        --ssh-key ~/.ssh/id_rsa

参数说明:
  --deploy TYPE                 部署类型 [fe|be|cn]
  --starrocks-home PATH         StarRocks安装目录
  --config FILE                 使用JSON配置文件
  --meta-dir PATH               FE元数据目录
  --storage-root-path PATH      BE存储路径（支持多磁盘: /path1,medium:HDD;/path2,medium:SSD）
  --http-port PORT              FE HTTP端口 (默认: 8030)
  --rpc-port PORT               FE RPC端口 (默认: 9020)
  --query-port PORT             FE查询端口 (默认: 9030)
  --edit-log-port PORT          FE编辑日志端口 (默认: 9010)
  --be-port PORT                BE端口 (默认: 9060)
  --be-http-port PORT           BE HTTP端口 (默认: 8040)
  --heartbeat-port PORT         BE心跳端口 (默认: 9050)
  --brpc-port PORT              BE brpc端口 (默认: 8060)
  --starlet-port PORT           BE starlet端口 (默认: 9070)
  --priority-networks CIDR      网络CIDR (如: 192.168.1.0/24)
  --java-home PATH              Java安装路径
  --helper-address ADDR:PORT    指定已有FE集群中任意一个FE节点（通常是Leader）的地址和edit_log_port
                                新Follower节点将通过此地址连接到集群，同步元数据并加入FE集群
                                格式: host:port (host为FE节点IP或主机名，port为edit_log_port，默认9010)
                                仅在多FE节点集群部署Follower节点时需要，单FE节点或Leader节点部署时不需要指定
  --use-fqdn                    使用FQDN模式
  --default-replication-num NUM 默认副本数（单BE节点需设为1）
  --user USER                   运行用户 (默认: starrocks)
  --group GROUP                 运行组 (默认: starrocks)
  --no-systemd                  禁用systemd服务管理
  --verify                      部署后验证服务启动
  --force                       强制覆盖已存在的部署（自动清理后重新部署）
  --clean                       清理已部署的服务和配置（不进行部署）
  --fe-host HOST                FE节点地址（用于自动将BE/CN加入集群）
  --fe-query-port PORT          FE查询端口 (默认: 9030)
  --target-host HOST            远程部署目标主机（支持 host:port）
  --ssh-user USER               SSH用户名 (默认: root)
  --ssh-port PORT               SSH端口 (默认: 22)
  --ssh-key PATH                SSH私钥路径 (默认: ~/.ssh/id_rsa)
  --disable-ssh-host-check      禁用SSH主机密钥检查（不推荐）
  --remote-workdir PATH         远程工作目录 (默认: /tmp/starrocks_deploy)
  --setup                       执行Post-deployment设置（设置root密码、系统变量等）
  --root-password PWD           设置root用户密码（用于Post-deployment设置）
  --status                      显示集群状态（FE/BE/CN节点）

注意:
  - 部署前请确保已安装JDK 17或更高版本（StarRocks 3.5+要求）
  - 确保所有端口未被占用
  - 多磁盘配置格式: /path1,medium:HDD;/path2,medium:SSD;/path3
  - systemd服务管理需要root权限
  - FE Follower节点部署时会自动添加到集群（如果已设置密码，需要提供 --root-password）
  - 单BE节点部署时，必须在FE配置中设置 default_replication_num = 1
  - 脚本具有幂等性：如果服务已部署且正常运行，重复运行会跳过部署步骤，只执行后续操作（如加入集群）
  - 如需强制重新部署，请使用 --force 选项（会自动清理后重新部署）
  - 使用 --clean 可以完全清理已部署的服务和配置（配置文件会自动备份）
  - 部署BE/CN必须指定 --fe-host，并确保FE可达（代码强制要求，缺少会报错）
  - 清理FE节点时必须指定 --fe-host，会先从集群移除节点（ALTER SYSTEM DROP FOLLOWER），然后清理本地服务
  - FE节点使用BDB JE的SIMPLE_MAJORITY策略，需要满足多数派要求（3节点需要至少2个在线）
  - 不能直接移除LEADER节点，需要先等待自动选举或手动执行 TRANSFER LEADER
  - 清理FE节点时必须指定 --fe-host，会先从集群移除节点（ALTER SYSTEM DROP FOLLOWER），然后清理本地服务
  - FE节点使用BDB JE的SIMPLE_MAJORITY策略，需要满足多数派要求（3节点需要至少2个在线）
  - 不能直接移除LEADER节点，需要先等待自动选举或手动执行 TRANSFER LEADER
  - 清理BE/CN节点时必须指定 --fe-host，如果节点在集群中会先从集群移除
  - 清理FE节点时，如果集群中还有BE/CN节点，会拒绝清理并提示先清理BE/CN节点
  - 使用 --setup 可以自动执行Post-deployment设置（设置root密码、系统变量等）
  - 使用 --status 可以查看集群状态（需要提供 --root-password 如果已设置密码）
  - 部分高级参数（如 sys_log_level, java_opts, enable_profile 等）只能通过JSON配置文件设置
  - 所有参数都支持通过JSON配置文件设置，配置文件优先级低于命令行参数
  - 默认本地部署，指定 --target-host 后将在远程主机执行部署
  - 远程部署：如果远程主机没有 starcli，脚本会自动复制本地脚本到远程并执行
  - 如果FE集群已设置密码，所有需要连接FE的操作（部署BE/CN、查看状态、清理等）都需要提供 --root-password
"""
    print(examples)


def main():
    parser = argparse.ArgumentParser(
        description="StarRocks自动化部署工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False
    )

    parser.add_argument(
        "-h", "--help",
        action="store_true",
        help="显示详细使用说明和示例"
    )

    parser.add_argument(
        "--deploy",
        choices=["fe", "be", "cn"],
        help="部署类型: fe (Frontend), be (Backend), cn (Compute Node)"
    )

    parser.add_argument(
        "--starrocks-home",
        help="StarRocks安装目录"
    )

    parser.add_argument(
        "--config",
        help="JSON配置文件路径"
    )

    # FE相关参数
    parser.add_argument(
        "--meta-dir",
        help="FE元数据存储目录"
    )

    parser.add_argument(
        "--http-port",
        type=int,
        help="FE HTTP端口 (默认: 8030)"
    )

    parser.add_argument(
        "--rpc-port",
        type=int,
        help="FE RPC端口 (默认: 9020)"
    )

    parser.add_argument(
        "--query-port",
        type=int,
        help="FE查询端口 (默认: 9030)"
    )

    parser.add_argument(
        "--edit-log-port",
        type=int,
        help="FE编辑日志端口 (默认: 9010)"
    )

    parser.add_argument(
        "--helper-address",
        help="FE Helper地址 (用于Follower节点，格式: host:port)"
    )

    parser.add_argument(
        "--use-fqdn",
        action="store_true",
        help="使用FQDN模式"
    )

    parser.add_argument(
        "--default-replication-num",
        type=int,
        help="默认副本数（单BE节点需设为1）"
    )

    # BE相关参数
    parser.add_argument(
        "--storage-root-path",
        help="BE存储路径（支持多磁盘: /path1,medium:HDD;/path2,medium:SSD）"
    )

    parser.add_argument(
        "--be-port",
        type=int,
        help="BE端口 (默认: 9060)"
    )

    parser.add_argument(
        "--be-http-port",
        type=int,
        help="BE HTTP端口 (默认: 8040)"
    )

    parser.add_argument(
        "--heartbeat-port",
        type=int,
        help="BE心跳端口 (默认: 9050)"
    )

    parser.add_argument(
        "--brpc-port",
        type=int,
        help="BE brpc端口 (默认: 8060)"
    )

    parser.add_argument(
        "--starlet-port",
        type=int,
        help="BE starlet端口 (默认: 9070)"
    )

    # [fix] 加入集群时未指定网络CIDR，结果自动选择了ipv6(fe80::5054:ff:fea2:8cf4)地址，集群异常
    # 场景1：多网卡服务器（指定网络优化业务带宽）
    # 场景2：避免选择错误的 IP，比如169.254.x.x (链路本地地址，错误)，fe80:: (IPv6地址，错误)
    parser.add_argument(
        "--priority-networks",
        help="网络CIDR (如: 192.168.1.0/24, StarRocks使用指定网段的 IP 进行业务通信)"
    )

    parser.add_argument(
        "--java-home",
        help="Java安装路径"
    )

    parser.add_argument(
        "--user",
        default="starrocks",
        help="运行用户 (默认: starrocks)"
    )

    parser.add_argument(
        "--group",
        default="starrocks",
        help="运行组 (默认: starrocks)"
    )

    parser.add_argument(
        "--no-systemd",
        action="store_true",
        help="禁用systemd服务管理"
    )

    parser.add_argument(
        "--verify",
        action="store_true",
        help="部署后验证服务启动"
    )

    parser.add_argument(
        "--force",
        action="store_true",
        help="强制覆盖已存在的部署（自动清理后重新部署）"
    )

    parser.add_argument(
        "--clean",
        action="store_true",
        help="清理已部署的服务和配置（不进行部署）"
    )

    parser.add_argument(
        "--fe-host",
        help="FE节点地址（用于自动将BE/CN加入集群）"
    )

    parser.add_argument(
        "--fe-query-port",
        type=int,
        default=9030,
        help="FE查询端口 (默认: 9030)"
    )

    parser.add_argument(
        "--setup",
        action="store_true",
        help="执行Post-deployment设置（设置root密码、系统变量等）"
    )

    parser.add_argument(
        "--root-password",
        help="设置root用户密码（用于Post-deployment设置）"
    )

    parser.add_argument(
        "--status",
        action="store_true",
        help="显示集群状态（FE/BE/CN节点）"
    )

    # 远程部署参数
    parser.add_argument(
        "--target-host",
        help="远程部署目标主机（支持 host:port，默认本地）"
    )
    parser.add_argument(
        "--ssh-user",
        default="root",
        help="SSH用户名 (默认: root)"
    )
    parser.add_argument(
        "--ssh-port",
        type=int,
        default=22,
        help="SSH端口 (默认: 22)"
    )
    parser.add_argument(
        "--ssh-key",
        default="~/.ssh/id_rsa",
        help="SSH私钥路径 (默认: ~/.ssh/id_rsa)"
    )
    parser.add_argument(
        "--disable-ssh-host-check",
        action="store_true",
        help="禁用SSH主机密钥检查（不推荐）"
    )
    parser.add_argument(
        "--remote-workdir",
        help="远程工作目录 (默认: /tmp/starrocks_deploy)"
    )

    args = parser.parse_args()

    if args.help:
        show_examples()
        return

    # 加载配置文件（需要先加载以检查是否指定了deploy）
    config = {}
    if args.config:
        config = load_json_config(args.config)

    # 远程部署处理（在status/clean等逻辑之前）
    target_host = config.get("target_host") or args.target_host
    if target_host:
        # 校验 target_host（如果提供）
        try:
            # 先解析以获取host部分
            host, _ = _parse_target_host(target_host, 22)
            # _parse_target_host 内部已经校验了host和port
        except ValueError as e:
            logger.error(f"无效的target_host格式: {target_host}", extra={"to_stdout": True})
            logger.error(f"错误详情: {e}", extra={"to_stdout": True})
            logger.error("target_host格式应为: host 或 host:port (例如: 192.168.1.100 或 192.168.1.100:22)",
                         extra={"to_stdout": True})
            sys.exit(1)

        if not _is_local_host(target_host):
            _run_remote_deploy(target_host, args, config, sys.argv)
            return

    # 处理status命令（如果只查看状态，不部署）
    # 如果同时指定了 --status 和 --deploy，则在部署后显示状态
    if args.status and not (args.deploy or config.get("deploy")):
        fe_host_status = args.fe_host or "127.0.0.1"
        fe_query_port_status = args.fe_query_port or 9030
        root_password = args.root_password

        # 校验 fe_host_status
        if fe_host_status and not InputValidator.validate_hostname(fe_host_status):
            logger.error(f"无效的FE主机地址格式: {fe_host_status}", extra={"to_stdout": True})
            logger.error("FE主机地址必须是有效的IPv4地址或主机名", extra={"to_stdout": True})
            sys.exit(1)

        try:
            deployer = StarRocksDeployer(
                starrocks_home="/tmp",  # 临时值，status命令不需要
                user="root",
                group="root"
            )
            deployer.show_cluster_status(
                fe_host=fe_host_status,
                fe_query_port=fe_query_port_status,
                password=root_password
            )
        except Exception as e:
            logger.error(f"显示集群状态失败: {e}", extra={"to_stdout": True})
            sys.exit(1)
        return

    # 从配置文件或命令行参数获取数据
    deploy_type = config.get("deploy") or args.deploy
    starrocks_home = config.get("starrocks_home") or args.starrocks_home

    # 处理清理命令
    if args.clean:
        if not deploy_type:
            logger.error("清理操作必须指定节点类型: --deploy [fe|be|cn]", extra={"to_stdout": True})
            sys.exit(1)
        if not starrocks_home:
            logger.error("清理操作必须指定StarRocks安装目录: --starrocks-home PATH", extra={"to_stdout": True})
            sys.exit(1)

        try:
            deployer = StarRocksDeployer(
                starrocks_home=starrocks_home,
                user=config.get("user") or args.user,
                group=config.get("group") or args.group
            )
        except Exception as e:
            logger.error(f"初始化部署器失败: {e}", extra={"to_stdout": True})
            sys.exit(1)

        # 获取FE主机地址（用于从集群移除节点）
        fe_host_clean = config.get("fe_host") or args.fe_host
        fe_query_port_clean = config.get("fe_query_port") or args.fe_query_port or 9030
        password_clean = config.get("root_password") or args.root_password

        # 校验 fe_host_clean（如果提供）
        if fe_host_clean and not InputValidator.validate_hostname(fe_host_clean):
            logger.error(f"无效的FE主机地址格式: {fe_host_clean}", extra={"to_stdout": True})
            logger.error("FE主机地址必须是有效的IPv4地址或主机名", extra={"to_stdout": True})
            sys.exit(1)

        try:
            if deployer.clean_deployment(
                    deploy_type,
                    backup_config=True,
                    fe_host=fe_host_clean,
                    fe_query_port=fe_query_port_clean,
                    password=password_clean
            ):
                logger.info("=== 清理完成 ===", extra={"to_stdout": True})
                sys.exit(0)
            else:
                logger.error("=== 清理失败 ===", extra={"to_stdout": True})
                sys.exit(1)
        except ValueError as e:
            logger.error(f"清理失败: {e}", extra={"to_stdout": True})
            sys.exit(1)
        except Exception as e:
            logger.error(f"清理过程中发生异常: {e}", extra={"to_stdout": True})
            sys.exit(1)

    if not deploy_type:
        logger.error("必须指定部署类型: --deploy [fe|be|cn]", extra={"to_stdout": True})
        sys.exit(1)

    if not starrocks_home:
        logger.error("必须指定StarRocks安装目录: --starrocks-home PATH", extra={"to_stdout": True})
        sys.exit(1)

    # 创建部署器
    try:
        deployer = StarRocksDeployer(
            starrocks_home=starrocks_home,
            user=config.get("user") or args.user,
            group=config.get("group") or args.group
        )
    except Exception as e:
        logger.error(f"初始化部署器失败: {e}", extra={"to_stdout": True})
        sys.exit(1)

    # 检查环境
    env_ok, errors = deployer.check_environment()
    if not env_ok:
        logger.error("环境检查失败:", extra={"to_stdout": True})
        for error in errors:
            logger.error(f"  - {error}", extra={"to_stdout": True})
        sys.exit(1)

    # 执行部署
    # 配置优先级: 命令行参数 > 配置文件 > 默认值
    success = False
    enable_systemd = not (args.no_systemd or (config.get("enable_systemd") is False))

    # 自动加入集群参数
    fe_host = args.fe_host or config.get("fe_host")
    fe_query_port = args.fe_query_port or config.get("fe_query_port") or 9030

    # 校验 fe_host（如果提供）
    if fe_host and not InputValidator.validate_hostname(fe_host):
        logger.error(f"无效的FE主机地址格式: {fe_host}", extra={"to_stdout": True})
        logger.error("FE主机地址必须是有效的IPv4地址或主机名", extra={"to_stdout": True})
        sys.exit(1)

    if deploy_type == "fe":
        # 命令行参数优先，然后是配置文件，最后是默认值
        meta_dir = args.meta_dir or config.get("meta_dir") or str(Path(starrocks_home) / "fe" / "meta")
        http_port = args.http_port if args.http_port is not None else (
                    config.get("http_port") or DEFAULT_FE_PORTS['http_port'])
        rpc_port = args.rpc_port if args.rpc_port is not None else (
                    config.get("rpc_port") or DEFAULT_FE_PORTS['rpc_port'])
        query_port = args.query_port if args.query_port is not None else (
                    config.get("query_port") or DEFAULT_FE_PORTS['query_port'])
        edit_log_port = args.edit_log_port if args.edit_log_port is not None else (
                    config.get("edit_log_port") or DEFAULT_FE_PORTS['edit_log_port'])
        priority_networks = args.priority_networks or config.get("priority_networks")
        java_home = args.java_home or config.get("java_home")
        default_replication_num = args.default_replication_num if args.default_replication_num is not None else config.get(
            "default_replication_num")
        use_fqdn = args.use_fqdn or config.get("use_fqdn", False)
        helper_address = args.helper_address or config.get("helper_address")

        # 校验 helper_address（如果提供，格式为 host:port）
        if helper_address:
            if ':' not in helper_address:
                logger.error(f"无效的helper_address格式: {helper_address}", extra={"to_stdout": True})
                logger.error("helper_address格式应为: host:port (例如: 192.168.1.100:9010)", extra={"to_stdout": True})
                sys.exit(1)
            helper_parts = helper_address.split(':', 1)
            if len(helper_parts) != 2:
                logger.error(f"无效的helper_address格式: {helper_address}", extra={"to_stdout": True})
                logger.error("helper_address格式应为: host:port (例如: 192.168.1.100:9010)", extra={"to_stdout": True})
                sys.exit(1)
            helper_host, helper_port_str = helper_parts
            if not InputValidator.validate_hostname(helper_host):
                logger.error(f"无效的helper_address主机地址: {helper_host}", extra={"to_stdout": True})
                logger.error("主机地址必须是有效的IPv4地址或主机名", extra={"to_stdout": True})
                sys.exit(1)
            try:
                helper_port = int(helper_port_str)
                if not InputValidator.validate_port(helper_port):
                    logger.error(f"无效的helper_address端口号: {helper_port}", extra={"to_stdout": True})
                    sys.exit(1)
            except ValueError:
                logger.error(f"无效的helper_address端口号: {helper_port_str}", extra={"to_stdout": True})
                sys.exit(1)

        java_opts = config.get("java_opts")

        password_fe = config.get("root_password") or args.root_password

        success = deployer.deploy_fe(
            meta_dir=meta_dir,
            http_port=http_port,
            rpc_port=rpc_port,
            query_port=query_port,
            edit_log_port=edit_log_port,
            priority_networks=priority_networks,
            java_home=java_home,
            default_replication_num=default_replication_num,
            use_fqdn=use_fqdn,
            helper_address=helper_address,
            enable_systemd=enable_systemd,
            java_opts=java_opts,
            extra_config=config.get("extra_config"),
            force=args.force,
            password=password_fe,
            fe_host=fe_host,
            fe_query_port=fe_query_port
        )

        if success and args.verify:
            time.sleep(5)  # 等待服务启动
            deployer.verify_fe_started()

    elif deploy_type == "be":
        # 命令行参数优先，然后是配置文件
        storage_root_path = args.storage_root_path or config.get("storage_root_path")
        if not storage_root_path:
            logger.error("必须指定BE存储路径: --storage-root-path PATH", extra={"to_stdout": True})
            sys.exit(1)

        be_port = args.be_port if args.be_port is not None else (config.get("be_port") or DEFAULT_BE_PORTS['be_port'])
        be_http_port = args.be_http_port if args.be_http_port is not None else (
                    config.get("be_http_port") or DEFAULT_BE_PORTS['be_http_port'])
        heartbeat_service_port = args.heartbeat_port if args.heartbeat_port is not None else (
                    config.get("heartbeat_port") or DEFAULT_BE_PORTS['heartbeat_service_port'])
        brpc_port = args.brpc_port if args.brpc_port is not None else (
                    config.get("brpc_port") or DEFAULT_BE_PORTS['brpc_port'])
        starlet_port = args.starlet_port if args.starlet_port is not None else (
                    config.get("starlet_port") or DEFAULT_BE_PORTS['starlet_port'])
        priority_networks = args.priority_networks or config.get("priority_networks")
        java_home = args.java_home or config.get("java_home")
        sys_log_level = config.get("sys_log_level") or "INFO"
        java_opts = config.get("java_opts")

        password_be = config.get("root_password") or args.root_password

        success = deployer.deploy_be(
            storage_root_path=storage_root_path,
            be_port=be_port,
            be_http_port=be_http_port,
            heartbeat_service_port=heartbeat_service_port,
            brpc_port=brpc_port,
            starlet_port=starlet_port,
            priority_networks=priority_networks,
            java_home=java_home,
            sys_log_level=sys_log_level,
            java_opts=java_opts,
            enable_systemd=enable_systemd,
            extra_config=config.get("extra_config"),
            force=args.force,
            fe_host=fe_host,
            fe_query_port=fe_query_port,
            password=password_be
        )

        if success and args.verify:
            time.sleep(5)  # 等待服务启动
            deployer.verify_be_started()

    elif deploy_type == "cn":
        # 命令行参数优先，然后是配置文件，最后是默认值
        be_port = args.be_port if args.be_port is not None else (config.get("be_port") or DEFAULT_CN_PORTS['be_port'])
        be_http_port = args.be_http_port if args.be_http_port is not None else (
                    config.get("be_http_port") or DEFAULT_CN_PORTS['be_http_port'])
        heartbeat_service_port = args.heartbeat_port if args.heartbeat_port is not None else (
                    config.get("heartbeat_port") or DEFAULT_CN_PORTS['heartbeat_service_port'])
        brpc_port = args.brpc_port if args.brpc_port is not None else (
                    config.get("brpc_port") or DEFAULT_CN_PORTS['brpc_port'])
        priority_networks = args.priority_networks or config.get("priority_networks")
        java_home = args.java_home or config.get("java_home")
        sys_log_level = config.get("sys_log_level") or "INFO"
        java_opts = config.get("java_opts")

        password_cn = config.get("root_password") or args.root_password

        success = deployer.deploy_cn(
            be_port=be_port,
            be_http_port=be_http_port,
            heartbeat_service_port=heartbeat_service_port,
            brpc_port=brpc_port,
            priority_networks=priority_networks,
            java_home=java_home,
            sys_log_level=sys_log_level,
            java_opts=java_opts,
            enable_systemd=enable_systemd,
            extra_config=config.get("extra_config"),
            force=args.force,
            fe_host=fe_host,
            fe_query_port=fe_query_port,
            password=password_cn
        )

    if success:
        logger.info("=== 部署成功 ===", extra={"to_stdout": True})

        # Post-deployment setup
        if args.setup:
            fe_host_setup = args.fe_host or config.get("fe_host") or "127.0.0.1"
            fe_query_port_setup = args.fe_query_port or config.get("fe_query_port") or 9030
            root_password = args.root_password or config.get("root_password")

            # 校验 fe_host_setup
            if fe_host_setup and not InputValidator.validate_hostname(fe_host_setup):
                logger.error(f"无效的FE主机地址格式: {fe_host_setup}", extra={"to_stdout": True})
                logger.error("FE主机地址必须是有效的IPv4地址或主机名", extra={"to_stdout": True})
                sys.exit(1)

            # 等待FE启动
            if deploy_type == "fe":
                logger.info("等待FE启动完成...", extra={"to_stdout": True})
                time.sleep(10)

            deployer.setup_cluster(
                fe_host=fe_host_setup,
                fe_query_port=fe_query_port_setup,
                root_password=root_password,
                enable_profile=config.get("enable_profile", False),
                enable_pipeline_engine=config.get("enable_pipeline_engine", True),
                parallel_fragment_exec_instance_num=config.get("parallel_fragment_exec_instance_num", 1),
                max_user_connections=config.get("max_user_connections", 1000)
            )

        # 显示集群状态
        if args.status:
            fe_host_status = args.fe_host or config.get("fe_host") or "127.0.0.1"
            fe_query_port_status = args.fe_query_port or config.get("fe_query_port") or 9030
            root_password = args.root_password or config.get("root_password")

            # 校验 fe_host_status
            if fe_host_status and not InputValidator.validate_hostname(fe_host_status):
                logger.error(f"无效的FE主机地址格式: {fe_host_status}", extra={"to_stdout": True})
                logger.error("FE主机地址必须是有效的IPv4地址或主机名", extra={"to_stdout": True})
                sys.exit(1)

            deployer.show_cluster_status(
                fe_host=fe_host_status,
                fe_query_port=fe_query_port_status,
                password=root_password
            )
    else:
        logger.error("=== 部署失败 ===", extra={"to_stdout": True})
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n操作被用户中断", extra={"to_stdout": True})
        sys.exit(1)
    except Exception as err:
        logger.error(f"程序异常: {err}", exc_info=True, extra={"to_stdout": True})
        sys.exit(1)

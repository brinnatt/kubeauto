#!/usr/bin/env python3
"""
高效镜像管理工具 - 支持批量打包、下载和分发
支持Docker和nerdctl运行时
Python 3.6+ 兼容
"""

import argparse
import json
import logging
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Tuple, Dict, Any, Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 安全配置常量
MAX_HOSTNAME_LENGTH = 253  # RFC 1123 主机名最大长度
MIN_PORT = 1
MAX_PORT = 65535
SSH_KEY_PERMISSIONS = 0o600  # SSH私钥文件权限要求


def _env_for_system_subprocess():
    """Return envvars so subprocess (ssh/scp/docker/nerdctl) use system libs, not the PyInstaller bundle.

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
    Subprocesses inherit the parent's environment. The chain is: this_script -> ssh/scp (or docker/nerdctl).
    So ssh runs with LD_LIBRARY_PATH still pointing at _MEIPASS. The dynamic linker then loads
    libcrypto from the bundle instead of the system. Host ssh (e.g. on Kylin) is built against the host's
    OpenSSL and expects symbols like OPENSSL_1_1_1f from the host's libcrypto; the bundled libcrypto from
    the build machine does not provide that symbol version, so ssh fails with "OPENSSL_1_1_1f not found".

    Reference:
    - Launching external programs and inherited library path:
      https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application

    Solution
    --------
    Before spawning ssh/scp or other system binaries, pass env that restores LD_LIBRARY_PATH from
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


class InputValidator:
    """输入验证器"""
    
    @staticmethod
    def validate_hostname(hostname: str) -> bool:
        """验证主机名格式"""
        if not hostname or len(hostname) > MAX_HOSTNAME_LENGTH:
            return False
        if re.match(r'^(\d{1,3}\.){3}\d{1,3}$', hostname):
            try:
                return all(0 <= int(p) <= 255 for p in hostname.split('.'))
            except ValueError:
                return False
        return bool(re.match(r'^[a-zA-Z0-9.-]+$', hostname))
    
    @staticmethod
    def validate_port(port: int) -> bool:
        """验证端口号范围"""
        return MIN_PORT <= port <= MAX_PORT
    
    @staticmethod
    def validate_namespace(namespace: str) -> bool:
        """验证命名空间 - 支持DNS标签格式（允许点号，如k8s.io）"""
        if not namespace or len(namespace) > 253:  # DNS标签最大长度
            return False
        # 允许小写字母、数字、连字符和点号，符合DNS标签规范
        # 但不能以点号开头或结尾，不能连续两个点号
        if namespace.startswith('.') or namespace.endswith('.'):
            return False
        if '..' in namespace:
            return False
        return bool(re.match(r'^[a-z0-9.-]+$', namespace))
    
    @staticmethod
    def sanitize_path(path: str, base_dir: Optional[Path] = None) -> Optional[Path]:
        """清理和验证文件路径"""
        if not path or '..' in path:
            return None
        try:
            resolved = Path(path).resolve()
            if base_dir:
                base_resolved = Path(base_dir).resolve()
                try:
                    resolved.relative_to(base_resolved)
                except ValueError:
                    return None
            return resolved
        except (OSError, ValueError):
            return None


class SecurityChecker:
    """安全检查器"""
    
    @staticmethod
    def check_ssh_key_permissions(key_path: str) -> bool:
        """检查SSH私钥文件权限"""
        try:
            file_mode = stat.S_IMODE(os.stat(key_path).st_mode)
            if file_mode != SSH_KEY_PERMISSIONS:
                logger.warning(f"SSH私钥权限不安全: {key_path} (应为600)")
                return False
            return True
        except OSError:
            return False


class RuntimeChecker:
    """运行时检查器"""

    @staticmethod
    def check_runtime(runtime: str) -> bool:
        """检查运行时是否可用（打包后使用系统库，兼容麒麟，见 _env_for_system_subprocess）"""
        try:
            subprocess.run(
                [runtime, "--version"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                env=_env_for_system_subprocess()
            )
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False


class SSHConnectivityChecker:
    """SSH 连通性检查器。远程操作前先检查，避免把连接失败误报为运行时未找到。"""

    @staticmethod
    def check(ssh) -> Tuple[bool, str]:
        """检查是否可连通。返回 (成功, 失败时错误信息)。"""
        ok, out = ssh.run_command("true", timeout=30)
        if ok:
            return True, ""
        return False, (out or "").strip() or "未知错误"


def _remote_ssh_runtime_ready(
        ssh: "SSHManager",
        host: str,
        port: int,
        runtime: str,
) -> bool:
    """SSHManager 已构造后：检查连通性与远程 runtime。失败时写日志，返回 False。"""
    ok, reason = SSHConnectivityChecker.check(ssh)
    if not ok:
        logger.error("无法连接主机 {}:{}: {}".format(host, port, reason))
        return False
    if not ssh.check_runtime(runtime):
        logger.error("主机 {}:{} 上未找到 {}".format(host, port, runtime))
        return False
    return True


class CommandRunner:
    """命令执行器 - 参照官方最佳实践"""

    @staticmethod
    def run(cmd, capture_output=True, check=False, timeout: Optional[int] = None, **kwargs):
        """执行命令 - 支持 List/Tuple/str 类型；未传 env 时使用 _env_for_system_subprocess 以兼容麒麟打包"""
        if not cmd:
            raise ValueError("命令为空")
        if "env" not in kwargs:
            kwargs["env"] = _env_for_system_subprocess()
        # 处理命令类型：如果是字符串且没有指定 shell，转换为列表
        if isinstance(cmd, str):
            if not kwargs.get("shell"):
                cmd = cmd.split()
        
        # 处理 capture_output 和 stdout/stderr 冲突
        if capture_output and ("stdout" in kwargs or "stderr" in kwargs):
            capture_output = False
        
        try:
            result = subprocess.run(
                cmd, 
                check=check, 
                capture_output=capture_output, 
                text=True, 
                timeout=timeout,
                **kwargs
            )
            return result
        except subprocess.TimeoutExpired:
            logger.error("命令执行超时")
            raise
        except subprocess.CalledProcessError as e:
            if capture_output:
                error_msg = (
                    f"命令失败 (退出码 {e.returncode}): {' '.join(e.cmd) if isinstance(e.cmd, (list, tuple)) else e.cmd}\n"
                    f"错误输出: {e.stderr.strip() if e.stderr else '(空)'}\n"
                    f"标准输出: {e.stdout.strip() if e.stdout else '(空)'}"
                )
                logger.error(error_msg)
            raise
        except Exception as e:
            logger.error(f"执行异常: {e}")
            raise


class ImageManager:
    """镜像管理器 - 支持批量操作"""

    def __init__(self, runtime="docker", namespace="k8s.io"):
        self.runtime = runtime
        self.namespace = namespace
        if not RuntimeChecker.check_runtime(runtime):
            logger.error(f"未找到 {runtime} 运行时")
            sys.exit(1)

    def _build_command(self, cmd_parts: List[str]) -> List[str]:
        """构建命令，如果是nerdctl则添加namespace参数"""
        if self.runtime == "nerdctl":
            # 对于nerdctl，添加namespace参数
            return [self.runtime, "-n", self.namespace] + cmd_parts
        else:
            # 对于docker，直接使用
            return [self.runtime] + cmd_parts

    def _batch_operation(self, images: List[str], operation_name: str, single_func, success_msg: str, fail_msg: str) -> int:
        """批量操作通用方法"""
        if not images:
            logger.warning("镜像列表为空")
            return 0
        
        logger.info(f"批量{operation_name} {len(images)} 个镜像")
        if self.runtime == "nerdctl":
            logger.info(f"运行时: {self.runtime}, 命名空间: {self.namespace}")
        else:
            logger.info(f"运行时: {self.runtime}")

        success_count = 0
        failed_images = []
        
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(single_func, img): img for img in images}
            for future in as_completed(futures):
                img = futures[future]
                try:
                    if future.result():
                        logger.info(f"✓ {success_msg}: {img}")
                        success_count += 1
                    else:
                        logger.error(f"✗ {fail_msg}: {img}")
                        failed_images.append(img)
                except Exception as e:
                    logger.error(f"✗ {operation_name}异常 {img}: {e}", exc_info=True)
                    failed_images.append(img)
        
        if failed_images:
            logger.warning(f"{operation_name}失败的镜像 ({len(failed_images)}/{len(images)}): {', '.join(failed_images[:5])}")
            if len(failed_images) > 5:
                logger.warning(f"... 还有 {len(failed_images) - 5} 个镜像失败")
        
        return success_count

    def batch_pull(self, images: List[str]) -> int:
        """批量拉取镜像"""
        return self._batch_operation(images, "拉取", self._pull_single, "拉取成功", "拉取失败")

    def _pull_single(self, image: str) -> bool:
        """拉取单个镜像"""
        cmd = self._build_command(["pull", image])
        try:
            result = CommandRunner.run(cmd, capture_output=False, check=False, timeout=1800)
            return result.returncode == 0
        except Exception as e:
            logger.error("拉取镜像失败 {}: {}".format(image, e))
            return False

    def batch_delete(self, images: List[str]) -> int:
        """批量删除镜像"""
        return self._batch_operation(images, "删除", self._delete_single, "删除完成", "删除失败")

    def _delete_single(self, image: str) -> bool:
        """删除单个镜像"""
        cmd = self._build_command(["rmi", image])
        try:
            result = CommandRunner.run(cmd, capture_output=True, check=False, timeout=300)
            return result.returncode == 0
        except Exception as e:
            logger.error("删除镜像失败 {}: {}".format(image, e))
            return False

    def batch_save(self, images: List[str], output_dir: str) -> List[str]:
        """批量保存镜像 - 一次保存所有到单个tar文件
        
        Args:
            images: 要保存的镜像列表
            output_dir: 输出目录
        """
        if not images:
            logger.warning("镜像列表为空")
            return []
        
        logger.info(f"批量保存 {len(images)} 个镜像")
        if self.runtime == "nerdctl":
            logger.info(f"运行时: {self.runtime}, 命名空间: {self.namespace}")
        else:
            logger.info(f"运行时: {self.runtime}")

        # 验证并创建输出目录
        output_path = InputValidator.sanitize_path(output_dir)
        if not output_path:
            logger.error(f"输出目录路径无效: {output_dir}")
            return []
        
        try:
            output_path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.error(f"无法创建输出目录: {output_dir}, 错误: {e}")
            return []
        
        # 过滤掉不存在的镜像
        existing_images = []
        for img in images:
            if self.image_exists(img):
                existing_images.append(img)
            else:
                logger.warning(f"跳过不存在的镜像: {img}")

        if not existing_images:
            logger.error("没有可用的本地镜像")
            return []

        # 生成唯一文件名
        timestamp = int(time.time())
        tar_file = output_path / f"images_batch_{timestamp}.tar"

        # 构建保存命令 - 批量保存所有镜像到一个tar文件
        cmd = self._build_command(["save", "-o", str(tar_file)] + existing_images)

        logger.info(f"开始保存镜像到: {tar_file.name}")
        try:
            result = CommandRunner.run(cmd, capture_output=True, check=False, timeout=3600)
            success = result.returncode == 0
        except Exception as e:
            logger.error("保存镜像失败: {}".format(e))
            success = False

        if success:
            size_mb = os.path.getsize(tar_file) / (1024 * 1024)
            logger.info(f"✓ 批量保存完成: {tar_file.name} ({size_mb:.1f}MB, {len(existing_images)} 个镜像)")
            return [str(tar_file)]
        else:
            # 原子性：如果批量保存失败，清理已创建的文件
            if tar_file.exists():
                try:
                    tar_file.unlink()
                    logger.debug(f"清理失败的文件: {tar_file.name}")
                except OSError as e:
                    logger.debug(f"清理失败的文件时出错: {e}")
            logger.warning("批量保存失败，尝试逐个保存...")
            return self._save_one_by_one(existing_images, output_path)

    def _save_one_by_one(self, images: List[str], output_path: Path) -> List[str]:
        """逐个保存镜像（备用方案）"""
        saved_files = []

        with ThreadPoolExecutor(max_workers=2) as executor:  # 限制并发避免磁盘IO瓶颈
            futures = {}
            for img in images:
                # 生成安全文件名
                safe_name = img.replace('/', '_').replace(':', '_').replace('@', '_')
                tar_file = output_path / f"{safe_name}.tar"

                future = executor.submit(self._save_single, img, str(tar_file))
                futures[future] = (img, tar_file)

            for future in as_completed(futures):
                img, tar_file = futures[future]
                try:
                    if future.result():
                        try:
                            size_mb = os.path.getsize(tar_file) / (1024 * 1024)
                            logger.info(f"✓ {img} -> {tar_file.name} ({size_mb:.1f}MB)")
                            saved_files.append(str(tar_file))
                        except OSError as e:
                            logger.warning(f"无法获取文件大小 {tar_file.name}: {e}")
                            saved_files.append(str(tar_file))
                    else:
                        logger.error(f"✗ 保存失败: {img}")
                except Exception as ex:
                    logger.error(f"✗ 保存异常 {img}: {ex}", exc_info=True)

        return saved_files

    def _save_single(self, image: str, output_path: str) -> bool:
        """保存单个镜像"""
        cmd = self._build_command(["save", "-o", output_path, image])
        try:
            result = CommandRunner.run(cmd, capture_output=True, check=False, timeout=3600)
            return result.returncode == 0
        except Exception as e:
            logger.error("保存单个镜像失败 {}: {}".format(image, e))
            return False

    def image_exists(self, image: str) -> bool:
        """检查镜像是否存在"""
        cmd = self._build_command(["images", "-q", image])
        try:
            result = CommandRunner.run(cmd, capture_output=True, check=False, timeout=30)
            return result.returncode == 0 and result.stdout.strip() != ""
        except Exception as e:
            logger.error("检查镜像是否存在失败 {}: {}".format(image, e))
            return False

    def save_specific_tar(self, tar_name: str, output_dir: str) -> bool:
        """保存指定的tar文件到输出目录"""
        # 验证并清理路径，防止路径遍历攻击
        tar_path = InputValidator.sanitize_path(tar_name)
        if not tar_path or not tar_path.exists():
            logger.error(f"指定的tar文件不存在或路径无效: {tar_name}")
            return False

        # 确保是tar文件
        if not tar_path.name.endswith('.tar'):
            logger.error(f"文件必须是.tar格式: {tar_name}")
            return False

        # 验证输出目录路径
        output_path = InputValidator.sanitize_path(output_dir)
        if not output_path:
            logger.error(f"输出目录路径无效: {output_dir}")
            return False
        
        output_path.mkdir(parents=True, exist_ok=True)

        # 如果指定的tar文件已经在输出目录中，直接使用它，不复制
        if tar_path.parent.resolve() == output_path.resolve():
            logger.info(f"✓ tar文件已在输出目录中: {tar_path.name}")
            return True

        dest_path = output_path / tar_path.name
        try:
            shutil.copy2(tar_path, dest_path)
            logger.info(f"✓ 复制tar文件: {tar_path.name} -> {dest_path.name}")
            return True
        except Exception as ex:
            logger.error(f"复制tar文件失败: {ex}")
            return False


class SSHManager:
    """SSH管理器 - 支持自定义端口，遵循安全最佳实践"""

    def __init__(self, host: str, port: int, username: str = "root", key_path: str = "~/.ssh/id_rsa",
                 namespace: str = "k8s.io", strict_host_key_checking: bool = True, validate_namespace: bool = True):
        # 验证输入
        if not InputValidator.validate_hostname(host):
            raise ValueError(f"无效的主机名: {host}")
        if not InputValidator.validate_port(port):
            raise ValueError(f"无效的端口号: {port}")
        # 只有当需要验证命名空间时才验证（例如使用nerdctl时）
        if validate_namespace and not InputValidator.validate_namespace(namespace):
            raise ValueError(f"无效的命名空间: {namespace}")
        
        self.host = host
        self.port = port
        self.username = username
        self.key_path = os.path.expanduser(key_path)
        self.namespace = namespace
        self.strict_host_key_checking = strict_host_key_checking
        
        # 检查SSH密钥文件是否存在（如果不存在，可能使用密码认证）
        self.has_key = os.path.exists(self.key_path)
        if self.has_key:
            # 检查SSH密钥文件权限（仅警告，不阻止执行）
            SecurityChecker.check_ssh_key_permissions(self.key_path)
        
        # 创建ControlMaster临时目录和控制路径
        # 使用临时目录存储ControlPath socket，避免权限问题
        self._control_dir = tempfile.mkdtemp(prefix="ssh_control_")
        # 生成唯一的控制路径，基于主机、端口和用户名
        control_name = f"{self.host}_{self.port}_{self.username}".replace('.', '_').replace(':', '_')
        self._control_path = os.path.join(self._control_dir, control_name)
        self._control_master_established = False
        
        # 清理可能存在的无效ControlSocket
        self._cleanup_stale_control_socket()

    def _build_ssh_options(self, use_control_master: bool = True) -> List[str]:
        """构建SSH选项"""
        ssh_options = [
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.port),
        ]
        
        # 如果密钥文件存在，使用密钥认证
        if self.has_key:
            ssh_options.extend(["-i", self.key_path])
        
        # 添加ControlMaster支持，复用连接避免重复输入密码
        # 使用 ControlMaster=auto 而不是 yes，这样如果连接已存在就复用，不存在就创建
        if use_control_master:
            ssh_options.extend([
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path}",
                "-o", "ControlPersist=300",  # 控制连接保持300秒
            ])
        
        if self.strict_host_key_checking:
            known_hosts_file = os.path.expanduser("~/.ssh/known_hosts")
            ssh_options.extend([
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={known_hosts_file}",
            ])
        else:
            logger.warning("警告: 已禁用SSH主机密钥检查")
            ssh_options.extend(["-o", "StrictHostKeyChecking=no"])
        return ssh_options

    def run_command(self, cmd: str, timeout: Optional[int] = None) -> Tuple[bool, str]:
        """执行远程命令。失败时返回 (False, 完整错误信息)，便于调用方直接打日志。"""
        if not cmd:
            return False, "命令为空"
        ssh_cmd = ["ssh"] + self._build_ssh_options() + [f"{self.username}@{self.host}", cmd]
        try:
            result = CommandRunner.run(ssh_cmd, capture_output=True, check=False, timeout=timeout or 3600)
            if result.returncode == 0:
                return True, result.stdout if result.stdout else ""
            # 合并 stderr/stdout，与 starcli 一致，确保连接失败等错误完整返回
            parts = []
            if result.stderr:
                parts.append(result.stderr.strip())
            if result.stdout:
                parts.append(result.stdout.strip())
            return False, "\n".join(parts) if parts else "命令执行失败，返回码: {}".format(result.returncode)
        except Exception as e:
            logger.error("SSH命令执行失败: {}".format(e))
            return False, str(e)

    def copy_file(self, src: str, dst: str) -> bool:
        """复制文件到远程"""
        src_path = InputValidator.sanitize_path(src)
        if not src_path or not src_path.exists():
            logger.error(f"源文件不存在: {src}")
            return False
        
        if not dst or '..' in dst:
            logger.error("目标路径无效")
            return False
        
        # SCP使用-P而不是-p指定端口
        scp_options = [
            "-o", "ConnectTimeout=10",
            "-P", str(self.port),
        ]
        
        # 如果密钥文件存在，使用密钥认证
        if self.has_key:
            scp_options.extend(["-i", self.key_path])
        
        # 添加ControlMaster支持，复用连接避免重复输入密码
        # 使用 ControlMaster=auto 而不是 yes，这样如果连接已存在就复用，不存在就创建
        scp_options.extend([
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self._control_path}",
            "-o", "ControlPersist=300",  # 控制连接保持300秒
        ])
        
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
            result = CommandRunner.run(scp_cmd, capture_output=True, check=False, timeout=1800)
            if result.returncode == 0:
                return True
            parts = []
            if result.stderr:
                parts.append(result.stderr.strip())
            if result.stdout:
                parts.append(result.stdout.strip())
            combined_output = "\n".join(parts) if parts else "返回码 {}".format(result.returncode)
            logger.error("复制文件到远程失败 {} -> {}，错误:".format(src, dst))
            for line in combined_output.split("\n"):
                logger.error("  {}".format(line))
            return False
        except Exception as e:
            logger.error("复制文件到远程失败 {} -> {}: {}".format(src, dst, e))
            return False

    def check_runtime(self, runtime: str) -> bool:
        """检查远程是否已安装指定运行时（命令存在且可执行）。仅在 SSH 已连通时调用。"""
        if runtime not in ("docker", "nerdctl"):
            return False
        
        # 先用 command -v 检测命令是否存在
        check_cmd = f"command -v {runtime}"
        success, _ = self.run_command(check_cmd, timeout=30)
        if not success:
            return False
        
        # 命令存在，验证版本
        version_cmd = f"{runtime} --version"
        success, _ = self.run_command(version_cmd, timeout=30)
        return success
    
    def close(self):
        """关闭ControlMaster连接并清理临时资源"""
        # 关闭ControlMaster连接
        if os.path.exists(self._control_path):
            try:
                # 使用 -O exit 关闭控制连接
                ssh_options = self._build_ssh_options(use_control_master=False)
                exit_cmd = ["ssh"] + ssh_options + [
                    "-O", "exit",
                    f"{self.username}@{self.host}"
                ]
                # 不检查返回值，因为连接可能已经关闭
                CommandRunner.run(exit_cmd, capture_output=True, check=False, timeout=10)
            except Exception as e:
                logger.debug(f"关闭SSH控制连接失败: {e}")
        
        # 清理临时目录
        try:
            if os.path.exists(self._control_dir):
                shutil.rmtree(self._control_dir)
        except Exception as e:
            logger.warning(f"清理SSH临时目录失败: {e}")
    
    def _cleanup_stale_control_socket(self):
        """清理无效的ControlSocket文件
        
        如果ControlSocket文件存在但连接已断开，SSH会报错并禁用多路复用。
        此方法检查并清理无效的ControlSocket。
        """
        if not os.path.exists(self._control_path):
            return
        
        try:
            # 尝试检查ControlSocket是否有效
            # 使用 -O check 检查控制连接状态，需要指定 ControlPath
            ssh_options = self._build_ssh_options(use_control_master=False)
            check_cmd = ["ssh"] + ssh_options + [
                "-o", f"ControlPath={self._control_path}",
                "-O", "check",
                f"{self.username}@{self.host}"
            ]
            # 如果ControlSocket有效，这个命令会成功（返回码0）
            result = CommandRunner.run(check_cmd, capture_output=True, check=False, timeout=5)
            
            # 如果检查失败（返回码非0），说明ControlSocket无效，删除它
            if result.returncode != 0:
                try:
                    os.remove(self._control_path)
                    logger.debug(f"清理无效的ControlSocket: {self._control_path}")
                except OSError:
                    pass  # 文件可能已被删除
        except Exception as e:
            # 如果检查过程出错，直接删除ControlSocket文件
            # 因为如果连接有效，检查应该不会出错
            logger.debug(f"检查ControlSocket状态时出错: {e}")
            try:
                if os.path.exists(self._control_path):
                    os.remove(self._control_path)
                    logger.debug(f"清理可能无效的ControlSocket: {self._control_path}")
            except OSError:
                pass  # 文件可能已被删除
    
    def __del__(self):
        """析构函数，确保资源被清理"""
        try:
            self.close()
        except Exception as e:
            logger.debug(f"SSHManager析构时清理资源失败: {e}")



def pack_images(images: List[str], runtime: str, output_dir: str, namespace: str = "k8s.io") -> Tuple[bool, List[str]]:
    """打包镜像功能
    
    Args:
        images: 要打包的镜像列表
        runtime: 运行时
        output_dir: 输出目录
        namespace: 命名空间
    
    Returns:
        (是否成功, 本次创建的文件列表) - 用于失败时清理
    """
    if not images:
        logger.error("未指定要打包的镜像")
        return False, []

    logger.info("=== 开始打包镜像 ===")

    # 检查本地镜像是否存在
    manager = ImageManager(runtime, namespace)
    existing_images = []

    for img in images:
        if manager.image_exists(img):
            existing_images.append(img)
        else:
            logger.warning(f"本地镜像不存在，跳过: {img}")

    if not existing_images:
        logger.error("没有可用的本地镜像")
        return False, []

    logger.info(f"将打包 {len(existing_images)} 个镜像")

    # 批量保存
    saved_files = manager.batch_save(existing_images, output_dir)

    if saved_files:
        total_size = sum(os.path.getsize(f) for f in saved_files if os.path.exists(f)) / (1024 * 1024)
        logger.info(f"✓ 打包完成: {len(saved_files)} 个文件，总共 {total_size:.1f}MB")
        return True, saved_files
    else:
        logger.error("✗ 打包失败")
        return False, []


def delete_images(
        images: List[str],
        runtime: str,
        namespace: str = "k8s.io",
        hosts: Optional[List[Tuple[str, int]]] = None,
        ssh_user: str = "root",
        ssh_key: str = "~/.ssh/id_rsa",
        strict_host_key_checking: bool = True
) -> bool:
    """删除镜像功能 - 支持本地和远程删除"""
    if not images:
        logger.error("未指定要删除的镜像")
        return False

    logger.info("=== 开始删除镜像 ===")

    # 如果指定了主机列表，执行远程删除
    if hosts:
        return _delete_images_remote(images, hosts, runtime, namespace, ssh_user, ssh_key, strict_host_key_checking)
    else:
        # 本地删除
        return _delete_images_local(images, runtime, namespace)


def _delete_images_local(images: List[str], runtime: str, namespace: str = "k8s.io") -> bool:
    """本地删除镜像"""
    manager = ImageManager(runtime, namespace)

    # 确认镜像存在
    existing_images = []
    for img in images:
        if manager.image_exists(img):
            existing_images.append(img)
        else:
            logger.warning(f"镜像不存在，跳过: {img}")

    if not existing_images:
        logger.warning("没有可删除的镜像")
        return True

    logger.info(f"将删除 {len(existing_images)} 个镜像")
    if runtime == "nerdctl":
        logger.info(f"运行时: {runtime}, 命名空间: {namespace}")
    else:
        logger.info(f"运行时: {runtime}")

    # 用户确认
    print(f"\n即将删除以下镜像:")
    for img in existing_images:
        print(f"  - {img}")

    confirm = input("\n确认删除? (yes/no): ").strip().lower()
    if confirm != 'yes':
        logger.info("取消删除操作")
        return False

    # 批量删除
    success_count = manager.batch_delete(existing_images)

    if success_count > 0:
        logger.info(f"✓ 删除完成: {success_count}/{len(existing_images)} 个镜像成功")
        return True
    else:
        logger.error("✗ 删除失败")
        return False


def _delete_images_remote(
        images: List[str],
        hosts: List[Tuple[str, int]],
        runtime: str,
        namespace: str = "k8s.io",
        ssh_user: str = "root",
        ssh_key: str = "~/.ssh/id_rsa",
        strict_host_key_checking: bool = True
) -> bool:
    """远程删除镜像"""
    logger.info(f"将在 {len(hosts)} 个远程主机上删除镜像")
    if runtime == "nerdctl":
        logger.info(f"运行时: {runtime}, 命名空间: {namespace}")
    else:
        logger.info(f"运行时: {runtime}")

    # 用户确认
    print(f"\n即将在以下主机上删除镜像:")
    for host, port in hosts:
        print(f"  - {host}:{port}")
    print(f"\n要删除的镜像:")
    for img in images:
        print(f"  - {img}")

    confirm = input("\n确认删除? (yes/no): ").strip().lower()
    if confirm != 'yes':
        logger.info("取消删除操作")
        return False

    success_hosts = 0

    for host, port in hosts:
        logger.info(f"\n删除主机 {host}:{port} 上的镜像")

        ssh = None
        try:
            # 只有当remote_runtime是nerdctl时才需要验证命名空间
            validate_ns = (runtime == "nerdctl")
            ssh = SSHManager(host, port, ssh_user, ssh_key, namespace, strict_host_key_checking, validate_namespace=validate_ns)
            if not _remote_ssh_runtime_ready(ssh, host, port, runtime):
                continue

            # 构建删除命令
            if runtime == "nerdctl":
                delete_cmd = f"{runtime} -n {namespace} rmi"
            else:
                delete_cmd = f"{runtime} rmi"

            # 批量删除镜像
            failed_images = []
            success_count = 0

            for img in images:
                cmd = f"{delete_cmd} {img}"
                success, output = ssh.run_command(cmd, timeout=300)
                if success:
                    logger.info(f"✓ 删除成功: {img}")
                    success_count += 1
                else:
                    logger.error("✗ 删除失败: {}".format(img))
                    if output and output.strip():
                        for line in output.strip().split("\n"):
                            logger.error("  {}".format(line))
                    failed_images.append(img)

            if success_count > 0:
                logger.info(f"✓ 主机 {host}:{port} 删除完成: {success_count}/{len(images)} 个镜像成功")
                if failed_images:
                    logger.warning(f"  失败的镜像: {', '.join(failed_images)}")
                success_hosts += 1
            else:
                logger.error(f"✗ 主机 {host}:{port} 删除失败")

        except Exception as e:
            logger.error(f"主机 {host}:{port} 处理异常: {e}")
        finally:
            # 确保SSH连接被正确关闭和清理
            if ssh:
                ssh.close()

    if success_hosts > 0:
        logger.info(f"\n✓ 删除完成: {success_hosts}/{len(hosts)} 个主机成功")
        return True
    else:
        logger.error("\n✗ 删除失败")
        return False


def download_images(images: List[str], runtime: str, namespace: str = "k8s.io") -> bool:
    """下载镜像功能"""
    if not images:
        logger.error("未指定要下载的镜像")
        return False

    logger.info("=== 开始下载镜像 ===")

    manager = ImageManager(runtime, namespace)

    # 批量下载
    success_count = manager.batch_pull(images)

    if success_count > 0:
        logger.info(f"✓ 下载完成: {success_count}/{len(images)} 个镜像成功")
        return True
    else:
        logger.error("✗ 下载失败")
        return False


def distribute_images(
        hosts: List[Tuple[str, int]],
        images_dir: str,
        remote_runtime: str,
        ssh_user: str = "root",
        ssh_key: str = "~/.ssh/id_rsa",
        namespace: str = "k8s.io",
        strict_host_key_checking: bool = True
) -> bool:
    """分发镜像到远程主机"""
    if not hosts:
        logger.error("未指定远程主机")
        return False

    images_dir = Path(images_dir)
    if not images_dir.exists():
        logger.error(f"镜像目录不存在: {images_dir}")
        return False

    logger.info("=== 开始分发镜像 ===")

    # 获取所有tar文件
    tar_files = list(images_dir.glob("*.tar"))
    if not tar_files:
        logger.error(f"在 {images_dir} 中没有找到.tar文件")
        return False

    logger.info(f"找到 {len(tar_files)} 个tar文件")
    for tf in tar_files:
        size_mb = os.path.getsize(tf) / (1024 * 1024)
        logger.info(f"  - {tf.name} ({size_mb:.1f}MB)")

    success_hosts = 0

    for host, port in hosts:
        logger.info(f"\n分发到主机: {host}:{port}")

        ssh = None
        try:
            # 只有当remote_runtime是nerdctl时才需要验证命名空间
            validate_ns = (remote_runtime == "nerdctl")
            ssh = SSHManager(host, port, ssh_user, ssh_key, namespace, strict_host_key_checking, validate_namespace=validate_ns)
            if not _remote_ssh_runtime_ready(ssh, host, port, remote_runtime):
                continue

            # 创建远程临时目录
            remote_dir = f"/tmp/images_{int(time.time())}_{random.randint(1000, 9999)}"
            mkdir_cmd = f"mkdir -p {remote_dir}"
            success, output = ssh.run_command(mkdir_cmd, timeout=30)
            if not success:
                logger.error("无法创建远程目录: {}".format(remote_dir))
                if output and output.strip():
                    for line in output.strip().split("\n"):
                        logger.error("  {}".format(line))
                continue

            # 批量传输文件
            transfer_success = True
            for tar_file in tar_files:
                logger.info(f"传输: {tar_file.name}")
                remote_path = f"{remote_dir}/{tar_file.name}"

                if not ssh.copy_file(str(tar_file), remote_path):
                    logger.error(f"传输失败: {tar_file.name}")
                    transfer_success = False
                    break

            if not transfer_success:
                # 清理临时文件
                ssh.run_command(f"rm -rf {remote_dir}", timeout=60)
                continue

            # 批量加载镜像
            logger.info(f"在远程主机加载镜像...")
            if remote_runtime == "nerdctl":
                load_cmd = f"find {remote_dir} -name '*.tar' -exec {remote_runtime} -n {namespace} load -i {{}} \\;"
            else:
                load_cmd = f"find {remote_dir} -name '*.tar' -exec {remote_runtime} load -i {{}} \\;"

            load_ok, load_out = ssh.run_command(load_cmd, timeout=3600)
            if load_ok:
                logger.info(f"✓ 主机 {host}:{port} 镜像加载完成")
                success_hosts += 1
            else:
                logger.error("✗ 主机 {}:{} 镜像加载失败".format(host, port))
                if load_out and load_out.strip():
                    for line in load_out.strip().split("\n"):
                        logger.error("  {}".format(line))

            # 清理临时文件
            ssh.run_command(f"rm -rf {remote_dir}", timeout=60)

        except Exception as e:
            logger.error(f"主机 {host}:{port} 处理异常: {e}")
        finally:
            # 确保SSH连接被正确关闭和清理
            if ssh:
                ssh.close()

    if success_hosts > 0:
        logger.info(f"\n✓ 分发完成: {success_hosts}/{len(hosts)} 个主机成功")
        return True
    else:
        logger.error("\n✗ 分发失败")
        return False


def parse_host_with_port(host_str: str) -> Tuple[str, int]:
    """解析主机和端口，格式: host[:port]"""
    if ':' in host_str:
        parts = host_str.split(':', 1)
        try:
            port = int(parts[1])
            # 验证端口范围
            if not InputValidator.validate_port(port):
                logger.warning(f"端口 {port} 超出有效范围，使用默认端口22")
                return parts[0], 22
            return parts[0], port
        except ValueError:
            logger.warning(f"无法解析端口 '{parts[1]}'，使用默认端口22")
            return parts[0], 22
    return host_str, 22


def _split_trailing_port(token: str):
    """若为 host:PORT 且 PORT 为十进制端口，返回 (主机部分, ':PORT' 或 '')。含 IPv6 或无法解析时整体作为主机部分。"""
    if "[" in token:
        return token, ""
    if token.count(":") != 1:
        return token, ""
    left, right = token.split(":", 1)
    if not right.isdigit():
        return token, ""
    try:
        p = int(right)
    except ValueError:
        return token, ""
    if not InputValidator.validate_port(p):
        return token, ""
    return left, ":" + right


def _expand_brace_numeric_range(inner: str):
    """解析 {01..17} 或 {1..17} 形式的数字区间，返回字符串列表；无法解析则返回 None。"""
    if ".." not in inner:
        return None
    left, right = inner.split("..", 1)
    left_s, right_s = left.strip(), right.strip()
    if not left_s.isdigit() or not right_s.isdigit():
        return None
    start, end = int(left_s), int(right_s)
    if start > end:
        return None
    width = len(left_s) if left_s.startswith("0") and len(left_s) > 1 else 0
    out = []
    for n in range(start, end + 1):
        s = str(n).zfill(width) if width else str(n)
        out.append(s)
    return out


def _expand_brace_in_string(base: str):
    """将 base 中第一处 {n..m} 展开为多个字符串；无区间则返回 [base]。"""
    m = re.match(r"^(.*)\{([^}]+)}(.*)$", base)
    if not m:
        return [base]
    pre, inner, post = m.group(1), m.group(2), m.group(3)
    nums = _expand_brace_numeric_range(inner)
    if nums is None:
        return [base]
    return [pre + n + post for n in nums]


def _expand_ipv4_last_octet_range(base: str):
    """将 192.168.4.7-19 展开为同一 /24 网段内末段连续 IPv4；不匹配则返回 [base]。"""
    m = re.match(r"^((?:\d{1,3}\.){3})(\d{1,3})-(\d{1,3})$", base)
    if not m:
        return [base]
    prefix, lo_s, hi_s = m.group(1), m.group(2), m.group(3)
    try:
        lo, hi = int(lo_s), int(hi_s)
    except ValueError:
        return [base]
    if lo > hi or not (0 <= lo <= 255 and 0 <= hi <= 255):
        return [base]
    out = []
    for i in range(lo, hi + 1):
        ip = prefix + str(i)
        if InputValidator.validate_hostname(ip):
            out.append(ip)
        else:
            logger.warning(f"跳过无效地址（区间展开）: {ip}")
    return out if out else [base]


def expand_host_distribution_token(token: str):
    """
    将单个主机表项展开为 host[:port] 列表。
    支持: worker-{01..17}、192.168.4.7-19、以及上述形式带末尾 :port。
    """
    token = token.strip()
    if not token:
        return []
    base, port_suf = _split_trailing_port(token)
    # 先花括号展开（仅第一处 {..}）
    if "{" in base and "}" in base and ".." in base:
        expanded = _expand_brace_in_string(base)
        if len(expanded) == 1 and expanded[0] == base:
            return [token]
        return [e + port_suf for e in expanded]
    # IPv4 末段区间
    if re.match(r"^((?:\d{1,3}\.){3})(\d{1,3})-(\d{1,3})$", base):
        ips = _expand_ipv4_last_octet_range(base)
        if len(ips) == 1 and ips[0] == base:
            return [token]
        return [ip + port_suf for ip in ips]
    return [token]


def expand_distribution_hosts(hosts):
    """对 distribute / delete-hosts 的主机列表逐项展开并扁平化。"""
    out = []
    for h in hosts:
        if not h:
            continue
        out.extend(expand_host_distribution_token(h))
    return out


def load_json_config(config_file: str) -> Dict[str, Any]:
    """加载JSON配置文件"""
    config_path = Path(config_file)
    if not config_path.exists():
        logger.error(f"配置文件不存在: {config_file}")
        sys.exit(1)

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        logger.info(f"✓ 加载配置文件: {config_file}")
        return config
    except json.JSONDecodeError as e:
        logger.error(f"配置文件格式错误: {e}")
        sys.exit(1)
    except Exception as ex:
        logger.error(f"加载配置文件失败: {ex}")
        sys.exit(1)


def cleanup_session_files(packed_files: List[str]) -> None:
    """清理本次会话产生的文件（失败时调用）
    
    Args:
        packed_files: 本次会话产生的文件列表
    """
    if not packed_files:
        return
    
    logger.info(f"清理本次会话产生的文件 ({len(packed_files)} 个)")
    for file_path in packed_files:
        try:
            file_obj = Path(file_path)
            if file_obj.exists():
                file_obj.unlink()
                logger.debug(f"已删除: {file_obj.name}")
        except Exception as e:
            logger.warning(f"清理文件失败 {file_path}: {e}")


def cleanup_old_auto_files(output_dir: str) -> None:
    """清理旧的自动生成文件（images_batch_*.tar格式）
    
    Args:
        output_dir: 输出目录
    """
    try:
        output_path = Path(output_dir)
        if not output_path.exists():
            return
        
        old_tar_files = list(output_path.glob("images_batch_*.tar"))
        if not old_tar_files:
            return
        
        logger.info(f"清理旧的自动生成tar文件 ({len(old_tar_files)} 个)")
        for old_tar in old_tar_files:
            try:
                old_tar.unlink()
                logger.debug(f"已删除: {old_tar.name}")
            except OSError as e:
                logger.warning(f"无法删除旧文件 {old_tar.name}: {e}")
    except Exception as ex:
        logger.warning(f"清理旧文件时出错: {ex}")


def show_examples():
    """显示使用示例"""
    examples = """
使用示例:

1. 下载镜像:
   python3 k8simgmanager.py --download nginx:alpine redis:latest ubuntu:20.04
   python3 k8simgmanager.py --download nginx:alpine --local-runtime nerdctl --namespace default

2. 打包本地镜像:
   python3 k8simgmanager.py --pack myapp:v1.0 mysql:8.0
   python3 k8simgmanager.py --pack myapp:v1.0 --output-dir /tmp/images --local-runtime nerdctl
   python3 k8simgmanager.py --pack myapp:v1.0 --cleanup  # 打包后清理旧的自动生成文件

3. 删除镜像:
   python3 k8simgmanager.py --delete nginx:alpine redis:latest
   python3 k8simgmanager.py --delete myapp:v1.0 --local-runtime nerdctl --namespace k8s.io
   python3 k8simgmanager.py --delete nginx:alpine redis:latest --delete-hosts host1:2222 host2 --remote-runtime nerdctl

4. 分发镜像到远程主机:
   # 方式1: 与--pack一起使用（推荐，同一会话完成）
   python3 k8simgmanager.py --pack nginx:alpine --distribute 192.168.1.100:2222 192.168.1.101
   # 批量主机（花括号数字区间、IPv4 末段区间； per-host 端口仍用 :PORT，全局 SSH 端口用 --ssh-port）
   python3 k8simgmanager.py --pack nginx:alpine --distribute worker-{01..17} 192.168.4.7-19 --ssh-port 58595
   python3 k8simgmanager.py --pack nginx:alpine --distribute 192.168.1.100 --cleanup  # 分发成功后清理旧文件
   
   # 方式2: 使用--tar指定已存在的tar文件
   python3 k8simgmanager.py --distribute 192.168.1.100 --tar images_batch.tar
   
   # 方式3: 分步执行（先打包，再分发）
   python3 k8simgmanager.py --pack nginx:alpine
   python3 k8simgmanager.py --distribute 192.168.1.100  # 使用输出目录中的tar文件

5. 使用JSON配置文件（推荐）:
   # config.json 示例（下载、打包、分发）:
   # {
   #   "download": ["nginx:alpine", "redis:latest", "ubuntu:20.04"],
   #   "pack": ["nginx:alpine", "redis:latest"],
   #   "distribute": [
   #     "192.168.1.100:2222",
   #     "worker-{01..17}",
   #     "192.168.4.7-19",
   #     {"host": "host2.example.com", "port": 2222}
   #   ]
   # }

   # config_delete.json 示例（删除本地镜像）:
   # {
   #   "delete": ["nginx:alpine", "redis:latest"]
   # }

   # config_delete_remote.json 示例（删除远程镜像）:
   # {
   #   "delete": {
   #     "images": ["nginx:alpine", "redis:latest"],
   #     "hosts": ["host1:2222", "host2"]
   #   }
   # }

   # 从配置文件执行所有操作（推荐）
   python3 k8simgmanager.py --config config.json
   
   # 从配置文件执行特定操作（命令行参数会覆盖配置文件）
   python3 k8simgmanager.py --config config.json --pack
   python3 k8simgmanager.py --config config.json --distribute
   
   # 使用配置文件删除镜像
   python3 k8simgmanager.py --config config_delete.json
   python3 k8simgmanager.py --config config_delete_remote.json --remote-runtime nerdctl

6. 组合使用 - 完整流程（推荐）:
   # 下载、打包、分发全流程（同一会话）
   python3 k8simgmanager.py \\
        --download nginx:alpine redis:latest \\
        --pack nginx:alpine redis:latest \\
        --distribute host1:2222 host2
   
   # 完整流程并清理旧文件
   python3 k8simgmanager.py \\
        --download nginx:alpine redis:latest \\
        --pack nginx:alpine redis:latest \\
        --distribute host1:2222 host2 \\
        --cleanup

7. 组合使用 - 下载并分发:
   python3 k8simgmanager.py \\
        --download ubuntu:20.04 \\
        --pack ubuntu:20.04 \\
        --distribute host1 host2 \\
        --remote-runtime nerdctl \\
        --namespace k8s.io

8. 分步执行（适合长时间操作）:
   # 步骤1: 下载镜像
   python3 k8simgmanager.py --download nginx:alpine redis:latest
   
   # 步骤2: 打包镜像
   python3 k8simgmanager.py --pack nginx:alpine redis:latest
   
   # 步骤3: 分发镜像（使用输出目录中的tar文件）
   python3 k8simgmanager.py --distribute 192.168.1.100 192.168.1.101

9. 仅分发已存在的tar文件:
   python3 k8simgmanager.py \\
        --distribute host1 host2 \\
        --tar my_images.tar \\
        --remote-runtime nerdctl

10. 使用nerdctl运行时:
    python3 k8simgmanager.py \\
         --local-runtime nerdctl \\
         --namespace k8s.io \\
         --pack nginx:alpine \\
         --distribute host1 \\
         --remote-runtime docker

11. 清理旧的自动生成文件:
    # 打包后清理旧文件
    python3 k8simgmanager.py --pack nginx:alpine --cleanup
    
    # 分发成功后清理旧文件
    python3 k8simgmanager.py --pack nginx:alpine --distribute host1 --cleanup

参数说明:
  --pack IMAGES        打包本地镜像 (如未指定，使用download列表)
  --download IMAGES    从仓库下载镜像
  --delete IMAGES      删除镜像 (只能单独使用)
  --delete-hosts HOSTS 指定要删除镜像的远程主机 (与--delete一起使用)
  --distribute HOSTS   分发到远程主机 (格式: host[:port]；可写 worker-{01..17}、192.168.4.7-19)
  --tar FILE           指定要分发的tar文件 (与--distribute一起使用)
  --config FILE        使用JSON配置文件
  --cleanup            在所有操作成功后清理旧的自动生成文件 (images_batch_*.tar格式)
  --local-runtime      本地运行时 [docker|nerdctl]
  --remote-runtime     远程运行时 [docker|nerdctl]
  --namespace          nerdctl命名空间 (默认: k8s.io)
  --output-dir         打包输出目录
  --ssh-user           SSH用户名
  --ssh-port           SSH端口 (默认: 22)
  --ssh-key            SSH私钥路径

注意:
  - --delete 只能单独使用，不能与其他操作组合
  - --distribute 需要指定 --tar 或 --pack，或者输出目录中已有tar文件
  - --pack 如未指定镜像，则使用 --download 的镜像列表
  - 使用 --config 时，如果只指定 --config，会执行配置文件中的所有操作
  - 使用 --config 时，命令行参数会覆盖配置文件中的对应设置
  - 清理逻辑：默认不清理，只有指定 --cleanup 时才会在所有操作成功后清理旧的自动生成文件
  - ACID原则：操作过程中不清理文件，失败时只清理本次会话产生的文件
  - 端口可以在host参数中指定 (host:port)，优先级高于 --ssh-port
  - 当使用nerdctl时，会自动添加-n参数指定命名空间
  - 批量操作效率高，适合大量镜像处理
"""
    print(examples)


def main():
    parser = argparse.ArgumentParser(
        description="高效镜像管理工具 - 支持批量打包、下载、删除和分发",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False  # 禁用默认的-h，使用自定义的
    )

    # 帮助参数
    parser.add_argument(
        "-h", "--help",
        action="store_true",
        help="显示详细使用说明和示例"
    )

    # 运行时选项
    parser.add_argument(
        "--local-runtime",
        choices=["docker", "nerdctl"],
        default="docker",
        help="本地容器运行时 (默认: docker)"
    )

    parser.add_argument(
        "--remote-runtime",
        choices=["docker", "nerdctl"],
        default="docker",
        help="远程容器运行时 (默认: docker)"
    )

    parser.add_argument(
        "--namespace",
        default="k8s.io",
        help="nerdctl命名空间 (默认: k8s.io)"
    )

    # 核心功能
    parser.add_argument(
        "--pack",
        nargs="*",
        metavar="IMAGE",
        help="打包本地镜像 (例如: --pack nginx:alpine redis:latest)"
    )

    parser.add_argument(
        "--download",
        nargs="*",
        metavar="IMAGE",
        help="下载镜像 (例如: --download nginx:alpine redis:latest)"
    )

    parser.add_argument(
        "--delete",
        nargs="*",
        metavar="IMAGE",
        help="删除镜像 (例如: --delete nginx:alpine redis:latest)。可配合--delete-hosts删除远程镜像"
    )

    parser.add_argument(
        "--delete-hosts",
        nargs="*",
        metavar="HOST[:PORT]",
        help="指定要删除镜像的远程主机 (例如: --delete-hosts host1:2222 host2；批量写法同--distribute)。与--delete一起使用"
    )

    parser.add_argument(
        "--distribute",
        nargs="*",
        metavar="HOST[:PORT]",
        help="分发镜像到远程主机 (例如: --distribute host1:2222 host2；支持 worker-{01..17}、192.168.4.7-19)"
    )

    parser.add_argument(
        "--tar",
        metavar="FILE",
        help="指定要分发的tar文件 (与--distribute一起使用)"
    )

    # JSON配置文件
    parser.add_argument(
        "--config",
        metavar="FILE",
        help="使用JSON配置文件"
    )

    # 其他选项
    parser.add_argument(
        "--output-dir",
        default="./packed_images",
        help="打包输出目录 (默认: ./packed_images)"
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
        help="禁用SSH主机密钥检查（不推荐，存在安全风险）"
    )
    
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="在所有操作成功后清理旧的自动生成文件（images_batch_*.tar格式）"
    )

    args = parser.parse_args()

    # 显示帮助
    if args.help:
        show_examples()
        return

    # 加载配置文件（如果指定）
    config = {}
    if args.config:
        config = load_json_config(args.config)

    # 从配置文件或命令行参数获取数据
    images_to_download = []
    images_to_pack = []
    images_to_delete = []
    hosts_to_distribute = []
    
    # 标志：是否要执行这些操作（从配置文件或命令行）
    should_download = False
    should_pack = False
    should_delete = False
    should_distribute = False

    # 处理下载镜像
    if args.config and config.get("download"):
        # 从配置文件读取
        images_to_download = config["download"]
        if isinstance(images_to_download, str):
            images_to_download = [images_to_download]
        should_download = True
    if args.download is not None:
        # 命令行参数覆盖配置文件
        if args.download:
            images_to_download = args.download
        should_download = True
    
    # 基本验证：过滤空值
    images_to_download = [img for img in images_to_download if img]
    if not images_to_download:
        should_download = False

    # 处理打包镜像 - 如果pack为空，使用download列表
    if args.config and config.get("pack") is not None:
        # 从配置文件读取
        images_to_pack = config["pack"]
        if isinstance(images_to_pack, str):
            images_to_pack = [images_to_pack]
        should_pack = True
    if args.pack is not None:
        # 命令行参数覆盖配置文件
        if args.pack:
            images_to_pack = args.pack
        elif not images_to_pack:
            # pack为空，使用download列表
            images_to_pack = images_to_download
        should_pack = True
    # 注意：只有当有distribute时才自动pack，如果只有download则不自动pack
    
    # 基本验证：过滤空值
    images_to_pack = [img for img in images_to_pack if img]
    if not images_to_pack:
        should_pack = False

    # 处理删除镜像
    hosts_to_delete = []
    if args.config and config.get("delete"):
        # 从配置文件读取
        delete_config = config["delete"]
        if isinstance(delete_config, dict):
            # 支持配置格式: {"images": [...], "hosts": [...]}
            images_to_delete = delete_config.get("images", [])
            hosts_to_delete = delete_config.get("hosts", [])
            if isinstance(images_to_delete, str):
                images_to_delete = [images_to_delete]
            if isinstance(hosts_to_delete, str):
                hosts_to_delete = [hosts_to_delete]
        elif isinstance(delete_config, list):
            # 简单列表格式，只包含镜像
            images_to_delete = delete_config
        elif isinstance(delete_config, str):
            images_to_delete = [delete_config]
        should_delete = True
    if args.delete is not None:
        # 命令行参数覆盖配置文件
        if args.delete:
            images_to_delete = args.delete
        should_delete = True
    
    # 处理删除主机（命令行参数覆盖配置文件）
    if args.delete_hosts is not None:
        if args.delete_hosts:
            hosts_to_delete = args.delete_hosts
        should_delete = True
    
    # 基本验证：过滤空值；delete-hosts 与 distribute 相同批量写法
    images_to_delete = [img for img in images_to_delete if img]
    hosts_to_delete = [h for h in hosts_to_delete if h]
    hosts_to_delete = expand_distribution_hosts(hosts_to_delete)
    if not images_to_delete:
        should_delete = False

    # 处理分发主机
    if args.config and config.get("distribute"):
        # 从配置文件读取
        distribute_config = config["distribute"]
        if isinstance(distribute_config, list):
            for item in distribute_config:
                if isinstance(item, str):
                    hosts_to_distribute.append(item)
                elif isinstance(item, dict):
                    host = item.get("host")
                    port = item.get("port", 22)
                    if host:
                        hosts_to_distribute.append(f"{host}:{port}")
        elif isinstance(distribute_config, str):
            hosts_to_distribute = [distribute_config]
        should_distribute = True
    if args.distribute is not None:
        # 命令行参数覆盖配置文件
        if args.distribute:
            hosts_to_distribute = args.distribute
        should_distribute = True
    
    # 基本验证：过滤空值；支持 worker-{01..17}、192.168.4.7-19 等批量写法
    hosts_to_distribute = [h for h in hosts_to_distribute if h]
    hosts_to_distribute = expand_distribution_hosts(hosts_to_distribute)
    if not hosts_to_distribute:
        should_distribute = False

    # 如果配置文件没有pack，命令行也没有指定pack，但有distribute和download，自动使用download列表进行pack
    if should_distribute and not should_pack and images_to_download:
        images_to_pack = images_to_download
        should_pack = True
        logger.info("检测到有distribute但没有pack，自动使用download列表进行pack")

    # 验证参数逻辑
    errors = []

    # 检查--delete只能单独使用
    if should_delete and (should_pack or should_download or should_distribute):
        errors.append("--delete 只能单独使用，不能与其他操作组合")
    
    # 检查--delete-hosts必须与--delete一起使用
    if hosts_to_delete and not should_delete:
        errors.append("--delete-hosts 必须与 --delete 一起使用")

    # 检查--distribute：如果指定了--tar，使用指定的tar；如果指定了--pack，使用打包后的tar；否则检查输出目录
    if should_distribute:
        if not args.tar and not should_pack:
            # 没有指定--tar或--pack，检查输出目录是否有tar文件
            output_path = Path(args.output_dir)
            if not output_path.exists() or not list(output_path.glob("*.tar")):
                errors.append("--distribute 需要指定 --tar 或 --pack，或者输出目录中已有tar文件")

    # 检查--tar必须与--distribute一起使用
    if args.tar and not should_distribute:
        errors.append("--tar 必须与 --distribute 一起使用")

    # 检查--tar文件是否存在
    if args.tar and not Path(args.tar).exists():
        errors.append(f"指定的tar文件不存在: {args.tar}")

    # 检查运行时
    if not RuntimeChecker.check_runtime(args.local_runtime):
        errors.append(f"本地运行时不可用: {args.local_runtime}")
    
    # 验证SSH端口
    if not InputValidator.validate_port(args.ssh_port):
        errors.append(f"SSH端口无效: {args.ssh_port} (范围: {MIN_PORT}-{MAX_PORT})")

    # 检查配置文件是否包含有效操作
    if args.config:
        has_valid_operation = bool(
            config.get("download") or 
            config.get("pack") is not None or 
            config.get("distribute") or 
            config.get("delete")
        )
        if not has_valid_operation:
            errors.append("使用 --config 时，配置文件必须包含至少一个有效操作 (download, pack, distribute, 或 delete)")
    
    # 检查是否提供了任何操作
    if not (should_download or should_pack or should_delete or should_distribute):
        errors.append("至少需要指定一个操作 (--pack, --download, --delete, 或 --distribute)，或使用 --config 指定配置文件")

    if errors:
        print("\n错误:")
        for error in errors:
            print(f"  ✗ {error}")
        print("\n使用 -h 查看完整示例")
        return

    # 如果指定了tar文件，先复制到输出目录
    if args.tar:
        logger.info("=== 处理指定的tar文件 ===")
        manager = ImageManager(args.local_runtime, args.namespace)
        if not manager.save_specific_tar(args.tar, args.output_dir):
            logger.error("处理tar文件失败")
            return

    # 执行删除（如果需要）
    if should_delete:
        # 解析删除主机列表
        delete_hosts = None
        if hosts_to_delete:
            delete_hosts = [parse_host_with_port(host_str) for host_str in hosts_to_delete]
        
        # 如果指定了远程主机，使用remote_runtime；否则使用local_runtime
        delete_runtime = args.remote_runtime if delete_hosts else args.local_runtime
        
        success = delete_images(
            images_to_delete,
            delete_runtime,
            args.namespace,
            delete_hosts,
            args.ssh_user,
            args.ssh_key,
            strict_host_key_checking=not args.disable_ssh_host_check
        )
        if not success:
            logger.error("删除失败")
            return

    # 执行下载（如果需要）
    if should_download:
        success = download_images(images_to_download, args.local_runtime, args.namespace)
        if not success:
            logger.error("下载失败")
            return

    # 执行打包（如果需要）
    packed_files = []  # 跟踪本次会话产生的文件，用于失败时清理
    if should_pack:
        success, packed_files = pack_images(images_to_pack, args.local_runtime, args.output_dir, args.namespace)
        if not success:
            logger.error("打包失败")
            return

    # 执行分发（如果需要）
    if should_distribute:
        # 解析主机和端口
        hosts_with_ports = []
        for host_str in hosts_to_distribute:
            host, port = parse_host_with_port(host_str)
            # 如果在host参数中指定了端口，使用它；否则使用ssh-port参数
            if port == 22:  # 默认端口，可能没有指定
                port = args.ssh_port
            hosts_with_ports.append((host, port))

        # 分发镜像
        success = distribute_images(
            hosts=hosts_with_ports,
            images_dir=args.output_dir,
            remote_runtime=args.remote_runtime,
            ssh_user=args.ssh_user,
            ssh_key=args.ssh_key,
            namespace=args.namespace,
            strict_host_key_checking=not args.disable_ssh_host_check
        )
        
        # ACID原子性：如果分发失败，清理本次会话产生的文件
        if not success:
            logger.warning("分发失败，清理本次会话产生的文件（保证原子性）")
            cleanup_session_files(packed_files)
            return

    # 如果用户指定了--cleanup，在所有操作成功后清理旧的自动生成文件
    if args.cleanup:
        cleanup_old_auto_files(args.output_dir)

    logger.info("=== 所有操作完成 ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n操作被用户中断")
        sys.exit(1)
    except Exception as err:
        logger.error(f"程序异常: {err}")
        sys.exit(1)
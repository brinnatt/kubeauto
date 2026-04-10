#!/usr/bin/env python3
"""
Kafka 自动化部署与运维封装（KRaft 模式），调用发行版 bin/ 下官方脚本；行为须与下列 **Apache Kafka 官方文献** 可对照。

**官方依据（Apache Kafka Documentation / KIP，非社区帖）**
- KRaft 概览与运维： https://kafka.apache.org/documentation/#kraft
- Broker / Topic 等配置说明： https://kafka.apache.org/documentation/#brokerconfigs
- 常用运维（kafka-topics、kafka-reassign-partitions 的 --generate / --execute / --verify、Broker 下线等）：
  https://kafka.apache.org/documentation/#basic_ops
  其中「Partition reassignment」「Decommissioning brokers」描述分区迁移与下线前须迁走副本；
  「Adding and removing topics」说明 topic 名长度上限（与日志目录命名相关）。
- 监控与指标语义： https://kafka.apache.org/documentation/#monitoring
- 运行 Kafka 的 JVM 版本说明： https://kafka.apache.org/documentation/#java
- KRaft 在 3.3 起对新集群标为 production-ready：KIP-833
  https://cwiki.apache.org/confluence/display/KAFKA/KIP-833:+Mark+KRaft+as+Production+Ready

**本脚本独有（官方文档未规定，仅为工程化默认值）**
- 环境变量 KAFKA_CLI_TIMEOUT、KAFKA_LOG_DIR、KAFKA_ADVERTISED_HOST、KAFKA_CLI_ASSUME_VERSION 等。
- Topic 创建/删除对 CLI 典型 **英文** 报错串做兼容（幂等友好）；以实际 kafka-topics.sh 退出码与输出为准，非协议层保证。
- 分区重分配 execute/verify 的超时下限（与 KAFKA_CLI_TIMEOUT 取 max）为本脚本策略。
- 与同仓库 StarCli 一致的 SSH/日志/退出码等 **本仓库** 约定，不属于 Apache Kafka 项目规范。

能力：单机/批量/远程部署、systemd、清理、封装官方 CLI（--bootstrap-server / --command-config）。
systemd 默认 User/Group 为 kafka：账户须事先存在（见 systemd 文档 User=/Group=）；测试可用 --user root --group root。
退出码：EXIT_OK(0) / EXIT_ERROR(1)，便于脚本判断。
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
from typing import Any, Dict, List, Optional, Tuple, Union

# 日志配置常量（可通过环境变量 KAFKA_LOG_DIR 覆盖日志目录，避免依赖当前工作目录）
LOG_DIR = os.getenv("KAFKA_LOG_DIR", os.path.join(os.getcwd(), "logs"))
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, "kafka_deploy.log")
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Kafka 官方默认端口与路径
DEFAULT_BROKER_PORT = 9092
DEFAULT_CONTROLLER_PORT = 9093
DEFAULT_LOG_DIR = "/tmp/kafka-logs"
DEFAULT_METADATA_LOG_DIR = "/tmp/kraft-combined-logs"

# 进程退出码（与 StarCli 及常见 CLI 约定一致，便于 CI/自动化判断）
EXIT_OK = 0
EXIT_ERROR = 1


def _kafka_cli_timeout_sec(default: int = 120) -> int:
    """
    本脚本对子进程 subprocess 的超时（非 Kafka 文档规定）。
    环境变量 KAFKA_CLI_TIMEOUT（10～86400）覆盖默认值。
    """
    try:
        v = int(os.getenv("KAFKA_CLI_TIMEOUT", str(default)))
        return max(10, min(86400, v))
    except ValueError:
        return default


def _reassign_cmd_timeout_sec() -> int:
    """
    kafka-reassign-partitions.sh 的 --execute / --verify 可能长时间运行（官方文档说明见 #basic_ops 分区重分配章节）。
    本脚本取 max(KAFKA_CLI_TIMEOUT, 3600) 作为子进程超时下限；3600 非 Apache 规定。
    """
    return max(_kafka_cli_timeout_sec(120), 3600)


def _validate_bootstrap_server(bootstrap_server: str) -> Tuple[bool, str]:
    """校验 --bootstrap-server 非空且主机/端口可解析（减少 silent 失败）。"""
    s = (bootstrap_server or "").strip()
    if not s:
        return False, "bootstrap server 不能为空"
    if ":" in s:
        host, _, port_part = s.rpartition(":")
        host = host.strip()
        port_part = port_part.strip()
        if port_part:
            try:
                p = int(port_part)
                if not InputValidator.validate_port(p):
                    return False, f"bootstrap 端口无效: {port_part}"
            except ValueError:
                return False, f"bootstrap 端口无效: {port_part}"
        if host and not InputValidator.validate_hostname(host):
            return False, f"bootstrap 主机无效: {host}"
    else:
        if not InputValidator.validate_hostname(s):
            return False, f"bootstrap 主机无效: {s}"
    return True, ""


def _validate_command_config_path(command_config: Optional[str]) -> Optional[str]:
    """若提供 SASL/SSL 客户端配置，文件须存在。"""
    if not (command_config or "").strip():
        return None
    p = Path(command_config).expanduser()
    if not p.is_file():
        return f"--command-config 文件不存在或不可读: {command_config}"
    return None


def _normalize_topic_name(topic: str) -> str:
    return (topic or "").strip()


def _validate_topic_name(topic: str) -> Tuple[bool, str]:
    """
    Topic 名非空与长度上限（≤249）。
    依据：Apache Kafka Documentation「Basic Kafka Operations」→「Adding and removing topics」
    （日志目录下文件夹命名 topic-partition，故 topic 名长度受限；与官方文档同页说明一致）。
    """
    t = _normalize_topic_name(topic)
    if not t:
        return False, "Topic 名称不能为空"
    if len(t) > 249:
        return False, "Topic 名称长度须 ≤ 249（见官方文档 #basic_ops Adding and removing topics）"
    return True, ""


def _cli_already_exists(msg: str) -> bool:
    """脚本侧启发式：匹配 kafka-topics 常见英文报错，非 Kafka 协议或文档保证。"""
    m = (msg or "").lower()
    return "already exists" in m or "already exist" in m


def _cli_topic_missing(msg: str) -> bool:
    """脚本侧启发式：匹配常见「topic 不存在」英文描述；请以实际 CLI 输出为准。"""
    m = (msg or "").lower()
    return (
        "unknown topic" in m
        or "does not exist" in m
        or "not found" in m
    )


def _advertised_host() -> str:
    """生成 advertised / quorum bootstrap 时使用的主机名或 IP（每次调用读取环境变量，便于部署前 export）。"""
    return (os.getenv("KAFKA_ADVERTISED_HOST") or "127.0.0.1").strip() or "127.0.0.1"


class CommandExecutionError(Exception):
    """命令执行异常；message 可通过 str(e) 或 e.message 获取，便于日志与告警"""

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

    def __str__(self) -> str:
        return self.message


def setup_logger(
        name: str = "kafka_deploy",
        log_file: Optional[str] = None,
        level: int = DEFAULT_LOG_LEVEL,
        fmt: str = DEFAULT_FORMAT,
        datefmt: str = DEFAULT_DATEFMT,
        handlers: Optional[List[logging.Handler]] = None
) -> logging.Logger:
    """
    设置日志记录器（与 StarCli 一致：先确保日志目录存在，再挂 RotatingFileHandler + 按需 stdout）。
    首次调用且未传 handlers 时使用默认：文件（UTF-8、轮转）+ stdout；
    若 logger 已有 handler 则直接返回，避免重复添加。可通过 KAFKA_LOG_DIR 环境变量覆盖日志目录。
    """
    if handlers is None:
        try:
            if not os.path.exists(LOG_DIR):
                os.makedirs(LOG_DIR, exist_ok=True)
        except OSError as e:
            print(f"Warning: 无法创建日志目录 {LOG_DIR}: {e}", file=sys.stderr)

    log = logging.getLogger(name)
    log.setLevel(level)

    if log.hasHandlers():
        return log

    formatter = logging.Formatter(fmt, datefmt)

    if handlers is None:
        try:
            log_file = log_file or DEFAULT_LOG_FILE
            file_handler = RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            file_handler.addFilter(lambda record: not getattr(record, "skip_file", False))
            log.addHandler(file_handler)
        except OSError as e:
            print(f"Warning: 日志目录不可用，仅输出到 stdout: {e}", file=sys.stderr)

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(level)
        stdout_handler.addFilter(lambda record: getattr(record, "to_stdout", False))
        log.addHandler(stdout_handler)
    else:
        for handler in handlers:
            handler.setLevel(level)
            if not handler.formatter:
                handler.setFormatter(formatter)
            log.addHandler(handler)

    return log


logger = setup_logger(__name__)

# KIP-833：Kafka 3.3 起 KRaft 对新集群标为 production-ready（见 KIP 正文）。低于 3.3 的 KRaft 自动部署本脚本默认拒绝。
KRAFT_DEPLOY_MIN_VERSION: Tuple[int, int, int] = (3, 3, 0)


def infer_kafka_release(kafka_home: Path, assume_version: Optional[str] = None) -> Optional[Tuple[int, int, int]]:
    """
    从 --assume-kafka-version / 环境变量 KAFKA_CLI_ASSUME_VERSION、安装路径名、libs/kafka-server-common-*.jar 推断
    Kafka 发行版语义版本 (major, minor, patch)；均失败时返回 None（脚本将按最严策略：Java 17+）。
    """
    raw = (assume_version or os.getenv("KAFKA_CLI_ASSUME_VERSION") or "").strip()
    if raw:
        m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", raw)
        if m:
            return int(m.group(1)), int(m.group(2)), int(m.group(3))
        logger.warning(
            "KAFKA_CLI_ASSUME_VERSION / --assume-kafka-version 须为 x.y.z，已忽略: %s",
            raw,
            extra={"to_stdout": True},
        )

    try:
        resolved = str(kafka_home.resolve())
    except OSError:
        resolved = str(kafka_home)

    m = re.search(r"kafka_2\.\d+-(\d+)\.(\d+)\.(\d+)", resolved, re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))
    base = kafka_home.name
    m = re.match(r"kafka_2\.\d+-(\d+)\.(\d+)\.(\d+)$", base, re.I)
    if m:
        return int(m.group(1)), int(m.group(2)), int(m.group(3))

    libs = kafka_home / "libs"
    best_jar: Optional[Tuple[int, int, int]] = None
    if libs.is_dir():
        ver_re = re.compile(r"kafka-server-common-(\d+)\.(\d+)\.(\d+)\.jar$", re.I)
        for j in libs.glob("kafka-server-common-*.jar"):
            mj = ver_re.search(j.name)
            if mj:
                t = (int(mj.group(1)), int(mj.group(2)), int(mj.group(3)))
                if best_jar is None or t > best_jar:
                    best_jar = t
    return best_jar


def min_java_major_for_kafka(kafka_release: Optional[Tuple[int, int, int]]) -> int:
    """
    本脚本部署门禁用的最低 JDK 主版本号。
    官方对运行时的说明见 https://kafka.apache.org/documentation/#java （当前文档以 Java 17/21/25 为完全支持主线）；
    各 tarball 附带 README 亦会写明该发行版最低 JDK。此处对 4.x 用 17、对 3.x 用 11 与常见发行版一致；
    未知版本时按 4.x 策略要求 17。
    """
    if kafka_release is None:
        return 17
    major, _, _ = kafka_release
    return 17 if major >= 4 else 11


def _listener_scheme_port(listeners: str, scheme: str, default_port: int) -> int:
    """从 listeners 行解析 SCHEME://host:port 中的端口（如 PLAINTEXT、CONTROLLER）。"""
    m = re.search(rf"{re.escape(scheme)}://[^:]*:(\d+)", listeners or "", re.I)
    return int(m.group(1)) if m else default_port


def _kraft_combined_listener_properties(listeners_override: Optional[str]) -> Dict[str, str]:
    """
    KRaft combined（process.roles=broker,controller）监听器必选配置，与官方 KRaft 文档（3.3+ / 4.x）一致。
    StorageTool / KafkaConfig（尤其 4.x）要求显式设置 controller.listener.names，且 listeners 须同时包含
    PLAINTEXT（broker）与 CONTROLLER；并需 inter.broker.listener.name、
    listener.security.protocol.map、advertised.listeners、controller.quorum.bootstrap.servers。
    advertised 与 quorum bootstrap 主机默认取环境变量 KAFKA_ADVERTISED_HOST（未设置则为 127.0.0.1）。
    参考: https://kafka.apache.org/documentation/#brokerconfigs_controller.listener.names
    """
    broker_port = DEFAULT_BROKER_PORT
    ctrl_port = DEFAULT_CONTROLLER_PORT
    adv_host = _advertised_host()
    if listeners_override and listeners_override.strip():
        ls = listeners_override.strip().rstrip(",")
        if "CONTROLLER" not in ls.upper():
            ls = f"{ls},CONTROLLER://0.0.0.0:{ctrl_port}"
            logger.info(
                "listeners 未包含 CONTROLLER，已自动追加（KRaft combined 必选）",
                extra={"to_stdout": True},
            )
    else:
        ls = f"PLAINTEXT://0.0.0.0:{broker_port},CONTROLLER://0.0.0.0:{ctrl_port}"
    pm = re.search(r"PLAINTEXT://[^:]*:(\d+)", ls, re.I)
    if pm:
        broker_port = int(pm.group(1))
    cm = re.search(r"CONTROLLER://[^:]*:(\d+)", ls, re.I)
    if cm:
        ctrl_port = int(cm.group(1))
    return {
        "listeners": ls,
        "advertised.listeners": (
            f"PLAINTEXT://{adv_host}:{broker_port},CONTROLLER://{adv_host}:{ctrl_port}"
        ),
        "controller.quorum.bootstrap.servers": f"{adv_host}:{ctrl_port}",
        "controller.listener.names": "CONTROLLER",
        "inter.broker.listener.name": "PLAINTEXT",
        "listener.security.protocol.map": "CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
    }


def _kraft_broker_listener_properties(listeners_override: Optional[str]) -> Dict[str, str]:
    """
    KRaft broker-only（3.3+ / 4.x）：在仅使用 PLAINTEXT 的常见场景下补充 inter.broker.listener.name、
    listener.security.protocol.map、advertised.listeners，避免配置不完整导致启动或 format 异常。
    若 listeners 含 SSL/SASL 等，请通过 extra_properties 自行提供完整协议映射与 advertised。
    """
    adv_host = _advertised_host()
    ls = (listeners_override or "").strip() or f"PLAINTEXT://0.0.0.0:{DEFAULT_BROKER_PORT}"
    broker_port = DEFAULT_BROKER_PORT
    pm = re.search(r"PLAINTEXT://[^:]*:(\d+)", ls, re.I)
    if pm:
        broker_port = int(pm.group(1))
    out: Dict[str, str] = {"listeners": ls}
    if (
        re.search(r"\bPLAINTEXT://", ls, re.I)
        and "SSL" not in ls.upper()
        and "SASL" not in ls.upper()
    ):
        out["inter.broker.listener.name"] = "PLAINTEXT"
        out["listener.security.protocol.map"] = "PLAINTEXT:PLAINTEXT"
        out["advertised.listeners"] = f"PLAINTEXT://{adv_host}:{broker_port}"
    return out


MIN_PORT = 1
MAX_PORT = 65535
MAX_HOSTNAME_LENGTH = 253


class InputValidator:
    """
    部署与 CLI 预检用的格式校验（主机名、端口、路径等）。
    其中端口范围、topic 长度等部分与官方文档或配置语义对齐；其余为脚本健壮性检查。
    """

    @staticmethod
    def validate_port(port: int) -> bool:
        """端口须为 1..65535 的整数（拒绝 bool 等）"""
        if not isinstance(port, int) or isinstance(port, bool):
            return False
        return MIN_PORT <= port <= MAX_PORT

    @staticmethod
    def validate_hostname(hostname: str) -> bool:
        """接受 IPv4 与常见主机名（字母数字、点、连字符，长度≤253）；不含 IPv6 字面量"""
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
        """路径格式基本校验（本脚本目标环境多为类 Unix；非 Kafka 文档条款）。"""
        if not path or ".." in path:
            return False
        try:
            Path(path).resolve()
            return True
        except (OSError, ValueError):
            return False


class SecurityChecker:
    """安全检查器（与 StarCli 一致：SSH 私钥权限告警，不阻断连接，由运维决定是否修正）"""

    @staticmethod
    def check_ssh_key_permissions(key_path: str) -> bool:
        """类 Unix 上建议 0600；不符合时告警并返回 False，与 StarCli 行为一致。"""
        try:
            if os.name != "posix":
                return True
            file_mode = stat.S_IMODE(os.stat(key_path).st_mode)
            if file_mode != 0o600:
                logger.warning(
                    "SSH 私钥权限不安全: %s (建议 chmod 600，当前 %s)",
                    key_path,
                    oct(file_mode),
                    extra={"to_stdout": True},
                )
                return False
            return True
        except OSError:
            return False


class SSHManager:
    """
    SSH 管理器（与 StarCli 同构）：BatchMode、ConnectTimeout、保活、StrictHostKeyChecking、
    失败时合并 stdout/stderr 便于排障。
    """

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
            raise FileNotFoundError(f"SSH 密钥文件不存在: {self.key_path}")
        SecurityChecker.check_ssh_key_permissions(self.key_path)

    def _build_ssh_options(self) -> List[str]:
        """与 StarCli 一致：超时、保活、非交互、端口、密钥、主机密钥策略。"""
        ssh_options = [
            "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=60",
            "-o", "ServerAliveCountMax=3",
            "-o", "BatchMode=yes",
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
            logger.warning("警告: 已禁用 SSH 主机密钥检查", extra={"to_stdout": True})
            ssh_options.extend(["-o", "StrictHostKeyChecking=no"])
        return ssh_options

    def run_command(self, cmd: str, timeout: Optional[int] = None) -> Tuple[bool, str]:
        """在远程执行命令，返回 (成功, 输出)；失败时合并 stderr/stdout（与 StarCli 一致）。"""
        if not cmd:
            return False, "命令为空"
        ssh_cmd = ["ssh"] + self._build_ssh_options() + [f"{self.username}@{self.host}", cmd]
        try:
            result = run_command(ssh_cmd, check=False, capture_output=True, timeout=timeout or 3600)
            if result.returncode == 0:
                return True, result.stdout if result.stdout else ""
            output_parts = []
            if result.stderr:
                output_parts.append(f"[stderr] {result.stderr}")
            if result.stdout:
                output_parts.append(f"[stdout] {result.stdout}")
            if output_parts:
                return False, "\n".join(output_parts)
            return False, f"命令执行失败，返回码: {result.returncode}"
        except Exception as e:
            logger.error(f"SSH 命令执行失败: {e}", extra={"to_stdout": True})
            return False, str(e)

    def copy_file(self, src: str, dst: str) -> bool:
        """scp 到远程；选项与 StarCli 一致单独构造（-P 端口，不用 ssh 的 -p 映射）。"""
        src_path = Path(src).resolve()
        if not src_path.exists():
            logger.error(f"源文件不存在: {src}", extra={"to_stdout": True})
            return False
        if not dst or ".." in dst:
            logger.error("目标路径无效", extra={"to_stdout": True})
            return False
        scp_options = [
            "-o", "ConnectTimeout=10",
            "-o", "BatchMode=yes",
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
            result = run_command(scp_cmd, check=False, capture_output=False, timeout=1800)
            return result.returncode == 0
        except Exception as e:
            logger.error("复制文件到远程失败 {} -> {}: {}".format(src, dst, e), extra={"to_stdout": True})
            return False


def run_command(
        cmd: Union[List[str], Tuple[str, ...], str],
        check: bool = True,
        capture_output: bool = True,
        allowed_exit_codes: Optional[List[int]] = None,
        **kwargs
):
    """
    执行 shell 命令并统一处理错误。成功时返回 CompletedProcess；
    当 allowed_exit_codes 非空且进程退出码在列表中时返回 CalledProcessError 实例（含 returncode/stdout/stderr）。
    cmd 为 str 时按空白分割，参数含空格或引号时请使用 list。
    """
    logger.debug(f"Executing: {' '.join(cmd) if isinstance(cmd, (list, tuple)) else cmd}")

    if isinstance(cmd, (list, tuple)):
        if kwargs.get("shell"):
            cmd = " ".join(cmd)
    elif isinstance(cmd, str):
        if not kwargs.get("shell"):
            cmd = cmd.split()
    else:
        raise CommandExecutionError(f"Command must be list, tuple or string: {cmd}")

    if capture_output and ("stdout" in kwargs or "stderr" in kwargs):
        capture_output = False

    # 非交互场景避免子进程阻塞读 stdin；若调用方使用 input= 则勿占用 DEVNULL
    run_kw = dict(kwargs)
    if run_kw.get("stdin") is None and "input" not in run_kw:
        run_kw["stdin"] = subprocess.DEVNULL
    if run_kw.get("encoding") is None:
        run_kw["encoding"] = "utf-8"
        run_kw.setdefault("errors", "replace")

    try:
        result = subprocess.run(cmd, check=check, capture_output=capture_output, text=True, **run_kw)
        return result
    except subprocess.TimeoutExpired as e:
        logger.error("命令执行超时: {}".format(e), extra={"to_stdout": True})
        raise CommandExecutionError(f"Command timeout: {cmd}")
    except subprocess.CalledProcessError as e:
        if allowed_exit_codes and e.returncode in allowed_exit_codes:
            # 允许的退出码时返回异常对象，调用方可通过 e.returncode / e.stdout / e.stderr 使用
            return e
        error_msg = (
            f"Command failed with exit code {e.returncode}: {cmd}\n"
            f"Stderr: {e.stderr.strip() if e.stderr else '(empty)'}\n"
            f"Stdout: {e.stdout.strip() if e.stdout else '(empty)'}"
        )
        raise CommandExecutionError(error_msg)
    except Exception as e:
        logger.error("命令执行异常: {}".format(e), extra={"to_stdout": True})
        raise CommandExecutionError(f"Command failed: {e}")


def _atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    """
    原子写入文本文件：同目录临时文件 + os.replace，避免进程崩溃留下半截配置（StarCli 直写可改为本模式）。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        tmp.write_text(content, encoding=encoding)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class EnvironmentChecker:
    """环境检查器（对齐 StarCli：端口、Java、目录、用户/组、发行版探测等）。"""

    @staticmethod
    def check_port_available(port: int, host: str = '0.0.0.0') -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                result = s.connect_ex((host, port))
                return result != 0
        except Exception as e:
            logger.error("检查端口可用性失败: {}".format(e), extra={"to_stdout": True})
            return False

    @staticmethod
    def _find_java_home_from_path(java_path: Path) -> Optional[str]:
        try:
            resolved = java_path.resolve()
        except Exception as ex:
            logger.debug("resolve java_path 失败，使用原路径: %s", ex)
            resolved = java_path
        for parent in resolved.parents:
            if (parent / 'lib').exists() or (parent / 'jre' / 'lib').exists():
                if (parent / 'bin' / 'java').exists() or (parent / 'java').exists():
                    return str(parent)
        return None

    @staticmethod
    def _get_os_info() -> Tuple[str, Optional[str]]:
        """获取发行版信息（与 StarCli 一致；无 distro 时返回空串）。"""
        try:
            import distro
            return distro.id(), distro.like()
        except ImportError:
            return "", None

    @staticmethod
    def _find_java_home_by_distro() -> Optional[str]:
        """
        按 Linux 发行版常见路径与 alternatives 解析 JAVA_HOME（对齐 StarCli，便于无 JAVA_HOME 的服务器）。
        未安装 distro 库时仍尝试通用路径列表。
        """
        os_id, os_like = EnvironmentChecker._get_os_info()

        # Debian/Ubuntu
        if os_id in ("debian", "ubuntu") or (os_like and "debian" in os_like):
            try:
                result = run_command(
                    ["update-alternatives", "--list", "java"],
                    capture_output=True,
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout:
                    java_path = Path(result.stdout.strip().split("\n")[0])
                    jh = EnvironmentChecker._find_java_home_from_path(java_path)
                    if jh:
                        return jh
            except Exception as ex:
                logger.debug("update-alternatives 查找 Java 失败: %s", ex)
            for path in (
                "/usr/lib/jvm/default-java",
                "/usr/lib/jvm/java-17-openjdk-amd64",
                "/usr/lib/jvm/java-17-openjdk",
                "/usr/lib/jvm/java-11-openjdk-amd64",
                "/usr/lib/jvm/java-11-openjdk",
            ):
                p = Path(path)
                if p.exists() and (p / "bin" / "java").exists():
                    return str(p)

        # RHEL/Rocky/Fedora 等
        if os_id in ("rhel", "centos", "rocky", "fedora", "almalinux") or (
            os_like and "rhel" in os_like
        ):
            try:
                result = run_command(
                    ["alternatives", "--display", "java"],
                    capture_output=True,
                    check=False,
                    allowed_exit_codes=[0, 1],
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.split("\n"):
                        if "currently points to" in line or "link currently points to" in line:
                            java_path_str = line.split()[-1]
                            java_path = Path(java_path_str)
                            jh = EnvironmentChecker._find_java_home_from_path(java_path)
                            if jh:
                                return jh
            except Exception as ex:
                logger.debug("alternatives 查找 Java 失败: %s", ex)
            for path in (
                "/usr/lib/jvm/java-17-openjdk",
                "/usr/lib/jvm/java-17",
                "/usr/lib/jvm/java-11-openjdk",
                "/usr/lib/jvm/java-1.17.0-openjdk",
                "/usr/lib/jvm/java-1.11.0-openjdk",
            ):
                p = Path(path)
                if p.exists() and (p / "bin" / "java").exists():
                    return str(p)

        for path in (
            "/usr/lib/jvm/default-java",
            "/usr/lib/jvm/default",
            "/usr/lib/jvm/java-17-openjdk",
            "/usr/lib/jvm/java-17",
            "/usr/lib/jvm/java-11-openjdk",
        ):
            p = Path(path)
            if p.exists() and (p / "bin" / "java").exists():
                return str(p)
        return None

    @staticmethod
    def check_user_group_exists(user: str, group: str) -> Tuple[bool, bool]:
        """
        检查系统用户、组是否存在（与 StarCli 一致：id / getent，便于自动化与远程环境）。
        非 POSIX 跳过，视为存在。
        """
        if os.name != "posix":
            return True, True
        user_exists = False
        group_exists = False
        try:
            result = run_command(
                ["id", user],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=5,
            )
            user_exists = result.returncode == 0
        except Exception as ex:
            logger.error("检查用户是否存在失败: {}".format(ex), extra={"to_stdout": True})
        try:
            result = run_command(
                ["getent", "group", group],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=5,
            )
            group_exists = result.returncode == 0
        except Exception as ex:
            logger.error("检查组是否存在失败: {}".format(ex), extra={"to_stdout": True})
        return user_exists, group_exists

    @staticmethod
    def check_java(minimum_java_major: int = 17) -> Tuple[bool, Optional[str], Optional[str]]:
        """检查 Java 环境；minimum_java_major 由 min_java_major_for_kafka() 与 Kafka 版本推断结果决定。"""
        try:
            result = run_command(
                ['java', '-version'],
                capture_output=True,
                check=False,
                allowed_exit_codes=[0, 1],
                timeout=10
            )
            if result.returncode != 0 and not result.stderr:
                return False, None, None

            version_output = (result.stderr or result.stdout or "").strip()
            java_version = None
            version_match = re.search(r'version\s+"?(\d+)\.?(\d+)?', version_output)
            if version_match:
                major = int(version_match.group(1))
                minor = int(version_match.group(2)) if version_match.group(2) else 0
                if major == 1 and version_match.group(2):
                    major = int(version_match.group(2))
                java_version = f"{major}.{minor}" if minor > 0 else str(major)
                if major < minimum_java_major:
                    logger.error(
                        "Java 版本过低: {}，当前需要 Java {}+（与检测到的 Kafka 版本策略一致，参见官方文档）".format(
                            java_version, minimum_java_major
                        ),
                        extra={"to_stdout": True}
                    )
                    return False, None, java_version
            elif version_output:
                logger.error(
                    "无法解析 Java 版本输出，需要 Java {}+（与 Kafka 版本策略一致）".format(minimum_java_major),
                    extra={"to_stdout": True}
                )
                return False, None, None

            java_home = os.environ.get('JAVA_HOME')
            if not java_home:
                java_bin = shutil.which('java')
                if java_bin:
                    java_home = EnvironmentChecker._find_java_home_from_path(Path(java_bin))
            if not java_home:
                java_home = EnvironmentChecker._find_java_home_by_distro()
                if java_home:
                    logger.debug("通过发行版探测得到 JAVA_HOME: %s", java_home)

            if java_home:
                java_home_path = Path(java_home)
                if not (java_home_path / 'bin' / 'java').exists():
                    if (java_home_path.parent / 'bin' / 'java').exists():
                        java_home = str(java_home_path.parent)

            return True, java_home, java_version
        except Exception as e:
            logger.error("检查 Java 环境失败: {}".format(e), extra={"to_stdout": True})
            return False, None, None

    @staticmethod
    def check_directory_writable(path: str) -> bool:
        try:
            path_obj = Path(path)
            if not path_obj.exists():
                path_obj.mkdir(parents=True, exist_ok=True)
            test_file = path_obj / '.kafka_write_test'
            test_file.write_text('test')
            test_file.unlink()
            return True
        except Exception as e:
            logger.error("检查目录可写性失败: {}".format(e), extra={"to_stdout": True})
            return False

    @staticmethod
    def warn_if_path_under_kafka_home(kafka_home: Path, path_list_csv: str, label: str) -> None:
        """
        若数据路径落在 Kafka 安装目录下则告警（本脚本/StarCli 项目约定，便于升级与清理；Kafka 文档未规定 log.dirs 必须独立于安装路径）。
        """
        first = (path_list_csv or "").split(",")[0].strip()
        if not first:
            return
        try:
            base = kafka_home.resolve()
            target = Path(first).resolve()
            target.relative_to(base)
            logger.warning(
                "%s 位于 Kafka 安装目录内 (%s)；独立数据盘/路径为常见运维实践（非 Kafka 文档硬性要求）。",
                label,
                target,
                extra={"to_stdout": True},
            )
        except ValueError:
            pass
        except Exception as ex:
            logger.debug("目录独立性检查跳过: %s", ex)


class ConfigGenerator:
    """
    Kafka 配置文件生成器（与官方 server.properties / KRaft 配置一致，适用于 3.3+～4.x）。

    - generate_combined_standalone_properties：单节点 broker+controller 完整默认（含 listener 套件），
      与 KafkaDeployer.deploy_standalone 写入内容一致。
    - generate_server_properties：通用键值对生成器；若 process.roles 含 combined 场景，
      须自行合并 _kraft_combined_listener_properties() 或改用 generate_combined_standalone_properties。
    - generate_controller_properties：独立 Controller 节点（已含 controller.listener.names 与 protocol map）。

    extra_properties 会合并并覆盖同名字段；使用 SSL/SASL 时须在 extra_properties 中提供完整 listener.security.protocol.map。
    参考: https://kafka.apache.org/documentation/#brokerconfigs, #kraft
    """

    @staticmethod
    def generate_combined_standalone_properties(
            node_id: int,
            log_dirs: str,
            listeners_override: Optional[str] = None,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """KRaft 单节点 combined（broker,controller）server.properties 键值对（3.3+ / 4.x）。"""
        props: Dict[str, str] = {
            "process.roles": "broker,controller",
            "node.id": str(node_id),
            "log.dirs": log_dirs,
            **_kraft_combined_listener_properties(listeners_override),
        }
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_server_properties(
            process_roles: str,
            node_id: int,
            log_dirs: str,
            listeners: str,
            controller_quorum_bootstrap_servers: Optional[str] = None,
            metadata_log_dir: Optional[str] = None,
            num_partitions: int = 1,
            default_replication_factor: Optional[int] = None,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """生成 Broker/Standalone server.properties 键值对（官方必选 + 常用）"""
        props = {
            "process.roles": process_roles,
            "node.id": str(node_id),
            "log.dirs": log_dirs,
            "listeners": listeners,
            "num.partitions": str(num_partitions),
        }
        if default_replication_factor is not None:
            props["default.replication.factor"] = str(default_replication_factor)
        if controller_quorum_bootstrap_servers:
            props["controller.quorum.bootstrap.servers"] = controller_quorum_bootstrap_servers
        if metadata_log_dir:
            props["metadata.log.dir"] = metadata_log_dir
        if extra_properties:
            props.update(extra_properties)
        return props

    @staticmethod
    def generate_controller_properties(
            node_id: int,
            controller_listener_port: int,
            controller_quorum_bootstrap_servers: str,
            metadata_log_dir: str,
            log_dirs: Optional[str] = None,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> Dict[str, str]:
        """生成 Controller 专用 properties（官方 KRaft 文档）"""
        props = {
            "process.roles": "controller",
            "node.id": str(node_id),
            "listeners": f"CONTROLLER://0.0.0.0:{controller_listener_port}",
            "controller.quorum.bootstrap.servers": controller_quorum_bootstrap_servers,
            "controller.listener.names": "CONTROLLER",
            "listener.security.protocol.map": "CONTROLLER:PLAINTEXT",
            "metadata.log.dir": metadata_log_dir,
        }
        if log_dirs:
            props["log.dirs"] = log_dirs
        if extra_properties:
            props.update(extra_properties)
        return props


class SystemdServiceGenerator:
    """
    systemd 单元生成；Documentation= 指向 Apache Kafka 官方文档。
    其余字段（WorkingDirectory、LimitNOFILE 等）为本脚本与同仓库 StarCli 的工程约定。
    """

    @staticmethod
    def generate_kafka_service(
            bin_dir: Path,
            config_path: Path,
            deploy_type: str,
            kafka_home: Path,
            user: str = "kafka",
            group: str = "kafka",
            java_home: Optional[str] = None,
    ) -> str:
        """bin_dir、config_path、kafka_home 须为绝对路径；ExecStart 使用官方 kafka-server-start.sh。"""
        kh = str(kafka_home.resolve())
        env_lines: List[str] = [f'Environment="KAFKA_HOME={kh}"']
        if java_home:
            env_lines.append(f'Environment="JAVA_HOME={java_home}"')
        env_block = "\n".join(env_lines)
        safe_ident = re.sub(r"[^a-z0-9-]+", "-", deploy_type.lower()).strip("-") or "kafka"
        return f"""[Unit]
Description=Apache Kafka - {deploy_type}
Documentation=https://kafka.apache.org/documentation/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={kh}
UMask=0022
{env_block}
ExecStart={bin_dir / "kafka-server-start.sh"} {config_path}
Restart=on-failure
RestartSec=10
TimeoutStartSec=600
TimeoutStopSec=120
SuccessExitStatus=0 143
KillMode=mixed
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal
SyslogIdentifier=kafka-{safe_ident}

LimitNOFILE=1048576
LimitNPROC=65536
LimitCORE=infinity
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
"""


class KafkaDeployer:
    """
    Kafka 部署器（KRaft 模式，支持 Apache Kafka 3.3+～4.x）。

    配置键与流程须对照官方文档 #kraft、#brokerconfigs 及 kafka-storage.sh / kafka-server-start.sh 的说明。
    standalone 使用 ConfigGenerator.generate_combined_standalone_properties 与 _kraft_combined_listener_properties。
    低于 3.3 默认拒绝 KRaft 自动部署（依据 KIP-833 适用范围；可 --skip-kraft-version-check 自担风险）。
    systemd unit 由 SystemdServiceGenerator 生成；环境与账户检查与同仓库 StarCli 一致。
    """

    SERVICE_NAME_STANDALONE = "kafka-standalone"
    SERVICE_NAME_CONTROLLER = "kafka-controller"
    SERVICE_NAME_BROKER = "kafka-broker"

    def __init__(
            self,
            kafka_home: str,
            user: str = "kafka",
            group: str = "kafka",
            assume_kafka_version: Optional[str] = None,
            skip_kraft_version_check: bool = False,
    ):
        self.kafka_home = Path(kafka_home).resolve()
        self.user = user
        self.group = group
        self.skip_kraft_version_check = skip_kraft_version_check
        if not self.kafka_home.exists():
            raise ValueError(f"Kafka 安装目录不存在: {kafka_home}")
        self.bin_dir = self.kafka_home / "bin"
        self.config_dir = self.kafka_home / "config"
        if not (self.bin_dir.exists() and (self.bin_dir / "kafka-storage.sh").exists()):
            raise ValueError(f"无效的 Kafka 安装目录（缺少 bin/kafka-storage.sh）: {kafka_home}")
        self.kafka_release = infer_kafka_release(self.kafka_home, assume_kafka_version)
        if self.kafka_release:
            logger.info(
                "检测到 Apache Kafka 版本: %s",
                ".".join(map(str, self.kafka_release)),
                extra={"to_stdout": True},
            )
        else:
            logger.warning(
                "未能解析 Kafka 版本（路径或 libs/kafka-server-common-*.jar）；将按 Kafka 4.x 策略要求 Java 17+。"
                "可设置 KAFKA_CLI_ASSUME_VERSION 或 --assume-kafka-version x.y.z。",
                extra={"to_stdout": True},
            )

    def check_environment(self) -> Tuple[bool, List[str]]:
        """检查部署环境：Java、系统用户/组（与 StarCli 一致）、KRaft 版本下限、安装目录可写等。"""
        errors = []
        min_java = min_java_major_for_kafka(self.kafka_release)
        java_ok, java_home, java_version = EnvironmentChecker.check_java(minimum_java_major=min_java)
        if not java_ok:
            if java_version:
                errors.append(f"Java 版本过低: {java_version}，需要 Java {min_java}+")
            else:
                errors.append(f"未找到 Java 环境，请先安装 JDK {min_java}+")
        elif java_version:
            logger.info(f"检测到 Java 版本: {java_version}", extra={"to_stdout": True})

        if os.name == "posix":
            user_ok, group_ok = EnvironmentChecker.check_user_group_exists(self.user, self.group)
            if not user_ok:
                errors.append(
                    f"系统用户不存在: {self.user}（systemd 将使用 User={self.user}，不存在则 217/USER）。"
                    f"请先创建用户或改用 --user 指定已存在账户。"
                )
            if not group_ok:
                errors.append(
                    f"系统组不存在: {self.group}（systemd 将使用 Group={self.group}）。"
                    f"请先执行 groupadd 或改用 --group。"
                )

        if self.kafka_release is not None and self.kafka_release < KRAFT_DEPLOY_MIN_VERSION:
            ver_s = ".".join(map(str, self.kafka_release))
            if self.skip_kraft_version_check:
                logger.warning(
                    "已跳过 KRaft 版本检查：检测到 Kafka %s（官方建议 KRaft 新集群自 3.3+ 起），请自担风险",
                    ver_s,
                    extra={"to_stdout": True},
                )
            else:
                errors.append(
                    f"检测到 Kafka {ver_s}：本脚本 KRaft 自动部署针对 {'.'.join(map(str, KRAFT_DEPLOY_MIN_VERSION))}+ "
                    f"（KIP-833）。请升级 Kafka 或使用 --skip-kraft-version-check / 配置 skip_kraft_version_check 自担风险。"
                )

        if not EnvironmentChecker.check_directory_writable(str(self.kafka_home)):
            errors.append(f"Kafka 目录不可写: {self.kafka_home}")

        if not (self.bin_dir / "kafka-server-start.sh").exists():
            errors.append(f"缺少 bin/kafka-server-start.sh，安装可能不完整: {self.bin_dir}")

        return len(errors) == 0, errors

    def _run_storage_cmd(self, args: List[str], env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
        """执行 bin/kafka-storage.sh"""
        cmd = [str(self.bin_dir / "kafka-storage.sh")] + args
        return run_command(cmd, capture_output=True, timeout=60, env=env or os.environ.copy())

    def _run_server_start(self, config_path: Path, java_home: Optional[str] = None) -> subprocess.CompletedProcess:
        """执行 bin/kafka-server-start.sh（前台；启用 systemd 时由 ExecStart 调用，未启用时可手动调用此方法做一次性启动）"""
        cmd = [str(self.bin_dir / "kafka-server-start.sh"), str(config_path)]
        env = os.environ.copy()
        if java_home:
            env["JAVA_HOME"] = java_home
        return run_command(cmd, capture_output=True, timeout=5, env=env)

    def _write_properties(self, path: Path, props: Dict[str, str]) -> bool:
        """写入 Java properties（原子写入，见 _atomic_write_text）。"""
        try:
            lines = []
            for k, v in props.items():
                if v is None or v == "":
                    continue
                v_str = str(v).replace("\\", "\\\\").replace("\n", "\\n")
                lines.append(f"{k}={v_str}")
            _atomic_write_text(path, "\n".join(lines) + "\n")
            logger.info(f"✓ 已写入配置: {path}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error(f"写入配置失败: {e}", extra={"to_stdout": True})
            return False

    def _systemd_unit_content(
            self,
            deploy_type: str,
            config_path: Path,
            java_home: Optional[str] = None,
            user: Optional[str] = None,
            group: Optional[str] = None
    ) -> str:
        """生成 systemd unit 文件内容（委托 SystemdServiceGenerator）。"""
        return SystemdServiceGenerator.generate_kafka_service(
            self.bin_dir,
            config_path,
            deploy_type,
            self.kafka_home,
            user=user or self.user,
            group=group or self.group,
            java_home=java_home,
        )

    def _generate_cluster_id(self) -> Optional[str]:
        """生成 KRaft cluster ID（random-uuid），失败返回 None。"""
        try:
            result = self._run_storage_cmd(["random-uuid"])
            cluster_id = (result.stdout or "").strip()
            if cluster_id:
                logger.info(f"生成 Cluster ID: {cluster_id}", extra={"to_stdout": True})
            return cluster_id or None
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh random-uuid 失败: {e}", extra={"to_stdout": True})
            return None

    def _journal_tail_for_unit(self, service_name: str, lines: int = 40) -> str:
        """读取最近 journal，便于排查 217/USER、启动失败等。"""
        try:
            r = run_command(
                ["journalctl", "-u", service_name, "-n", str(lines), "--no-pager"],
                capture_output=True,
                check=False,
                timeout=20,
            )
            return (r.stdout or r.stderr or "").strip()
        except Exception as ex:
            return f"(journalctl 失败: {ex})"

    def _log_friendly_systemd_failure(self, journal_tail: str) -> None:
        """根据 journal 内容给出可读原因，优先于原始日志展示。"""
        t = journal_tail or ""
        if re.search(r"217\s*/\s*USER|status\s*=\s*217", t, re.I) or (
            "217" in t and "USER" in t.upper()
        ):
            u, g = self.user, self.group
            logger.error(
                "【判定】systemd 无法切换到 unit 中配置的系统用户（退出码 217/USER），进程未真正运行。\n"
                "【当前配置】User=%s, Group=%s（若用户/组不存在即会如此）。\n"
                "【处理】创建专用账户，例如：sudo groupadd -r %s 2>/dev/null; "
                "sudo useradd -r -g %s -s /sbin/nologin %s\n"
                "【或】仅本机测试可加：--user $(id -un) --group $(id -gn)",
                u, g, g, g, u,
                extra={"to_stdout": True},
            )
            return
        if "permission denied" in t.lower():
            logger.error(
                "【判定】journal 中出现 Permission denied：多为数据目录 log.dirs/metadata.log.dir 属主与 User= 不一致，"
                "请 chown 或调整目录。",
                extra={"to_stdout": True},
            )

    @staticmethod
    def _tcp_connect_ok(host: str, port: int, timeout: float = 2.0) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((host, port))
            return True
        except OSError:
            return False

    def _wait_for_tcp_listening(
            self, host: str, port: int, label: str, total_sec: float = 50.0
    ) -> bool:
        """部署后轮询 TCP 直至可连（JVM 启动可能较慢）；失败则 ERROR 一次。"""
        deadline = time.time() + total_sec
        while time.time() < deadline:
            if self._tcp_connect_ok(host, port):
                logger.info("✓ %s 已在 %s:%s 监听（TCP 探测成功）", label, host, port, extra={"to_stdout": True})
                return True
            time.sleep(1.2)
        logger.error(
            "【判定】在约 %.0f 秒内 %s 未在 %s:%s 接受连接，部署不视为成功。"
            "若 systemd 为 active，请查 server 日志与 listeners 端口。",
            total_sec,
            label,
            host,
            port,
            extra={"to_stdout": True},
        )
        return False

    def _verify_systemd_service_active(self, service_name: str, wait_sec: int = 45) -> Tuple[bool, str]:
        """
        Type=simple 下 systemctl start 可能在主进程立刻退出时仍返回 0，必须轮询 is-active；
        若 is-failed 则提前结束并拉 journal。
        """
        deadline = time.time() + wait_sec
        while time.time() < deadline:
            if self._is_service_active(service_name):
                return True, ""
            try:
                rf = run_command(
                    ["systemctl", "is-failed", service_name],
                    check=False,
                    capture_output=True,
                    allowed_exit_codes=[0, 1],
                    timeout=8,
                )
                if rf.returncode == 0 and (rf.stdout or "").strip() == "failed":
                    return False, self._journal_tail_for_unit(service_name)
            except Exception as ex:
                logger.debug("systemctl is-failed: %s", ex)
            time.sleep(1)
        return False, self._journal_tail_for_unit(service_name)

    def _enable_systemd_service(
            self,
            service_name: str,
            config_path: Path,
            deploy_type: str,
            java_home: Optional[str] = None
    ) -> bool:
        """安装并启动 systemd 服务（写 unit、daemon-reload、enable、start），并确认进入 active。"""
        unit_path = Path(f"/etc/systemd/system/{service_name}.service")
        unit_content = self._systemd_unit_content(deploy_type, config_path, java_home)
        try:
            _atomic_write_text(unit_path, unit_content)
            run_command(["systemctl", "daemon-reload"], timeout=30)
            run_command(["systemctl", "enable", service_name], timeout=30)
            run_command(["systemctl", "reset-failed", service_name], check=False, timeout=10)
            run_command(["systemctl", "start", service_name], timeout=60)
        except Exception as e:
            logger.error(f"systemd 启动阶段异常: {e}", extra={"to_stdout": True})
            tail = self._journal_tail_for_unit(service_name)
            self._log_friendly_systemd_failure(tail)
            if tail:
                logger.error("journal 摘录:\n%s", tail, extra={"to_stdout": True})
            return False

        ok, jtail = self._verify_systemd_service_active(service_name)
        if not ok:
            self._log_friendly_systemd_failure(jtail)
            logger.error(
                "【详情】systemd 未处于 active 或已进入 failed，以下为 journal 摘录（便于核对）：\n%s",
                jtail or "(无)",
                extra={"to_stdout": True},
            )
            return False
        logger.info(f"✓ systemd 服务已处于 active: {service_name}", extra={"to_stdout": True})
        return True

    def _verify_port_reachable(self, host: str, port: int, label: str, timeout: int = 5) -> bool:
        """检查 host:port 是否 TCP 可达。"""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(timeout)
                s.connect((host, port))
            logger.info(f"✓ {label} 可连接: {host}:{port}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.warning(f"{label} 连接检查失败: {e}", extra={"to_stdout": True})
            return False

    def _check_service_exists(self, service_name: str) -> bool:
        """检查 unit 是否已加载（与 StarCli 一致：优先 LoadState，再回退 status / 文件）。"""
        try:
            show_result = run_command(
                ["systemctl", "show", service_name, "-p", "LoadState", "--value"],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 3, 4],
                timeout=10,
            )
            load_state = (show_result.stdout or "").strip().lower()
            if load_state == "not-found":
                return False
            if load_state in ("loaded", "masked", "stub", "generated", "bad"):
                return True
            result = run_command(
                ["systemctl", "status", service_name, "--no-pager"],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 2, 3, 4],
                timeout=10,
            )
            combined = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
            if result.returncode in (2, 4) or "not-found" in combined or "could not be found" in combined:
                return False
            return True
        except Exception as ex:
            logger.debug("检查 systemd 服务是否存在失败: %s", ex)
            return Path(f"/etc/systemd/system/{service_name}.service").exists()

    def _is_service_active(self, service_name: str) -> bool:
        try:
            result = run_command(
                ['systemctl', 'is-active', service_name],
                check=False,
                capture_output=True,
                allowed_exit_codes=[0, 1, 3],
                timeout=10
            )
            return result.returncode == 0 and (result.stdout or "").strip() == 'active'
        except Exception as ex:
            logger.debug("systemctl is-active 失败: %s", ex)
            return False

    def _stop_and_disable_service(self, service_name: str, force: bool = True) -> bool:
        """停止并禁用 systemd 服务"""
        try:
            if not self._check_service_exists(service_name):
                return True
            if self._is_service_active(service_name):
                logger.info(f"正在停止服务: {service_name}", extra={"to_stdout": True})
                run_command(
                    ['systemctl', 'stop', service_name],
                    check=False,
                    allowed_exit_codes=[0, 1, 5],
                    timeout=60
                )
                for _ in range(10):
                    if not self._is_service_active(service_name):
                        break
                    time.sleep(1)
                if force and self._is_service_active(service_name):
                    self._kill_service_processes(service_name)
                    time.sleep(2)
            run_command(['systemctl', 'disable', service_name], check=False, timeout=30)
            run_command(['systemctl', 'reset-failed', service_name], check=False, timeout=10)
            logger.info(f"✓ 已停止并禁用: {service_name}", extra={"to_stdout": True})
            return True
        except Exception as e:
            logger.error(f"停止服务失败: {e}", extra={"to_stdout": True})
            return False

    def _kill_service_processes(self, service_name: str) -> bool:
        """强制结束与服务相关的进程"""
        try:
            result = run_command(
                ['systemctl', 'show', service_name, '--property=MainPID', '--value'],
                check=False,
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0 and result.stdout and result.stdout.strip().isdigit():
                pid = int(result.stdout.strip())
                if pid > 1:
                    run_command(['kill', '-TERM', str(pid)], check=False, timeout=5)
                    time.sleep(2)
                    run_command(['kill', '-0', str(pid)], check=False, timeout=5)
                    if run_command(['kill', '-0', str(pid)], check=False, timeout=5).returncode == 0:
                        run_command(['kill', '-KILL', str(pid)], check=False, timeout=5)
            # 禁止使用过于宽泛的匹配（如单独 "kafka"）：会命中 kafkacli 命令行里的 --kafka-home /path/kafka，误杀本进程。
            for pattern in ("kafka-server-start.sh", "kafka.Kafka"):
                try:
                    r = run_command(["pgrep", "-f", pattern], check=False, capture_output=True, timeout=10)
                    if r.returncode == 0 and r.stdout:
                        for pid_str in r.stdout.strip().split("\n"):
                            if pid_str.strip().isdigit() and int(pid_str.strip()) > 1:
                                run_command(["kill", "-KILL", pid_str.strip()], check=False, timeout=5)
                except Exception as ex:
                    logger.debug("pgrep/kill 处理失败: %s", ex)
            return True
        except Exception as e:
            logger.error(f"强制结束进程失败: {e}", extra={"to_stdout": True})
            return False

    def deploy_standalone(
            self,
            log_dirs: str,
            cluster_id: Optional[str] = None,
            node_id: int = 1,
            listeners: Optional[str] = None,
            java_home: Optional[str] = None,
            enable_systemd: bool = True,
            force: bool = False,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> bool:
        """
        部署单节点 KRaft（combined broker+controller），对齐 Kafka 4.x KRaft combined 必选项：
        listeners（PLAINTEXT+CONTROLLER）、controller.listener.names、inter.broker.listener.name、
        listener.security.protocol.map、advertised.listeners、controller.quorum.bootstrap.servers。
        生产环境请设置环境变量 KAFKA_ADVERTISED_HOST，或通过 extra_properties 覆盖 advertised.listeners /
        controller.quorum.bootstrap.servers。流程：生成 cluster id → format --standalone → 启动 server。
        """
        logger.info("=== 部署 Kafka Standalone（KRaft 单节点）===", extra={"to_stdout": True})

        ok, errs = self.check_environment()
        if not ok:
            for e in errs:
                logger.error(e, extra={"to_stdout": True})
            return False

        log_dirs_path = Path(log_dirs.split(",")[0].strip())
        if not EnvironmentChecker.check_directory_writable(str(log_dirs_path)):
            logger.error(f"日志目录不可写: {log_dirs}", extra={"to_stdout": True})
            return False
        EnvironmentChecker.warn_if_path_under_kafka_home(self.kafka_home, log_dirs, "log.dirs")

        config_path = self.config_dir / "server-standalone.properties"
        if config_path.exists() and not force:
            logger.error(
                f"配置已存在: {config_path}，使用 --force 覆盖或先执行 --clean",
                extra={"to_stdout": True}
            )
            return False

        # 1) 生成或使用已有 cluster id
        if not cluster_id:
            cluster_id = self._generate_cluster_id()
            if not cluster_id:
                logger.error("无法生成 KAFKA_CLUSTER_ID", extra={"to_stdout": True})
                return False

        # 2) 构建 server.properties（与 ConfigGenerator.generate_combined_standalone_properties 一致）
        props = ConfigGenerator.generate_combined_standalone_properties(
            node_id, log_dirs, listeners, extra_properties=extra_properties
        )
        if not self._write_properties(config_path, props):
            return False

        # 3) Format storage（官方：format --standalone -t <CLUSTER_ID> -c <config>）
        try:
            self._run_storage_cmd([
                "format",
                "--standalone",
                "-t", cluster_id,
                "-c", str(config_path)
            ])
            logger.info("✓ Kafka 存储已格式化（standalone）", extra={"to_stdout": True})
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh format 失败: {e}", extra={"to_stdout": True})
            return False

        # 4) systemd 或前台启动（启用 systemd 时必须 active + 数据端口可连，否则返回失败、不报成功）
        if enable_systemd:
            if not self._enable_systemd_service(
                    self.SERVICE_NAME_STANDALONE, config_path, "standalone", java_home
            ):
                return False
            ls = props.get("listeners", "")
            bport = _listener_scheme_port(ls, "PLAINTEXT", DEFAULT_BROKER_PORT)
            if not self._wait_for_tcp_listening("127.0.0.1", bport, "Broker（PLAINTEXT）"):
                return False
        else:
            logger.info("未启用 systemd，请手动执行:", extra={"to_stdout": True})
            logger.info(f"  {self.bin_dir / 'kafka-server-start.sh'} {config_path}", extra={"to_stdout": True})

        return True

    def deploy_controller(
            self,
            node_id: int,
            controller_quorum_bootstrap_servers: str,
            controller_listener_port: int = DEFAULT_CONTROLLER_PORT,
            metadata_log_dir: Optional[str] = None,
            log_dirs: Optional[str] = None,
            cluster_id: Optional[str] = None,
            initial_controllers: Optional[str] = None,
            java_home: Optional[str] = None,
            enable_systemd: bool = True,
            force: bool = False,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> bool:
        """
        部署 KRaft Controller 节点（Kafka 4.x）。配置与 ConfigGenerator.generate_controller_properties 一致，
        含 controller.listener.names、listener.security.protocol.map。
        - 首个 controller：format --standalone 或 --initial-controllers
        - 后续 controller：format --no-initial-controllers
        """
        logger.info("=== 部署 Kafka Controller（KRaft）===", extra={"to_stdout": True})

        ok, errs = self.check_environment()
        if not ok:
            for e in errs:
                logger.error(e, extra={"to_stdout": True})
            return False

        if not controller_quorum_bootstrap_servers:
            logger.error("必须指定 controller.quorum.bootstrap.servers", extra={"to_stdout": True})
            return False

        metadata_dir = metadata_log_dir or DEFAULT_METADATA_LOG_DIR
        if not EnvironmentChecker.check_directory_writable(metadata_dir):
            logger.error(f"元数据日志目录不可写: {metadata_dir}", extra={"to_stdout": True})
            return False
        EnvironmentChecker.warn_if_path_under_kafka_home(self.kafka_home, metadata_dir, "metadata.log.dir")

        config_path = self.config_dir / f"controller-{node_id}.properties"
        if config_path.exists() and not force:
            logger.error(f"配置已存在: {config_path}，使用 --force 覆盖", extra={"to_stdout": True})
            return False

        # Controller 配置（与 ConfigGenerator.generate_controller_properties 一致，含 Kafka 4.x protocol map）
        props = ConfigGenerator.generate_controller_properties(
            node_id=node_id,
            controller_listener_port=controller_listener_port,
            controller_quorum_bootstrap_servers=controller_quorum_bootstrap_servers,
            metadata_log_dir=metadata_dir,
            log_dirs=log_dirs,
            extra_properties=extra_properties,
        )
        if not self._write_properties(config_path, props):
            return False

        is_first_controller = not cluster_id
        if not cluster_id:
            cluster_id = self._generate_cluster_id()
            if not cluster_id:
                logger.error("无法生成 KAFKA_CLUSTER_ID", extra={"to_stdout": True})
                return False
            logger.info("请保存上述 Cluster ID 用于后续 Controller/Broker", extra={"to_stdout": True})

        format_args = ["format", "--cluster-id", cluster_id, "-c", str(config_path)]
        if initial_controllers:
            format_args.extend(["--initial-controllers", initial_controllers])
        elif is_first_controller:
            format_args.append("--standalone")
        else:
            format_args.append("--no-initial-controllers")

        try:
            self._run_storage_cmd(format_args)
            logger.info("✓ Controller 存储已格式化", extra={"to_stdout": True})
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh format 失败: {e}", extra={"to_stdout": True})
            return False

        if enable_systemd:
            if not self._enable_systemd_service(
                    f"{self.SERVICE_NAME_CONTROLLER}-{node_id}", config_path, "controller", java_home
            ):
                return False
            ls = props.get("listeners", "")
            cport = _listener_scheme_port(ls, "CONTROLLER", controller_listener_port)
            if not self._wait_for_tcp_listening("127.0.0.1", cport, "Controller（CONTROLLER）"):
                return False

        return True

    def deploy_broker(
            self,
            node_id: int,
            controller_quorum_bootstrap_servers: str,
            log_dirs: str,
            listeners: Optional[str] = None,
            cluster_id: Optional[str] = None,
            java_home: Optional[str] = None,
            enable_systemd: bool = True,
            force: bool = False,
            extra_properties: Optional[Dict[str, str]] = None
    ) -> bool:
        """
        部署 KRaft Broker 节点（Kafka 4.x）。format --no-initial-controllers，process.roles=broker。
        默认 PLAINTEXT 监听器时自动补全 inter.broker.listener.name、listener.security.protocol.map、
        advertised.listeners（主机见 KAFKA_ADVERTISED_HOST）；SSL/SASL 请用 extra_properties 完整配置。
        """
        logger.info("=== 部署 Kafka Broker（KRaft）===", extra={"to_stdout": True})

        ok, errs = self.check_environment()
        if not ok:
            for e in errs:
                logger.error(e, extra={"to_stdout": True})
            return False

        if not controller_quorum_bootstrap_servers:
            logger.error("必须指定 controller.quorum.bootstrap.servers", extra={"to_stdout": True})
            return False

        if not cluster_id:
            logger.error("Broker 部署必须指定 --cluster-id（与 Controller 集群一致）", extra={"to_stdout": True})
            return False

        log_dirs_path = Path(log_dirs.split(",")[0].strip())
        if not EnvironmentChecker.check_directory_writable(str(log_dirs_path)):
            logger.error(f"日志目录不可写: {log_dirs}", extra={"to_stdout": True})
            return False
        EnvironmentChecker.warn_if_path_under_kafka_home(self.kafka_home, log_dirs, "log.dirs")

        config_path = self.config_dir / f"server-broker-{node_id}.properties"
        if config_path.exists() and not force:
            logger.error(f"配置已存在: {config_path}，使用 --force 覆盖", extra={"to_stdout": True})
            return False

        props = {
            "process.roles": "broker",
            "node.id": str(node_id),
            "controller.quorum.bootstrap.servers": controller_quorum_bootstrap_servers,
            "log.dirs": log_dirs,
            **_kraft_broker_listener_properties(listeners),
        }
        if extra_properties:
            props.update(extra_properties)
        if not self._write_properties(config_path, props):
            return False

        try:
            self._run_storage_cmd([
                "format",
                "--cluster-id", cluster_id,
                "--no-initial-controllers",
                "-c", str(config_path)
            ])
            logger.info("✓ Broker 存储已格式化", extra={"to_stdout": True})
        except CommandExecutionError as e:
            logger.error(f"kafka-storage.sh format 失败: {e}", extra={"to_stdout": True})
            return False

        if enable_systemd:
            if not self._enable_systemd_service(
                    f"{self.SERVICE_NAME_BROKER}-{node_id}", config_path, "broker", java_home
            ):
                return False
            ls = props.get("listeners", "")
            bport = _listener_scheme_port(ls, "PLAINTEXT", DEFAULT_BROKER_PORT)
            if not self._wait_for_tcp_listening("127.0.0.1", bport, "Broker（PLAINTEXT）"):
                return False

        return True

    def verify_broker_started(self, host: str = "localhost", port: int = DEFAULT_BROKER_PORT) -> bool:
        """健康检查：验证 Broker 是否可连接（TCP 端口）"""
        return self._verify_port_reachable(host, port, "Broker")

    def verify_controller_started(
            self, host: str = "localhost", port: int = DEFAULT_CONTROLLER_PORT
    ) -> bool:
        """健康检查：验证 Controller 端口是否监听"""
        return self._verify_port_reachable(host, port, "Controller")

    def clean_deployment(
            self,
            deploy_type: str,
            node_id: Optional[int] = None,
            backup_config: bool = True
    ) -> bool:
        """
        清理本脚本安装的 systemd 服务与生成配置（停止→禁用→进程清理→可选备份→删 unit→daemon-reload）。
        不删除 log.dirs / metadata.log.dir（数据保留策略为本脚本约定）。
        """
        logger.info(f"=== 清理 Kafka {deploy_type} 部署 ===", extra={"to_stdout": True})

        if deploy_type == "standalone":
            service_name = self.SERVICE_NAME_STANDALONE
            config_path = self.config_dir / "server-standalone.properties"
        elif deploy_type == "controller":
            if node_id is None:
                logger.error("清理 controller 需指定 --node-id", extra={"to_stdout": True})
                return False
            service_name = f"{self.SERVICE_NAME_CONTROLLER}-{node_id}"
            config_path = self.config_dir / f"controller-{node_id}.properties"
        elif deploy_type == "broker":
            if node_id is None:
                logger.error("清理 broker 需指定 --node-id", extra={"to_stdout": True})
                return False
            service_name = f"{self.SERVICE_NAME_BROKER}-{node_id}"
            config_path = self.config_dir / f"server-broker-{node_id}.properties"
        else:
            logger.error(f"无效的 deploy 类型: {deploy_type}", extra={"to_stdout": True})
            return False

        service_path = Path(f"/etc/systemd/system/{service_name}.service")

        logger.info("正在停止服务...", extra={"to_stdout": True})
        self._stop_and_disable_service(service_name, force=True)
        self._kill_service_processes(service_name)
        time.sleep(1)

        logger.info("正在删除服务文件...", extra={"to_stdout": True})
        if service_path.exists():
            service_path.unlink()
            logger.info(f"✓ 已删除: {service_path}", extra={"to_stdout": True})
        run_command(['systemctl', 'daemon-reload'], check=False, timeout=30)

        logger.info("正在处理配置文件...", extra={"to_stdout": True})
        if config_path.exists():
            if backup_config:
                backup_path = config_path.with_suffix(config_path.suffix + f".backup.{int(time.time())}")
                shutil.copy2(config_path, backup_path)
                logger.info(f"✓ 配置已备份: {backup_path}", extra={"to_stdout": True})
            config_path.unlink()
            logger.info(f"✓ 已删除配置: {config_path}", extra={"to_stdout": True})

        logger.info("=== 清理完成 ===", extra={"to_stdout": True})
        return True

    def show_cluster_status(
            self,
            bootstrap_server: str = "localhost:9092",
            command_config: Optional[str] = None
    ) -> bool:
        """
        调用 bin/kafka-metadata-quorum.sh（见官方文档 #kraft 与工具说明）。
        须在已安装 Kafka 且网络可达 --bootstrap-server 的环境执行。
        """
        print("=== Kafka 集群状态 (KRaft) ===\n")
        bin_dir = self.bin_dir
        quorum_script = bin_dir / "kafka-metadata-quorum.sh"
        if not quorum_script.exists():
            logger.error(f"未找到 {quorum_script}，请指定正确的 --kafka-home", extra={"to_stdout": True})
            return False

        cmd_common = [str(quorum_script), "--bootstrap-server", bootstrap_server]
        if command_config:
            cmd_common.extend(["--command-config", command_config])

        def _describe_and_print(describe_args: List[str], fallback: str, abort_on_error: bool) -> bool:
            try:
                result = run_command(
                    cmd_common + ["describe"] + describe_args,
                    capture_output=True, check=False, timeout=30
                )
                if result.returncode == 0 and result.stdout:
                    for line in result.stdout.strip().split("\n"):
                        print(line)
                else:
                    print(result.stderr or result.stdout or fallback)
                return True
            except Exception as e:
                if abort_on_error:
                    logger.error(f"查询 quorum 状态失败: {e}", extra={"to_stdout": True})
                    return False
                logger.debug("describe %s 失败: %s", describe_args, e)
                return True

        logger.info("元数据 Quorum 状态:", extra={"to_stdout": True})
        if not _describe_and_print(["--status"], "查询失败", abort_on_error=True):
            return False
        print("\nQuorum 副本:")
        _describe_and_print(["--replication"], "(无或查询失败)", abort_on_error=False)
        print("")
        return True


def _build_bootstrap_cmd(
        script_path: Path,
        bootstrap_server: str,
        args: List[str],
        command_config: Optional[str] = None,
        timeout: Optional[int] = None,
) -> subprocess.CompletedProcess:
    """
    构建并执行带 --bootstrap-server 的 Kafka CLI。
    执行前校验脚本存在、bootstrap 格式、command-config 路径；超时默认取自 KAFKA_CLI_TIMEOUT（见 _kafka_cli_timeout_sec）。
    """
    if not script_path.is_file():
        raise CommandExecutionError(
            f"未找到 Kafka CLI 脚本: {script_path}（请确认 --kafka-home 为完整安装目录）"
        )
    ok, bmsg = _validate_bootstrap_server(bootstrap_server)
    if not ok:
        raise CommandExecutionError(bmsg)
    cc_err = _validate_command_config_path(command_config)
    if cc_err:
        raise CommandExecutionError(cc_err)
    to = timeout if timeout is not None else _kafka_cli_timeout_sec(120)
    cmd = [str(script_path), "--bootstrap-server", bootstrap_server.strip()] + args
    if command_config:
        cmd.extend(["--command-config", command_config])
    return run_command(cmd, capture_output=True, timeout=to)


class KafkaTopicManager:
    """封装 bin/kafka-topics.sh（参数与行为以官方文档 #basic_ops 及工具 --help 为准）。"""

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._script = self.bin_dir / "kafka-topics.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return _build_bootstrap_cmd(
            self._script, self.bootstrap_server, args, self.command_config, timeout=timeout
        )

    def create(self, topic: str, partitions: int = 1, replication_factor: int = 1) -> bool:
        """--create；若 CLI 报错匹配「已存在」常见英文串则视为成功（本脚本策略，非 Kafka 协议）。"""
        ok, msg = _validate_topic_name(topic)
        if not ok:
            logger.error(msg, extra={"to_stdout": True})
            return False
        t = _normalize_topic_name(topic)
        try:
            self._run(
                ["--create", "--topic", t, "--partitions", str(partitions),
                 "--replication-factor", str(replication_factor)],
            )
            logger.info(f"✓ Topic 已创建: {t}", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            if _cli_already_exists(str(e)):
                logger.info("Topic 已存在，跳过创建（幂等）: %s", t, extra={"to_stdout": True})
                return True
            logger.error(f"创建 Topic 失败: {e}", extra={"to_stdout": True})
            return False

    def delete(self, topic: str) -> bool:
        """--delete（需 broker 配置 delete.topic.enable=true，见官方文档 broker 配置）；若报「不存在」常见英文串则视为成功（本脚本策略）。"""
        ok, msg = _validate_topic_name(topic)
        if not ok:
            logger.error(msg, extra={"to_stdout": True})
            return False
        t = _normalize_topic_name(topic)
        try:
            self._run(["--delete", "--topic", t])
            logger.info(f"✓ Topic 已删除: {t}", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            if _cli_topic_missing(str(e)):
                logger.info("Topic 已不存在，跳过删除（幂等）: %s", t, extra={"to_stdout": True})
                return True
            logger.error(f"删除 Topic 失败: {e}", extra={"to_stdout": True})
            return False

    def describe(self, topic: Optional[str] = None) -> Optional[str]:
        """描述 Topic（--describe），不传 topic 则列出所有"""
        try:
            args = ["--describe"]
            if topic:
                ok, msg = _validate_topic_name(topic)
                if not ok:
                    logger.error(msg, extra={"to_stdout": True})
                    return None
                args.extend(["--topic", _normalize_topic_name(topic)])
            r = self._run(args)
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe topic 失败: %s", e, extra={"to_stdout": True})
            return None

    def list(self) -> Optional[str]:
        """列出所有 Topic（--list）"""
        try:
            r = self._run(["--list"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("list topics 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaConsumerGroupManager:
    """Consumer Group 运维（官方 kafka-consumer-groups.sh：lag、状态）"""

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._script = self.bin_dir / "kafka-consumer-groups.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return _build_bootstrap_cmd(
            self._script, self.bootstrap_server, args, self.command_config, timeout=timeout
        )

    def list_groups(self) -> Optional[str]:
        """列出所有 Consumer Group（--list）"""
        try:
            r = self._run(["--list"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("list consumer groups 失败: %s", e, extra={"to_stdout": True})
            return None

    def describe_group(self, group: str) -> Optional[str]:
        """描述 Group（含 lag、--describe --group）"""
        if not (group or "").strip():
            logger.error("consumer group 名称不能为空", extra={"to_stdout": True})
            return None
        try:
            r = self._run(["--describe", "--group", group.strip()])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe consumer group 失败: %s", e, extra={"to_stdout": True})
            return None

    def describe_all(self) -> Optional[str]:
        """描述所有 Group（--all-groups --describe）"""
        try:
            r = self._run(["--all-groups", "--describe"], timeout=_kafka_cli_timeout_sec(300))
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe all groups 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaQuorumManager:
    """
    KRaft 元数据 Quorum 运维（add-controller、remove-controller、describe）。
    需提供 bootstrap_server 或 bootstrap_controller 之一，否则 _quorum_cmd 将缺少连接参数。
    """

    def __init__(self, kafka_home: str, bootstrap_server: Optional[str] = None,
                 bootstrap_controller: Optional[str] = None, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.bootstrap_controller = bootstrap_controller
        self.command_config = command_config

    def _quorum_cmd(self, args: List[str]) -> subprocess.CompletedProcess:
        script = self.bin_dir / "kafka-metadata-quorum.sh"
        if not script.is_file():
            raise CommandExecutionError(f"未找到 Quorum CLI: {script}")
        cc_err = _validate_command_config_path(self.command_config)
        if cc_err:
            raise CommandExecutionError(cc_err)
        bc = (self.bootstrap_controller or "").strip()
        bs = (self.bootstrap_server or "").strip()
        if bc:
            ok, msg = _validate_bootstrap_server(bc)
            if not ok:
                raise CommandExecutionError(f"bootstrap-controller: {msg}")
            conn = ["--bootstrap-controller", bc]
        elif bs:
            ok, msg = _validate_bootstrap_server(bs)
            if not ok:
                raise CommandExecutionError(msg)
            conn = ["--bootstrap-server", bs]
        else:
            raise CommandExecutionError("须指定 bootstrap_server 或 bootstrap_controller（kafka-metadata-quorum.sh）")
        cmd = [str(script)] + args + conn
        if self.command_config:
            cmd.extend(["--command-config", self.command_config])
        to = _kafka_cli_timeout_sec(120)
        return run_command(cmd, capture_output=True, timeout=to)

    def add_controller(self) -> bool:
        """动态添加 Controller（官方 add-controller）"""
        try:
            self._quorum_cmd(["add-controller"])
            logger.info("✓ 已提交 add-controller", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            logger.error(f"add-controller 失败: {e}", extra={"to_stdout": True})
            return False

    def remove_controller(self, controller_id: int, directory_id: str) -> bool:
        """动态移除 Controller（--controller-id --controller-directory-id）"""
        try:
            self._quorum_cmd(["remove-controller", "--controller-id", str(controller_id),
                             "--controller-directory-id", directory_id])
            logger.info("✓ 已提交 remove-controller", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            logger.error(f"remove-controller 失败: {e}", extra={"to_stdout": True})
            return False

    def describe_replication(self) -> Optional[str]:
        """describe --replication"""
        try:
            r = self._quorum_cmd(["describe", "--replication"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe quorum replication 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaConfigManager:
    """Broker/Topic 配置运维（kafka-configs.sh）"""

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._script = self.bin_dir / "kafka-configs.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        return _build_bootstrap_cmd(
            self._script, self.bootstrap_server, args, self.command_config, timeout=timeout
        )

    def describe_broker(self, broker_id: Optional[int] = None) -> Optional[str]:
        """--entity-type brokers [--entity-name id] --describe"""
        args = ["--entity-type", "brokers", "--describe"]
        if broker_id is not None:
            args.extend(["--entity-name", str(broker_id)])
        try:
            r = self._run(args)
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe broker config 失败: %s", e, extra={"to_stdout": True})
            return None

    def describe_topic(self, topic: str) -> Optional[str]:
        """--entity-type topics --entity-name <topic> --describe"""
        ok, msg = _validate_topic_name(topic)
        if not ok:
            logger.error(msg, extra={"to_stdout": True})
            return None
        t = _normalize_topic_name(topic)
        try:
            r = self._run(["--entity-type", "topics", "--entity-name", t, "--describe"])
            return r.stdout
        except CommandExecutionError as e:
            logger.error("describe topic config 失败: %s", e, extra={"to_stdout": True})
            return None


class KafkaMetricsCollector:
    """
    只读采集：调用官方 bin/ 脚本解析 Quorum / Under-replicated / Consumer lag 等相关信息。
    指标语义与 JMX/Monitoring 说明见 https://kafka.apache.org/documentation/#monitoring
    本类输出的 JSON 结构为本脚本约定，非 Kafka 发行版内建格式。
    """

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.kafka_home = Path(kafka_home).resolve()
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self.bin_dir = self.kafka_home / "bin"

    def _metrics_precheck(self, script_leaf: str) -> Optional[str]:
        """脚本存在、bootstrap、command-config；失败返回错误说明。"""
        p = self.bin_dir / script_leaf
        if not p.is_file():
            return f"未找到 CLI: {p}"
        ok, msg = _validate_bootstrap_server(self.bootstrap_server)
        if not ok:
            return msg
        return _validate_command_config_path(self.command_config)

    def collect_quorum_status(self) -> Dict[str, Any]:
        """元数据 Quorum 状态（LeaderId、HighWatermark、CurrentVoters）"""
        out: Dict[str, Any] = {"ok": False, "leader_id": None, "leader_epoch": None, "voters": []}
        try:
            pre = self._metrics_precheck("kafka-metadata-quorum.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = [str(self.bin_dir / "kafka-metadata-quorum.sh"), "--bootstrap-server",
                   self.bootstrap_server.strip(), "describe", "--status"]
            if self.command_config:
                cmd.extend(["--command-config", self.command_config])
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
            if r.returncode != 0 or not r.stdout:
                return out
            for line in r.stdout.splitlines():
                if "LeaderId:" in line:
                    out["leader_id"] = line.split(":", 1)[1].strip()
                elif "LeaderEpoch:" in line:
                    out["leader_epoch"] = line.split(":", 1)[1].strip()
                elif "CurrentVoters:" in line:
                    out["voters"] = line.split(":", 1)[1].strip()
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_topic_partition_health(self) -> Dict[str, Any]:
        """kafka-topics.sh --describe --under-replicated-partitions（见 #basic_ops / #monitoring 中 under-replicated 语义）。"""
        out: Dict[str, Any] = {"ok": False, "under_replicated_partitions": 0, "topics": {}}
        try:
            pre = self._metrics_precheck("kafka-topics.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = [str(self.bin_dir / "kafka-topics.sh"), "--bootstrap-server", self.bootstrap_server.strip(),
                   "--describe", "--under-replicated-partitions"]
            if self.command_config:
                cmd.extend(["--command-config", self.command_config])
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(120))
            if r.returncode != 0:
                out["error"] = r.stderr or r.stdout or "describe failed"
                return out
            lines = (r.stdout or "").strip().splitlines()
            for line in lines:
                if "Topic:" in line and "Partition:" in line:
                    out["under_replicated_partitions"] += 1
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_consumer_lag(self) -> Dict[str, Any]:
        """Consumer Group Lag（--all-groups --describe）解析 LAG 列"""
        out: Dict[str, Any] = {"ok": False, "groups": [], "total_lag": 0}
        try:
            pre = self._metrics_precheck("kafka-consumer-groups.sh")
            if pre:
                out["error"] = pre
                return out
            cmd = [str(self.bin_dir / "kafka-consumer-groups.sh"), "--bootstrap-server",
                   self.bootstrap_server.strip(), "--all-groups", "--describe"]
            if self.command_config:
                cmd.extend(["--command-config", self.command_config])
            r = run_command(cmd, capture_output=True, check=False, timeout=_kafka_cli_timeout_sec(300))
            if r.returncode != 0 or not r.stdout:
                out["error"] = r.stderr or "describe failed"
                return out
            lines = r.stdout.strip().splitlines()
            for line in lines:
                parts = line.split()
                if len(parts) >= 6 and parts[5].isdigit():
                    try:
                        lag = int(parts[5])
                        out["total_lag"] += lag
                        out["groups"].append({"topic": parts[1], "partition": parts[2], "lag": lag})
                    except (IndexError, ValueError):
                        pass
            out["ok"] = True
        except Exception as e:
            out["error"] = str(e)
        return out

    def collect_all(self) -> Dict[str, Any]:
        """汇总：quorum、under_replicated、consumer_lag、bootstrap 连通性"""
        result = {
            "bootstrap_server": self.bootstrap_server,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "quorum": self.collect_quorum_status(),
            "partition_health": self.collect_topic_partition_health(),
            "consumer_lag": self.collect_consumer_lag(),
        }
        return result


class KafkaBrokerDecommission:
    """
    封装 kafka-reassign-partitions.sh 的 --generate / --execute / --verify（见官方文档 #basic_ops
    「Partition reassignment」与「Decommissioning brokers」：下线前须将副本迁出待下线 broker，工具三种模式与文档一致）。
    本类不替代管理员编制完整 reassignment JSON 的责任；关停进程为 clean/运维步骤。
    """

    def __init__(self, kafka_home: str, bootstrap_server: str, command_config: Optional[str] = None):
        self.bin_dir = Path(kafka_home).resolve() / "bin"
        self.bootstrap_server = bootstrap_server
        self.command_config = command_config
        self._reassign_script = self.bin_dir / "kafka-reassign-partitions.sh"

    def _run(self, args: List[str], timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        if not self._reassign_script.is_file():
            raise CommandExecutionError(f"未找到分区重分配脚本: {self._reassign_script}")
        ok, msg = _validate_bootstrap_server(self.bootstrap_server)
        if not ok:
            raise CommandExecutionError(msg)
        cc_err = _validate_command_config_path(self.command_config)
        if cc_err:
            raise CommandExecutionError(cc_err)
        to = timeout if timeout is not None else _kafka_cli_timeout_sec(120)
        cmd = [str(self._reassign_script), "--bootstrap-server", self.bootstrap_server.strip()] + args
        if self.command_config:
            cmd.extend(["--command-config", self.command_config])
        return run_command(cmd, capture_output=True, timeout=to)

    def generate(self, broker_ids: str, topics_to_move_json_path: Optional[str] = None) -> Optional[str]:
        """
        对应官方文档示例：--generate --broker-list 与可选 --topics-to-move-json-file（见 #basic_ops）。
        topics-to-move JSON 格式以发行版工具帮助与文档为准；列出 topic 可限定迁移范围。
        """
        args = ["--generate", "--broker-list", broker_ids]
        json_path = topics_to_move_json_path if (topics_to_move_json_path and Path(topics_to_move_json_path).exists()) else None
        if not json_path:
            import tempfile
            fd, tmp = tempfile.mkstemp(suffix=".json")
            try:
                os.write(fd, b'{"topics":[],"version":1}')
                os.close(fd)
                args.extend(["--topics-to-move-json-file", tmp])
                r = self._run(args)
                return r.stdout
            except Exception as ex:
                logger.warning("生成迁移计划（临时 JSON）失败: %s", ex, extra={"to_stdout": True})
                return None
            finally:
                try:
                    os.unlink(tmp)
                except Exception as ex_cleanup:
                    logger.debug("清理临时文件失败: %s", ex_cleanup)
        args.extend(["--topics-to-move-json-file", json_path])
        try:
            r = self._run(args)
            return r.stdout
        except CommandExecutionError:
            return None

    def execute(self, reassignment_json_path: str, throttle_bytes: Optional[int] = None) -> bool:
        """执行迁移（--execute --reassignment-json-file）"""
        args = ["--execute", "--reassignment-json-file", reassignment_json_path]
        if throttle_bytes:
            args.extend(["--throttle", str(throttle_bytes)])
        try:
            self._run(args, timeout=_reassign_cmd_timeout_sec())
            logger.info("✓ 副本迁移已提交", extra={"to_stdout": True})
            return True
        except CommandExecutionError as e:
            logger.error(f"执行迁移失败: {e}", extra={"to_stdout": True})
            return False

    def verify(self, reassignment_json_path: str) -> bool:
        """校验迁移进度（--verify）"""
        try:
            r = self._run(
                ["--verify", "--reassignment-json-file", reassignment_json_path],
                timeout=_reassign_cmd_timeout_sec(),
            )
            if "Reassignment of partition" in (r.stdout or "") and "completed" in (r.stdout or "").lower():
                logger.info("✓ 迁移已完成", extra={"to_stdout": True})
                return True
            return False
        except CommandExecutionError:
            return False


def _parse_bootstrap_server(bs: str, default_port: int = DEFAULT_BROKER_PORT) -> Tuple[str, int]:
    """解析 bootstrap_server 字符串为 (host, port)。"""
    s = (bs or "").strip() or "localhost"
    if ":" in s:
        parts = s.rsplit(":", 1)
        host = (parts[0] or "localhost").strip()
        try:
            port = int(parts[1].strip()) if parts[1].strip() else default_port
        except ValueError:
            port = default_port
    else:
        host = s or "localhost"
        port = default_port
    return host, port


def _require(condition: bool, message: str) -> None:
    """条件不满足时打日志并退出。用于 main 中必选参数校验。"""
    if not condition:
        logger.error(message, extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)


def _get_opt(args: argparse.Namespace, config: Dict[str, Any], attr: str, config_key: Optional[str] = None) -> Any:
    """从 args 或 config 取可选值；config_key 未指定时用 attr。"""
    key = config_key or attr.replace("-", "_")
    val = getattr(args, key, None)
    if val is not None and (not isinstance(val, str) or val.strip() != ""):
        return val
    return config.get(key)


def _kafka_deployer_kwargs(args: argparse.Namespace, config: Dict[str, Any]) -> Dict[str, Any]:
    """传给 KafkaDeployer 的版本相关参数（JSON 中可使用同名字段）。"""
    return {
        "assume_kafka_version": _get_opt(args, config, "assume_kafka_version"),
        "skip_kraft_version_check": bool(
            getattr(args, "skip_kraft_version_check", False) or config.get("skip_kraft_version_check")
        ),
    }


def _parse_target_host(target_host: str, default_port: int) -> Tuple[str, int]:
    """解析 target_host（支持 host 或 host:port）；空或仅空白时抛出 ValueError"""
    th = (target_host or "").strip()
    if not th:
        raise ValueError("target_host 不能为空")
    host, port = th, default_port
    if ":" in th:
        parts = th.rsplit(":", 1)
        if len(parts) == 2 and parts[1].isdigit():
            host, port = parts[0].strip(), int(parts[1])
    if host and not InputValidator.validate_hostname(host):
        raise ValueError(f"无效的 target_host 主机: {host}")
    if not InputValidator.validate_port(port):
        raise ValueError(f"无效的 target_host 端口: {port}")
    return host, port


def _is_local_host(host: str) -> bool:
    """判断是否为本机"""
    if not host:
        return True
    h = host.lower()
    if h in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return h in {socket.gethostname().lower(), socket.getfqdn().lower()}
    except Exception as ex:
        logger.debug("判断本机主机名失败: %s", ex)
        return False


def _build_remote_command(
        argv: List[str],
        remote_config_path: Optional[str],
        remote_script: Optional[str] = None
) -> str:
    """
    构建远程执行命令：过滤仅本机有效的参数（如 --target-host、--batch、SSH 相关），
    将 --config 替换为远程路径；若已复制配置但 argv 中无 --config（如批量部署），则追加 --config。
    """
    filtered: List[str] = []
    skip_next = False
    flags_with_value = {"--target-host", "--ssh-user", "--ssh-port", "--ssh-key", "--remote-workdir", "--config"}
    flags_no_value = {"--disable-ssh-host-check", "--batch"}
    for arg in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if arg in flags_no_value:
            continue
        if arg in flags_with_value:
            if arg == "--config" and remote_config_path:
                filtered.extend(["--config", remote_config_path])
            skip_next = True
            continue
        filtered.append(arg)
    # 批量部署时 argv 不含 --config，但已复制配置到远程，需显式传入
    if remote_config_path and "--config" not in filtered:
        filtered.extend(["--config", remote_config_path])

    if remote_script:
        return f"python3 {shlex.quote(remote_script)} " + " ".join(shlex.quote(a) for a in filtered)
    return "kafkacli " + " ".join(shlex.quote(a) for a in filtered)


def _run_remote_deploy(
        target_host: str,
        args: argparse.Namespace,
        config: Dict[str, Any],
        argv: List[str],
        exit_on_finish: bool = True
) -> Optional[bool]:
    """在前置机通过 SSH 在目标机执行部署，日志回显到本地。exit_on_finish=False 时返回成功与否不退出（供批量用）。"""
    ssh_user = config.get("ssh_user") or args.ssh_user
    ssh_port = config.get("ssh_port") or args.ssh_port or 22
    ssh_key = config.get("ssh_key") or args.ssh_key
    strict = not (config.get("disable_ssh_host_check") or args.disable_ssh_host_check)
    remote_workdir = config.get("remote_workdir") or args.remote_workdir or "/tmp/kafka_deploy"

    host, port = _parse_target_host(target_host, ssh_port)
    try:
        ssh = SSHManager(host=host, port=port, username=ssh_user, key_path=ssh_key, strict_host_key_checking=strict)
    except Exception as e:
        logger.error(f"初始化 SSH 失败: {e}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)

    # 检查远程是否有 kafkacli（与 StarCli 远程探测 starcli 一致）
    check_cmd = "command -v kafkacli 2>/dev/null || true"
    success, which_out = ssh.run_command(check_cmd, timeout=20)
    remote_script = None
    if not success or not (which_out or "").strip():
        logger.info(f"远程 {host}:{port} 未找到 kafkacli，将复制本地脚本到远程", extra={"to_stdout": True})
        local_script = Path(__file__).resolve() if __file__ and Path(__file__).exists() else None
        if not local_script or not local_script.exists():
            which_k = shutil.which("kafkacli")
            local_script = Path(which_k).resolve() if which_k and Path(which_k).exists() else None
        if not local_script or not local_script.exists():
            logger.error("无法确定本地 kafkacli 脚本路径，请确保脚本存在或安装到 PATH", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        ok, out = ssh.run_command(f"mkdir -p {shlex.quote(remote_workdir)}", timeout=20)
        if not ok:
            logger.error(f"创建远程目录失败: {out}", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        remote_script = f"{remote_workdir}/kafka_deploy.py"
        if not ssh.copy_file(str(local_script), remote_script):
            logger.error("复制脚本到远程失败", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        ssh.run_command(f"chmod +x {remote_script}", timeout=10)
        logger.info(f"已复制脚本到远程: {remote_script}", extra={"to_stdout": True})
    else:
        test_cmd = "kafkacli --help 2>&1 | head -5 || true"
        ok_help, help_out = ssh.run_command(test_cmd, timeout=20)
        if not ok_help:
            logger.warning(f"远程 kafkacli --help 探测异常: {help_out}", extra={"to_stdout": True})
        else:
            logger.debug("远程 kafkacli 可用")

    remote_config_path = None
    if args.config:
        if not remote_script:
            ssh.run_command(f"mkdir -p {shlex.quote(remote_workdir)}", timeout=20)
        remote_config_path = f"{remote_workdir}/config.json"
        if not ssh.copy_file(str(Path(args.config).resolve()), remote_config_path):
            logger.error("复制配置文件到远程失败", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)

    remote_cmd = _build_remote_command(argv, remote_config_path, remote_script)
    logger.info(f"正在远程主机 {host}:{port} 执行...", extra={"to_stdout": True})
    logger.info(f"远程命令: {remote_cmd}", extra={"to_stdout": True})
    success, output = ssh.run_command(f"{remote_cmd} 2>&1", timeout=3600)
    if success:
        if output:
            for line in output.strip().split("\n"):
                logger.info(line, extra={"to_stdout": True})
        if not exit_on_finish:
            return True
        sys.exit(EXIT_OK)
    if output:
        logger.error("远程执行失败，完整输出:", extra={"to_stdout": True})
        for line in output.strip().split("\n"):
            logger.error(f"  {line}", extra={"to_stdout": True})
    else:
        logger.error("远程执行失败（无输出）", extra={"to_stdout": True})
    logger.error(f"命令: {remote_cmd}", extra={"to_stdout": True})
    logger.error(f"目标: {host}:{port} (用户: {ssh_user})", extra={"to_stdout": True})
    if not output:
        logger.error("远程失败排查（本脚本约定，非 Kafka 项目文档）：", extra={"to_stdout": True})
        logger.error("  1. 目标机是否已将 kafkacli 安装到 PATH，或本次是否已成功复制脚本到远程", extra={"to_stdout": True})
        logger.error("  2. 在目标机手动执行: kafkacli --help", extra={"to_stdout": True})
        logger.error("  3. 检查目标机 Python3、SSH 与环境变量", extra={"to_stdout": True})
    if not exit_on_finish:
        return False
    sys.exit(EXIT_ERROR)


def load_json_config(config_file: str) -> Dict[str, Any]:
    """加载 JSON 配置文件；路径须非空且指向已存在的文件"""
    if not (config_file or "").strip():
        logger.error("配置文件路径不能为空", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    config_path = Path(config_file).resolve()
    if not config_path.exists() or not config_path.is_file():
        logger.error(f"配置文件不存在或不是文件: {config_file}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        if not isinstance(config, dict):
            logger.error("配置文件根类型须为 JSON 对象 {...}，不能为数组或标量", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        logger.info(f"✓ 加载配置文件: {config_file}", extra={"to_stdout": True})
        return config
    except json.JSONDecodeError as ex:
        logger.error(f"配置文件格式错误: {ex}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    except Exception as e:
        logger.error(f"加载配置文件失败: {e}", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)


def show_examples():
    """打印使用说明与示例；语义以 Apache Kafka 官方文档为准（见文件头「官方依据」URL）。"""
    examples = """
Kafka 自动化部署与运维（KRaft）- 封装官方 bin/ 脚本；部署与运维语义见:
  https://kafka.apache.org/documentation/#kraft
  https://kafka.apache.org/documentation/#basic_ops
  https://kafka.apache.org/documentation/#monitoring
  https://kafka.apache.org/documentation/#java
  KIP-833: https://cwiki.apache.org/confluence/display/KAFKA/KIP-833:+Mark+KRaft+as+Production+Ready

能力：前置机/批量部署、Topic/Group/Config/Quorum、分区重分配、指标采集、systemd、SASL（--command-config）。

版本与 JDK:
  - KRaft 对新集群 production-ready 自 3.3 起（KIP-833）；ZooKeeper 模式移除时间见各发行版发布说明。
  - 版本推断：kafka_home 路径或 libs/kafka-server-common-*.jar；可 --assume-kafka-version 或 KAFKA_CLI_ASSUME_VERSION。
  - JDK 要求以官方 #java 与各 tarball README 为准；本脚本门禁：4.x/未知→17+，3.x→11+。
  - KRaft 自动部署默认 Kafka ≥3.3；更低须 --skip-kraft-version-check。
  - systemd User/Group 须已存在；可 --user root 等做试验。
  - 与同仓库 StarCli 风格的环境检查、SSH 选项为本仓库约定。
  - log.dirs 落在安装目录下时告警（工程风险提示，非 Kafka 文档强制）。
  - advertised：对客户端可达地址须正确（见 #brokerconfigs advertised.listeners）；可用 KAFKA_ADVERTISED_HOST 或 extra_properties。

用法:
  部署与清理:
    kafkacli --deploy <standalone|controller|broker> [选项]
    kafkacli --clean --deploy <standalone|controller|broker> [--node-id N]
    kafkacli --batch --config cluster.json              # 按 nodes 列表批量远程部署
    kafkacli --target-host HOST [SSH 选项] --deploy ...  # 单机远程部署
  集群与运维:
    kafkacli --status [--bootstrap-server] [--kafka-home] [--command-config]
    kafkacli --metrics [--metrics-json]                  # 指标采集（Quorum/UnderReplicated/Lag）
    kafkacli --topic-create --topic NAME [--partitions] [--replication-factor]
    kafkacli --topic-list | --topic-describe [--topic] | --topic-delete --topic NAME
    kafkacli --group-list | --group-describe --consumer-group NAME
    kafkacli --config-describe-broker [--config-entity-name id] | --config-describe-topic --topic NAME
    kafkacli --quorum-add-controller                     # KRaft 动态添加 Controller
  Broker 下线（分区重分配后再停进程；见 #basic_ops Decommissioning brokers / Partition reassignment）:
    kafkacli --broker-decommission-generate --broker-list 1,2 [--topics-to-move-json-file PATH]
    kafkacli --broker-decommission-execute --reassignment-json-file plan.json [--throttle N]
    kafkacli --broker-decommission-verify --reassignment-json-file plan.json
  kafkacli --help

示例:

1) 单节点快速体验（KRaft combined，脚本自动生成完整 listener 套件，适用于 3.3+ / 4.x）:
   export KAFKA_ADVERTISED_HOST=127.0.0.1   # 生产改为本机可达 IP
   kafkacli --deploy standalone --kafka-home /opt/kafka --log-dirs /tmp/kafka-logs

2) 批量多机部署（配置文件 nodes 数组，依次 SSH 到每台执行）:
   # cluster.json 示例: { "kafka_home": "/opt/kafka", "nodes": [
   #   { "target_host": "ctrl1", "deploy": "controller", "node_id": 1, "metadata_log_dir": "/var/kafka/meta", ... },
   #   { "target_host": "broker1", "deploy": "broker", "node_id": 1, "log_dirs": "/var/kafka/logs", "cluster_id": "xxx", ... }
   # ]}
   kafkacli --batch --config cluster.json

3) 前置机远程部署单节点:
   kafkacli --target-host 192.168.1.10 --deploy standalone --kafka-home /opt/kafka --log-dirs /var/kafka/logs
   kafkacli --target-host broker1 --deploy broker --kafka-home /opt/kafka --node-id 1 \\
     --controller-quorum-bootstrap-servers "ctrl1:9093,ctrl2:9093,ctrl3:9093" \\
     --log-dirs /var/kafka/logs --cluster-id <CLUSTER_ID> --config cluster.json

4) 生产环境 KRaft：3 台 Controller + N 台 Broker
   kafkacli --deploy controller --kafka-home /opt/kafka --node-id 1 \\
     --controller-quorum-bootstrap-servers "ctrl1:9093,ctrl2:9093,ctrl3:9093" \\
     --metadata-log-dir /var/kafka/metadata-log --config cluster.json
   kafkacli --deploy controller --kafka-home /opt/kafka --node-id 2 --cluster-id <CLUSTER_ID> ...
   kafkacli --deploy broker --kafka-home /opt/kafka --node-id 1 --cluster-id <CLUSTER_ID> \\
     --controller-quorum-bootstrap-servers "ctrl1:9093,ctrl2:9093,ctrl3:9093" --log-dirs /var/kafka/logs

5) 集群状态与指标（#monitoring；JSON 为本脚本输出格式）:
   kafkacli --status --bootstrap-server broker1:9092 --kafka-home /opt/kafka
   kafkacli --metrics --kafka-home /opt/kafka --bootstrap-server broker1:9092
   kafkacli --metrics-json --kafka-home /opt/kafka --bootstrap-server broker1:9092

6) Topic / Consumer Group 运维:
   kafkacli --topic-create --topic my-topic --partitions 6 --replication-factor 2 --kafka-home /opt/kafka
   kafkacli --topic-list --kafka-home /opt/kafka --bootstrap-server broker1:9092
   kafkacli --topic-describe --topic my-topic --kafka-home /opt/kafka
   kafkacli --group-describe --consumer-group my-consumer --kafka-home /opt/kafka --bootstrap-server broker1:9092

7) Broker 下线（与 #basic_ops 中 kafka-reassign-partitions 工作流一致：generate → execute → verify，再停 Broker）:
   kafkacli --broker-decommission-generate --broker-list 1,2 --kafka-home /opt/kafka --bootstrap-server broker1:9092
   # 将输出中的 Current partition reassignment configuration 保存为 plan.json
   kafkacli --broker-decommission-execute --reassignment-json-file plan.json --throttle 1048576 --kafka-home /opt/kafka
   kafkacli --broker-decommission-verify --reassignment-json-file plan.json --kafka-home /opt/kafka
   # 迁移完成后在该 Broker 上: kafkacli --clean --deploy broker --node-id <id> --kafka-home /opt/kafka

8) 清理与配置查看:
   kafkacli --clean --deploy standalone --kafka-home /opt/kafka
   kafkacli --config-describe-broker --kafka-home /opt/kafka --config-entity-name 1
   kafkacli --config-describe-topic --topic my-topic --kafka-home /opt/kafka
"""
    print(examples)


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Kafka 部署与运维（KRaft）；行为以 https://kafka.apache.org/documentation/ "
            "（#kraft、#basic_ops、#brokerconfigs）及发行版 bin/ 工具为准。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False
    )
    parser.add_argument("-h", "--help", action="store_true", help="显示使用说明和示例")
    parser.add_argument("--deploy", choices=["standalone", "controller", "broker"], help="部署类型")
    parser.add_argument("--kafka-home", help="Kafka 安装目录（解压后的根目录）")
    parser.add_argument("--config", help="JSON 配置文件路径")
    parser.add_argument(
        "--assume-kafka-version",
        metavar="X.Y.Z",
        help="无法从安装路径或 libs/kafka-server-common-*.jar 推断 Kafka 版本时指定（如 3.7.0、4.2.0）；或设环境变量 KAFKA_CLI_ASSUME_VERSION",
    )
    parser.add_argument(
        "--skip-kraft-version-check",
        action="store_true",
        help="跳过「KRaft 自动部署须 Kafka 3.3+（KIP-833）」检查，自担风险",
    )

    # 通用
    parser.add_argument("--log-dirs", help="Broker/Standalone 日志目录，逗号分隔（官方 log.dirs）")
    parser.add_argument("--metadata-log-dir", help="Controller 元数据日志目录（metadata.log.dir）")
    parser.add_argument("--node-id", type=int, help="KRaft node.id（controller/broker 必填）")
    parser.add_argument("--cluster-id", help="KRaft 集群 ID（多节点时与首节点一致）")
    parser.add_argument("--controller-quorum-bootstrap-servers", help="controller.quorum.bootstrap.servers，逗号分隔 host:port")
    parser.add_argument("--controller-port", type=int, default=DEFAULT_CONTROLLER_PORT, help=f"Controller 监听端口 (默认 {DEFAULT_CONTROLLER_PORT})")
    parser.add_argument(
        "--listeners",
        help="listeners；standalone 仅写 PLAINTEXT 时会自动追加 CONTROLLER（KRaft combined）；"
             "生产请配合环境变量 KAFKA_ADVERTISED_HOST 或 extra_properties 中的 advertised.listeners",
    )
    parser.add_argument("--initial-controllers", help="KRaft 多 controller 时首次 format 的 initial-controllers 列表")
    parser.add_argument("--java-home", help="JAVA_HOME 路径")
    parser.add_argument(
        "--user",
        default="kafka",
        help="systemd 运行用户（须已存在于系统，默认 kafka；缺失则 217/USER。测试可传当前用户）",
    )
    parser.add_argument(
        "--group",
        default="kafka",
        help="systemd 运行组（须已存在，默认 kafka）",
    )
    parser.add_argument("--no-systemd", action="store_true", help="不安装 systemd 服务")
    parser.add_argument("--verify", action="store_true", help="部署后验证 Broker 可连接")
    parser.add_argument("--force", action="store_true", help="强制覆盖已存在配置并重新 format（慎用）")
    parser.add_argument("--clean", action="store_true", help="仅清理服务与生成配置，不部署")
    parser.add_argument("--status", action="store_true", help="显示集群状态（元数据 Quorum + 副本信息）")
    parser.add_argument("--bootstrap-server", default="localhost:9092", help="--status 时连接的 bootstrap server")
    parser.add_argument("--command-config", help="--status 时可选，Kafka 客户端配置文件（如 SASL/SSL）")

    # 远程部署（前置机 → 目标机）
    parser.add_argument("--target-host", help="远程目标主机（host 或 host:port），指定则在本机 SSH 到目标机执行")
    parser.add_argument("--ssh-user", default="root", help="SSH 用户名")
    parser.add_argument("--ssh-port", type=int, default=22, help="SSH 端口")
    parser.add_argument("--ssh-key", default="~/.ssh/id_rsa", help="SSH 私钥路径")
    parser.add_argument("--disable-ssh-host-check", action="store_true", help="禁用 SSH 主机密钥检查（不推荐）")
    parser.add_argument("--remote-workdir", help="远程工作目录 (默认: /tmp/kafka_deploy)")

    # 批量部署（config 中 nodes: [{ target_host, deploy, node_id, ... }]）
    parser.add_argument("--batch", action="store_true", help="按配置文件 nodes 列表批量远程部署（需 --config）")

    # Topic 运维（官方 kafka-topics.sh）
    parser.add_argument("--topic-create", action="store_true", help="创建 Topic")
    parser.add_argument("--topic-delete", action="store_true", help="删除 Topic")
    parser.add_argument("--topic-describe", action="store_true", help="描述 Topic")
    parser.add_argument("--topic-list", action="store_true", help="列出所有 Topic")
    parser.add_argument("--topic", help="Topic 名称（与 --topic-* 配合）")
    parser.add_argument("--partitions", type=int, default=1, help="分区数（--topic-create，默认 1）")
    parser.add_argument("--replication-factor", type=int, default=1, help="副本数（--topic-create，默认 1）")

    # Consumer Group 运维（kafka-consumer-groups.sh）
    parser.add_argument("--group-list", action="store_true", help="列出所有 Consumer Group")
    parser.add_argument("--group-describe", action="store_true", help="描述 Group（含 Lag）")
    parser.add_argument("--consumer-group", help="Consumer Group 名称（与 --group-describe 配合）")

    # 指标采集（封装官方 CLI；语义见文档 #monitoring）
    parser.add_argument("--metrics", action="store_true", help="采集并打印关键指标（Quorum/副本健康/Lag）")
    parser.add_argument("--metrics-json", action="store_true", help="指标输出为 JSON（便于接入监控）")

    # 配置运维（kafka-configs.sh）
    parser.add_argument("--config-describe-broker", action="store_true", help="查看 Broker 配置")
    parser.add_argument("--config-describe-topic", action="store_true", help="查看 Topic 配置")
    parser.add_argument("--config-entity-name", help="--config-describe-* 的 entity name（如 broker id）")

    # KRaft Quorum 运维
    parser.add_argument("--quorum-add-controller", action="store_true", help="动态添加 Controller（KRaft）")

    # Broker 下线（副本迁移后停 Broker）
    parser.add_argument("--broker-decommission-generate", action="store_true",
                        help="生成副本迁移计划（kafka-reassign-partitions --generate；见文档 #basic_ops）")
    parser.add_argument("--broker-decommission-execute", action="store_true", help="执行副本迁移（--reassignment-json-file）")
    parser.add_argument("--broker-decommission-verify", action="store_true", help="校验迁移进度")
    parser.add_argument("--broker-list", help="副本迁移目标 Broker ID 列表，逗号分隔（如 1,2,3）")
    parser.add_argument(
        "--topics-to-move-json-file",
        "--topics-to-move-json",
        dest="topics_to_move_json",
        metavar="PATH",
        help="kafka-reassign-partitions 的 --topics-to-move-json-file（#basic_ops Partition reassignment）",
    )
    parser.add_argument("--reassignment-json-file", help="副本迁移 JSON 文件路径（execute/verify）")
    parser.add_argument("--throttle", type=int, help="迁移限流（字节/秒）")

    args = parser.parse_args()

    if args.help:
        show_examples()
        return

    config = {}
    if args.config:
        config = load_json_config(args.config)

    # 远程部署：在前置机执行时，将命令转发到目标机并回显日志
    target_host = config.get("target_host") or args.target_host
    if target_host:
        try:
            _parse_target_host(target_host, args.ssh_port or 22)
        except ValueError as e:
            logger.error(f"无效的 target_host: {target_host}，{e}", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        if not _is_local_host(target_host):
            _run_remote_deploy(target_host, args, config, sys.argv)
            return

    # 批量部署：按 config.nodes 依次远程部署每台机器
    if getattr(args, "batch", False):
        _require(args.config, "--batch 需同时指定 --config（含 nodes 数组的 JSON 文件）")
        nodes = config.get("nodes")
        _require(nodes, "--batch 需在配置文件中提供 nodes 数组")
        _require(isinstance(nodes, list) and len(nodes) > 0, "--batch 需在配置文件中提供非空 nodes 数组")
        for i, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue
            th = node.get("target_host")
            if not th:
                logger.warning(f"nodes[{i}] 缺少 target_host，跳过", extra={"to_stdout": True})
                continue
            logger.info(f"=== 批量部署 [{i+1}/{len(nodes)}] 目标: {th} ===", extra={"to_stdout": True})
            # 合并 node 与 config，构建远程执行的 argv（不含 --target-host / --batch）
            argv_parts = [sys.argv[0]]
            for k, v in node.items():
                if k in ("target_host", "ssh_user", "ssh_port", "ssh_key", "remote_workdir", "disable_ssh_host_check"):
                    continue
                key = "--" + k.replace("_", "-")
                if isinstance(v, bool):
                    if v:
                        argv_parts.append(key)
                elif v is not None and str(v).strip() != "":
                    argv_parts.append(key)
                    argv_parts.append(str(v))
            # 全局 config 中的 kafka_home 等若 node 未覆盖则从 config 取
            base = config.get("kafka_home") or args.kafka_home
            if base and "--kafka-home" not in argv_parts:
                argv_parts.extend(["--kafka-home", base])
            ok = _run_remote_deploy(th, args, config, argv_parts, exit_on_finish=False)
            if ok is False:
                logger.error(f"批量部署在节点 {th} 失败，终止", extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
        logger.info("=== 批量部署全部完成 ===", extra={"to_stdout": True})
        return

    kafka_home = args.kafka_home or config.get("kafka_home")

    # 仅查看状态：kafka_home 可选；有则拉取完整 Quorum 状态，无则仅检查 Broker 端口
    if args.status and not (args.deploy or config.get("deploy")):
        if kafka_home:
            try:
                deployer = KafkaDeployer(
                    kafka_home,
                    user=config.get("user", "kafka"),
                    group=config.get("group", "kafka"),
                    **_kafka_deployer_kwargs(args, config),
                )
                deployer.show_cluster_status(
                    bootstrap_server=args.bootstrap_server or config.get("bootstrap_server", "localhost:9092"),
                    command_config=args.command_config or config.get("command_config")
                )
            except ValueError as e:
                logger.error(str(e), extra={"to_stdout": True})
                sys.exit(EXIT_ERROR)
        else:
            host, port = _parse_bootstrap_server(
                args.bootstrap_server or config.get("bootstrap_server", ""), DEFAULT_BROKER_PORT
            )
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(5)
                    s.connect((host, port))
                logger.info(f"Broker 可连接: {host}:{port}", extra={"to_stdout": True})
            except Exception as e:
                logger.warning(f"无法连接 Broker {host}:{port}: {e}", extra={"to_stdout": True})
        return

    # Topic / Group / 指标 / 配置 / Quorum / Broker 下线 运维子命令
    bootstrap = args.bootstrap_server or config.get("bootstrap_server", "localhost:9092")
    cmd_config = args.command_config or config.get("command_config")

    if getattr(args, "topic_create", False):
        _require(kafka_home, "--topic-create 需指定 --kafka-home")
        topic = _get_opt(args, config, "topic")
        _require(topic, "--topic-create 需指定 --topic")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if mgr.create(topic, args.partitions, args.replication_factor) else EXIT_ERROR)

    if getattr(args, "topic_delete", False):
        _require(kafka_home, "--topic-delete 需指定 --kafka-home")
        topic = _get_opt(args, config, "topic")
        _require(topic, "--topic-delete 需指定 --topic")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if mgr.delete(topic) else EXIT_ERROR)

    if getattr(args, "topic_describe", False):
        _require(kafka_home, "--topic-describe 需指定 --kafka-home")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        out = mgr.describe(_get_opt(args, config, "topic"))
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "topic_list", False):
        _require(kafka_home, "--topic-list 需指定 --kafka-home")
        mgr = KafkaTopicManager(kafka_home, bootstrap, cmd_config)
        out = mgr.list()
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "group_list", False):
        _require(kafka_home, "--group-list 需指定 --kafka-home")
        mgr = KafkaConsumerGroupManager(kafka_home, bootstrap, cmd_config)
        out = mgr.list_groups()
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "group_describe", False):
        _require(kafka_home, "--group-describe 需指定 --kafka-home")
        grp = _get_opt(args, config, "consumer_group")
        _require(grp, "--group-describe 需指定 --consumer-group")
        mgr = KafkaConsumerGroupManager(kafka_home, bootstrap, cmd_config)
        out = mgr.describe_group(grp)
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "metrics", False) or getattr(args, "metrics_json", False):
        _require(kafka_home, "--metrics 需指定 --kafka-home")
        coll = KafkaMetricsCollector(kafka_home, bootstrap, cmd_config)
        data = coll.collect_all()
        if getattr(args, "metrics_json", False):
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            print("=== Quorum ===")
            print(json.dumps(data.get("quorum", {}), ensure_ascii=False))
            print("=== Partition 健康（Under-Replicated）===")
            print(json.dumps(data.get("partition_health", {}), ensure_ascii=False))
            print("=== Consumer Lag ===")
            print(json.dumps(data.get("consumer_lag", {}), ensure_ascii=False))
        sys.exit(EXIT_OK)

    if getattr(args, "config_describe_broker", False):
        _require(kafka_home, "--config-describe-broker 需指定 --kafka-home")
        mgr = KafkaConfigManager(kafka_home, bootstrap, cmd_config)
        entity = _get_opt(args, config, "config_entity_name")
        bid = int(entity) if entity and str(entity).isdigit() else None
        out = mgr.describe_broker(bid)
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "config_describe_topic", False):
        _require(kafka_home, "--config-describe-topic 需指定 --kafka-home")
        topic = _get_opt(args, config, "topic") or _get_opt(args, config, "config_entity_name")
        _require(topic, "--config-describe-topic 需指定 --topic 或 --config-entity-name")
        mgr = KafkaConfigManager(kafka_home, bootstrap, cmd_config)
        out = mgr.describe_topic(topic)
        if out:
            print(out)
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "quorum_add_controller", False):
        _require(kafka_home, "--quorum-add-controller 需指定 --kafka-home")
        qm = KafkaQuorumManager(kafka_home, bootstrap_server=bootstrap, command_config=cmd_config)
        sys.exit(EXIT_OK if qm.add_controller() else EXIT_ERROR)

    if getattr(args, "broker_decommission_generate", False):
        _require(kafka_home, "--broker-decommission-generate 需指定 --kafka-home")
        blist = _get_opt(args, config, "broker_list")
        _require(blist, "--broker-decommission-generate 需指定 --broker-list（目标 Broker ID 列表）")
        dec = KafkaBrokerDecommission(kafka_home, bootstrap, cmd_config)
        out = dec.generate(blist, _get_opt(args, config, "topics_to_move_json"))
        if out:
            print(out)
            logger.info("请将上述 Current partition reassignment configuration 保存为 JSON 文件，再使用 --broker-decommission-execute", extra={"to_stdout": True})
        sys.exit(EXIT_OK if out else EXIT_ERROR)

    if getattr(args, "broker_decommission_execute", False):
        _require(kafka_home, "--broker-decommission-execute 需指定 --kafka-home")
        path = _get_opt(args, config, "reassignment_json_file")
        _require(path, "--broker-decommission-execute 需指定 --reassignment-json-file")
        _require(Path(path).exists(), f"reassignment 文件不存在: {path}")
        dec = KafkaBrokerDecommission(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if dec.execute(path, _get_opt(args, config, "throttle")) else EXIT_ERROR)

    if getattr(args, "broker_decommission_verify", False):
        _require(kafka_home, "--broker-decommission-verify 需指定 --kafka-home")
        path = _get_opt(args, config, "reassignment_json_file")
        _require(path, "--broker-decommission-verify 需指定 --reassignment-json-file")
        _require(Path(path).exists(), f"reassignment 文件不存在: {path}")
        dec = KafkaBrokerDecommission(kafka_home, bootstrap, cmd_config)
        sys.exit(EXIT_OK if dec.verify(path) else EXIT_ERROR)

    _require(kafka_home, "必须指定 --kafka-home 或配置文件中的 kafka_home")

    try:
        deployer = KafkaDeployer(
            kafka_home,
            user=args.user or config.get("user", "kafka"),
            group=args.group or config.get("group", "kafka"),
            **_kafka_deployer_kwargs(args, config),
        )
    except ValueError as e:
        logger.error(str(e), extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)

    if args.clean:
        deploy_type = args.deploy or config.get("deploy")
        if not deploy_type:
            logger.error("--clean 需指定 --deploy standalone|controller|broker", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        node_id = args.node_id or config.get("node_id")
        if not deployer.clean_deployment(deploy_type, node_id=node_id, backup_config=True):
            sys.exit(EXIT_ERROR)
        sys.exit(EXIT_OK)

    deploy_type = args.deploy or config.get("deploy")
    if not deploy_type:
        logger.error("必须指定 --deploy standalone|controller|broker 或配置文件中的 deploy", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)

    enable_systemd = not (args.no_systemd or config.get("no_systemd", False))
    java_home = args.java_home or config.get("java_home")
    extra_properties = config.get("extra_properties") or config.get("server_properties")

    if deploy_type == "standalone":
        log_dirs = args.log_dirs or config.get("log_dirs") or DEFAULT_LOG_DIR
        if not InputValidator.validate_path(log_dirs.split(",")[0].strip()):
            logger.error("无效的 --log-dirs 路径", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        success = deployer.deploy_standalone(
            log_dirs=log_dirs,
            cluster_id=args.cluster_id or config.get("cluster_id"),
            node_id=args.node_id or config.get("node_id", 1),
            listeners=args.listeners or config.get("listeners"),
            java_home=java_home,
            enable_systemd=enable_systemd,
            force=args.force,
            extra_properties=extra_properties,
        )
    elif deploy_type == "controller":
        node_id = args.node_id or config.get("node_id")
        if node_id is None:
            logger.error("部署 Controller 必须指定 --node-id", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        quorum = args.controller_quorum_bootstrap_servers or config.get("controller_quorum_bootstrap_servers")
        if not quorum:
            logger.error("必须指定 --controller-quorum-bootstrap-servers", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        success = deployer.deploy_controller(
            node_id=node_id,
            controller_quorum_bootstrap_servers=quorum,
            controller_listener_port=args.controller_port or config.get("controller_port", DEFAULT_CONTROLLER_PORT),
            metadata_log_dir=args.metadata_log_dir or config.get("metadata_log_dir"),
            log_dirs=args.log_dirs or config.get("log_dirs"),
            cluster_id=args.cluster_id or config.get("cluster_id"),
            initial_controllers=args.initial_controllers or config.get("initial_controllers"),
            java_home=java_home,
            enable_systemd=enable_systemd,
            force=args.force,
            extra_properties=extra_properties,
        )
    else:  # broker
        node_id = args.node_id or config.get("node_id")
        if node_id is None:
            logger.error("部署 Broker 必须指定 --node-id", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        quorum = args.controller_quorum_bootstrap_servers or config.get("controller_quorum_bootstrap_servers")
        if not quorum:
            logger.error("必须指定 --controller-quorum-bootstrap-servers", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        log_dirs = args.log_dirs or config.get("log_dirs")
        if not log_dirs:
            logger.error("必须指定 --log-dirs", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        cluster_id = args.cluster_id or config.get("cluster_id")
        if not cluster_id:
            logger.error("Broker 必须指定 --cluster-id", extra={"to_stdout": True})
            sys.exit(EXIT_ERROR)
        success = deployer.deploy_broker(
            node_id=node_id,
            controller_quorum_bootstrap_servers=quorum,
            log_dirs=log_dirs,
            listeners=args.listeners or config.get("listeners"),
            cluster_id=cluster_id,
            java_home=java_home,
            enable_systemd=enable_systemd,
            force=args.force,
            extra_properties=extra_properties,
        )

    if success:
        if enable_systemd:
            logger.info(
                "=== 部署成功：systemd 已为 active，且必备监听端口 TCP 探测通过 ===",
                extra={"to_stdout": True},
            )
        else:
            logger.info(
                "=== 部署完成（未由 systemd 拉起进程）：配置与 storage format 已就绪；"
                "请按上文命令手动启动并自行确认服务，本结果不表示 Broker 已在线 ===",
                extra={"to_stdout": True},
            )
        if args.verify and deploy_type in ("standalone", "broker"):
            time.sleep(2)
            deployer.verify_broker_started()
    else:
        logger.error("=== 部署失败 ===", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("\n操作被用户中断", extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)
    except Exception as err:
        logger.error(f"程序异常: {err}", exc_info=True, extra={"to_stdout": True})
        sys.exit(EXIT_ERROR)

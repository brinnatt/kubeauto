#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
网络连接稳定性测试工具（生产环境）

- 启动前前置校验（时长上限、报告/日志路径可写、ping 可用、默认 NODE_IPS 是否全无效等），与 --dry-run 共用同一套逻辑
- 输入校验后使用 subprocess 参数列表调用 ping，避免注入
- 目标语法：ICMP 支持 IPv4 / IPv6 / 域名、IPv4 末段区间、IPv4 CIDR（.hosts() 展开）；
  TCP 仅为 host:port（单一冒号 + 数字端口），仅 IPv4 或域名（不支持 IPv6:port 与地址段）
- ICMP 按 --max-icmp-workers 分片轮询，控制线程与子进程数量
- 多线程探测 + Event；SIGINT/SIGTERM 与阈值退出码（0/1/2）见 --help epilog

需要 Python 3.8+。
"""

from __future__ import annotations

__version__ = "1.3.1"

import argparse
import ipaddress
import json
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, DefaultDict, Dict, List, Optional, Tuple, Union

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
DEFAULT_TEST_DURATION = 7200
PING_INTERVAL = 1
PING_TIMEOUT = 2
PING_COUNT = 3
TCP_TIMEOUT = 2

MIN_PORT = 1
MAX_PORT = 65535
MAX_HOSTNAME_LENGTH = 253


def is_tcp_style_target(s: str) -> bool:
    """唯一 `:` 且右侧为十进制端口 → host:port（与 expand_cli_target / validate_target 共用）。"""
    if s.count(":") != 1:
        return False
    left, right = s.split(":", 1)
    if not right.isdigit():
        return False
    try:
        pr = int(right)
    except ValueError:
        return False
    if not MIN_PORT <= pr <= MAX_PORT:
        return False
    if "/" in left:
        return False
    return True


# 报告与健康判定阈值（可按环境调整）
ICMP_FAIL_SAMPLE_RATIO_MAX_PCT = 5.0  # 失败/无统计样本占比上限
ICMP_AVG_LATENCY_MS_WARN = 100.0
ICMP_JITTER_MS_WARN = 50.0
TCP_SUCCESS_RATIO_MIN_PCT = 95.0
TCP_AVG_LATENCY_MS_WARN = 200.0

DEFAULT_LOG_BASENAME = "netcheck.log"
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

LOG_DIR = os.environ.get("NETCHECK_LOG_DIR", os.path.join(os.getcwd(), "logs"))

# 展开 / 并发 / 时长（环境变量非法时回退默认值，避免进程启动即崩溃）
MIN_IPV4_CIDR_PREFIX = 8  # 禁止比 /8 更短，避免整 A 类误扩


def _env_int(name: str, default: int, min_v: int = 1, max_v: int = 2**31 - 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        v = int(raw.strip(), 10)
    except ValueError:
        return default
    return max(min_v, min(max_v, v))


DEFAULT_MAX_EXPAND = _env_int("NETCHECK_MAX_EXPAND", 1024, 1, 1_000_000)
DEFAULT_MAX_ICMP_WORKERS = _env_int("NETCHECK_MAX_ICMP_WORKERS", 64, 1, 4096)
# 单次运行最长时长（秒），防止误输入极大值占满调度；可用 NETCHECK_MAX_DURATION_SEC 调整
MAX_TEST_DURATION_SEC = _env_int("NETCHECK_MAX_DURATION_SEC", 30 * 86400, 60, 366 * 86400)

NODE_IPS = [
    "11.2.26.250", "11.2.26.251", "11.2.26.252",
    "11.2.26.1", "11.2.26.2", "11.2.26.3", "11.2.26.4",
    "11.2.26.5", "11.2.26.7", "11.2.26.8", "11.2.26.9",
    "11.2.26.33", "11.2.26.34", "11.2.26.35", "11.2.26.36", "11.2.26.37",
    "11.2.26.40",
    "11.2.26.50",
    "11.2.26.57",
]


class NetcheckError(Exception):
    """netcheck 业务错误基类"""


class NetcheckValidationError(NetcheckError):
    """参数或路径校验失败"""


# ---------------------------------------------------------------------------
# 日志（默认：控制台 + 滚动文件，与 starcli 默认落盘思路一致）
# ---------------------------------------------------------------------------
def setup_logging(
    verbose: bool = False,
    log_file: Optional[Union[str, Path]] = None,
    no_log_file: bool = False,
    name: str = "netcheck",
) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    log = logging.getLogger(name)
    log.handlers.clear()
    log.setLevel(level)
    log.propagate = False

    fmt = logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(fmt)
    log.addHandler(sh)

    if not no_log_file:
        if not os.path.isdir(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        path = Path(log_file) if log_file else Path(LOG_DIR) / DEFAULT_LOG_BASENAME
        if not path.is_absolute():
            path = Path(LOG_DIR) / path.name
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            str(path), maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setLevel(level)
        fh.setFormatter(fmt)
        log.addHandler(fh)

    return log


logger = logging.getLogger("netcheck")


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------
class InputValidator:
    """主机 / 目标 / 输出路径校验（参考 starcli.InputValidator）。"""

    _FORBIDDEN_HOST_CHARS = frozenset(';|&$`()<> \t\n\r')

    @classmethod
    def normalize_str(cls, value: Any, label: str = "值") -> str:
        if value is None:
            raise NetcheckValidationError("%s 不能为空" % label)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if not isinstance(value, str):
            raise NetcheckValidationError("%s 必须是字符串" % label)
        s = value.strip()
        if not s:
            raise NetcheckValidationError("%s 不能为空" % label)
        if len(s) > MAX_HOSTNAME_LENGTH:
            raise NetcheckValidationError("%s 过长（最多 %d 字符）" % (label, MAX_HOSTNAME_LENGTH))
        return s

    @classmethod
    def validate_host(cls, host: Any) -> str:
        host = cls.normalize_str(host, "主机")
        bad = set(host) & cls._FORBIDDEN_HOST_CHARS
        if bad:
            raise NetcheckValidationError("主机包含非法字符: %s" % ", ".join(sorted(bad)))
        if any(ord(ch) < 32 for ch in host):
            raise NetcheckValidationError("主机包含不可见控制字符")
        try:
            ipaddress.ip_address(host)
            return host
        except ValueError:
            pass
        if not re.match(
            r"^[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)*$",
            host,
        ):
            raise NetcheckValidationError("无效的主机格式: %s" % host)
        return host

    @classmethod
    def validate_target(cls, target: Any) -> str:
        """与 is_tcp_style_target / expand_cli_target 一致：仅「单一冒号 + 数字端口」视为 TCP。"""
        target = cls.normalize_str(target, "目标")
        if is_tcp_style_target(target):
            host_part, port_part = target.split(":", 1)
            cls.validate_host(host_part)
            port = int(port_part)
            if not MIN_PORT <= port <= MAX_PORT:
                raise NetcheckValidationError("端口号必须在 %d–%d 之间" % (MIN_PORT, MAX_PORT))
            return "%s:%d" % (host_part, port)
        # 单一目标串：ICMP 用（含 IPv6 字面量等多冒号形式）；TCP 段展开须走 expand_cli_target
        return cls.validate_host(target)

    @classmethod
    def validate_report_dir(cls, path_str: str) -> Path:
        """解析报告目录，禁止裸 .. 段，解析为绝对路径。"""
        s = cls.normalize_str(path_str, "报告目录")
        p = Path(s)
        if ".." in p.parts:
            raise NetcheckValidationError("报告目录路径不能包含 '..'")
        resolved = p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
        return resolved


# ---------------------------------------------------------------------------
# 目标展开：IPv4 末段区间、IPv4 CIDR（生产约束：上限 + 线程分片）
# ---------------------------------------------------------------------------
_RE_IPV4_LAST_OCTET_RANGE = re.compile(
    r"^(\d{1,3}\.\d{1,3}\.\d{1,3})\.(\d{1,3})-(\d{1,3})$"
)


def _octets_0_255(parts: Tuple[str, ...]) -> None:
    for p in parts:
        v = int(p)
        if not 0 <= v <= 255:
            raise NetcheckValidationError("IPv4 段越界: %s" % ".".join(parts))


def expand_last_octet_range(s: str, max_expand: int) -> List[str]:
    """192.168.125.29-87 → 192.168.125.29 … 192.168.125.87"""
    m = _RE_IPV4_LAST_OCTET_RANGE.match(s.strip())
    if not m:
        raise NetcheckValidationError("无效的 IPv4 末段区间（示例 192.168.125.29-87）: %s" % s)
    prefix, lo_s, hi_s = m.group(1), m.group(2), m.group(3)
    lo, hi = int(lo_s), int(hi_s)
    _octets_0_255(tuple(prefix.split(".")))
    if not 0 <= lo <= 255 or not 0 <= hi <= 255:
        raise NetcheckValidationError("末段必须在 0–255: %s" % s)
    if lo > hi:
        raise NetcheckValidationError("区间起点不能大于终点: %s" % s)
    n = hi - lo + 1
    if n > max_expand:
        raise NetcheckValidationError(
            "展开 %d 个 IP，超过 --max-expand=%d；请缩小范围或提高上限" % (n, max_expand)
        )
    out: List[str] = []
    base = prefix + "."
    for i in range(lo, hi + 1):
        ip = "%s%d" % (base, i)
        ipaddress.IPv4Address(ip)
        out.append(ip)
    return out


def expand_ipv4_cidr(s: str, max_expand: int) -> List[str]:
    """IPv4 CIDR → 可 ping 主机列表（/32 单主机；其余用 hosts 去掉网络位与广播位）。"""
    raw = s.strip()
    try:
        net = ipaddress.ip_network(raw, strict=False)
    except ValueError as e:
        raise NetcheckValidationError("无效的 CIDR: %s" % raw) from e
    if net.version != 4:
        raise NetcheckValidationError("当前仅支持 IPv4 CIDR 展开: %s" % raw)
    if not isinstance(net, ipaddress.IPv4Network):
        raise NetcheckValidationError("内部错误: 非 IPv4Network")
    if net.prefixlen < MIN_IPV4_CIDR_PREFIX:
        raise NetcheckValidationError(
            "CIDR 前缀不可短于 /%d（防止误扩过大网段）: %s" % (MIN_IPV4_CIDR_PREFIX, raw)
        )

    if net.prefixlen == 32:
        ips = [str(net.network_address)]
    elif net.prefixlen == 31:
        ips = [str(h) for h in net.hosts()]
    else:
        ips = [str(h) for h in net.hosts()]

    if len(ips) > max_expand:
        raise NetcheckValidationError(
            "CIDR %s 展开为 %d 个地址，超过 --max-expand=%d" % (raw, len(ips), max_expand)
        )
    for ip in ips:
        InputValidator.validate_host(ip)
    return ips


def expand_cli_target(raw: str, max_expand: int) -> List[str]:
    """
    将单个 CLI 目标展开为 1..N 个探测端点（顺序稳定，去重则在上层做）。
    支持: 单 IP/域名、host:port(TCP)、末段区间、IPv4 CIDR。
    """
    s = InputValidator.normalize_str(raw, "目标")
    if is_tcp_style_target(s):
        return [InputValidator.validate_target(s)]
    if "/" in s:
        return expand_ipv4_cidr(s, max_expand)
    m = _RE_IPV4_LAST_OCTET_RANGE.match(s)
    if m:
        return expand_last_octet_range(s, max_expand)
    return [InputValidator.validate_host(s)]


def ordered_dedupe(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def partition_for_workers(items: List[str], max_workers: int) -> List[List[str]]:
    """轮询分片，使各分片大小至多相差 1。"""
    if not items:
        return []
    w = max(1, min(max_workers, len(items)))
    buckets: List[List[str]] = [[] for _ in range(w)]
    for i, it in enumerate(items):
        buckets[i % w].append(it)
    return [b for b in buckets if b]


def _probe_path_writable(path: Path, is_dir: bool) -> bool:
    """目录则探测该目录；文件则探测其父目录是否可创建并写入临时文件。"""
    try:
        if is_dir:
            path.mkdir(parents=True, exist_ok=True)
            parent = path
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            parent = path.parent
        probe = parent / (".netcheck_wrprobe_%d.tmp" % os.getpid())
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def count_valid_builtin_icmp_nodes() -> int:
    """内置 NODE_IPS 中能通过主机校验的数量（与 start_test 中逐项 validate 逻辑一致）。"""
    n = 0
    for ip in NODE_IPS:
        try:
            InputValidator.validate_host(ip)
            n += 1
        except NetcheckValidationError:
            continue
    return n


def icmp_probes_required(args: argparse.Namespace) -> bool:
    """是否与 ICMP/ping 有关（需要 ping 可执行文件可用）。"""
    if args.no_default_nodes:
        return len(args.icmp_cli_targets) > 0
    return count_valid_builtin_icmp_nodes() > 0 or len(args.icmp_cli_targets) > 0


def collect_preflight_issues(args: argparse.Namespace, log: logging.Logger) -> List[str]:
    """
    正式跑与 --dry-run 共用。返回非空即应中止（退出码 1）。
    与 parse_arguments 中的语法/时长上限校验互补：环境、路径可写、ping、默认列表有效性。
    """
    err: List[str] = []

    if not _probe_path_writable(args.report_dir_resolved, True):
        err.append("报告目录不可写或无法创建: %s" % args.report_dir_resolved)

    if args.json_summary:
        jp = Path(args.json_summary)
        if not _probe_path_writable(jp, False):
            err.append("JSON 摘要输出路径父目录不可写或无法创建: %s" % jp.resolve())

    if not args.no_log_file:
        if not _probe_path_writable(Path(LOG_DIR), True):
            err.append("日志目录 NETCHECK_LOG_DIR 不可写或无法创建: %s" % Path(LOG_DIR).resolve())
        if args.log_file:
            lp = Path(args.log_file)
            if lp.is_absolute() and not _probe_path_writable(lp, False):
                err.append("自定义 --log-file 父目录不可写: %s" % lp.parent.resolve())

    builtin_ok = count_valid_builtin_icmp_nodes()
    if not args.no_default_nodes and builtin_ok == 0:
        log.warning(
            "内置 NODE_IPS 共 %d 项全部未通过主机校验，ICMP 将仅使用命令行展开结果",
            len(NODE_IPS),
        )

    if not args.no_default_nodes and builtin_ok == 0 and not args.icmp_cli_targets and not args.tcp_cli_targets:
        err.append(
            "未提供任何有效探测目标：内置 NODE_IPS 全部无效，且命令行无 ICMP/TCP；"
            "请修正 NODE_IPS 或使用 --no-default-nodes 并指定目标"
        )

    if icmp_probes_required(args) and shutil.which("ping") is None:
        err.append(
            "当前 PATH 中未找到 ping 可执行文件，无法执行 ICMP 探测；"
            "若仅需 TCP 请使用 --no-default-nodes 且不传 ICMP/段/CIDR 目标"
        )

    return err


# ---------------------------------------------------------------------------
# Ping
# ---------------------------------------------------------------------------
def _ping_command(host: str) -> List[str]:
    system = platform.system()
    if system == "Windows":
        return [
            "ping",
            "-n",
            str(PING_COUNT),
            "-w",
            str(int(PING_TIMEOUT * 1000)),
            host,
        ]
    if system == "Darwin":
        return [
            "ping",
            "-c",
            str(PING_COUNT),
            "-W",
            str(int(PING_TIMEOUT * 1000)),
            host,
        ]
    return [
        "ping",
        "-c",
        str(PING_COUNT),
        "-W",
        str(PING_TIMEOUT),
        "-q",
        host,
    ]


def _parse_ping_output(text: str) -> Tuple[float, List[float]]:
    if not text:
        return 100.0, []

    packet_loss = 100.0
    m = re.search(r"(\d+)% packet loss", text)
    if m:
        packet_loss = float(m.group(1))
    if packet_loss >= 100 and re.search(r"packet loss", text) is None:
        m = re.search(r"\((\d+)%\s*loss\)", text, re.I)
        if m:
            packet_loss = float(m.group(1))
    if packet_loss >= 100:
        m = re.search(r"\((\d+)%\s*丢失\)", text)
        if m:
            packet_loss = float(m.group(1))

    rtts: List[float] = []
    rtt_m = re.search(
        r"rtt min/avg/max/(?:mdev|stddev) = ([\d.]+)/([\d.]+)/([\d.]+)/[\d.]+ ms",
        text,
    )
    if rtt_m:
        rtts = [float(rtt_m.group(1)), float(rtt_m.group(2)), float(rtt_m.group(3))]
        return packet_loss, rtts

    rtt_m = re.search(
        r"Minimum\s*=\s*(\d+)\s*ms\s*,\s*Maximum\s*=\s*(\d+)\s*ms\s*,\s*Average\s*=\s*(\d+)\s*ms",
        text,
        re.I,
    )
    if rtt_m:
        mn, mx, avg = float(rtt_m.group(1)), float(rtt_m.group(2)), float(rtt_m.group(3))
        return packet_loss, [mn, avg, mx]

    rtt_m = re.search(
        r"最短\s*=\s*(\d+)\s*ms\s*[，,]\s*最长\s*=\s*(\d+)\s*ms\s*[，,]\s*平均\s*=\s*(\d+)\s*ms",
        text,
    )
    if rtt_m:
        mn, mx, avg = float(rtt_m.group(1)), float(rtt_m.group(2)), float(rtt_m.group(3))
        return packet_loss, [mn, avg, mx]

    return packet_loss, rtts


def run_ping(host: str) -> str:
    InputValidator.validate_host(host)
    cmd = _ping_command(host)
    timeout = max(PING_TIMEOUT * PING_COUNT + 5, PING_COUNT * 3)
    logger.debug("执行 ping: %s", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=True,
            errors="replace",
        )
        out = (proc.stdout or "").strip()
        if not out and proc.stderr:
            out = (proc.stderr or "").strip()
        return out
    except subprocess.TimeoutExpired:
        logger.debug("ping 超时: %s", host)
        return ""
    except OSError as e:
        logger.debug("ping 执行失败: %s", e)
        return ""
    except Exception as e:
        logger.debug("ping 异常: %s", e)
        return ""


def test_tcp_port(target: str) -> Tuple[bool, float]:
    if ":" not in target:
        raise ValueError("TCP 目标必须为 host:port")
    host, port_str = target.split(":", 1)
    host = InputValidator.validate_host(host)
    try:
        port = int(port_str)
    except ValueError:
        return False, 0.0
    if not MIN_PORT <= port <= MAX_PORT:
        return False, 0.0

    start = time.time()
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT):
            return True, float((time.time() - start) * 1000.0)
    except OSError:
        return False, 0.0
    except Exception:
        return False, 0.0


# ---------------------------------------------------------------------------
# 探测引擎
# ---------------------------------------------------------------------------
class NetworkTester:
    def __init__(self) -> None:
        self.results: DefaultDict[str, List[Dict[str, Any]]] = defaultdict(list)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self._shutdown = threading.Event()

    def request_shutdown(self) -> None:
        """供信号处理与 KeyboardInterrupt 调用，触发工作线程与主循环退出。"""
        self.stop_event.set()
        self._shutdown.set()

    def ping_worker_slice(self, hosts: List[str], slice_id: int = 0) -> None:
        """一线程负责多个 IP 顺序轮询（分片），控制线程与 ping 子进程总数，避免大规模扫网时拖垮本机。"""
        valid: List[str] = []
        for h in hosts:
            try:
                valid.append(InputValidator.validate_host(h))
            except NetcheckValidationError as e:
                logger.warning("无效主机 %s: %s", h, e)
        if not valid:
            return

        while not self.stop_event.is_set():
            try:
                t_round = time.time()
                for host in valid:
                    if self.stop_event.is_set():
                        break
                    text = run_ping(host)
                    loss, rtt_list = _parse_ping_output(text)
                    ts = datetime.now().isoformat()

                    with self.lock:
                        if loss >= 100 or len(rtt_list) < 3:
                            self.results[host].append(
                                {
                                    "loss": 100.0,
                                    "min": None,
                                    "avg": None,
                                    "max": None,
                                    "type": "icmp",
                                    "timestamp": ts,
                                }
                            )
                            if loss >= 100:
                                logger.info("%s: ICMP 超时/失败", host)
                            else:
                                logger.info("%s: ICMP 输出无法解析 RTT（丢包=%.1f%%）", host, loss)
                        else:
                            mn, avg, mx = rtt_list[0], rtt_list[1], rtt_list[2]
                            self.results[host].append(
                                {
                                    "loss": float(loss),
                                    "min": mn,
                                    "avg": avg,
                                    "max": mx,
                                    "type": "icmp",
                                    "timestamp": ts,
                                }
                            )
                            logger.info(
                                "%s: ICMP 延迟 avg=%.2fms (min=%.2f max=%.2f) 丢包=%.1f%%",
                                host,
                                avg,
                                mn,
                                mx,
                                loss,
                            )

                elapsed = time.time() - t_round
                time.sleep(max(0.0, float(PING_INTERVAL) - elapsed))
            except Exception as e:
                logger.warning(
                    "ping_worker_slice #%d 异常: %s",
                    slice_id,
                    e,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                time.sleep(PING_INTERVAL)

    def tcp_worker(self, target: str) -> None:
        while not self.stop_event.is_set():
            try:
                t0 = time.time()
                ok, latency = test_tcp_port(target)
                ts = datetime.now().isoformat()

                with self.lock:
                    self.results[target].append(
                        {
                            "success": ok,
                            "latency": latency,
                            "type": "tcp",
                            "timestamp": ts,
                        }
                    )
                    if ok:
                        logger.info("%s: TCP 成功 延迟=%.2fms", target, latency)
                    else:
                        logger.info("%s: TCP 失败", target)

                elapsed = time.time() - t0
                time.sleep(max(0.0, float(PING_INTERVAL) - elapsed))
            except Exception as e:
                logger.warning(
                    "tcp_worker 异常 target=%s: %s",
                    target,
                    e,
                    exc_info=logger.isEnabledFor(logging.DEBUG),
                )
                time.sleep(PING_INTERVAL)

    def start_test(
        self,
        duration: int,
        icmp_cli: Optional[List[str]] = None,
        tcp_cli: Optional[List[str]] = None,
        no_default_nodes: bool = False,
        max_icmp_workers: int = DEFAULT_MAX_ICMP_WORKERS,
    ) -> DefaultDict[str, List[Dict[str, Any]]]:
        icmp_cli = icmp_cli or []
        tcp_cli = tcp_cli or []
        start_time = time.time()
        end_time = start_time + float(duration)
        threads: List[threading.Thread] = []

        logger.info("=" * 80)
        logger.info(
            "网络连接稳定性测试 version=%s python=%s platform=%s",
            __version__,
            platform.python_version(),
            platform.platform(),
        )
        logger.info("=" * 80)
        logger.info("测试时长: %d 秒 (%.1f 小时)", duration, duration / 3600.0)
        logger.info(
            "预计结束: %s",
            datetime.fromtimestamp(end_time).strftime("%Y-%m-%d %H:%M:%S"),
        )

        all_icmp: List[str] = []
        if not no_default_nodes:
            logger.info("默认节点数: %d", len(NODE_IPS))
            if len(NODE_IPS) <= 10:
                logger.info("默认节点: %s", ", ".join(NODE_IPS))
            else:
                logger.info("默认节点(节选): %s ... 共 %d 个", ", ".join(NODE_IPS[:10]), len(NODE_IPS))
            for ip in NODE_IPS:
                try:
                    all_icmp.append(InputValidator.validate_host(ip))
                except NetcheckValidationError as e:
                    logger.warning("跳过无效默认节点 %s: %s", ip, e)

        all_icmp.extend(icmp_cli)
        all_icmp = ordered_dedupe(all_icmp)

        if icmp_cli:
            logger.info("命令行展开 ICMP 目标数: %d（已与默认列表去重合并）", len(icmp_cli))
        if tcp_cli:
            logger.info("TCP 目标 (%d): %s", len(tcp_cli), ", ".join(tcp_cli[:20]) + (" ..." if len(tcp_cli) > 20 else ""))

        if not all_icmp and not tcp_cli:
            logger.info("ICMP: 无；TCP: 无")
        else:
            logger.info(
                "ICMP 总目标: %d | ICMP 工作线程上限: %d | TCP 目标: %d",
                len(all_icmp),
                max(1, min(max_icmp_workers, max(1, len(all_icmp)))),
                len(tcp_cli),
            )

        logger.info("间隔 %ds | Ping 超时 %ds | TCP 超时 %ds", PING_INTERVAL, PING_TIMEOUT, TCP_TIMEOUT)
        logger.info("说明: 多 IP 时分片轮询，单 IP 两次探测间隔≈（本分片一轮耗时）+ %ds", PING_INTERVAL)
        logger.info("开始测试 (Ctrl+C 或 SIGTERM 可优雅结束)")
        logger.info("=" * 80)

        chunks = partition_for_workers(all_icmp, max_icmp_workers)
        for idx, chunk in enumerate(chunks):
            t = threading.Thread(
                target=self.ping_worker_slice,
                args=(chunk, idx),
                daemon=True,
                name="netcheck-ping-chunk-%d" % idx,
            )
            t.start()
            threads.append(t)

        for target in tcp_cli:
            try:
                InputValidator.validate_target(target)
                t = threading.Thread(
                    target=self.tcp_worker,
                    args=(target,),
                    daemon=True,
                    name="netcheck-tcp-%s" % target.replace(":", "_"),
                )
                t.start()
                threads.append(t)
            except NetcheckValidationError as e:
                logger.warning("跳过无效 TCP 目标 %s: %s", target, e)

        if not threads:
            logger.error("没有任何有效测试目标，退出。")
            return self.results

        try:
            while time.time() < end_time and not self.stop_event.is_set():
                time.sleep(0.25)
        except KeyboardInterrupt:
            logger.info("收到 KeyboardInterrupt，正在停止工作线程…")
            self.request_shutdown()

        if not self._shutdown.is_set():
            self.stop_event.set()

        for t in threads:
            t.join(timeout=5.0)
            if t.is_alive():
                logger.warning("工作线程未及时退出: %s", t.name)

        return self.results


# ---------------------------------------------------------------------------
# 报告与 SLO
# ---------------------------------------------------------------------------
@dataclass
class QualitySummary:
    """汇总结论，供退出码与 JSON 输出使用。"""

    overall_ok: bool
    target_count: int
    failed_targets: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": __version__,
                "overall_ok": self.overall_ok,
                "target_count": self.target_count,
                "failed_targets": self.failed_targets,
                "notes": self.notes,
                "generated_at": datetime.now().isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        )


def evaluate_quality(results: DefaultDict[str, List[Dict[str, Any]]]) -> QualitySummary:
    if not results:
        return QualitySummary(
            overall_ok=False,
            target_count=0,
            failed_targets=[],
            notes=["无测试数据"],
        )

    failed: List[str] = []
    notes: List[str] = []

    for target in sorted(results.keys()):
        data = results[target]
        if not data:
            failed.append(target)
            notes.append("%s: 无数据" % target)
            continue

        ttype = data[0]["type"]
        if ttype == "icmp":
            total = len(data)
            bad = sum(1 for d in data if d.get("avg") is None or d.get("loss", 100) >= 100)
            bad_pct = (float(bad) / float(total)) * 100.0 if total else 100.0
            ok_rows = [d for d in data if d.get("loss", 100) < 100 and d.get("avg") is not None]

            if bad_pct > ICMP_FAIL_SAMPLE_RATIO_MAX_PCT:
                failed.append(target)
                notes.append(
                    "%s: ICMP 失败/无统计占比 %.1f%% (阈值 %.1f%%)"
                    % (target, bad_pct, ICMP_FAIL_SAMPLE_RATIO_MAX_PCT)
                )
                continue
            if ok_rows:
                avg_lat = sum(float(d["avg"]) for d in ok_rows) / len(ok_rows)
                jitter = max(float(d["max"]) for d in ok_rows) - min(float(d["min"]) for d in ok_rows)
                if avg_lat > ICMP_AVG_LATENCY_MS_WARN:
                    failed.append(target)
                    notes.append("%s: ICMP 平均延迟 %.2fms (阈值 %.0fms)" % (target, avg_lat, ICMP_AVG_LATENCY_MS_WARN))
                    continue
                if jitter > ICMP_JITTER_MS_WARN:
                    failed.append(target)
                    notes.append("%s: ICMP 抖动 %.2fms (阈值 %.0fms)" % (target, jitter, ICMP_JITTER_MS_WARN))
                    continue

        elif ttype == "tcp":
            total = len(data)
            succ = sum(1 for d in data if d["success"])
            succ_pct = (float(succ) / float(total)) * 100.0 if total else 0.0
            ok_rows = [d for d in data if d["success"]]

            if succ_pct < TCP_SUCCESS_RATIO_MIN_PCT:
                failed.append(target)
                notes.append(
                    "%s: TCP 成功率 %.1f%% (阈值 ≥%.1f%%)"
                    % (target, succ_pct, TCP_SUCCESS_RATIO_MIN_PCT)
                )
                continue
            if ok_rows:
                avg_lat = sum(float(d["latency"]) for d in ok_rows) / len(ok_rows)
                if avg_lat > TCP_AVG_LATENCY_MS_WARN:
                    failed.append(target)
                    notes.append("%s: TCP 平均延迟 %.2fms (阈值 %.0fms)" % (target, avg_lat, TCP_AVG_LATENCY_MS_WARN))
                    continue

    return QualitySummary(
        overall_ok=len(failed) == 0,
        target_count=len(results),
        failed_targets=failed,
        notes=notes,
    )


def generate_report(results: DefaultDict[str, List[Dict[str, Any]]]) -> str:
    if not results:
        return "=" * 80 + "\n测试报告\n" + "=" * 80 + "\n\n无测试数据\n"

    lines: List[str] = []
    lines.extend(
        [
            "=" * 80,
            "网络连接稳定性测试报告",
            "工具版本: %s" % __version__,
            "生成时间: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "=" * 80,
            "",
        ]
    )

    for target in sorted(results.keys()):
        data = results[target]
        if not data:
            continue

        test_type = data[0]["type"]

        if test_type == "icmp":
            total_tests = len(data)
            bad = sum(1 for d in data if d.get("avg") is None or d.get("loss", 100) >= 100)
            timeout_percent = (float(bad) / float(total_tests)) * 100.0 if total_tests else 0.0

            ok_rows = [d for d in data if d.get("loss", 100) < 100 and d.get("avg") is not None]
            if ok_rows:
                avg_latency = sum(float(d["avg"]) for d in ok_rows) / len(ok_rows)
                min_latency = min(float(d["min"]) for d in ok_rows)
                max_latency = max(float(d["max"]) for d in ok_rows)
                jitter = max_latency - min_latency
            else:
                avg_latency = min_latency = max_latency = jitter = 0.0

            lines.append("节点: %s (ICMP)" % target)
            lines.append("  总次数: %d" % total_tests)
            lines.append("  失败/无统计次数: %d (%.1f%%)" % (bad, timeout_percent))
            if ok_rows:
                lines.append("  平均延迟: %.2f ms" % avg_latency)
                lines.append("  最小延迟: %.2f ms" % min_latency)
                lines.append("  最大延迟: %.2f ms" % max_latency)
                lines.append("  抖动: %.2f ms" % jitter)
            else:
                lines.append("  无成功样本")

            status_ok = True
            if timeout_percent > ICMP_FAIL_SAMPLE_RATIO_MAX_PCT:
                lines.append(
                    "  [警告] 失败/无统计占比过高 (>%.1f%%)" % ICMP_FAIL_SAMPLE_RATIO_MAX_PCT
                )
                status_ok = False
            elif ok_rows and avg_latency > ICMP_AVG_LATENCY_MS_WARN:
                lines.append("  [警告] 平均延迟过高 (>%g ms)" % ICMP_AVG_LATENCY_MS_WARN)
                status_ok = False
            elif ok_rows and jitter > ICMP_JITTER_MS_WARN:
                lines.append("  [警告] 抖动过大 (>%g ms)" % ICMP_JITTER_MS_WARN)
                status_ok = False
            if status_ok and ok_rows:
                lines.append("  [OK] 连接质量良好")

        elif test_type == "tcp":
            total_tests = len(data)
            success_count = sum(1 for d in data if d["success"])
            success_percent = (float(success_count) / float(total_tests)) * 100.0 if total_tests else 0.0

            ok_rows = [d for d in data if d["success"]]
            if ok_rows:
                avg_latency = sum(float(d["latency"]) for d in ok_rows) / len(ok_rows)
                min_latency = min(float(d["latency"]) for d in ok_rows)
                max_latency = max(float(d["latency"]) for d in ok_rows)
                jitter = max_latency - min_latency
            else:
                avg_latency = min_latency = max_latency = jitter = 0.0

            lines.append("目标: %s (TCP)" % target)
            lines.append("  总次数: %d" % total_tests)
            lines.append("  成功率: %.1f%%" % success_percent)
            if ok_rows:
                lines.append("  平均延迟: %.2f ms" % avg_latency)
                lines.append("  最小延迟: %.2f ms" % min_latency)
                lines.append("  最大延迟: %.2f ms" % max_latency)
                lines.append("  抖动: %.2f ms" % jitter)
            else:
                lines.append("  无成功连接")

            status_ok = True
            if success_percent < TCP_SUCCESS_RATIO_MIN_PCT:
                lines.append("  [警告] 成功率过低 (<%g%%)" % TCP_SUCCESS_RATIO_MIN_PCT)
                status_ok = False
            elif ok_rows and avg_latency > TCP_AVG_LATENCY_MS_WARN:
                lines.append("  [警告] 延迟过高 (>%g ms)" % TCP_AVG_LATENCY_MS_WARN)
                status_ok = False
            if status_ok and ok_rows:
                lines.append("  [OK] 连接质量良好")

        lines.append("-" * 60)
        lines.append("")

    summary = evaluate_quality(results)
    lines.extend(
        [
            "=" * 80,
            "SLO 汇总",
            "=" * 80,
            "目标数: %d" % summary.target_count,
            "SLO 是否通过: %s" % ("是" if summary.overall_ok else "否"),
        ]
    )
    if summary.notes:
        lines.append("说明:")
        for n in summary.notes:
            lines.append("  - %s" % n)
    lines.append("=" * 80)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _configure_stdio_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf:
            try:
                reconf(encoding="utf-8", errors="replace")
            except (OSError, ValueError, AttributeError):
                pass


def _install_signal_handlers(request_shutdown: Callable[[], None]) -> None:
    def _handler(signum: int, frame: Any) -> None:
        sig_name = getattr(signal, "Signals", None)
        name = str(signum)
        if sig_name is not None:
            try:
                name = signal.Signals(signum).name
            except (ValueError, AttributeError):
                pass
        logging.getLogger("netcheck").info("收到信号 %s，准备停止…", name)
        request_shutdown()

    signal.signal(signal.SIGINT, _handler)
    if hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, OSError):
            pass


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "网络连接稳定性测试（ICMP / TCP）。参数解析阶段做语法与展开上限校验；"
            "启动前与 --dry-run 共用前置检查（路径可写、ping、内置节点等）。"
            "默认滚动日志目录见 NETCHECK_LOG_DIR。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s
  %(prog)s -d 3600
  %(prog)s --no-default-nodes 192.168.125.29-87
  %(prog)s --no-default-nodes 192.168.125.0/28
  %(prog)s --dry-run --no-default-nodes 10.0.0.1
  %(prog)s --json-summary summary.json 192.168.1.10:443

展开与并发:
  --max-expand              命令行 ICMP 展开去重后总个数上限（不含内置 NODE_IPS）
  --max-icmp-workers        ICMP 分片工作线程上限
  NETCHECK_MAX_EXPAND       覆盖默认展开上限（非法整型则忽略，用 1024）
  NETCHECK_MAX_ICMP_WORKERS 覆盖默认线程上限（非法整型则忽略，用 64）

前置校验（与 --dry-run 一致）:
  报告目录 / 日志目录 / --json-summary 父目录可写；需 ICMP 时 PATH 需有 ping；
  内置 NODE_IPS 全无效且无命令行目标时报错

环境变量:
  NETCHECK_LOG_DIR             日志目录（默认 ./logs）
  NETCHECK_MAX_DURATION_SEC    单次运行最大秒数（默认约 30 天）

目标语法（与实现一致）:
  ICMP: IPv4 / 域名 / IPv6；IPv4 末段区间 a.b.c.d-e；IPv4 CIDR（hosts 展开）
  TCP:  仅 host:port（单一冒号 + 数字端口），不支持 IPv6:port 与段展开

退出码:
  0  正常且 SLO 通过
  1  参数错误 / 失败 / 用户中断 / 无目标
  2  测试跑完但 SLO 未通过（适合 CI）
        """,
    )
    parser.add_argument("--version", action="version", version="netcheck %s" % __version__)
    parser.add_argument(
        "-d", "--duration", type=int, default=DEFAULT_TEST_DURATION,
        help="测试时长（秒），默认 %(default)s",
    )
    parser.add_argument(
        "--no-default-nodes", action="store_true",
        help="不测试内置 NODE_IPS，仅测命令行目标",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="DEBUG 日志",
    )
    parser.add_argument(
        "--no-log-file", action="store_true",
        help="仅控制台输出，不写滚动日志文件",
    )
    parser.add_argument(
        "--log-file", type=str, default=None,
        help="日志文件名或绝对路径；相对路径只取文件名并落在 NETCHECK_LOG_DIR 下",
    )
    parser.add_argument(
        "--report-dir", type=str, default=".",
        help="测试报告 txt 输出目录（默认当前目录）",
    )
    parser.add_argument(
        "--no-report-file", action="store_true",
        help="不写入 txt 报告（仍打印到 stdout）",
    )
    parser.add_argument(
        "--json-summary", type=str, default=None, metavar="FILE",
        help="写入 JSON 摘要（SLO 结果），便于监控/流水线采集",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="校验目标语法、展开上限及前置条件（路径可写、ping 等），打印计划后不探测",
    )
    parser.add_argument(
        "--max-expand",
        type=int,
        default=DEFAULT_MAX_EXPAND,
        metavar="N",
        help="命令行 ICMP 展开去重后地址总数上限（不含内置节点），默认 %(default)s",
    )
    parser.add_argument(
        "--max-icmp-workers",
        type=int,
        default=DEFAULT_MAX_ICMP_WORKERS,
        metavar="N",
        help="ICMP 分片工作线程上限，默认 %(default)s",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="额外目标: IP / IPv4末段区间 / IPv4 CIDR / 域名 / host:port(TCP)",
    )

    args = parser.parse_args()

    if args.duration <= 0:
        parser.error("测试时长必须大于 0")
    if args.duration > MAX_TEST_DURATION_SEC:
        parser.error(
            "测试时长 %d 秒超过上限 %d（环境变量 NETCHECK_MAX_DURATION_SEC 可调）"
            % (args.duration, MAX_TEST_DURATION_SEC)
        )
    if args.max_expand < 1:
        parser.error("--max-expand 至少为 1")
    if args.max_icmp_workers < 1:
        parser.error("--max-icmp-workers 至少为 1")

    raw_flat: List[str] = []
    for t in args.targets:
        try:
            raw_flat.extend(expand_cli_target(t, args.max_expand))
        except NetcheckValidationError as e:
            parser.error("目标 %r: %s" % (t, e))

    icmp_cli = ordered_dedupe([x for x in raw_flat if not is_tcp_style_target(x)])
    tcp_cli = ordered_dedupe([x for x in raw_flat if is_tcp_style_target(x)])

    if len(icmp_cli) > args.max_expand:
        parser.error(
            "命令行 ICMP 展开去重后共 %d 个，超过 --max-expand=%d；请缩小范围或调大上限"
            % (len(icmp_cli), args.max_expand)
        )

    try:
        args.report_dir_resolved = InputValidator.validate_report_dir(args.report_dir)
    except NetcheckValidationError as e:
        parser.error("无效报告目录: %s" % e)

    args.icmp_cli_targets = icmp_cli
    args.tcp_cli_targets = tcp_cli
    return args


def main() -> int:
    _configure_stdio_utf8()
    args = parse_arguments()

    setup_logging(
        verbose=args.verbose,
        log_file=args.log_file,
        no_log_file=args.no_log_file,
    )

    if args.no_default_nodes and not args.icmp_cli_targets and not args.tcp_cli_targets:
        logger.error("使用 --no-default-nodes 时必须至少指定一个目标。运行: %s --help", Path(sys.argv[0]).name)
        return 1

    pre_issues = collect_preflight_issues(args, logger)
    if pre_issues:
        for msg in pre_issues:
            logger.error("[preflight] %s", msg)
        return 1

    if args.dry_run:
        logger.info(
            "[dry-run] 语法与前置校验均已通过 duration=%ss no_default_nodes=%s icmp_cli=%d tcp_cli=%d "
            "max_expand=%d max_icmp_workers=%d max_duration_cap=%d",
            args.duration,
            args.no_default_nodes,
            len(args.icmp_cli_targets),
            len(args.tcp_cli_targets),
            args.max_expand,
            args.max_icmp_workers,
            MAX_TEST_DURATION_SEC,
        )
        return 0

    tester = NetworkTester()
    _install_signal_handlers(tester.request_shutdown)

    tester.start_test(
        args.duration,
        args.icmp_cli_targets,
        args.tcp_cli_targets,
        args.no_default_nodes,
        args.max_icmp_workers,
    )

    report = generate_report(tester.results)
    print("\n" + report)

    summary = evaluate_quality(tester.results)
    if args.json_summary:
        try:
            Path(args.json_summary).write_text(summary.to_json(), encoding="utf-8")
            logger.info("JSON 摘要已写入: %s", Path(args.json_summary).resolve())
        except OSError as e:
            logger.warning("无法写入 JSON 摘要: %s", e)

    if not args.no_report_file:
        filename = args.report_dir_resolved / (
            "network_test_report_%s.txt" % datetime.now().strftime("%Y%m%d_%H%M%S")
        )
        try:
            filename.parent.mkdir(parents=True, exist_ok=True)
            filename.write_text(report, encoding="utf-8")
            logger.info("报告已保存: %s", filename.resolve())
            print("=" * 80)
            print("报告已保存: %s" % filename.resolve())
            print("=" * 80)
        except OSError as e:
            logger.warning("无法写入报告文件: %s", e)

    if not tester.results:
        return 1
    return 0 if summary.overall_ok else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        logging.getLogger("netcheck").info("操作被用户中断")
        raise SystemExit(1)
    except SystemExit:
        raise
    except Exception as err:
        logging.getLogger("netcheck").error("运行失败: %s", err, exc_info=True)
        raise SystemExit(1)

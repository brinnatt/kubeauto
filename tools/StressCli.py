#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
StressCli：K8s 工作节点上按本地时间窗调度 stress-ng（多进程包装）。

说明与行为以 STRESSCLI_PARSER_DESCRIPTION、show_examples()、下方「上游 stress-ng 约定」常量为权威；
实现入口见 main()、_run_scheduler_loop()、_build_stress_ng_*_cmd()。

日志：默认 /var/log/res.log（滚动，见 init_stresscli_logging）；主进程与 worker 经 QueueListener 汇总，
便于全程追踪正常运行、停窗、信号与 stress-ng 异常退出。

退出码：0 成功；1 一般错误；2 参数错误。
"""

import argparse
import atexit
import logging
import multiprocessing
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
from datetime import datetime, time as time_of_day
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from typing import Any, List, Optional, Tuple

# 进程退出码（与文件头一致）
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BAD_ARGS = 2

# 日志（与 StarCli 思路一致：文件滚动 + 可选 stdout；多进程经 Queue 汇总）
LOGGER_NAME = "stresscli"
DEFAULT_LOG_FILE = os.getenv("STRESSCLI_LOG_FILE", "/var/log/res.log")
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5

# -----------------------------------------------------------------------------
# 上游 stress-ng（stress-ng.1）与本脚本对齐的约定（修改命令行时须核对手册对应条目）
# -----------------------------------------------------------------------------
# --timeout 0     手册：未指定时默认 24h；0 表示不因超时而结束（长驻必选）。
# --quiet         手册：不输出；无人值守场景推荐（本进程仍对 stdout/stderr 做 DEVNULL）。
# --no-oom-adjust 手册：不修改 OOM score，保持系统默认；默认行为会主动调分以「制造更大内存压力」，
#                   在与其他工作负载共节点时易干扰 OOM 行为，故默认关闭其调整。
# 子进程会话：Popen(..., start_new_session=True) 等价 setsid，便于向进程组发信号停止（见 subprocess 文档）。

STRESS_NG_GLOBAL_OPTS = (
    "--timeout",
    "0",
    "--quiet",
    "--no-oom-adjust",
)


def _quote_cmd(cmd: List[str]) -> str:
    return " ".join(shlex.quote(c) for c in cmd)


def _ensure_log_file_path(path: str) -> Tuple[str, bool]:
    """
    尝试使用 path；若无法创建（如无写 /var/log 权限）则退回到 ./logs/res.log。
    返回 (实际路径, 是否发生了回退)。
    """
    directory = os.path.dirname(os.path.abspath(path))
    try:
        os.makedirs(directory, mode=0o755, exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        return path, False
    except OSError as e:
        fallback = os.path.join(os.getcwd(), "logs", "res.log")
        try:
            os.makedirs(os.path.dirname(os.path.abspath(fallback)), mode=0o755, exist_ok=True)
            with open(fallback, "a", encoding="utf-8"):
                pass
        except OSError as e2:
            raise SystemExit(
                "无法创建日志文件（已尝试 %s 与 %s）：首选错误 %s；回退错误 %s"
                % (path, fallback, e, e2)
            ) from e2
        return fallback, True


def _parse_log_level(s: str) -> int:
    m = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
    }
    k = (s or "").strip().upper()
    if k not in m:
        raise ValueError("log-level 须为 DEBUG/INFO/WARNING/ERROR")
    return m[k]


def init_stresscli_logging(
    log_file: str,
    level: int,
    log_to_stdout: bool,
) -> Tuple[QueueListener, Any]:
    """
    主进程与 worker 共用同一 multiprocessing.Queue + QueueListener，
    保证多进程写同一日志文件时由 listener 单线程落盘（logging 推荐做法）。
    """
    log_queue: Any = multiprocessing.Queue(-1)
    formatter = logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT)
    fh = RotatingFileHandler(
        log_file,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setFormatter(formatter)
    fh.setLevel(level)
    handlers: List[logging.Handler] = [fh]
    if log_to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(formatter)
        sh.setLevel(level)
        handlers.append(sh)
    listener = QueueListener(log_queue, *handlers)
    listener.start()
    atexit.register(listener.stop)

    log = logging.getLogger(LOGGER_NAME)
    log.handlers.clear()
    log.setLevel(level)
    log.addHandler(QueueHandler(log_queue))
    log.propagate = False

    return listener, log_queue


def _configure_worker_logger(log_queue: Any, suffix: str) -> logging.Logger:
    """子进程内：仅向 Queue 发记录，由主进程 listener 写盘。"""
    name = "%s.worker.%s" % (LOGGER_NAME, suffix)
    w = logging.getLogger(name)
    w.handlers.clear()
    w.setLevel(logging.DEBUG)
    w.addHandler(QueueHandler(log_queue))
    w.propagate = False
    return w


# -----------------------------------------------------------------------------
# 与 argparse -h 中 description 同步；修改脚本逻辑时请一并更新本节。
# -----------------------------------------------------------------------------

STRESSCLI_PARSER_DESCRIPTION = """\
Linux 专用：在指定本地时间窗内，用 multiprocessing 启动若干子进程，每个子进程内执行一条 stress-ng 命令。

【脚本做什么】
  · 主进程按 --poll-interval 周期醒来，判断「当前本地时间是否在 [ --start , --end )」内。
  · 若在窗内且尚未启动：fork 共 (--cpu-workers + --mem-workers) 个子进程。
  · 每个子进程用 setsid 新建会话启动 stress-ng；停窗或收到 SIGTERM 时向进程组发信号结束 stress-ng。
  · 若不在窗内：确保上述子进程已全部停止。

【与直接跑 stress-ng 的区别】
  · 负载仍由 stress-ng 产生（参数见 show_examples / 下方「stress-ng 与规模」），本脚本只负责时间窗与进程拓扑。
  · 进程表会出现「Python worker + 其子进程 stress-ng」，与手工多开终端等价类。
  · 每条 stress-ng 命令在 stressor 之前统一加全局选项（源码常量 STRESS_NG_GLOBAL_OPTS，与 stress-ng.1 一致）：
      --timeout 0（避免默认 24h 自动结束）、--quiet（无人值守）、--no-oom-adjust（不篡改 OOM 分数，利于与同台业务共存）。

【默认规模（可按节点调整）】
  · CPU：每 worker 一条「--cpu 1 --cpu-load 100」，默认 4 个 worker（约占满 4 逻辑核）。
  · 内存：每 worker 一条「--vm 1 --vm-bytes NG …」，默认 2 个 worker、每 worker 10G（合计约 20G，stress-ng 的 G 后缀语义见其手册）。

【systemd】
  · Type=simple，ExecStart 指向本脚本；KillMode=mixed + TimeoutStopSec 给主进程先发 SIGTERM、再清理 cgroup，便于 Python 侧收掉 stress-ng。
  · 完整片段见 show_examples()。

【日志】
  · 默认写入 /var/log/res.log（滚动，约 10MB×5）；环境变量 STRESSCLI_LOG_FILE 可覆盖路径。
  · 无权限写 /var/log 时自动退回到当前工作目录下 logs/res.log，并在日志中告警。
  · 多进程通过队列汇总写入，主进程与 worker 事件均落同一文件，便于排障。

【如何阅读下方选项】
  分组标题下列出长选项；带默认值的会在 help 字符串中说明。
"""


def show_examples() -> None:
    """接续「python StressCli.py -h」：示例命令与 systemd 片段（须与 STRESSCLI_PARSER_DESCRIPTION 同步维护）。"""
    text = """
========================================================================
分步示例与可复制命令（与上方选项、源码实现一致）
========================================================================

一、先看帮助（含本段）
  python3 StressCli.py -h

二、生产节点：按默认窗 09:00～19:00（19:00 起停止）运行
  python3 /path/to/tools/StressCli.py

三、指定 stress-ng 路径（PATH 中找不到时）
  python3 StressCli.py --stress-ng-binary /usr/bin/stress-ng

四、日志（默认 /var/log/res.log；调试可加控制台与 DEBUG）
  export STRESSCLI_LOG_FILE=/var/log/res.log
  python3 StressCli.py --log-to-stdout --log-level DEBUG

五、调试：忽略时间窗，立即按默认 worker 数跑满（测完请 Ctrl+C）
  python3 StressCli.py --force-run

六、systemd 单元示例（路径请改为本机绝对路径）
  [Unit]
  Description=StressCli time-window stress-ng scheduler
  After=network-online.target
  Wants=network-online.target

  [Service]
  Type=simple
  ExecStart=/usr/bin/python3 /opt/kubeauto/tools/StressCli.py
  Restart=always
  RestartSec=10
  KillMode=mixed
  TimeoutStopSec=90

  [Install]
  WantedBy=multi-user.target

七、每个 worker 内实际执行的 stress-ng（全局前缀与 STRESS_NG_GLOBAL_OPTS 一致；N 来自 --mem-per-worker-gib）
  CPU worker:
    stress-ng --timeout 0 --quiet --no-oom-adjust --cpu 1 --cpu-load 100
  内存 worker:
    stress-ng --timeout 0 --quiet --no-oom-adjust --vm 1 --vm-bytes NG --vm-keep --vm-method all --vm-hang 0

"""
    print(text)


class _StressCliHelpFormatter(argparse.HelpFormatter):
    """略增宽度，便于中文 help 换行。"""

    def __init__(self, prog: str) -> None:
        try:
            w = min(110, max(80, shutil.get_terminal_size(fallback=(100, 24)).columns))
        except OSError:
            w = 100
        super().__init__(prog, max_help_position=28, width=w)


# 子进程入口须为模块级函数，便于 pickle（spawn 场景）


def _cleanup_stress_proc(proc: subprocess.Popen) -> None:
    """向 stress-ng 进程组发 SIGTERM，必要时 SIGKILL。"""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        proc.wait(timeout=10)


def _build_stress_ng_cpu_cmd(stress_ng: str) -> List[str]:
    """与 stress-ng.1 及 STRESS_NG_GLOBAL_OPTS 一致。"""
    return list(
        (stress_ng,)
        + STRESS_NG_GLOBAL_OPTS
        + ("--cpu", "1", "--cpu-load", "100")
    )


def _build_stress_ng_mem_cmd(stress_ng: str, vm_bytes: str) -> List[str]:
    return list(
        (stress_ng,)
        + STRESS_NG_GLOBAL_OPTS
        + (
            "--vm",
            "1",
            "--vm-bytes",
            vm_bytes,
            "--vm-keep",
            "--vm-method",
            "all",
            "--vm-hang",
            "0",
        )
    )


def _run_stress_ng_worker(stop: Any, cmd: List[str], wlog: logging.Logger) -> None:
    """新建会话启动 stress-ng；stop 置位或 stress 异常退出时结束。"""
    wlog.info("执行命令 %s", _quote_cmd(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        wlog.exception("Popen stress-ng 失败: %s", e)
        raise SystemExit(127)

    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = -1
    wlog.info("stress-ng 已启动 pid=%s pgid=%s", proc.pid, pgid)
    try:
        while not stop.is_set():
            rc = proc.poll()
            if rc is not None:
                wlog.error(
                    "stress-ng 在收到停窗信号前已退出 pid=%s rc=%s（可能为 OOM、被 kill、或二进制错误）",
                    proc.pid,
                    rc,
                )
                raise SystemExit(rc if rc != 0 else 1)
            stop.wait(0.25)
    finally:
        wlog.info("开始清理 stress-ng 会话 pid=%s stop_set=%s", proc.pid, stop.is_set())
        _cleanup_stress_proc(proc)
        wlog.info("清理结束 pid=%s poll=%s", proc.pid, proc.poll())


def _cpu_stress_ng_worker(stop: Any, stress_ng: str, log_queue: Any, worker_id: int) -> None:
    wlog = _configure_worker_logger(log_queue, "cpu-%d" % worker_id)
    wlog.info("CPU worker 进程启动 pid=%s", os.getpid())
    cmd = _build_stress_ng_cpu_cmd(stress_ng)
    _run_stress_ng_worker(stop, cmd, wlog)


def _mem_stress_ng_worker(
    stop: Any,
    stress_ng: str,
    vm_bytes: str,
    log_queue: Any,
    worker_id: int,
) -> None:
    wlog = _configure_worker_logger(log_queue, "mem-%d" % worker_id)
    wlog.info("内存 worker 进程启动 pid=%s vm_bytes=%s", os.getpid(), vm_bytes)
    cmd = _build_stress_ng_mem_cmd(stress_ng, vm_bytes)
    _run_stress_ng_worker(stop, cmd, wlog)


def _parse_hhmm(s: str) -> time_of_day:
    parts = s.strip().split(":")
    if len(parts) != 2:
        raise ValueError("时间格式应为 HH:MM")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError("无效时间")
    return time_of_day(hour=h, minute=m)


def _now_in_window(
    now: datetime,
    start: time_of_day,
    end: time_of_day,
) -> bool:
    t = now.time()
    return start <= t < end


def _require_linux() -> None:
    if sys.platform != "linux":
        sys.stderr.write("StressCli 仅支持 Linux（当前 platform=%r）\n" % (sys.platform,))
        raise SystemExit(EXIT_ERROR)


def _resolve_stress_ng(explicit: Optional[str]) -> str:
    if explicit:
        p = os.path.abspath(explicit)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
        raise ValueError("指定的 stress-ng 不可执行或不存在: %s" % explicit)
    found = shutil.which("stress-ng")
    if not found:
        raise ValueError("未在 PATH 中找到 stress-ng，请安装或使用 --stress-ng-binary 指定路径")
    return found


class WorkerBundle(object):
    __slots__ = ("stop", "processes")

    def __init__(self, stop: Any, processes: List[multiprocessing.Process]) -> None:
        self.stop = stop
        self.processes = processes


def _start_workers(
    stress_ng: str,
    cpu_workers: int,
    mem_workers: int,
    vm_bytes: str,
    log_queue: Any,
) -> WorkerBundle:
    log = logging.getLogger(LOGGER_NAME)
    stop = multiprocessing.Event()
    processes: List[multiprocessing.Process] = []

    for i in range(cpu_workers):
        p = multiprocessing.Process(
            target=_cpu_stress_ng_worker,
            args=(stop, stress_ng, log_queue, i),
            name="cpu-worker-%d" % i,
            daemon=False,
        )
        p.start()
        log.info("已 fork CPU worker name=%s subprocess_pid=%s", p.name, p.pid)
        processes.append(p)

    for i in range(mem_workers):
        p = multiprocessing.Process(
            target=_mem_stress_ng_worker,
            args=(stop, stress_ng, vm_bytes, log_queue, i),
            name="mem-worker-%d" % i,
            daemon=False,
        )
        p.start()
        log.info("已 fork 内存 worker name=%s subprocess_pid=%s vm=%s", p.name, p.pid, vm_bytes)
        processes.append(p)

    log.info(
        "全部 worker 已提交启动：共 %s 个进程（CPU %s + 内存 %s）",
        len(processes),
        cpu_workers,
        mem_workers,
    )
    return WorkerBundle(stop=stop, processes=processes)


def _stop_workers(bundle: Optional[WorkerBundle], join_timeout: float = 25.0) -> None:
    log = logging.getLogger(LOGGER_NAME)
    if bundle is None:
        return
    log.info("发送停窗事件：共 %s 个 worker 将收到 Event.set()", len(bundle.processes))
    bundle.stop.set()
    for p in bundle.processes:
        p.join(timeout=join_timeout)
        if p.is_alive():
            log.warning("worker %s 在 %.0fs 内未退出，执行 terminate()", p.name, join_timeout)
            p.terminate()
            p.join(timeout=8.0)
        ec = p.exitcode
        if ec is None:
            log.error("worker %s 结束但 exitcode 仍未知", p.name)
        else:
            st = "正常" if ec == 0 else "异常"
            log.info("worker %s 已结束 exitcode=%s (%s)", p.name, ec, st)
    bundle.processes.clear()
    log.info("worker 列表已清空")


def _run_scheduler_loop(
    *,
    stress_ng: str,
    start: time_of_day,
    end: time_of_day,
    poll_seconds: float,
    cpu_workers: int,
    mem_workers: int,
    vm_bytes: str,
    force_run: bool,
    shutdown: threading.Event,
    log_queue: Any,
) -> None:
    log = logging.getLogger(LOGGER_NAME)
    active: Optional[WorkerBundle] = None
    try:
        while not shutdown.is_set():
            now = datetime.now()
            inside = force_run or _now_in_window(now, start, end)
            n_live = sum(1 for p in active.processes if p.is_alive()) if active else 0

            log.info(
                "poll local_time=%s inside_window=%s force_run=%s "
                "tracked_workers=%s alive=%s",
                now.isoformat(timespec="seconds"),
                inside,
                force_run,
                len(active.processes) if active else 0,
                n_live,
            )
            if active:
                log.debug(
                    "worker 存活明细: %s",
                    [(p.name, p.is_alive(), p.pid, p.exitcode) for p in active.processes],
                )

            if inside and active is None:
                log.info(
                    "进入运行窗口：将启动 stress-ng=%s，CPU worker=%s，内存 worker=%s，每 VM %s",
                    stress_ng,
                    cpu_workers,
                    mem_workers,
                    vm_bytes,
                )
                log.debug("完整 CPU 命令示例: %s", _quote_cmd(_build_stress_ng_cpu_cmd(stress_ng)))
                log.debug("完整内存命令示例: %s", _quote_cmd(_build_stress_ng_mem_cmd(stress_ng, vm_bytes)))
                active = _start_workers(
                    stress_ng, cpu_workers, mem_workers, vm_bytes, log_queue
                )
            elif inside and active is not None:
                if any(not p.is_alive() for p in active.processes):
                    log.error(
                        "检测到 worker 已退出（可能为 stress-ng 失败、OOM 或被信号杀死），整组回收；"
                        "若仍在时间窗内，下一轮 poll 将重新拉起",
                    )
                    _stop_workers(active)
                    active = None
            elif not inside and active is not None:
                log.info("离开运行窗口：停止所有 worker（本地时间已不在 [%s, %s)）", start, end)
                _stop_workers(active)
                active = None

            shutdown.wait(timeout=poll_seconds)
    finally:
        if shutdown.is_set():
            log.warning("调度循环结束：shutdown 已置位，执行最终清理")
        _stop_workers(active)


def _log_startup_banner(
    stress_path: str,
    log_file: str,
    log_level: int,
    log_to_stdout: bool,
    args: argparse.Namespace,
) -> None:
    log = logging.getLogger(LOGGER_NAME)
    log.info(
        "======== StressCli 启动 ======== pid=%s ppid=%s euid=%s egid=%s",
        os.getpid(),
        os.getppid(),
        os.geteuid(),
        os.getegid(),
    )
    log.info("argv: %s", _quote_cmd(sys.argv))
    log.info(
        "配置: log_file=%s log_level=%s (%s) log_to_stdout=%s stress_ng=%s",
        log_file,
        logging.getLevelName(log_level),
        log_level,
        log_to_stdout,
        stress_path,
    )
    log.info(
        "时间窗: start=%s end=%s poll_interval=%.1fs force_run=%s cpu_workers=%s "
        "mem_workers=%s mem_per_worker_gib=%s",
        args.start,
        args.end,
        args.poll_interval,
        args.force_run,
        args.cpu_workers,
        args.mem_workers,
        args.mem_per_worker_gib,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=STRESSCLI_PARSER_DESCRIPTION,
        formatter_class=_StressCliHelpFormatter,
        add_help=False,
    )

    g_help = parser.add_argument_group(
        "帮助",
        "默认执行「时间窗调度」；须先阅读本节与 STRESSCLI_PARSER_DESCRIPTION（脚本做什么、何时启停 worker）。",
    )
    g_help.add_argument(
        "-h",
        "--help",
        action="store_true",
        help="打印本帮助（含全部分组选项），并输出文末「分步示例」可复制命令。",
    )

    g_time = parser.add_argument_group(
        "时间窗与轮询",
        "使用节点本地时区与系统时间；窗为左闭右开 [start, end)，即 end 时刻起不再运行。",
    )
    g_time.add_argument(
        "--start",
        default="09:00",
        metavar="HH:MM",
        help="运行窗口开始，默认 09:00。",
    )
    g_time.add_argument(
        "--end",
        default="19:00",
        metavar="HH:MM",
        help="运行窗口结束（不含该时刻），默认 19:00。",
    )
    g_time.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        metavar="SEC",
        help="检查是否进/出时间窗的周期（秒），默认 30。",
    )

    g_bin = parser.add_argument_group(
        "stress-ng",
        "实际施压由 stress-ng 完成；每条命令在 stressor 前统一附加 STRESS_NG_GLOBAL_OPTS（见源码与 -h 文首）。",
    )
    g_bin.add_argument(
        "--stress-ng-binary",
        default=None,
        metavar="PATH",
        help="stress-ng 可执行文件；默认从 PATH 查找。",
    )

    g_scale = parser.add_argument_group(
        "worker 规模",
        "CPU：每 worker 一条 --cpu 1；内存：每 worker 一条 --vm 1，--vm-bytes 为「整数+G」。",
    )
    g_scale.add_argument(
        "--cpu-workers",
        type=int,
        default=4,
        metavar="N",
        help="CPU worker 个数，默认 4。",
    )
    g_scale.add_argument(
        "--mem-workers",
        type=int,
        default=2,
        metavar="N",
        help="内存 worker 个数，默认 2。",
    )
    g_scale.add_argument(
        "--mem-per-worker-gib",
        type=int,
        default=10,
        metavar="GIB",
        help="每个内存 worker 传给 --vm-bytes 的 GiB 整数，默认 10（即 NG 中的 N）。",
    )

    g_debug = parser.add_argument_group(
        "调试",
        "生产环境勿长期开启 --force-run。",
    )
    g_debug.add_argument(
        "--force-run",
        action="store_true",
        help="忽略时间窗，始终运行 worker（用于联调）。",
    )

    g_log = parser.add_argument_group(
        "日志",
        "默认写入 /var/log/res.log（滚动）；环境变量 STRESSCLI_LOG_FILE 可覆盖默认路径。"
        " 多进程事件经队列汇总，主进程与 worker 同一文件。",
    )
    g_log.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help="日志文件绝对或相对路径；未指定时用 STRESSCLI_LOG_FILE 或 /var/log/res.log。",
    )
    g_log.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="主进程与 worker 日志级别，默认 INFO。",
    )
    g_log.add_argument(
        "--log-to-stdout",
        action="store_true",
        help="除写文件外同时输出到 stdout（便于 systemd journalctl -u 跟随）。",
    )

    args = parser.parse_args(argv)

    if args.help:
        parser.print_help()
        print()
        print("=" * 72)
        print("分步示例与场景命令（可复制；与上方选项对应）")
        print("=" * 72)
        show_examples()
        return EXIT_OK

    _require_linux()

    try:
        start_t = _parse_hhmm(args.start)
        end_t = _parse_hhmm(args.end)
    except ValueError as e:
        sys.stderr.write("参数错误: %s\n" % e)
        return EXIT_BAD_ARGS

    if args.cpu_workers < 1 or args.mem_workers < 1:
        sys.stderr.write("cpu-workers / mem-workers 至少为 1\n")
        return EXIT_BAD_ARGS

    if args.mem_per_worker_gib < 1:
        sys.stderr.write("mem-per-worker-gib 至少为 1\n")
        return EXIT_BAD_ARGS

    vm_bytes = "%dG" % int(args.mem_per_worker_gib)

    try:
        stress_path = _resolve_stress_ng(args.stress_ng_binary)
    except ValueError as e:
        sys.stderr.write("%s\n" % e)
        return EXIT_BAD_ARGS

    try:
        log_level = _parse_log_level(args.log_level)
    except ValueError as e:
        sys.stderr.write("%s\n" % e)
        return EXIT_BAD_ARGS

    log_file_resolved = args.log_file or DEFAULT_LOG_FILE
    try:
        log_path, log_fallback = _ensure_log_file_path(log_file_resolved)
    except SystemExit as e:
        sys.stderr.write("%s\n" % (e.args[0] if e.args else str(e),))
        return EXIT_ERROR

    _listener_keepalive, log_queue = init_stresscli_logging(
        log_path,
        log_level,
        bool(args.log_to_stdout),
    )
    # 引用保留至进程退出，供 atexit 注册的 QueueListener.stop 使用
    _ = _listener_keepalive

    log = logging.getLogger(LOGGER_NAME)
    if log_fallback:
        log.warning(
            "无法写入首选日志路径 %s，已回退到 %s（请检查目录权限或改用 STRESSCLI_LOG_FILE）",
            log_file_resolved,
            log_path,
        )

    _log_startup_banner(stress_path, log_path, log_level, bool(args.log_to_stdout), args)

    shutdown = threading.Event()

    def _handle_signal(signum: int, _frame: object) -> None:
        log.warning("收到 OS 信号 signum=%s，将置位 shutdown 并清理 worker", signum)
        shutdown.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        _run_scheduler_loop(
            stress_ng=stress_path,
            start=start_t,
            end=end_t,
            poll_seconds=float(args.poll_interval),
            cpu_workers=args.cpu_workers,
            mem_workers=args.mem_workers,
            vm_bytes=vm_bytes,
            force_run=bool(args.force_run),
            shutdown=shutdown,
            log_queue=log_queue,
        )
    finally:
        log.info("StressCli 主流程结束 pid=%s", os.getpid())

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

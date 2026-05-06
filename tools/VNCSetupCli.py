#!/usr/bin/env python3
"""
setup_x11vnc.py (Python 3.9+)

目标：
- 面向 Ubuntu/Debian（apt）与 Rocky Linux（dnf/yum）两系，可生产、幂等部署
- 对外命令仅 install / remove / status；systemd 通过内部子命令（见 INTERNAL_SYSTEMD_SUBCMD）在每次服务启动时重新探测 DISPLAY/XAUTHORITY，避免会话切换后固定 :N 导致永久失败
- root用户：install/remove 须 root；systemd 服务固定以 root 运行（不设 User=），仅通过 XAUTHORITY 附着已有 Xorg
- 可重复执行，可回收脚本创建的资源，保持系统干净

幂等、原子性与脏数据防护（与实现一一对应）：
- 关键文件原子写：state.json、systemd unit、rfbauth 均经同目录临时文件 + os.replace 落盘，避免半写可读。
- install 三阶段边界（违反即产生假幂等或误伤运行中服务）：
  1. 阶段一 preflight：仅探测与校验（xdpyinfo、端口、GDM 等）；不写 state.json、不写 unit、不 systemctl disable；失败则直接退出。
  2. 阶段二：包安装、密码、unit；在 systemctl restart 之前 必须 save_state（ExecStart 内 service-run 依赖磁盘上的 state.json，此为契约）；随后 daemon-reload / restart / 端口验收。
  3. 阶段三：成功日志；不再写配置。
- 预检端口：若目标端口已被 x11vnc 监听则放行（幂等 reinstall）；被其他进程占用则失败且不 quiesce。
- 阶段二失败回滚：仅调用 `quiesce_managed_unit_if_ours`（对本脚本托管 unit 执行 disable--now），不自动删除 state.json；避免与磁盘 unit 长期不一致的可由再次成功的 install 覆盖。
- state.json：install 入口以 strict 读取，损坏或非 JSON 时失败，避免静默吞状态。
- remove/status：遇损坏 state 按空字典尽量继续（与 install strict 区分，文档与 argparse epilog 已说明）。

依据与参考（均为可核验的官方或上游入口；非 Wiki 草稿页）：
- Debian 稳定版软件包（apt 目标系）: https://packages.debian.org/stable/x11vnc
- Ubuntu 软件包索引: https://packages.ubuntu.com/x11vnc
- Rocky Linux 9 桌面文档（x11vnc + SSH）: https://docs.rockylinux.org/9/desktop/gnome/x11vnc_plus_ssh_lan/
- 上游项目（手册与发行说明以仓库为准）: https://github.com/LibVNC/x11vnc

日志标记（便于 grep / 排障）：
- [通用]  与发行版无关的步骤
- [Debian/apt]  仅 apt-get 路径
- [RHEL/rpm]    dnf/yum 路径（Rocky 等）
- [GDM]         显示管理 / Wayland vs Xorg 说明与 custom.conf 预检
- [预检]       root 身份、xdpyinfo、端口、会话探测
- [systemd]   unit 与 systemctl
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, cast

SCRIPT_VERSION = "4.5.2"
MANAGED_MARK = "# Managed-By: setup_x11vnc.py"
STATE_DIR_DEFAULT = Path("/var/lib/x11vnc_setup")
STATE_FILE_NAME = "state.json"
STATE_SCHEMA_VERSION = 1
# 默认含 -noshm：root 附着本机其他 Xorg 会话时常见 MIT-SHM BadAccess（journal 中 X_ShmAttach）；与发行版无关。
DEFAULT_X11VNC_FLAGS = "-noxdamage -noscr -noxfixes -noxrecord -xkb -nowf -noshm"

# systemd unit 的 ExecStart 调用的子命令名；在 argparse 帮助中隐藏，不作为对外稳定 API。
INTERNAL_SYSTEMD_SUBCMD = "service-run"


class DeployError(RuntimeError):
    pass


@dataclass
class Config:
    command: str
    vnc_password: str
    vnc_port: int
    display: str
    xauthority_override: str
    x11vnc_pkg: str
    auto_epel: bool
    service_file: Path
    passwd_file: Path
    log_file: Path
    state_dir: Path
    x11vnc_flags: str
    purge_log: bool
    remove_package: bool
    assume_yes: bool
    force_remove_unmanaged: bool

    @property
    def state_file(self) -> Path:
        return self.state_dir / STATE_FILE_NAME

    @property
    def unit_name(self) -> str:
        return self.service_file.name


class Logger:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=True)
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
        print(line, flush=True)
        try:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass

    def tagged(self, tag: str, msg: str) -> None:
        """企业日志：统一 [分类] 前缀，便于检索与交接。"""
        self.log(f"[{tag}] {msg}")


def run_cmd(
    log: Logger,
    cmd: Sequence[str],
    *,
    desc: Optional[str] = None,
    check: bool = True,
    env: Optional[Dict[str, str]] = None,
    capture: bool = False,
) -> subprocess.CompletedProcess:
    if desc:
        log.log(desc)
    with log.path.open("a", encoding="utf-8") as lf:
        if capture:
            cp = subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            lf.write(cp.stdout or "")
        else:
            cp = subprocess.run(cmd, env=env, text=True, stdout=lf, stderr=lf)
    if check and cp.returncode != 0:
        raise DeployError(f"命令失败({cp.returncode}): {' '.join(cmd)}")
    return cp


def require_root() -> None:
    if os.geteuid() != 0:
        raise DeployError("install/remove 须以 root 执行（euid≠0）。")


def require_linux() -> None:
    if sys.platform != "linux":
        raise DeployError("本脚本仅支持 Linux（Ubuntu/Debian 或 Rocky Linux）。")


def require_systemd() -> None:
    if not shutil.which("systemctl"):
        raise DeployError("未检测到 systemctl。")
    if not Path("/run/systemd/system").exists():
        raise DeployError("当前非 systemd 启动环境。")


def detect_rpm_backend() -> str:
    """Rocky 系优先 dnf，仅旧环境回退 yum。"""
    if shutil.which("dnf"):
        return "dnf"
    if shutil.which("yum"):
        return "yum"
    return ""


def detect_pkg_mgr() -> str:
    if shutil.which("apt-get"):
        return "apt-get"
    rpm = detect_rpm_backend()
    if rpm:
        return rpm
    raise DeployError("未识别包管理器：需要 Ubuntu/Debian（apt-get）或 Rocky Linux（dnf/yum）。")


def package_backend_label(pkg_mgr: str) -> str:
    """人类可读的包后端说明（日志与排障一致）。"""
    if pkg_mgr == "apt-get":
        return "Debian 系 / apt-get"
    return f"RHEL 系 / {pkg_mgr}"


def atomic_write_bytes(path: Path, data: bytes, *, mode: int) -> None:
    """同目录临时文件 + fsync + os.replace：读者要么见旧完整内容，要么见新完整内容，无半截脏读。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    closed = False
    try:
        os.write(fd, data)
        os.fsync(fd)
        os.fchmod(fd, mode)
        os.close(fd)
        closed = True
        os.replace(raw, path)
    except OSError:
        if not closed:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            Path(raw).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, *, mode: int) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def load_state(cfg: Config, *, strict: bool = False) -> Dict[str, Any]:
    if not cfg.state_file.exists():
        return {}
    try:
        raw = cfg.state_file.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        if strict and raw.strip():
            raise DeployError(
                f"状态文件损坏或非法 JSON: {cfg.state_file}。请备份后删除该文件再执行 install，避免静默产生不一致。"
            )
        return {}


def save_state(cfg: Config, data: Dict[str, Any]) -> None:
    """整份 state 原子替换；调用方须保证在 restart 前写入，以便 service-run 读到与 unit 一致的参数。"""
    payload = dict(data)
    payload["schema_version"] = STATE_SCHEMA_VERSION
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    atomic_write_text(cfg.state_file, text, mode=0o600)


def remove_state(cfg: Config) -> None:
    try:
        cfg.state_file.unlink(missing_ok=True)
    except OSError:
        pass


def explain_wayland_vs_x11_for_x11vnc(log: Logger) -> None:
    """
    说明为何在 GDM+GNOME 下常须 WaylandEnable=false：x11vnc 依赖完整 Xorg 桌面与 MIT cookie。
    不修改系统文件；对 custom.conf 的硬性校验在预检 `require_gdm_custom_conf_wayland_disabled_if_present` 中完成。
    """
    log.tagged("GDM", "原理：x11vnc 通过 X11 协议（Xlib + MIT-MAGIC-COOKIE）附着到「已有 Xorg 会话」。")
    log.tagged("GDM", "原理：GNOME 在 Wayland 会话下通常没有可供 x11vnc 附着的传统 X11 桌面（或仅有隔离的 Xwayland）。")
    log.tagged(
        "GDM",
        "原理：GDM 在未显式关闭 Wayland 时，默认登录会话走 Wayland；此时没有与 x11vnc 期望一致的完整 Xorg DISPLAY/cookie，"
        "服务常无法稳定监听或出现黑屏。设置 [daemon] WaylandEnable=false 后，GDM 提供 Xorg 会话，才有稳定附着点。",
    )
    log.tagged("GDM", "因此：使用 GDM 时须在 /etc/gdm/custom.conf 的 [daemon] 段设置 WaylandEnable=false。")
    log.tagged(
        "GDM",
        "操作：编辑上述文件后执行 systemctl restart gdm（或重启），再以 root 在本地控制台登录图形会话后执行 install。",
    )
    log.tagged("GDM", "依据：Rocky 官方桌面文档与 Debian/Ubuntu 上 GNOME + GDM 的常见实践（见脚本头部链接）。")
    log.tagged(
        "GDM",
        "说明：若未安装 GDM（无 custom.conf），本项预检跳过；仍须存在可附着的 Xorg（后文 xdpyinfo 验证）。"
        "若使用 lightdm/sddm 等，请在该显示管理器下同样选择 Xorg 会话。",
    )


def parse_wayland_enable_in_gdm_custom_conf(content: str) -> str:
    """
    扫描 [daemon] 段内 WaylandEnable 有效赋值（忽略 # / ; 注释）。
    返回: false | true | unset | no_daemon_section | unknown:<原值>
    """
    in_daemon = False
    seen_daemon = False
    last_val: Optional[str] = None
    for raw in content.splitlines():
        line = raw.split("#", 1)[0].split(";", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            inner = line[1:-1].strip()
            sec = "".join(inner.split()).lower()
            in_daemon = sec == "daemon"
            if in_daemon:
                seen_daemon = True
            continue
        if not in_daemon or "=" not in line:
            continue
        key, val = line.split("=", 1)
        if key.strip().lower() != "waylandenable":
            continue
        last_val = val.strip().strip('"').strip("'")
    if not seen_daemon:
        return "no_daemon_section"
    if last_val is None:
        return "unset"
    lv = last_val.lower()
    if lv in ("false", "0", "no", "off"):
        return "false"
    if lv in ("true", "1", "yes", "on"):
        return "true"
    return f"unknown:{last_val}"


def require_gdm_custom_conf_wayland_disabled_if_present(log: Logger) -> None:
    """
    若存在 /etc/gdm/custom.conf，则强制 [daemon] WaylandEnable=false，与两系 GDM+GNOME 实践一致。
    无该文件时跳过（非 GDM 环境由 xdpyinfo 兜底）。
    """
    path = Path("/etc/gdm/custom.conf")
    if not path.is_file():
        log.tagged(
            "GDM",
            "预检：未找到 /etc/gdm/custom.conf（未使用 GDM 或尚未安装）。跳过 WaylandEnable 文件校验；"
            "若实际使用 GDM，请安装并配置后再执行 install。",
        )
        return
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        raise DeployError(f"无法读取 GDM 配置 {path}: {exc}") from exc
    st = parse_wayland_enable_in_gdm_custom_conf(raw)
    if st == "false":
        log.tagged("GDM", f"预检通过：{path} 中 [daemon] WaylandEnable=false。")
        return
    explain = (
        "使用 GDM 时须关闭 Wayland、强制 Xorg 登录会话，否则 x11vnc 往往无法附着完整桌面。"
        "请在 [daemon] 下设置 WaylandEnable=false，执行 systemctl restart gdm 或重启后再 install。"
        f" 官方依据见脚本头部链接；当前文件: {path}。"
    )
    if st == "true":
        raise DeployError(f"GDM 预检失败：WaylandEnable=true（Wayland 登录）。{explain}")
    if st == "unset":
        raise DeployError(f"GDM 预检失败：[daemon] 中未设置 WaylandEnable=false（缺省仍倾向 Wayland）。{explain}")
    if st == "no_daemon_section":
        raise DeployError(f"GDM 预检失败：{path} 中缺少 [daemon] 段，无法确认 WaylandEnable=false。{explain}")
    raise DeployError(f"GDM 预检失败：WaylandEnable 取值无法识别（{st}）。{explain}")


def validate_display(display: str) -> Path:
    if not display.startswith(":"):
        raise DeployError(f"X11VNC_DISPLAY 格式错误: {display}，应为 :N")
    idx = display[1:]
    if not idx.isdigit():
        raise DeployError(f"X11VNC_DISPLAY 非法: {display}")
    sock = Path(f"/tmp/.X11-unix/X{idx}")
    if not sock.exists():
        raise DeployError(f"未发现 X socket: {sock}")
    return sock


def read_proc_environ(pid: str) -> Dict[str, str]:
    p = Path(f"/proc/{pid}/environ")
    if not p.exists():
        return {}
    out: Dict[str, str] = {}
    for item in p.read_bytes().split(b"\0"):
        if b"=" not in item:
            continue
        k, v = item.split(b"=", 1)
        out[k.decode(errors="ignore")] = v.decode(errors="ignore")
    return out


def loginctl_session_value(sid: str, prop: str) -> str:
    cp = subprocess.run(
        ["loginctl", "show-session", sid, "-p", prop, "--value"],
        text=True,
        capture_output=True,
    )
    return cp.stdout.strip()


def normalize_loginctl_display(raw: str) -> str:
    """loginctl Display 可能为 '-' 或空；非法占位一律视为未知，避免拼出 ':-' 等假 display。"""
    if not raw:
        return ""
    s = raw.strip()
    if s in ("-", "n/a", "N/A", "(none)"):
        return ""
    s = s.lstrip(":")
    if not s.isdigit():
        return ""
    return f":{s}"


def collect_loginctl_sessions() -> List[Dict[str, str]]:
    if not shutil.which("loginctl"):
        return []
    cp = subprocess.run(["loginctl", "list-sessions", "--no-legend"], text=True, capture_output=True)
    sessions: List[Dict[str, str]] = []
    for line in cp.stdout.splitlines():
        cols = line.split()
        if not cols:
            continue
        sid = cols[0]
        disp = normalize_loginctl_display(loginctl_session_value(sid, "Display"))
        sess_type = loginctl_session_value(sid, "Type")
        leader = loginctl_session_value(sid, "Leader")
        uid = loginctl_session_value(sid, "User")
        name = loginctl_session_value(sid, "Name")
        sessions.append(
            {
                "sid": sid,
                "display": disp,
                "type": sess_type.lower(),
                "leader": leader,
                "uid": uid,
                "name": name,
            }
        )
    return sessions


def discover_xauthority(cfg: Config, log: Logger, sessions: Optional[List[Dict[str, str]]] = None) -> str:
    tried: List[str] = []
    if cfg.xauthority_override:
        xa = str(Path(cfg.xauthority_override).expanduser())
        tried.append(xa)
        if not Path(xa).exists() or not os.access(xa, os.R_OK):
            raise DeployError(f"X11VNC_XAUTHORITY 不可读: {xa}")
        log.tagged("预检", f"使用显式 X11VNC_XAUTHORITY={xa}")
        return xa

    want = cfg.display
    sess_list = list(sessions) if sessions is not None else collect_loginctl_sessions()
    for s in sess_list:
        disp = s.get("display", "")
        if not disp or disp != want:
            continue
        if s.get("type") == "wayland":
            continue
        leader = s.get("leader", "")
        envs = read_proc_environ(leader)
        xa = envs.get("XAUTHORITY", "")
        if xa:
            tried.append(xa)
        if xa and Path(xa).exists() and os.access(xa, os.R_OK):
            sid = s.get("sid", "?")
            log.tagged("预检", f"loginctl 会话 {sid} → XAUTHORITY={xa}")
            return xa

    for pattern in (
        "/run/user/*/gdm/Xauthority",
        "/run/user/*/lightdm/xauthority",
        "/var/lib/gdm/.Xauthority",
        "/var/lib/gdm/:0.Xauth",
    ):
        for p in Path("/").glob(pattern.lstrip("/")):
            tried.append(str(p))
            if p.exists() and os.access(p, os.R_OK):
                log.tagged("预检", f"常见路径发现 XAUTHORITY={p}")
                return str(p)

    # 最后兜底：环境变量 XAUTHORITY，其次 root 家目录 ~/.Xauthority（install 仅支持 root）
    env_xa = os.environ.get("XAUTHORITY", "")
    if env_xa:
        env_path = str(Path(env_xa).expanduser())
        tried.append(env_path)
        if Path(env_path).exists() and os.access(env_path, os.R_OK):
            log.tagged("预检", f"环境变量回退 → XAUTHORITY={env_path}")
            return env_path

    home_xa = str((Path.home() / ".Xauthority").expanduser())
    tried.append(home_xa)
    if Path(home_xa).exists() and os.access(home_xa, os.R_OK):
        log.tagged("预检", f"家目录回退 → XAUTHORITY={home_xa}")
        return home_xa

    tried_txt = ", ".join(dict.fromkeys(tried)) if tried else "(none)"
    raise DeployError(
        f"未自动发现可读 XAUTHORITY。已尝试: {tried_txt}。"
        "请设置 X11VNC_XAUTHORITY（例如 /run/user/0/gdm/Xauthority 或 /run/user/<uid>/gdm/Xauthority）或 root 的 ~/.Xauthority"
    )


def _xdpyinfo_ok(display: str, xauth_path: str) -> bool:
    """静默探测，避免 display×xauth 矩阵对日志文件刷屏。"""
    env = os.environ.copy()
    env["DISPLAY"] = display
    env["XAUTHORITY"] = xauth_path
    try:
        cp = subprocess.run(
            ["xdpyinfo", "-display", display],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
        )
    except subprocess.TimeoutExpired:
        return False
    return cp.returncode == 0


def probe_working_display_xauth(
    cfg: Config,
    log: Logger,
    preferred_xauth: str,
    sessions_hint: Optional[List[Dict[str, str]]] = None,
) -> str:
    if not shutil.which("xdpyinfo"):
        raise DeployError("缺少 xdpyinfo。Ubuntu/Debian: 安装 x11-utils；Rocky: 安装 xorg-x11-utils。")

    displays = [cfg.display] + [d for d in _detect_displays() if d != cfg.display]
    xauth_candidates = [preferred_xauth]
    env_xa = os.environ.get("XAUTHORITY", "")
    if env_xa:
        xauth_candidates.append(str(Path(env_xa).expanduser()))
    xauth_candidates.append(str((Path.home() / ".Xauthority").expanduser()))
    for pattern in ("/run/user/*/gdm/Xauthority", "/run/user/*/lightdm/xauthority", "/var/lib/gdm/.Xauthority"):
        for p in Path("/").glob(pattern.lstrip("/")):
            xauth_candidates.append(str(p))
    xauth_candidates = list(dict.fromkeys(xauth_candidates))

    attempts: List[str] = []
    for disp in displays:
        if not disp:
            continue
        for xa in xauth_candidates:
            if not xa:
                continue
            p = Path(xa)
            if not p.exists() or not os.access(p, os.R_OK):
                continue
            if _xdpyinfo_ok(disp, xa):
                cfg.display = disp
                log.tagged("预检", f"xdpyinfo 已收敛 → DISPLAY={disp} XAUTHORITY={xa}")
                return xa
            attempts.append(f"{disp}|{xa}")

    sessions = sessions_hint if sessions_hint is not None else collect_loginctl_sessions()
    sess_info = ", ".join(
        f"sid={s['sid']} type={s['type'] or 'unknown'} display={s['display'] or '-'} uid={s['uid'] or '-'}"
        for s in sessions
    ) or "(none)"
    raise DeployError(
        "xdpyinfo 无法打开任何候选 display+xauth 组合。"
        f"已尝试: {', '.join(attempts) if attempts else '(none)'}；loginctl: {sess_info}。"
        "请在本地控制台使用 Xorg 会话登录图形环境（可为 root）；Wayland-only 下无完整桌面 X11 可供附着。"
    )


def strip_phase_annotation(flags_line: str) -> str:
    """去掉末尾 `` [phase-name]``（旧版本 last_used_flags 可能附带），得到纯 x11vnc 参数串。"""
    # [^]]+：否定类内首字符 ] 为字面量；末尾匹配 phase 的 ] 在类外，勿写成 \]
    return re.sub(r"\s*\[[^]]+]\s*$", "", flags_line).strip()


def xdpyinfo_accepts(display: str, xauthority_env: str) -> bool:
    if not shutil.which("xdpyinfo"):
        return False
    return _xdpyinfo_ok(display, xauthority_env)


def resolve_runtime_x_attach(cfg: Config, log: Logger) -> tuple[str, str, str]:
    """
    每次服务启动调用：优先 GDM+loginctl 典型候选（Rocky 文档路径），再回退与 install 相同的 xdpyinfo 矩阵探测。
    返回 (DISPLAY, 用于环境的 XAUTHORITY, 传给 -auth 的路径)。
    """
    sessions = collect_loginctl_sessions()
    official = discover_gdm_xorg_candidate(log, sessions)
    if official:
        d = official["display"]
        xa_env = official["env_xauth"]
        auth_path = official["auth_path"]
        if xdpyinfo_accepts(d, xa_env):
            log.tagged("预检", f"运行时附着(GDM 候选): DISPLAY={d} XAUTHORITY={xa_env} -auth={auth_path}")
            return d, xa_env, auth_path
        log.tagged("预检", "WARNING: GDM 候选 xdpyinfo 未通过，回退通用 display+xauth 探测。")

    xa = discover_xauthority(cfg, log, sessions)
    auth_used = probe_working_display_xauth(cfg, log, xa, sessions)
    log.tagged("预检", f"运行时附着(通用探测): DISPLAY={cfg.display} XAUTHORITY={auth_used} -auth={auth_used}")
    return cfg.display, auth_used, auth_used


def build_x11vnc_argv(
    passwd_file: Path,
    vnc_port: int,
    display: str,
    auth_path: str,
    flags: str,
) -> List[str]:
    x11vnc_bin = shutil.which("x11vnc") or "/usr/bin/x11vnc"
    argv: List[str] = [x11vnc_bin, "-display", display, "-auth", auth_path]
    fs = flags.strip()
    if fs:
        argv.extend(shlex.split(fs))
    argv.extend(["-forever", "-shared", "-rfbauth", str(passwd_file), "-rfbport", str(vnc_port)])
    return argv


def validate_install_inputs(cfg: Config) -> None:
    if not cfg.vnc_password:
        raise DeployError("VNC_PASSWORD 不能为空。")
    if not (1024 <= cfg.vnc_port <= 65535):
        raise DeployError("VNC_PORT 必须在 1024-65535。")


def _ss_lines_listening_on_port(ss_stdout: str, port: int) -> List[str]:
    """返回 ss -lntp 中含该 TCP 端口监听信息的行（含 IPv4/IPv6）。"""
    needle = f":{port}"
    return [ln.strip() for ln in (ss_stdout or "").splitlines() if needle in ln and "LISTEN" in ln]


def ensure_vnc_port_available_for_install(cfg: Config, log: Logger) -> None:
    """
    阶段一端口预检：须空闲，或已由 x11vnc 占用（幂等 reinstall，后续 restart 收敛配置）。
    仅读 ss；失败为 DeployError 且**不**触发阶段二的 quiesce。
    """
    cp_ss = run_cmd(log, ["ss", "-lntp"], desc="[预检] 扫描本机监听端口", capture=True)
    lines = _ss_lines_listening_on_port(cp_ss.stdout or "", cfg.vnc_port)
    if not lines:
        return
    blob = "\n".join(lines).lower()
    if "x11vnc" in blob:
        log.tagged(
            "预检",
            f"端口 {cfg.vnc_port}/tcp 已由 x11vnc 监听；视为幂等 reinstall，预检放行（随后将 restart 统一配置）。",
        )
        return
    raise DeployError(
        f"端口 {cfg.vnc_port}/tcp 已被非 x11vnc 进程占用，无法安装。ss 片段: {lines[0][:240]}"
    )


def dry_run_package_install(cfg: Config, log: Logger, pkg_mgr: str) -> None:
    """Debian 与 RHEL 路径分离说明；仅 dry-run 或等价检查，不安装。"""
    if shutil.which("x11vnc"):
        log.tagged("包", "x11vnc 已在 PATH 中，跳过包管理器 dry-run。")
        return
    if pkg_mgr == "apt-get":
        run_cmd(
            log,
            ["apt-get", "install", "-s", "-y", "--no-install-recommends", cfg.x11vnc_pkg],
            desc="[Debian/apt] dry-run: apt-get install -s（不修改系统）",
        )
    elif pkg_mgr == "dnf":
        run_cmd(
            log,
            ["dnf", "-y", "install", "--assumeno", cfg.x11vnc_pkg],
            desc="[RHEL/rpm] dry-run: dnf install --assumeno",
            check=False,
        )
    else:
        log.tagged("RHEL/rpm", "当前后端为 yum，无与 dnf 等价的 --assumeno，跳过 dry-run。")


def preflight_common(
    cfg: Config,
    log: Logger,
    pkg_mgr: str,
    xauth: str,
    sessions_hint: Optional[List[Dict[str, str]]] = None,
) -> str:
    """
    install 阶段一：不产生持久化副作用（无 state/unit 写入、无 systemctl disable）。
    失败由调用方直接向上抛出，不得与阶段二回滚混用。
    """
    log.log("======== 阶段一：预检 ========")
    require_root()
    log.tagged("预检", "install 预检：已确认以 root 执行（euid=0）。")
    require_gdm_custom_conf_wayland_disabled_if_present(log)
    log.tagged("通用", f"DISPLAY={cfg.display}；包后端={package_backend_label(pkg_mgr)}（两系共用同一套 X 探测逻辑）。")
    validate_install_inputs(cfg)
    if sessions_hint:
        brief = ", ".join(
            f"sid={s['sid']} type={s['type'] or 'unknown'} display={s['display'] or '-'} uid={s['uid'] or '-'}"
            for s in sessions_hint
        )
        log.tagged("预检", f"loginctl 会话快照: {brief}")
    resolved_xauth = probe_working_display_xauth(cfg, log, xauth, sessions_hint)
    log.tagged(
        "预检",
        "图形附着：由 xdpyinfo 确认当前 root 可读 XAUTHORITY 且能打开 DISPLAY；"
        "x11vnc 服务进程亦以 root 运行（不设 User=）。",
    )
    ensure_vnc_port_available_for_install(cfg, log)
    dry_run_package_install(cfg, log, pkg_mgr)
    return resolved_xauth


def _rpm_install_x11vnc_with_optional_epel(cfg: Config, log: Logger, rpm: str) -> None:
    cp = run_cmd(log, [rpm, "-y", "install", cfg.x11vnc_pkg], desc=f"[RHEL/rpm] 安装 x11vnc（{rpm}）", check=False)
    if cp.returncode == 0:
        return
    if not cfg.auto_epel:
        raise DeployError(
            f"{rpm} 安装 x11vnc 失败。Rocky 可设置环境变量 ENABLE_AUTO_EPEL=1 或传入 --auto-epel 以自动安装 epel-release 后重试。"
        )
    run_cmd(log, [rpm, "-y", "install", "epel-release"], desc="[RHEL/rpm] 安装 epel-release")
    run_cmd(log, [rpm, "-y", "install", cfg.x11vnc_pkg], desc=f"[RHEL/rpm] 重试安装 x11vnc（{rpm}）")


def install_package(cfg: Config, log: Logger, pkg_mgr: str, state: Dict[str, Any]) -> None:
    if shutil.which("x11vnc"):
        log.tagged("包", "x11vnc 已在 PATH 中，跳过包安装步骤。")
        return

    if pkg_mgr == "apt-get":
        cp = run_cmd(
            log,
            ["apt-get", "install", "-y", "--no-install-recommends", cfg.x11vnc_pkg],
            desc="[Debian/apt] 安装 x11vnc",
            check=False,
        )
        if cp.returncode != 0:
            run_cmd(log, ["apt-get", "update"], desc="[Debian/apt] apt-get update")
            run_cmd(
                log,
                ["apt-get", "install", "-y", "--no-install-recommends", cfg.x11vnc_pkg],
                desc="[Debian/apt] 重试安装 x11vnc",
            )
    else:
        _rpm_install_x11vnc_with_optional_epel(cfg, log, pkg_mgr)

    if not shutil.which("x11vnc"):
        raise DeployError("安装后仍未找到 x11vnc。")
    state["package_installed_by_script"] = True
    log.tagged("包", f"x11vnc 安装完成（后端={package_backend_label(pkg_mgr)}）。")


def ensure_password(cfg: Config, log: Logger, tmp_dir: Path, state: Dict[str, Any]) -> None:
    if cfg.vnc_password == "123456":
        log.tagged("通用", "WARNING: 正在使用默认密码，生产请覆盖 VNC_PASSWORD。")
    newp = tmp_dir / "x11vnc.pass.new"
    run_cmd(log, ["x11vnc", "-storepasswd", cfg.vnc_password, str(newp)], desc="[通用] x11vnc -storepasswd 生成密码文件")
    os.chmod(newp, 0o600)
    if cfg.passwd_file.exists():
        if cfg.passwd_file.read_bytes() == newp.read_bytes():
            log.tagged("通用", "密码文件内容与现网一致，跳过写入。")
            return
    else:
        state["passwd_created_by_script"] = True
    atomic_write_bytes(cfg.passwd_file, newp.read_bytes(), mode=0o600)
    log.tagged("通用", f"密码文件已原子写入: {cfg.passwd_file}")


def build_unit_text(
    cfg: Config,
    flags: str,
    python_exe: str,
    launcher_script: Path,
) -> str:
    """
    写入 systemd unit：ExecStart 调用本脚本内部子命令（INTERNAL_SYSTEMD_SUBCMD），
    由该入口在运行时解析 DISPLAY/-auth，不在 unit 内写死 display 编号。不设 User=，服务始终以 root 运行。
    """
    py = shlex.quote(python_exe)
    script = shlex.quote(str(launcher_script.resolve()))
    sd = shlex.quote(str(cfg.state_dir.resolve()))
    exec_start = f"{py} {script} {INTERNAL_SYSTEMD_SUBCMD} --state-dir {sd}"
    flags_note = flags.strip().replace("\n", " ").replace("#", "")[:240]
    return (
        f"{MANAGED_MARK}\n"
        "[Unit]\n"
        "Description=x11vnc attached to existing X session (dynamic DISPLAY)\n"
        "Documentation=man:x11vnc(1)\n"
        "After=display-manager.service graphical.target\n"
        "StartLimitIntervalSec=300\n"
        "StartLimitBurst=15\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"# x11vnc 参数（由 {INTERNAL_SYSTEMD_SUBCMD} exec x11vnc）：{flags_note}\n"
        f"ExecStart={exec_start}\n"
        "Restart=on-failure\n"
        "RestartSec=8\n\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def backup_existing_unit(cfg: Config, log: Logger, state: Dict[str, Any]) -> None:
    if not cfg.service_file.exists():
        return
    txt = cfg.service_file.read_text(encoding="utf-8", errors="ignore")
    if MANAGED_MARK in txt:
        return
    backup_path = cfg.state_dir / f"{cfg.service_file.name}.backup"
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    if not backup_path.exists():
        shutil.copy2(cfg.service_file, backup_path)
        log.tagged("systemd", f"已备份非托管 unit → {backup_path}")
    state["service_backup"] = str(backup_path)


def write_unit(cfg: Config, log: Logger, unit_text: str, state: Dict[str, Any]) -> bool:
    backup_existing_unit(cfg, log, state)
    old = cfg.service_file.read_text(encoding="utf-8", errors="ignore") if cfg.service_file.exists() else ""
    if old == unit_text:
        log.tagged("systemd", "unit 内容与现网一致，跳过写入。")
        return False
    atomic_write_text(cfg.service_file, unit_text, mode=0o644)
    state["service_managed"] = True
    log.tagged("systemd", f"unit 已原子写入: {cfg.service_file}")
    return True


def quiesce_managed_unit_if_ours(cfg: Config, log: Logger) -> None:
    """
    仅在 install 的「阶段二」抛出 DeployError 后调用：避免已写入损坏 unit/state 后被 systemd 反复拉起。
    不得在预检失败时调用（否则会把正在正常监听的旧实例 disable --now 误杀掉）。
    """
    if not cfg.service_file.exists():
        return
    if MANAGED_MARK not in cfg.service_file.read_text(encoding="utf-8", errors="ignore"):
        return
    run_cmd(
        log,
        ["systemctl", "disable", "--now", cfg.unit_name],
        desc="[systemd] install 失败回滚：disable --now（仅本脚本托管 unit）",
        check=False,
    )


def verify_unit(cfg: Config, log: Logger) -> None:
    if not shutil.which("systemd-analyze"):
        return
    cp = run_cmd(log, ["systemd-analyze", "verify", str(cfg.service_file)], desc="[systemd] systemd-analyze verify", check=False, capture=True)
    if cp.returncode != 0:
        log.tagged("systemd", "WARNING: systemd-analyze verify 返回非 0，继续执行。")


def is_port_listening(port: int) -> bool:
    cp = subprocess.run(["ss", "-lntp"], text=True, capture_output=True)
    return f":{port}" in cp.stdout


def wait_active(unit: str, seconds: int = 20) -> bool:
    for _ in range(seconds):
        if subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0:
            return True
        time.sleep(1)
    return False


def wait_vnc_port_listening(port: int, seconds: int = 25) -> bool:
    """等待 RFB 端口开始监听（与 is_port_listening 配套，单次 install 内一次验收）。"""
    for _ in range(seconds):
        if is_port_listening(port):
            return True
        time.sleep(1)
    return False


def journal(unit: str, lines: int = 120) -> str:
    cp = subprocess.run(["journalctl", "-u", unit, "-n", str(lines), "--no-pager"], text=True, capture_output=True)
    return cp.stdout + cp.stderr


def journal_rollout_hints_from_tail(tail: str, max_items: int = 6) -> str:
    """从 journal 尾部挑出与排障相关的行，避免仅看到「端口未监听」而无从判断。"""
    keys = (
        "error",
        "denied",
        "failed",
        "x11vnc",
        "cannot",
        "rfbauth",
        "password",
        "permission",
        "auth",
        "fatal",
        "errno",
    )
    picked: List[str] = []
    for line in tail.splitlines():
        low = line.lower()
        if any(k in low for k in keys):
            x = line.strip()
            if x and x not in picked:
                picked.append(x)
    if picked:
        return " | ".join(picked[-max_items:])
    lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    return lines[-1] if lines else "(journal 无有效行)"


def _detect_displays() -> List[str]:
    displays: List[str] = []
    xdir = Path("/tmp/.X11-unix")
    if not xdir.exists():
        return displays
    for p in xdir.glob("X*"):
        suffix = p.name[1:]
        if suffix.isdigit():
            displays.append(f":{suffix}")
    # 优先 GDM 常见 :1，其次 :0，再其余按数字升序
    preferred = [d for d in (":1", ":0") if d in displays]
    rest = sorted([d for d in displays if d not in preferred], key=lambda x: int(x[1:]))
    return preferred + rest


def _xorg_session_priority(sess: Dict[str, str]) -> tuple[int, int]:
    """
    discover_gdm 遍历顺序：x11/xorg 优先；同类型时 uid 较高者优先（多会话时减少误选，不排除 root）。
    """
    st = (sess.get("type") or "").lower()
    uid_s = sess.get("uid", "")
    uidn = int(uid_s) if uid_s.isdigit() else -1
    if st == "wayland":
        return 8, uidn
    if st in ("x11", "xorg"):
        return 0, -uidn
    return 6, uidn


def _pick_display_for_xauth(
    sess: Dict[str, str],
    displays: List[str],
    env_xauth: str,
    log: Logger,
) -> str:
    """在 loginctl 提示与 /tmp/.X11-unix 列表上，用 xdpyinfo 选出与 cookie 真实匹配的 DISPLAY。"""
    hinted = (sess.get("display") or "").strip()
    if hinted and xdpyinfo_accepts(hinted, env_xauth):
        return hinted
    if hinted:
        log.tagged(
            "预检",
            f"WARNING: 会话 sid={sess.get('sid', '?')} 的 DISPLAY={hinted} 与当前 XAUTHORITY 无法通过 xdpyinfo，"
            "将按本机 X socket 顺序探测。",
        )
    for cand in displays:
        if xdpyinfo_accepts(cand, env_xauth):
            return cand
    return ""


def discover_gdm_xorg_candidate(log: Logger, sessions: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """
    与 Rocky 9 桌面文档、Debian/Ubuntu 上 GDM + Xorg 常见布局对齐（loginctl、/run/user/<uid>/gdm/Xauthority 等）：
    - 仅返回能通过 xdpyinfo 校验的 (DISPLAY, XAUTHORITY) 组合，供 service-run 优先快速附着。
    - 遍历顺序见 _xorg_session_priority（多会话时减少误选 gdm 辅助会话 + 错误 display）。
    """
    displays = _detect_displays()
    if not displays:
        log.tagged("预检", "WARNING: 未探测到 /tmp/.X11-unix 下的 X socket。")

    ordered = sorted(sessions, key=_xorg_session_priority)
    for sess in ordered:
        if (sess.get("type") or "").lower() == "wayland":
            continue
        uid = sess.get("uid", "")
        run_user = ""
        if uid.isdigit():
            try:
                run_user = pwd.getpwuid(int(uid)).pw_name
            except KeyError:
                run_user = ""
        if not run_user:
            cand_name = sess.get("name", "")
            if cand_name:
                try:
                    run_user = pwd.getpwnam(cand_name).pw_name
                    uid = str(pwd.getpwnam(cand_name).pw_uid)
                except KeyError:
                    run_user = ""
        if not run_user:
            continue
        leader = sess.get("leader", "")
        envs = read_proc_environ(leader) if leader else {}
        env_xauth = envs.get("XAUTHORITY", "")
        if not env_xauth or not Path(env_xauth).exists():
            if uid.isdigit():
                cand = Path(f"/run/user/{uid}/gdm/Xauthority")
                if cand.exists():
                    env_xauth = str(cand)
        if not env_xauth or not Path(env_xauth).exists():
            continue
        auth_path = "/var/lib/gdm/.Xauthority" if Path("/var/lib/gdm/.Xauthority").exists() else env_xauth
        display = _pick_display_for_xauth(sess, displays, env_xauth, log)
        if not display:
            continue
        log.tagged(
            "GDM",
            f"候选(loginctl): sid={sess.get('sid', '?')} session_owner={run_user} display={display} "
            f"env_xauth={env_xauth} auth={auth_path}",
        )
        return {
            "display": display,
            "env_xauth": env_xauth,
            "auth_path": auth_path,
        }

    for p in Path("/run/user").glob("*/gdm/Xauthority"):
        uid = p.parts[3] if len(p.parts) > 3 else ""
        if not uid.isdigit():
            continue
        try:
            run_user = pwd.getpwuid(int(uid)).pw_name
        except KeyError:
            continue
        env_xauth = str(p)
        auth_path = "/var/lib/gdm/.Xauthority" if Path("/var/lib/gdm/.Xauthority").exists() else env_xauth
        display = ""
        for cand in displays:
            if xdpyinfo_accepts(cand, env_xauth):
                display = cand
                break
        if not display:
            continue
        log.tagged(
            "GDM",
            f"候选(path): session_owner={run_user} display={display} env_xauth={env_xauth} auth={auth_path}",
        )
        return {
            "display": display,
            "env_xauth": env_xauth,
            "auth_path": auth_path,
        }
    return None


def rollout_x11vnc_unit(
    cfg: Config,
    log: Logger,
    resolved_xauth: str,
    launcher_script: Path,
    python_exe: str,
    state: Dict[str, Any],
) -> str:
    """
    install 阶段二核心：写 unit → **在 restart 前 save_state**（service-run 读 state.json 的契约）→
    daemon-reload / enable / restart → 验收 active 与 RFB 端口。
    RHEL 与 Debian 同一路径；MIT-SHM 等由默认 X11VNC_FLAGS（含 -noshm）与预检 xdpyinfo 约束。
    """
    flags = re.sub(r"\s+", " ", cfg.x11vnc_flags.strip())
    if not flags:
        raise DeployError("x11vnc 参数串为空，请检查 --flags / X11VNC_FLAGS。")

    launcher_script = launcher_script.resolve()
    state["launcher_script"] = str(launcher_script)
    state["python_executable"] = python_exe
    state["passwd_file"] = str(cfg.passwd_file)
    state["vnc_port"] = cfg.vnc_port
    state["xauthority_override"] = cfg.xauthority_override
    state["runtime_flags"] = flags
    state["preferred_display"] = cfg.display
    state["service_run_user"] = ""

    log.tagged(
        "systemd",
        "注册并启动 x11vnc（ExecStart 以 root 运行；不设 User=）。"
        f" DISPLAY={cfg.display} XAUTHORITY(预检)={resolved_xauth}；参数: {flags}",
    )

    unit_text = build_unit_text(cfg, flags, python_exe, launcher_script)
    write_unit(cfg, log, unit_text, state)
    # restart 后 ExecStart 立即读 state.json；故必须先原子落盘再 systemctl restart。
    save_state(cfg, state)
    verify_unit(cfg, log)
    run_cmd(log, ["systemctl", "daemon-reload"], desc="[systemd] daemon-reload")
    run_cmd(log, ["systemctl", "enable", cfg.unit_name], desc="[systemd] systemctl enable")
    run_cmd(log, ["systemctl", "restart", cfg.unit_name], desc="[systemd] systemctl restart")

    if not wait_active(cfg.unit_name, 25):
        tail = journal(cfg.unit_name, 120)
        with cfg.log_file.open("a", encoding="utf-8") as f:
            f.write("\n----- journal tail (unit not active) -----\n")
            f.write(tail)
            f.write("\n------------------------------------------\n")
        log.tagged("预检", f"journal 摘要: {journal_rollout_hints_from_tail(tail)}")
        raise DeployError(
            f"服务未进入 active：{cfg.unit_name}。请检查 journalctl -u {cfg.unit_name} -n 120 --no-pager"
        )

    if not wait_vnc_port_listening(cfg.vnc_port, 25):
        tail = journal(cfg.unit_name, 120)
        with cfg.log_file.open("a", encoding="utf-8") as f:
            f.write("\n----- journal tail (port not listening) -----\n")
            f.write(tail)
            f.write("\n---------------------------------------------\n")
        log.tagged("预检", f"journal 摘要: {journal_rollout_hints_from_tail(tail)}")
        raise DeployError(
            f"验收失败：{cfg.vnc_port}/tcp 未监听。请检查 journalctl -u {cfg.unit_name} -n 120 --no-pager；"
            "若与 X 扩展相关可尝试调整 --flags（默认已含 -noshm）。"
        )

    return flags


def do_service_run(cfg: Config, log: Logger) -> int:
    """
    仅由 systemd ExecStart 调用（在 argparse 帮助中默认隐藏）。
    读取 state.json，按与 install 相同的策略解析 DISPLAY/XAUTHORITY，再 os.execve 进入 x11vnc。
    unit 不设 User=，始终以 root 运行；本入口不得作为运维日常 CLI 依赖。
    """
    log.tagged("通用", f"{INTERNAL_SYSTEMD_SUBCMD}：内部执行器启动（动态附着 X 会话）")
    state = load_state(cfg)
    if not state:
        log.log("ERROR: 未找到状态文件，请先由 root 执行 install。")
        return 1

    sv = state.get("schema_version")
    if sv is not None:
        try:
            if int(sv) > STATE_SCHEMA_VERSION:
                log.log(f"ERROR: state schema_version={sv} 高于脚本 {STATE_SCHEMA_VERSION}，请升级本脚本。")
                return 1
        except (TypeError, ValueError):
            log.log("WARNING: state schema_version 非法，忽略并继续。")

    passwd_file = Path(str(state.get("passwd_file", cfg.passwd_file)))
    try:
        vnc_port = int(state.get("vnc_port", cfg.vnc_port))
    except (TypeError, ValueError):
        vnc_port = cfg.vnc_port

    raw_rf = state.get("runtime_flags")
    last_meta = state.get("last_used_flags", "")
    if isinstance(raw_rf, str) and raw_rf.strip():
        runtime_flags = raw_rf.strip()
    elif isinstance(last_meta, str) and last_meta.strip():
        runtime_flags = strip_phase_annotation(last_meta)
    else:
        runtime_flags = cfg.x11vnc_flags.strip()

    pref = state.get("preferred_display", "")
    if isinstance(pref, str) and pref.strip():
        cfg.display = pref.strip()

    xo = state.get("xauthority_override", "")
    if isinstance(xo, str):
        cfg.xauthority_override = xo

    if not passwd_file.is_file():
        log.log(f"ERROR: 密码文件不可用: {passwd_file}")
        return 1

    try:
        display, env_xauth, auth_path = resolve_runtime_x_attach(cfg, log)
    except DeployError as e:
        log.log(f"ERROR: {e}")
        return 1

    argv = build_x11vnc_argv(passwd_file, vnc_port, display, auth_path, runtime_flags)
    run_env = os.environ.copy()
    run_env["DISPLAY"] = display
    run_env["XAUTHORITY"] = env_xauth
    log.tagged("通用", f"execve x11vnc argc={len(argv)} DISPLAY={display}")
    os.execve(argv[0], argv, run_env)


def do_install(cfg: Config, log: Logger) -> int:
    """
    三阶段 install：预检（无副作用）→ 写资源与启服（失败则 quiesce 托管 unit）→ 成功摘要。
    幂等：重复 install 在相同参数下应收敛为同配置；端口已被本机 x11vnc 占用时预检放行。
    """
    require_root()
    require_systemd()
    pkg_mgr = detect_pkg_mgr()
    explain_wayland_vs_x11_for_x11vnc(log)

    state = load_state(cfg, strict=True)
    with tempfile.TemporaryDirectory(prefix="x11vnc_setup.") as td:
        tmp_dir = Path(td)
        sess = collect_loginctl_sessions()
        # 阶段一：失败不得 quiesce（避免预检误杀正在提供 VNC 的实例）。
        resolved_xauth = preflight_common(cfg, log, pkg_mgr, discover_xauthority(cfg, log, sess), sess)

        try:
            log.log("======== 阶段二：执行（包安装 / 密码 / systemd 注册）========")
            install_package(cfg, log, pkg_mgr, state)
            ensure_password(cfg, log, tmp_dir, state)
            backup_existing_unit(cfg, log, state)
            used_flags = rollout_x11vnc_unit(
                cfg,
                log,
                resolved_xauth,
                Path(sys.argv[0]).resolve(),
                sys.executable,
                state,
            )
            state["last_used_flags"] = used_flags
            state["runtime_flags"] = strip_phase_annotation(used_flags)
            state["service_file"] = str(cfg.service_file)
            state["passwd_file"] = str(cfg.passwd_file)
            state["unit_name"] = cfg.unit_name
            save_state(cfg, state)
        except DeployError:
            # 仅阶段二失败：停止可能已写入不完整配置的托管 unit，防止 systemd 无限重启；不删 state（可由下次成功 install 覆盖）。
            quiesce_managed_unit_if_ours(cfg, log)
            raise

    log.log("======== 阶段三：完成 ========")
    log.log(f"服务已监听 {cfg.vnc_port}/tcp，使用参数: {state.get('last_used_flags', cfg.x11vnc_flags)}")
    return 0


def do_remove(cfg: Config, log: Logger) -> int:
    """
    幂等卸载：停服、删/还原本脚本托管 unit、按 state 回收脚本创建的文件；state 与备份原子清理。
    """
    require_root()
    require_systemd()
    state = load_state(cfg)
    log.tagged("通用", "开始卸载 x11vnc 配置（幂等）。")

    # stop/disable 永远可重复执行
    run_cmd(log, ["systemctl", "disable", "--now", cfg.unit_name], desc="[systemd] disable --now", check=False)

    service_backup = state.get("service_backup")
    service_exists = cfg.service_file.exists()
    service_txt = cfg.service_file.read_text(encoding="utf-8", errors="ignore") if service_exists else ""
    managed = MANAGED_MARK in service_txt

    if service_exists and (managed or cfg.force_remove_unmanaged):
        cfg.service_file.unlink(missing_ok=True)
        log.tagged("systemd", f"已删除 unit: {cfg.service_file}")
    elif service_exists and not managed:
        log.tagged("systemd", "unit 非本脚本管理，保留（可用 --force-remove-unmanaged-unit 强制删除）。")

    if isinstance(service_backup, str) and Path(service_backup).exists():
        shutil.copy2(Path(service_backup), cfg.service_file)
        log.tagged("systemd", f"已恢复原 unit: {service_backup} -> {cfg.service_file}")

    run_cmd(log, ["systemctl", "daemon-reload"], desc="[systemd] daemon-reload")

    passwd_created = bool(state.get("passwd_created_by_script", False))
    passwd_path = Path(str(state.get("passwd_file", str(cfg.passwd_file))))
    if passwd_created and passwd_path.exists():
        passwd_path.unlink(missing_ok=True)
        log.tagged("通用", f"已删除脚本创建的密码文件: {passwd_path}")

    if cfg.remove_package and bool(state.get("package_installed_by_script", False)):
        pkg_mgr = detect_pkg_mgr()
        if pkg_mgr == "apt-get":
            run_cmd(log, ["apt-get", "remove", "-y", cfg.x11vnc_pkg], desc="[Debian/apt] remove x11vnc", check=False)
        else:
            run_cmd(log, [pkg_mgr, "-y", "remove", cfg.x11vnc_pkg], desc=f"[RHEL/rpm] remove x11vnc ({pkg_mgr})", check=False)

    if cfg.purge_log and cfg.log_file.exists():
        cfg.log_file.unlink(missing_ok=True)
        print(f"已删除日志: {cfg.log_file}")

    # 清理状态与备份
    backup_path = state.get("service_backup")
    if isinstance(backup_path, str) and Path(backup_path).exists():
        Path(backup_path).unlink(missing_ok=True)
    remove_state(cfg)
    if cfg.state_dir.exists() and not any(cfg.state_dir.iterdir()):
        cfg.state_dir.rmdir()

    log.tagged("通用", "卸载完成。")
    return 0


def do_status(cfg: Config) -> int:
    require_systemd()
    state = load_state(cfg)
    print(f"脚本版本: {SCRIPT_VERSION}")
    print(f"unit 路径: {cfg.service_file}")
    print(f"密码路径: {cfg.passwd_file}")
    print(f"日志路径: {cfg.log_file}")
    print(f"状态文件: {cfg.state_file}")
    print(f"状态文件存在: {cfg.state_file.exists()}")
    if state:
        print(f"记录状态: {json.dumps(state, ensure_ascii=False)}")
    service_exists = cfg.service_file.exists()
    print(f"unit 存在: {service_exists}")
    if service_exists:
        txt = cfg.service_file.read_text(encoding="utf-8", errors="ignore")
        print(f"unit 是否脚本管理: {MANAGED_MARK in txt}")
        print(f"ExecStart 动态附着（内部子命令 {INTERNAL_SYSTEMD_SUBCMD}）: {INTERNAL_SYSTEMD_SUBCMD in txt}")
    cp = subprocess.run(["systemctl", "is-enabled", cfg.unit_name], text=True, capture_output=True)
    print(f"is-enabled: {cp.stdout.strip() or cp.stderr.strip() or cp.returncode}")
    cp = subprocess.run(["systemctl", "is-active", cfg.unit_name], text=True, capture_output=True)
    print(f"is-active: {cp.stdout.strip() or cp.stderr.strip() or cp.returncode}")
    print(f"端口 {cfg.vnc_port} 监听: {is_port_listening(cfg.vnc_port)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    epilog = """
示例:
  # 安装/更新（幂等，Ubuntu 或 Rocky）
  sudo python3 setup_x11vnc.py install --password 'StrongPass' --port 5901 --display :0

  # 显式指定 cookie（路径须与当前图形登录身份的 uid 一致；root 图形登录常见如下）
  sudo python3 setup_x11vnc.py install --xauthority /run/user/0/gdm/Xauthority

  # 查看状态
  sudo python3 setup_x11vnc.py status

  # 卸载（恢复备份 unit，删除脚本创建的密码文件）
  sudo python3 setup_x11vnc.py remove

  # 卸载并移除脚本安装的 x11vnc 包、删除日志
  sudo python3 setup_x11vnc.py remove --remove-package --purge-log

说明:
  install 须由 root 执行；unit 固定以 root 运行，每次启动由 service-run 读 state.json 解析 DISPLAY/XAUTHORITY。
  幂等与原子性：预检不写盘、不 stop 服务；阶段二失败仅 disable--now 本脚本托管 unit；state/unit/密码均为原子写。
  端口已被 x11vnc 监听时预检放行，便于重复 install 同一端口。
  install 若发现 state.json 非法 JSON 会直接失败；remove/status 遇损坏状态则按空状态尽量幂等处理。
  注销/重登后 display 变化时仍可附着当前 X 会话；旧版固定 :N 的 unit 请重新 install 覆盖。
"""
    p = argparse.ArgumentParser(
        description=f"x11vnc 部署（Ubuntu/Debian·apt 与 Rocky·dnf/yum；root、幂等、原子写）v{SCRIPT_VERSION}",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=epilog,
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--password", default=os.getenv("VNC_PASSWORD", "123456"), help="VNC 密码（默认 123456）")
        sp.add_argument("--port", type=int, default=int(os.getenv("VNC_PORT", "5901")), help="VNC 端口（默认 5901）")
        sp.add_argument("--display", default=os.getenv("X11VNC_DISPLAY", ":0"), help="X display（默认 :0）")
        sp.add_argument("--xauthority", default=os.getenv("X11VNC_XAUTHORITY", ""), help="显式 XAUTHORITY 路径")
        sp.add_argument("--pkg", default=os.getenv("X11VNC_PKG", "x11vnc"), help="x11vnc 包名（默认 x11vnc）")
        sp.add_argument(
            "--auto-epel",
            action="store_true",
            default=os.getenv("ENABLE_AUTO_EPEL", "0") == "1",
            help="Rocky 系：dnf/yum 安装 x11vnc 失败时自动安装 epel-release",
        )
        sp.add_argument("--service-file", default=os.getenv("SERVICE_FILE", "/etc/systemd/system/x11vnc.service"), help="systemd unit 路径")
        sp.add_argument("--passwd-file", default=os.getenv("PASSWD_FILE", "/etc/x11vnc.pass"), help="rfbauth 密码文件路径")
        sp.add_argument("--log-file", default=os.getenv("LOG_FILE", "/var/log/x11vnc_setup.log"), help="日志路径")
        sp.add_argument("--state-dir", default=os.getenv("STATE_DIR", str(STATE_DIR_DEFAULT)), help="状态目录")
        sp.add_argument(
            "--flags",
            default=os.getenv("X11VNC_FLAGS", DEFAULT_X11VNC_FLAGS),
            help=f"x11vnc 参数串（默认 {DEFAULT_X11VNC_FLAGS!r}）",
        )

    p_install = sub.add_parser("install", help="安装/更新并启动 x11vnc（幂等）")
    add_common(p_install)

    p_remove = sub.add_parser("remove", help="卸载脚本管理的 x11vnc 配置（幂等）")
    add_common(p_remove)
    p_remove.add_argument("--remove-package", action="store_true", help="同时移除脚本安装的 x11vnc 包")
    p_remove.add_argument("--purge-log", action="store_true", help="同时删除日志文件")
    p_remove.add_argument("--force-remove-unmanaged-unit", action="store_true", help="强制删除非脚本管理 unit")

    p_status = sub.add_parser("status", help="查看当前部署状态")
    add_common(p_status)

    # 仅供 systemd ExecStart；SUPPRESS 使其不出现在主 --help 子命令列表；add_help=False 避免误当成运维 CLI
    p_svc = sub.add_parser(INTERNAL_SYSTEMD_SUBCMD, help=argparse.SUPPRESS, add_help=False)
    add_common(p_svc)

    return p


def parse_config(argv: Sequence[str]) -> Config:
    parser = build_parser()
    ns = parser.parse_args(argv)
    return Config(
        command=cast(str, ns.command),
        vnc_password=cast(str, ns.password),
        vnc_port=cast(int, ns.port),
        display=cast(str, ns.display),
        xauthority_override=cast(str, ns.xauthority),
        x11vnc_pkg=cast(str, ns.pkg),
        auto_epel=cast(bool, ns.auto_epel),
        service_file=Path(cast(str, ns.service_file)),
        passwd_file=Path(cast(str, ns.passwd_file)),
        log_file=Path(cast(str, ns.log_file)),
        state_dir=Path(cast(str, ns.state_dir)),
        x11vnc_flags=cast(str, ns.flags),
        purge_log=cast(bool, getattr(ns, "purge_log", False)),
        remove_package=cast(bool, getattr(ns, "remove_package", False)),
        assume_yes=True,
        force_remove_unmanaged=cast(bool, getattr(ns, "force_remove_unmanaged_unit", False)),
    )


def register_signal_handler(log: Logger) -> None:
    def _handler(signum, _frame):
        log.log(f"收到信号 {signum}，退出。")
        raise SystemExit(130)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def main(argv: Sequence[str]) -> int:
    cfg = parse_config(argv)
    log = Logger(cfg.log_file)
    register_signal_handler(log)
    log.log("日志初始化完成")
    try:
        require_linux()
        if cfg.command == "install":
            return do_install(cfg, log)
        if cfg.command == "remove":
            return do_remove(cfg, log)
        if cfg.command == "status":
            return do_status(cfg)
        if cfg.command == INTERNAL_SYSTEMD_SUBCMD:
            return do_service_run(cfg, log)
        raise DeployError(f"未知命令: {cfg.command}")
    except DeployError as e:
        log.log(f"ERROR: {e}")
        return 1
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as e:
        log.log(f"ERROR: 未预期异常: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


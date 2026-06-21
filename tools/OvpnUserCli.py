#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenVPN 客户端证书签发 / 吊销（Easy-RSA 3.0.7）

标准目录（/etc/openvpn/，详见 PKI_LAYOUT 与 -h）:
  <Easy-RSA>/pki/  issued/ private/ reqs/ revoked/ certs_by_serial/（默认 3.0.7）
  client/          create 输出 client.ovpn（默认对齐 server.conf；--proto/--dev 可覆盖）
  server/          server.conf 须含 dev/proto/tls/cipher 等；crl-verify → pki/crl.pem

有效证书: pki/issued/<用户>.crt 存在（权威判定，优先于 index.txt）
index.txt: 同一 CN 可有多行 V/R（重签/吊销历史）；无 issued 时以末条状态为准
吊销后:   easyrsa revoke → move_revoked 按 serial 归档，须再执行 gen-crl

环境变量: GENOVPN_OPENVPN_BASE / GENOVPN_EASYRSA_DIR / GENOVPN_EASYRSA_VERSION
（默认面向 Easy-RSA 3.0.7，与上游 easyrsa3/easyrsa 行为对齐）

client.ovpn 默认与 server.conf 对齐；仅 --proto/--dev/--rhost/--rport 可手动覆盖。

控制台日志块（与 -h / 运行输出一致，详见 LOG_CONSOLE_HELP）:
  [结果] [说明] [警告] [注意] [核验 · 标题]；末尾 --- 操作完成 · 用户 --- 摘要
  终端支持配色高亮（NO_COLOR=1 可关闭）
"""

from __future__ import print_function

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Tuple, TypedDict, cast

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOG_DIR = os.environ.get("GENOVPN_LOG_DIR", "/var/log/openvpn")
LOG_FILE = os.path.join(LOG_DIR, "genovpnuser.log")
LOG_LEVEL = logging.INFO
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# 配置（对齐 /etc/openvpn/ 标准布局；路径可由环境变量覆盖）
# ---------------------------------------------------------------------------
# 脚本按 Easy-RSA v3.0.7 源码约定实现（revoke → move_revoked → gen-crl）：
EASYRSA_UPSTREAM_TAG = "v3.0.7"
EASYRSA_UPSTREAM_URL = (
    "https://github.com/OpenVPN/easy-rsa/blob/{0}/easyrsa3/easyrsa"
).format(EASYRSA_UPSTREAM_TAG)
OPENVPN_BASE = os.environ.get("GENOVPN_OPENVPN_BASE", "/etc/openvpn")
EASYRSA_VERSION = os.environ.get("GENOVPN_EASYRSA_VERSION", "3.0.7")
EASYRSA_DIR = os.environ.get(
    "GENOVPN_EASYRSA_DIR",
    os.path.join(OPENVPN_BASE, EASYRSA_VERSION),
)
CLIENT_DIR = os.environ.get("GENOVPN_CLIENT_DIR", os.path.join(OPENVPN_BASE, "client"))
SERVER_DIR = os.path.join(OPENVPN_BASE, "server")
SERVER_CONF = os.path.join(SERVER_DIR, "server.conf")
SERVER_STATUS_LOG = os.path.join(SERVER_DIR, "openvpn-status.log")
SERVER_IPP = os.path.join(SERVER_DIR, "ipp.txt")

# Easy-RSA verify_ca_init / verify_pki_init 要求的 PKI 条目（v3.0.7）
EASYRSA_PKI_REQUIRED = (
    "pki/index.txt",
    "pki/index.txt.attr",
    "pki/serial",
    "pki/ca.crt",
    "pki/private/ca.key",
    "pki/private",
    "pki/reqs",
    "pki/issued",
    "pki/certs_by_serial",
    "pki/revoked",
    "pki/revoked/certs_by_serial",
    "pki/revoked/private_by_serial",
    "pki/revoked/reqs_by_serial",
)
# OpenVPN 客户端包依赖（非 easyrsa 生成；openvpn --genkey secret pki/ta.key）
OPENVPN_PKI_EXTRA = ("pki/ta.key",)

DEFAULT_REMOTE_HOST = "61.187.64.38"
DEFAULT_REMOTE_PORT = 11940
OVPN_PROTO_CHOICES = frozenset(
    ["tcp", "udp", "tcp4", "tcp6", "udp4", "udp6"]
)
OVPN_DEV_RE = re.compile(r"^tun\d*$", re.IGNORECASE)
OVPN_TAP_DEV_RE = re.compile(r"^tap\d*$", re.IGNORECASE)

# Easy-RSA 内置/server 证书名，禁止作为客户端用户名
RESERVED_USERS = frozenset(["server", "ca"])

USER_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,15}$")
CN_ATTR_RE = re.compile(r"^CN\s*=\s*(.+)$", re.IGNORECASE)
CN_SLASH_RE = re.compile(r"/CN=([^/]+)", re.IGNORECASE)

HISTORY_NONE = "none"
HISTORY_REVOKED = "revoked"
HISTORY_EXPIRED = "expired"

HISTORY_LABEL = {
    HISTORY_NONE: "无历史记录",
    HISTORY_REVOKED: "已吊销",
    HISTORY_EXPIRED: "已过期",
}

STATUS_LABEL = {
    "valid": "有效",
    "revoked": "已吊销",
    "expired": "已过期",
    "none": "不存在",
}

# 目录树说明；硬性检查见 EASYRSA_PKI_REQUIRED / OPENVPN_PKI_EXTRA（须同步维护）
PKI_LAYOUT = """
/etc/openvpn/ 标准布局（Easy-RSA {ver} + OpenVPN，上游 {upstream}）
├── {ver}/                  Easy-RSA 工作区（EASYRSA=/ GENOVPN_EASYRSA_DIR）
│   ├── easyrsa / vars      签发工具与变量
│   └── pki/                PKI（openssl ca 数据库，见 openssl-easyrsa.cnf）
│       ├── ca.crt          CA 证书
│       ├── ta.key          OpenVPN tls-auth/tls-crypt（openvpn --genkey，非 easyrsa）
│       ├── crl.pem         吊销列表（revoke 后须 easyrsa gen-crl）
│       ├── dh.pem          DH 参数（easyrsa gen-dh，仅服务端需要）
│       ├── index.txt       台账 V/R/E（重签/吊销追加行，非覆盖）
│       ├── index.txt.attr  须含 unique_subject = no（续签同 CN）
│       ├── issued/         有效证书 <CN>.crt（revoke 后 move_revoked 移走）
│       ├── private/        有效私钥 <CN>.key
│       ├── reqs/           证书请求 <CN>.req
│       ├── certs_by_serial/<SERIAL>.pem  签发索引（revoke 时删除）
│       ├── revoked/        move_revoked 归档（按 serial 文件名）
│       │   ├── certs_by_serial/<SERIAL>.crt
│       │   ├── private_by_serial/<SERIAL>.key
│       │   └── reqs_by_serial/<SERIAL>.req
│       └── renewed/        renew 时 move_renewed（本脚本未调用 renew）
├── client/                 create 输出 client/<用户>/client.ovpn（默认对齐 server.conf）
└── server/
    └── server.conf         dev/proto/tls/cipher 等；crl-verify → pki/crl.pem

说明: init-pki 仅建 private/ reqs/；build-ca 建 issued/ revoked/* 等；crl.pem 首次 revoke 后 gen-crl 生成。
""".format(ver=EASYRSA_VERSION, upstream=EASYRSA_UPSTREAM_TAG).strip()

# 控制台输出说明（模块文档、-h、USER_EPILOG 共用，与 GenovpnLogger 实现一致）
LOG_CONSOLE_HELP = """
控制台输出:
  === 开始/完成 ===     阶段标题（青色加粗）
  操作: / 日志:         命令与日志文件路径
  [N/M] 步骤            进度；行首 → 表示该步结果（绿色）
  [结果]                本步客观结果（路径为相对 /etc/openvpn 的短路径）
  [说明]                业务含义、交付与后续提示
  [警告]                严重不一致/将导致失败（红底白字高亮）
  [注意]                需关注但不阻断（黄字高亮）
  [核验 · 标题]         操作建议（非 shell 命令清单）
  --- 标题 ---          末尾摘要（行首 · 为要点）
  失败时: [当前状态 · 用户]（简要）、[后续操作]（红色）
  配色: 终端自动启用；设 NO_COLOR=1 关闭；日志文件始终纯文本
""".strip()

# ---------------------------------------------------------------------------
# 控制台配色（仅 TTY 上屏；日志文件写纯文本）
# ---------------------------------------------------------------------------
def _color_enabled():
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


class ConsoleStyle(object):
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    WHITE = "\033[97m"
    BLACK = "\033[30m"
    BG_RED = "\033[41m"
    BG_YELLOW = "\033[43m"

    PHASE = BOLD + CYAN
    OK = BOLD + GREEN
    TAG = BOLD
    WARN = BOLD + YELLOW
    ALERT = BG_RED + WHITE + BOLD
    ERROR = BOLD + RED
    MUTED = DIM


def _paint(text, style):
    if not style or not _color_enabled():
        return text
    return style + text + ConsoleStyle.RESET

# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class GenovpnError(Exception):
    """业务错误，退出码 1。"""

    def __init__(self, message, hint=None):
        super(GenovpnError, self).__init__(message)
        self.message = message
        self.hint = hint


class GenovpnLogger(object):
    """
    RotatingFileHandler 写文件；仅 extra to_stdout=True 的行上屏。
    块标签与结构见 LOG_CONSOLE_HELP；result / note / verify / summary 对应实现。
    """

    _ready = False

    def __init__(self, name="genovpnuser"):
        self.logger = logging.getLogger(name)
        self.log_file = LOG_FILE
        if not GenovpnLogger._ready:
            self._setup()
            GenovpnLogger._ready = True

    def _ensure_log_dir(self):
        for candidate in (LOG_DIR, os.path.join(os.getcwd(), "logs")):
            try:
                os.makedirs(candidate, exist_ok=True)
                if os.access(candidate, os.W_OK):
                    return candidate
            except OSError:
                continue
        return os.getcwd()

    def _setup(self):
        log_dir = self._ensure_log_dir()
        self.log_file = os.path.join(log_dir, "genovpnuser.log")

        self.logger.setLevel(LOG_LEVEL)
        self.logger.propagate = False
        if self.logger.handlers:
            return

        formatter = logging.Formatter(LOG_FORMAT, LOG_DATEFMT)

        file_handler = RotatingFileHandler(
            self.log_file,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(LOG_LEVEL)
        file_handler.addFilter(
            lambda record: not getattr(record, "skip_file", False)
        )

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(LOG_LEVEL)
        stdout_handler.addFilter(
            lambda record: getattr(record, "to_stdout", False)
        )

        self.logger.addHandler(file_handler)
        self.logger.addHandler(stdout_handler)

    def _emit(self, level, msg, to_stdout=False, skip_file=False):
        self.logger.log(
            level,
            msg,
            extra={"to_stdout": to_stdout, "skip_file": skip_file},
        )

    def _stdout(self, plain, styled=None):
        """文件写纯文本，控制台可带配色。"""
        self._emit(logging.INFO, plain)
        self._emit(
            logging.INFO,
            styled if styled is not None else plain,
            to_stdout=True,
            skip_file=True,
        )

    def debug(self, msg):
        self._emit(logging.DEBUG, msg)

    def info(self, msg, to_stdout=False):
        self._emit(logging.INFO, msg, to_stdout=to_stdout)

    def warning(self, msg):
        plain = msg
        self._emit(logging.WARNING, plain)
        self._emit(
            logging.WARNING,
            _paint(plain, ConsoleStyle.WARN),
            to_stdout=True,
            skip_file=True,
        )

    def error(self, msg):
        plain = msg
        self._emit(logging.ERROR, plain)
        self._emit(
            logging.ERROR,
            _paint(plain, ConsoleStyle.ERROR),
            to_stdout=True,
            skip_file=True,
        )

    def phase_start(self, title):
        plain = "=== 开始: {0} ===".format(title)
        self._stdout(plain, _paint(plain, ConsoleStyle.PHASE))

    def phase_end(self, title, note=""):
        suffix = " ({0})".format(note) if note else ""
        plain = "=== 完成: {0}{1} ===".format(title, suffix)
        self._stdout(plain, _paint(plain, ConsoleStyle.PHASE))

    def meta(self, lines):
        """操作元信息：时间戳由 logging 自动附加，此处记录命令上下文。"""
        for line in lines:
            plain = line
            self._stdout(plain, _paint(plain, ConsoleStyle.MUTED))

    def step(self, index, total, action):
        plain = "[{0}/{1}] {2}".format(index, total, action)
        self._stdout(plain)

    def step_ok(self, detail="通过"):
        plain = "  → {0}".format(detail)
        self._stdout(plain, _paint(plain, ConsoleStyle.OK))

    def step_block(self, tag, lines, tag_style=None, line_style=None):
        tag_plain = "  [{0}]".format(tag)
        tag_out = _paint(tag_plain, tag_style) if tag_style else tag_plain
        self._stdout(tag_plain, tag_out)
        for line in _as_lines(lines):
            plain = "    {0}".format(line)
            styled = _paint(plain, line_style) if line_style else plain
            self._stdout(plain, styled)

    def result(self, lines):
        self.step_block("结果", _as_lines(lines))

    def verify(self, title, lines):
        """操作完成后的简要核验提示（非 shell 命令清单）。"""
        self.step_block(
            "核验 · {0}".format(title),
            _as_lines(lines),
            tag_style=ConsoleStyle.TAG,
        )

    def note(self, lines):
        self.step_block("说明", _as_lines(lines))

    def alert(self, lines):
        """严重警告：配置不一致等将导致失败的关键信息。"""
        self.step_block(
            "警告",
            _as_lines(lines),
            tag_style=ConsoleStyle.ALERT,
            line_style=ConsoleStyle.ALERT,
        )

    def warn(self, lines):
        """需关注但不阻断的提示。"""
        self.step_block(
            "注意",
            _as_lines(lines),
            tag_style=ConsoleStyle.WARN,
            line_style=ConsoleStyle.WARN,
        )

    def summary(self, headline, lines):
        plain_head = "--- {0} ---".format(headline)
        self._stdout(plain_head, _paint(plain_head, ConsoleStyle.TAG))
        for line in _as_lines(lines):
            plain = "  · {0}".format(line)
            self._stdout(plain)


def _as_lines(lines):
    if lines is None:
        return []
    if isinstance(lines, (list, tuple)):
        return [line for line in lines if line]
    return [lines]


log = GenovpnLogger()


def prog_name():
    return os.path.basename(sys.argv[0]) or "genovpnuser.py"


def cmd_hint(action, user=None, extra=None):
    parts = [prog_name(), action]
    if user:
        parts.extend(["--user", user])
    if extra:
        parts.extend(extra)
    return " ".join(parts)


def hint_create(user, **kwargs):
    extra = []
    if kwargs.get("rhost"):
        extra.extend(["--rhost", kwargs["rhost"]])
    if kwargs.get("rport"):
        extra.extend(["--rport", str(kwargs["rport"])])
    if kwargs.get("proto"):
        extra.extend(["--proto", kwargs["proto"]])
    if kwargs.get("dev"):
        extra.extend(["--dev", kwargs["dev"]])
    return cmd_hint("create", user, extra or None)


def hint_revoke(user):
    return cmd_hint("revoke", user)


def hint_status(user):
    return cmd_hint("status", user)


def log_failure(error):
    log.error("操作失败: {0}".format(error.message))
    if error.hint:
        log.step_block(
            "后续操作",
            _as_lines(error.hint),
            tag_style=ConsoleStyle.ERROR,
            line_style=ConsoleStyle.ERROR,
        )


def log_user_status(user, brief=False):
    _, lines = describe_user_status(user)
    if brief:
        brief_lines = [lines[0]]
        for line in lines[1:]:
            if line.startswith("serial:"):
                brief_lines.append(line)
                break
        lines = brief_lines
    log.step_block("当前状态 · {0}".format(user), lines)


# ---------------------------------------------------------------------------
# 命令执行
# ---------------------------------------------------------------------------


def run_cmd(cmd, cwd=None, env=None, check=True):
    """执行命令，返回 CompletedProcess（Python 3.6 兼容）。"""
    log.debug("执行命令: {0}".format(" ".join(cmd)))
    merged = os.environ.copy()
    if env:
        merged.update(env)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        env=merged,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        log.debug(
            "命令退出码 {0}: {1}".format(
                proc.returncode, (proc.stdout or "").strip()
            )
        )
    if check and proc.returncode != 0:
        detail = (proc.stdout or "").strip()
        raise GenovpnError(
            "命令执行失败（退出码 {0}）\n    {1}".format(
                proc.returncode, " ".join(cmd)
            ),
            hint=detail if detail else None,
        )
    return proc


def easyrsa_bin_path():
    return os.path.join(EASYRSA_DIR, "easyrsa")


def easyrsa(*args):
    """在 EASYRSA_DIR 下以批处理模式调用 easyrsa（等同官方 EASYRSA_BATCH=1）。"""
    return run_cmd(
        [easyrsa_bin_path()] + list(args),
        cwd=EASYRSA_DIR,
        env={"EASYRSA_BATCH": "1"},
    )


def read_easyrsa_bundle_version():
    """
    从工作区 ChangeLog 读取 Easy-RSA 发行版本（v3.0.7  tarball 根目录常见）。
    无法解析时返回 None。
    """
    changelog = os.path.join(EASYRSA_DIR, "ChangeLog")
    if not os.path.isfile(changelog):
        return None
    try:
        with open(changelog, "r") as fh:
            for line in fh:
                m = re.match(r"^(\d+\.\d+\.\d+)", line)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


def assert_easyrsa_version():
    """目录名与 ChangeLog 版本应与脚本面向的 EASYRSA_VERSION 一致。"""
    dir_name = os.path.basename(os.path.realpath(EASYRSA_DIR))
    expected = EASYRSA_VERSION
    if dir_name != expected:
        raise GenovpnError(
            "Easy-RSA 目录名与脚本要求不一致",
            hint=(
                "脚本面向 Easy-RSA {0}（见 GENOVPN_EASYRSA_VERSION）\n"
                "当前目录名: {1}\n"
                "工作目录:   {2}".format(expected, dir_name, EASYRSA_DIR)
            ),
        )
    bundle_ver = read_easyrsa_bundle_version()
    if bundle_ver and bundle_ver != expected:
        raise GenovpnError(
            "Easy-RSA 发行版本与脚本要求不一致",
            hint=(
                "ChangeLog 版本: {0}\n"
                "脚本要求:       {1}\n"
                "请使用 v{1} 或更新脚本常量后充分回归测试。".format(
                    bundle_ver, expected
                )
            ),
        )


def openssl_version():
    proc = run_cmd(["openssl", "version"], check=False)
    if proc.returncode == 0:
        return (proc.stdout or "").strip()
    return None


# ---------------------------------------------------------------------------
# PKI 路径与证书解析
# ---------------------------------------------------------------------------


class ServerConfSummary(TypedDict):
    exists: bool
    crl_verify: Optional[str]
    crl_matches_pki: Optional[bool]
    port: Optional[str]
    proto: Optional[str]
    dev: Optional[str]
    cipher: Optional[str]
    data_ciphers: Optional[str]
    auth: Optional[str]
    compress: Optional[str]
    comp_lzo: Optional[str]
    allow_compression: Optional[str]
    tls_mode: Optional[str]
    tls_key_file: Optional[str]


class CreateLinkOptions(TypedDict):
    proto: str
    dev: str
    proto_manual: bool
    dev_manual: bool


def path_label(abs_path: str) -> str:
    """日志用短路径（相对 /etc/openvpn）。"""
    abs_path = os.path.normpath(abs_path)
    base = OPENVPN_BASE + os.sep
    if abs_path.startswith(base):
        return abs_path[len(base):]
    return abs_path


def pki_path(*parts: str) -> str:
    return cast(str, os.path.join(EASYRSA_DIR, "pki", *parts))


def client_bundle_dir(user: str) -> str:
    return os.path.join(CLIENT_DIR, user)


def issued_cert(user: str) -> str:
    return pki_path("issued", "{0}.crt".format(user))


def private_key(user: str) -> str:
    return pki_path("private", "{0}.key".format(user))


def user_req(user: str) -> str:
    return pki_path("reqs", "{0}.req".format(user))


def serial_for_pki_paths(serial):
    """
    与 Easy-RSA move_revoked 一致：openssl x509 -noout -serial 去掉 serial= 前缀，
    文件名原样使用（含可能存在的冒号分隔）。
    """
    if not serial:
        return None
    s = serial.strip()
    if s.lower().startswith("serial="):
        s = s.split("=", 1)[1].strip()
    return s or None


def normalize_serial(serial):
    """仅用于 CRL 文本比对，去除冒号并大写。"""
    s = serial_for_pki_paths(serial)
    if not s:
        return None
    s = s.upper()
    if s.startswith("0X"):
        s = s[2:]
    s = s.replace(":", "")
    return s or None


def serial_path_candidates(serial):
    """PKI 路径查找：优先官方格式，再尝试去冒号形式（兼容不同 OpenSSL 输出）。"""
    primary = serial_for_pki_paths(serial)
    if not primary:
        return []
    candidates = [primary]
    norm = normalize_serial(primary)
    if norm and norm not in candidates:
        candidates.append(norm)
    return candidates


def cert_by_serial_pem(serial):
    for sn in serial_path_candidates(serial):
        path = pki_path("certs_by_serial", "{0}.pem".format(sn))
        if os.path.isfile(path):
            return path
    primary = serial_for_pki_paths(serial)
    if not primary:
        return None
    return pki_path("certs_by_serial", "{0}.pem".format(primary))


def revoked_archive_paths(serial):
    """Easy-RSA revoke 后证书/密钥/请求归档路径（按 serial，见 easyrsa move_revoked）。"""
    primary = serial_for_pki_paths(serial)
    if not primary:
        return {}
    for sn in serial_path_candidates(serial):
        paths = {
            "cert": pki_path("revoked", "certs_by_serial", "{0}.crt".format(sn)),
            "key": pki_path("revoked", "private_by_serial", "{0}.key".format(sn)),
            "req": pki_path("revoked", "reqs_by_serial", "{0}.req".format(sn)),
        }
        if any(os.path.isfile(p) for p in paths.values()):
            return paths
    return {
        "cert": pki_path("revoked", "certs_by_serial", "{0}.crt".format(primary)),
        "key": pki_path("revoked", "private_by_serial", "{0}.key".format(primary)),
        "req": pki_path("revoked", "reqs_by_serial", "{0}.req".format(primary)),
    }


def cn_from_index_dn(dn):
    if "/CN=" not in dn:
        return None
    return dn.split("/CN=", 1)[-1].split("/", 1)[0]


def parse_index_fields(parts):
    """
    解析 OpenSSL CA 数据库行（与 Easy-RSA openssl-easyrsa.cnf database= 一致）：
    status, expiry, [revoke_date], serial, filename, DN
    filename 恒为 unknown，DN 在第 6 列（索引 5）。
    """
    if len(parts) < 4:
        return None
    status = parts[0]
    expiry = parts[1]
    revoke_date = parts[2] if len(parts) > 2 else ""
    serial = parts[3] if len(parts) > 3 else ""
    if len(parts) > 5:
        dn = parts[5]
    elif len(parts) > 4 and str(parts[4]).startswith("/"):
        dn = parts[4]
    else:
        dn = ""
    return {
        "status": status,
        "expiry": expiry,
        "revoke_date": revoke_date,
        "serial": serial,
        "dn": dn,
    }


def parse_index_for_user(user):
    """读取 index.txt 中该 CN 的全部记录（文件顺序）。"""
    index = pki_path("index.txt")
    if not os.path.isfile(index):
        return []

    entries = []
    with open(index, "r") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            row = parse_index_fields(parts)
            if not row:
                continue
            cn = cn_from_index_dn(row["dn"])
            if cn != user:
                continue
            entries.append(row)
    return entries


def latest_index_entry(user):
    """该 CN 在 index.txt 中的最后一条记录（重签会追加新行，不覆盖旧行）。"""
    entries = parse_index_for_user(user)
    return entries[-1] if entries else None


def latest_revoked_index_entry(user):
    """该 CN 最后一条 R 记录（用于展示最近一次吊销 serial/时间）。"""
    entries = parse_index_for_user(user)
    revoked = [e for e in entries if e.get("status") == "R"]
    return revoked[-1] if revoked else None


def index_entries_for_user(user):
    return parse_index_for_user(user)


def list_active_client_users():
    """pki/issued/ 下有效客户端（排除 server.crt）。"""
    issued_dir = pki_path("issued")
    if not os.path.isdir(issued_dir):
        return []
    users = []
    for name in sorted(os.listdir(issued_dir)):
        if not name.endswith(".crt"):
            continue
        user = name[:-4]
        if user.lower() == "server":
            continue
        users.append(user)
    return users


def _empty_server_conf_summary() -> ServerConfSummary:
    return {
        "exists": False,
        "crl_verify": None,
        "crl_matches_pki": None,
        "port": None,
        "proto": None,
        "dev": None,
        "cipher": None,
        "data_ciphers": None,
        "auth": None,
        "compress": None,
        "comp_lzo": None,
        "allow_compression": None,
        "tls_mode": None,
        "tls_key_file": None,
    }


def read_server_conf_summary() -> ServerConfSummary:
    """解析 server.conf 中与 PKI、客户端打包相关的关键项。"""
    summary = _empty_server_conf_summary()
    summary["exists"] = os.path.isfile(SERVER_CONF)
    if not summary["exists"]:
        return summary

    expected_crl = os.path.realpath(pki_path("crl.pem"))
    with open(SERVER_CONF, "r") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            parts = line.split()
            if not parts:
                continue
            key = parts[0]
            rest = line[len(key):].strip()

            if key == "crl-verify" and rest:
                summary["crl_verify"] = rest
                try:
                    configured = os.path.realpath(rest)
                    summary["crl_matches_pki"] = configured == expected_crl
                except OSError:
                    summary["crl_matches_pki"] = False
            elif key == "port" and len(parts) >= 2:
                summary["port"] = parts[1]
            elif key == "proto" and len(parts) >= 2:
                summary["proto"] = parts[1]
            elif key == "dev" and len(parts) >= 2:
                summary["dev"] = parts[1]
            elif key == "cipher" and rest:
                summary["cipher"] = rest
            elif key == "data-ciphers" and rest:
                summary["data_ciphers"] = rest
            elif key == "auth" and rest:
                summary["auth"] = rest
            elif key == "compress":
                summary["compress"] = rest if rest else ""
            elif key == "comp-lzo":
                summary["comp_lzo"] = rest if rest else ""
            elif key == "allow-compression" and rest:
                summary["allow_compression"] = rest
            elif key == "tls-auth" and len(parts) >= 2:
                summary["tls_mode"] = "tls-auth"
                summary["tls_key_file"] = parts[1]
            elif key == "tls-crypt-v2" and len(parts) >= 2:
                summary["tls_mode"] = "tls-crypt-v2"
                summary["tls_key_file"] = parts[1]
            elif key == "tls-crypt" and len(parts) >= 2:
                summary["tls_mode"] = "tls-crypt"
                summary["tls_key_file"] = parts[1]
    return summary


def _format_openvpn_directive(name: str, value: Optional[str] = None) -> str:
    """将 OpenVPN 配置项格式化为单行（value 为空时仅指令名）。"""
    if value:
        return "{0} {1}".format(name, value)
    return name


def _append_cipher_directives(
    server: ServerConfSummary,
    lines: List[str],
    missing: Optional[List[str]] = None,
) -> None:
    if server.get("data_ciphers"):
        lines.append(
            _format_openvpn_directive("data-ciphers", server["data_ciphers"])
        )
        if server.get("cipher"):
            lines.append(_format_openvpn_directive("cipher", server["cipher"]))
    elif server.get("cipher"):
        lines.append(_format_openvpn_directive("cipher", server["cipher"]))
    elif missing is not None:
        missing.append("cipher / data-ciphers")


def _append_auth_directive(
    server: ServerConfSummary, lines: List[str]
) -> None:
    if server.get("auth"):
        lines.append(_format_openvpn_directive("auth", server["auth"]))


def _append_compression_directives(
    server: ServerConfSummary, lines: List[str]
) -> None:
    if server.get("allow_compression") is not None:
        lines.append(
            _format_openvpn_directive(
                "allow-compression", server["allow_compression"]
            )
        )
    elif server.get("compress") is not None:
        lines.append(
            _format_openvpn_directive("compress", server["compress"] or None)
        )
    elif server.get("comp_lzo") is not None:
        lines.append(
            _format_openvpn_directive("comp-lzo", server["comp_lzo"] or None)
        )


def _format_tls_server_line(server: ServerConfSummary) -> Optional[str]:
    tls_mode = server.get("tls_mode")
    if not tls_mode:
        return None
    key_file = server.get("tls_key_file") or "ta.key"
    if tls_mode == "tls-auth":
        return _format_openvpn_directive(tls_mode, "{0} 0".format(key_file))
    return _format_openvpn_directive(tls_mode, key_file)


def _append_tls_client_directives(
    server: ServerConfSummary,
    lines: List[str],
    missing: Optional[List[str]] = None,
) -> None:
    tls_mode = server.get("tls_mode")
    if tls_mode == "tls-crypt-v2":
        lines.append("tls-crypt-v2 ta.key")
    elif tls_mode == "tls-crypt":
        lines.append("tls-crypt ta.key")
    elif tls_mode == "tls-auth":
        lines.append("tls-auth ta.key 1")
    elif missing is not None:
        missing.append("tls-auth / tls-crypt / tls-crypt-v2")


def format_server_security_lines(server: ServerConfSummary) -> List[str]:
    """将 server.conf 安全/压缩项格式化为可读行（check 输出用）。"""
    if not server.get("exists"):
        return []
    lines: List[str] = []
    tls_line = _format_tls_server_line(server)
    if tls_line:
        lines.append(tls_line)
    _append_cipher_directives(server, lines)
    _append_auth_directive(server, lines)
    _append_compression_directives(server, lines)
    return lines


def validate_ovpn_proto(proto: str) -> str:
    """校验 client.ovpn 的 proto。"""
    value = (proto or "").strip().lower()
    if value not in OVPN_PROTO_CHOICES:
        raise GenovpnError(
            "协议无效: {0}".format(proto),
            hint="--proto 可选: {0}".format(", ".join(sorted(OVPN_PROTO_CHOICES))),
        )
    return value


def validate_ovpn_dev(dev: str) -> str:
    """校验 client.ovpn 的 dev（tun 路由 / tap 桥接）。"""
    value = (dev or "").strip()
    if OVPN_DEV_RE.match(value) or OVPN_TAP_DEV_RE.match(value):
        return value
    raise GenovpnError(
        "虚拟设备无效: {0}".format(dev),
        hint="--dev 须为 tun/tap 或其编号形式，如 tun、tun0、tap、tap0",
    )


def require_server_conf(server: ServerConfSummary) -> ServerConfSummary:
    if not server.get("exists"):
        raise GenovpnError(
            "server.conf 不存在，无法生成与服务器对齐的 client.ovpn",
            hint="请确认 {0} 存在，或设置 GENOVPN_OPENVPN_BASE".format(
                SERVER_CONF
            ),
        )
    return server


def resolve_create_link_options(
    server: ServerConfSummary,
    proto_override: Optional[str],
    dev_override: Optional[str],
) -> CreateLinkOptions:
    """默认读取 server.conf；仅 --proto/--dev 显式指定时覆盖。"""
    require_server_conf(server)
    proto = proto_override if proto_override is not None else server.get("proto")
    dev = dev_override if dev_override is not None else server.get("dev")
    if not proto:
        raise GenovpnError(
            "无法确定 proto",
            hint="请在 server.conf 配置 proto，或使用 --proto 手动指定",
        )
    if not dev:
        raise GenovpnError(
            "无法确定 dev",
            hint="请在 server.conf 配置 dev，或使用 --dev 手动指定",
        )
    return {
        "proto": validate_ovpn_proto(proto),
        "dev": validate_ovpn_dev(dev),
        "proto_manual": proto_override is not None,
        "dev_manual": dev_override is not None,
    }


def _link_override_warning(
    field: str,
    manual: bool,
    client_value: str,
    server_value: Optional[str],
) -> Optional[str]:
    if manual and server_value and client_value != server_value:
        return "{0} 手动指定 {1} ≠ server.conf {2}".format(
            field, client_value, server_value
        )
    return None


def client_link_override_warnings(
    server: ServerConfSummary, link: CreateLinkOptions
) -> List[str]:
    """手动覆盖且与 server.conf 不一致时告警（操作者已知风险）。"""
    warnings = []
    for field, manual, client_val, server_val in (
        ("proto", link["proto_manual"], link["proto"], server.get("proto")),
        ("dev", link["dev_manual"], link["dev"], server.get("dev")),
    ):
        msg = _link_override_warning(field, manual, client_val, server_val)
        if msg:
            warnings.append(msg)
    return warnings


def resolve_client_security_lines(server: ServerConfSummary) -> List[str]:
    """
    从 server.conf 生成 client.ovpn 安全/压缩指令行（须与服务器 opt-verify 项一致）。
    客户端包固定分发 ta.key，故 tls 行文件名恒为 ta.key。
    """
    require_server_conf(server)
    lines: List[str] = []
    missing: List[str] = []
    _append_tls_client_directives(server, lines, missing)
    _append_cipher_directives(server, lines, missing)
    _append_auth_directive(server, lines)
    _append_compression_directives(server, lines)
    if missing:
        raise GenovpnError(
            "server.conf 缺少客户端必需配置",
            hint="请补充: {0}".format(", ".join(missing)),
        )
    return lines


def pki_files_for_user(user: str, serial=None) -> Dict[str, Optional[str]]:
    """列出某用户相关 PKI 文件路径（用于日志说明）。"""
    files: Dict[str, Optional[str]] = {
        "issued_crt": issued_cert(user),
        "private_key": private_key(user),
        "user_req": user_req(user),
    }
    if serial:
        files["certs_by_serial"] = cert_by_serial_pem(serial)
        archives = revoked_archive_paths(serial)
        files["revoked_cert"] = archives.get("cert")
        files["revoked_key"] = archives.get("key")
        files["revoked_req"] = archives.get("req")
    return files


def format_pki_file_lines(user, serial=None, prefix=""):
    """生成 PKI 文件清单行（仅列出存在的文件）。"""
    files = pki_files_for_user(user, serial)
    mapping = [
        ("issued_crt", "有效证书"),
        ("private_key", "有效私钥"),
        ("user_req", "证书请求"),
        ("certs_by_serial", "serial 索引"),
        ("revoked_cert", "吊销归档·证书"),
        ("revoked_key", "吊销归档·私钥"),
        ("revoked_req", "吊销归档·请求"),
    ]
    lines = []
    for key, label in mapping:
        path = files.get(key)
        if path and os.path.exists(path):
            lines.append("{0}{1}: {2}".format(prefix, label, path_label(path)))
    return lines


def has_valid_cert(user):
    return os.path.isfile(issued_cert(user))


def cn_from_dn(text):
    """从 DN 字符串提取 CN，兼容 CN=alice 与 CN = alice。"""
    for part in text.split(","):
        m = CN_ATTR_RE.match(part.strip())
        if m:
            return m.group(1).strip()
    m = CN_SLASH_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def cert_common_name(cert_file):
    """
    从证书读取 CN。兼容 OpenSSL 1.0.x / 1.1.x 多种 -subject 输出格式。
    """
    if not os.path.isfile(cert_file):
        return None

    # RFC2253（OpenSSL 1.1+，输出 CN=alice,O=...）
    proc = run_cmd(
        [
            "openssl",
            "x509",
            "-in",
            cert_file,
            "-noout",
            "-subject",
            "-nameopt",
            "RFC2253,sep_comma_plus,space_eq",
        ],
        check=False,
    )
    text = (proc.stdout or "").strip()
    if proc.returncode == 0 and text:
        cn = cn_from_dn(text)
        if cn:
            return cn

    # 默认 -subject（1.0: subject=/CN=alice/...；1.1: subject=CN = alice, ...）
    proc = run_cmd(
        ["openssl", "x509", "-in", cert_file, "-noout", "-subject"],
        check=False,
    )
    text = (proc.stdout or "").strip()
    if proc.returncode == 0 and text:
        if text.lower().startswith("subject="):
            text = text[8:].strip()
        cn = cn_from_dn(text)
        if cn:
            return cn

    # -text 兜底（各版本最稳）
    proc = run_cmd(
        ["openssl", "x509", "-in", cert_file, "-noout", "-text"],
        check=False,
    )
    if proc.returncode == 0 and proc.stdout:
        for line in proc.stdout.splitlines():
            line = line.strip()
            if line.startswith("Subject:"):
                cn = cn_from_dn(line[8:].strip())
                if cn:
                    return cn

    return None


def crl_revoked_count():
    """读取 CRL 中已吊销证书数量；CRL 不存在或解析失败时返回 None。"""
    crl = pki_path("crl.pem")
    if not os.path.isfile(crl):
        return None
    proc = run_cmd(
        ["openssl", "crl", "-in", crl, "-text", "-noout"],
        check=False,
    )
    if proc.returncode != 0:
        return None
    return len(re.findall(r"Serial Number:", proc.stdout or ""))


def cert_serial(cert_file):
    """读取证书序列号，用于日志与核验展示。"""
    if not os.path.isfile(cert_file):
        return None
    proc = run_cmd(
        ["openssl", "x509", "-in", cert_file, "-noout", "-serial"],
        check=False,
    )
    if proc.returncode != 0:
        return None
    return serial_for_pki_paths((proc.stdout or "").strip())


def verify_hints_create(user: str, out_dir: str) -> List[str]:
    """[核验 · 签发结果] 提示行（供 log.verify 使用）。"""
    return [
        "{0} → 应显示「有效（可连接 VPN）」".format(hint_status(user)),
        "确认 pki/issued/{0}.crt 存在".format(user),
        "确认 {0}/ 含 client.ovpn 等 5 个文件".format(path_label(out_dir)),
        "完整日志: {0}".format(log.log_file),
    ]


def verify_hints_revoke(user, serial=None):
    """[核验 · 吊销结果] 提示行（供 log.verify 使用）。"""
    lines = [
        "{0} → 应显示「已吊销（无法连接 VPN）」".format(hint_status(user)),
        "确认 pki/issued/{0}.crt 已移除".format(user),
        "确认 pki/crl.pem 已更新",
    ]
    if serial:
        sn = normalize_serial(serial) or serial_for_pki_paths(serial)
        if sn:
            lines.append("确认 CRL 含 serial {0}".format(sn))
    lines.append("确认 server.conf 已配置 crl-verify")
    lines.append("完整日志: {0}".format(log.log_file))
    return lines


def verify_hints_check():
    """[核验 · 环境] 提示行（供 log.verify 使用）。"""
    return [
        "确认 Easy-RSA {0} 与上游 {1} 一致".format(
            path_label(EASYRSA_DIR), EASYRSA_UPSTREAM_TAG
        ),
        "确认已 build-ca（pki/ca.crt、issued/、revoked/*_by_serial/）",
        "确认 pki/ta.key 存在（OpenVPN tls-auth）",
        "确认 server.conf 中 crl-verify 指向 pki/crl.pem",
        "完整日志: {0}".format(log.log_file),
    ]


# ---------------------------------------------------------------------------
# PKI 状态
# ---------------------------------------------------------------------------


def get_index_history(user):
    """
    无 issued/ 证书时，以 index.txt 中该 CN 最后一条记录为准。
    同一 CN 可有多条 V/R（Easy-RSA 重签/吊销均追加行，见 openssl ca 数据库）。
    """
    entry = latest_index_entry(user)
    if not entry:
        return HISTORY_NONE
    status = entry.get("status")
    if status == "R":
        return HISTORY_REVOKED
    if status == "E":
        return HISTORY_EXPIRED
    return HISTORY_NONE


def _index_status_label(status):
    return {"V": "Valid", "R": "Revoked", "E": "Expired"}.get(status, status)


def describe_user_status(user):
    """返回 (状态键, 说明行列表)。有效证书以 pki/issued/<CN>.crt 为准（Easy-RSA 惯例）。"""
    entries = index_entries_for_user(user)
    entry = latest_index_entry(user)

    if has_valid_cert(user):
        cert = issued_cert(user)
        cn = cert_common_name(cert)
        serial = cert_serial(cert)
        lines = ["状态:     有效（可连接 VPN）"]
        if entries:
            lines.append(
                "台账:     index.txt 共 {0} 条，末条 {1}（{2}）".format(
                    len(entries),
                    entry.get("status", "?"),
                    _index_status_label(entry.get("status")),
                )
            )
            if entry and entry.get("status") != "V":
                lines.append(
                    "注意:     末条非 V，当前以 issued/ 证书为准（重签后常见）"
                )
        else:
            lines.append("台账:     index.txt 无匹配记录（以 issued/ 为准）")
        if serial:
            lines.append("serial:   {0}（当前证书）".format(serial))
        lines.extend(format_pki_file_lines(user, serial))
        bundle = client_bundle_dir(user)
        if os.path.isdir(bundle):
            lines.append("客户端包: {0}".format(path_label(bundle)))
        else:
            lines.append(
                "客户端包: 未生成（create 将输出到 {0}/<用户>/）".format(
                    path_label(CLIENT_DIR)
                )
            )
        if cn and cn != user:
            lines.append("注意:     证书 CN 与用户名不一致，建议 revoke 后重建")
        return "valid", lines

    history = get_index_history(user)
    if history == HISTORY_REVOKED:
        rev = latest_revoked_index_entry(user) or entry
        serial = rev["serial"] if rev else None
        lines = [
            "状态:     已吊销（无法连接 VPN）",
            "台账:     index.txt 末条 R（Revoked）",
        ]
        if entries:
            lines.append(
                "历史:     共 {0} 条记录，其中吊销 {1} 次".format(
                    len(entries),
                    sum(1 for e in entries if e.get("status") == "R"),
                )
            )
        if rev:
            if rev.get("serial"):
                lines.append("serial:   {0}（最近一次吊销）".format(rev["serial"]))
            if rev.get("revoke_date"):
                lines.append("吊销时间: {0}".format(rev["revoke_date"]))
        lines.extend(format_pki_file_lines(user, serial))
        lines.append("CRL:      {0}".format(path_label(pki_path("crl.pem"))))
        bundle = client_bundle_dir(user)
        if os.path.isdir(bundle):
            lines.append("客户端包: {0} （建议删除或重新 create 覆盖）".format(
                path_label(bundle)
            ))
        return "revoked", lines

    if history == HISTORY_EXPIRED:
        lines = [
            "状态:     已过期（无法连接 VPN）",
            "台账:     index.txt 末条 E（Expired）",
        ]
        if entries:
            lines.append("历史:     index.txt 共 {0} 条记录".format(len(entries)))
        if entry and entry.get("serial"):
            lines.append("serial:   {0}".format(entry["serial"]))
        lines.append("说明:     可直接 create 重新签发")
        return "expired", lines

    if entries and entry and entry.get("status") == "V":
        lines = [
            "状态:     无有效证书（issued/ 缺失，index 末条仍为 V）",
            "台账:     index.txt 共 {0} 条，末条 V（可能为残留台账）".format(
                len(entries)
            ),
            "说明:     create 将重新签发（easyrsa 会追加新行）",
        ]
        return "none", lines

    lines = [
        "状态:     不存在（从未签发或台账无记录）",
        "说明:     create 将在 pki/issued/ 新建 {0}.crt".format(user),
    ]
    return "none", lines


# ---------------------------------------------------------------------------
# PKI 维护
# ---------------------------------------------------------------------------


def ensure_reissue_allowed():
    attr = pki_path("index.txt.attr")
    if os.path.isfile(attr):
        with open(attr, "r") as fh:
            if re.search(r"unique_subject\s*=\s*no", fh.read()):
                return
    with open(attr, "a") as fh:
        fh.write("unique_subject = no\n")


def cleanup_pki_stale(user):
    """清理妨碍 build-client-full 的残留（官方 build_full 遇同名 req/key/crt 会中止）。"""
    patterns = [
        pki_path("reqs", "{0}.req".format(user)),
        pki_path("private", "{0}.key".format(user)),
        pki_path("issued", "{0}.crt".format(user)),
        pki_path("{0}.creds".format(user)),
    ]
    for path in patterns:
        if os.path.isfile(path):
            os.remove(path)


# ---------------------------------------------------------------------------
# 环境检查
# ---------------------------------------------------------------------------


def preflight():
    missing = []
    if not os.path.isdir(EASYRSA_DIR):
        raise GenovpnError(
            "Easy-RSA 工作目录不存在",
            hint="期望路径: {0}\n可通过 GENOVPN_EASYRSA_DIR 指定。".format(
                EASYRSA_DIR
            ),
        )

    assert_easyrsa_version()

    if not shutil.which("openssl"):
        missing.append("openssl 未安装或不在 PATH 中")

    for rel in ("easyrsa", "vars", "openssl-easyrsa.cnf"):
        path = os.path.join(EASYRSA_DIR, rel)
        if not os.path.exists(path):
            missing.append("缺少: {0}".format(rel))

    for rel in EASYRSA_PKI_REQUIRED:
        path = os.path.join(EASYRSA_DIR, rel)
        if not os.path.exists(path):
            missing.append("缺少: {0}（CA 是否已 build-ca / init-pki？）".format(rel))

    for rel in OPENVPN_PKI_EXTRA:
        path = os.path.join(EASYRSA_DIR, rel)
        if not os.path.isfile(path):
            missing.append(
                "缺少: {0}（OpenVPN tls-auth；"
                "例: openvpn --genkey secret {1}）".format(
                    rel, os.path.join(EASYRSA_DIR, "pki", "ta.key")
                )
            )

    easyrsa_bin = easyrsa_bin_path()
    if os.path.exists(easyrsa_bin) and not os.access(easyrsa_bin, os.X_OK):
        missing.append("不可执行: {0}".format(easyrsa_bin))

    if missing:
        raise GenovpnError(
            "Easy-RSA 环境不完整",
            hint="\n".join("· {0}".format(item) for item in missing),
        )

    proc = run_cmd([easyrsa_bin], cwd=EASYRSA_DIR, check=False)
    expected = "configuration from: {0}/vars".format(EASYRSA_DIR)
    if expected not in (proc.stdout or ""):
        raise GenovpnError(
            "Easy-RSA vars 配置路径不一致",
            hint="期望: {0}\n实际输出:\n{1}".format(
                expected, (proc.stdout or "").strip()
            ),
        )

    for subcmd in ("revoke", "gen-crl", "build-client-full"):
        help_proc = run_cmd(
            [easyrsa_bin, "help", subcmd], cwd=EASYRSA_DIR, check=False
        )
        if help_proc.returncode != 0:
            raise GenovpnError(
                "当前 easyrsa 不支持子命令: {0}".format(subcmd),
                hint=(
                    "本脚本面向 Easy-RSA {0}（{1}）\n"
                    "请核对 GENOVPN_EASYRSA_DIR / ChangeLog 版本。"
                ).format(EASYRSA_VERSION, EASYRSA_UPSTREAM_TAG),
            )


def validate_username(user):
    if user.lower() in RESERVED_USERS:
        raise GenovpnError(
            "用户名 {0!r} 为 Easy-RSA / OpenVPN 保留名".format(user),
            hint=(
                "server / ca 用于服务端与 CA，不能作为客户端用户名\n"
                "pki/issued/server.crt 为 VPN 服务端证书，请勿对本脚本使用 --user server"
            ),
        )
    if not USER_RE.match(user):
        raise GenovpnError(
            "用户名格式无效: {0!r}".format(user),
            hint=(
                "规则: 字母开头，3～16 位，仅含字母、数字、下划线\n"
                "示例: zhangsan、user_01、brinnatt"
            ),
        )


# ---------------------------------------------------------------------------
# 业务校验
# ---------------------------------------------------------------------------


def assert_create_allowed(user):
    if has_valid_cert(user):
        cn = cert_common_name(issued_cert(user))
        log_user_status(user, brief=True)
        if cn != user:
            raise GenovpnError(
                "用户 {0} 存在异常证书（CN={1}）".format(user, cn),
                hint="\n".join(
                    [
                        hint_revoke(user),
                        hint_create(user),
                        hint_status(user),
                    ]
                ),
            )
        raise GenovpnError(
            "用户 {0} 已有有效证书，无法重复签发".format(user),
            hint="\n".join([hint_revoke(user), hint_status(user)]),
        )

    history = get_index_history(user)
    if history in (HISTORY_REVOKED, HISTORY_EXPIRED):
        log.warn(
            [
                "检测到历史记录（{0}），准备重新签发".format(
                    HISTORY_LABEL[history]
                ),
            ]
        )

    cleanup_pki_stale(user)
    ensure_reissue_allowed()


def assert_revoke_allowed(user):
    if has_valid_cert(user):
        return

    history = get_index_history(user)
    log_user_status(user, brief=True)

    if history == HISTORY_REVOKED:
        raise GenovpnError(
            "用户 {0} 已吊销，无需重复操作".format(user),
            hint="\n".join([hint_create(user), hint_status(user)]),
        )
    if history == HISTORY_EXPIRED:
        raise GenovpnError(
            "用户 {0} 证书已过期，无需吊销".format(user),
            hint="\n".join([hint_create(user), hint_status(user)]),
        )

    raise GenovpnError(
        "用户 {0} 不存在，无法吊销".format(user),
        hint="\n".join([hint_create(user), hint_status(user)]),
    )


def assert_issue_done(user):
    cert = issued_cert(user)
    key = private_key(user)

    if not os.path.isfile(cert):
        raise GenovpnError("签发未完成：缺少证书文件", hint="路径: {0}".format(cert))
    if not os.path.isfile(key):
        raise GenovpnError("签发未完成：缺少私钥文件", hint="路径: {0}".format(key))

    cn = cert_common_name(cert)
    if cn is None:
        raise GenovpnError(
            "无法从证书读取 CN（可能是 OpenSSL 版本兼容问题）",
            hint="证书路径: {0}\n请手动执行: openssl x509 -in {0} -noout -text".format(
                cert
            ),
        )
    if cn != user:
        cleanup_pki_stale(user)
        raise GenovpnError(
            "证书 CN 与用户名不一致（CN={0!r}，user={1}）".format(cn, user),
            hint="已清理异常文件，请重试: " + hint_create(user),
        )


def assert_revoke_done(user, serial=None):
    """对照 Easy-RSA revoke + move_revoked：issued 应清空、index 末条为 R、按 serial 归档。"""
    if has_valid_cert(user):
        log_user_status(user, brief=True)
        raise GenovpnError(
            "吊销未完成：证书文件仍存在",
            hint="路径: {0}".format(issued_cert(user)),
        )
    entry = latest_index_entry(user)
    if not entry or entry.get("status") != "R":
        raise GenovpnError(
            "吊销未完成：index.txt 中该用户末条未标记为 R",
            hint="请检查 easyrsa revoke 输出与 {0}".format(pki_path("index.txt")),
        )
    if serial:
        archives = revoked_archive_paths(serial)
        cert_arc = archives.get("cert")
        if not cert_arc or not os.path.isfile(cert_arc):
            raise GenovpnError(
                "吊销未完成：未找到 move_revoked 归档证书",
                hint="期望路径类似: pki/revoked/certs_by_serial/<SERIAL>.crt",
            )


# ---------------------------------------------------------------------------
# 客户端打包
# ---------------------------------------------------------------------------

OVPN_TEMPLATE_HEAD = """\
client
dev {dev}
proto {proto}
remote {host} {port}
resolv-retry infinite
nobind
persist-key
persist-tun
mute-replay-warnings
ca ca.crt
cert {user}.crt
key {user}.key
remote-cert-tls server
"""

OVPN_TEMPLATE_TAIL = """\
verb 3
mute 20
reneg-sec 0
"""


def build_client_ovpn_content(
    user: str,
    host: str,
    port: int,
    link: CreateLinkOptions,
    server: ServerConfSummary,
) -> Tuple[str, List[str]]:
    """组装 client.ovpn 全文；返回 (内容, 安全指令行)。"""
    security_lines = resolve_client_security_lines(server)
    body = OVPN_TEMPLATE_HEAD.format(
        user=user,
        host=host,
        port=str(port),
        proto=link["proto"],
        dev=link["dev"],
    )
    content = body + "\n".join(security_lines) + "\n" + OVPN_TEMPLATE_TAIL
    return content, security_lines


def bundle_client(
    user: str,
    host: str,
    port: int,
    link: CreateLinkOptions,
    server: ServerConfSummary,
) -> Tuple[str, List[str], List[str]]:
    out_dir = client_bundle_dir(user)
    os.makedirs(CLIENT_DIR, exist_ok=True)

    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)

    copies = (
        (pki_path("ca.crt"), "ca.crt", "pki/ca.crt"),
        (pki_path("ta.key"), "ta.key", "pki/ta.key"),
        (issued_cert(user), "{0}.crt".format(user), "pki/issued/{0}.crt".format(user)),
        (private_key(user), "{0}.key".format(user), "pki/private/{0}.key".format(user)),
    )
    copy_log = []
    for src, dst_name, src_label in copies:
        if not os.path.isfile(src):
            raise GenovpnError(
                "打包失败：缺少 PKI 文件",
                hint="路径: {0}（{1}）".format(src, src_label),
            )
        dst = os.path.join(out_dir, dst_name)
        shutil.copy2(src, dst)
        copy_log.append("{0} ← {1}".format(dst_name, src_label))

    ovpn_content, security_lines = build_client_ovpn_content(
        user, host, port, link, server
    )
    ovpn_path = os.path.join(out_dir, "client.ovpn")
    with open(ovpn_path, "w") as fh:
        fh.write(ovpn_content)

    for name in (
        "client.ovpn",
        "ca.crt",
        "ta.key",
        "{0}.crt".format(user),
        "{0}.key".format(user),
    ):
        if not os.path.isfile(os.path.join(out_dir, name)):
            raise GenovpnError(
                "客户端配置包不完整",
                hint="缺少文件: {0}".format(name),
            )

    return out_dir, copy_log, security_lines


# ---------------------------------------------------------------------------
# 命令处理
# ---------------------------------------------------------------------------


def cmd_check(_args):
    log.phase_start("OpenVPN + Easy-RSA 环境检查")
    log.meta(
        [
            "操作: check",
            "日志: {0}".format(log.log_file),
            "布局: {0}".format(OPENVPN_BASE),
        ]
    )

    log.step(1, 3, "检查 Easy-RSA PKI 目录结构")
    preflight()
    ov = openssl_version()
    log.step_ok("PKI 目录就绪 ({0})".format(EASYRSA_DIR))

    log.step(2, 3, "检查 OpenVPN 服务端与客户端目录")
    server = read_server_conf_summary()
    active = list_active_client_users()
    dir_lines = [
        "Easy-RSA:  {0}".format(path_label(EASYRSA_DIR)),
        "客户端包:  {0}".format(path_label(CLIENT_DIR)),
        "服务端:    {0}".format(path_label(SERVER_DIR)),
    ]
    check_alerts = []
    check_warns = []
    if server["exists"]:
        dir_lines.append("server.conf: {0}".format(path_label(SERVER_CONF)))
        if server["crl_verify"]:
            match = "一致" if server["crl_matches_pki"] else "不一致，请核对"
            dir_lines.append(
                "crl-verify: {0} （与 pki/crl.pem {1}）".format(
                    server["crl_verify"], match
                )
            )
            if not server["crl_matches_pki"]:
                check_alerts.append(
                    "crl-verify 路径与 pki/crl.pem 不一致，吊销将无法生效"
                )
        else:
            dir_lines.append("crl-verify: 未配置（吊销证书不会对已连接用户生效）")
            check_warns.append(
                "server.conf 未配置 crl-verify，吊销证书不会对已连接用户生效"
            )
        if server["port"] or server["proto"] or server["dev"]:
            dir_lines.append(
                "监听:      {0} {1} dev {2}".format(
                    server["proto"] or "?",
                    server["port"] or "?",
                    server["dev"] or "?",
                )
            )
        security_lines = format_server_security_lines(server)
        if security_lines:
            dir_lines.append(
                "安全/压缩: {0}".format("；".join(security_lines))
            )
        else:
            check_warns.append(
                "server.conf 未解析到 tls/cipher 等安全项，create 将失败"
            )
        dir_lines.append(
            "client.ovpn: create 默认对齐 server.conf（--proto/--dev 可手动覆盖）"
        )
        if not server.get("proto") or not server.get("dev"):
            check_warns.append(
                "server.conf 缺少 proto 或 dev，create 须手动 --proto/--dev"
            )
    else:
        dir_lines.append("server.conf: 不存在（{0}）".format(SERVER_CONF))
        check_alerts.append(
            "server.conf 不存在，create 无法生成客户端配置"
        )
    log.result(dir_lines)
    if check_alerts:
        log.alert(check_alerts)
    if check_warns:
        log.warn(check_warns)

    log.step(3, 3, "汇总 PKI 与客户端状态")
    crl_count = crl_revoked_count()
    if server["exists"] and server.get("proto") and server.get("dev"):
        link_hint = "{0}/dev {1}".format(server["proto"], server["dev"])
    else:
        link_hint = "见 server.conf 或 --proto/--dev"
    summary = [
        "OpenSSL:   {0}".format(ov or "未知"),
        "远程地址:  {0}:{1}（--rhost/--rport 可覆盖）".format(
            DEFAULT_REMOTE_HOST, DEFAULT_REMOTE_PORT
        ),
        "连接参数:  {0}（默认 server.conf）".format(link_hint),
    ]
    crl_file = pki_path("crl.pem")
    if os.path.isfile(crl_file):
        if crl_count is not None:
            summary.append("CRL 吊销:  {0} 张 → {1}".format(
                crl_count, path_label(crl_file)
            ))
    else:
        summary.append(
            "CRL:      未生成（首次 revoke 后 easyrsa gen-crl 将创建 {0}）".format(
                path_label(crl_file)
            )
        )
    if active:
        summary.append("有效客户端 ({0}): {1}".format(
            len(active), ", ".join(active)
        ))
    else:
        summary.append("有效客户端: 无（pki/issued/ 仅 server.crt 或为空）")
    log.result(summary)
    for line in PKI_LAYOUT.splitlines():
        log.debug(line)
    log.note(
        [
            "create → pki/issued/<用户>.crt + client/<用户>/client.ovpn",
            "client.ovpn 默认与 server.conf 对齐；仅 --proto/--dev 等可手动覆盖",
            "revoke → 更新 pki/crl.pem 并删除 client/<用户>/",
        ]
    )
    log.verify("环境", verify_hints_check())
    log.summary(
        "检查通过",
        [
            "签发 {0} create --user <用户名>".format(prog_name()),
            "查询 {0} status --user <用户名>".format(prog_name()),
        ],
    )
    log.phase_end("环境检查")


def cmd_status(args):
    user = args.user
    log.phase_start("查询用户 {0} 证书状态".format(user))
    log.meta(
        [
            "操作: status --user {0}".format(user),
            "日志: {0}".format(log.log_file),
        ]
    )

    log.step(1, 2, "检查 Easy-RSA 环境")
    preflight()
    validate_username(user)
    log.step_ok("环境就绪")

    log.step(2, 2, "读取 PKI 与用户状态")
    key, lines = describe_user_status(user)
    log.result(lines)

    hints = {
        "valid": "可连接 VPN；更换证书请先 {0}".format(hint_revoke(user)),
        "revoked": "已吊销；续费请 {0}".format(hint_create(user)),
        "expired": "已过期；重新签发 {0}".format(hint_create(user)),
        "none": "未签发；首次签发 {0}".format(hint_create(user)),
    }
    log.note([hints.get(key, "")])
    log.phase_end("状态查询 · {0}".format(user), note=STATUS_LABEL.get(key, key))


def _format_create_meta(user, args, link):
    parts = [
        "操作: create --user {0} --rhost {1} --rport {2}".format(
            user, args.rhost, args.rport
        ),
    ]
    if link["proto_manual"]:
        parts[0] += " --proto {0}".format(link["proto"])
    if link["dev_manual"]:
        parts[0] += " --dev {0}".format(link["dev"])
    parts.append(
        "连接: {0}/dev {1}（{2}）".format(
            link["proto"],
            link["dev"],
            "手动覆盖" if (link["proto_manual"] or link["dev_manual"]) else "server.conf",
        )
    )
    parts.append("日志: {0}".format(log.log_file))
    return parts


def cmd_create(args):
    user = args.user
    total = 4
    server = read_server_conf_summary()
    link = resolve_create_link_options(server, args.proto, args.dev)
    log.phase_start("签发用户 {0} 的 VPN 证书".format(user))
    log.meta(_format_create_meta(user, args, link))

    log.step(1, total, "检查 Easy-RSA 环境")
    preflight()
    log.step_ok("环境就绪 ({0})".format(EASYRSA_DIR))

    log.step(2, total, "校验用户 {0} 是否允许签发".format(user))
    history = get_index_history(user)
    assert_create_allowed(user)
    if history in (HISTORY_REVOKED, HISTORY_EXPIRED):
        log.step_ok("允许重新签发（历史: {0}）".format(HISTORY_LABEL[history]))
    else:
        log.step_ok("允许首次签发")

    log.step(3, total, "签发客户端证书 (easyrsa build-client-full)")
    easyrsa("build-client-full", user, "nopass")
    assert_issue_done(user)
    cert = issued_cert(user)
    serial = cert_serial(cert)
    cn = cert_common_name(cert)
    sn = serial_for_pki_paths(serial)
    pki_result = [
        "CN={0}  serial={1}".format(cn, serial or "见 index.txt"),
        "pki: issued/{0}.crt  private/{0}.key  reqs/{0}.req".format(user),
    ]
    if sn:
        pki_result.append("pki: certs_by_serial/{0}.pem".format(sn))
    log.result(pki_result)

    log.step(4, total, "打包客户端配置")
    out_dir, _copy_log, security_lines = bundle_client(
        user, args.rhost, args.rport, link, server
    )
    link_source = (
        "手动覆盖"
        if (link["proto_manual"] or link["dev_manual"])
        else "server.conf"
    )
    log.result(
        [
            "配置包 {0}/client.ovpn".format(path_label(out_dir)),
            "连接 {0}:{1} ({2}/dev {3}，{4})".format(
                args.rhost,
                args.rport,
                link["proto"],
                link["dev"],
                link_source,
            ),
            "安全选项:  {0}（server.conf）".format("；".join(security_lines)),
            "含 ca.crt、ta.key、{0}.crt、{0}.key".format(user),
        ]
    )
    override_warnings = client_link_override_warnings(server, link)
    if override_warnings:
        log.alert(
            ["client.ovpn 与 server.conf 不一致，将无法连通"]
            + override_warnings
        )
    log.note(
        [
            "请将 {0}/ 整目录安全发给用户，勿公开私钥".format(
                path_label(out_dir)
            ),
        ]
    )
    log.verify("签发结果", verify_hints_create(user, out_dir))
    log.summary(
        "签发完成 · {0}".format(user),
        [
            "用户 {0}，状态有效，可连接 VPN".format(user),
            "配置包 {0}/".format(path_label(out_dir)),
            "查询 {0}".format(hint_status(user)),
        ],
    )
    log.phase_end("签发用户 {0}".format(user))


def cmd_revoke(args):
    user = args.user
    total = 5
    log.phase_start("吊销用户 {0} 的 VPN 证书".format(user))
    log.meta(
        [
            "操作: revoke --user {0}".format(user),
            "日志: {0}".format(log.log_file),
        ]
    )

    log.step(1, total, "检查 Easy-RSA 环境")
    preflight()
    log.step_ok("环境就绪 ({0})".format(EASYRSA_DIR))

    log.step(2, total, "确认用户 {0} 存在有效证书".format(user))
    assert_revoke_allowed(user)
    cert_path = issued_cert(user)
    serial = cert_serial(cert_path)
    cn = cert_common_name(cert_path)
    log.step_ok(
        "CN={0} serial={1}".format(cn, serial or "未知")
    )

    crl_before = crl_revoked_count()
    crl_path = pki_path("crl.pem")

    log.step(3, total, "吊销证书 (easyrsa revoke {0})".format(user))
    easyrsa("revoke", user)
    assert_revoke_done(user, serial)
    sn = serial_for_pki_paths(serial)
    revoke_result = [
        "index.txt: {0} → R".format(user),
        "已移除 issued/{0}.crt、private/{0}.key".format(user),
    ]
    if sn:
        revoke_result.append(
            "已归档 revoked/*_by_serial/{0}.*".format(sn)
        )
    log.result(revoke_result)
    log.note(
        [
            "该用户无法新建 VPN 连接；已在线会话重连后将被拒绝",
        ]
    )

    log.step(4, total, "更新 CRL (easyrsa gen-crl)")
    easyrsa("gen-crl")
    crl_after = crl_revoked_count()
    delta_text = ""
    if crl_before is not None and crl_after is not None:
        delta_text = "（{0} → {1}，本次 +{2}）".format(
            crl_before, crl_after, crl_after - crl_before
        )
    server = read_server_conf_summary()
    log.result(
        [
            "CRL {0}".format(path_label(crl_path)),
            "吊销总数 {0} 张{1}".format(
                crl_after if crl_after is not None else "未知", delta_text
            ),
        ]
    )
    note_lines = []
    if server["exists"] and server["crl_verify"]:
        if server["crl_matches_pki"]:
            note_lines.append(
                "crl-verify 已指向 pki/crl.pem，reload OpenVPN 后生效"
            )
        else:
            log.alert(
                ["crl-verify 路径与 pki/crl.pem 不一致，请核对 server.conf"]
            )
    else:
        log.warn(
            ["请在 server/server.conf 配置 crl-verify 指向 pki/crl.pem"]
        )
    note_lines.append("续费请 {0}".format(hint_create(user)))
    if note_lines:
        log.note(note_lines)
    log.verify("吊销结果", verify_hints_revoke(user, serial))

    log.step(5, total, "清理客户端配置目录")
    client_dir = client_bundle_dir(user)
    if os.path.isdir(client_dir):
        shutil.rmtree(client_dir)
        log.step_ok("已删除 {0}/".format(path_label(client_dir)))
    else:
        log.step_ok("client/{0}/ 不存在，跳过".format(user))

    log.summary(
        "吊销完成 · {0}".format(user),
        [
            "用户 {0}，已吊销，不可连接 VPN".format(user),
            "CRL {0}".format(path_label(crl_path)),
            "续费 {0}".format(hint_create(user)),
        ],
    )
    log.phase_end("吊销用户 {0}".format(user))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USER_HELP = "用户名（兼作证书 CN）：字母开头，3～16 位字母、数字或下划线"
USER_EPILOG = """
目录与版本:
  · 脚本面向 Easy-RSA {ver}（与 OpenVPN/easy-rsa v{ver} 源码一致）
  · 环境变量: GENOVPN_OPENVPN_BASE / GENOVPN_EASYRSA_DIR / GENOVPN_EASYRSA_VERSION
  · {ver}/pki/     issued/ private/ reqs/ revoked/ crl.pem（easyrsa 维护）
  · pki/ta.key     OpenVPN tls-auth/tls-crypt（须单独生成，非 easyrsa）
  · client/        create → client/<用户>/client.ovpn（默认对齐 server.conf）
  · server/        server.conf 含 dev/proto/tls/cipher；crl-verify → pki/crl.pem

用户名规则:
  · 字母开头，3～16 位，字母/数字/下划线
  · 不可用 server、ca（分别为服务端与 CA 保留名）

有效证书判定:
  · pki/issued/<用户名>.crt 存在 → 可连接
  · revoke 后移入 pki/revoked/certs_by_serial/<SERIAL>.crt

{log_help}

完整日志文件: /var/log/openvpn/genovpnuser.log（环境变量 GENOVPN_LOG_DIR 可改）
""".format(ver=EASYRSA_VERSION, log_help=LOG_CONSOLE_HELP)


def build_parser():
    parser = argparse.ArgumentParser(
        description=(
            "OpenVPN 客户端证书管理工具（Easy-RSA {0}）\n"
            "签发证书、打包客户端配置、吊销并更新 CRL。"
        ).format(EASYRSA_VERSION),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
快速开始:
  {prog} check                      # 检查 Easy-RSA 环境
  {prog} create --user zhangsan     # 签发并打包客户端
  {prog} status --user zhangsan     # 查询用户证书状态
  {prog} revoke --user zhangsan     # 吊销并更新 CRL
  {prog} create --user zhangsan     # 吊销后续费 / 重新签发

常用参数:
  create --user USER   必填，用户名
  create --rhost HOST  VPN 服务器地址（默认 {host}）
  create --rport PORT  VPN 端口（默认 {port}）
  create --proto P     覆盖 server.conf 传输协议（默认与 server.conf 一致）
  create --dev D       覆盖 server.conf 虚拟设备（默认与 server.conf 一致）

{log_help}

说明:
  · create: easyrsa build-client-full；revoke: easyrsa revoke + gen-crl（官方流程）
  · client.ovpn 默认对齐 server.conf；仅 --proto/--dev/--rhost/--rport 可手动覆盖
  · CRL（crl.pem）全 CA 共用，每次 revoke 后 gen-crl 覆盖更新
  · 客户端包: client/<用户>/（{client}）
  · Easy-RSA: {easyrsa}
  · 服务端: {server_conf}
""".format(
            prog=prog_name(),
            host=DEFAULT_REMOTE_HOST,
            port=DEFAULT_REMOTE_PORT,
            ver=EASYRSA_VERSION,
            client=CLIENT_DIR,
            easyrsa=EASYRSA_DIR,
            server_conf=SERVER_CONF,
            log_help=LOG_CONSOLE_HELP,
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser(
        "check",
        help="检查 Easy-RSA 与 OpenSSL 环境是否就绪",
        description="检查 Easy-RSA 目录、vars、PKI 文件及 OpenSSL 是否可用。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  {prog} check

运行后可见 [结果] [警告] [注意] [说明] [核验 · 环境]，末尾 --- 检查通过 ---
{common}
""".format(prog=prog_name(), common=USER_EPILOG),
    )

    p_create = sub.add_parser(
        "create",
        help="签发客户端证书并打包 OpenVPN 配置",
        description="为新用户签发证书，或已为吊销/过期用户重新签发（续费）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  {prog} create --user zhangsan
  {prog} create --user lisi --rhost 10.0.0.1 --rport 1194
  {prog} create --user lisi --proto tcp   # 仅当需覆盖 server.conf 时指定
  {prog} create -u zhangsan              # -u 为 --user 简写

输出:
  client/<用户名>/client.ovpn（默认对齐 server.conf；手动覆盖不一致时 [警告] 高亮）
  含 ca.crt、ta.key、<用户>.crt/key、nobind
  控制台末尾: --- 签发完成 · <用户名> ---

运行中可见 [结果] [警告] [注意] [说明] [核验 · 签发结果] 等块（详见 -h 说明）
{common}
""".format(prog=prog_name(), common=USER_EPILOG),
    )
    p_create.add_argument(
        "--user", "-u", required=True, metavar="USER", help=USER_HELP
    )
    p_create.add_argument(
        "--rhost",
        default=DEFAULT_REMOTE_HOST,
        metavar="HOST",
        help="OpenVPN 服务器地址（默认: {0}）".format(DEFAULT_REMOTE_HOST),
    )
    p_create.add_argument(
        "--rport",
        type=int,
        default=DEFAULT_REMOTE_PORT,
        metavar="PORT",
        help="OpenVPN 端口（默认: {0}）".format(DEFAULT_REMOTE_PORT),
    )
    p_create.add_argument(
        "--proto",
        default=None,
        metavar="PROTO",
        help=(
            "覆盖 server.conf 传输协议（默认读取 server.conf；"
            "可选 tcp/udp 及 tcp4/udp4 等）"
        ),
    )
    p_create.add_argument(
        "--dev",
        default=None,
        metavar="DEV",
        help=(
            "覆盖 server.conf 虚拟设备 tun（路由）或 tap（桥接）；"
            "默认读取 server.conf"
        ),
    )

    p_revoke = sub.add_parser(
        "revoke",
        help="吊销用户证书并更新 CRL",
        description="吊销有效证书，更新 pki/crl.pem，并删除客户端配置目录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  {prog} revoke --user zhangsan
  {prog} revoke -u zhangsan

流程（控制台按 [N/5] 步骤输出）:
  1. easyrsa revoke  → issued/<用户>.crt 移除，归档至 pki/revoked/
  2. easyrsa gen-crl → pki/crl.pem 覆盖更新
  3. 删除 client/<用户>/（旧 client.ovpn 不可再分发）

末尾摘要: --- 吊销完成 · <用户名> ---；含 [核验 · 吊销结果]
服务端须配置 crl-verify 指向 pki/crl.pem
{common}
""".format(prog=prog_name(), common=USER_EPILOG),
    )
    p_revoke.add_argument(
        "--user", "-u", required=True, metavar="USER", help=USER_HELP
    )

    p_status = sub.add_parser(
        "status",
        help="查询用户证书状态",
        description="查看用户证书是否有效、已吊销、已过期或不存在。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  {prog} status --user zhangsan
  {prog} status -u zhangsan

输出 [结果] 为状态与 PKI 路径（短路径）；[说明] 为后续操作建议
{common}
""".format(prog=prog_name(), common=USER_EPILOG),
    )
    p_status.add_argument(
        "--user", "-u", required=True, metavar="USER", help=USER_HELP
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    try:
        if args.command == "check":
            cmd_check(args)
        elif args.command == "status":
            cmd_status(args)
        elif args.command == "create":
            validate_username(args.user)
            if not (1 <= args.rport <= 65535):
                raise GenovpnError(
                    "端口号无效: {0}".format(args.rport),
                    hint="--rport 须为 1～65535 的整数",
                )
            if not args.rhost or args.rhost.isspace():
                raise GenovpnError(
                    "服务器地址无效",
                    hint="请指定有效 --rhost，例如: 61.187.64.38",
                )
            if args.proto is not None:
                args.proto = validate_ovpn_proto(args.proto)
            if args.dev is not None:
                args.dev = validate_ovpn_dev(args.dev)
            cmd_create(args)
        elif args.command == "revoke":
            validate_username(args.user)
            cmd_revoke(args)
    except GenovpnError as exc:
        log_failure(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

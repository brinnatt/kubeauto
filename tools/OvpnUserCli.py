#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenVPN 客户端证书签发 / 吊销（Easy-RSA 3.0.7）

标准目录（/etc/openvpn/）:
  3.0.7/     Easy-RSA 工作区（easyrsa、vars、pki/）
  client/    客户端配置包输出（create 生成）
  server/    OpenVPN 服务端（server.conf、crl-verify）

有效证书: pki/issued/<用户>.crt 存在（权威判定，优先于 index.txt）
index.txt: 同一 CN 可有多行 V/R（重签/吊销历史）；无 issued 时以末条状态为准
吊销后:   移入 pki/revoked/，index.txt 追加 R 行，gen-crl 更新 pki/crl.pem
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
from typing import Dict, List, Optional, Tuple, TypedDict

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
LOG_DIR = os.environ.get("GENOVPN_LOG_DIR", "/var/log/openvpn")
LOG_FILE = os.path.join(LOG_DIR, "genovpnuser.log")
LOG_LEVEL = logging.INFO
LOG_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# ---------------------------------------------------------------------------
# 配置（对齐 /etc/openvpn/ 标准布局）
# ---------------------------------------------------------------------------
OPENVPN_BASE = "/etc/openvpn"
EASYRSA_VERSION = "3.0.7"
EASYRSA_DIR = os.path.join(OPENVPN_BASE, EASYRSA_VERSION)
CLIENT_DIR = os.environ.get("GENOVPN_CLIENT_DIR", os.path.join(OPENVPN_BASE, "client"))
SERVER_DIR = os.path.join(OPENVPN_BASE, "server")
SERVER_CONF = os.path.join(SERVER_DIR, "server.conf")
SERVER_STATUS_LOG = os.path.join(SERVER_DIR, "openvpn-status.log")
SERVER_IPP = os.path.join(SERVER_DIR, "ipp.txt")

DEFAULT_REMOTE_HOST = "61.187.64.38"
DEFAULT_REMOTE_PORT = 11940

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

PKI_LAYOUT = """
/etc/openvpn/ 标准布局
├── {ver}/                  Easy-RSA 工作区
│   ├── easyrsa / vars      签发工具与变量
│   └── pki/                PKI 数据库
│       ├── ca.crt          CA 证书（客户端 trust anchor）
│       ├── ta.key          tls-auth 密钥
│       ├── crl.pem         吊销列表（全 CA 共用，revoke 后 gen-crl 更新）
│       ├── dh.pem          DH 参数（服务端用）
│       ├── index.txt       证书台账（V/R/E；同一 CN 可多行，末条+issued/ 判定）
│       ├── issued/         当前有效证书 → <CN>.crt
│       ├── private/        当前有效私钥 → <CN>.key
│       ├── reqs/           证书请求     → <CN>.req
│       ├── certs_by_serial/ 按 serial 索引 → <SERIAL>.pem
│       └── revoked/        吊销归档（按 serial，revoke 后从 issued/ 移入）
│           ├── certs_by_serial/<SERIAL>.crt
│           ├── private_by_serial/<SERIAL>.key
│           └── reqs_by_serial/<SERIAL>.req
├── client/                 客户端配置包（本脚本 create 输出 → <用户>/client.ovpn）
└── server/                 OpenVPN 服务端
    ├── server.conf         须含 crl-verify 指向 pki/crl.pem
    ├── openvpn-status.log
    └── ipp.txt
""".format(ver=EASYRSA_VERSION).strip()

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
    企业级日志：RotatingFileHandler 写文件，extra={'to_stdout': True} 才上屏。
    关键步骤统一输出 [结果] / [审计] / [影响] 三段，便于运维与客户对账。
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

    def debug(self, msg):
        self._emit(logging.DEBUG, msg)

    def info(self, msg, to_stdout=False):
        self._emit(logging.INFO, msg, to_stdout=to_stdout)

    def warning(self, msg):
        self._emit(logging.WARNING, msg, to_stdout=True)

    def error(self, msg):
        self._emit(logging.ERROR, msg, to_stdout=True)

    def phase_start(self, title):
        self.info("=== 开始: {0} ===".format(title), to_stdout=True)

    def phase_end(self, title, note=""):
        suffix = " ({0})".format(note) if note else ""
        self.info("=== 完成: {0}{1} ===".format(title, suffix), to_stdout=True)

    def meta(self, lines):
        """操作元信息：时间戳由 logging 自动附加，此处记录命令上下文。"""
        for line in lines:
            self.info(line, to_stdout=True)

    def step(self, index, total, action):
        self.info("[{0}/{1}] {2}".format(index, total, action), to_stdout=True)

    def step_ok(self, detail="通过"):
        self.info("  → {0}".format(detail), to_stdout=True)

    def step_block(self, tag, lines):
        self.info("  [{0}]".format(tag), to_stdout=True)
        for line in lines:
            self.info("    {0}".format(line), to_stdout=True)

    def result(self, lines):
        self.step_block("结果", _as_lines(lines))

    def audit(self, title, lines):
        self.step_block("审计 · {0}".format(title), _as_lines(lines))

    def impact(self, lines):
        self.step_block("影响", _as_lines(lines))

    def summary(self, title, lines):
        self.info("-" * 60, to_stdout=True)
        self.info("【{0}】".format(title), to_stdout=True)
        for line in _as_lines(lines):
            self.info("  {0}".format(line), to_stdout=True)
        self.info("-" * 60, to_stdout=True)


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
    return cmd_hint("create", user, extra or None)


def hint_revoke(user):
    return cmd_hint("revoke", user)


def hint_status(user):
    return cmd_hint("status", user)


def log_failure(error):
    log.error("操作失败: {0}".format(error.message))
    if error.hint:
        log.audit("建议操作", error.hint.splitlines())


def log_user_status(user):
    _, lines = describe_user_status(user)
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


def easyrsa(*args):
    """在 EASYRSA_DIR 下以批处理模式调用 easyrsa。"""
    easyrsa_bin = os.path.join(EASYRSA_DIR, "easyrsa")
    return run_cmd(
        [easyrsa_bin] + list(args),
        cwd=EASYRSA_DIR,
        env={"EASYRSA_BATCH": "1"},
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


def path_label(abs_path: str) -> str:
    """绝对路径 + 相对 /etc/openvpn 的短名。"""
    abs_path = os.path.normpath(abs_path)
    base = OPENVPN_BASE + os.sep
    if abs_path.startswith(base):
        return "{0}  ({1})".format(abs_path, abs_path[len(base):])
    return abs_path


def pki_path(*parts: str) -> str:
    return os.path.join(EASYRSA_DIR, "pki", *parts)


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
    """仅用于 CRL / 审计 grep 比对，去除冒号并大写。"""
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


def index_audit_grep_cn(user, status=None):
    """生成可在 Linux 上核对 index.txt 的 grep/awk 命令（状态列在行首）。"""
    path = pki_path("index.txt")
    cn_pat = "/CN={0}".format(user)
    if status:
        return "awk -F'\\t' '$1==\"{0}\" && $0 ~ /{1}/' {2}".format(
            status, cn_pat, path
        )
    return "grep '{0}' {1}".format(cn_pat, path)


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


def read_server_conf_summary() -> ServerConfSummary:
    """解析 server.conf 中与 PKI 相关的关键项。"""
    summary: ServerConfSummary = {
        "exists": os.path.isfile(SERVER_CONF),
        "crl_verify": None,
        "crl_matches_pki": None,
        "port": None,
        "proto": None,
    }
    if not summary["exists"]:
        return summary

    expected_crl = os.path.realpath(pki_path("crl.pem"))
    with open(SERVER_CONF, "r") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith(";"):
                continue
            if line.startswith("crl-verify"):
                parts = line.split(None, 1)
                if len(parts) == 2:
                    summary["crl_verify"] = parts[1].strip()
                    try:
                        configured = os.path.realpath(parts[1].strip())
                        summary["crl_matches_pki"] = configured == expected_crl
                    except OSError:
                        summary["crl_matches_pki"] = False
            elif line.startswith("port"):
                parts = line.split()
                if len(parts) >= 2:
                    summary["port"] = parts[1]
            elif line.startswith("proto"):
                parts = line.split()
                if len(parts) >= 2:
                    summary["proto"] = parts[1]
    return summary


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
    """读取证书序列号，用于审计对账。"""
    if not os.path.isfile(cert_file):
        return None
    proc = run_cmd(
        ["openssl", "x509", "-in", cert_file, "-noout", "-serial"],
        check=False,
    )
    if proc.returncode != 0:
        return None
    return serial_for_pki_paths((proc.stdout or "").strip())


def audit_tail():
    return "tail -30 {0}    # 查看完整操作日志".format(log.log_file)


def audit_create(user: str, out_dir: str) -> List[str]:
    cert = issued_cert(user)
    serial = cert_serial(cert) if os.path.isfile(cert) else None
    lines = [
        hint_status(user) + "    # 应显示「有效（可连接 VPN）」",
        "test -f {0} && echo 'issued OK'".format(cert),
        "openssl x509 -in {0} -noout -subject -dates".format(cert),
        "ls -la {0}/".format(out_dir),
        "tree -L 2 {0} {1}".format(EASYRSA_DIR, CLIENT_DIR),
    ]
    if serial:
        pem = cert_by_serial_pem(serial)
        if pem:
            lines.append("test -f {0} && echo 'certs_by_serial OK'".format(pem))
    lines.append(audit_tail())
    return lines


def audit_revoke(user, serial=None):
    lines = [
        hint_status(user) + "    # 应显示「已吊销（无法连接 VPN）」",
        "test ! -f {0} && echo 'issued 已移除 OK'".format(issued_cert(user)),
        index_audit_grep_cn(user, "R") + "    # 该用户全部吊销行",
        "openssl crl -in {0} -text -noout | grep -c 'Serial Number'".format(
            pki_path("crl.pem")
        ),
    ]
    if serial:
        archives = revoked_archive_paths(serial)
        for label, path in (
            ("吊销归档证书", archives.get("cert")),
            ("吊销归档私钥", archives.get("key")),
        ):
            if path:
                lines.append("test -f {0} && echo '{1} OK'".format(path, label))
        sn = normalize_serial(serial)
        lines.append(
            "openssl crl -in {0} -text -noout | grep -i {1}".format(
                pki_path("crl.pem"), sn
            )
        )
    lines.append("grep crl-verify {0}".format(SERVER_CONF))
    lines.append(audit_tail())
    return lines


def audit_check():
    return [
        "tree -L 3 {0}".format(OPENVPN_BASE),
        "openssl version",
        "ls -la {0}/pki/{{issued,private,revoked,certs_by_serial}}".format(EASYRSA_DIR),
        "grep -E '^(port|proto|crl-verify)' {0}".format(SERVER_CONF),
        "openssl crl -in {0} -text -noout | head -20".format(pki_path("crl.pem")),
        audit_tail(),
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
    patterns = [
        pki_path("reqs", "{0}.req".format(user)),
        pki_path("private", "{0}.key".format(user)),
        pki_path("issued", "{0}.crt".format(user)),
        pki_path("expired", "{0}.crt".format(user)),
        pki_path("renewed", "issued", "{0}.crt".format(user)),
        pki_path("renewed", "reqs", "{0}.req".format(user)),
        pki_path("inline", "{0}.inline".format(user)),
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
            hint="期望路径: {0}\n请确认 OpenVPN / Easy-RSA 已正确安装。".format(
                EASYRSA_DIR
            ),
        )

    real = os.path.realpath(EASYRSA_DIR)
    if os.path.basename(real) != EASYRSA_VERSION:
        raise GenovpnError(
            "Easy-RSA 版本目录不匹配",
            hint="脚本配置版本: {0}\n实际目录名:   {1}".format(
                EASYRSA_VERSION, os.path.basename(real)
            ),
        )
    if real != os.path.normpath(EASYRSA_DIR):
        raise GenovpnError(
            "Easy-RSA 路径解析异常",
            hint="配置路径: {0}\n解析结果: {1}".format(EASYRSA_DIR, real),
        )

    if not shutil.which("openssl"):
        missing.append("openssl 未安装或不在 PATH 中")

    for rel in (
        "easyrsa",
        "vars",
        "pki/index.txt",
        "pki/ca.crt",
        "pki/ta.key",
        "pki/issued",
        "pki/private",
        "pki/reqs",
        "pki/certs_by_serial",
        "pki/revoked",
    ):
        path = os.path.join(EASYRSA_DIR, rel)
        if not os.path.exists(path):
            missing.append("缺少: {0}".format(path))

    easyrsa_bin = os.path.join(EASYRSA_DIR, "easyrsa")
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
        log_user_status(user)
        if cn != user:
            raise GenovpnError(
                "用户 {0} 存在异常证书（CN={1}）".format(user, cn),
                hint="\n".join(
                    [
                        hint_revoke(user) + "    # 先吊销异常证书",
                        hint_create(user) + "    # 再重新签发",
                        hint_status(user) + "     # 查看详情",
                    ]
                ),
            )
        raise GenovpnError(
            "用户 {0} 已有有效证书，无法重复签发".format(user),
            hint="\n".join(
                [
                    hint_revoke(user) + "    # 如需更换证书，请先吊销",
                    hint_status(user) + "     # 查看当前状态",
                ]
            ),
        )

    history = get_index_history(user)
    if history in (HISTORY_REVOKED, HISTORY_EXPIRED):
        log.info(
            "检测到历史记录（{0}），准备重新签发".format(HISTORY_LABEL[history]),
            to_stdout=True,
        )

    cleanup_pki_stale(user)
    ensure_reissue_allowed()


def assert_revoke_allowed(user):
    if has_valid_cert(user):
        return

    history = get_index_history(user)
    log_user_status(user)

    if history == HISTORY_REVOKED:
        raise GenovpnError(
            "用户 {0} 已吊销，无需重复操作".format(user),
            hint="\n".join(
                [
                    hint_create(user) + "    # 续费 / 重新签发",
                    hint_status(user) + "     # 查看详情",
                ]
            ),
        )
    if history == HISTORY_EXPIRED:
        raise GenovpnError(
            "用户 {0} 证书已过期，无需吊销".format(user),
            hint="\n".join(
                [
                    hint_create(user) + "    # 直接重新签发",
                    hint_status(user) + "     # 查看详情",
                ]
            ),
        )

    raise GenovpnError(
        "用户 {0} 不存在，无法吊销".format(user),
        hint="\n".join(
            [
                hint_create(user) + "    # 首次签发",
                hint_status(user) + "     # 确认状态",
            ]
        ),
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


def assert_revoke_done(user):
    if has_valid_cert(user):
        log_user_status(user)
        raise GenovpnError(
            "吊销未完成：证书文件仍存在",
            hint="路径: {0}".format(issued_cert(user)),
        )


# ---------------------------------------------------------------------------
# 客户端打包
# ---------------------------------------------------------------------------

OVPN_TEMPLATE = """\
client
dev tun
proto tcp
remote {host} {port}
resolv-retry infinite
persist-key
persist-tun
mute-replay-warnings
ca ca.crt
cert {user}.crt
key {user}.key
remote-cert-tls server
tls-auth ta.key 1
cipher AES-256-CBC
compress lzo
verb 3
mute 20
reneg-sec 0
"""


def bundle_client(user: str, host: str, port: int) -> Tuple[str, List[str]]:
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
        dst = os.path.join(out_dir, dst_name)
        shutil.copy2(src, dst)
        copy_log.append(
            "{0} ← {1}".format(path_label(dst), path_label(src))
        )

    ovpn_path = os.path.join(out_dir, "client.ovpn")
    with open(ovpn_path, "w") as fh:
        fh.write(
            OVPN_TEMPLATE.format(user=user, host=host, port=str(port))
        )

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

    return out_dir, copy_log


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
    if server["exists"]:
        dir_lines.append("server.conf: {0}".format(path_label(SERVER_CONF)))
        if server["crl_verify"]:
            match = "一致" if server["crl_matches_pki"] else "不一致，请核对"
            dir_lines.append(
                "crl-verify: {0} （与 pki/crl.pem {1}）".format(
                    server["crl_verify"], match
                )
            )
        else:
            dir_lines.append("crl-verify: 未配置（吊销证书不会对已连接用户生效）")
        if server["port"] or server["proto"]:
            dir_lines.append(
                "监听:      {0} {1}".format(
                    server["proto"] or "?", server["port"] or "?"
                )
            )
    else:
        dir_lines.append("server.conf: 不存在（{0}）".format(SERVER_CONF))
    log.result(dir_lines)

    log.step(3, 3, "汇总 PKI 与客户端状态")
    crl_count = crl_revoked_count()
    summary = [
        "OpenSSL:   {0}".format(ov or "未知"),
        "默认连接:  {0}:{1}（create 写入 client.ovpn）".format(
            DEFAULT_REMOTE_HOST, DEFAULT_REMOTE_PORT
        ),
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
    legacy_certs = "/client.certs"
    if os.path.isdir(legacy_certs):
        summary.append(
            "注意: 发现旧版目录 {0}，当前标准为 {1}".format(
                legacy_certs, path_label(CLIENT_DIR)
            )
        )
    if active:
        summary.append("有效客户端 ({0}): {1}".format(
            len(active), ", ".join(active)
        ))
    else:
        summary.append("有效客户端: 无（pki/issued/ 仅 server.crt 或为空）")
    log.result(summary)
    log.step_block("目录说明", PKI_LAYOUT.splitlines())
    log.impact(
        [
            "create  → pki/issued/<用户>.crt + {0}/<用户>/client.ovpn".format(
                CLIENT_DIR
            ),
            "revoke  → 移入 pki/revoked/ + 更新 pki/crl.pem + 删除 client/<用户>/",
            "status  → 对照 index.txt 与 issued/ revoked/ 目录",
        ]
    )
    log.audit("核验环境", audit_check())
    log.summary(
        "检查通过",
        [
            "签发: {0} create --user <用户名>".format(prog_name()),
            "查询: {0} status --user <用户名>".format(prog_name()),
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

    if key == "valid":
        log.impact(
            [
                "该用户当前可正常连接 VPN",
                "吊销: {0}".format(hint_revoke(user)),
            ]
        )
    elif key == "revoked":
        log.impact(
            [
                "该用户已被吊销，无法连接 VPN",
                "旧 client.ovpn 已失效，续费请: {0}".format(hint_create(user)),
            ]
        )
    elif key == "expired":
        log.impact(
            [
                "证书已过期，无法连接 VPN",
                "重新签发: {0}".format(hint_create(user)),
            ]
        )
    else:
        log.impact(
            [
                "该用户尚未签发证书",
                "首次签发: {0}".format(hint_create(user)),
            ]
        )

    log.audit(
        "核验状态",
        [
            "test -f {0} ; echo issued=$?".format(issued_cert(user)),
            index_audit_grep_cn(user),
            audit_tail(),
        ],
    )
    log.phase_end("状态查询 · {0}".format(user), note=STATUS_LABEL.get(key, key))


def cmd_create(args):
    user = args.user
    total = 4
    log.phase_start("签发用户 {0} 的 VPN 证书".format(user))
    log.meta(
        [
            "操作: create --user {0} --rhost {1} --rport {2}".format(
                user, args.rhost, args.rport
            ),
            "日志: {0}".format(log.log_file),
        ]
    )

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
    log.impact(
        [
            "将执行 easyrsa build-client-full {0} nopass".format(user),
            "证书 CN 将与用户名 {0} 一致".format(user),
        ]
    )

    log.step(3, total, "签发客户端证书 (easyrsa build-client-full)")
    easyrsa("build-client-full", user, "nopass")
    assert_issue_done(user)
    cert = issued_cert(user)
    serial = cert_serial(cert)
    cn = cert_common_name(cert)
    sn = serial_for_pki_paths(serial)
    pki_result = [
        "Easy-RSA 写入 PKI（相对 {0}/pki/）:".format(EASYRSA_DIR),
        "  issued/{0}.crt      有效证书".format(user),
        "  private/{0}.key     有效私钥".format(user),
        "  reqs/{0}.req        证书请求".format(user),
    ]
    if sn:
        pki_result.append("  certs_by_serial/{0}.pem  serial 索引".format(sn))
    pki_result.extend(
        [
            "CN: {0}  serial: {1}".format(cn, serial or "见 index.txt"),
        ]
    )
    log.result(pki_result)
    log.impact(
        [
            "index.txt 新增 V（Valid）记录",
            "pki/issued/{0}.crt 存在即表示该用户可连接 VPN".format(user),
        ]
    )

    log.step(4, total, "打包客户端配置 → {0}".format(path_label(CLIENT_DIR)))
    out_dir, copy_log = bundle_client(user, args.rhost, args.rport)
    pack_result = [
        "生成: {0}/client.ovpn".format(path_label(out_dir)),
        "连接: {0}:{1} (tcp/tun，与 server/ 独立配置)".format(
            args.rhost, args.rport
        ),
        "自 pki/ 复制:",
    ]
    pack_result.extend(["  " + line for line in copy_log])
    log.result(pack_result)
    log.impact(
        [
            "请将 {0}/ 整个目录安全发给用户".format(path_label(out_dir)),
            "私钥仅存在于 pki/private/ 与此客户端包，勿公开",
            "服务端证书 pki/issued/server.crt 不在客户端包中（正常）",
        ]
    )
    log.audit("核验签发结果", audit_create(user, out_dir))
    log.summary(
        "签发摘要 · {0}".format(user),
        [
            "用户:     {0}".format(user),
            "状态:     有效（可连接 VPN）",
            "配置目录: {0}/".format(out_dir),
            "后续:     {0} status --user {1}".format(prog_name(), user),
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
        "有效证书 confirmed · CN={0}, serial={1}".format(
            cn, serial or "未知"
        )
    )

    crl_before = crl_revoked_count()
    crl_path = pki_path("crl.pem")

    log.step(3, total, "吊销证书 (easyrsa revoke {0})".format(user))
    easyrsa("revoke", user)
    assert_revoke_done(user)
    archives = revoked_archive_paths(serial)
    revoke_result = [
        "index.txt: {0} → R（Revoked）".format(user),
        "移除: {0}".format(path_label(issued_cert(user))),
        "移除: {0}".format(path_label(private_key(user))),
    ]
    if archives.get("cert") and os.path.isfile(archives["cert"]):
        revoke_result.append(
            "归档: {0}".format(path_label(archives["cert"]))
        )
    if archives.get("key") and os.path.isfile(archives["key"]):
        revoke_result.append(
            "归档: {0}".format(path_label(archives["key"]))
        )
    if archives.get("req") and os.path.isfile(archives["req"]):
        revoke_result.append(
            "归档: {0}".format(path_label(archives["req"]))
        )
    log.result(revoke_result)
    log.impact(
        [
            "pki/issued/{0}.crt 不再存在 → 无法新建 VPN 连接".format(user),
            "原证书/私钥/请求按 serial 移入 pki/revoked/ 子目录（Easy-RSA 标准行为）",
            "已在线连接: TLS 重协商或重连后被服务端拒绝",
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
    crl_result = [
        "文件: {0}".format(path_label(crl_path)),
        "机制: 单文件汇总全部吊销 serial（easyrsa gen-crl 覆盖写入）",
        "吊销总数: {0} 张{1}".format(
            crl_after if crl_after is not None else "未知", delta_text
        ),
    ]
    log.result(crl_result)
    impact_lines = [
        "serial {0} 已写入 CRL，与 pki/revoked/ 归档对应".format(
            normalize_serial(serial) or serial or "见 index.txt"
        ),
    ]
    if server["exists"] and server["crl_verify"]:
        if server["crl_matches_pki"]:
            impact_lines.append(
                "server.conf crl-verify 已指向 pki/crl.pem，reload 服务后生效"
            )
        else:
            impact_lines.append(
                "警告: server.conf crl-verify={0} 与 pki/crl.pem 路径不一致".format(
                    server["crl_verify"]
                )
            )
    else:
        impact_lines.append(
            "须在 {0} 配置 crl-verify {1}".format(SERVER_CONF, crl_path)
        )
    impact_lines.append("续费: {0}（新 serial，与本次吊销无关）".format(
        hint_create(user)
    ))
    log.impact(impact_lines)
    log.audit("核验吊销与 CRL", audit_revoke(user, serial))

    log.step(5, total, "清理客户端配置目录")
    client_dir = client_bundle_dir(user)
    if os.path.isdir(client_dir):
        shutil.rmtree(client_dir)
        log.step_ok("已删除 {0}".format(path_label(client_dir)))
        log.impact(
            [
                "client/{0}/ 已清除，旧 client.ovpn 不可再分发".format(user),
                "pki/revoked/ 中仍保留该 serial 的归档供审计".format(user),
            ]
        )
    else:
        log.step_ok("目录不存在，跳过 ({0})".format(path_label(client_dir)))

    log.summary(
        "吊销摘要 · {0}".format(user),
        [
            "用户:     {0}".format(user),
            "状态:     已吊销（不可连接 VPN）",
            "CRL:      {0}".format(crl_path),
            "续费:     {0}".format(hint_create(user)),
        ],
    )
    log.phase_end("吊销用户 {0}".format(user))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

USER_HELP = "用户名（兼作证书 CN）：字母开头，3～16 位字母、数字或下划线"
USER_EPILOG = """
目录布局（/etc/openvpn/）:
  · {ver}/pki/     Easy-RSA PKI（issued/ private/ revoked/ crl.pem）
  · client/        客户端配置包（create 输出）
  · server/        服务端 server.conf（crl-verify 指向 pki/crl.pem）

用户名规则:
  · 字母开头，3～16 位，字母/数字/下划线
  · 不可用 server、ca（分别为服务端与 CA 保留名）

有效证书判定:
  · pki/issued/<用户名>.crt 存在 → 可连接
  · revoke 后移入 pki/revoked/certs_by_serial/<SERIAL>.crt

日志: /var/log/openvpn/genovpnuser.log（GENOVPN_LOG_DIR 可改）
""".format(ver=EASYRSA_VERSION)


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

说明:
  · 操作日志: /var/log/openvpn/genovpnuser.log（环境变量 GENOVPN_LOG_DIR 可改）
  · 每步含 [结果] [审计] [影响] 三段，便于运维核验与交付说明
  · CRL（crl.pem）为整个 CA 共用一份，每次 revoke 后 gen-crl 覆盖更新
  · 客户端包目录: {client}
  · Easy-RSA 目录: {easyrsa}
  · 服务端配置: {server_conf}
""".format(
            prog=prog_name(),
            host=DEFAULT_REMOTE_HOST,
            port=DEFAULT_REMOTE_PORT,
            client=CLIENT_DIR,
            easyrsa=EASYRSA_DIR,
            server_conf=SERVER_CONF,
        ),
    )
    sub = parser.add_subparsers(dest="command", metavar="command")

    sub.add_parser(
        "check",
        help="检查 Easy-RSA 与 OpenSSL 环境是否就绪",
        description="检查 Easy-RSA 目录、vars、PKI 文件及 OpenSSL 是否可用。",
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
  {prog} create -u zhangsan              # -u 为 --user 简写

输出:
  {client}/<用户名>/client.ovpn
  自 pki/ca.crt、pki/ta.key、pki/issued/<用户>.crt、pki/private/<用户>.key 复制
{common}
""".format(prog=prog_name(), client=CLIENT_DIR, common=USER_EPILOG),
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

    p_revoke = sub.add_parser(
        "revoke",
        help="吊销用户证书并更新 CRL",
        description="吊销有效证书，更新 pki/crl.pem，并删除客户端配置目录。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  {prog} revoke --user zhangsan
  {prog} revoke -u zhangsan

吊销流程（Easy-RSA 标准，脚本会逐步说明）:
  1. easyrsa revoke
       pki/issued/<用户>.crt      → 移除
       pki/private/<用户>.key     → 移入 pki/revoked/private_by_serial/<SERIAL>.key
       pki/revoked/certs_by_serial/<SERIAL>.crt  ← 归档
       pki/index.txt              → 标记 R
  2. easyrsa gen-crl
       pki/crl.pem                → 覆盖更新（含全部吊销 serial）
  3. 删除 {client}/<用户>/        → 旧 client.ovpn 不可再分发

服务端: server/server.conf 须 crl-verify 指向 pki/crl.pem
{common}
""".format(prog=prog_name(), client=CLIENT_DIR, common=USER_EPILOG),
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
            cmd_create(args)
        elif args.command == "revoke":
            validate_username(args.user)
            cmd_revoke(args)
    except GenovpnError as exc:
        log_failure(exc)
        sys.exit(1)


if __name__ == "__main__":
    main()

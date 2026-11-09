#!/usr/bin/env python3
"""Standalone MySQL 8.0+ logical backup CLI."""

import argparse
import bz2
import gzip
import lzma
import zlib
import configparser
import fcntl
import hashlib
import io
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


VERSION = "1.0.0"
DEFAULT_LOGIN_PATH = "mysql-backup"
DEFAULT_BACKUP_MOUNT = "/backups"
DEFAULT_LOGIN_FILE = "/root/.mylogin.cnf"
DEFAULT_LOCK_FILE = "/run/lock/mysql_backup.lock"
DEFAULT_LOG_FILE = "/var/log/mysql_backup.log"
DEFAULT_UNIT_DIR = "/etc/systemd/system"
SERVICE_NAME = "mysql-logi-backup.service"
TIMER_NAME = "mysql-logi-backup.timer"
MAX_BACKUP_FILES = 3
SPACE_SAFETY_FACTOR = 1.2
SPACE_RESERVE_BYTES = 128 * 1024 ** 2
HEARTBEAT_SECONDS = 60
TERMINATE_GRACE_SECONDS = 30
SYSTEM_DATABASES = frozenset(("information_schema", "performance_schema", "sys", "ndbinfo"))
VERSION_RE = re.compile(r"(?<!\d)(\d+)\.(\d+)(?:\.(\d+))?")

ACTIVE_PROCESS = None
RECEIVED_SIGNAL = None
LOGGER = logging.getLogger("mylogi_backup")


NATIVE_TARGETS = frozenset(("mysql", "mysqldump"))

# Native options stay local to each standalone CLI. Values are argv, never shell.
NATIVE_PROTECTED = frozenset("""
help version print-defaults defaults-file defaults-extra-file defaults-group-suffix
no-defaults login-path no-login-paths user password password1 password2 password3
host port socket protocol execute init-command init-command-add database databases
all-databases tables result-file tab xml json force opt compact compatible
single-transaction lock-tables lock-all-tables source-data master-data
set-gtid-purged comments dump-date quick no-data no-create-info ignore-table
ignore-table-data where delete-source-logs delete-master-logs flush-logs flush-privileges
backup prepare copy-back move-back decompress decrypt export stats target-dir datadir
incremental incremental-basedir incremental-dir incremental-lsn incremental-history-name
incremental-history-uuid apply-log-only stream compress remove-original lock-ddl no-lock
ignore-error no-backup-locks no-server-version-check start-position stop-position start-datetime
stop-datetime read-from-remote-server raw to-last-log stop-never
""".split())


def native_option_help():
    """Customer reference, available without credentials or database access."""
    protected = sorted(NATIVE_PROTECTED)
    listing = "\n".join("  " + "  ".join("--" + name for name in protected[index:index + 4])
                        for index in range(0, len(protected), 4))
    return """原生参数帮助：MyLogiBackupCli

参数交给哪个命令（等号左侧必须完整填写）：
  mysql       前置检查和信息查询客户端，不负责导出 SQL
  mysqldump   真正导出 SQL 的命令，不作用于前置 mysql 查询
  同一个选项需要作用于两者时，分别指定；不会自动复制给另一命令。

示例（先配置登录路径、独立 /backups 挂载和匹配版本的客户端）：
  MyLogiBackupCli preflight --database app --native-option mysql=--connect-timeout=15
  MyLogiBackupCli backup --database app --native-option mysqldump=--extended-insert
  MyLogiBackupCli backup --database app --native-option mysql=--connect-timeout=15 --native-option mysqldump=--max-allowed-packet=256M
  MyLogiBackupCli backup --database app --native-option 'mysqldump=--ssl-ca=/etc/mysql/backup CA.pem'
  CA 文件必须真实存在、可读且符合当前 TLS 配置；以上示例不代替 TLS 信任与身份配置。
  --extended-insert 只改变 INSERT 输出，不消除查询等待或 1412；默认仍为逐行 INSERT。

定时任务保存参数（仅影响此后新启动的任务）：
  MyLogiBackupCli timer install --at 01:01 --exec-path /usr/local/bin/MyLogiBackupCli --database app --native-option mysqldump=--extended-insert
  MyLogiBackupCli timer status
  安装时须把本次需要的全部范围、压缩和原生参数一并写入；不会继承以前一次 backup 的参数。
  已安装任务须重新 timer install 后生效；timer remove 停用任务，不删除备份归档。

受保护选项的正常入口：
  --password/--user/--host/--port：使用 setup-login 和 --login-path 管理凭据与连接。
  --databases/--all-databases/--ignore-table：使用 --database/--exclude-database 选择库范围。
  --result-file：由工具在备份挂载目录生成临时文件，校验后原子发布。
  --single-transaction/--source-data/--set-gtid-purged：由工具统一管理，无覆盖入口。
  --force：不允许跳过导出错误继续生成不完整备份；应移除该参数并排查首个错误。

原生参数使用规则：
  日常备份继续使用原有命令；只有需要额外的 MySQL/XtraBackup 选项时才添加 --native-option。
  格式：--native-option '命令或阶段=--完整长参数名=值'
  无值开关：--native-option '命令或阶段=--完整长参数名'
  每次只传一个参数；多个参数重复写 --native-option。值必须用 = 连接。
  路径含空格时，用引号包住整个“命令或阶段=--参数=值”。引号由终端处理，不传给原生命令。
  不接受短参数、位置参数或命令字符串；不通过 shell 执行，也不展开 $变量、通配符或命令替换。
  不添加原生参数时保留既有默认值；添加后按顺序传入，是否支持由实际安装的命令决定。

为何部分参数会被拒绝：
  备份范围、输出文件、凭据、一致性和恢复流程由工具统一管理，不能从扩展入口绕过。
  拒绝清单不是“允许参数大全”；未列出的完整长参数仍需符合该命令版本的官方说明。
  下列名称的缩写、下划线写法及 loose-/skip-/disable-/enable-/maximum- 变体也会检查。
  名称含 password/secret/token/encryption-key/encrypt-key/transition-key/passphrase 的参数不接受。
  即使某参数不属于所选命令，命中以下共用保护清单仍会拒绝。

完整保护清单：
""" + listing + "\n\n若被拒绝，先按上面的正常入口设置；仍无法满足时保留报错并联系维护人员，不要修改脚本绕过。\n"


def native_arguments(target, *args, **kwargs):
    """Accept --long-option[=value] tokens and keyword options (lists repeat)."""
    if target not in NATIVE_TARGETS:
        raise ValueError("不支持的原生命令或阶段：" + str(target) + "；可选值：" + ", ".join(sorted(NATIVE_TARGETS)) + "。用 --help-native 查看示例。")
    tokens = list(args)
    for name, value in kwargs.items():
        option = "--" + name.replace("_", "-")
        for item in value if isinstance(value, (list, tuple)) else [value]:
            tokens.append(option if item is None or item is True else option + "=" + ("OFF" if item is False else str(item)))
    for token in tokens:
        if not isinstance(token, str) or not re.fullmatch(r"--[a-zA-Z0-9][a-zA-Z0-9_-]*(?:=[^\x00-\x1f\x7f]*)?", token):
            raise ValueError("原生参数须写为 --完整长参数名[=值]；不接受短参数、位置参数或控制字符。用 --help-native 查看示例。")
        name = token[2:].split("=", 1)[0].replace("_", "-").lower()
        # MySQL accepts prefixes, underscores and loose/skip/disable/enable forms.
        while name.startswith(("loose-", "skip-", "disable-", "enable-", "maximum-")):
            name = name.split("-", 1)[1]
        if any(protected.startswith(name) for protected in NATIVE_PROTECTED) or any(
            secret in name for secret in ("password", "secret", "token", "encryption-key", "encrypt-key", "transition-key", "passphrase")
        ):
            raise ValueError("原生参数 --" + name + " 被拒绝：该选项由工具管理或可能包含凭据。用 --help-native 查找正常配置入口。")
    return tokens


def parse_native_options(entries):
    result = {}
    for entry in entries:
        target, separator, token = entry.partition("=")
        if not separator:
            raise ValueError("--native-option 格式错误：须写为 命令或阶段=--完整长参数名[=值]。用 --help-native 查看示例。")
        result.setdefault(target, []).extend(native_arguments(target, token))
    return result


class BackupError(RuntimeError):
    pass


class StrictArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise BackupError("参数错误：{}".format(message))


def env_path(name, default):
    return os.environ.get(name, default)


def _env_for_system_subprocess():
    env = os.environ.copy()
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        original = env.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            env.pop("LD_LIBRARY_PATH", None)
        else:
            env["LD_LIBRARY_PATH"] = original
    env["MYSQL_TEST_LOGIN_FILE"] = os.environ.get("MYLOGI_LOGIN_FILE", "/root/.mylogin.cnf")
    return env


def configure_logging():
    if LOGGER.handlers:
        return
    os.umask(0o077)
    path = Path(env_path("MYLOGI_LOG_FILE", DEFAULT_LOG_FILE))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.is_symlink():
        raise BackupError("日志文件不能是符号链接：{}".format(path))
    path.touch(mode=0o600, exist_ok=True)
    os.chmod(str(path), 0o600)
    handler = RotatingFileHandler(str(path), maxBytes=20 * 1024 ** 2, backupCount=10, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    LOGGER.addHandler(handler)


def emit(marker, **values):
    text = " ".join([marker] + ["{}={}".format(key, values[key]) for key in sorted(values)])
    print(text, flush=True)
    LOGGER.info(text)


def require_root():
    if os.geteuid() != 0:
        raise BackupError("该操作必须以 root 执行")


def require_executable(path):
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise BackupError("客户端不存在或不可执行：{}".format(path))


def run_command(command, check=True, capture=True, input_text=None):
    result = subprocess.run(
        command,
        check=False,
        input=input_text,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        env=_env_for_system_subprocess(),
    )
    if check and result.returncode != 0:
        detail = (result.stderr or "").strip()
        raise BackupError("命令失败 exit_code={} command={}{}".format(
            result.returncode,
            " ".join(shlex.quote(item) for item in command),
            " stderr=" + detail if detail else "",
        ))
    return result


def parse_version(text, source):
    match = VERSION_RE.search(text)
    if not match:
        raise BackupError("无法解析 {} 版本：{}".format(source, text.strip()))
    return tuple(int(item or 0) for item in match.groups())


def require_mysql_8_or_newer(version, source):
    if version < (8, 0, 0):
        raise BackupError("{} 版本 {}.{}.{} 不受支持；仅支持 MySQL 8.0 及以上".format(
            source, version[0], version[1], version[2]
        ))


def mysql_binary():
    return env_path("MYLOGI_MYSQL_BIN", "/usr/local/bin/mysql")


def mysqldump_binary():
    return env_path("MYLOGI_MYSQLDUMP_BIN", "/usr/local/bin/mysqldump")


def editor_binary():
    return env_path("MYLOGI_MYSQL_CONFIG_EDITOR", "/usr/local/bin/mysql_config_editor")


def mysql_query(login_path, sql, *args, **kwargs):
    result = run_command([
        mysql_binary(), "--login-path=" + login_path, "--batch", "--skip-column-names",
        "--connect-timeout=10", "--execute=" + sql,
    ] + native_arguments("mysql", *args, **kwargs))
    if result.stderr and result.stderr.strip():
        LOGGER.warning("mysql: %s", result.stderr.strip())
    return result.stdout.strip()


def read_login_endpoint(login_path):
    result = run_command([editor_binary(), "print", "--login-path=" + login_path])
    parser = configparser.RawConfigParser()
    try:
        parser.read_string(result.stdout)
        host = parser.get(login_path, "host").strip().strip('"')
        port = int(parser.get(login_path, "port").strip().strip('"'))
        address = ipaddress.ip_address(host)
    except (configparser.Error, ValueError) as exc:
        raise BackupError("登录路径 {} 缺少有效 IPv4 host/port：{}".format(login_path, exc))
    if address.version != 4:
        raise BackupError("登录路径 host 必须是 IPv4 地址：{}".format(host))
    if not 1 <= port <= 65535:
        raise BackupError("登录路径 port 超出范围：{}".format(port))
    return str(address), port


def flatten(values):
    return [item for group in values for item in group]


def validate_database_names(names, option):
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise BackupError("{} 存在重复数据库：{}".format(option, ", ".join(duplicates)))
    invalid = sorted(
        name for name in names
        if not name or name.startswith("-") or any(ord(character) < 32 or ord(character) == 127 for character in name)
    )
    if invalid:
        raise BackupError("{} 包含非法数据库名：{}".format(option, ", ".join(invalid)))


def resolve_selection(args):
    requested = flatten(args.database)
    excluded = flatten(args.exclude_database)
    validate_database_names(requested, "--database")
    validate_database_names(excluded, "--exclude-database")
    available = [line for line in mysql_query(args.login_path, "SHOW DATABASES;", *native_for(args, "mysql")).splitlines() if line]
    unknown = sorted((set(requested) | set(excluded)) - set(available))
    if unknown:
        raise BackupError("数据库不存在或账号不可见：{}".format(", ".join(unknown)))
    forbidden = sorted(set(requested) & SYSTEM_DATABASES)
    if forbidden:
        raise BackupError("不支持显式备份系统数据库：{}".format(", ".join(forbidden)))
    base = requested or [name for name in available if name not in SYSTEM_DATABASES]
    selected = [name for name in base if name not in set(excluded)]
    if not selected:
        raise BackupError("参数计算后的数据库集合为空")
    if not requested and not excluded:
        scope = "all"
    else:
        digest = hashlib.sha256("\0".join(sorted(selected)).encode("utf-8")).hexdigest()
        scope = "selected-" + digest[:12]
    return {"databases": selected, "scope": scope, "compression": args.compression}


def selected_non_innodb(databases, login_path, *options):
    rows = mysql_query(login_path, (
        "SELECT table_schema,table_name,engine FROM information_schema.tables "
        "WHERE table_type='BASE TABLE' AND engine IS NOT NULL AND engine<>'InnoDB' "
        "AND table_schema NOT IN ('information_schema','performance_schema','sys') "
        "AND NOT (table_schema='mysql' AND table_name IN ('general_log','slow_log'));"
    ), *options)
    selected = set(databases)
    found = []
    for row in rows.splitlines():
        columns = row.split("\t")
        if len(columns) == 3 and columns[0] in selected:
            found.append("{}.{}({})".format(*columns))
    return found


def selected_data_size(databases, login_path, *options):
    rows = mysql_query(login_path, (
        "SELECT table_schema,COALESCE(SUM(data_length),0),COALESCE(SUM(index_length),0) "
        "FROM information_schema.tables WHERE table_type='BASE TABLE' GROUP BY table_schema;"
    ), *options)
    selected = set(databases)
    total = 0
    for row in rows.splitlines():
        columns = row.split("\t")
        if len(columns) == 3 and columns[0] in selected:
            try:
                total += int(columns[1]) + int(columns[2])
            except ValueError:
                raise BackupError("无法解析数据库容量：{}".format(row))
    return total


def login_file_preflight():
    path = Path(env_path("MYLOGI_LOGIN_FILE", DEFAULT_LOGIN_FILE))
    if not path.is_file() or path.is_symlink():
        raise BackupError("凭据必须是普通文件：{}".format(path))
    stat = path.stat()
    if stat.st_uid != 0 or stat.st_mode & 0o077:
        raise BackupError("凭据必须由 root 所有且权限为 0600：{}".format(path))


def remove_partials(directory):
    for path in directory.glob(".*.partial"):
        if path.is_file():
            path.unlink()


def previous_sql_size(directory, scope):
    archives = sorted(directory.glob("mysql-{}-*.tar.*".format(scope)), key=lambda p: p.stat().st_mtime)
    if not archives:
        return 0
    try:
        with tarfile.open(str(archives[-1]), "r:*") as archive:
            # SQL is the first member by our writer contract; do not decompress
            # yesterday's entire multi-GB archive just to estimate today's space.
            member = archive.next()
            return member.size if member and member.isfile() and member.name.endswith(".sql") else 0
    except (OSError, tarfile.TarError):
        return 0


def preflight(args):
    require_root()
    for binary in (mysql_binary(), mysqldump_binary(), editor_binary()):
        require_executable(binary)
    login_file_preflight()
    mount = Path(env_path("MYLOGI_BACKUP_MOUNT", DEFAULT_BACKUP_MOUNT))
    if not os.path.ismount(str(mount)):
        raise BackupError("备份文件系统未挂载：{}".format(mount))
    host, port = read_login_endpoint(args.login_path)
    directory = mount / "mysql-{}_{}-backups".format(host, port)
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(str(directory), 0o700)

    server_text = mysql_query(args.login_path, "SELECT @@version;", *native_for(args, "mysql"))
    mysql_text = run_command([mysql_binary(), "--version"]).stdout.strip()
    dump_text = run_command([mysqldump_binary(), "--version"]).stdout.strip()
    server_version = parse_version(server_text, "服务端")
    mysql_version = parse_version(mysql_text, "mysql 客户端")
    dump_version = parse_version(dump_text, "mysqldump 客户端")
    for version, source in ((server_version, "服务端"), (mysql_version, "mysql 客户端"), (dump_version, "mysqldump 客户端")):
        require_mysql_8_or_newer(version, source)
    if mysql_version[:2] != server_version[:2] or dump_version[:2] != server_version[:2]:
        raise BackupError("客户端与服务端主次版本不匹配：server={} mysql={} mysqldump={}".format(
            server_text, mysql_text, dump_text
        ))

    selection = resolve_selection(args)
    warnings = selected_non_innodb(selection["databases"], args.login_path, *native_for(args, "mysql"))
    if warnings:
        LOGGER.warning("非 InnoDB 表不受 --single-transaction 一致性保护：%s", ", ".join(warnings))
    source_size = selected_data_size(selection["databases"], args.login_path, *native_for(args, "mysql"))
    previous_size = previous_sql_size(directory, selection["scope"])
    estimated = max(source_size, previous_size)
    required = int(estimated * SPACE_SAFETY_FACTOR) + SPACE_RESERVE_BYTES
    free = shutil.disk_usage(str(directory)).free
    if free < required:
        raise BackupError("备份空间不足：free={} required={} estimated={}".format(free, required, estimated))
    selection.update({
        "directory": directory,
        "server_version": server_version,
        "free": free,
        "required": required,
    })
    emit(
        "MYSQL_BACKUP_PREFLIGHT_OK",
        databases=json.dumps(selection["databases"], ensure_ascii=False),
        free_bytes=free,
        required_bytes=required,
        scope=selection["scope"],
        server="{}.{}.{}".format(*server_version),
    )
    return selection


def handle_signal(signum, _frame):
    global RECEIVED_SIGNAL
    if RECEIVED_SIGNAL is not None:
        return
    RECEIVED_SIGNAL = signum
    if LOGGER.handlers:
        LOGGER.error("收到终止信号 signal=%s", signal.Signals(signum).name)
    if ACTIVE_PROCESS is not None and ACTIVE_PROCESS.poll() is None:
        try:
            os.killpg(ACTIVE_PROCESS.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def ensure_running():
    if RECEIVED_SIGNAL is not None:
        raise BackupError("操作被信号 {} 终止".format(signal.Signals(RECEIVED_SIGNAL).name))


def relay_stderr(stream):
    previous = time.monotonic()
    try:
        for line in iter(stream.readline, ""):
            if line.rstrip():
                now = time.monotonic()
                LOGGER.info("mysqldump interval_seconds=%.3f: %s", now - previous, line.rstrip())
                previous = now
    finally:
        stream.close()


def build_dump_command(selection, sql_path, login_path, *args, **kwargs):
    command = [
        mysqldump_binary(), "--login-path=" + login_path, "--comments", "--verbose", "--single-transaction",
        "--quick", "--hex-blob", "--routines", "--events", "--triggers", "--source-data=2",
        "--set-gtid-purged=OFF", "--skip-extended-insert", "--default-character-set=utf8mb4",
        "--ignore-table=mysql.general_log", "--ignore-table=mysql.slow_log",
        "--result-file=" + str(sql_path),
    ]
    return command + native_arguments("mysqldump", *args, **kwargs) + ["--databases"] + selection["databases"]


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 ** 2), b""):
            ensure_running()
            digest.update(block)
    return digest.hexdigest()


def validate_footer(path):
    with path.open("rb") as source:
        source.seek(max(0, path.stat().st_size - 4096))
        tail = source.read()
    if not re.search(br"(?m)^-- Dump completed(?: on .*)?$", tail):
        raise BackupError("备份文件缺少 mysqldump 完成标记")


def fsync_directory(path):
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class HashingReader:
    """Hash the exact bytes consumed by tarfile, with bounded progress logs."""
    def __init__(self, source, phase):
        self.source = source
        self.digest = hashlib.sha256()
        self.phase = phase
        self.started = self.last = time.monotonic()
        self.size = 0
        LOGGER.info("MYSQL_BACKUP_PHASE_START phase=%s", phase)

    def read(self, size=-1):
        ensure_running()
        block = self.source.read(size)
        self.digest.update(block)
        self.size += len(block)
        now = time.monotonic()
        if now - self.last >= HEARTBEAT_SECONDS:
            LOGGER.info("MYSQL_BACKUP_HEARTBEAT phase=%s elapsed_seconds=%d bytes=%d", self.phase, int(now - self.started), self.size)
            self.last = now
        return block

    def finish(self):
        elapsed = time.monotonic() - self.started
        LOGGER.info("MYSQL_BACKUP_PHASE_DONE phase=%s elapsed_seconds=%.3f bytes=%d bytes_per_second=%.0f",
                    self.phase, elapsed, self.size, self.size / max(elapsed, 0.001))
        return self.digest.hexdigest()


def write_archive(sql_path, archive_path, sql_name, digest, compression):
    mode = {"gz": "w:gz", "bz2": "w:bz2", "xz": "w:xz"}[compression]
    with tarfile.open(str(archive_path), mode, copybufsize=1024 ** 2) as archive:
        info = archive.gettarinfo(str(sql_path), arcname=sql_name)
        with sql_path.open("rb") as source:
            reader = HashingReader(source, "compress")
            archive.addfile(info, reader)
            calculated = reader.finish()
        if digest is not None and calculated != digest:
            raise BackupError("SQL SHA-256 在压缩时变化")
        digest = calculated
        checksum = "{}  {}\n".format(digest, sql_name).encode("ascii")
        info = tarfile.TarInfo(sql_name + ".sha256")
        info.size = len(checksum)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(checksum))
    # Stream once through the complete archive; checking names first would
    # decompress the SQL an extra time on large backups.
    opener = {"gz": gzip.open, "bz2": bz2.open, "xz": lzma.open}[compression]
    with opener(str(archive_path), "rb") as compressed:
        with tarfile.open(fileobj=compressed, mode="r|") as archive:
            member = archive.next()
            if member is None or member.name != sql_name or not member.isfile():
                raise BackupError("归档缺少 SQL")
            with archive.extractfile(member) as source:
                reader = HashingReader(source, "verify")
                while reader.read(1024 ** 2):
                    pass
                actual = reader.finish()
            member = archive.next()
            if member is None or member.name != sql_name + ".sha256" or not member.isfile() or member.size != len(checksum):
                raise BackupError("归档缺少 SHA-256")
            with archive.extractfile(member) as source:
                recorded = source.read()
            if actual != digest or recorded != checksum:
                raise BackupError("归档 SHA-256 回读不一致")
            if archive.next() is not None:
                raise BackupError("归档内容不符合 SQL + SQL.sha256 合同")
        # Drain the compression decoder, including its trailer/checksum.
        # tar's end blocks alone do not validate a gzip CRC or truncated trailer.
        while compressed.read(1024 ** 2):
            ensure_running()
    return digest


def enforce_retention(directory, scope):
    candidates = sorted(
        directory.glob("mysql-{}-*.tar.*".format(scope)),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for path in candidates[MAX_BACKUP_FILES:]:
        path.unlink()


def backup(args):
    global ACTIVE_PROCESS
    selection = preflight(args)
    lock_path = Path(env_path("MYLOGI_LOCK_FILE", DEFAULT_LOCK_FILE))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("已有逻辑备份持有锁：{}".format(lock_path))
        emit("MYSQL_BACKUP_LOCK_ACQUIRED", lock=lock_path, pid=os.getpid())
        directory = selection["directory"]
        remove_partials(directory)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        base = "mysql-{}-{}".format(selection["scope"], timestamp)
        sql_name = base + ".sql"
        sql_path = directory / ("." + sql_name + ".partial")
        archive_partial = directory / ("." + base + ".tar." + args.compression + ".partial")
        archive_final = directory / (base + ".tar." + args.compression)
        try:
            command = build_dump_command(selection, sql_path, args.login_path, *native_for(args, "mysqldump"))
            LOGGER.info("阶段=mysqldump scope=%s databases=%s", selection["scope"], selection["databases"])
            ACTIVE_PROCESS = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                env=_env_for_system_subprocess(),
                start_new_session=True,
            )
            stderr_thread = threading.Thread(target=relay_stderr, args=(ACTIVE_PROCESS.stderr,), daemon=True)
            stderr_thread.start()
            started = time.monotonic()
            next_heartbeat = started + HEARTBEAT_SECONDS
            while ACTIVE_PROCESS.poll() is None:
                ensure_running()
                if time.monotonic() >= next_heartbeat:
                    size = sql_path.stat().st_size if sql_path.exists() else 0
                    LOGGER.info("MYSQL_BACKUP_HEARTBEAT pid=%d elapsed_seconds=%d bytes=%d", ACTIVE_PROCESS.pid, int(time.monotonic() - started), size)
                    next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
                time.sleep(0.2)
            returncode = ACTIVE_PROCESS.wait()
            stderr_thread.join(timeout=5)
            ACTIVE_PROCESS = None
            ensure_running()
            if returncode != 0:
                raise BackupError("mysqldump 失败 exit_code={}".format(returncode))
            if not sql_path.is_file() or sql_path.stat().st_size == 0:
                raise BackupError("mysqldump 未生成有效备份")
            validate_footer(sql_path)
            LOGGER.info("MYSQL_BACKUP_PHASE_DONE phase=mysqldump elapsed_seconds=%.3f bytes=%d",
                        time.monotonic() - started, sql_path.stat().st_size)
            digest = write_archive(sql_path, archive_partial, sql_name, None, args.compression)
            os.chmod(str(archive_partial), 0o600)
            with archive_partial.open("rb") as source:
                os.fsync(source.fileno())
            os.replace(str(archive_partial), str(archive_final))
            fsync_directory(directory)
            sql_path.unlink()
            enforce_retention(directory, selection["scope"])
            emit("MYSQL_BACKUP_OK", archive=archive_final.name, scope=selection["scope"], sha256=digest)
        finally:
            if ACTIVE_PROCESS is not None and ACTIVE_PROCESS.poll() is None:
                try:
                    os.killpg(ACTIVE_PROCESS.pid, signal.SIGTERM)
                    ACTIVE_PROCESS.wait(timeout=TERMINATE_GRACE_SECONDS)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(ACTIVE_PROCESS.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                ACTIVE_PROCESS = None
            for path in (sql_path, archive_partial):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


def setup_login(args):
    require_root()
    require_executable(editor_binary())
    try:
        address = ipaddress.ip_address(args.host)
    except ValueError as exc:
        raise BackupError("--host 必须是有效 IPv4 地址：{}".format(exc))
    if address.version != 4:
        raise BackupError("--host 必须是 IPv4 地址：{}".format(args.host))
    login_file = Path(env_path("MYLOGI_LOGIN_FILE", DEFAULT_LOGIN_FILE))
    if login_file.exists() and login_file.is_symlink():
        raise BackupError("凭据文件不能是符号链接：{}".format(login_file))
    command = [
        editor_binary(), "set", "--login-path=" + args.login_path,
        "--host=" + str(address), "--port=" + str(args.port),
        "--user=" + args.user, "--password",
    ]
    run_command(command, capture=False)
    if not login_file.is_file():
        raise BackupError("mysql_config_editor 未生成凭据文件：{}".format(login_file))
    os.chown(str(login_file), 0, 0)
    os.chmod(str(login_file), 0o600)
    emit("MYSQL_BACKUP_LOGIN_PATH_OK", host=args.host, login_path=args.login_path, port=args.port, user=args.user)


def atomic_write(path, content, mode=0o644):
    temporary = path.with_name("." + path.name + ".partial")
    with temporary.open("w", encoding="utf-8") as target:
        target.write(content)
        target.flush()
        os.fsync(target.fileno())
    os.chmod(str(temporary), mode)
    os.replace(str(temporary), str(path))
    fsync_directory(path.parent)


def validated_exec_path(value):
    supplied = Path(value)
    if not supplied.is_absolute():
        raise BackupError("--exec-path 必须是可执行的绝对文件：{}".format(value))
    path = supplied.resolve()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise BackupError("--exec-path 必须是可执行的绝对文件：{}".format(value))
    return path


def timer_backup_arguments(args):
    command = ["backup", "--login-path", args.login_path, "--compression", args.compression]
    for group in args.database:
        command.extend(["--database"] + group)
    for group in args.exclude_database:
        command.extend(["--exclude-database"] + group)
    for item in getattr(args, "native_option", []):
        command.extend(["--native-option", item])
    return " ".join(shlex.quote(item) for item in command)


def systemd_quote(value):
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise BackupError("systemd 参数包含控制字符")
    return '\"' + value.replace("\\", "\\\\").replace('\"', '\\\"').replace("%", "%%").replace("$", "$$") + '\"'


def native_for(args, target):
    return parse_native_options(getattr(args, "native_option", [])).get(target, [])


def timer_install(args):
    require_root()
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", args.at):
        raise BackupError("--at 必须是 HH:MM，且范围为 00:00-23:59")
    executable = validated_exec_path(args.exec_path)
    unit_dir = Path(env_path("MYLOGI_SYSTEMD_UNIT_DIR", DEFAULT_UNIT_DIR))
    unit_dir.mkdir(parents=True, exist_ok=True)
    command = " ".join(systemd_quote(item) for item in [str(executable)] + shlex.split(timer_backup_arguments(args)))
    service = """[Unit]\nDescription=MySQL logical backup\nWants=network-online.target\nAfter=network-online.target\n\n[Service]\nType=oneshot\nUser=root\nGroup=root\nUMask=0077\nEnvironment=HOME=/root\nExecStart={}\nTimeoutStartSec=infinity\nKillMode=control-group\nStandardOutput=journal\nStandardError=journal\n""".format(command)
    timer = """[Unit]\nDescription=Run MySQL logical backup daily\n\n[Timer]\nOnCalendar=*-*-* {}:00\nPersistent=true\nAccuracySec=1min\nUnit={}\n\n[Install]\nWantedBy=timers.target\n""".format(args.at, SERVICE_NAME)
    service_path = unit_dir / SERVICE_NAME
    timer_path = unit_dir / TIMER_NAME
    atomic_write(service_path, service)
    atomic_write(timer_path, timer)
    analyze = env_path("MYLOGI_SYSTEMD_ANALYZE", "/usr/bin/systemd-analyze")
    systemctl = env_path("MYLOGI_SYSTEMCTL", "/usr/bin/systemctl")
    require_executable(analyze)
    require_executable(systemctl)
    run_command([analyze, "verify", str(service_path), str(timer_path)])
    run_command([systemctl, "daemon-reload"])
    run_command([systemctl, "enable", "--now", TIMER_NAME])
    emit("MYSQL_BACKUP_TIMER_INSTALLED", at=args.at, timer=TIMER_NAME)


def timer_status(_args):
    require_root()
    systemctl = env_path("MYLOGI_SYSTEMCTL", "/usr/bin/systemctl")
    require_executable(systemctl)
    enabled = run_command([systemctl, "is-enabled", TIMER_NAME], check=False).returncode
    active = run_command([systemctl, "is-active", TIMER_NAME], check=False).returncode
    run_command([systemctl, "list-timers", "--all", TIMER_NAME], check=False, capture=False)
    if enabled != 0 or active != 0:
        raise BackupError("timer 未同时处于 enabled/active 状态")
    emit("MYSQL_BACKUP_TIMER_OK", timer=TIMER_NAME)


def timer_remove(_args):
    require_root()
    systemctl = env_path("MYLOGI_SYSTEMCTL", "/usr/bin/systemctl")
    require_executable(systemctl)
    unit_dir = Path(env_path("MYLOGI_SYSTEMD_UNIT_DIR", DEFAULT_UNIT_DIR))
    run_command([systemctl, "disable", "--now", TIMER_NAME], check=False)
    for name in (SERVICE_NAME, TIMER_NAME):
        try:
            (unit_dir / name).unlink()
        except FileNotFoundError:
            pass
    run_command([systemctl, "daemon-reload"])
    run_command([systemctl, "reset-failed", SERVICE_NAME], check=False)
    emit("MYSQL_BACKUP_TIMER_REMOVED", timer=TIMER_NAME)


def add_scope_arguments(parser):
    parser.add_argument("--help-native", action="version", version=native_option_help(),
                        help="查看原生参数的命令说明、完整示例、定时保存及拒绝清单；无需连接数据库")
    parser.add_argument("--native-option", action="append", default=[], metavar="TARGET=--OPTION[=VALUE]",
                        help="给 mysql 查询或 mysqldump 导出追加一个原生长参数；可重复。示例和限制见 --help-native")
    parser.add_argument("--login-path", default=DEFAULT_LOGIN_PATH, help="mysql_config_editor 登录路径；默认 mysql-backup")
    parser.add_argument("--database", action="append", nargs="+", default=[], metavar="NAME", help="只备份指定数据库；可连续指定或重复使用")
    parser.add_argument("--exclude-database", action="append", nargs="+", default=[], metavar="NAME", help="从备份范围排除数据库；可连续指定或重复使用")
    parser.add_argument("--compression", choices=("gz", "bz2", "xz"), default="gz", help="tar 压缩格式；默认 gz")


def create_parser():
    examples = """完整示例：
  # 1. 首次创建加密登录路径（密码交互输入，不进入 argv）
  sudo MyLogiBackupCli setup-login --host 192.168.103.101 --port 3306 --user mysql_backup

  # 2. 校验 MySQL 8.0+ 客户端、凭据、挂载、连接、版本、库范围和空间
  sudo MyLogiBackupCli preflight

  # 3. 默认全库备份，或选择/排除数据库并切换压缩格式
  sudo MyLogiBackupCli backup
  sudo MyLogiBackupCli backup --database app audit --compression bz2
  sudo MyLogiBackupCli backup --exclude-database test archive --compression xz
  sudo MyLogiBackupCli backup --database app --native-option mysqldump=--extended-insert
  sudo MyLogiBackupCli backup --native-option mysql=--ssl-mode=VERIFY_IDENTITY --native-option mysqldump=--ssl-mode=VERIFY_IDENTITY

  原生参数可选：mysql=... 只作用于前置查询，mysqldump=... 只作用于导出。
  每个参数重复写 --native-option；值用 = 连接；含空格时给整个值加引号。
  不填写时保留默认值；extended-insert 只改变 INSERT 输出，不消除查询等待或 1412。
  拒绝清单、正常配置入口和定时任务保存示例：MyLogiBackupCli --help-native。
  --single-transaction 不隔离 DDL；备份窗口内应协调 ALTER/DROP/RENAME/TRUNCATE。
  日志保留 mysqldump 表阶段及相邻事件间隔、导出/压缩/校验耗时与字节数。
  1412 不能单凭错误文字唯一归因于 TRUNCATE；失败不发布归档，也不自动重试。

  # 4. 安装每天 01:01 执行的 timer，查看状态并移除
  sudo MyLogiBackupCli timer install --at 01:01 --exec-path /usr/local/bin/MyLogiBackupCli --compression gz
  sudo MyLogiBackupCli timer status
  sudo MyLogiBackupCli timer remove

运行合同：
  仅支持 MySQL 8.0 及以上；mysql、mysqldump 与服务端必须为相同主次版本。
  默认二进制位于 /usr/local/bin；备份根目录 /backups 必须是独立挂载点。
  凭据文件 /root/.mylogin.cnf 必须由 root 所有且权限为 0600。
  环境覆盖：MYLOGI_MYSQL_BIN、MYLOGI_MYSQLDUMP_BIN、MYLOGI_MYSQL_CONFIG_EDITOR、
  MYLOGI_BACKUP_MOUNT、MYLOGI_LOGIN_FILE、MYLOGI_LOCK_FILE、MYLOGI_LOG_FILE。
  成功标记：MYSQL_BACKUP_LOGIN_PATH_OK、MYSQL_BACKUP_PREFLIGHT_OK、MYSQL_BACKUP_LOCK_ACQUIRED、MYSQL_BACKUP_OK、
  MYSQL_BACKUP_TIMER_INSTALLED、MYSQL_BACKUP_TIMER_OK、MYSQL_BACKUP_TIMER_REMOVED。
"""
    native_examples = """原生参数示例与判断：
  MyLogiBackupCli preflight --database app --native-option mysql=--connect-timeout=15
  MyLogiBackupCli backup --database app --native-option mysqldump=--extended-insert
  mysql 只作用于检查查询；mysqldump 只作用于导出；不会互相复制参数。
  多个参数重复写 --native-option；带值选项用 =，含空格时给整个值加引号。
  凭据用 setup-login，库范围用 --database/--exclude-database；禁止从原生参数覆盖。
  timer install 须重新声明全部备份选项和原生参数，不继承上次 backup 参数。
  完整命令说明、定时保存、拒绝清单及替代入口：MyLogiBackupCli --help-native
"""
    parser = StrictArgumentParser(
        prog="MyLogiBackupCli",
        description="MySQL 8.0+ 生产级逻辑备份：凭据、前置校验、原子归档、轮换和 systemd timer。",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--help-native", action="version", version=native_option_help(),
                        help="查看原生参数命令、示例、定时保存及拒绝清单；无需连接数据库")
    parser.add_argument("--version", action="version", version="%(prog)s " + VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup-login", help="交互创建 mysql_config_editor 登录路径", allow_abbrev=False)
    setup.add_argument("--host", required=True, help="MySQL IPv4 地址")
    setup.add_argument("--port", type=int, default=3306, choices=range(1, 65536), metavar="PORT")
    setup.add_argument("--user", required=True, help="备份账号")
    setup.add_argument("--login-path", default=DEFAULT_LOGIN_PATH)
    setup.set_defaults(handler=setup_login)

    preflight_parser = subparsers.add_parser("preflight", help="执行全部只读前置检查", allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=native_examples)
    add_scope_arguments(preflight_parser)
    preflight_parser.set_defaults(handler=lambda args: preflight(args))

    backup_parser = subparsers.add_parser("backup", help="执行逻辑备份并原子发布归档", allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=native_examples)
    add_scope_arguments(backup_parser)
    backup_parser.set_defaults(handler=backup)

    timer = subparsers.add_parser("timer", help="管理 systemd service/timer", allow_abbrev=False)
    timer_subparsers = timer.add_subparsers(dest="timer_command", required=True)
    install = timer_subparsers.add_parser("install", help="安装并启动每日 timer", allow_abbrev=False,
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=native_examples)
    install.add_argument("--at", default="01:01", help="每日执行时间 HH:MM；默认 01:01")
    install.add_argument("--exec-path", required=True, help="MyLogiBackupCli 可执行文件绝对路径")
    add_scope_arguments(install)
    install.set_defaults(handler=timer_install)
    status = timer_subparsers.add_parser("status", help="检查 timer enabled/active 状态", allow_abbrev=False)
    status.set_defaults(handler=timer_status)
    remove = timer_subparsers.add_parser("remove", help="停用并删除 service/timer", allow_abbrev=False)
    remove.set_defaults(handler=timer_remove)
    return parser


def main(argv=None):
    global RECEIVED_SIGNAL
    RECEIVED_SIGNAL = None
    handled_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGQUIT, signal.SIGTERM)
    previous_handlers = {item: signal.getsignal(item) for item in handled_signals}
    for item in handled_signals:
        signal.signal(item, handle_signal)
    try:
        args = create_parser().parse_args(argv)
        parse_native_options(getattr(args, "native_option", []))
        configure_logging()
        args.handler(args)
        return 0
    except (BackupError, ValueError, OSError, EOFError, tarfile.TarError, lzma.LZMAError, zlib.error) as exc:
        if LOGGER.handlers:
            LOGGER.error("MYSQL_BACKUP_FAILED error=%s", exc)
        print("MYSQL_BACKUP_FAILED error={}".format(exc), file=sys.stderr)
        return 130 if RECEIVED_SIGNAL in (signal.SIGINT, signal.SIGTERM) else 1
    except KeyboardInterrupt:
        print("MYSQL_BACKUP_FAILED error=interrupted", file=sys.stderr)
        return 130
    finally:
        for item, previous in previous_handlers.items():
            signal.signal(item, previous)


if __name__ == "__main__":
    raise SystemExit(main())

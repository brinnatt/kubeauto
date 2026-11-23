#!/usr/bin/env python3
"""独立 StarRocks 3.5.7 离线逻辑迁移工具；不修改源端或自动写入目标端。"""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
import datetime as dt
import fnmatch
import getpass
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import threading
import time
import uuid

VERSION = "2.0.0"
FORMAT_VERSION = 1
SYSTEM_DATABASES = {"information_schema", "sys", "_statistics_", "starrocks_monitor"}
NUMERIC_TYPES = {
    "tinyint", "smallint", "int", "integer", "bigint", "largeint", "float", "double",
    "decimal", "decimalv2", "decimal32", "decimal64", "decimal128",
}
SUPPORTED_TYPES = NUMERIC_TYPES | {
    "boolean", "bool", "char", "varchar", "string", "date", "datetime", "json", "varbinary",
}
NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z", re.ASCII)
SESSION_SQL = "SET sql_mode = '';\nSET enable_insert_strict = true;\nSET insert_max_filter_ratio = 0;\n"
HASH_MOD = 1 << 256


class MigrationError(Exception):
    """可直接展示给用户的操作错误，不包含查询返回值或密码。"""


def failure_text(error):
    if isinstance(error, MigrationError):
        return str(error)
    if isinstance(error, OSError):
        return "文件/网络操作失败（%s），请检查路径权限、空间或网络" % type(error).__name__
    # Raw database messages can contain credentials or row values.
    code = error.args[0] if error.args and isinstance(error.args[0], int) else "unknown"
    hints = {1045: "认证失败", 1044: "数据库访问被拒绝", 1142: "表操作权限不足",
             1064: "SQL语法或版本兼容问题", 2003: "连接失败", 2006: "服务端连接已关闭",
             2013: "查询期间连接中断"}
    return "%s，类型 %s，数据库错误码 %s" % (hints.get(code, "操作失败"), type(error).__name__, code)


def ident(value):
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise MigrationError("数据库、表、列名称不能为空或包含控制字符")
    return "`" + value.replace("`", "``") + "`"


def sql_string(value):
    escapes = {"\\": "\\\\", "'": "\\'", "\0": "\\0", "\n": "\\n", "\r": "\\r",
               "\t": "\\t", "\b": "\\b", "\x1a": "\\Z"}
    return "'" + "".join(escapes.get(c, c) for c in value) + "'"


def literal(value, data_type):
    if value is None:
        return "NULL"
    if data_type == "varbinary":
        if not isinstance(value, (bytes, bytearray)):
            raise MigrationError("VARBINARY 未以二进制返回")
        return "unhex('" + value.hex() + "')"
    if data_type in {"boolean", "bool"}:
        text = str(value).lower()
        if text in {"true", "1"}:
            return "TRUE"
        if text in {"false", "0"}:
            return "FALSE"
        raise MigrationError("无法无损导出 BOOLEAN")
    if data_type in NUMERIC_TYPES:
        if isinstance(value, float) and not math.isfinite(value):
            raise MigrationError("SQL 逻辑迁移不支持 NaN/Infinity")
        text = str(value)
        if not NUMBER.fullmatch(text):
            raise MigrationError("无法无损导出数值类型 " + data_type)
        return "CAST(" + sql_string(text) + " AS LARGEINT)" if data_type == "largeint" else text
    if isinstance(value, dt.datetime):
        value = value.isoformat(sep=" ")
    elif isinstance(value, dt.date):
        value = value.isoformat()
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if not isinstance(value, str):
        raise MigrationError("无法无损导出返回类型 " + type(value).__name__)
    text = sql_string(value)
    return "parse_json(" + text + ")" if data_type == "json" else text


def row_sql(row, columns):
    if len(row) != len(columns):
        raise MigrationError("查询返回列数与预检不一致，停止以避免截断")
    return "(" + ",".join(literal(v, c["type"]) for v, c in zip(row, columns)) + ")"


def sql_tokens(sql):
    """带偏移的 SQL 词法单元；属性修改不触碰注释、列默认值或字符串。"""
    i = 0
    while i < len(sql):
        start, c = i, sql[i]
        if c.isspace():
            i += 1
            continue
        if sql.startswith("--", i) or c == "#":
            end = sql.find("\n", i)
            i = len(sql) if end < 0 else end + 1
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            if end < 0:
                raise MigrationError("DDL 包含未闭合注释")
            i = end + 2
            continue
        if c in "'\"`":
            quote = c
            i += 1
            value = []
            while i < len(sql):
                if sql[i] == "\\" and quote != "`" and i + 1 < len(sql):
                    value.append(sql[i:i + 2])
                    i += 2
                elif sql[i] == quote:
                    if i + 1 < len(sql) and sql[i + 1] == quote:
                        value.append(quote)
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    value.append(sql[i])
                    i += 1
            else:
                raise MigrationError("DDL 包含未闭合引号")
            yield (quote, "".join(value), start, i)
        elif c.isalnum() or c == "_":
            i += 1
            while i < len(sql) and (sql[i].isalnum() or sql[i] == "_"):
                i += 1
            yield ("word", sql[start:i], start, i)
        else:
            i += 1
            yield (c, c, start, i)


def ddl_properties(ddl):
    tokens = list(sql_tokens(ddl))
    # Native range bounds use [(lower), (upper)), so counting only parentheses
    # cannot locate the table-level clause. SHOW CREATE puts table PROPERTIES
    # last; quoted text is already excluded by the lexer.
    starts = [i + 1 for i, token in enumerate(tokens[:-1])
              if token[0] == "word" and token[1].upper() == "PROPERTIES"
              and tokens[i + 1][0] == "("]
    if not starts:
        raise MigrationError("没有找到普通内表 PROPERTIES 块")
    result = {}
    i = starts[-1] + 1
    while i < len(tokens) and tokens[i][0] != ")":
        if i + 2 >= len(tokens) or tokens[i][0] not in {'"', "'"} or tokens[i + 1][0] != "=" or tokens[i + 2][0] not in {'"', "'"}:
            raise MigrationError("无法安全解析普通内表属性")
        key = tokens[i][1].lower()
        if key in result:
            raise MigrationError("DDL 包含重复属性 " + key)
        result[key] = tokens[i + 2]
        i += 3
        if i >= len(tokens):
            raise MigrationError("普通内表属性块未闭合")
        if tokens[i][0] == ",":
            i += 1
        elif tokens[i][0] != ")":
            raise MigrationError("无法安全解析普通内表属性分隔符")
    if i >= len(tokens) or any(t[0] != ";" for t in tokens[i + 1:]):
        raise MigrationError("普通内表属性块未闭合或不是最后一个子句")
    return result


def prepare_ddl(original, target_db, name, replication_num=None, with_data=True):
    ddl = original.strip().rstrip(";")
    pattern = r"^CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:`(?:``|[^`])+`\s*\.\s*)?`(?:``|[^`])+`"
    ddl, count = re.subn(pattern, lambda _: "CREATE TABLE " + ident(target_db) + "." + ident(name),
                         ddl, count=1, flags=re.I)
    if count != 1:
        raise MigrationError(name + ": 非预期 SHOW CREATE TABLE 格式")
    properties = ddl_properties(ddl)
    replacements = []
    if replication_num is not None:
        if "replication_num" not in properties:
            raise MigrationError(name + ": 当前表没有 replication_num，不能修改副本数")
        for key in ("replication_num", "dynamic_partition.replication_num"):
            if key in properties:
                token = properties[key]
                replacements.append((token[2], token[3], '"' + str(replication_num) + '"'))
    dynamic = properties.get("dynamic_partition.enable")
    deferred = bool(with_data and dynamic and dynamic[1].lower() == "true")
    if deferred:
        replacements.append((dynamic[2], dynamic[3], '"false"'))
    if with_data:
        for key in ("partition_live_number", "partition_ttl_number", "partition_ttl", "partition_retention_condition"):
            if key in properties and properties[key][1].strip() not in {"", "0", "-1"}:
                raise MigrationError(name + ": 存在分区保留策略 " + key + "；暂不支持数据迁移，请显式排除此表或仅导结构")
    for start, end, value in sorted(replacements, reverse=True):
        ddl = ddl[:start] + value + ddl[end:]
    return ddl + ";\n", deferred


def all_rows(conn, statement, args=None):
    with conn.cursor() as cur:
        cur.execute(statement, args)
        return cur.fetchall()


def inventory(conn, db):
    return all_rows(conn, "SELECT TABLE_NAME, TABLE_TYPE, ENGINE FROM information_schema.tables "
                          "WHERE TABLE_SCHEMA=%s ORDER BY TABLE_NAME", (db,))


def columns_for(conn, db, table):
    # information_schema exposes MySQL aliases: LARGEINT becomes "bigint
    # unsigned" and BOOLEAN becomes "tinyint" in 3.5.7. DESC retains native types.
    rows = all_rows(conn, "DESC " + ident(db) + "." + ident(table))
    if any(len(row) != 6 for row in rows):
        raise MigrationError("DESC 返回结构与 StarRocks 3.5.7 普通内表契约不一致")
    return [dict(name=row[0], type=str(row[1]).split("(", 1)[0].strip().lower(),
                 extra=str(row[5] or "")) for row in rows]


def matches(pattern, db, table):
    # 未带库名的模式对每个选中库生效；名称本身含点号时请使用完整模式。
    return fnmatch.fnmatchcase(db + "." + table if "." in pattern else table, pattern)


def inspect(conn, args, progress):
    visible = [row[0] for row in all_rows(conn, "SHOW DATABASES")]
    requested = list(dict.fromkeys((args.database or []) + (args.databases or [])))
    if args.all_databases:
        requested = sorted(db for db in visible if db.lower() not in SYSTEM_DATABASES)
    missing = set(requested) - set(visible)
    if missing:
        raise MigrationError("数据库不存在或不可见: " + ", ".join(sorted(missing)))
    databases = [db for db in requested if not any(fnmatch.fnmatchcase(db, p) for p in args.exclude_database)]
    if not databases:
        raise MigrationError("筛选结果没有数据库")
    mapping = {}
    for expression in args.database_map:
        if "=" not in expression:
            raise MigrationError("--database-map 格式为 源库=目标库")
        source, target = expression.split("=", 1)
        if source not in databases or source in mapping:
            raise MigrationError("库映射的源库未选中或重复: " + source)
        ident(target)
        mapping[source] = target
    if args.target_database:
        if len(databases) != 1 or mapping:
            raise MigrationError("--target-database 只适用于单库，且不能与 --database-map 同用")
        mapping[databases[0]] = args.target_database
    targets = [mapping.get(db, db) for db in databases]
    if len({name.casefold() for name in targets}) != len(targets):
        raise MigrationError("多个源库不能映射到同名目标库")
    for name in databases + targets:
        ident(name)
        if name.lower() in SYSTEM_DATABASES:
            raise MigrationError("不允许迁移系统库: " + name)
    tables, errors, seen_patterns = [], [], set()
    for db in databases:
        for name, kind, engine in inventory(conn, db):
            included = not args.table
            for pattern in args.table:
                if matches(pattern, db, name):
                    included = True
                    seen_patterns.add(pattern)
            if not included or any(matches(p, db, name) for p in args.exclude_table):
                continue
            progress.update(phase="检查结构", table=db + "." + name)
            ident(name)
            ref = db + "." + name
            if str(kind).upper() != "BASE TABLE" or str(engine).upper() not in {"OLAP", "STARROCKS"}:
                errors.append(ref + ": 当前只支持普通内表，实际 " + str(kind) + "/" + str(engine) + "；请显式 --exclude-table")
                continue
            original = all_rows(conn, "SHOW CREATE TABLE " + ident(db) + "." + ident(name))[0][1]
            columns = columns_for(conn, db, name)
            if not columns:
                errors.append(ref + ": 没有可见列")
                continue
            for column in columns:
                ident(column["name"])
                if not args.schema_only and column["type"] not in SUPPORTED_TYPES:
                    errors.append(ref + "." + column["name"] + ": 未适配数据类型 " + column["type"])
                if not args.schema_only and "GENERATED" in column["extra"].upper():
                    errors.append(ref + ": 数据迁移暂不支持生成列")
            if not args.schema_only and re.search(r"(?im)^\s*`(?:``|[^`])+`[^\n]*\bAS\s*\(", original):
                errors.append(ref + ": 数据迁移暂不支持生成列")
            try:
                ddl, dynamic = prepare_ddl(original, mapping.get(db, db), name,
                                           args.replication_num, not args.schema_only)
            except MigrationError as error:
                errors.append(ref + ": " + str(error))
                continue
            number = len(tables) + 1
            filename = "table-%06d.sql%s" % (number, ".gz" if args.gzip else "")
            tables.append(dict(database=db, target_database=mapping.get(db, db), name=name,
                               source_ddl=original, ddl=ddl, dynamic_disabled=dynamic,
                               columns=columns, file=filename))
    for pattern in set(args.table) - seen_patterns:
        errors.append("--table 没有匹配对象: " + pattern)
    if errors:
        raise MigrationError("预检未通过，未导出任何数据:\n" + "\n".join(errors))
    if not tables:
        raise MigrationError("筛选结果没有普通内表；不会生成空迁移包")
    return dict(databases=[dict(source=db, target=mapping.get(db, db)) for db in databases], tables=tables)


def read_password(args):
    if args.password_file:
        path = args.password_file
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise MigrationError("密码文件必须为普通文件，不能为符号链接")
        if os.name != "nt" and (info.st_mode & 0o077 or info.st_uid != os.getuid()):
            raise MigrationError("密码文件必须属于当前用户且权限不高于 0600")
        if info.st_size > 65536:
            raise MigrationError("密码文件过大")
        return path.read_text(encoding="utf-8").removesuffix("\n").removesuffix("\r")
    if args.password_env in os.environ:
        return os.environ[args.password_env]
    if not sys.stdin.isatty():
        raise MigrationError("非交互运行请设置 " + args.password_env + " 或 --password-file；空密码也需显式设置空环境变量")
    return getpass.getpass("StarRocks 密码（不回显）: ")


class Source:
    def __init__(self, args):
        try:
            import pymysql
        except ImportError:
            raise MigrationError("缺少依赖，请执行 python3 -m pip install 'PyMySQL==1.1.2'") from None
        self.driver = pymysql
        self.connection_factory = pymysql.connect
        self.options = dict(host=args.host, port=args.port, user=args.user, password=read_password(args),
                            charset="utf8mb4", autocommit=True, connect_timeout=args.connect_timeout,
                            read_timeout=args.query_timeout + 30, write_timeout=30)
        if args.ssl_ca:
            self.options.update(ssl_ca=str(args.ssl_ca), ssl_verify_cert=True, ssl_verify_identity=True)
            # PyMySQL 1.1.2 otherwise skips TLS when the server omits CLIENT.SSL.
            # Refuse before authentication, rather than checking after credentials were sent.
            class TLSConnection(pymysql.connections.Connection):
                def _request_authentication(self):
                    if not self.server_capabilities & pymysql.constants.CLIENT.SSL:
                        raise MigrationError("服务端未声明 TLS 能力，拒绝降级为明文认证")
                    return super()._request_authentication()
            self.connection_factory = TLSConnection
        self.timeout = args.query_timeout
        self.lock = threading.Lock()
        self.connections = set()
        self.cancelled = False

    def abort(self):
        """唤醒等待网络的工作线程，避免线程池退出时等待整个查询超时。"""
        with self.lock:
            self.cancelled = True
            connections = list(self.connections)
        for conn in connections:
            # PyMySQL 1.1.2 owns this socket; shutdown wakes its blocked recv().
            sock = getattr(conn, "_sock", None)
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    @contextmanager
    def connect(self):
        conn = self.connection_factory(**self.options)
        with self.lock:
            if self.cancelled:
                conn.close()
                raise MigrationError("迁移已取消")
            self.connections.add(conn)
        try:
            all_rows(conn, "SET query_timeout = " + str(self.timeout))
            # Fix SHOW CREATE output mode as well as INSERT literal mode.
            all_rows(conn, "SET sql_mode = ''")
            version = str(all_rows(conn, "SELECT current_version()")[0][0])
            if not re.fullmatch(r"3\.5\.7(?:[-+ ].*)?", version):
                raise MigrationError("目前仅适配 StarRocks 3.5.7，检测到 " + version)
            yield conn, version
        finally:
            with self.lock:
                self.connections.discard(conn)
            if conn.open:
                conn.close()


class Progress:
    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"phase": "连接/预检", "table": "", "rows": 0, "finished": 0}
        self.stop = threading.Event()
        self.started = time.monotonic()
        self.thread = threading.Thread(target=self.heartbeat, daemon=True)

    def update(self, **values):
        with self.lock:
            self.state.update(values)

    def add_rows(self, rows):
        with self.lock:
            self.state["rows"] += rows

    def heartbeat(self):
        while not self.stop.wait(15):
            with self.lock:
                state = dict(self.state)
            elapsed = time.monotonic() - self.started
            print("[心跳 %.0fs] %s；最近对象 %s；已完成 %s 表；已读 %s 行；%.0f 行/s" %
                  (elapsed, state["phase"], state["table"], state["finished"], state["rows"],
                   state["rows"] / max(elapsed, 1)), file=sys.stderr, flush=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=1)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as data:
        for block in iter(lambda: data.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_file(root, name):
    if not isinstance(name, str) or not re.fullmatch(r"(?:table-\d{6}\.sql(?:\.gz)?|schema\.sql|verify\.sql|restore_dynamic_partitions\.sql)", name):
        raise MigrationError("迁移包包含非法文件名")
    path = root / name
    if path.is_symlink() or not path.is_file():
        raise MigrationError("迁移包文件缺失或为符号链接: " + name)
    return path


def sync_directory(path):
    if os.name != "nt":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def atomic_output(path, compressed=False):
    partial = path.with_name(path.name + ".partial")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw:
            if compressed:
                with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as zipped:
                    import io
                    with io.TextIOWrapper(zipped, encoding="utf-8", newline="\n") as output:
                        yield output
            else:
                import io
                with io.TextIOWrapper(raw, encoding="utf-8", newline="\n", write_through=True) as output:
                    yield output
                    output.flush()
                    os.fsync(output.fileno())
            if compressed:
                raw.flush()
                os.fsync(raw.fileno())
        os.replace(partial, path)
        sync_directory(path.parent)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def write_json(path, value):
    with atomic_output(path) as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")


@contextmanager
def package_lock(root):
    path = root / ".lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError:
                raise MigrationError("迁移包正被另一个进程使用") from None
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise MigrationError("迁移包正被另一个进程使用") from None
        yield
    finally:
        os.close(fd)


def read_manifest(root):
    if root.is_symlink() or not root.is_dir():
        raise MigrationError("--input/--output 必须为真实目录，不能为符号链接")
    path = root / "manifest.json"
    if path.is_symlink() or not path.is_file():
        raise MigrationError("缺少 manifest.json 或文件为符号链接")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if manifest["tool"] != "StarMigrtCli" or manifest["format_version"] != FORMAT_VERSION:
            raise MigrationError("不支持的迁移包格式；旧版 ioc_dump 不能续传")
        if hashlib.sha256(canonical(manifest["plan"])).hexdigest() != manifest["plan_sha256"]:
            raise MigrationError("迁移计划校验失败")
        if not isinstance(manifest["completed"], dict) or not manifest["plan"]["tables"]:
            raise MigrationError("迁移包清单结构错误")
        if not re.fullmatch(r"[a-f0-9]{32}", manifest["package_id"]):
            raise MigrationError("迁移包任务标识错误")
        return manifest
    except (ValueError, KeyError, TypeError):
        raise MigrationError("无法解析迁移包清单") from None


def validate_files(root, manifest, complete=True):
    plan = manifest["plan"]
    expected = {t["file"] for t in plan["tables"]}
    completed = manifest["completed"]
    if not set(completed) <= expected:
        raise MigrationError("完成记录不属于迁移计划")
    if complete and (manifest.get("status") != "complete" or set(completed) != expected):
        raise MigrationError("迁移包尚未完成，不能导入、合并或进行目标验收；请先 dump --resume")
    for name, record in completed.items():
        path = safe_file(root, name)
        if sha256(path) != record["sha256"] or path.stat().st_size != record["bytes"]:
            raise MigrationError("已完成文件校验失败，不能续传或导入: " + name)
    for name, digest in manifest.get("sha256", {}).items():
        if sha256(safe_file(root, name)) != digest:
            raise MigrationError("公共文件校验失败: " + name)
    if complete and set(manifest.get("sha256", {})) != {"schema.sql", "verify.sql", "restore_dynamic_partitions.sql"}:
        raise MigrationError("迁移包缺少公共文件校验记录")


def abort_stream(conn, cursor):
    """放弃本次连接上的流；禁用 PyMySQL 1.1.2 析构时的排空操作。"""
    result = getattr(cursor, "_result", None) or getattr(conn, "_result", None)
    if result is not None:
        result.unbuffered_active = False
        result.connection = None
    cursor.connection = None
    if conn.open:
        conn.close()


def export_table(source, table, path, args, cancel, progress, package_id):
    rows, row_hash, buffered = 0, 0, []
    names = ",".join(ident(c["name"]) for c in table["columns"])
    table_number = table["file"].split("-")[1].split(".")[0]
    batch_number = 1

    def insert_prefix():
        label = "sr_%s_t%s_b%012d" % (package_id, table_number, batch_number)
        return ("INSERT INTO " + ident(table["target_database"]) + "." + ident(table["name"]) +
                " WITH LABEL " + ident(label) + " (" + names + ") VALUES\n")

    prefix = insert_prefix()
    prefix_bytes = len(prefix.encode("utf-8"))
    size, limit = prefix_bytes, args.batch_mib * 1024 * 1024
    started = time.monotonic()
    with atomic_output(path, args.gzip) as output:
        output.write(SESSION_SQL)
        if not args.schema_only:
            with source.connect() as (conn, _):
                cursor = conn.cursor(source.driver.cursors.SSCursor)
                try:
                    cursor.execute("SELECT " + names + " FROM " + ident(table["database"]) + "." + ident(table["name"]))
                    for row in cursor:
                        if cancel.is_set():
                            raise MigrationError("迁移已取消；已完成表仍保留")
                        text = row_sql(row, table["columns"])
                        row_hash = (row_hash + int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest(), "big")) % HASH_MOD
                        byte_count = len(text.encode("utf-8")) + 2
                        if prefix_bytes + byte_count > limit:
                            raise MigrationError("单行超过 --batch-mib，未截断；请增大批次上限并用新输出目录重导")
                        if buffered and (len(buffered) >= args.batch_rows or size + byte_count > limit):
                            output.write(prefix + ",\n".join(buffered) + ";\n")
                            batch_number += 1
                            prefix = insert_prefix()
                            buffered.clear()
                            size = prefix_bytes
                        buffered.append(text)
                        size += byte_count
                        rows += 1
                        if rows % 1000 == 0:
                            progress.add_rows(1000)
                    if buffered:
                        output.write(prefix + ",\n".join(buffered) + ";\n")
                    progress.add_rows(rows % 1000)
                except BaseException:
                    # SSCursor.close() drains unread results; disconnect before closing it.
                    abort_stream(conn, cursor)
                    raise
                finally:
                    cursor.close()
                current = all_rows(conn, "SHOW CREATE TABLE " + ident(table["database"]) + "." + ident(table["name"]))[0][1]
                if current != table["source_ddl"]:
                    raise MigrationError("导出期间表结构/分区发生变化；停止 DDL、分区调度后重新导出")
    return dict(rows=rows, row_sha256_sum="%064x" % row_hash, sha256=sha256(path),
                bytes=path.stat().st_size, seconds=round(time.monotonic() - started, 3))


def write_common_files(root, manifest):
    plan = manifest["plan"]
    with atomic_output(root / "schema.sql") as output:
        output.write("-- Execute ONCE into empty target databases; stop on the FIRST error.\n" + SESSION_SQL)
        if plan["mode"] != "data-only":
            for database in plan["databases"]:
                output.write("CREATE DATABASE IF NOT EXISTS " + ident(database["target"]) + ";\n")
            for table in plan["tables"]:
                output.write(table["ddl"])
    with atomic_output(root / "verify.sql") as output:
        output.write(SESSION_SQL + "-- Every ok must be 1. Counts alone do not prove content equality.\n")
        for table in plan["tables"]:
            if plan["mode"] == "schema-only":
                continue
            count = manifest["completed"][table["file"]]["rows"]
            ref = ident(table["target_database"]) + "." + ident(table["name"])
            output.write("SELECT " + sql_string(ref) + " AS table_name, " + str(count) +
                         " AS expected_rows, COUNT(*) AS actual_rows, COUNT(*) = " + str(count) +
                         " AS ok FROM " + ref + ";\n")
    with atomic_output(root / "restore_dynamic_partitions.sql") as output:
        output.write("-- Run ONLY after data verification and review of partition retention.\n")
        for table in plan["tables"]:
            if table["dynamic_disabled"] and plan["mode"] != "data-only":
                output.write("ALTER TABLE " + ident(table["target_database"]) + "." + ident(table["name"]) +
                             ' SET ("dynamic_partition.enable"="true");\n')
    manifest["sha256"] = {name: sha256(root / name) for name in
                          ("schema.sql", "verify.sql", "restore_dynamic_partitions.sql")}


def export_package(source, plan, root, args, progress):
    if root.is_symlink():
        raise MigrationError("输出目录不能是符号链接")
    if not args.resume:
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            raise MigrationError("输出目录已存在；使用新目录或明确指定 --resume") from None
    elif not root.is_dir():
        raise MigrationError("续传目录不存在")
    with package_lock(root):
        if args.resume:
            manifest = read_manifest(root)
            if manifest["plan"] != plan:
                raise MigrationError("续传输入或源结构已变化；保留旧包，使用新目录重新导出")
            validate_files(root, manifest, complete=False)
            if manifest["status"] == "complete":
                validate_files(root, manifest)
                return manifest
        else:
            manifest = dict(tool="StarMigrtCli", tool_version=VERSION, format_version=FORMAT_VERSION,
                            created_at=dt.datetime.now(dt.timezone.utc).isoformat(), status="incomplete",
                            package_id=uuid.uuid4().hex,
                            plan=plan, plan_sha256=hashlib.sha256(canonical(plan)).hexdigest(), completed={})
            write_json(root / "manifest.json", manifest)
        # Only known runner-owned partials may be removed under the held lock.
        allowed = {t["file"] + ".partial" for t in plan["tables"]} | {
            name + ".partial" for name in ("manifest.json", "schema.sql", "verify.sql", "restore_dynamic_partitions.sql")}
        for name in allowed:
            path = root / name
            if path.is_symlink():
                raise MigrationError("发现符号链接临时文件，拒绝续传")
            path.unlink(missing_ok=True)
        for table in plan["tables"]:
            path = root / table["file"]
            if path.is_symlink():
                raise MigrationError("数据文件不能为符号链接")
            if table["file"] not in manifest["completed"]:
                # A crash between file publication and checkpoint commit is safe to re-export.
                path.unlink(missing_ok=True)
        pending = iter(t for t in plan["tables"] if t["file"] not in manifest["completed"])
        cancel = threading.Event()
        progress.update(phase="导出数据", finished=len(manifest["completed"]))
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {}

            def submit_one():
                table = next(pending, None)
                if table is not None:
                    progress.update(table=table["database"] + "." + table["name"])
                    future = pool.submit(export_table, source, table, root / table["file"], args, cancel, progress,
                                         manifest["package_id"])
                    futures[future] = table

            for _ in range(args.workers):
                submit_one()
            try:
                while futures:
                    done, _ = wait(futures, timeout=1, return_when=FIRST_COMPLETED)
                    failure = None
                    # Consume every finished result before scheduling any new work.
                    for future in done:
                        table = futures.pop(future)
                        try:
                            record = future.result()
                        except BaseException as error:
                            if failure is None:
                                failure = error if isinstance(error, KeyboardInterrupt) else MigrationError(
                                    table["database"] + "." + table["name"] + ": " + failure_text(error))
                            cancel.set()
                            source.abort()
                            continue
                        manifest["completed"][table["file"]] = record
                        write_json(root / "manifest.json", manifest)
                        progress.update(finished=len(manifest["completed"]))
                        print("[%d/%d] %s.%s: %s 行，%.1fs" %
                              (len(manifest["completed"]), len(plan["tables"]), table["database"],
                               table["name"], record["rows"], record["seconds"]), file=sys.stderr, flush=True)
                    if failure is not None:
                        raise failure
                    for _ in done:
                        submit_one()
            except BaseException:
                cancel.set()
                source.abort()
                for future in futures:
                    future.cancel()
                raise
        # Recheck all selected metadata after parallel reads, including already resumed tables.
        with source.connect() as (conn, _):
            current = inspect(conn, args, progress)
            if current != {key: plan[key] for key in ("databases", "tables")}:
                raise MigrationError("最终元数据检查发现变化；不会将此包标为完成，请新建目录重导")
        progress.update(phase="生成校验与导入文件", table="")
        write_common_files(root, manifest)
        manifest["status"] = "complete"
        manifest["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        write_json(root / "manifest.json", manifest)
        validate_files(root, manifest)
        return manifest


def verify_target(source, manifest, content, progress):
    plan = manifest["plan"]
    failures = []
    with source.connect() as (conn, _):
        for table in plan["tables"]:
            db, name = table["target_database"], table["name"]
            progress.update(phase="目标只读校验", table=db + "." + name)
            columns = columns_for(conn, db, name)
            if [(c["name"], c["type"]) for c in columns] != [(c["name"], c["type"]) for c in table["columns"]]:
                failures.append(db + "." + name + ": 列名称/类型/顺序不匹配")
                continue
            if plan["mode"] == "schema-only":
                continue
            expected = manifest["completed"][table["file"]]
            ref = ident(db) + "." + ident(name)
            count = int(all_rows(conn, "SELECT COUNT(*) FROM " + ref)[0][0])
            if count != expected["rows"]:
                failures.append(db + "." + name + ": 行数不匹配，期望 %d，实际 %d" % (expected["rows"], count))
                continue
            if content:
                names = ",".join(ident(c["name"]) for c in table["columns"])
                cursor = conn.cursor(source.driver.cursors.SSCursor)
                total, rows = 0, 0
                try:
                    cursor.execute("SELECT " + names + " FROM " + ref)
                    for row in cursor:
                        total = (total + int.from_bytes(hashlib.sha256(row_sql(row, table["columns"]).encode("utf-8")).digest(), "big")) % HASH_MOD
                        rows += 1
                        if rows % 1000 == 0:
                            progress.add_rows(1000)
                    progress.add_rows(rows % 1000)
                except BaseException:
                    abort_stream(conn, cursor)
                    raise
                finally:
                    cursor.close()
                if rows != count or "%064x" % total != expected["row_sha256_sum"]:
                    failures.append(db + "." + name + ": 内容摘要不匹配或校验期间数据发生变化")
    if failures:
        raise MigrationError("目标校验未通过:\n" + "\n".join(failures))


def merge_package(root, manifest, destination):
    if destination.exists() or destination.is_symlink():
        raise MigrationError("合并目标已存在，不会覆盖")
    if destination.resolve().is_relative_to(root.resolve()):
        raise MigrationError("合并文件请放在迁移包目录之外")
    # Claim the destination before writing so concurrent merges cannot overwrite each other.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with atomic_output(destination) as output:
            output.write("-- Import ONCE; stop on FIRST error. This is NOT a database transaction.\n")
            names = ["schema.sql"] + [t["file"] for t in manifest["plan"]["tables"]]
            for name in names:
                path = safe_file(root, name)
                opener = gzip.open if name.endswith(".gz") else open
                with opener(path, "rt", encoding="utf-8", newline="") as data:
                    for block in iter(lambda: data.read(1024 * 1024), ""):
                        output.write(block)
                output.write("\n")
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return sha256(destination)


def add_connection(parser, required=True):
    parser.add_argument("--host", required=required, help="FE MySQL 地址；verify 省略时仅校验本地包")
    parser.add_argument("--port", type=int, default=9030)
    parser.add_argument("--user", required=required)
    parser.add_argument("--password-env", default="SR_PASSWORD", help="密码环境变量名，默认 SR_PASSWORD")
    parser.add_argument("--password-file", type=Path, help="当前用户拥有的 0600 UTF-8 密码文件")
    parser.add_argument("--ssl-ca", type=Path, help="CA 文件；同时验证证书和主机名")
    parser.add_argument("--connect-timeout", type=int, default=15, help="建连超时秒，1–120")
    parser.add_argument("--query-timeout", type=int, default=3600, help="查询超时秒；网络读取超时为此值+30")


def make_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="示例：\n  StarMigrtCli.py check --host fe --user reader --databases db1 db2\n"
                                            "  StarMigrtCli.py dump --host fe --user reader --databases db1 db2 --source-quiesced --output backup\n"
                                            "  StarMigrtCli.py verify --input backup\n"
                                            "  StarMigrtCli.py merge --input backup --output migration.sql\n详见同目录 README.md；仅适配 StarRocks 3.5.7。")
    parser.add_argument("--version", action="version", version="StarMigrtCli " + VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "dump"):
        sub = commands.add_parser(name, help="源端只读预检" if name == "check" else "导出/续传离线逻辑迁移包")
        add_connection(sub)
        sub.add_argument("--database", action="append", help="单个库，可重复；兼容旧入口")
        sub.add_argument("--databases", nargs="+", help="多个库，以空格分隔")
        sub.add_argument("--all-databases", action="store_true", help="所有可见业务库，不包含系统库")
        sub.add_argument("--exclude-database", action="append", default=[], metavar="PATTERN")
        sub.add_argument("--table", action="append", default=[], metavar="[DB.]PATTERN", help="包含表；支持 shell 通配符，可重复，需用引号")
        sub.add_argument("--exclude-table", action="append", default=[], metavar="[DB.]PATTERN")
        sub.add_argument("--database-map", action="append", default=[], metavar="SOURCE=TARGET")
        sub.add_argument("--target-database", help="单库重命名快捷入口")
        mode = sub.add_mutually_exclusive_group()
        mode.add_argument("--schema-only", action="store_true", help="只导结构，允许未适配数据类型")
        mode.add_argument("--data-only", action="store_true", help="只导数据；目标结构/分区管理须自行准备")
        sub.add_argument("--replication-num", type=int, help="显式修改目标副本数；默认保留，不自动降为1")
        sub.add_argument("--batch-rows", type=int, default=500)
        sub.add_argument("--batch-mib", type=int, default=1, help="每条 INSERT UTF-8 大小上限 MiB")
        sub.add_argument("--gzip", action="store_true", help="压缩逐表 SQL；merge 时自动解压")
        if name == "dump":
            sub.add_argument("--output", type=Path, required=True, help="新建私有目录；续传时为原目录")
            sub.add_argument("--resume", action="store_true", help="核对计划/文件后仅重导未完成表")
            sub.add_argument("--workers", type=int, default=2, help="并发读表连接数，1–16；另有1个元数据连接")
            consistency = sub.add_mutually_exclusive_group()
            consistency.add_argument("--source-quiesced", action="store_true", help="声明源端写入、DDL和分区调度已暂停，并持续到导出/续传结束")
            consistency.add_argument("--allow-inconsistent", action="store_true", help="明确接受各表不同读取时点；不能用于一致性切换")
    sub = commands.add_parser("verify", help="校验包完整性；可选目标端只读行数/内容校验")
    sub.add_argument("--input", type=Path, required=True)
    add_connection(sub, required=False)
    sub.add_argument("--content", action="store_true", help="目标全表流式扫描，核对无序行摘要（需 --host/--user）")
    sub = commands.add_parser("merge", help="将完整迁移包合并成一个可导入的 SQL 文件")
    sub.add_argument("--input", type=Path, required=True)
    sub.add_argument("--output", type=Path, required=True, help="包目录之外的新文件；不覆盖")
    return parser


def parse_args(argv):
    parser = make_parser()
    # Preserve explicit old invocations, but remove the unsafe implicit ioc database default.
    if argv and argv[0].startswith("--") and argv[0] not in {"--help", "--version"}:
        argv = list(argv)
        command = "check" if "--check-only" in argv else "dump"
        if "--check-only" in argv:
            argv.remove("--check-only")
        argv.insert(0, command)
    args = parser.parse_args(argv)
    if hasattr(args, "port"):
        if not 1 <= args.port <= 65535 or not 1 <= args.connect_timeout <= 120 or not 1 <= args.query_timeout <= 86400:
            parser.error("端口须为 1–65535；建连超时 1–120 秒；查询超时 1–86400 秒")
    if args.command in {"check", "dump"}:
        if not (args.database or args.databases or args.all_databases):
            parser.error("必须显式选择 --database/--databases/--all-databases，不再默认 ioc")
        if args.all_databases and (args.database or args.databases):
            parser.error("--all-databases 不能与显式数据库列表同用")
        if not 1 <= args.batch_rows <= 1000000 or not 1 <= args.batch_mib <= 256:
            parser.error("--batch-rows 为 1–1000000；--batch-mib 为 1–256")
        if args.replication_num is not None and not 1 <= args.replication_num <= 32767:
            parser.error("--replication-num 必须为 1–32767")
        if args.data_only and args.replication_num is not None:
            parser.error("--data-only 不生成 DDL，不能指定 --replication-num")
    if args.command == "dump":
        if not 1 <= args.workers <= 16:
            parser.error("--workers 为 1–16")
        if not args.schema_only and not (args.source_quiesced or args.allow_inconsistent):
            parser.error("导数据须声明 --source-quiesced，或明确接受 --allow-inconsistent；工具不能建立跨表快照")
    if args.command == "verify":
        if bool(args.host) != bool(args.user) or args.content and not args.host:
            parser.error("目标校验须同时指定 --host 和 --user；--content 需要目标连接")
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with Progress() as progress:
            if args.command in {"verify", "merge"}:
                root = args.input.absolute()
                # A missing/malformed input must never create a lock or other output.
                read_manifest(root)
                with package_lock(root):
                    manifest = read_manifest(root)
                    validate_files(root, manifest)
                    if args.command == "merge":
                        digest = merge_package(root, manifest, args.output.absolute())
                        print("STARMIGRT_MERGE_PASS sha256=" + digest + " output=" + str(args.output))
                    else:
                        if args.content and manifest["plan"]["mode"] == "schema-only":
                            raise MigrationError("--content 需要含数据的迁移包；结构包仅支持目标列校验")
                        if args.host:
                            verify_target(Source(args), manifest, args.content, progress)
                        scope = "local-files"
                        if args.host:
                            scope = "target-columns" if manifest["plan"]["mode"] == "schema-only" else "target-content" if args.content else "target-rows"
                        print("STARMIGRT_VERIFY_PASS scope=" + scope)
                return 0
            source = Source(args)
            with source.connect() as (conn, version):
                plan = inspect(conn, args, progress)
            plan.update(source=dict(host=args.host, port=args.port, user=args.user, version=version),
                        mode="schema-only" if args.schema_only else "data-only" if args.data_only else "schema-and-data",
                        consistent=bool(getattr(args, "source_quiesced", False)),
                        batch_rows=args.batch_rows, batch_mib=args.batch_mib, gzip=args.gzip,
                        replication_num=args.replication_num)
            print("预检通过：%d 个库、%d 张普通内表；副本数 %s" %
                  (len(plan["databases"]), len(plan["tables"]), args.replication_num or "保留源值"), file=sys.stderr, flush=True)
            for table in plan["tables"]:
                print("  %s.%s → %s.%s" % (table["database"], table["name"], table["target_database"], table["name"]), file=sys.stderr)
            if args.command == "check":
                print("STARMIGRT_CHECK_PASS tables=" + str(len(plan["tables"])))
                return 0
            if args.allow_inconsistent:
                print("注意：本次明确接受非一致读取，包不可作为一致性切换依据。", file=sys.stderr)
            manifest = export_package(source, plan, args.output.absolute(), args, progress)
            print("STARMIGRT_PASS tables=%d rows=%d output=%s" %
                  (len(plan["tables"]), sum(r["rows"] for r in manifest["completed"].values()), args.output))
        return 0
    except KeyboardInterrupt:
        print("STARMIGRT_STOPPED：收到中断；已提交到清单的表可续传，当前未完成表会重新读取。", file=sys.stderr)
        return 130
    except Exception as error:
        print("STARMIGRT_FAIL：" + failure_text(error), file=sys.stderr)
        return 1


def handle_termination(signum, frame):
    raise KeyboardInterrupt


if __name__ == "__main__":
    # Convert TERM to normal unwind so temporary files and locks are reclaimed.
    signal.signal(signal.SIGTERM, handle_termination)
    sys.exit(main())

#!/usr/bin/env python3
"""StarRocks SQL 导出与导入工具。"""

import argparse
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
import io

VERSION = "3.0.0"
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
                 native_type=str(row[1]).strip().lower(), extra=str(row[5] or "")) for row in rows]


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
            tables.append(dict(database=db, target_database=mapping.get(db, db), name=name,
                               source_ddl=original, ddl=ddl, dynamic_disabled=dynamic,
                               columns=columns))
    for pattern in set(args.table) - seen_patterns:
        errors.append("--table 没有匹配对象: " + pattern)
    if errors:
        raise MigrationError("预检未通过，未导出任何数据:\n" + "\n".join(errors))
    if not tables:
        raise MigrationError("筛选结果没有普通内表；不会生成空 SQL 文件")
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

    @contextmanager
    def connect(self):
        conn = self.connection_factory(**self.options)
        try:
            all_rows(conn, "SET query_timeout = " + str(self.timeout))
            # Fix SHOW CREATE output mode as well as INSERT literal mode.
            all_rows(conn, "SET sql_mode = ''")
            version = str(all_rows(conn, "SELECT current_version()")[0][0])
            if not re.fullmatch(r"3\.5\.7(?:[-+ ].*)?", version):
                raise MigrationError("目前仅适配 StarRocks 3.5.7，检测到 " + version)
            yield conn, version
        finally:
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


def abort_stream(conn, cursor):
    """放弃本次连接上的流；禁用 PyMySQL 1.1.2 析构时的排空操作。"""
    result = getattr(cursor, "_result", None) or getattr(conn, "_result", None)
    if result is not None:
        result.unbuffered_active = False
        result.connection = None
    cursor.connection = None
    if conn.open:
        conn.close()


# SQL files remain directly executable by the MySQL client. Comments carry the
# information needed for automatic preflight and readback by this tool.
SIGNATURE = '-- StarMigrtCli SQL 1\n'
FOOTER = '-- StarMigrtCli complete sha256='
MAX_STATEMENT = 256 * 1024 * 1024
MAX_METADATA = 8 * 1024 * 1024


@contextmanager
def sql_output(path):
    if path.exists() or path.is_symlink():
        raise MigrationError("输出文件已存在，不会覆盖；请指定新文件")
    partial = path.with_name(path.name + '.partial')
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as raw:
            stream = gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1, mtime=0) if path.suffix == '.gz' else raw
            output = io.TextIOWrapper(stream, encoding='utf-8', newline='\n')
            try:
                yield output
                output.flush()
            finally:
                output.detach()
                if stream is not raw:
                    stream.close()
            raw.flush()
            os.fsync(raw.fileno())
        # link publishes a complete file atomically and refuses concurrent output.
        os.link(partial, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        partial.unlink(missing_ok=True)


class SQLWriter:
    def __init__(self, output):
        self.output = output
        self.digest = hashlib.sha256()

    def write(self, text):
        self.output.write(text)
        self.digest.update(text.encode('utf-8'))

    def metadata(self, kind, value):
        line = '-- ' + kind + ' ' + canonical(value).decode('utf-8') + '\n'
        if len(line.encode('utf-8')) > MAX_METADATA:
            raise MigrationError('表结构信息超过 8 MiB，停止导出')
        self.write(line)

    def finish(self):
        self.output.write(FOOTER + self.digest.hexdigest() + '\n')


def export_rows(source, conn, table, writer, args, progress, dump_id, number):
    names = ','.join(ident(c['name']) for c in table['columns'])
    ref = ident(table['target_database']) + '.' + ident(table['name'])
    batch, rows, total, size, buffered = 1, 0, 0, 0, []
    limit = args.batch_mib * 1024 * 1024

    def prefix():
        return 'INSERT INTO %s WITH LABEL `sr_%s_t%d_b%d` (%s) VALUES\n' % (ref, dump_id, number, batch, names)

    cursor = conn.cursor(source.driver.cursors.SSCursor)
    try:
        cursor.execute('SELECT ' + names + ' FROM ' + ident(table['database']) + '.' + ident(table['name']))
        for row in cursor:
            text = row_sql(row, table['columns'])
            length = len(text.encode('utf-8')) + 2
            if len(prefix().encode('utf-8')) + length > limit:
                raise MigrationError('单行超过 --batch-mib；请提高上限并重新导出')
            if buffered and (len(buffered) >= args.batch_rows or size + length + len(prefix().encode('utf-8')) > limit):
                writer.write(prefix() + ',\n'.join(buffered) + ';\n')
                batch += 1
                buffered, size = [], 0
            buffered.append(text)
            size += length
            rows += 1
            total = (total + int.from_bytes(hashlib.sha256(text.encode('utf-8')).digest(), 'big')) % HASH_MOD
            if rows % 1000 == 0:
                progress.add_rows(1000)
        if buffered:
            writer.write(prefix() + ',\n'.join(buffered) + ';\n')
        progress.add_rows(rows % 1000)
    except BaseException:
        abort_stream(conn, cursor)
        raise
    finally:
        cursor.close()
    return dict(rows=rows, row_sha256_sum='%064x' % total)


def dump_sql(source, conn, plan, args, progress):
    started = time.monotonic()
    dump_id = uuid.uuid4().hex
    with sql_output(args.output.absolute()) as output:
        writer = SQLWriter(output)
        writer.write(SIGNATURE)
        writer.metadata('plan', plan)
        writer.write('-- Import into an empty target; stop on the first error.\n' + SESSION_SQL)
        if plan['mode'] != 'data-only':
            for db in plan['databases']:
                writer.write('CREATE DATABASE IF NOT EXISTS ' + ident(db['target']) + ';\n')
            for table in plan['tables']:
                writer.write(table['ddl'])
        rows = 0
        for number, table in enumerate(plan['tables'], 1):
            ref = table['database'] + '.' + table['name']
            progress.update(phase='导出', table=ref)
            record = dict(rows=0, row_sha256_sum='0' * 64)
            if plan['mode'] != 'schema-only':
                record = export_rows(source, conn, table, writer, args, progress, dump_id, number)
            record['table'] = number - 1
            writer.metadata('table', record)
            rows += record['rows']
            progress.update(finished=number)
            print('[导出 %d/%d] %s：%d 行；累计 %.1fs' %
                  (number, len(plan['tables']), ref, record['rows'], time.monotonic()-started), file=sys.stderr, flush=True)
        progress.update(phase='复查源端结构', table='')
        if inspect(conn, args, progress) != {key: plan[key] for key in ('databases', 'tables')}:
            raise MigrationError('导出期间库表结构或分区发生变化；请暂停相关变更后重新导出')
        for table in plan['tables']:
            if table['dynamic_disabled'] and plan['mode'] != 'data-only':
                writer.write('-- After verification and retention review, enable scheduling manually:\n-- ALTER TABLE ' +
                             ident(table['target_database']) + '.' + ident(table['name']) +
                             ' SET ("dynamic_partition.enable"="true");\n')
        writer.finish()
    print('STARMIGRT_DUMP_OK tables=%d rows=%d file=%s sha256=%s' %
          (len(plan['tables']), rows, args.output, sha256(args.output)), flush=True)


@contextmanager
def sql_input(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as raw:
        if not stat.S_ISREG(os.fstat(raw.fileno()).st_mode):
            raise MigrationError('输入必须为普通 SQL 文件')
        stream = gzip.GzipFile(fileobj=raw) if path.suffix == '.gz' else raw
        with io.TextIOWrapper(stream, encoding='utf-8', newline='') as data:
            yield data


def read_dump(data):
    digest, plan, records = hashlib.sha256(), None, []
    if data.readline() != SIGNATURE:
        raise MigrationError('输入不是 StarMigrtCli 导出的 SQL 文件')
    digest.update(SIGNATURE.encode('utf-8'))
    for line in data:
        if line.startswith(FOOTER):
            if line != FOOTER + digest.hexdigest() + '\n' or data.read(1):
                raise MigrationError('SQL 文件校验失败：文件损坏或被修改')
            break
        digest.update(line.encode('utf-8'))
        if line.startswith('-- plan ') or line.startswith('-- table '):
            if len(line.encode('utf-8')) > MAX_METADATA:
                raise MigrationError('SQL 结构信息过大')
            try:
                value = json.loads(line.split(' ', 2)[2])
            except ValueError:
                raise MigrationError('SQL 结构信息损坏') from None
            if line.startswith('-- plan '):
                if plan is not None:
                    raise MigrationError('SQL 结构信息重复')
                plan = value
            else:
                records.append(value)
    else:
        raise MigrationError('SQL 文件不完整，不能导入；请重新导出')
    try:
        if plan['mode'] not in {'schema-only', 'data-only', 'schema-and-data'} or not plan['tables']:
            raise ValueError()
        if len(records) != len(plan['tables']) or [r['table'] for r in records] != list(range(len(records))):
            raise ValueError()
        for t, r in zip(plan['tables'], records):
            ident(t['target_database'])
            ident(t['name'])
            if not t['columns'] or r['rows'] < 0 or not re.fullmatch('[0-9a-f]{64}', r['row_sha256_sum']):
                raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise MigrationError('SQL 结构或校验信息不完整') from None
    return plan, records


def statements(data):
    """Stream SQL, preserving delimiters inside strings, identifiers and comments."""
    parts, size, quote, block, escaped = [], 0, None, False, False
    for line in data:
        i, start = 0, 0
        while i < len(line):
            c = line[i]
            if block:
                if line.startswith('*/', i):
                    block = False
                    i += 2
                else:
                    i += 1
                continue
            if quote:
                if escaped:
                    escaped = False
                elif c == '\\' and quote != '`':
                    escaped = True
                elif c == quote:
                    if i + 1 < len(line) and line[i+1] == quote:
                        i += 2
                        continue
                    quote = None
                i += 1
                continue
            if line.startswith('--', i) or c == '#':
                parts.append(line[start:i])
                size += i - start
                start = len(line)
                break
            if line.startswith('/*', i):
                block = True
                i += 2
                continue
            if c in "'\"`":
                quote = c
            elif c == ';':
                parts.append(line[start:i+1])
                size += i + 1 - start
                if size > MAX_STATEMENT:
                    raise MigrationError('SQL 单条语句超过 256 MiB')
                statement = ''.join(parts).strip()
                if statement:
                    yield statement
                parts, size, start = [], 0, i + 1
            i += 1
        parts.append(line[start:])
        size += len(line) - start
        if size > MAX_STATEMENT:
            raise MigrationError('SQL 单条语句超过 256 MiB')
    if quote or block or ''.join(parts).strip():
        raise MigrationError('SQL 语句未闭合')


def table_ref(table):
    return ident(table['target_database']) + '.' + ident(table['name'])


def preflight_target(conn, plan):
    # Check ALL objects before any CREATE or INSERT, not one table at a time.
    visible = {r[0] for r in all_rows(conn, 'SHOW DATABASES')}
    objects = {db['target']: {r[0] for r in inventory(conn, db['target'])}
               for db in plan['databases'] if db['target'] in visible}
    for table in plan['tables']:
        exists = table['name'] in objects.get(table['target_database'], set())
        if plan['mode'] != 'data-only':
            if exists:
                raise MigrationError(table_ref(table) + ' 已存在；导入已停止，未写入任何数据')
        else:
            if not exists or columns_for(conn, table['target_database'], table['name']) != table['columns']:
                raise MigrationError(table_ref(table) + ' 不存在或列结构不一致')
            if all_rows(conn, 'SELECT COUNT(*) FROM ' + table_ref(table))[0][0] != 0:
                raise MigrationError(table_ref(table) + ' 不是空表，不能导入数据')
            original = all_rows(conn, 'SHOW CREATE TABLE ' + table_ref(table))[0][1]
            props = ddl_properties(original)
            if props.get('dynamic_partition.enable', ('', 'false'))[1].lower() == 'true':
                raise MigrationError(table_ref(table) + ' 的动态分区仍在运行，请先暂停调度')
            prepare_ddl(original, table['target_database'], table['name'], with_data=True)


def verify_rows(source, conn, plan, records, progress):
    for number, (table, record) in enumerate(zip(plan['tables'], records), 1):
        ref = table_ref(table)
        progress.update(phase='校验目标', table=ref)
        if columns_for(conn, table['target_database'], table['name']) != table['columns']:
            raise MigrationError(ref + ' 列名称、原生类型或顺序不一致')
        if plan['mode'] != 'schema-only':
            names = ','.join(ident(c['name']) for c in table['columns'])
            cursor = conn.cursor(source.driver.cursors.SSCursor)
            count, total = 0, 0
            try:
                cursor.execute('SELECT ' + names + ' FROM ' + ref)
                for row in cursor:
                    total = (total + int.from_bytes(hashlib.sha256(row_sql(row, table['columns']).encode('utf-8')).digest(), 'big')) % HASH_MOD
                    count += 1
                    if count % 1000 == 0:
                        progress.add_rows(1000)
                progress.add_rows(count % 1000)
            except BaseException:
                abort_stream(conn, cursor)
                raise
            finally:
                cursor.close()
            if count != record['rows'] or '%064x' % total != record['row_sha256_sum']:
                raise MigrationError(ref + ' 行数或内容校验不一致；请保持目标隔离')
        print('[校验 %d/%d] %s：通过' % (number, len(records), ref), file=sys.stderr, flush=True)
        progress.update(finished=number)


def load_sql(source, conn, data, plan, records, progress):
    preflight_target(conn, plan)
    data.seek(0)
    number = 0
    for statement in statements(data):
        number += 1
        progress.update(phase='导入', table='SQL #%d' % number)
        try:
            with conn.cursor() as cursor:
                cursor.execute(statement)
                if cursor.warning_count:
                    raise MigrationError('SQL #%d 返回警告，已停止；请核查目标数据' % number)
                message = getattr(getattr(cursor, '_result', None), 'message', b'')
                if isinstance(message, bytes):
                    message = message.decode('utf-8', errors='replace')
                if re.search(r"['\"]status['\"]\s*:\s*['\"]COMMITTED['\"]", message):
                    raise MigrationError('SQL #%d 已提交但尚未可见；请查询该事务状态，禁止重放' % number)
        except Exception as error:
            # Do not print SQL values or raw server errors.
            raise MigrationError('导入在 SQL #%d 停止：%s。已提交语句不会自动回滚；请保留失败目标，勿直接重放文件。' %
                                 (number, failure_text(error))) from None
        if number % 100 == 0:
            print('[导入] 已执行 %d 条 SQL' % number, file=sys.stderr, flush=True)
    verify_rows(source, conn, plan, records, progress)
    scope = 'columns' if plan['mode'] == 'schema-only' else 'content'
    print('STARMIGRT_LOAD_OK tables=%d statements=%d verification=%s' % (len(records), number, scope), flush=True)


def add_connection(parser):
    parser.add_argument('--host', required=True, help='FE 地址')
    parser.add_argument('--port', type=int, default=9030, help='FE MySQL 端口，默认 9030')
    parser.add_argument('--user', required=True, help='数据库账号')
    parser.add_argument('--password-env', default='SR_PASSWORD', help='密码环境变量名，默认 SR_PASSWORD；未设置时交互输入')
    parser.add_argument('--password-file', type=Path, help='当前用户拥有的 0600 密码文件')
    parser.add_argument('--ssl-ca', type=Path, help='CA 文件；验证证书及主机名，拒绝 TLS 降级')
    parser.add_argument('--connect-timeout', type=int, default=15, help='连接超时秒，默认 15')
    parser.add_argument('--query-timeout', type=int, default=3600, help='查询及导入超时秒，默认 3600')


def make_parser():
    parser = argparse.ArgumentParser(description='StarRocks 3.5.7 SQL 导出与导入',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog='示例：\n'
        '  StarMigrtCli dump --host source-fe --user reader --database sales --output sales.sql\n'
        '  StarMigrtCli load --host target-fe --user writer --input sales.sql\n'
        '生产迁移：先暂停源端写入、DDL 和分区调度；导入到空目标，自动校验后再切换业务。')
    parser.add_argument('--version', action='version', version='StarMigrtCli ' + VERSION)
    commands = parser.add_subparsers(dest='command', required=True)
    dump = commands.add_parser('dump', help='导出一个 SQL 文件', description='导出库表结构及数据；源端只读。')
    add_connection(dump)
    selection = dump.add_mutually_exclusive_group(required=True)
    selection.add_argument('--database', action='append', help='源库，可重复')
    selection.add_argument('--databases', nargs='+', help='多个源库，以空格分隔')
    selection.add_argument('--all-databases', action='store_true', help='当前账号可见的全部业务库')
    dump.add_argument('--output', type=Path, help='新 SQL 文件；.gz 后缀自动压缩，不覆盖已有文件')
    dump.add_argument('--check', action='store_true', help='只检查范围、结构和类型，不读取表数据或生成文件')
    dump.add_argument('--table', action='append', default=[], metavar='[DB.]PATTERN', help='选择表，支持通配符，可重复')
    dump.add_argument('--exclude-table', action='append', default=[], metavar='[DB.]PATTERN', help='排除表，可重复')
    dump.add_argument('--exclude-database', action='append', default=[], metavar='PATTERN', help='排除库，可重复')
    dump.add_argument('--target-database', help='单库导出时修改 SQL 中的目标库名；默认同源库')
    dump.add_argument('--database-map', action='append', default=[], metavar='SOURCE=TARGET', help='多库导出时修改目标库名，可重复')
    mode = dump.add_mutually_exclusive_group()
    mode.add_argument('--schema-only', action='store_true', help='只导出结构')
    mode.add_argument('--data-only', action='store_true', help='只导出数据；导入时要求目标已有空表')
    dump.add_argument('--replication-num', type=int, help='目标副本数；默认保留源值')
    dump.add_argument('--batch-rows', type=int, default=500, help='每条 INSERT 最多行数，默认 500')
    dump.add_argument('--batch-mib', type=int, default=1, help='每条 INSERT 最大 MiB，默认 1')
    load = commands.add_parser('load', help='导入 SQL 并自动校验', description='校验文件、检查空目标、顺序导入、核对行数和内容。')
    add_connection(load)
    load.add_argument('--input', type=Path, required=True, help='本工具导出的 SQL 文件；.gz 自动解压')
    return parser


def parse_args(argv):
    parser = make_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.connect_timeout <= 120 or not 1 <= args.query_timeout <= 86400:
        parser.error('端口范围 1–65535；连接超时 1–120 秒；查询超时 1–86400 秒')
    if args.command == 'dump':
        if args.check and args.output:
            parser.error('--check 不生成文件，无需 --output')
        if not args.check and not args.output:
            parser.error('导出必须指定 --output sales.sql 或 sales.sql.gz')
        if not 1 <= args.batch_rows <= 1000000 or not 1 <= args.batch_mib <= 256:
            parser.error('--batch-rows 范围 1–1000000；--batch-mib 范围 1–256')
        if args.replication_num is not None and not 1 <= args.replication_num <= 32767:
            parser.error('--replication-num 范围 1–32767')
        if args.data_only and args.replication_num is not None:
            parser.error('--data-only 不生成结构，不能修改副本数')
    return args


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with Progress() as progress:
            if args.command == 'dump':
                source = Source(args)
                with source.connect() as (conn, _):
                    plan = inspect(conn, args, progress)
                    plan['mode'] = 'schema-only' if args.schema_only else 'data-only' if args.data_only else 'schema-and-data'
                    print('[检查通过] %d 个库、%d 张表；副本数：%s' %
                          (len(plan['databases']), len(plan['tables']), args.replication_num or '保留源值'), file=sys.stderr, flush=True)
                    for table in plan['tables']:
                        print('  %s.%s → %s.%s' % (table['database'], table['name'], table['target_database'], table['name']), file=sys.stderr, flush=True)
                    if args.check:
                        print('STARMIGRT_CHECK_OK tables=%d' % len(plan['tables']))
                    else:
                        print('[开始导出] 请保持源端写入、DDL 和分区调度暂停，直到迁移校验及业务切换完成。', file=sys.stderr, flush=True)
                        dump_sql(source, conn, plan, args, progress)
            else:
                with sql_input(args.input) as data:
                    progress.update(phase='校验 SQL 文件')
                    plan, records = read_dump(data)
                    # Parse to completion before connecting or changing the target.
                    data.seek(0)
                    for _ in statements(data):
                        pass
                    source = Source(args)
                    with source.connect() as (conn, _):
                        all_rows(conn, 'SET insert_timeout = ' + str(args.query_timeout))
                        load_sql(source, conn, data, plan, records, progress)
        return 0
    except KeyboardInterrupt:
        print('STARMIGRT_STOPPED：操作已中断。导出临时文件已回收；导入已提交数据须保留核查，不能直接重放。', file=sys.stderr, flush=True)
        return 130
    except Exception as error:
        print('STARMIGRT_ERROR：' + failure_text(error), file=sys.stderr, flush=True)
        return 1


def handle_termination(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, handle_termination)
    sys.exit(main())

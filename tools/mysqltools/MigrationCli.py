#!/usr/bin/env python3
"""
MySQL 生产级数据迁移工具 - 基于官方 mysqldump/mysql

快速入门:
  python MigrationCli.py --guide          # 完整用户手册（推荐首次阅读）
  python MigrationCli.py --help           # 命令行参数速查
  python MigrationCli.py -c config.json --dry-run   # 预演

适用场景:
  - MySQL 5.7 / 8.0 / 8.4 跨版本逻辑迁移
  - GTID 复制环境 (auto/off/on/commented)
  - 大库 (>50GB) 分表迁移、压缩 dump、可恢复进度
  - 亿级行表迁移后校验

官方依据:
  - https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html
  - https://dev.mysql.com/doc/refman/8.0/en/replication-gtids.html
"""

import argparse
import atexit
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from logging.handlers import RotatingFileHandler
from typing import Dict, List, Optional, Tuple

import pymysql
from pymysql import Error as MySQLError
from pymysql.constants import CLIENT

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
LOG_DIR = os.getenv("MYSQL_MIGRATION_LOG_DIR", os.path.join(os.getcwd(), "logs"))
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] [%(threadName)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

DEFINER_PATTERN = re.compile(
    r"DEFINER\s*=\s*(?:`[^`]+`@`[^`]+`|'[^']+'@'[^']+')",
    re.IGNORECASE,
)
IDENTIFIER_PATTERN = re.compile(r"^[a-zA-Z_$][a-zA-Z0-9_$]*$")
VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")


# ---------------------------------------------------------------------------
# 枚举与配置
# ---------------------------------------------------------------------------
class MigrationMode(Enum):
    STRUCTURE_ONLY = "structure_only"
    STRUCTURE_AND_DATA = "structure_and_data"


class GtidMode(Enum):
    AUTO = "auto"
    OFF = "off"
    ON = "on"
    COMMENTED = "commented"


class SslMode(Enum):
    DISABLED = "DISABLED"
    PREFERRED = "PREFERRED"
    REQUIRED = "REQUIRED"


@dataclass
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    mode: MigrationMode = MigrationMode.STRUCTURE_AND_DATA


@dataclass
class MigrationOptions:
    """全局迁移选项 - 可通过 CLI 或配置文件 options 段覆盖"""
    dry_run: bool = False
    keep_dump_files: bool = False
    gtid_mode: GtidMode = GtidMode.AUTO
    replication_target: bool = False
    ssl_mode: SslMode = SslMode.PREFERRED
    add_drop_table: bool = False
    force_overwrite: bool = False
    skip_target_empty_check: bool = False
    fix_definer: bool = True
    compress_dump: bool = True
    per_table: bool = True
    max_workers: int = 2
    max_workers_per_target_host: int = 1
    dump_timeout: int = 86400
    import_timeout: int = 172800
    exact_row_count: bool = False
    row_count_threshold: int = 10_000_000
    row_count_tolerance_pct: float = 5.0
    rollback_on_failure: bool = False
    report_dir: str = LOG_DIR
    net_read_timeout: int = 3600
    net_write_timeout: int = 3600
    disk_space_margin: float = 1.3
    skip_version_check: bool = False
    complete_insert: bool = False


@dataclass
class MigrationTask:
    source: DatabaseConfig
    target: DatabaseConfig
    dry_run: bool = False
    options: MigrationOptions = field(default_factory=MigrationOptions)


@dataclass
class ServerInfo:
    version: str
    version_tuple: Tuple[int, int, int]
    gtid_mode: str
    gtid_enabled: bool
    server_id: int


@dataclass
class TableInfo:
    name: str
    engine: str
    row_estimate: int
    data_bytes: int


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
class MigrationLogger:
    _initialized = False
    _lock = threading.Lock()

    def __init__(self):
        self.logger = logging.getLogger("MySQLMigration")
        with MigrationLogger._lock:
            if not MigrationLogger._initialized:
                self.setup_logging()
                MigrationLogger._initialized = True

    def setup_logging(self):
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)
        if self.logger.hasHandlers():
            return

        formatter = logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT)
        self.logger.setLevel(DEFAULT_LOG_LEVEL)
        self.logger.propagate = False

        log_file = os.path.join(
            LOG_DIR, f"migration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        )
        file_handler = RotatingFileHandler(
            log_file, maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(lambda r: not getattr(r, "skip_file", False))
        self.logger.addHandler(file_handler)

        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.addFilter(lambda r: getattr(r, "to_stdout", False))
        self.logger.addHandler(stdout_handler)

    def log_step(self, message: str, level=logging.INFO, to_stdout: bool = True):
        self.logger.log(level, f"[STEP] {message}", extra={"to_stdout": to_stdout})

    def log_progress(self, message: str, to_stdout: bool = True):
        self.logger.info(f"[PROGRESS] {message}", extra={"to_stdout": to_stdout})

    def log_warning(self, message: str, to_stdout: bool = True):
        self.logger.warning(f"[WARN] {message}", extra={"to_stdout": to_stdout})

    def log_error(self, message: str, to_stdout: bool = True):
        self.logger.error(f"[ERROR] {message}", extra={"to_stdout": to_stdout})

    def log_command(self, message: str, to_stdout: bool = False):
        self.logger.info(f"[CMD] {message}", extra={"to_stdout": to_stdout})


# ---------------------------------------------------------------------------
# 进程注册表 (信号中断时终止子进程)
# ---------------------------------------------------------------------------
class ProcessRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._processes: List[subprocess.Popen] = []

    def register(self, proc: subprocess.Popen):
        with self._lock:
            self._processes.append(proc)

    def unregister(self, proc: subprocess.Popen):
        with self._lock:
            if proc in self._processes:
                self._processes.remove(proc)

    def terminate_all(self, grace_seconds: int = 10):
        with self._lock:
            active = [p for p in self._processes if p.poll() is None]
        for proc in active:
            try:
                proc.terminate()
            except OSError:
                pass
        deadline = time.time() + grace_seconds
        for proc in active:
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.2)
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
        with self._lock:
            self._processes.clear()


# ---------------------------------------------------------------------------
# 目标主机并发限流
# ---------------------------------------------------------------------------
class HostConcurrencyLimiter:
    def __init__(self, max_per_host: int = 1):
        self._max = max(1, max_per_host)
        self._lock = threading.Lock()
        self._semaphores: Dict[str, threading.Semaphore] = {}

    def _key(self, host: str, port: int) -> str:
        return f"{host}:{port}"

    @contextmanager
    def acquire(self, host: str, port: int):
        with self._lock:
            key = self._key(host, port)
            if key not in self._semaphores:
                self._semaphores[key] = threading.Semaphore(self._max)
            sem = self._semaphores[key]
        sem.acquire()
        try:
            yield
        finally:
            sem.release()


# ---------------------------------------------------------------------------
# 凭证文件 (替代 MYSQL_PWD)
# ---------------------------------------------------------------------------
class CredentialManager:
    @staticmethod
    @contextmanager
    def defaults_extra_file(config: DatabaseConfig, options: MigrationOptions):
        fd, path = tempfile.mkstemp(prefix="mysql_migration_", suffix=".cnf")
        try:
            os.chmod(path, 0o600)
            lines = [
                "[client]",
                f"host={config.host}",
                f"port={config.port}",
                f"user={config.user}",
                f"password={config.password}",
                "default-character-set=utf8mb4",
                f"ssl-mode={options.ssl_mode.value}",
                f"net_read_timeout={options.net_read_timeout}",
                f"net_write_timeout={options.net_write_timeout}",
            ]
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
            yield path
        finally:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def parse_version(version_str: str) -> Tuple[int, int, int]:
    match = VERSION_PATTERN.search(version_str or "")
    if not match:
        return (0, 0, 0)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.2f} KB"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / 1024 ** 2:.2f} MB"
    return f"{num_bytes / 1024 ** 3:.2f} GB"


def safe_parse_migration_mode(mode_str: str) -> MigrationMode:
    if not mode_str:
        return MigrationMode.STRUCTURE_AND_DATA
    mode_map = {
        "structure_only": MigrationMode.STRUCTURE_ONLY,
        "structure_and_data": MigrationMode.STRUCTURE_AND_DATA,
        "data_only": MigrationMode.STRUCTURE_AND_DATA,
    }
    return mode_map.get(mode_str.lower().strip(), MigrationMode.STRUCTURE_AND_DATA)


def safe_parse_gtid_mode(value: str) -> GtidMode:
    if not value:
        return GtidMode.AUTO
    mode_map = {m.value: m for m in GtidMode}
    return mode_map.get(value.lower().strip(), GtidMode.AUTO)


def safe_parse_ssl_mode(value: str) -> SslMode:
    if not value:
        return SslMode.PREFERRED
    upper = value.upper().strip()
    for mode in SslMode:
        if mode.value == upper:
            return mode
    return SslMode.PREFERRED


def resolve_env_in_string(value: str) -> str:
    """支持 ${ENV_VAR} 环境变量替换"""
    if not isinstance(value, str):
        return value

    def replacer(match):
        var = match.group(1)
        return os.environ.get(var, "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replacer, value)


def build_migration_options(args, config_options: Optional[dict] = None) -> MigrationOptions:
    cfg = config_options or {}
    opts = MigrationOptions()

    def pick(name, arg_val, cast=str):
        if arg_val is not None and arg_val is not False:
            return cast(arg_val) if cast != str else arg_val
        if name in cfg and cfg[name] is not None:
            val = cfg[name]
            return cast(val) if cast != str else val
        return getattr(opts, name)

    opts.dry_run = bool(args.dry_run)
    opts.keep_dump_files = bool(args.keep_dump_files)
    opts.gtid_mode = safe_parse_gtid_mode(
        args.gtid_mode if args.gtid_mode else cfg.get("gtid_mode", "auto")
    )
    opts.replication_target = bool(
        args.replication_target or cfg.get("replication_target", False)
    )
    opts.ssl_mode = safe_parse_ssl_mode(
        args.ssl_mode if args.ssl_mode else cfg.get("ssl_mode", "PREFERRED")
    )
    opts.add_drop_table = bool(
        args.add_drop_table or cfg.get("add_drop_table", False)
    )
    opts.force_overwrite = bool(
        args.force_overwrite or cfg.get("force_overwrite", False)
    )
    opts.skip_target_empty_check = bool(
        args.skip_target_empty_check or cfg.get("skip_target_empty_check", False)
    )
    opts.fix_definer = not bool(args.no_fix_definer or cfg.get("fix_definer") is False)
    opts.compress_dump = not bool(args.no_compress or cfg.get("compress_dump") is False)
    opts.per_table = not bool(args.no_per_table or cfg.get("per_table") is False)
    opts.max_workers = int(args.max_workers if args.max_workers else cfg.get("max_workers", 2))
    opts.max_workers_per_target_host = int(
        args.max_workers_per_target_host
        if getattr(args, "max_workers_per_target_host", None)
        else cfg.get("max_workers_per_target_host", 1)
    )
    opts.dump_timeout = int(
        args.dump_timeout if args.dump_timeout else cfg.get("dump_timeout", 86400)
    )
    opts.import_timeout = int(
        args.import_timeout if args.import_timeout else cfg.get("import_timeout", 172800)
    )
    opts.exact_row_count = bool(
        args.exact_row_count or cfg.get("exact_row_count", False)
    )
    opts.row_count_threshold = int(cfg.get("row_count_threshold", opts.row_count_threshold))
    opts.row_count_tolerance_pct = float(
        cfg.get("row_count_tolerance_pct", opts.row_count_tolerance_pct)
    )
    opts.rollback_on_failure = bool(
        args.rollback_on_failure or cfg.get("rollback_on_failure", False)
    )
    opts.report_dir = str(
        args.report_dir if args.report_dir else cfg.get("report_dir", LOG_DIR)
    )
    opts.net_read_timeout = int(cfg.get("net_read_timeout", opts.net_read_timeout))
    opts.net_write_timeout = int(cfg.get("net_write_timeout", opts.net_write_timeout))
    opts.disk_space_margin = float(cfg.get("disk_space_margin", opts.disk_space_margin))
    opts.skip_version_check = bool(
        args.skip_version_check or cfg.get("skip_version_check", False)
    )
    opts.complete_insert = bool(
        args.complete_insert or cfg.get("complete_insert", False)
    )
    return opts


# ---------------------------------------------------------------------------
# 数据库连接器
# ---------------------------------------------------------------------------
class DatabaseConnector:
    def __init__(self, config: DatabaseConfig, options: Optional[MigrationOptions] = None):
        self.config = config
        self.options = options or MigrationOptions()
        self.logger = MigrationLogger()

    @contextmanager
    def get_connection(self, database: Optional[str] = None):
        conn = None
        db = database if database is not None else self.config.database
        ssl_args = {}
        if self.options.ssl_mode == SslMode.REQUIRED:
            ssl_args["ssl"] = {"check_hostname": False}
        try:
            conn = pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=db,
                charset="utf8mb4",
                client_flag=CLIENT.MULTI_STATEMENTS,
                connect_timeout=30,
                read_timeout=max(600, self.options.net_read_timeout),
                write_timeout=max(600, self.options.net_write_timeout),
                **ssl_args,
            )
            yield conn
        except pymysql.err.OperationalError as e:
            error_code = e.args[0] if e.args else None
            if error_code == 2003:
                msg = (
                    f"无法连接到 MySQL {self.config.host}:{self.config.port}，"
                    "请检查服务、地址、端口与防火墙"
                )
            elif error_code == 1045:
                msg = "认证失败，请检查用户名和密码"
            elif error_code == 1049:
                msg = f"数据库 {db} 不存在"
            else:
                msg = f"MySQL 连接错误 (code={error_code}): {e}"
            self.logger.log_error(
                f"连接失败 {self.config.host}:{self.config.port}/{db} - {msg}",
                to_stdout=True,
            )
            raise ConnectionError(msg) from e
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    @staticmethod
    def validate_identifier(identifier: str) -> str:
        if not identifier or not IDENTIFIER_PATTERN.match(identifier):
            raise ValueError(f"Invalid identifier: {identifier}")
        return identifier

    def get_server_info(self) -> ServerInfo:
        with self.get_connection("mysql") as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT VERSION()")
                version = cursor.fetchone()[0]
                cursor.execute("SELECT @@GLOBAL.gtid_mode, @@GLOBAL.server_id")
                gtid_mode, server_id = cursor.fetchone()
                gtid_enabled = str(gtid_mode).upper() in ("ON", "ON_PERMISSIVE")
                return ServerInfo(
                    version=version,
                    version_tuple=parse_version(version),
                    gtid_mode=str(gtid_mode),
                    gtid_enabled=gtid_enabled,
                    server_id=int(server_id),
                )

    def get_tables(self) -> List[TableInfo]:
        db = self.validate_identifier(self.config.database)
        with self.get_connection(db) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT table_name, engine,
                           COALESCE(table_rows, 0),
                           COALESCE(data_length, 0) + COALESCE(index_length, 0)
                    FROM information_schema.tables
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                    ORDER BY (data_length + index_length) ASC
                    """,
                    (db,),
                )
                return [
                    TableInfo(name=r[0], engine=(r[1] or "UNKNOWN").upper(),
                              row_estimate=int(r[2] or 0), data_bytes=int(r[3] or 0))
                    for r in cursor.fetchall()
                ]

    def estimate_database_bytes(self) -> int:
        db = self.validate_identifier(self.config.database)
        with self.get_connection("information_schema") as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(data_length + index_length), 0)
                    FROM tables WHERE table_schema = %s
                    """,
                    (db,),
                )
                return int(cursor.fetchone()[0] or 0)

    def count_tables(self) -> int:
        db = self.validate_identifier(self.config.database)
        with self.get_connection("information_schema") as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM tables WHERE table_schema=%s AND table_type='BASE TABLE'",
                    (db,),
                )
                return int(cursor.fetchone()[0])

    def target_has_data(self) -> bool:
        return self.count_tables() > 0

    def create_database_if_not_exists(self):
        db_name = self.validate_identifier(self.config.database)
        temp = DatabaseConfig(
            host=self.config.host, port=self.config.port,
            user=self.config.user, password=self.config.password,
            database="mysql", mode=self.config.mode,
        )
        with DatabaseConnector(temp, self.options).get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                    "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                )
                conn.commit()

    def drop_database(self):
        db_name = self.validate_identifier(self.config.database)
        temp = DatabaseConfig(
            host=self.config.host, port=self.config.port,
            user=self.config.user, password=self.config.password,
            database="mysql", mode=self.config.mode,
        )
        with DatabaseConnector(temp, self.options).get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(f"DROP DATABASE IF EXISTS `{db_name}`")
                conn.commit()

    def get_table_row_count(self, table: str, exact: bool = False) -> int:
        table = self.validate_identifier(table)
        db = self.validate_identifier(self.config.database)
        if exact:
            with self.get_connection(db) as conn:
                with conn.cursor() as cursor:
                    cursor.execute(f"SELECT COUNT(*) FROM `{table}`")
                    return int(cursor.fetchone()[0])
        with self.get_connection("information_schema") as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COALESCE(table_rows, 0) FROM tables "
                    "WHERE table_schema=%s AND table_name=%s",
                    (db, table),
                )
                row = cursor.fetchone()
                return int(row[0] if row else 0)


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------
class PreflightChecker:
    def __init__(self, options: MigrationOptions):
        self.options = options
        self.logger = MigrationLogger()

    def check_config_file_permissions(self, path: str):
        if not os.path.isfile(path):
            return
        mode = os.stat(path).st_mode & 0o777
        if mode & 0o077:
            self.logger.log_warning(
                f"配置文件 {path} 权限过宽 ({oct(mode)})，建议 chmod 600",
                to_stdout=True,
            )

    def check_disk_space(self, required_bytes: int, path: str):
        usage = shutil.disk_usage(path)
        need = int(required_bytes * self.options.disk_space_margin)
        if usage.free < need:
            raise RuntimeError(
                f"磁盘空间不足: 需要约 {format_bytes(need)}，"
                f"可用 {format_bytes(usage.free)} (路径: {path})"
            )
        self.logger.log_progress(
            f"磁盘空间检查通过: 需要 {format_bytes(need)}，可用 {format_bytes(usage.free)}",
            to_stdout=True,
        )

    def check_version_compatibility(
        self, source: ServerInfo, target: ServerInfo, client_version: str
    ):
        if self.options.skip_version_check:
            return
        src = source.version_tuple
        tgt = target.version_tuple
        if src > tgt:
            self.logger.log_warning(
                f"源版本 ({source.version}) 高于目标 ({target.version})，请确认兼容性",
                to_stdout=True,
            )
        if src[0] < 8 and tgt[0] >= 8:
            self.logger.log_progress(
                "检测到 5.x -> 8.x 迁移，已启用 --column-statistics=0 等跨版本参数",
                to_stdout=True,
            )
        client = parse_version(client_version)
        if client[0] >= 8 and src[0] < 8:
            self.logger.log_progress(
                "mysqldump 8.x 客户端连接 5.x 源库，使用 --column-statistics=0",
                to_stdout=False,
            )

    def check_storage_engines(self, tables: List[TableInfo]):
        non_innodb = [t for t in tables if t.engine not in ("INNODB", "UNKNOWN")]
        if non_innodb:
            names = ", ".join(f"{t.name}({t.engine})" for t in non_innodb[:10])
            self.logger.log_warning(
                f"发现非 InnoDB 表: {names}。"
                "--single-transaction 仅保证 InnoDB 一致性，MyISAM 可能不一致",
                to_stdout=True,
            )

    def check_target_empty(self, target: DatabaseConnector, force: bool):
        if target.target_has_data():
            if force:
                self.logger.log_warning(
                    f"目标库 {target.config.database} 非空，已启用 --force-overwrite",
                    to_stdout=True,
                )
            else:
                raise RuntimeError(
                    f"目标库 {target.config.database} 已有表，"
                    "请使用空库或 --force-overwrite"
                )

    def resolve_gtid_purged(
        self, source: ServerInfo, target: ServerInfo, options: MigrationOptions
    ) -> str:
        mode = options.gtid_mode
        if mode == GtidMode.OFF:
            return "OFF"
        if mode == GtidMode.ON:
            return "ON"
        if mode == GtidMode.COMMENTED:
            return "COMMENTED"
        # AUTO
        if options.replication_target and source.gtid_enabled:
            self.logger.log_progress(
                "GTID AUTO: 复制目标，使用 COMMENTED（保留 GTID 信息但不自动执行）",
                to_stdout=True,
            )
            return "COMMENTED"
        if source.gtid_enabled:
            self.logger.log_progress(
                "GTID AUTO: 独立迁移，使用 OFF",
                to_stdout=True,
            )
        return "OFF"


# ---------------------------------------------------------------------------
# Dump 后处理
# ---------------------------------------------------------------------------
class DumpPostProcessor:
    @staticmethod
    def fix_definer(source_path: str, dest_path: Optional[str] = None) -> str:
        out_path = dest_path or source_path
        tmp_path = out_path + ".definer_tmp"
        with open(source_path, "r", encoding="utf-8", errors="replace") as inf, open(
            tmp_path, "w", encoding="utf-8"
        ) as outf:
            for line in inf:
                outf.write(DEFINER_PATTERN.sub("DEFINER=CURRENT_USER", line))
        if dest_path is None:
            os.replace(tmp_path, source_path)
            return source_path
        os.replace(tmp_path, out_path)
        if dest_path != source_path and os.path.exists(source_path):
            os.remove(source_path)
        return out_path

    @staticmethod
    def compress_file(source_path: str) -> str:
        gz_path = source_path + ".gz"
        with open(source_path, "rb") as inf, gzip.open(gz_path, "wb", compresslevel=6) as outf:
            shutil.copyfileobj(inf, outf, length=1024 * 1024)
        os.remove(source_path)
        return gz_path


# ---------------------------------------------------------------------------
# mysqldump / mysql 管理
# ---------------------------------------------------------------------------
class MySQLDumpManager:
    def __init__(
        self,
        logger: MigrationLogger,
        options: MigrationOptions,
        process_registry: ProcessRegistry,
        keep_dump_files: bool = False,
    ):
        self.logger = logger
        self.options = options
        self.process_registry = process_registry
        self.keep_dump_files = keep_dump_files
        self.temp_dir = tempfile.mkdtemp(prefix="mysql_migration_")
        self._lock = threading.Lock()
        self._active_files: set = set()
        self._gtid_purged = "OFF"
        self._skip_lock_tables = False
        atexit.register(self._atexit_cleanup)

    def set_dump_context(self, gtid_purged: str, skip_lock_tables: bool):
        self._gtid_purged = gtid_purged
        self._skip_lock_tables = skip_lock_tables

    def _atexit_cleanup(self):
        if not self.keep_dump_files:
            self.cleanup()

    def cleanup(self):
        if not os.path.exists(self.temp_dir):
            return
        try:
            with self._lock:
                for fp in list(self._active_files):
                    try:
                        if os.path.exists(fp):
                            os.remove(fp)
                    except OSError:
                        pass
                self._active_files.clear()
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.logger.log_progress("临时文件清理完成", to_stdout=False)
        except Exception as e:
            self.logger.log_warning(f"清理临时文件失败: {e}", to_stdout=False)

    @staticmethod
    def _validate_command_arg(value: str, arg_name: str) -> str:
        if not value:
            raise ValueError(f"{arg_name} cannot be empty")
        for char in (";", "&", "|", "`", "$", "(", ")", "<", ">", "\n", "\r"):
            if char in value:
                raise ValueError(f"{arg_name} contains dangerous character: {char}")
        return value

    def _register_dump_path(self, path: str) -> str:
        abs_path = os.path.abspath(path)
        with self._lock:
            if not abs_path.startswith(os.path.abspath(self.temp_dir)):
                raise ValueError(f"Dump path outside temp directory: {path}")
            self._active_files.add(abs_path)
        return abs_path

    def _get_dump_filename(self, config: DatabaseConfig, suffix: str = "") -> str:
        safe_db = re.sub(r"[^a-zA-Z0-9_]", "_", config.database)[:50]
        db_hash = hashlib.md5(config.database.encode()).hexdigest()[:6]
        ts = datetime.now().strftime("%H%M%S")
        tid = threading.current_thread().name.replace("ThreadPoolExecutor", "t")[:8]
        uid = uuid.uuid4().hex[:4]
        ext = suffix or ".sql"
        return f"{safe_db}_{db_hash}_{ts}_{tid}_{uid}{ext}"

    def build_mysqldump_command(
        self,
        config: DatabaseConfig,
        cnf_path: str,
        output_file: str,
        tables: Optional[List[str]] = None,
        no_data: bool = False,
        no_create_info: bool = False,
        include_schema_objects: bool = True,
    ) -> List[str]:
        host = self._validate_command_arg(config.host, "host")
        self._validate_command_arg(config.user, "user")
        self._validate_command_arg(config.database, "database")
        if not isinstance(config.port, int) or not (1 <= config.port <= 65535):
            raise ValueError(f"Invalid port: {config.port}")
        if not os.path.isabs(output_file) or ".." in output_file:
            raise ValueError(f"Invalid output path: {output_file}")

        cmd = [
            "mysqldump",
            f"--defaults-extra-file={cnf_path}",
            f"-h{host}",
            f"-P{config.port}",
            "--single-transaction",
            f"--set-gtid-purged={self._gtid_purged}",
            "--column-statistics=0",
            "--default-character-set=utf8mb4",
            "--max-allowed-packet=1G",
            "--hex-blob",
        ]
        if include_schema_objects:
            cmd.extend(["--routines", "--events", "--triggers"])
        if self._skip_lock_tables:
            cmd.append("--skip-lock-tables")
        if self.options.add_drop_table and not no_create_info:
            cmd.append("--add-drop-table")
        if no_data:
            cmd.append("--no-data")
        elif config.mode == MigrationMode.STRUCTURE_ONLY:
            cmd.append("--no-data")
        else:
            cmd.extend(["--extended-insert", "--quick", "--order-by-primary"])
            if self.options.complete_insert:
                cmd.append("--complete-insert")
        if no_create_info:
            cmd.append("--no-create-info")
        cmd.append(config.database)
        if tables:
            cmd.extend(tables)
        cmd.append(f"--result-file={output_file}")
        return cmd

    def build_mysql_command(self, config: DatabaseConfig, cnf_path: str) -> List[str]:
        host = self._validate_command_arg(config.host, "host")
        self._validate_command_arg(config.database, "database")
        return [
            "mysql",
            f"--defaults-extra-file={cnf_path}",
            f"-h{host}",
            f"-P{config.port}",
            "--max-allowed-packet=1G",
            "--connect-timeout=120",
            "--default-character-set=utf8mb4",
            config.database,
        ]

    def _run_subprocess(
        self, cmd: List[str], timeout: int, stdin_file=None, text_stderr: bool = True
    ) -> Tuple[int, str]:
        stderr_pipe = subprocess.PIPE
        proc = subprocess.Popen(
            cmd,
            stdin=stdin_file,
            stdout=subprocess.DEVNULL,
            stderr=stderr_pipe,
        )
        self.process_registry.register(proc)
        try:
            _, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate()
            raise exc
        finally:
            self.process_registry.unregister(proc)
        err = (
            stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else (stderr or "")
        )
        return proc.returncode, err.strip()

    def _post_process_dump(self, dump_file: str) -> str:
        result = dump_file
        if self.options.fix_definer:
            self.logger.log_progress("处理 DEFINER 子句...", to_stdout=False)
            DumpPostProcessor.fix_definer(result)
        if self.options.compress_dump:
            self.logger.log_progress(f"压缩 dump: {os.path.basename(result)}", to_stdout=False)
            with self._lock:
                self._active_files.discard(os.path.abspath(result))
            result = DumpPostProcessor.compress_file(result)
            self._register_dump_path(result)
        return result

    def execute_dump(
        self,
        config: DatabaseConfig,
        dry_run: bool = False,
        tables: Optional[List[str]] = None,
        no_data: bool = False,
        no_create_info: bool = False,
        include_schema_objects: bool = True,
        label: str = "",
    ) -> str:
        suffix = ".sql"
        if tables and len(tables) == 1:
            suffix = f"_{tables[0]}.sql"
        dump_file = self._register_dump_path(
            os.path.join(self.temp_dir, self._get_dump_filename(config, suffix))
        )
        with CredentialManager.defaults_extra_file(config, self.options) as cnf:
            cmd = self.build_mysqldump_command(
                config, cnf, dump_file, tables=tables,
                no_data=no_data, no_create_info=no_create_info,
                include_schema_objects=include_schema_objects,
            )
            if dry_run:
                self.logger.log_command(f"[DRY-RUN] {' '.join(cmd)}", to_stdout=True)
                return dump_file

            desc = label or config.database
            self.logger.log_step(f"导出: {desc}", to_stdout=True)
            self.logger.log_command(" ".join(cmd), to_stdout=False)
            rc, err = self._run_subprocess(cmd, self.options.dump_timeout)
            if rc != 0:
                raise RuntimeError(f"mysqldump 失败 ({desc}): {err}")

        if not os.path.exists(dump_file):
            raise FileNotFoundError(f"导出文件未生成: {dump_file}")
        size = os.path.getsize(dump_file)
        self.logger.log_progress(
            f"导出完成: {desc} ({format_bytes(size)})", to_stdout=True
        )
        return self._post_process_dump(dump_file)

    def execute_import(
        self,
        config: DatabaseConfig,
        dump_file: str,
        dry_run: bool = False,
        label: str = "",
    ):
        if dry_run:
            self.logger.log_command(f"[DRY-RUN] import {dump_file}", to_stdout=True)
            return

        abs_dump = os.path.abspath(dump_file)
        if not abs_dump.startswith(os.path.abspath(self.temp_dir)):
            raise ValueError(f"Dump 文件不在临时目录内: {dump_file}")
        if not os.path.isfile(abs_dump):
            raise ValueError(f"Dump 文件不存在: {dump_file}")

        desc = label or config.database
        self.logger.log_step(f"导入: {desc}", to_stdout=True)

        with CredentialManager.defaults_extra_file(config, self.options) as cnf:
            cmd = self.build_mysql_command(config, cnf)
            self.logger.log_command(f"{' '.join(cmd)} < {abs_dump}", to_stdout=False)

            if abs_dump.endswith(".gz"):
                with gzip.open(abs_dump, "rb") as gz:
                    rc, err = self._run_subprocess(
                        cmd, self.options.import_timeout, stdin_file=gz, text_stderr=True
                    )
            else:
                with open(abs_dump, "rb") as fh:
                    rc, err = self._run_subprocess(
                        cmd, self.options.import_timeout, stdin_file=fh, text_stderr=True
                    )
            if rc != 0:
                raise RuntimeError(f"mysql 导入失败 ({desc}): {err}")

        self.logger.log_progress(f"导入完成: {desc}", to_stdout=True)


# ---------------------------------------------------------------------------
# 迁移后校验
# ---------------------------------------------------------------------------
class MigrationValidator:
    def __init__(self, options: MigrationOptions):
        self.options = options
        self.logger = MigrationLogger()

    def validate(
        self,
        source: DatabaseConnector,
        target: DatabaseConnector,
        mode: MigrationMode,
    ) -> dict:
        report = {"table_count_match": False, "tables": [], "passed": False}
        src_tables = source.get_tables()
        tgt_tables = target.get_tables()
        src_names = {t.name for t in src_tables}
        tgt_names = {t.name for t in tgt_tables}

        if src_names != tgt_names:
            missing = src_names - tgt_names
            extra = tgt_names - src_names
            raise RuntimeError(
                f"表数量/名称不一致: 缺失 {missing or '无'}，多余 {extra or '无'}"
            )
        report["table_count_match"] = True
        self.logger.log_progress(
            f"表数量校验通过: {len(src_names)} 张表", to_stdout=True
        )

        if mode == MigrationMode.STRUCTURE_ONLY:
            report["passed"] = True
            return report

        mismatches = []
        for st in src_tables:
            use_exact = (
                self.options.exact_row_count
                or st.row_estimate <= self.options.row_count_threshold
            )
            src_rows = source.get_table_row_count(st.name, exact=use_exact)
            tgt_rows = target.get_table_row_count(st.name, exact=use_exact)
            entry = {
                "table": st.name,
                "source_rows": src_rows,
                "target_rows": tgt_rows,
                "exact": use_exact,
            }
            report["tables"].append(entry)

            if src_rows == tgt_rows:
                self.logger.log_progress(
                    f"  {st.name}: {src_rows} 行 OK", to_stdout=False
                )
                continue

            if not use_exact and src_rows > 0:
                diff_pct = abs(tgt_rows - src_rows) / src_rows * 100
                if diff_pct <= self.options.row_count_tolerance_pct:
                    self.logger.log_warning(
                        f"  {st.name}: 估算行数偏差 {diff_pct:.1f}% "
                        f"(源 {src_rows}, 目标 {tgt_rows})，在容忍范围内",
                        to_stdout=True,
                    )
                    continue

            mismatches.append(entry)
            self.logger.log_error(
                f"  {st.name}: 行数不一致 (源 {src_rows}, 目标 {tgt_rows})",
                to_stdout=True,
            )

        if mismatches:
            raise RuntimeError(
                f"{len(mismatches)} 张表行数校验失败，"
                "可使用 --exact-row-count 做精确 COUNT(*)"
            )

        report["passed"] = True
        self.logger.log_step("迁移后数据校验通过", to_stdout=True)
        return report


# ---------------------------------------------------------------------------
# 审计报告
# ---------------------------------------------------------------------------
class AuditReportWriter:
    def __init__(self, report_dir: str):
        self.report_dir = report_dir
        self.logger = MigrationLogger()
        os.makedirs(report_dir, exist_ok=True)

    def write(self, report: dict) -> str:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.report_dir, f"migration_report_{ts}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
        self.logger.log_step(f"审计报告已写入: {path}", to_stdout=True)
        return path


# ---------------------------------------------------------------------------
# 迁移管理器
# ---------------------------------------------------------------------------
class MigrationManager:
    def __init__(
        self,
        options: MigrationOptions,
        process_registry: ProcessRegistry,
        host_limiter: HostConcurrencyLimiter,
    ):
        self.options = options
        self.logger = MigrationLogger()
        self.process_registry = process_registry
        self.host_limiter = host_limiter
        self.preflight = PreflightChecker(options)
        self.validator = MigrationValidator(options)
        self.dump_manager = MySQLDumpManager(
            self.logger, options, process_registry, keep_dump_files=options.keep_dump_files
        )
        self.migration_progress: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def _update_progress(self, task_id: str, updates: dict):
        with self._lock:
            self.migration_progress.setdefault(task_id, {}).update(updates)

    def _migrate_whole_database(self, task: MigrationTask, task_id: str):
        opts = task.options or self.options
        dump_file = self.dump_manager.execute_dump(task.source, task.dry_run)
        self._update_progress(task_id, {"dump_file": dump_file, "mode": "whole_db"})
        if not task.dry_run:
            DatabaseConnector(task.target, opts).create_database_if_not_exists()
        self.dump_manager.execute_import(task.target, dump_file, task.dry_run)

    def _migrate_per_table(self, task: MigrationTask, task_id: str):
        opts = task.options or self.options
        source_conn = DatabaseConnector(task.source, opts)
        tables = source_conn.get_tables()
        if not tables:
            raise RuntimeError(f"源库 {task.source.database} 无基表")

        completed = self.migration_progress.get(task_id, {}).get("tables_done", [])

        if not task.dry_run:
            DatabaseConnector(task.target, opts).create_database_if_not_exists()

        # 1) 结构 (含 routines/events/triggers/views)
        if "__schema__" not in completed:
            schema_file = self.dump_manager.execute_dump(
                task.source, task.dry_run, no_data=True, label=f"{task.source.database} [schema]"
            )
            if not task.dry_run:
                self.dump_manager.execute_import(
                    task.target, schema_file, label=f"{task.target.database} [schema]"
                )
            completed.append("__schema__")
            self._update_progress(task_id, {"tables_done": list(completed)})

        if task.source.mode == MigrationMode.STRUCTURE_ONLY:
            return

        # 2) 逐表数据
        for tbl in tables:
            if tbl.name in completed:
                continue
            data_file = self.dump_manager.execute_dump(
                task.source, task.dry_run,
                tables=[tbl.name], no_create_info=True,
                include_schema_objects=False,
                label=f"{task.source.database}.{tbl.name}",
            )
            if not task.dry_run:
                self.dump_manager.execute_import(
                    task.target, data_file,
                    label=f"{task.target.database}.{tbl.name}",
                )
            completed.append(tbl.name)
            self._update_progress(task_id, {"tables_done": list(completed)})
            self.logger.log_progress(
                f"表进度: {len(completed)-1}/{len(tables)} - {tbl.name}",
                to_stdout=True,
            )

    def execute_migration(self, task: MigrationTask) -> dict:
        task_id = f"{task.source.database}->{task.target.database}"
        opts = task.options or self.options
        audit = {
            "task_id": task_id,
            "start_time": datetime.now().isoformat(),
            "status": "running",
            "source": {"host": task.source.host, "database": task.source.database},
            "target": {"host": task.target.host, "database": task.target.database},
            "options": {k: (v.value if isinstance(v, Enum) else v) for k, v in asdict(opts).items()},
        }

        target_key = (task.target.host, task.target.port)
        prev_dump_opts = self.dump_manager.options
        self.dump_manager.options = opts
        with self.host_limiter.acquire(*target_key):
            try:
                self.logger.log_step(f"开始迁移: {task_id}", to_stdout=True)
                self._update_progress(task_id, {"status": "running", "start_time": datetime.now()})

                if not task.dry_run:
                    src_conn = DatabaseConnector(task.source, opts)
                    tgt_conn = DatabaseConnector(task.target, opts)

                    src_info = src_conn.get_server_info()
                    tgt_info = tgt_conn.get_server_info()
                    audit["source"]["version"] = src_info.version
                    audit["target"]["version"] = tgt_info.version
                    audit["source"]["gtid"] = src_info.gtid_mode
                    audit["target"]["gtid"] = tgt_info.gtid_mode

                    client_ver = subprocess.check_output(
                        ["mysqldump", "--version"], text=True, stderr=subprocess.STDOUT
                    ).strip()
                    audit["client_version"] = client_ver
                    self.preflight.check_version_compatibility(src_info, tgt_info, client_ver)

                    tables = src_conn.get_tables()
                    self.preflight.check_storage_engines(tables)
                    est = src_conn.estimate_database_bytes()
                    audit["estimated_bytes"] = est
                    self.preflight.check_disk_space(est, self.dump_manager.temp_dir)

                    if not opts.skip_target_empty_check:
                        self.preflight.check_target_empty(tgt_conn, opts.force_overwrite)

                    gtid = self.preflight.resolve_gtid_purged(src_info, tgt_info, opts)
                    non_innodb = any(t.engine not in ("INNODB", "UNKNOWN") for t in tables)
                    self.dump_manager.set_dump_context(
                        gtid_purged=gtid, skip_lock_tables=non_innodb
                    )
                    audit["gtid_purged"] = gtid

                use_per_table = opts.per_table and task.source.mode == MigrationMode.STRUCTURE_AND_DATA
                if use_per_table:
                    self._migrate_per_table(task, task_id)
                    audit["migration_strategy"] = "per_table"
                else:
                    self._migrate_whole_database(task, task_id)
                    audit["migration_strategy"] = "whole_db"

                validation = None
                if not task.dry_run:
                    validation = self.validator.validate(
                        DatabaseConnector(task.source, opts),
                        DatabaseConnector(task.target, opts),
                        task.source.mode,
                    )
                    audit["validation"] = validation

                audit["status"] = "completed"
                audit["end_time"] = datetime.now().isoformat()
                self._update_progress(task_id, {"status": "completed", "end_time": datetime.now()})
                self.logger.log_step(f"迁移完成: {task_id}", to_stdout=True)
                return audit

            except Exception as exc:
                audit["status"] = "failed"
                audit["error"] = str(exc)
                audit["end_time"] = datetime.now().isoformat()
                self._update_progress(task_id, {
                    "status": "failed", "error": str(exc), "end_time": datetime.now(),
                })
                self.logger.log_error(f"迁移失败 {task_id}: {exc}", to_stdout=True)

                if opts.rollback_on_failure and not task.dry_run:
                    try:
                        self.logger.log_warning(
                            f"回滚: 删除目标库 {task.target.database}", to_stdout=True
                        )
                        DatabaseConnector(task.target, opts).drop_database()
                        audit["rollback"] = "dropped_target_database"
                    except Exception as rb_exc:
                        audit["rollback"] = f"failed: {rb_exc}"
                        self.logger.log_error(f"回滚失败: {rb_exc}", to_stdout=True)
                raise
            finally:
                self.dump_manager.options = prev_dump_opts


# ---------------------------------------------------------------------------
# 用户手册 (--guide)
# ---------------------------------------------------------------------------
USER_GUIDE = r"""
================================================================================
                    MySQL MigrationCli 生产迁移工具 - 用户手册
================================================================================

一、工具简介
------------
本工具基于 MySQL 官方 mysqldump / mysql 客户端，实现数据库逻辑迁移（导出 SQL
再导入）。适用于 5.7 / 8.0 / 8.4 跨版本、GTID 复制、50GB+ 大库、亿级行表等
生产场景。

核心能力:
  * 分表迁移 (per_table): 先迁结构，再逐表迁数据，失败可断点续跑
  * GTID 策略: auto / off / on / commented，适配独立迁移与复制搭 slave
  * 迁移前预检: 版本、磁盘、目标库是否为空、存储引擎、GTID 状态
  * 迁移后校验: 表数量一致 + 行数对比（估算或精确 COUNT）
  * 安全: 临时 cnf 传密码(0600)、DEFINER 自动修复、目标非空保护
  * 审计: JSON 报告 + 滚动日志

依赖:
  * Python 3.6+、pymysql
  * 系统已安装 mysqldump、mysql 客户端（建议与源库大版本接近）

环境变量:
  * MYSQL_MIGRATION_LOG_DIR  日志目录，默认 ./logs
  * 配置文件中 password 支持 ${MYSQL_XXX} 引用环境变量


二、快速开始（推荐流程）
------------------------
【生产环境强烈建议使用 JSON 配置文件，而非 -s/-t 命令行传密码】

  第 1 步  复制配置模板
    cp migration_config.json.example migration_config.json
    chmod 600 migration_config.json

  第 2 步  填写源/目标连接、设置环境变量
    export MYSQL_SOURCE_PASSWORD='...'
    export MYSQL_TARGET_PASSWORD='...'

  第 3 步  预演（不连库执行 dump/import，只打印将执行的命令）
    python MigrationCli.py -c migration_config.json --dry-run

  第 4 步  正式迁移
    python MigrationCli.py -c migration_config.json

  第 5 步  检查结果
    * 终端输出: 成功 x/y，失败则 exit code=1
    * 日志: logs/migration_YYYYMMDD_HHMMSS.log
    * 审计: logs/migration_report_YYYYMMDD_HHMMSS.json


三、命令行参数详解
------------------

【连接与任务】
  -c, --config PATH
      JSON 配置文件路径（生产推荐）。可同时配置多库迁移、全局 options、
      单任务 options 覆盖。详见第四节。

  -s, --source STR
      单库迁移-源库。格式: host:port:database:user:password
      注意: password 若含冒号，从第 5 段起全部视为密码；更复杂密码请用 JSON。
      示例: 192.168.1.10:3306:mydb:migrator:Secr3t

  -t, --target STR
      单库迁移-目标库。格式同 -s。

【迁移模式】
  --dry-run
      预演模式: 执行连接预检逻辑，dump/import 只打印命令不实际运行。
      上线前必须至少跑一次。

  --structure-only
      仅迁移表结构（等同 mode=structure_only），不含数据。
      仅对 -s/-t 单库模式生效；JSON 配置请在 source.mode 中设置。

  --keep-dump-files
      迁移完成后保留 dump 文件（默认自动删除临时目录）。
      大库排查问题时建议开启；注意磁盘占用。

【并发与性能】
  --max-workers N          并行迁移任务数，默认 2，上限 min(CPU, 8)。
                           多个独立库可并行；单库内分表仍为串行导入。

  --max-workers-per-target-host N
                           同一目标主机最大并发，默认 1。
                           防止多库同时导入打满目标 IO。

  --no-per-table           关闭分表迁移，整库一次 dump/import。
                           小库(<几GB)可关闭；50GB+ 大库务必保持分表(默认)。

  --no-compress            关闭 dump gzip 压缩。默认开启以节省磁盘。

  --dump-timeout SEC       mysqldump 超时秒数，默认 86400 (24h)。
  --import-timeout SEC     mysql 导入超时秒数，默认 172800 (48h)。

  --complete-insert        dump 时 INSERT 带完整列名，文件更大更慢，一般不需要。

【GTID 与复制】
  --gtid-mode {auto,off,on,commented}
      auto       (默认) 独立迁移用 OFF；replication_target 时用 COMMENTED
      off        不写入 GTID 语句，适合普通迁库
      on         写入 SET @@GLOBAL.GTID_PURGED，用于搭建 replica（需目标无冲突）
      commented  GTID 语句写入但注释掉，便于 DBA 手工处理

  --replication-target     声明目标用于 GTID 复制从库；配合 gtid-mode=auto
                           自动选 COMMENTED。

【安全与覆盖】
  --ssl-mode {DISABLED,PREFERRED,REQUIRED}
      连接 SSL 模式，默认 PREFERRED。公网/合规场景建议 REQUIRED。

  --add-drop-table         dump 含 DROP TABLE，重复迁移时先删后建。
  --force-overwrite        目标库已有表时仍继续（默认拒绝非空目标）。
  --skip-target-empty-check  跳过目标非空检查（危险，仅特殊场景）。
  --no-fix-definer         不替换视图/存储过程 DEFINER（默认会改为 CURRENT_USER）。
  --rollback-on-failure    任务失败时 DROP 目标库（需确认可接受）。

【校验】
  --exact-row-count        迁移后用 SELECT COUNT(*) 精确对比每张表行数。
                           亿级表耗时长，建议低峰期或仅对核心库开启。
                           默认: 小表精确/估算，>1000万行用 information_schema 估算。

  --skip-version-check     跳过源/目标/client 版本兼容性提示。

【输出】
  --report-dir PATH        JSON 审计报告目录，默认 ./logs。

  --guide                  显示本手册并退出。


四、JSON 配置文件格式
---------------------

  {
    "options": { ... 全局默认，见下表 ... },
    "migrations": [
      {
        "source": { "host", "port", "user", "password", "database", "mode" },
        "target": { 同上 },
        "options": { ... 可选，覆盖本任务 ... }
      }
    ]
  }

  source / target 字段:
    host      MySQL 主机名或 IP
    port      端口，1-65535
    user      迁移账号
    password  密码，支持 "${ENV_VAR}" 从环境变量读取
    database  库名（字母数字下划线，不含连字符）
    mode      structure_only | structure_and_data （默认后者）

  options 字段（CLI 与 JSON 通用，JSON 键名为 snake_case）:

    选项名                      类型      默认值        说明
    --------------------------------------------------------------------------
    dry_run                     bool      false       预演模式
    keep_dump_files             bool      false       保留 dump 文件
    gtid_mode                   string    auto        GTID 策略，见 --gtid-mode
    replication_target          bool      false       目标为 GTID 从库
    ssl_mode                    string    PREFERRED   SSL 模式
    add_drop_table              bool      false       dump 含 DROP TABLE
    force_overwrite             bool      false       允许覆盖非空目标库
    skip_target_empty_check     bool      false       跳过目标非空检查
    fix_definer                 bool      true        修复 DEFINER 子句
    compress_dump               bool      true        gzip 压缩 dump
    per_table                   bool      true        分表迁移（大库推荐）
    max_workers                 int       2           并行任务数
    max_workers_per_target_host int       1           同目标主机并发上限
    dump_timeout                int       86400       dump 超时(秒)
    import_timeout              int       172800      import 超时(秒)
    exact_row_count             bool      false       精确 COUNT 校验
    row_count_threshold         int       10000000    超过此行数用估算校验
    row_count_tolerance_pct     float     5.0         估算行数允许偏差百分比
    rollback_on_failure         bool      false       失败时删除目标库
    report_dir                  string    ./logs      审计报告目录
    net_read_timeout            int       3600        客户端读超时(秒)
    net_write_timeout           int       3600        客户端写超时(秒)
    disk_space_margin           float     1.3         磁盘预留倍数
    skip_version_check          bool      false       跳过版本检查
    complete_insert             bool      false       dump 完整 INSERT

  优先级: 命令行 > 任务级 options > 全局 options > 内置默认值


五、典型场景示例
----------------

【场景 A】MySQL 5.7 -> 8.4 普通迁库（非复制）
  options.gtid_mode = "auto"
  options.replication_target = false
  options.per_table = true
  options.compress_dump = true

【场景 B】5.7 主库 -> 8.4 GTID 从库
  options.replication_target = true
  options.gtid_mode = "auto"        # 自动 COMMENTED
  源端账号需 RELOAD 或 FLUSH_TABLES（8.0.32+ 且 GTID 开启时）

【场景 C】50GB+ 大库，含亿级单表
  options.per_table = true          # 必须
  options.dump_timeout = 86400
  options.import_timeout = 172800
  options.keep_dump_files = true    # 首次建议保留便于排错
  校验: 默认估算；核心表可二次跑 --exact-row-count

【场景 D】仅迁结构（如预建库）
  source.mode = "structure_only"
  或 CLI: --structure-only

【场景 E】多库并行迁移到同一台目标机
  migrations: [ 任务1, 任务2, ... ]
  options.max_workers = 2
  options.max_workers_per_target_host = 1   # 避免打满目标磁盘 IO

【场景 F】重复迁移到同一目标（覆盖）
  options.force_overwrite = true
  options.add_drop_table = true           # 可选，确保表定义更新


六、迁移执行过程（便于排障）
----------------------------
  1. 预检: 连接源/目标、版本、GTID、磁盘空间、目标是否为空、存储引擎
  2. 导出:
     - 分表模式: 先 schema(含视图/存储过程/触发器/事件)，再逐表 data
     - 整库模式: 一次 mysqldump
  3. 后处理: DEFINER 修复 -> gzip 压缩(可选)
  4. 导入: mysql 客户端 stdin 流式导入
  5. 校验: 表名集合 + 行数
  6. 输出: JSON 审计报告

  分表模式进度保存在内存 migration_progress.tables_done；
  进程中断后需重新运行（已完成表会重复导出，建议 --keep-dump-files 排错）。


七、账号权限要求
----------------
  源库:
    SELECT（所有待迁表）
    SHOW VIEW（视图）
    TRIGGER（触发器）
    EVENT（事件）
    PROCESS（部分版本）
    RELOAD 或 FLUSH_TABLES（GTID + single-transaction 时，8.0.32+）

  目标库:
    CREATE, DROP, INSERT, UPDATE, DELETE, INDEX, ALTER
    CREATE ROUTINE, ALTER ROUTINE（存储过程）
    TRIGGER, EVENT


八、日志与审计
--------------
  运行日志:  logs/migration_YYYYMMDD_HHMMSS.log  (10MB 轮转，保留 10 份)
  审计报告:  logs/migration_report_YYYYMMDD_HHMMSS.json
  报告内容:  每任务版本/GTID/策略/校验结果/耗时/错误信息

  退出码:
    0  全部任务成功
    1  存在失败任务或致命错误
    130 用户 Ctrl+C 中断


九、常见问题
------------
  Q: mysqldump: Unknown table 'COLUMN_STATISTICS' in information_schema
  A: 已内置 --column-statistics=0；请确认使用本工具而非手工命令。

  Q: 导入报 DEFINER 不存在
  A: 默认 fix_definer=true；若仍失败检查 --no-fix-definer 是否误开。

  Q: GTID 相关 ERROR
  A: 独立迁库用 gtid_mode=off；搭 slave 用 replication_target + auto/commented。

  Q: 目标库非空被拒绝
  A: 使用空库，或 --force-overwrite（生产慎用）。

  Q: 磁盘空间不足
  A: 增大临时目录所在分区空间，或指定 MYSQL_MIGRATION_LOG_DIR 到大磁盘分区。

  Q: 亿级表校验太慢
  A: 不要开 --exact-row-count；依赖默认估算 + 5% 容忍；抽样可手工 COUNT。

  Q: 密码含特殊字符
  A: 使用 JSON + ${ENV_VAR}，避免 -s/-t 命令行传密码。


十、配置文件模板
----------------
  请参考同目录: migration_config.json.example
  内含 _documentation 字段说明各配置项（运行时忽略，仅文档用途）。

================================================================================
"""


class _HelpFormatter(
    argparse.RawDescriptionHelpFormatter,
    argparse.ArgumentDefaultsHelpFormatter,
):
    """同时保留换行格式与默认值显示"""
    pass


def parse_arguments():
    prog = os.path.basename(sys.argv[0])
    parser = argparse.ArgumentParser(
        prog=prog,
        description=(
            "MySQL 生产级逻辑迁移工具 (mysqldump/mysql)\n"
            "支持 5.7/8.0/8.4 跨版本、GTID、50GB+ 大库分表迁移。\n"
            "完整说明请运行: %(prog)s --guide"
        ),
        formatter_class=_HelpFormatter,
        epilog=(
            f"快速示例:\n"
            f"  {prog} --guide                              # 完整用户手册\n"
            f"  {prog} -c migration_config.json --dry-run   # 预演\n"
            f"  {prog} -c migration_config.json             # 正式迁移\n"
            f"配置模板: migration_config.json.example"
        ),
    )

    # ---- 手册 ----
    parser.add_argument(
        "--guide",
        action="store_true",
        help="显示完整用户手册（含全部参数说明、场景示例、FAQ）并退出",
    )

    # ---- 连接与任务 ----
    conn = parser.add_argument_group(
        "连接与任务",
        "指定要迁移的源库与目标库。生产环境推荐使用 -c JSON 配置文件。",
    )
    conn.add_argument(
        "-c", "--config",
        metavar="FILE",
        help=(
            "JSON 配置文件路径。可定义多库迁移任务、全局 options、"
            "单任务 options 覆盖。密码支持 ${ENV_VAR}。"
            "模板见 migration_config.json.example"
        ),
    )
    conn.add_argument(
        "-s", "--source",
        metavar="SPEC",
        help=(
            "单库模式-源库。格式 host:port:database:user:password。"
            "password 含冒号时从第5段起全部作为密码。"
            "复杂密码请改用 -c JSON 配置"
        ),
    )
    conn.add_argument(
        "-t", "--target",
        metavar="SPEC",
        help="单库模式-目标库。格式同 --source",
    )

    # ---- 迁移模式 ----
    mode_grp = parser.add_argument_group(
        "迁移模式",
        "控制迁移范围与是否实际执行。",
    )
    mode_grp.add_argument(
        "--dry-run",
        action="store_true",
        help="预演: 执行预检并打印 dump/import 命令，不实际导出导入。上线前必跑",
    )
    mode_grp.add_argument(
        "--structure-only",
        action="store_true",
        help=(
            "仅迁移表结构(无数据)，等同 mode=structure_only。"
            "仅 -s/-t 单库模式生效；JSON 请在 source.mode 设置"
        ),
    )
    mode_grp.add_argument(
        "--keep-dump-files",
        action="store_true",
        help="迁移完成后保留 dump 文件(默认自动删除临时目录)。排障时建议开启",
    )

    # ---- 并发与性能 ----
    perf = parser.add_argument_group(
        "并发与性能",
        "大库(>50GB)建议: per_table=true(默认), max_workers=2, compress_dump=true",
    )
    perf.add_argument(
        "--max-workers",
        type=int,
        default=None,
        metavar="N",
        help="并行迁移的任务数(多库场景)。默认 2，实际上限 min(N, CPU核数, 8)",
    )
    perf.add_argument(
        "--max-workers-per-target-host",
        type=int,
        default=None,
        metavar="N",
        help="同一目标 MySQL 主机最大并发迁移数。默认 1，防止 IO 打满",
    )
    perf.add_argument(
        "--no-per-table",
        action="store_true",
        help=(
            "关闭分表迁移，改为整库一次 dump/import。"
            "仅适合小库；50GB+ 或亿级表务必保持默认分表模式"
        ),
    )
    perf.add_argument(
        "--no-compress",
        action="store_true",
        help="关闭 dump 的 gzip 压缩。默认开启以节省磁盘",
    )
    perf.add_argument(
        "--dump-timeout",
        type=int,
        default=None,
        metavar="SEC",
        help="mysqldump 单步超时(秒)。默认 86400 (24 小时)",
    )
    perf.add_argument(
        "--import-timeout",
        type=int,
        default=None,
        metavar="SEC",
        help="mysql 导入单步超时(秒)。默认 172800 (48 小时)",
    )
    perf.add_argument(
        "--complete-insert",
        action="store_true",
        help="dump 时 INSERT 带完整列名。文件更大、恢复更慢，一般不需要",
    )

    # ---- GTID 与复制 ----
    gtid = parser.add_argument_group(
        "GTID 与复制",
        "GTID 环境必读。独立迁库通常 auto 即可；搭从库需 --replication-target",
    )
    gtid.add_argument(
        "--gtid-mode",
        choices=[m.value for m in GtidMode],
        default=None,
        metavar="MODE",
        help=(
            "GTID dump 策略: auto=自动(默认); off=不写入; "
            "on=写入 GTID_PURGED; commented=写入但注释。"
            "详见 --guide 第五节"
        ),
    )
    gtid.add_argument(
        "--replication-target",
        action="store_true",
        help=(
            "声明目标库用于 GTID 复制从库。"
            "配合 gtid-mode=auto 时自动使用 COMMENTED 策略"
        ),
    )

    # ---- 安全与覆盖 ----
    safety = parser.add_argument_group(
        "安全与覆盖",
        "生产默认: 目标非空拒绝、fix_definer 开启、密码走临时 cnf 文件",
    )
    safety.add_argument(
        "--ssl-mode",
        choices=[m.value for m in SslMode],
        default=None,
        metavar="MODE",
        help="MySQL 连接 SSL: DISABLED / PREFERRED(默认) / REQUIRED",
    )
    safety.add_argument(
        "--add-drop-table",
        action="store_true",
        help="dump 包含 DROP TABLE IF EXISTS，重复迁移时先删后建",
    )
    safety.add_argument(
        "--force-overwrite",
        action="store_true",
        help="目标库已有表时仍继续迁移(默认拒绝非空目标)。生产慎用",
    )
    safety.add_argument(
        "--skip-target-empty-check",
        action="store_true",
        help="跳过目标库非空检查。仅在明确要追加/覆盖数据时使用",
    )
    safety.add_argument(
        "--no-fix-definer",
        action="store_true",
        help=(
            "不将视图/存储过程的 DEFINER 替换为 CURRENT_USER。"
            "默认会修复，避免目标库无源 DEFINER 账号导致导入失败"
        ),
    )
    safety.add_argument(
        "--rollback-on-failure",
        action="store_true",
        help="迁移失败时 DROP 目标库。仅当目标库可安全删除时启用",
    )

    # ---- 校验 ----
    validate = parser.add_argument_group(
        "迁移后校验",
        "迁移完成后自动对比源/目标表数量与行数",
    )
    validate.add_argument(
        "--exact-row-count",
        action="store_true",
        help=(
            "用 SELECT COUNT(*) 精确校验每张表行数。"
            "亿级表极慢；默认对小表精确、大表用 information_schema 估算"
        ),
    )
    validate.add_argument(
        "--skip-version-check",
        action="store_true",
        help="跳过源/目标/客户端版本兼容性检查与提示",
    )

    # ---- 输出 ----
    output = parser.add_argument_group("日志与报告")
    output.add_argument(
        "--report-dir",
        default=None,
        metavar="DIR",
        help=f"JSON 审计报告输出目录。默认 {LOG_DIR}",
    )

    return parser.parse_args()


def print_user_guide():
    print(USER_GUIDE)


# ---------------------------------------------------------------------------
# CLI 与配置加载
# ---------------------------------------------------------------------------
def parse_database_config(db_str: str, mode: MigrationMode) -> DatabaseConfig:
    parts = db_str.split(":")
    if len(parts) < 5:
        raise ValueError(
            f"格式错误: {db_str}，应为 host:port:database:user:password"
            "（password 中可含冒号，会取第5段及之后全部内容）"
        )
    try:
        port = int(parts[1])
        if not (1 <= port <= 65535):
            raise ValueError(f"端口超出范围: {port}")
    except ValueError as e:
        raise ValueError(f"端口必须是整数: {parts[1]}") from e

    password = ":".join(parts[4:]).replace("\\:", ":")
    return DatabaseConfig(
        host=parts[0].strip(),
        port=port,
        database=parts[2].strip(),
        user=parts[3].strip(),
        password=password,
        mode=mode,
    )


def _parse_db_dict(data: dict, label: str) -> DatabaseConfig:
    required = ["host", "port", "user", "password", "database"]
    for field_name in required:
        if field_name not in data:
            raise ValueError(f"{label} 缺少字段: {field_name}")
    port = int(data["port"])
    if not (1 <= port <= 65535):
        raise ValueError(f"{label} 端口无效: {port}")
    return DatabaseConfig(
        host=str(data["host"]).strip(),
        port=port,
        user=str(resolve_env_in_string(str(data["user"]))).strip(),
        password=str(resolve_env_in_string(str(data["password"]))),
        database=str(data["database"]).strip(),
        mode=safe_parse_migration_mode(data.get("mode")),
    )


def load_config_file(config_path: str, args, file_options: MigrationOptions) -> List[MigrationTask]:
    logger = MigrationLogger()
    PreflightChecker(file_options).check_config_file_permissions(config_path)

    if not os.path.isfile(config_path):
        logger.log_error(f"配置文件不存在: {config_path}", to_stdout=True)
        sys.exit(1)

    with open(config_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            logger.log_error(f"JSON 格式错误: {e}", to_stdout=True)
            sys.exit(1)

    if "migrations" not in data:
        logger.log_error("配置文件缺少 migrations 字段", to_stdout=True)
        sys.exit(1)

    tasks = []
    for idx, item in enumerate(data["migrations"], 1):
        try:
            if "source" not in item or "target" not in item:
                raise ValueError("缺少 source 或 target")
            task_opts = (
                build_migration_options(args, item["options"])
                if "options" in item
                else file_options
            )
            tasks.append(MigrationTask(
                source=_parse_db_dict(item["source"], f"任务{idx}.source"),
                target=_parse_db_dict(item["target"], f"任务{idx}.target"),
                dry_run=file_options.dry_run,
                options=task_opts,
            ))
        except ValueError as e:
            logger.log_error(f"任务 {idx} 配置错误: {e}", to_stdout=True)
            sys.exit(1)

    if not tasks:
        logger.log_error("无有效迁移任务", to_stdout=True)
        sys.exit(1)

    logger.log_progress(f"已加载 {len(tasks)} 个迁移任务", to_stdout=True)
    return tasks


def check_dependencies():
    missing = [t for t in ("mysqldump", "mysql") if not shutil.which(t)]
    if missing:
        logger = MigrationLogger()
        logger.log_error(f"缺少命令: {', '.join(missing)}", to_stdout=True)
        sys.exit(1)
    try:
        ver = subprocess.check_output(
            ["mysqldump", "--version"], text=True, stderr=subprocess.STDOUT
        )
        MigrationLogger().log_progress(f"客户端: {ver.strip()}", to_stdout=False)
    except subprocess.CalledProcessError:
        pass


def setup_signal_handlers(process_registry: ProcessRegistry, dump_manager: MySQLDumpManager):
    def handler(sig, _frame):
        logger = MigrationLogger()
        logger.log_warning(f"收到信号 {sig}，终止子进程并清理...", to_stdout=True)
        process_registry.terminate_all()
        dump_manager.cleanup()
        sys.exit(130 if sig == signal.SIGINT else 1)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main():
    args = parse_arguments()
    if args.guide:
        print_user_guide()
        sys.exit(0)

    logger = MigrationLogger()
    check_dependencies()

    file_cfg = None
    if args.config and os.path.isfile(args.config):
        with open(args.config, "r", encoding="utf-8") as fh:
            file_cfg = json.load(fh).get("options")

    options = build_migration_options(args, file_cfg)
    process_registry = ProcessRegistry()
    host_limiter = HostConcurrencyLimiter(options.max_workers_per_target_host)
    migration_manager = MigrationManager(options, process_registry, host_limiter)
    setup_signal_handlers(process_registry, migration_manager.dump_manager)

    tasks: List[MigrationTask] = []

    if args.config:
        tasks = load_config_file(args.config, args, options)
    elif args.source and args.target:
        mode = (
            MigrationMode.STRUCTURE_ONLY
            if args.structure_only
            else MigrationMode.STRUCTURE_AND_DATA
        )
        tasks.append(MigrationTask(
            source=parse_database_config(args.source, mode),
            target=parse_database_config(args.target, mode),
            dry_run=options.dry_run,
            options=options,
        ))
    else:
        logger.log_error(
            "请指定迁移任务:\n"
            "  推荐: python MigrationCli.py -c migration_config.json\n"
            "  单库: python MigrationCli.py -s host:port:db:user:pass -t host:port:db:user:pass\n"
            "  手册: python MigrationCli.py --guide\n"
            "  参数: python MigrationCli.py --help",
            to_stdout=True,
        )
        sys.exit(1)

    opts = migration_manager.options
    max_workers = min(opts.max_workers, os.cpu_count() or 4, 8)
    report_writer = AuditReportWriter(opts.report_dir)

    logger.log_step(
        f"开始 {len(tasks)} 个任务 | dry_run={opts.dry_run} | "
        f"per_table={opts.per_table} | gtid={opts.gtid_mode.value}",
        to_stdout=True,
    )

    start = time.time()
    failed = 0
    audits = []

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(migration_manager.execute_migration, t): t for t in tasks
            }
            for fut in as_completed(futures):
                task = futures[fut]
                tid = f"{task.source.database}->{task.target.database}"
                try:
                    audit = fut.result()
                    audits.append(audit)
                except Exception as e:
                    failed += 1
                    audits.append({
                        "task_id": tid,
                        "status": "failed",
                        "error": str(e),
                    })
                    logger.log_error(f"任务失败 {tid}: {e}", to_stdout=True)

        elapsed = time.time() - start
        success = len(tasks) - failed
        summary = {
            "summary": {
                "total": len(tasks),
                "success": success,
                "failed": failed,
                "elapsed_seconds": round(elapsed, 2),
                "timestamp": datetime.now().isoformat(),
            },
            "tasks": audits,
        }
        report_writer.write(summary)

        if failed:
            logger.log_error(
                f"迁移结束: 成功 {success}/{len(tasks)}，失败 {failed}，"
                f"耗时 {elapsed:.0f}s",
                to_stdout=True,
            )
            sys.exit(1)

        logger.log_step(
            f"全部成功 {success}/{len(tasks)}，耗时 {elapsed:.0f}s",
            to_stdout=True,
        )
        if opts.keep_dump_files:
            logger.log_progress(
                f"dump 保留于: {migration_manager.dump_manager.temp_dir}",
                to_stdout=True,
            )
        elif not opts.dry_run:
            migration_manager.dump_manager.cleanup()

    except KeyboardInterrupt:
        process_registry.terminate_all()
        migration_manager.dump_manager.cleanup()
        sys.exit(130)


if __name__ == "__main__":
    main()

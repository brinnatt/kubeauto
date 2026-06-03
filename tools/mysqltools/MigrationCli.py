#!/usr/bin/env python3
"""
MySQL 生产级数据迁移工具 - 基于官方 mysqldump/mysql (MySQL 8.0+)

快速入门:
  python MigrationCli.py --guide          # 完整用户手册（推荐首次阅读）
  python MigrationCli.py --help           # 命令行参数速查
  python MigrationCli.py -c config.json --dry-run   # 预演

适用场景:
  - MySQL 8.0 / 8.1~8.4 / 9.0~9.x 全系列自动版本识别与兼容策略
  - GTID 复制环境 (auto/off/on/commented)
  - 大库 (>50GB) 分表迁移、压缩 dump、分表串行可观测进度
  - 亿级行表迁移后校验

官方依据:
  - https://dev.mysql.com/doc/refman/8.4/en/mysql-releases.html  (LTS/Innovation 模型)
  - https://dev.mysql.com/doc/refman/8.4/en/upgrade-paths.html   (升级路径表)
  - https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html
  - https://dev.mysql.com/doc/refman/9.4/en/mysqldump-upgrade-testing.html
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
from typing import Any, Dict, IO, List, Optional, Tuple

import pymysql
from pymysql.constants import CLIENT

MIGRATION_FAILURE_EXCEPTIONS = (
    RuntimeError,
    ConnectionError,
    ValueError,
    OSError,
    FileNotFoundError,
    subprocess.SubprocessError,
    pymysql.err.MySQLError,
)

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
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)}")

TOOL_VERSION = "2.1.0"
EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2
EXIT_INTERRUPT = 130

MAX_WORKERS_RECOMMENDED = 8
VALID_MIGRATION_MODES = frozenset({"structure_only", "structure_and_data"})
TASK_META_KEYS = frozenset({"_comment", "_documentation"})
TASK_OPTIONAL_KEYS = frozenset({
    "expected_source_version", "expected_target_version",
})

KNOWN_OPTION_KEYS = frozenset({
    "dry_run", "keep_dump_files", "gtid_mode", "replication_target", "ssl_mode",
    "add_drop_table", "force_overwrite", "skip_target_empty_check", "fix_definer",
    "compress_dump", "per_table", "max_workers", "max_workers_per_target_host",
    "dump_timeout", "import_timeout", "exact_row_count", "row_count_threshold",
    "row_count_tolerance_pct", "rollback_on_failure", "report_dir",
    "net_read_timeout", "net_write_timeout", "disk_space_margin",
    "skip_version_check", "complete_insert",
})


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
    options: MigrationOptions = field(default_factory=MigrationOptions)
    expected_source_version: Optional[str] = None
    expected_target_version: Optional[str] = None


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
# MySQL 版本识别与兼容策略引擎 (官方 LTS / Innovation 模型)
# ---------------------------------------------------------------------------
class ReleaseTrack(Enum):
    LTS = "lts"
    INNOVATION = "innovation"


class MigrationDirection(Enum):
    SAME = "same"
    UPGRADE = "upgrade"
    DOWNGRADE = "downgrade"


class MigrationCategory(Enum):
    """对应官方 Upgrade Paths 表的逻辑迁移分类。"""
    WITHIN_SERIES = "within_series"
    LTS_TO_LTS = "lts_to_lts"
    TO_INNOVATION = "to_innovation"
    INNOVATION_TO_LTS = "innovation_to_lts"
    WITHIN_INNOVATION_8 = "within_innovation_8"
    WITHIN_INNOVATION_9 = "within_innovation_9"
    DOWNGRADE = "downgrade"
    UNKNOWN_FUTURE = "unknown_future"


# 官方已发布/文档化的 8.x 系列元数据 (major, minor) -> (track, label, eol)
MYSQL_8_SERIES_META: Dict[Tuple[int, int], Tuple[ReleaseTrack, str, bool]] = {
    (8, 0): (ReleaseTrack.LTS, "8.0 LTS (Bugfix)", False),
    (8, 1): (ReleaseTrack.INNOVATION, "8.1 Innovation", True),
    (8, 2): (ReleaseTrack.INNOVATION, "8.2 Innovation", True),
    (8, 3): (ReleaseTrack.INNOVATION, "8.3 Innovation", True),
    (8, 4): (ReleaseTrack.LTS, "8.4 LTS", False),
}

MIN_MYSQL_MAJOR = 8


@dataclass(frozen=True)
class MySQLReleaseInfo:
    version_string: str
    version_tuple: Tuple[int, int, int]
    major: int
    minor: int
    patch: int
    series_id: str
    track: ReleaseTrack
    series_label: str
    is_eol: bool
    is_known_series: bool

    @property
    def short_label(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass
class DumpContext:
    """兼容策略生成的 mysqldump/mysql 参数与预检提示。"""
    column_statistics: Optional[bool] = None
    extra_mysqldump_args: List[str] = field(default_factory=list)
    extra_mysql_args: List[str] = field(default_factory=list)
    preflight_messages: List[str] = field(default_factory=list)
    require_fix_definer: bool = False


@dataclass
class MigrationCompatibilityProfile:
    source: MySQLReleaseInfo
    target: MySQLReleaseInfo
    direction: MigrationDirection
    category: MigrationCategory
    migration_label: str
    official_notes: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    dump_context: DumpContext = field(default_factory=DumpContext)
    logical_migration_supported: bool = True
    in_place_officially_supported: Optional[bool] = None

    def to_audit_dict(self) -> Dict[str, Any]:
        return {
            "migration_label": self.migration_label,
            "direction": self.direction.value,
            "category": self.category.value,
            "source_series": self.source.series_label,
            "target_series": self.target.series_label,
            "official_notes": self.official_notes,
            "warnings": self.warnings,
            "logical_migration_supported": self.logical_migration_supported,
            "in_place_officially_supported": self.in_place_officially_supported,
        }


def format_version_tuple(version_tuple: Tuple[int, int, int]) -> str:
    return f"{version_tuple[0]}.{version_tuple[1]}.{version_tuple[2]}"


def assert_mysql8_plus(version_tuple: Tuple[int, int, int], label: str):
    if version_tuple[0] < MIN_MYSQL_MAJOR:
        raise RuntimeError(
            f"{label} 版本 {format_version_tuple(version_tuple)} 不受支持，"
            f"本工具仅支持 MySQL {MIN_MYSQL_MAJOR}.0+"
        )


def classify_release(version_string: str, version_tuple: Tuple[int, int, int]) -> MySQLReleaseInfo:
    assert_mysql8_plus(version_tuple, "服务器")
    major, minor, patch = version_tuple

    if major == 8:
        meta = MYSQL_8_SERIES_META.get((major, minor))
        if meta:
            track, label, eol = meta
            return MySQLReleaseInfo(
                version_string=version_string,
                version_tuple=version_tuple,
                major=major, minor=minor, patch=patch,
                series_id=f"{major}.{minor}",
                track=track, series_label=label, is_eol=eol, is_known_series=True,
            )
        if minor > 4:
            return MySQLReleaseInfo(
                version_string=version_string,
                version_tuple=version_tuple,
                major=major, minor=minor, patch=patch,
                series_id=f"{major}.{minor}",
                track=ReleaseTrack.INNOVATION,
                series_label=f"8.{minor} (未在元数据登记的新系列)",
                is_eol=False, is_known_series=False,
            )
        raise RuntimeError(
            f"无法识别的 MySQL 8.x 次版本 {major}.{minor}，"
            "请确认服务器 VERSION() 输出正确"
        )

    if major >= 9:
        return MySQLReleaseInfo(
            version_string=version_string,
            version_tuple=version_tuple,
            major=major, minor=minor, patch=patch,
            series_id=f"{major}.{minor}",
            track=ReleaseTrack.INNOVATION,
            series_label=f"{major}.{minor} Innovation",
            is_eol=False, is_known_series=True,
        )

    raise RuntimeError(
        f"MySQL {major}.{minor} 不在支持范围，本工具仅支持 8.0+ 及 9.x Innovation"
    )


def _is_lts_series(info: MySQLReleaseInfo) -> bool:
    return info.track == ReleaseTrack.LTS


def _is_innovation_8(info: MySQLReleaseInfo) -> bool:
    return info.major == 8 and info.track == ReleaseTrack.INNOVATION


def _is_innovation_9(info: MySQLReleaseInfo) -> bool:
    return info.major >= 9


def validate_expected_version(
    actual: ServerInfo, expected: Optional[str], label: str
):
    """可选：配置中声明的预期版本，用于上线前核对连库是否正确。"""
    if not expected:
        return
    exp = str(expected).strip()
    if not exp:
        return
    actual_ver = actual.version
    actual_short = format_version_tuple(actual.version_tuple)
    if actual_ver.startswith(exp) or actual_short.startswith(exp):
        return
    raise RuntimeError(
        f"{label} 版本不符: 预期 {exp}，实际 {actual_ver} ({actual_short})"
    )


class MySQLCompatibilityEngine:
    """
    基于 MySQL 官方 LTS/Innovation 发布模型与 Upgrade Paths 表，
    自动识别任意 8.0+ / 9.x 源/目标组合并生成迁移策略。
    逻辑迁移 (mysqldump/mysql) 覆盖官方表中所有支持的方法。
    """

    @staticmethod
    def analyze(source: ServerInfo, target: ServerInfo) -> MigrationCompatibilityProfile:
        src = classify_release(source.version, source.version_tuple)
        tgt = classify_release(target.version, target.version_tuple)
        sv, tv = src.version_tuple, tgt.version_tuple

        if sv == tv:
            direction = MigrationDirection.SAME
        elif sv < tv:
            direction = MigrationDirection.UPGRADE
        else:
            direction = MigrationDirection.DOWNGRADE

        category, official_notes, warnings, in_place = (
            MySQLCompatibilityEngine._classify_category(src, tgt, direction)
        )
        dump_ctx = MySQLCompatibilityEngine._build_dump_context(
            src, tgt, direction, category
        )
        label = (
            f"{src.short_label} ({src.series_label}) -> "
            f"{tgt.short_label} ({tgt.series_label})"
        )

        return MigrationCompatibilityProfile(
            source=src,
            target=tgt,
            direction=direction,
            category=category,
            migration_label=label,
            official_notes=official_notes,
            warnings=warnings,
            dump_context=dump_ctx,
            logical_migration_supported=True,
            in_place_officially_supported=in_place,
        )

    @staticmethod
    def _classify_category(
        src: MySQLReleaseInfo,
        tgt: MySQLReleaseInfo,
        direction: MigrationDirection,
    ) -> Tuple[MigrationCategory, List[str], List[str], Optional[bool]]:
        notes: List[str] = []
        warns: List[str] = []

        if not src.is_known_series:
            warns.append(
                f"源库系列 {src.series_label} 未在工具元数据登记，"
                "将按通用逻辑迁移策略处理"
            )
        if not tgt.is_known_series:
            warns.append(
                f"目标库系列 {tgt.series_label} 未在工具元数据登记，"
                "将按通用逻辑迁移策略处理"
            )
        if src.is_eol:
            warns.append(
                f"源库 {src.series_label} 已 EOL，官方建议迁移至 8.4 LTS 或 9.x Innovation"
            )

        if direction == MigrationDirection.SAME:
            notes.append("同版本逻辑迁移（同 patch 或重复迁移）")
            return MigrationCategory.WITHIN_SERIES, notes, warns, True

        if direction == MigrationDirection.DOWNGRADE:
            notes.append(
                "降级迁移：MySQL 不支持跨系列原地降级，本工具使用逻辑 dump/load"
                "（官方 Downgrade 文档推荐方式）"
            )
            if _is_innovation_9(src) or _is_innovation_8(src):
                notes.append(
                    "Innovation 系列降级必须使用逻辑导出/导入（官方 Innovation Notes）"
                )
            if _is_lts_series(tgt) and _is_lts_series(src) and src.major == tgt.major:
                in_place = True
            else:
                in_place = False
                warns.append(
                    f"从 {src.series_label} 降级至 {tgt.series_label} 存在语法/特性不兼容风险，"
                    "务必先 structure_only + dry-run 验证"
                )
            return MigrationCategory.DOWNGRADE, notes, warns, in_place

        # UPGRADE
        if src.series_id == tgt.series_id:
            notes.append(
                f"同系列内升级: {src.series_id}.x -> {tgt.series_id}.x "
                "（官方支持 in-place 与 logical dump/load）"
            )
            in_place = True
            return MigrationCategory.WITHIN_SERIES, notes, warns, in_place

        if _is_lts_series(src) and _is_lts_series(tgt):
            notes.append(
                "LTS -> LTS 升级（如 8.0.x -> 8.4.x），"
                "官方支持 in-place 与 logical dump/load"
            )
            in_place = True
            return MigrationCategory.LTS_TO_LTS, notes, warns, in_place

        if _is_innovation_8(src) and _is_innovation_8(tgt):
            notes.append(
                "8.x Innovation 系列内升级（如 8.1 -> 8.3），"
                "官方支持 in-place 与 logical dump/load"
            )
            in_place = True
            return MigrationCategory.WITHIN_INNOVATION_8, notes, warns, in_place

        if _is_innovation_9(src) and _is_innovation_9(tgt):
            notes.append(
                f"9.x Innovation 系列内升级（如 {src.series_id} -> {tgt.series_id}），"
                "官方支持 in-place 与 logical dump/load"
            )
            in_place = True
            return MigrationCategory.WITHIN_INNOVATION_9, notes, warns, in_place

        if _is_innovation_8(src) and _is_lts_series(tgt) and tgt.series_id == "8.4":
            notes.append(
                "Innovation -> LTS（如 8.3 -> 8.4），"
                "官方推荐路径，支持 in-place 与 logical dump/load"
            )
            in_place = True
            return MigrationCategory.INNOVATION_TO_LTS, notes, warns, in_place

        if _is_innovation_8(src) and _is_innovation_9(tgt):
            notes.append(
                f"跨大版本 Innovation 升级 {src.series_id} -> {tgt.series_id}："
                "官方 in-place 不允许直接跳跃，须先升至 8.4 LTS 再升 9.x"
            )
            notes.append(
                "本工具 logical dump/load 可一次完成数据迁移，"
                "但请自行验证 SQL 兼容性"
            )
            in_place = False
            warns.append(
                f"官方 in-place 路径: {src.series_id} -> 8.4 -> {tgt.series_id}；"
                "逻辑迁移一步完成需人工确认"
            )
            return MigrationCategory.TO_INNOVATION, notes, warns, in_place

        if (_is_lts_series(src) or _is_innovation_8(src)) and _is_innovation_9(tgt):
            notes.append(
                f"升级至 9.x Innovation（{src.series_id} -> {tgt.series_id}），"
                "官方支持 in-place 与 logical dump/load（如 8.4 -> 9.0）"
            )
            if src.series_id == "8.0":
                notes.append(
                    "自 8.0 LTS 升级至 9.x 时，官方建议经 8.4 LTS 过渡；"
                    "逻辑迁移可直达但需验证 authentication/sql_mode 等变更"
                )
            in_place = True
            return MigrationCategory.TO_INNOVATION, notes, warns, in_place

        if _is_lts_series(src) and _is_innovation_8(tgt):
            notes.append(
                f"升级至 8.x Innovation（{src.series_id} -> {tgt.series_id}），"
                "官方支持 in-place 与 logical dump/load"
            )
            in_place = True
            return MigrationCategory.TO_INNOVATION, notes, warns, in_place

        notes.append("未精确匹配的版本组合，使用通用逻辑迁移策略")
        warns.append("请查阅官方 Upgrade Paths 并在 dry-run 后人工确认")
        in_place = None
        return MigrationCategory.UNKNOWN_FUTURE, notes, warns, in_place

    @staticmethod
    def _build_dump_context(
        src: MySQLReleaseInfo,
        tgt: MySQLReleaseInfo,
        direction: MigrationDirection,
        category: MigrationCategory,
    ) -> DumpContext:
        ctx = DumpContext(
            preflight_messages=[
                f"自动识别迁移: {src.short_label} -> {tgt.short_label} "
                f"[{direction.value}/{category.value}]",
            ],
        )

        if direction == MigrationDirection.DOWNGRADE:
            ctx.column_statistics = False
            ctx.require_fix_definer = True
            ctx.preflight_messages.append(
                "降级策略: column-statistics=0, 强烈建议 fix_definer=true, "
                "先 structure_only 验证"
            )
        elif category == MigrationCategory.LTS_TO_LTS and tgt.series_id == "8.4":
            ctx.preflight_messages.append(
                "8.0 -> 8.4: 导入后检查 authentication_policy、sql_mode 等 8.4 默认值"
            )
        elif category == MigrationCategory.TO_INNOVATION and _is_innovation_9(tgt):
            ctx.preflight_messages.append(
                f"升级至 {tgt.series_label}: 导入后验证认证插件与 replication 兼容性"
            )
        elif category == MigrationCategory.WITHIN_INNOVATION_9:
            ctx.preflight_messages.append(
                f"9.x 系列内迁移 {src.series_id} -> {tgt.series_id}，"
                "官方 quarterly Innovation 发布，逻辑迁移通常安全"
            )

        ctx.preflight_messages.append(
            "官方建议: 无论 Server 版本，优先使用最新版 MySQL Client/Tools"
        )
        return ctx


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
        return 0, 0, 0
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 ** 2:
        return f"{num_bytes / 1024:.2f} KB"
    if num_bytes < 1024 ** 3:
        return f"{num_bytes / 1024 ** 2:.2f} MB"
    return f"{num_bytes / 1024 ** 3:.2f} GB"


def cli_die(message: str, code: int = EXIT_CONFIG):
    MigrationLogger().log_error(message, to_stdout=True)
    sys.exit(code)


def parse_migration_mode(mode_str: Optional[str], label: str = "mode") -> MigrationMode:
    if not mode_str:
        return MigrationMode.STRUCTURE_AND_DATA
    key = str(mode_str).lower().strip()
    if key not in VALID_MIGRATION_MODES:
        raise ValueError(
            f"{label} 无效: '{mode_str}'，仅支持: "
            + ", ".join(sorted(VALID_MIGRATION_MODES))
        )
    return (
        MigrationMode.STRUCTURE_ONLY
        if key == "structure_only"
        else MigrationMode.STRUCTURE_AND_DATA
    )


def parse_gtid_mode(value: Optional[str], label: str = "gtid_mode") -> GtidMode:
    if not value:
        return GtidMode.AUTO
    key = str(value).lower().strip()
    mode_map = {m.value: m for m in GtidMode}
    if key not in mode_map:
        raise ValueError(
            f"{label} 无效: '{value}'，仅支持: "
            + ", ".join(sorted(mode_map.keys()))
        )
    return mode_map[key]


def parse_ssl_mode(value: Optional[str], label: str = "ssl_mode") -> SslMode:
    if not value:
        return SslMode.PREFERRED
    upper = str(value).upper().strip()
    for mode in SslMode:
        if mode.value == upper:
            return mode
    allowed = ", ".join(m.value for m in SslMode)
    raise ValueError(f"{label} 无效: '{value}'，仅支持: {allowed}")


def merge_options_dict(
    global_opts: Optional[dict],
    task_opts: Optional[dict],
) -> dict:
    """合并全局与任务级 options（任务级覆盖全局）。"""
    merged: Dict[str, Any] = {}
    if global_opts:
        merged.update(global_opts)
    if task_opts:
        merged.update(task_opts)
    return merged


def require_known_option_keys(options: dict, label: str):
    unknown = [
        k for k in options
        if not k.startswith("_") and k not in KNOWN_OPTION_KEYS
    ]
    if unknown:
        raise ValueError(f"{label} 含未知选项: {', '.join(sorted(unknown))}")


def validate_options_dict(options: dict, label: str):
    if not options:
        return
    max_workers = options.get("max_workers")
    if max_workers is not None:
        mw = int(max_workers)
        if mw < 1:
            raise ValueError(f"{label}.max_workers 必须 >= 1")
        if mw > MAX_WORKERS_RECOMMENDED:
            MigrationLogger().log_warning(
                f"{label}.max_workers={mw} 超过建议上限 {MAX_WORKERS_RECOMMENDED}，"
                "请确认目标 IO 与并行任务数",
                to_stdout=True,
            )
    per_host = options.get("max_workers_per_target_host")
    if per_host is not None and int(per_host) < 1:
        raise ValueError(f"{label}.max_workers_per_target_host 必须 >= 1")
    for key in ("dump_timeout", "import_timeout"):
        if key in options and int(options[key]) <= 0:
            raise ValueError(f"{label}.{key} 必须为正整数")
    if "row_count_threshold" in options and int(options["row_count_threshold"]) < 0:
        raise ValueError(f"{label}.row_count_threshold 不能为负")
    if "row_count_tolerance_pct" in options:
        pct = float(options["row_count_tolerance_pct"])
        if not 0 <= pct <= 100:
            raise ValueError(f"{label}.row_count_tolerance_pct 须在 0-100 之间")
    if "disk_space_margin" in options and float(options["disk_space_margin"]) < 1.0:
        raise ValueError(f"{label}.disk_space_margin 须 >= 1.0")


def resolve_env_in_string(value: str) -> str:
    """支持 ${ENV_VAR} 环境变量替换"""
    def replacer(match):
        return os.environ.get(match.group(1), "")

    return ENV_VAR_PATTERN.sub(replacer, str(value))


def _cfg_bool(cfg: dict, key: str, default: bool = False) -> bool:
    return bool(cfg.get(key, default))


def _cli_or_cfg_bool(args, cli_attr: str, cfg: dict, cfg_key: str) -> bool:
    return bool(getattr(args, cli_attr, False) or _cfg_bool(cfg, cfg_key))


def _cli_or_cfg_int(args, cli_attr: str, cfg: dict, cfg_key: str, default: int) -> int:
    cli_val = getattr(args, cli_attr, None)
    if cli_val is not None:
        return int(cli_val)
    if cfg_key in cfg:
        return int(cfg[cfg_key])
    return default


def _cfg_enabled_by_default(cfg: dict, key: str, cli_disable_attr: str, args) -> bool:
    if getattr(args, cli_disable_attr, False):
        return False
    if key in cfg and cfg[key] is False:
        return False
    return True


def build_migration_options(
    args,
    config_options: Optional[dict] = None,
    label: str = "options",
) -> MigrationOptions:
    cfg = config_options or {}
    require_known_option_keys(cfg, label)
    validate_options_dict(cfg, label)
    opts = MigrationOptions()

    opts.dry_run = _cli_or_cfg_bool(args, "dry_run", cfg, "dry_run")
    opts.keep_dump_files = _cli_or_cfg_bool(args, "keep_dump_files", cfg, "keep_dump_files")
    opts.gtid_mode = parse_gtid_mode(
        args.gtid_mode if args.gtid_mode else cfg.get("gtid_mode"),
        f"{label}.gtid_mode",
    )
    opts.replication_target = _cli_or_cfg_bool(
        args, "replication_target", cfg, "replication_target"
    )
    opts.ssl_mode = parse_ssl_mode(
        args.ssl_mode if args.ssl_mode else cfg.get("ssl_mode"),
        f"{label}.ssl_mode",
    )
    opts.add_drop_table = _cli_or_cfg_bool(args, "add_drop_table", cfg, "add_drop_table")
    opts.force_overwrite = _cli_or_cfg_bool(args, "force_overwrite", cfg, "force_overwrite")
    opts.skip_target_empty_check = _cli_or_cfg_bool(
        args, "skip_target_empty_check", cfg, "skip_target_empty_check"
    )
    opts.fix_definer = _cfg_enabled_by_default(cfg, "fix_definer", "no_fix_definer", args)
    opts.compress_dump = _cfg_enabled_by_default(cfg, "compress_dump", "no_compress", args)
    opts.per_table = _cfg_enabled_by_default(cfg, "per_table", "no_per_table", args)
    opts.max_workers = _cli_or_cfg_int(args, "max_workers", cfg, "max_workers", 2)
    opts.max_workers_per_target_host = _cli_or_cfg_int(
        args, "max_workers_per_target_host", cfg, "max_workers_per_target_host", 1
    )
    opts.dump_timeout = _cli_or_cfg_int(args, "dump_timeout", cfg, "dump_timeout", 86400)
    opts.import_timeout = _cli_or_cfg_int(
        args, "import_timeout", cfg, "import_timeout", 172800
    )
    opts.exact_row_count = _cli_or_cfg_bool(args, "exact_row_count", cfg, "exact_row_count")
    opts.row_count_threshold = int(cfg.get("row_count_threshold", opts.row_count_threshold))
    opts.row_count_tolerance_pct = float(
        cfg.get("row_count_tolerance_pct", opts.row_count_tolerance_pct)
    )
    opts.rollback_on_failure = _cli_or_cfg_bool(
        args, "rollback_on_failure", cfg, "rollback_on_failure"
    )
    opts.report_dir = str(
        args.report_dir if args.report_dir else cfg.get("report_dir", LOG_DIR)
    )
    opts.net_read_timeout = int(cfg.get("net_read_timeout", opts.net_read_timeout))
    opts.net_write_timeout = int(cfg.get("net_write_timeout", opts.net_write_timeout))
    opts.disk_space_margin = float(cfg.get("disk_space_margin", opts.disk_space_margin))
    opts.skip_version_check = _cli_or_cfg_bool(
        args, "skip_version_check", cfg, "skip_version_check"
    )
    opts.complete_insert = _cli_or_cfg_bool(args, "complete_insert", cfg, "complete_insert")
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
                except OSError:
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

    def check_client_version(
        self,
        client_version: str,
        profile: MigrationCompatibilityProfile,
    ):
        if self.options.skip_version_check:
            return
        client = parse_version(client_version)
        if client[0] < MIN_MYSQL_MAJOR:
            self.logger.log_warning(
                f"mysqldump 客户端 ({client_version.strip()}) 低于 8.0，"
                "官方建议安装最新 MySQL Client/Tools",
                to_stdout=True,
            )
        self.logger.log_progress(
            f"迁移策略: {profile.migration_label} "
            f"[{profile.direction.value}/{profile.category.value}]",
            to_stdout=True,
        )
        for note in profile.official_notes:
            self.logger.log_progress(f"  官方: {note}", to_stdout=True)
        for warn in profile.warnings:
            self.logger.log_warning(f"  {warn}", to_stdout=True)

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
        self, source: ServerInfo, _target: ServerInfo, options: MigrationOptions
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
        self._path_context = DumpContext()
        atexit.register(self._atexit_cleanup)

    def set_dump_context(self, gtid_purged: str, skip_lock_tables: bool):
        self._gtid_purged = gtid_purged
        self._skip_lock_tables = skip_lock_tables

    def set_path_context(self, ctx: DumpContext):
        self._path_context = ctx

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
        except OSError as e:
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
        if not isinstance(config.port, int) or not 1 <= config.port <= 65535:
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
            "--default-character-set=utf8mb4",
            "--max-allowed-packet=1G",
            "--hex-blob",
        ]
        ctx = self._path_context
        if ctx.column_statistics is False:
            cmd.append("--column-statistics=0")
        elif ctx.column_statistics:
            cmd.append("--column-statistics=1")
        if ctx.extra_mysqldump_args:
            cmd.extend(ctx.extra_mysqldump_args)
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
        cmd = [
            "mysql",
            f"--defaults-extra-file={cnf_path}",
            f"-h{host}",
            f"-P{config.port}",
            "--max-allowed-packet=1G",
            "--connect-timeout=120",
            "--default-character-set=utf8mb4",
            config.database,
        ]
        if self._path_context.extra_mysql_args:
            insert_at = len(cmd) - 1
            cmd[insert_at:insert_at] = self._path_context.extra_mysql_args
        return cmd

    def _run_subprocess(
        self, cmd: List[str], timeout: int, stdin_file: Optional[IO[bytes]] = None
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
            stderr.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes)
            else stderr or ""
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
                        cmd, self.options.import_timeout, stdin_file=gz
                    )
            else:
                with open(abs_dump, "rb") as fh:
                    rc, err = self._run_subprocess(
                        cmd, self.options.import_timeout, stdin_file=fh
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
    ) -> Dict[str, Any]:
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
        self.migration_progress: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _update_progress(self, task_id: str, updates: Dict[str, Any]):
        with self._lock:
            self.migration_progress.setdefault(task_id, {}).update(updates)

    def _migrate_whole_database(self, task: MigrationTask, task_id: str):
        opts = task.options or self.options
        dump_file = self.dump_manager.execute_dump(task.source, task.options.dry_run)
        self._update_progress(task_id, {"dump_file": dump_file, "mode": "whole_db"})
        if not task.options.dry_run:
            DatabaseConnector(task.target, opts).create_database_if_not_exists()
        self.dump_manager.execute_import(task.target, dump_file, task.options.dry_run)

    def _migrate_per_table(self, task: MigrationTask, task_id: str):
        opts = task.options or self.options
        source_conn = DatabaseConnector(task.source, opts)
        tables = source_conn.get_tables()
        if not tables:
            raise RuntimeError(f"源库 {task.source.database} 无基表")

        completed = self.migration_progress.get(task_id, {}).get("tables_done", [])

        if not task.options.dry_run:
            DatabaseConnector(task.target, opts).create_database_if_not_exists()

        # 1) 结构 (含 routines/events/triggers/views)
        if "__schema__" not in completed:
            schema_file = self.dump_manager.execute_dump(
                task.source, task.options.dry_run, no_data=True, label=f"{task.source.database} [schema]"
            )
            if not task.options.dry_run:
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
                task.source, task.options.dry_run,
                tables=[tbl.name], no_create_info=True,
                include_schema_objects=False,
                label=f"{task.source.database}.{tbl.name}",
            )
            if not task.options.dry_run:
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

    def execute_migration(self, task: MigrationTask) -> Dict[str, Any]:
        task_id = f"{task.source.database}->{task.target.database}"
        opts = task.options or self.options
        audit: Dict[str, Any] = {
            "task_id": task_id,
            "start_time": datetime.now().isoformat(),
            "status": "running",
            "source": {"host": task.source.host, "database": task.source.database},
            "target": {"host": task.target.host, "database": task.target.database},
            "options": {k: v.value if isinstance(v, Enum) else v for k, v in asdict(opts).items()},
        }

        target_key = (task.target.host, task.target.port)
        prev_dump_opts = self.dump_manager.options
        self.dump_manager.options = opts
        with self.host_limiter.acquire(*target_key):
            try:
                src_conn = DatabaseConnector(task.source, opts)
                tgt_conn = DatabaseConnector(task.target, opts)

                src_info = src_conn.get_server_info()
                tgt_info = tgt_conn.get_server_info()
                validate_expected_version(
                    src_info, task.expected_source_version, "源库"
                )
                validate_expected_version(
                    tgt_info, task.expected_target_version, "目标库"
                )

                profile = MySQLCompatibilityEngine.analyze(src_info, tgt_info)
                dump_ctx = profile.dump_context
                audit["source"]["version"] = src_info.version
                audit["target"]["version"] = tgt_info.version
                audit["source"]["series"] = profile.source.series_label
                audit["target"]["series"] = profile.target.series_label
                audit["source"]["gtid"] = src_info.gtid_mode
                audit["target"]["gtid"] = tgt_info.gtid_mode
                audit["compatibility"] = profile.to_audit_dict()
                audit["path_preflight"] = dump_ctx.preflight_messages

                self.logger.log_step(
                    f"开始迁移: {task_id} [{profile.migration_label}]",
                    to_stdout=True,
                )
                self._update_progress(
                    task_id, {"status": "running", "start_time": datetime.now().isoformat()}
                )

                for msg in dump_ctx.preflight_messages:
                    self.logger.log_progress(msg, to_stdout=True)
                if dump_ctx.require_fix_definer and not opts.fix_definer:
                    self.logger.log_warning(
                        f"迁移 {profile.migration_label} 强烈建议 fix_definer=true，"
                        "当前已关闭，导入可能因 DEFINER 失败",
                        to_stdout=True,
                    )

                client_ver = subprocess.check_output(
                    ["mysqldump", "--version"], text=True, stderr=subprocess.STDOUT
                ).strip()
                audit["client_version"] = client_ver
                self.preflight.check_client_version(client_ver, profile)

                tables = src_conn.get_tables()
                self.preflight.check_storage_engines(tables)
                est = src_conn.estimate_database_bytes()
                audit["estimated_bytes"] = est
                if not task.options.dry_run:
                    self.preflight.check_disk_space(est, self.dump_manager.temp_dir)

                if not opts.skip_target_empty_check:
                    self.preflight.check_target_empty(tgt_conn, opts.force_overwrite)

                gtid = self.preflight.resolve_gtid_purged(src_info, tgt_info, opts)
                non_innodb = any(t.engine not in ("INNODB", "UNKNOWN") for t in tables)
                self.dump_manager.set_dump_context(
                    gtid_purged=gtid, skip_lock_tables=non_innodb
                )
                self.dump_manager.set_path_context(dump_ctx)
                audit["gtid_purged"] = gtid

                use_per_table = opts.per_table and task.source.mode == MigrationMode.STRUCTURE_AND_DATA
                if use_per_table:
                    self._migrate_per_table(task, task_id)
                    audit["migration_strategy"] = "per_table"
                else:
                    self._migrate_whole_database(task, task_id)
                    audit["migration_strategy"] = "whole_db"

                if not task.options.dry_run:
                    audit["validation"] = self.validator.validate(
                        DatabaseConnector(task.source, opts),
                        DatabaseConnector(task.target, opts),
                        task.source.mode,
                    )

                audit["status"] = "completed"
                audit["end_time"] = datetime.now().isoformat()
                self._update_progress(
                    task_id,
                    {"status": "completed", "end_time": datetime.now().isoformat()},
                )
                self.logger.log_step(f"迁移完成: {task_id}", to_stdout=True)
                return audit

            except MIGRATION_FAILURE_EXCEPTIONS as exc:
                audit["status"] = "failed"
                audit["error"] = str(exc)
                audit["end_time"] = datetime.now().isoformat()
                self._update_progress(task_id, {
                    "status": "failed",
                    "error": str(exc),
                    "end_time": datetime.now().isoformat(),
                })
                self.logger.log_error(f"迁移失败 {task_id}: {exc}", to_stdout=True)

                if opts.rollback_on_failure and not task.options.dry_run:
                    try:
                        self.logger.log_warning(
                            f"回滚: 删除目标库 {task.target.database}", to_stdout=True
                        )
                        DatabaseConnector(task.target, opts).drop_database()
                        audit["rollback"] = "dropped_target_database"
                    except (OSError, pymysql.err.MySQLError) as rb_exc:
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
再导入）。支持 MySQL 8.0+ 全系列（8.0 / 8.1~8.4 / 9.0~9.x 及后续版本），
连接源/目标后自动识别精确版本并生成兼容策略。
适用于 GTID 复制、50GB+ 大库、亿级行表等生产场景。

核心能力:
  * 自动版本识别: 读取 VERSION()，按官方 LTS/Innovation 模型分类
  * 兼容策略引擎: 对照官方 Upgrade Paths 表生成 dump 参数与预检提示
  * 分表迁移 (per_table): 先迁结构，再逐表迁数据
  * GTID 策略: auto / off / on / commented
  * 迁移前预检: 版本策略、磁盘、目标库是否为空、存储引擎
  * 迁移后校验: 表数量一致 + 行数对比（估算或精确 COUNT）
  * 安全: 临时 cnf 传密码(0600)、DEFINER 自动修复、目标非空保护
  * 审计: JSON 报告含完整 compatibility 分析

MySQL 官方版本模型 (参见 mysql-releases.html):
  * LTS (长期支持): 8.0.x Bugfix、8.4.x LTS — 系列内可 in-place 升降级
  * Innovation (创新): 8.1~8.3 (已 EOL)、9.0~9.6+ — 季度发布
  * 逻辑迁移 (mysqldump/mysql) 为官方所有 Upgrade Paths 均支持的方法

依赖:
  * Python 3.6+、pymysql
  * 系统已安装 mysqldump、mysql 客户端（官方建议始终用最新 Client/Tools）

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

  第 3 步  预演（连库做预检，打印 dump/import 命令，不实际导出导入）
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

  版本由程序自动识别，无需手动指定 migration_path。
  可选 expected_source_version / expected_target_version 用于核对连库是否正确。

【迁移模式】
  --dry-run
      预演模式: 连接源/目标做预检（版本、GTID、目标是否为空等），
      dump/import 只打印命令不实际运行。dry-run 不校验临时目录磁盘空间。
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
        "expected_source_version": "8.0.41",
        "expected_target_version": "8.4.4",
        "source": { "host", "port", "user", "password", "database", "mode" },
        "target": { 同上 },
        "options": { ... 可选，覆盖本任务 ... }
      }
    ]
  }

  expected_source_version / expected_target_version (可选):
    用于上线前核对 VERSION() 是否与预期一致（前缀匹配即可，如 "8.0" 或 "8.0.41"）。
    不填则完全依赖自动识别。

  程序连接源/目标后自动:
    1. 解析精确版本 (如 8.0.41, 8.4.4, 9.6.0)
    2. 分类为 LTS/Innovation 系列
    3. 对照官方 Upgrade Paths 生成迁移策略与 dump 参数

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
  未知 options 键名、非法 mode/gtid_mode/ssl_mode 会在启动时报错退出。
  ${ENV} 未设置或密码为空同样会拒绝启动。


五、典型场景示例
----------------

【场景 A】MySQL 8.0.41 -> 8.4.4 普通迁库
  自动识别为 lts_to_lts 升级；gtid_mode=auto, per_table=true

【场景 B】8.0 主库 -> 8.4 GTID 从库
  replication_target=true, gtid_mode=auto

【场景 C】8.4.2 -> 9.6.0 升级至 Innovation
  自动识别为 to_innovation；建议 --dry-run 后验证认证插件

【场景 D】9.4.0 -> 8.4.4 降级（高风险）
  自动识别为 downgrade；务必 structure_only + dry-run 先验证

【场景 E】9.0.1 -> 9.6.0 同 Innovation 系列内升级
  自动识别为 within_innovation_9

【场景 F】50GB+ 大库，含亿级单表
  options.per_table = true          # 必须
  options.dump_timeout = 86400
  options.import_timeout = 172800
  options.keep_dump_files = true    # 首次建议保留便于排错
  校验: 默认估算；核心表可二次跑 --exact-row-count

【场景 G】仅迁结构（如预建库）
  source.mode = "structure_only"

【场景 H】多库并行迁移到同一台目标机
  migrations: [ 任务1, 任务2, ... ]
  options.max_workers = 2
  options.max_workers_per_target_host = 1   # 避免打满目标磁盘 IO

【场景 I】重复迁移到同一目标（覆盖）
  options.force_overwrite = true
  options.add_drop_table = true           # 可选，确保表定义更新


六、迁移执行过程（便于排障）
----------------------------
  1. 预检: 连接源/目标、自动版本识别、兼容策略、GTID、磁盘、目标是否为空
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
    0    全部任务成功 (EXIT_SUCCESS)
    1    存在失败任务或运行时错误 (EXIT_FAILURE)
    2    配置文件错误 (EXIT_CONFIG)
    130  用户 Ctrl+C / SIGINT 中断 (EXIT_INTERRUPT)


九、常见问题
------------
  Q: 8.3 能否直接 in-place 升到 9.0?
  A: 官方不允许，须 8.3->8.4->9.0。本工具 logical dump 可一步迁移但需人工验证兼容性。

  Q: expected_source_version 报错
  A: 检查是否连错库；该字段仅用于核对，前缀匹配 VERSION() 即可。

  Q: 导入报 DEFINER 不存在
  A: 默认 fix_definer=true；降级路径(如 9.x->8.0)强烈建议保持开启。

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
            "MySQL 生产级逻辑迁移工具 (mysqldump/mysql, MySQL 8.0+)\n"
            "自动识别 8.0/8.1~8.4/9.x 全系列版本并生成官方兼容策略。\n"
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
        help=(
            "预演: 连库预检并打印 dump/import 命令，不实际导出导入。"
            "JSON 可在 options 设 dry_run=true。上线前必跑"
        ),
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
        help=f"并行迁移的任务数(多库场景)。默认 2，实际上限 min(N, CPU核数, {MAX_WORKERS_RECOMMENDED})",
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
def parse_database_config(db_str: str, mode: MigrationMode, label: str = "连接") -> DatabaseConfig:
    parts = db_str.split(":")
    if len(parts) < 5:
        raise ValueError(
            f"{label} 格式错误，应为 host:port:database:user:password"
            "（password 中可含冒号，会取第5段及之后全部内容）"
        )
    try:
        port = int(parts[1])
        if not 1 <= port <= 65535:
            raise ValueError(f"{label} 端口超出范围: {port}")
    except ValueError as e:
        raise ValueError(f"{label} 端口必须是整数: {parts[1]}") from e

    host = parts[0].strip()
    database = parts[2].strip()
    user = parts[3].strip()
    password = ":".join(parts[4:]).replace("\\:", ":")
    if not host or not user or not database:
        raise ValueError(f"{label} host/user/database 不能为空")
    if not password:
        raise ValueError(f"{label} password 不能为空")
    DatabaseConnector.validate_identifier(database)
    return DatabaseConfig(
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
        mode=mode,
    )


def _check_credential_field(raw_value: str, resolved: str, field_label: str):
    if not str(raw_value).strip():
        raise ValueError(f"{field_label} 不能为空")
    if ENV_VAR_PATTERN.search(raw_value) and not resolved:
        missing = ENV_VAR_PATTERN.findall(raw_value)
        raise ValueError(
            f"{field_label} 引用的环境变量未设置或为空: {', '.join(missing)}"
        )
    if field_label.endswith("password") and not resolved:
        raise ValueError(f"{field_label} 不能为空")


def _parse_db_dict(data: dict, label: str) -> DatabaseConfig:
    if not isinstance(data, dict):
        raise ValueError(f"{label} 必须是对象")
    required = ["host", "port", "user", "password", "database"]
    for field_name in required:
        if field_name not in data:
            raise ValueError(f"{label} 缺少字段: {field_name}")
    port = int(data["port"])
    if not 1 <= port <= 65535:
        raise ValueError(f"{label} 端口无效: {port}")

    raw_user = str(data["user"])
    raw_password = str(data["password"])
    user = str(resolve_env_in_string(raw_user)).strip()
    password = str(resolve_env_in_string(raw_password))
    database = str(data["database"]).strip()
    host = str(data["host"]).strip()

    _check_credential_field(raw_user, user, f"{label}.user")
    _check_credential_field(raw_password, password, f"{label}.password")
    if not database:
        raise ValueError(f"{label}.database 不能为空")
    DatabaseConnector.validate_identifier(database)

    return DatabaseConfig(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        mode=parse_migration_mode(data.get("mode"), f"{label}.mode"),
    )


def read_config_file(config_path: str) -> dict:
    """读取并解析 JSON 配置文件（单次 IO）。"""
    PreflightChecker(MigrationOptions()).check_config_file_permissions(config_path)

    if not os.path.isfile(config_path):
        cli_die(f"配置文件不存在: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        try:
            data = json.load(fh)
        except json.JSONDecodeError as e:
            cli_die(f"JSON 格式错误: {e}")

    if not isinstance(data, dict):
        cli_die("配置文件根节点必须是 JSON 对象")
    return data


def validate_migration_config(data: dict):
    if "migrations" not in data:
        cli_die("配置文件缺少 migrations 字段")
    migrations = data["migrations"]
    if not isinstance(migrations, list):
        cli_die("migrations 必须是数组")
    if not migrations:
        cli_die("migrations 不能为空")

    global_opts = data.get("options")
    if global_opts is not None and not isinstance(global_opts, dict):
        cli_die("options 必须是对象")


def _optional_task_str(item: dict, key: str) -> Optional[str]:
    if key not in item:
        return None
    val = str(item[key]).strip()
    return val if val else None


def load_migration_tasks(config_data: dict, args) -> List[MigrationTask]:
    logger = MigrationLogger()
    global_cfg = config_data.get("options") or {}
    tasks: List[MigrationTask] = []
    seen_targets: set = set()

    for idx, item in enumerate(config_data["migrations"], 1):
        if not isinstance(item, dict):
            cli_die(f"任务 {idx} 必须是对象")
        if "source" not in item or "target" not in item:
            meta_keys = TASK_META_KEYS | TASK_OPTIONAL_KEYS
            if set(item.keys()) <= meta_keys:
                continue
            cli_die(f"任务 {idx} 缺少 source 或 target")
        try:
            unknown_keys = [
                k for k in item
                if not k.startswith("_")
                and k not in ("source", "target", "options")
                and k not in TASK_OPTIONAL_KEYS
            ]
            if unknown_keys:
                raise ValueError(f"未知字段: {', '.join(sorted(unknown_keys))}")

            task_cfg = merge_options_dict(
                global_cfg,
                item["options"] if isinstance(item.get("options"), dict) else None,
            )
            task_opts = build_migration_options(args, task_cfg, f"任务{idx} options")
            source = _parse_db_dict(item["source"], f"任务{idx}.source")
            target = _parse_db_dict(item["target"], f"任务{idx}.target")
            target_key = (target.host, target.port, target.database)
            if target_key in seen_targets:
                logger.log_warning(
                    f"任务 {idx} 与先前任务写入同一目标库 "
                    f"{target.host}:{target.port}/{target.database}，请确认无冲突",
                    to_stdout=True,
                )
            seen_targets.add(target_key)
            tasks.append(MigrationTask(
                source=source,
                target=target,
                options=task_opts,
                expected_source_version=_optional_task_str(
                    item, "expected_source_version"
                ),
                expected_target_version=_optional_task_str(
                    item, "expected_target_version"
                ),
            ))
        except ValueError as e:
            cli_die(f"任务 {idx} 配置错误: {e}")

    if not tasks:
        cli_die("无有效迁移任务（migrations 不能为空或仅含 _comment 占位）")

    logger.log_progress(f"已加载 {len(tasks)} 个迁移任务", to_stdout=True)
    return tasks


def check_dependencies():
    missing = [t for t in ("mysqldump", "mysql") if not shutil.which(t)]
    if missing:
        logger = MigrationLogger()
        logger.log_error(f"缺少命令: {', '.join(missing)}", to_stdout=True)
        sys.exit(EXIT_FAILURE)
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
        sys.exit(EXIT_INTERRUPT if sig == signal.SIGINT else EXIT_FAILURE)

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


def main():
    args = parse_arguments()
    if args.guide:
        print_user_guide()
        sys.exit(EXIT_SUCCESS)

    logger = MigrationLogger()
    check_dependencies()

    config_data = None
    if args.config:
        config_data = read_config_file(args.config)
        validate_migration_config(config_data)

    global_cfg = (
        config_data.get("options")
        if config_data and isinstance(config_data.get("options"), dict)
        else None
    )
    options = build_migration_options(args, global_cfg)
    process_registry = ProcessRegistry()
    host_limiter = HostConcurrencyLimiter(options.max_workers_per_target_host)
    migration_manager = MigrationManager(options, process_registry, host_limiter)
    setup_signal_handlers(process_registry, migration_manager.dump_manager)

    tasks: List[MigrationTask] = []

    if args.config:
        tasks = load_migration_tasks(config_data, args)
    elif args.source and args.target:
        mode = (
            MigrationMode.STRUCTURE_ONLY
            if args.structure_only
            else MigrationMode.STRUCTURE_AND_DATA
        )
        tasks.append(MigrationTask(
            source=parse_database_config(args.source, mode, "源库"),
            target=parse_database_config(args.target, mode, "目标库"),
            options=options,
        ))
    else:
        cli_die(
            "请指定迁移任务:\n"
            "  推荐: python MigrationCli.py -c migration_config.json\n"
            "  单库: python MigrationCli.py -s host:port:db:user:pass -t host:port:db:user:pass\n"
            "  手册: python MigrationCli.py --guide\n"
            "  参数: python MigrationCli.py --help"
        )

    opts = migration_manager.options
    max_workers = min(opts.max_workers, os.cpu_count() or 4, MAX_WORKERS_RECOMMENDED)
    any_dry_run = any(t.options.dry_run for t in tasks)
    report_writer = AuditReportWriter(opts.report_dir)

    logger.log_step(
        f"MigrationCli v{TOOL_VERSION} | {len(tasks)} 个任务 | "
        f"dry_run={any_dry_run} | per_table={opts.per_table} | "
        f"gtid={opts.gtid_mode.value} | 版本策略=自动识别",
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
                except MIGRATION_FAILURE_EXCEPTIONS as e:
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
                "tool_version": TOOL_VERSION,
                "config_file": os.path.abspath(args.config) if args.config else None,
                "dry_run": any_dry_run,
                "total": len(tasks),
                "success": success,
                "failed": failed,
                "elapsed_seconds": round(elapsed, 2),
                "timestamp": datetime.now().isoformat(),
                "exit_code": EXIT_FAILURE if failed else EXIT_SUCCESS,
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
            sys.exit(EXIT_FAILURE)

        logger.log_step(
            f"全部成功 {success}/{len(tasks)}，耗时 {elapsed:.0f}s",
            to_stdout=True,
        )
        if opts.keep_dump_files:
            logger.log_progress(
                f"dump 保留于: {migration_manager.dump_manager.temp_dir}",
                to_stdout=True,
            )
        elif not any_dry_run:
            migration_manager.dump_manager.cleanup()

    except KeyboardInterrupt:
        process_registry.terminate_all()
        migration_manager.dump_manager.cleanup()
        sys.exit(EXIT_INTERRUPT)


if __name__ == "__main__":
    main()

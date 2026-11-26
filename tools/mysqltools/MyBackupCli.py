#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
MySQL Enterprise Backup Script (Production Ready)
==================================================

Author: (Brinnatt)
Description:
  Full / Incremental / Binlog backup for MySQL 5.7, 8.0, 8.4
  Binlog continuity guaranteed
  Restore with mandatory dry-run audit gate
  
Compatibility:
  - MySQL 5.7 with the matching Percona XtraBackup 2.4 series
  - MySQL 8.0 with the matching Percona XtraBackup 8.0 series
  - MySQL 8.4 with the matching Percona XtraBackup 8.4 series
  - MySQL 9.x is intentionally unsupported for physical backup
  - Python 3.6+

Best Practices Implemented:
  - Percona XtraBackup official recommendations
  - Compressed backup decompression before prepare
  - Capability-aware --lock-ddl parameter from the actual XtraBackup binary
  - --use-memory for optimized performance
  - Proper incremental backup prepare sequence
  - Enhanced error handling and validation

References:
  - https://docs.percona.com/percona-xtrabackup/8.4/
  - https://docs.percona.com/percona-xtrabackup/8.4/prepare-compressed-backup.html
  - https://docs.percona.com/percona-xtrabackup/8.4/prepare-incremental-backup.html
  - https://docs.percona.com/percona-xtrabackup/8.4/reduction-in-locks.html
  - https://www.percona.com/blog/percona-xtrabackup-8-enables-lock-ddl-by-default/
"""

import os
import sys
import json
import shutil
import subprocess
import argparse
import contextlib
import tempfile
import time
import fcntl
import re
import shlex
import configparser
import logging
import codecs
import selectors
from datetime import datetime, timedelta
from pathlib import Path
from logging.handlers import RotatingFileHandler
from typing import Optional, List

# =========================================================
# 日志配置
# =========================================================
LOG_DIR = "/var/log"
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, "mysqlbackup.log")
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"
XTRABACKUP_HEARTBEAT_SECONDS = 30


def _env_for_system_subprocess():
    """Restore the host linker path for children of a frozen Linux tool."""
    if not (sys.platform.startswith("linux") and getattr(sys, "frozen", False)):
        return None
    env = os.environ.copy()
    original = env.get("LD_LIBRARY_PATH_ORIG")
    if original is None:
        env.pop("LD_LIBRARY_PATH", None)
    else:
        env["LD_LIBRARY_PATH"] = original
    return env


def setup_logger(
    name: str = "mysqlbackup",
    log_file: Optional[str] = None,
    level: int = DEFAULT_LOG_LEVEL,
    fmt: str = DEFAULT_FORMAT,
    datefmt: str = DEFAULT_DATEFMT,
    handlers: Optional[List[logging.Handler]] = None
) -> logging.Logger:
    """
    设置日志记录器
    
    Args:
        name: logger名称
        log_file: 日志文件路径，默认使用DEFAULT_LOG_FILE
        level: 日志级别
        fmt: 日志格式
        datefmt: 日期格式
        handlers: 自定义handlers，如果提供则使用这些handlers
        
    Returns:
        logging.Logger: 配置好的logger
    """
    if not os.path.exists(LOG_DIR):
        os.makedirs(LOG_DIR, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    if logger.hasHandlers():
        return logger  # 避免重复添加 handler

    # 如果没有指定 handlers，则默认使用文件 handler 或标准输出
    if handlers is None:
        log_file = log_file or os.getenv("MYSQLBACKUP_LOG_FILE", DEFAULT_LOG_FILE)
        formatter = logging.Formatter(fmt, datefmt)
        try:
            file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
            file_handler.setFormatter(formatter)
            file_handler.setLevel(level)
            # 默认输入到文件，传 extra={'skip_file': True} 不输入到文件
            file_handler.addFilter(lambda record: not getattr(record, 'skip_file', False))
            logger.addHandler(file_handler)
        except OSError:
            # CLI must remain usable on non-root systems where /var/log is unwritable.
            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler.setFormatter(formatter)
            stderr_handler.setLevel(level)
            logger.addHandler(stderr_handler)

        # 添加一个默认的 stdout handler 但不启用
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(level)
        # 传 extra={'to_stdout': True} 才可以标准输出
        stdout_handler.addFilter(lambda record: getattr(record, 'to_stdout', False))
        logger.addHandler(stdout_handler)
    else:
        for handler in handlers:
            handler.setLevel(level)
            if not handler.formatter:
                handler.setFormatter(logging.Formatter(fmt, datefmt))
            logger.addHandler(handler)

    return logger


NATIVE_TARGETS = frozenset(("mysql", "mysqlbinlog", "xtrabackup-backup", "xtrabackup-prepare", "xtrabackup-decompress", "xtrabackup-copy-back"))

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
log-bin log-bin-index
incremental incremental-basedir incremental-dir incremental-lsn incremental-history-name
incremental-history-uuid apply-log-only stream compress remove-original lock-ddl no-lock
ignore-error no-backup-locks no-server-version-check start-position stop-position start-datetime
stop-datetime read-from-remote-server raw to-last-log stop-never
slave-info safe-slave-backup galera-info
""".split())


def native_option_help():
    """Customer reference, available without credentials or database access."""
    protected = sorted(NATIVE_PROTECTED)
    listing = "\n".join("  " + "  ".join("--" + name for name in protected[index:index + 4])
                        for index in range(0, len(protected), 4))
    return """原生参数帮助：MyBackupCli

参数交给哪个命令（等号左侧必须完整填写）：
  mysql                   MySQL 查询与 binlog 恢复写入客户端；不配置 XtraBackup 的连接
  mysqlbinlog             恢复时的 binlog 解析命令；不作用于 binlog 文件复制
  xtrabackup-backup       full/incr 的备份阶段
  xtrabackup-prepare      restore 的全量与增量 prepare 阶段
  xtrabackup-decompress   restore 的解压阶段
  xtrabackup-copy-back    restore 的数据复制回目录阶段

示例（先使用已验证且权限为 0600 的配置文件）：
  MyBackupCli full -c /etc/mysql/backup.conf --native-option xtrabackup-backup=--throttle=100
  MyBackupCli full -c /etc/mysql/backup.conf --native-option mysql=--connect-timeout=15 --native-option xtrabackup-backup=--throttle=100
  以上 100 是语法示例，不是生产限速建议；只影响备份阶段，不影响 prepare。

长期保存：编辑现有 JSON 的 native_options，值为原生参数字符串数组。例如：
  "native_options": {"mysql": ["--connect-timeout=15"], "xtrabackup-prepare": ["--use-memory=64M"]}
  这是配置片段，须合并进现有 JSON；其它阶段可省略或填 []。
  配置参数先传入，CLI 参数追加在后；同一选项的重复解释由原生命令决定，避免重复设置。
  64M 是小测试数据示例；生产 prepare 内存优先使用既有 xtrabackup_use_memory 配置。

受保护选项的正常入口：
  --password/--user/--socket：使用配置 mysql_password/mysql_user/mysql_socket。
  --datadir/--target-dir：使用 mysql_datadir/backup_base，工具维护备份链目录。
  --lock-ddl：使用 xtrabackup_lock_ddl=AUTO/ON/OFF/REDUCED；AUTO 检查实际能力。
  --start-datetime/--stop-datetime：使用 --binlog-start-time/--binlog-stop-time。
  --backup/--prepare/--copy-back：使用 full/incr/restore；恢复须先审计并明确确认覆盖。
  --force、关闭保护或更改输出格式没有绕过入口；应移除这些原生参数。

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


class Config:
    """配置类 - 管理所有配置项"""
    
    def __init__(self, config_file=None, native_options=None, offline=False):
        # 初始化logger（在配置加载前使用）
        self.logger = setup_logger("mysqlbackup.config")
        """
        加载配置文件
        配置文件格式（JSON）:
        {
            "backup_base": "/backup/mysql",
            "mysql_user": "backup",
            "mysql_password": "password",
            "mysql_socket": "/var/lib/mysql/mysql.sock",
            "mysql_service": "mysqld",
            "mysql_datadir": "/var/lib/mysql",
            "mysql_binlog_prefix": "mysql-bin",
            "xtrabackup_parallel": 4,
            "xtrabackup_compress_threads": 4,
            "xtrabackup_use_memory": "1G",
            "xtrabackup_lock_ddl": "AUTO"  # AUTO, REDUCED, ON, OFF
        }
        """
        # 默认配置
        defaults = {
            "backup_base": "/backup/mysql",
            "mysql_user": "backup",
            "mysql_password": os.getenv("MYSQL_BACKUP_PASSWORD", ""),
            "mysql_socket": "/var/lib/mysql/mysql.sock",
            "mysql_service": "mysqld",
            "mysql_datadir": "/var/lib/mysql",
            "mysql_binlog_prefix": "mysql-bin",
            "xtrabackup_parallel": 4,
            "xtrabackup_compress_threads": 4,
            "xtrabackup_use_memory": "1G",
            "xtrabackup_lock_ddl": "AUTO"
        }
        
        # 从配置文件加载
        if config_file and os.path.exists(config_file):
            try:
                mode = os.stat(config_file).st_mode
                if mode & 0o077:
                    self._fail("Config file permissions are too broad; use mode 0600")
                with open(config_file, 'r') as f:
                    config = json.load(f)
                defaults.update(config)
                self.logger.info("Loaded configuration from: {0}".format(config_file))
            except Exception as e:
                self.logger.error("Failed to load config file {0}: {1}".format(config_file, e))
                sys.exit(1)
        elif config_file:
            self.logger.error("Config file not found: {0}".format(config_file))
            sys.exit(1)
        
        # 应用配置
        self.backup_base = Path(defaults["backup_base"])
        self.mysql_user = defaults["mysql_user"]
        self.mysql_password = defaults["mysql_password"] or os.getenv("MYSQL_BACKUP_PASSWORD", "")
        self.mysql_socket = defaults["mysql_socket"]
        self.mysql_service = defaults["mysql_service"]
        self.mysql_container = defaults.get("mysql_container", "")
        if self.mysql_container and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", self.mysql_container):
            self._fail("mysql_container must be a Docker container name or ID")
        if not self.mysql_container and (not isinstance(self.mysql_service, str) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*', self.mysql_service)):
            self._fail('mysql_service must name the real systemd unit when mysql_container is empty')
        self.mysql_topology = defaults.get("mysql_topology", "standalone")
        self.offline = offline
        self.mysql_defaults_file = defaults.get('mysql_defaults_file', '')
        if self.mysql_topology not in ('standalone', 'primary-replica', 'dual-primary'):
            self._fail("mysql_topology must be standalone, primary-replica or dual-primary; empty is invalid")
        self.mysql_datadir = Path(defaults["mysql_datadir"])
        self.mysql_binlog_prefix = defaults["mysql_binlog_prefix"]
        self.mysql_binlog_dir = Path(defaults.get("mysql_binlog_dir") or self.mysql_datadir)
        self.mysql_binlog_index = Path(defaults.get("mysql_binlog_index") or
                                      self.mysql_binlog_dir / (self.mysql_binlog_prefix + ".index"))
        for path in (self.backup_base, self.mysql_datadir, self.mysql_binlog_dir, self.mysql_binlog_index):
            if not path.is_absolute():
                self._fail("MySQL and backup paths must be absolute in the tool's execution namespace")
        if (self.mysql_datadir.resolve() == Path('/') or
                self.mysql_binlog_index.parent.resolve() != self.mysql_binlog_dir.resolve() or
                self.mysql_binlog_index.suffix != '.index' or
                Path(self.mysql_binlog_prefix).name != self.mysql_binlog_prefix or
                self.mysql_binlog_prefix in ('', '.', '..')):
            self._fail("Unsafe datadir, binlog prefix or index location")
        for source in (self.mysql_datadir.resolve(), self.mysql_binlog_dir.resolve()):
            target = self.backup_base.resolve()
            if target == source or source in target.parents or target in source.parents:
                self._fail("backup_base must not overlap MySQL data or binlog paths")
        self.xtrabackup_parallel = int(defaults.get("xtrabackup_parallel", 4))
        self.xtrabackup_compress_threads = int(defaults.get("xtrabackup_compress_threads", 4))
        self.xtrabackup_use_memory = defaults.get("xtrabackup_use_memory", "1G")
        if self.xtrabackup_parallel < 1 or self.xtrabackup_compress_threads < 1:
            self._fail("xtrabackup thread counts must be positive")
        
        # 验证密码
        if not self.mysql_password:
            self._fail("MySQL password is required. Set it in config file or MYSQL_BACKUP_PASSWORD environment variable")
        
        # 设置路径（log_file已不再使用，日志统一使用/var/log/mysqlbackup.log）
        self.lock_file = self.backup_base / "lock/mysqlbackup.lock"
        self.audit_base = self.backup_base / "restore_audit"
        
        self.native_options = defaults.get("native_options", {})
        if not isinstance(self.native_options, dict):
            self._fail("native_options must map targets to argv lists")
        for target, tokens in self.native_options.items():
            if not isinstance(tokens, list):
                self._fail("native_options values must be argv lists")
            native_arguments(target, *tokens)
        for target, tokens in (native_options or {}).items():
            self.native_options.setdefault(target, []).extend(native_arguments(target, *tokens))

        # 检测MySQL版本
        self.mysql_version = self._offline_version() if offline else self._detect_mysql_version()
        if self.mysql_version[0] >= 9:
            self._fail(
                "MySQL {0}.{1} is not supported by the declared XtraBackup "
                "compatibility set; use MigrationCli for logical migration".format(*self.mysql_version)
            )
        
        # 设置lock-ddl参数
        lock_ddl_config = defaults.get("xtrabackup_lock_ddl", "AUTO")
        self.xtrabackup_lock_ddl = lock_ddl_config
        if lock_ddl_config not in {"AUTO", "REDUCED", "ON", "OFF"}:
            self._fail("xtrabackup_lock_ddl must be AUTO, REDUCED, ON or OFF")

        self.logger.info("Configuration loaded: MySQL {0}.{1}, lock-ddl={2}".format(
            self.mysql_version[0], self.mysql_version[1], self.xtrabackup_lock_ddl))
    
    def _log(self, msg, level=logging.INFO):
        """内部日志方法（在配置加载完成前使用）"""
        self.logger.log(level, msg)
    
    def _fail(self, msg):
        """内部失败方法（在配置加载完成前使用）"""
        self.logger.error(msg)
        sys.exit(1)
    
    def _detect_mysql_version(self):
        """
        检测MySQL版本
        Returns: (major, minor) 例如 (8, 0) 或 (5, 7)
        Reference: https://docs.percona.com/percona-xtrabackup/8.4/
        """
        try:
            result = self._mysql_cmd("SELECT VERSION();")
            version_match = re.search(r'(\d+)\.(\d+)', result)
            if version_match:
                major = int(version_match.group(1))
                minor = int(version_match.group(2))
                self.logger.info("Detected MySQL version: {0}.{1}".format(major, minor))
                return major, minor
            else:
                self._fail("Could not parse MySQL version; refusing to assume compatibility")
        except Exception as e:
            self._fail("Could not detect MySQL version; refusing to assume compatibility")

    def _offline_version(self):
        """Admit one explicit native unit; ask its binary for effective options.

        No guessed server version and no empty temporary database. Wrapper
        units and containers need their own startup proof and are refused here.
        """
        defaults = Path(self.mysql_defaults_file)
        if (self.mysql_container or not self.mysql_defaults_file or not defaults.is_absolute()
                or defaults.is_symlink() or not defaults.is_file()):
            self._fail('Offline restore requires native systemd and an explicit ordinary mysql_defaults_file')

        def output(argv):
            result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    text=True, env=_env_for_system_subprocess())
            if result.returncode:
                self._fail('Cannot verify offline MySQL startup configuration')
            return result.stdout

        execution = output(['systemctl', 'show', '--property=ExecStart', '--value', self.mysql_service])
        match = re.fullmatch(r'\s*\{ path=([^;]+?) ; argv\[\]=([^;]+?) ; [^{}]*\}\s*', execution)
        if not match:
            self._fail('Offline restore requires a direct mysqld ExecStart with --defaults-file')
        binary = match.group(1)
        if (not Path(binary).is_absolute() or Path(binary).name != 'mysqld' or
                shlex.split(match.group(2)) != [binary, '--defaults-file=' + str(defaults)]):
            self._fail('Offline restore refuses wrapper units or extra ExecStart arguments')
        status = subprocess.run(['systemctl', 'is-active', '--quiet', self.mysql_service],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=_env_for_system_subprocess())
        if status.returncode == 0:
            self._fail('Offline restore requires the target unit already stopped')
        version = output([binary, '--version'])
        parsed = re.search(r'\bVer (\d+)\.(\d+)\.\d+', version)
        if not parsed:
            self._fail('Cannot establish the installed offline MySQL version')
        self.mysql_version_text = parsed.group(0).split()[1]
        help_text = output([binary, '--defaults-file=' + str(defaults), '--verbose', '--help'])
        values = dict(re.findall(r'^([a-z][a-z0-9-]*)\s+([^\r\n]+?)\s*$', help_text, re.M))
        for key, expected in (('datadir', self.mysql_datadir), ('socket', Path(self.mysql_socket)),
                              ('log-bin', self.mysql_binlog_dir / self.mysql_binlog_prefix),
                              ('log-bin-index', self.mysql_binlog_index)):
            if not values.get(key) or Path(values[key]).resolve() != expected.resolve():
                self._fail('Offline MySQL startup path mismatch: ' + key)
        replicated = self.mysql_topology != 'standalone'
        if replicated and any(values.get(key) != 'TRUE' for key in ('skip-replica-start', 'super-read-only')):
            self._fail('Offline replication restore requires persistent skip_replica_start=ON and super_read_only=ON')
        persisted = self.mysql_datadir / 'mysqld-auto.cnf'
        if persisted.is_symlink():
            self._fail('Offline restore refuses symlinked persisted configuration')
        if persisted.exists():
            try:
                document = json.loads(persisted.read_text())
                if document.get('Version') not in (1, 2):
                    raise ValueError('unsupported format')
                def inspect(node):
                    if not isinstance(node, dict):
                        return
                    for name, item in node.items():
                        key = name.replace('_', '-')
                        if key in ('datadir', 'socket', 'log-bin', 'log-bin-index'):
                            raise ValueError('persisted path override')
                        if replicated and key in ('skip-replica-start', 'skip-slave-start', 'super-read-only'):
                            if not isinstance(item, dict) or str(item.get('Value', '')).upper() not in ('ON', '1', 'TRUE'):
                                raise ValueError('persisted unsafe startup override')
                        inspect(item)
                inspect(document)
            except (OSError, ValueError, TypeError):
                self._fail('Offline restore cannot prove persisted startup configuration is safe')
        self.offline_state = {'channels': 0, 'running': 0, 'skip_start': 'ON' if replicated else 'OFF',
                              'gtid': values.get('gtid-mode')}
        identity = self.mysql_datadir / 'auto.cnf'
        if identity.is_symlink():
            self._fail('Offline restore refuses symlinked server identity')
        if identity.is_file():
            parser = configparser.ConfigParser()
            parser.read(str(identity))
            self.offline_state['uuid'] = parser.get('auto', 'server-uuid')
        return int(parsed.group(1)), int(parsed.group(2))
    
    def _mysql_cmd(self, sql):
        """执行MySQL命令（用于版本检测）"""
        cmd = [
            "mysql",
            "--user={0}".format(self.mysql_user),
            "--password={0}".format(self.mysql_password),
            "--socket={0}".format(self.mysql_socket),
            "-e", sql
        ] + self.native_options.get("mysql", [])
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env_for_system_subprocess(),
        )
        if p.returncode != 0:
            raise RuntimeError("MySQL command failed")
        return p.stdout.strip()
    


class BackupManager:
    """备份管理器 - 封装所有备份相关操作"""
    
    def __init__(self, config):
        self.config = config
        self.today = datetime.now().strftime("%F")
        self.now = datetime.now().strftime("%H%M%S")
        # 使用统一的logger
        self.logger = setup_logger("mysqlbackup.manager")
    
    def fail(self, msg):
        """记录错误并退出"""
        self.logger.error(msg)
        sys.exit(1)
    
    def backup_lock_ddl(self):
        requested = self.config.xtrabackup_lock_ddl
        if requested in {"ON", "OFF"}:
            return requested
        probe = subprocess.run(
            ["xtrabackup", "--no-defaults", "--help"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=30, env=_env_for_system_subprocess(),
        )
        help_text = probe.stdout + "\n" + probe.stderr
        option = re.search(r"(?m)^\s*--lock-ddl(?:[=\[ \t].*|$)(?:\n(?!\s*--)[^\n]*)*", help_text)
        # PXB 8.4.0-5 prints usage with EXIT_FAILURE when no operation is
        # selected, even for --help. Accept that documented source path only
        # when a complete usage/lock-ddl section is present.
        if probe.returncode not in (0, 1) or not option or (probe.returncode == 1 and "Usage:" not in help_text):
            detail = probe.stderr.strip().replace(self.config.mysql_password, "***")
            raise RuntimeError("Unable to inspect XtraBackup lock-ddl capability rc={0}: {1}".format(probe.returncode, detail))
        reduced = bool(re.search(r"\bREDUCED\b", option.group()))
        if requested == "REDUCED" and not reduced:
            raise RuntimeError("Actual XtraBackup does not advertise lock-ddl=REDUCED; use ON")
        selected = "REDUCED" if reduced else "ON"
        self.logger.info("XtraBackup lock-ddl=%s requested=%s", selected, requested)
        return selected

    def command_options(self, target, *args, **kwargs):
        options = getattr(self.config, "native_options", {})
        configured = options.get(target, []) if isinstance(options, dict) else []
        return native_arguments(target, *configured, *args, **kwargs)

    def run_cmd(self, cmd, check=True):
        """执行系统命令"""
        if cmd[0] == "mysql":
            cmd = cmd + self.command_options("mysql")
        elif cmd[0] == "xtrabackup":
            for mode in ("backup", "prepare", "decompress", "copy-back"):
                if "--" + mode in cmd:
                    cmd = cmd + self.command_options("xtrabackup-" + mode)
                    break
        self.logger.info("RUN: {0}".format(self._redact_command(cmd)),
                         extra={'to_stdout': cmd[0] == 'xtrabackup'})
        if cmd[0] == 'xtrabackup':
            return self._stream_xtrabackup(cmd, check)
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env_for_system_subprocess(),
        )
        if p.returncode == 0 and p.stderr.strip():
            self.logger.info(p.stderr.strip().replace(self.config.mysql_password, "***"))
        if p.returncode != 0:
            if p.stderr.strip():
                detail = p.stderr.strip().replace(self.config.mysql_password, "***")
                self.logger.error(detail)
                # Keep durable CLI runners and operators able to diagnose the
                # first failing external command even when file logging is
                # unavailable or not collected.
                print(detail, file=sys.stderr)
            if check:
                raise RuntimeError("COMMAND FAILED {0}".format(self._redact_command(cmd)))
        return p.stdout.strip()

    def _stream_xtrabackup(self, cmd, check):
        """Drain both native pipes concurrently; diagnostics must not wait for exit."""
        password = self.config.mysql_password
        phase = next((mode for mode in ('backup', 'decompress', 'prepare', 'copy-back')
                      if '--' + mode in cmd), 'command')
        started = last_output = last_heartbeat = time.monotonic()
        stdout = []

        def emit(stream, line):
            nonlocal last_output
            if password:
                line = line.replace(password, '***')
                # A configured password may itself contain a line break.
                for part in password.splitlines():
                    if part:
                        line = line.replace(part, '***')
            self.logger.info('XtraBackup[%s]: %s', stream, line.rstrip('\r'),
                             extra={'to_stdout': True})
            last_output = time.monotonic()

        with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=_env_for_system_subprocess()) as process:
            try:
                with selectors.DefaultSelector() as selector:
                    for name in ('stdout', 'stderr'):
                        selector.register(getattr(process, name), selectors.EVENT_READ,
                                          [name, codecs.getincrementaldecoder('utf-8')('replace'), ''])
                    while selector.get_map() or process.poll() is None:
                        for key, _ in selector.select(timeout=1):
                            name, decoder, pending = key.data
                            chunk = os.read(key.fd, 8192)
                            decoded = decoder.decode(chunk, final=not chunk)
                            if name == 'stdout':
                                stdout.append(decoded)
                            pending += decoded
                            while '\n' in pending:
                                line, pending = pending.split('\n', 1)
                                emit(name, line)
                            # Bound a native line with no newline, retaining any
                            # password that straddles the fragment boundary.
                            limit = max(65536, 2 * len(password))
                            while len(pending) > limit + len(password):
                                cut = limit
                                if password:
                                    at = pending.rfind(password, 0, cut + len(password))
                                    if 0 <= at < cut < at + len(password):
                                        cut = at
                                emit(name, pending[:cut])
                                pending = pending[cut:]
                            key.data[2] = pending
                            if not chunk:
                                if pending:
                                    emit(name, pending)
                                selector.unregister(key.fileobj)
                        now = time.monotonic()
                        if (now - last_output >= XTRABACKUP_HEARTBEAT_SECONDS
                                and now - last_heartbeat >= XTRABACKUP_HEARTBEAT_SECONDS):
                            self.logger.info(
                                'XtraBackup still running: phase=%s pid=%s elapsed=%.0fs; '
                                'no native log line for %.0fs', phase, process.pid,
                                now - started, now - last_output, extra={'to_stdout': True})
                            last_heartbeat = now
                    returncode = process.wait()
            except BaseException:
                # Match subprocess.run's child-reaping guarantee on interrupt
                # or logging/read failure before the caller releases its lock.
                process.kill()
                process.wait()
                raise
        self.logger.log(logging.INFO if returncode == 0 else logging.ERROR,
                        'XtraBackup finished: phase=%s rc=%s elapsed=%.1fs',
                        phase, returncode, time.monotonic() - started,
                        extra={'to_stdout': True})
        if returncode != 0 and check:
            raise RuntimeError('COMMAND FAILED rc={0} {1}'.format(returncode, self._redact_command(cmd)))
        return ''.join(stdout).strip()

    @staticmethod
    def _redact_command(cmd):
        """Remove inline credentials before a command is written to logs/errors."""
        redacted = []
        for arg in cmd:
            if arg.startswith("--password="):
                redacted.append("--password=***")
            else:
                redacted.append(arg)
        return " ".join(redacted)
    
    def mysql_cmd(self, sql, *args, **kwargs):
        """执行MySQL命令"""
        return self.run_cmd([
            "mysql",
            "--user={0}".format(self.config.mysql_user),
            "--password={0}".format(self.config.mysql_password),
            "--socket={0}".format(self.config.mysql_socket),
            "-e", sql
        ] + native_arguments("mysql", *args, **kwargs))

    def _replication_state(self):
        """Observe local channels; never infer or control remote writers."""
        sql = """SELECT JSON_OBJECT(
            'channels', (SELECT COUNT(*) FROM performance_schema.replication_connection_configuration),
            'running', (SELECT COUNT(*) FROM performance_schema.replication_connection_status WHERE SERVICE_STATE='ON')
                     + (SELECT COUNT(*) FROM performance_schema.replication_applier_status WHERE SERVICE_STATE='ON'),
            'skip_start', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables
                          WHERE VARIABLE_NAME IN ('skip_replica_start','skip_slave_start') LIMIT 1),
            'log_updates', (SELECT VARIABLE_VALUE FROM performance_schema.global_variables
                           WHERE VARIABLE_NAME IN ('log_replica_updates','log_slave_updates') LIMIT 1),
            'group_members', (SELECT COUNT(*) FROM performance_schema.replication_group_members),
            'uuid', @@GLOBAL.server_uuid, 'gtid', @@GLOBAL.gtid_mode, 'version', VERSION(),
            'datadir', @@GLOBAL.datadir, 'binlog', @@GLOBAL.log_bin_basename,
            'index', @@GLOBAL.log_bin_index, 'super_read_only', @@GLOBAL.super_read_only);"""
        state = json.loads(self.mysql_cmd(sql, batch=True, skip_column_names=True))
        if not isinstance(state, dict) or not all(isinstance(state.get(k), int) for k in ('channels', 'running', 'group_members')):
            raise RuntimeError('Unable to establish replication state')
        if not self.config.mysql_container:
            paths = {'datadir': self.config.mysql_datadir,
                     'binlog': self.config.mysql_binlog_dir / self.config.mysql_binlog_prefix,
                     'index': self.config.mysql_binlog_index}
            if any(not state.get(key) or Path(state[key]).resolve() != expected.resolve()
                   for key, expected in paths.items()):
                raise RuntimeError('Configured local paths do not match the connected MySQL instance')
        if state['group_members']:
            raise RuntimeError('Group Replication is outside this asynchronous replication workflow')
        if state['channels'] > 1:
            raise RuntimeError('Only one asynchronous replication channel per instance is supported')
        if self.config.mysql_topology == 'standalone' and state['channels']:
            raise RuntimeError('Replication detected: configure mysql_topology explicitly')
        return state

    def _replication_backup_options(self):
        state = self._replication_state()
        self._backup_replication_state = state
        restored_authority = (state['running'] == 0 and state.get('super_read_only') == 1 and
                              (self.config.backup_base / 'lock/restore-needs-full').is_file())
        if self.config.mysql_topology == 'dual-primary' and state['channels'] != 1 and not restored_authority:
            raise RuntimeError('dual-primary backup requires its single configured replication channel')
        if not state['channels']:
            return []
        if state['running'] != 2 and not restored_authority:
            raise RuntimeError('Replica must have healthy receiver and applier threads before backup')
        if state['log_updates'] != 'ON':
            raise RuntimeError('Replica PITR requires log_replica_updates=ON')
        # PXB owns stopping/resuming the SQL thread and recording upstream coordinates.
        return ['--slave-info', '--safe-slave-backup']

    def _record_replication_backup(self, target):
        state = getattr(self, '_backup_replication_state', None)
        if state is None:
            return
        if state['channels'] and not any((target / ('xtrabackup_slave_info' + ext)).is_file()
                                         for ext in ('', '.zst', '.qp', '.lz4')):
            raise RuntimeError('Replica backup is missing upstream replication coordinates')
        (target / '.mysql_topology.json').write_text(json.dumps({
            'topology': self.config.mysql_topology, 'source': state}, indent=2) + '\n')

    def _replication_restore_check(self, confirmed, restarted=False):
        if self.config.mysql_topology != 'standalone' and not confirmed:
            raise RuntimeError('Replication restore requires --confirm-replication-isolated; isolate all application writers and peers first')
        state = (self.config.offline_state if getattr(self.config, 'offline', False) and not restarted
                 else self._replication_state())
        if self.config.mysql_topology != 'standalone':
            if state['skip_start'] != 'ON':
                raise RuntimeError('Set persistent skip_replica_start=ON and restart before auditing/restoring this instance')
            if restarted and state['running']:
                raise RuntimeError('Restored replication threads are running; refusing PITR')
        return state

    def _offline_backup_check(self, full):
        if not getattr(self.config, 'offline', False):
            return
        try:
            metadata = json.loads((Path(full) / '.mysql_topology.json').read_text())
            source = metadata['source']
            version = re.match(r'\d+\.\d+\.\d+', source['version']).group(0)
            if (metadata['topology'] != self.config.mysql_topology or
                    version != self.config.mysql_version_text or
                    source['gtid'] != self.config.offline_state['gtid']):
                raise ValueError('identity mismatch')
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise RuntimeError('Offline restore requires current backup metadata matching topology, exact MySQL version and GTID mode')

    def _donor_restore(self, full, target_state):
        """Never mix the target's old logs into another server's archive."""
        metadata = Path(full) / '.mysql_topology.json'
        if not metadata.is_file():
            if self.config.mysql_topology != 'standalone':
                raise RuntimeError('Replication restore requires backup source identity metadata')
            return False
        source = json.loads(metadata.read_text()).get('source', {})
        if not source.get('uuid'):
            raise RuntimeError('Backup source UUID is missing')
        return source['uuid'] != (target_state or {}).get('uuid')
    
    def ensure_dirs(self):
        """确保必要的目录存在"""
        for d in [
            "log", "lock", "full", "incr",
            "binlog", "binlog/state", "restore_audit"
        ]:
            (self.config.backup_base / d).mkdir(parents=True, exist_ok=True)
    
    def acquire_lock(self):
        """获取文件锁（Linux/Unix only）"""
        self.config.lock_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(self.config.lock_file), os.O_CREAT | os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                os.close(fd)
                self.logger.error("Another backup process is running; refusing concurrent operation")
                raise RuntimeError("BACKUP LOCK BUSY")
        except Exception as e:
            self.logger.error("Failed to acquire lock: {0}".format(e))
            sys.exit(1)
    
    def latest_full_date(self):
        """获取最新全量备份日期"""
        full_dirs = sorted(
            d for d in (self.config.backup_base / "full").iterdir()
            if d.is_dir() and not d.is_symlink() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name)
        )
        return full_dirs[-1].name if full_dirs else None
    
    def full_backup(self):
        """
        全量备份 - 遵循Percona XtraBackup官方最佳实践
        Reference: https://docs.percona.com/percona-xtrabackup/8.4/
        """
        self.logger.info("START FULL BACKUP ...", extra={'to_stdout': True})
        replica_options = self._replication_backup_options()
        if self.config.mysql_container:
            if not self.manage_mysql_service('status'):
                raise RuntimeError('MySQL container must be running for backup')
            self._verify_container_mounts({})
        lock_ddl = self.backup_lock_ddl()
        target = self.config.backup_base / "full" / self.today / self.now / "backup"
        target.mkdir(parents=True)
        
        backup_cmd = [
            "xtrabackup", "--backup",
            "--datadir={0}".format(self.config.mysql_datadir),
            "--log-bin={0}".format(self.config.mysql_binlog_dir / self.config.mysql_binlog_prefix),
            "--log-bin-index={0}".format(self.config.mysql_binlog_index),
            "--target-dir={0}".format(target),
            "--user={0}".format(self.config.mysql_user),
            "--password={0}".format(self.config.mysql_password),
            "--socket={0}".format(self.config.mysql_socket),
            "--parallel={0}".format(self.config.xtrabackup_parallel),
            "--compress",
            "--compress-threads={0}".format(self.config.xtrabackup_compress_threads),
            "--use-memory={0}".format(self.config.xtrabackup_use_memory),
            "--lock-ddl={0}".format(lock_ddl)
        ]
        
        try:
            self.run_cmd(backup_cmd + replica_options)
            
            # 验证备份完整性（压缩备份的文件会有.zst、.qp或.lz4后缀）
            # Reference: https://docs.percona.com/percona-xtrabackup/8.4/prepare-compressed-backup.html
            # Percona XtraBackup 8.0.34+ 默认使用 .zst (ZSTD)
            # Reference: https://docs.percona.com/percona-xtrabackup/8.4/create-compressed-backup.html
            required_files = ["xtrabackup_checkpoints", "xtrabackup_info"]
            for req_file in required_files:
                # 检查压缩文件（.zst、.qp、.lz4）或未压缩文件
                found = False
                for ext in ["", ".zst", ".qp", ".lz4"]:
                    if (target / "{0}{1}".format(req_file, ext)).exists():
                        found = True
                        break
                if not found:
                    self.fail("Backup incomplete: missing {0} (checked: {0}, {0}.zst, {0}.qp, {0}.lz4)".format(req_file))
            
            self._record_replication_backup(target)
            (target / ".binlog_generation").write_text(self.today + "/" + self.now)
            (target / ".backup_ok").touch()
            latest = self.config.backup_base / "full/latest_raw"
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            try:
                latest.symlink_to(target)
            except (OSError, RuntimeError) as e:
                self.logger.error("Failed to create symlink: {0}".format(e))
                raise
            latest_incr = self.config.backup_base / 'incr/latest'
            if latest_incr.is_symlink():
                latest_incr.unlink()
            restore_marker = self.config.backup_base / 'lock/restore-needs-full'
            if restore_marker.exists():
                restore_marker.unlink()
            self.logger.info("FULL BACKUP DONE SUCCESSFULLY!", extra={'to_stdout': True})
        except Exception as e:
            self.logger.error("FULL BACKUP FAILED: {0}".format(e))
            if target.exists():
                shutil.rmtree(str(target))
            raise
    
    def incr_backup(self):
        """
        增量备份 - 遵循Percona XtraBackup官方最佳实践
        Reference: https://docs.percona.com/percona-xtrabackup/8.4/
        """
        self.logger.info("START INCREMENTAL BACKUP ...", extra={'to_stdout': True})
        self._require_fresh_full()
        replica_options = self._replication_backup_options()
        if self.config.mysql_container:
            if not self.manage_mysql_service('status'):
                raise RuntimeError('MySQL container must be running for backup')
            self._verify_container_mounts({})
        latest_full = self.config.backup_base / "full/latest_raw"
        if not latest_full.exists():
            self.fail("NO FULL BACKUP, please make sure you have a full backup")
        
        # 解析全量备份路径（作为默认base）
        # latest_full 已确认存在，resolve() 如果失败说明符号链接损坏，应该失败
        base = latest_full.resolve()
        backup_root = self.config.backup_base.resolve()
        if backup_root not in base.parents or not base.is_dir() or not (base / ".backup_ok").exists():
            self.fail("LATEST FULL BACKUP INVALID")
        
        # 如果有增量备份，使用最新的增量备份作为base
        latest_incr = self.config.backup_base / "incr/latest"
        if latest_incr.exists():
            # latest_incr 已确认存在，resolve() 如果失败说明符号链接损坏
            # 失败时继续使用 latest_full 的值
            try:
                candidate = latest_incr.resolve()
                if backup_root not in candidate.parents or not candidate.is_dir() or not (candidate / ".backup_ok").exists():
                    self.fail("LATEST INCREMENTAL BACKUP INVALID")
                base = candidate
            except (OSError, RuntimeError) as e:
                self.logger.warning("Failed to resolve latest_incr symlink: {0}, using latest_full".format(e))
                # base 保持为 latest_full.resolve() 的值

        state = getattr(self, '_backup_replication_state', None)
        if state is not None:
            metadata = base / '.mysql_topology.json'
            previous = json.loads(metadata.read_text()) if metadata.is_file() else {}
            if (previous.get('topology') != self.config.mysql_topology or
                    any(previous.get('source', {}).get(key) != state.get(key) for key in ('uuid', 'gtid'))):
                raise RuntimeError('Incremental source identity/topology changed or metadata is missing; take a new full backup')
        
        lock_ddl = self.backup_lock_ddl()
        target = self.config.backup_base / "incr" / self.today / self.now
        target.mkdir(parents=True)
        
        backup_cmd = [
            "xtrabackup", "--backup",
            "--datadir={0}".format(self.config.mysql_datadir),
            "--log-bin={0}".format(self.config.mysql_binlog_dir / self.config.mysql_binlog_prefix),
            "--log-bin-index={0}".format(self.config.mysql_binlog_index),
            "--target-dir={0}".format(target),
            "--incremental-basedir={0}".format(base),
            "--user={0}".format(self.config.mysql_user),
            "--password={0}".format(self.config.mysql_password),
            "--socket={0}".format(self.config.mysql_socket),
            "--parallel={0}".format(self.config.xtrabackup_parallel),
            "--compress",
            "--compress-threads={0}".format(self.config.xtrabackup_compress_threads),
            "--use-memory={0}".format(self.config.xtrabackup_use_memory),
            "--lock-ddl={0}".format(lock_ddl)
        ]
        
        try:
            self.run_cmd(backup_cmd + replica_options)
            
            # 验证备份完整性（压缩备份的文件会有.zst、.qp或.lz4后缀）
            # Reference: https://docs.percona.com/percona-xtrabackup/8.4/prepare-compressed-backup.html
            # Percona XtraBackup 8.0.34+ 默认使用 .zst (ZSTD)
            # Reference: https://docs.percona.com/percona-xtrabackup/8.4/create-compressed-backup.html
            required_files = ["xtrabackup_checkpoints", "xtrabackup_info"]
            for req_file in required_files:
                # 检查压缩文件（.zst、.qp、.lz4）或未压缩文件
                found = False
                for ext in ["", ".zst", ".qp", ".lz4"]:
                    if (target / "{0}{1}".format(req_file, ext)).exists():
                        found = True
                        break
                if not found:
                    self.fail("Incremental backup incomplete: missing {0} (checked: {0}, {0}.zst, {0}.qp, {0}.lz4)".format(req_file))

            self._record_replication_backup(target)
            (target / ".backup_ok").touch()
            if latest_incr.exists() or latest_incr.is_symlink():
                latest_incr.unlink()
            try:
                latest_incr.symlink_to(target)
            except (OSError, RuntimeError) as e:
                self.logger.error("Failed to create symlink: {0}".format(e))
                raise
            self.logger.info("INCREMENTAL BACKUP DONE SUCCESSFULLY!", extra={'to_stdout': True})
        except Exception as e:
            self.logger.error("INCREMENTAL BACKUP FAILED: {0}".format(e))
            if target.exists():
                shutil.rmtree(str(target))
            raise
    
    def _require_fresh_full(self):
        if (self.config.backup_base / 'lock/restore-needs-full').exists():
            raise RuntimeError('Take a new full backup after restore before incremental or binlog backup')

    def _binlog_root(self, full=None):
        """A full generation owns its immutable log history and archive cursor."""
        root = self.config.backup_base / 'binlog'
        if full is None:
            latest = self.config.backup_base / 'full/latest_raw'
            if not latest.exists():
                return root
            full = latest.resolve()
        marker = Path(full) / '.binlog_generation'
        if not marker.exists():
            return root  # Previously created backups retain their legacy layout.
        generation = marker.read_text().strip()
        expected = Path(full).parent.parent.name + '/' + Path(full).parent.name
        if generation != expected or not re.fullmatch(r'\d{4}-\d{2}-\d{2}/\d{6}', generation):
            raise RuntimeError('Backup binlog generation is invalid')
        return root / 'generation' / generation

    def binlog_backup(self, backall=False):
        """
        binlog归档
        
        Args:
            backall: 是否包含最后一个（当前活跃的）binlog文件
                False: 只备份已关闭的binlog（默认，日常备份用）
                True: 备份所有binlog，包括当前活跃的（手动停mysql服务）
        """
        self.logger.info("CHECK AND ARCHIVE BINLOG ...")
        self._require_fresh_full()
        archive_root = self._binlog_root()
        state = archive_root / "state/last_archived"
        state.parent.mkdir(parents=True, exist_ok=True)
        last_archived = None
        if state.exists():
            try:
                last_archived = state.read_text().strip()
            except (IOError, OSError) as e:
                raise RuntimeError("Unable to read binlog archive state") from e

        index = self.config.mysql_binlog_index
        if not index.exists():
            self.fail("{0}.index NOT FOUND".format(self.config.mysql_binlog_prefix))

        with open(str(index)) as f:
            # MySQL writes either absolute or relative paths in the .index
            # file; resolve basenames in the configured local binlog directory.
            binlogs = [Path(l.strip()).name for l in f if l.strip()]

        pattern = re.compile(re.escape(self.config.mysql_binlog_prefix) + r'\.\d+')
        if any(not pattern.fullmatch(name) for name in binlogs):
            raise RuntimeError('Invalid binlog index entry')
        numbers = [int(name.rsplit('.', 1)[1]) for name in binlogs]
        if any(right != left + 1 for left, right in zip(numbers, numbers[1:])):
            raise RuntimeError('Binlog index has a gap or duplicate entry')
        if last_archived:
            if not pattern.fullmatch(last_archived):
                raise RuntimeError('Invalid binlog archive state')
            last_number = int(last_archived.rsplit('.', 1)[1])
            if numbers and (numbers[0] > last_number + 1 or numbers[-1] < last_number):
                raise RuntimeError('Binlog history changed or was purged before archival; take a new full backup')

        if len(binlogs) == 0:
            self.logger.info("NO BINLOG FOUND")
            return

        # 根据backall参数决定是否包含最后一个binlog
        if backall:
            closed = binlogs
            self.logger.info("BACKUP MODE: INCLUDING LAST ACTIVE BINLOG")
        else:
            if len(binlogs) < 2:
                self.logger.info("NO CLOSED BINLOG")
                return
            closed = binlogs[:-1]
            self.logger.info("BACKUP MODE: ONLY CLOSED BINLOGS")

        if last_archived:
            closed = [b for b in closed if int(b.rsplit(".", 1)[1]) > last_number]

        if not closed:
            self.logger.info("NO NEW BINLOG")
            return

        dst = archive_root / self.today
        dst.mkdir(parents=True, exist_ok=True)

        archived = []
        for b in closed:
            src_file = self.config.mysql_binlog_dir / b
            dst_file = dst / b
            if not src_file.exists():
                raise RuntimeError("Binlog archive source missing: " + b)
            try:
                with tempfile.NamedTemporaryFile(prefix=".archive-", dir=str(dst), delete=False) as temporary:
                    temporary_path = Path(temporary.name)
                try:
                    shutil.copy2(str(src_file), str(temporary_path))
                    os.replace(str(temporary_path), str(dst_file))
                finally:
                    if temporary_path.exists():
                        temporary_path.unlink()
                archived.append(b)
                self.logger.info("ARCHIVED {0}".format(b))
            except (IOError, OSError, shutil.Error) as e:
                self.fail("Failed to copy binlog {0}: {1}".format(b, e))

        if not archived:
            self.fail("NO BINLOG FILES TO ARCHIVE")
        
        try:
            with tempfile.NamedTemporaryFile(mode="w", prefix=".state-", dir=str(state.parent), delete=False) as temporary:
                temporary.write(archived[-1])
                temporary_path = Path(temporary.name)
            try:
                os.replace(str(temporary_path), str(state))
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        except (IOError, OSError) as e:
            self.logger.error("Failed to write last_archived state: {0}".format(e))
            raise
        self.logger.info("BINLOG ARCHIVE COMPLETE SUCCESSFULLY!")
    
    def backup_chain(self, date):
        """Select one full generation and its contiguous raw incremental chain."""
        root = self.config.backup_base
        def full_time(path):
            return path.parent.parent.name, path.parent.name
        fulls = sorted((p for p in (root / 'full').glob('*/*/backup')
                        if p.is_dir() and re.fullmatch(r'\d{4}-\d{2}-\d{2}', full_time(p)[0])
                        and re.fullmatch(r'\d{6}', full_time(p)[1])), key=full_time)
        matches = [p for p in fulls if full_time(p)[0] == date]
        if not matches:
            self.fail('FULL BACKUP DIRECTORY NOT FOUND for date ' + date)
        full = matches[-1]
        start = full_time(full)
        following = [full_time(p) for p in fulls if full_time(p) > start]
        end = min(following) if following else ('9999-99-99', '999999')
        increments = sorted(p for p in (root / 'incr').glob('*/*')
                            if p.is_dir() and start < (p.parent.name, p.name) < end
                            and re.fullmatch(r'\d{4}-\d{2}-\d{2}', p.parent.name)
                            and re.fullmatch(r'\d{6}', p.name))
        previous = 0
        for path in [full] + increments:
            if not (path / '.backup_ok').is_file():
                self.fail('BACKUP INVALID: missing .backup_ok ' + str(path))
            try:
                values = dict(line.strip().split(' = ', 1) for line in
                              (path / 'xtrabackup_checkpoints').read_text().splitlines() if ' = ' in line)
                first, last = int(values['from_lsn']), int(values['to_lsn'])
            except (OSError, ValueError, KeyError):
                self.fail('BACKUP CHECKPOINT INVALID ' + str(path))
            if first != previous or last < first:
                self.fail('INCREMENTAL LSN CHAIN INVALID ' + str(path))
            previous = last
        return full, increments

    def checksum(self, date=None):
        """校验备份完整性"""
        date = date or self.latest_full_date()
        if not date:
            self.fail("NO FULL BACKUP FOUND")
        
        self.logger.info("CHECKSUM {0}".format(date))
        
        full, _ = self.backup_chain(date)
        binlog = self._binlog_root(full)
        # 只检查文件，排除目录
        binlog_files = [f for f in binlog.rglob("{0}.*".format(self.config.mysql_binlog_prefix)) 
                        if f.is_file() and f.relative_to(binlog).parts[0] != "generation" and f.name != "{0}.index".format(self.config.mysql_binlog_prefix)]
        if not binlog_files:
            self.fail("BINLOG MISSING")
        self.logger.info("CHECKSUM OK")
    
    def check_binlog_sequence(self, directory):
        """
        检查binlog文件连续性
        
        Args:
            directory: binlog目录路径
            
        Returns:
            list: binlog文件名列表
        """
        # 只获取文件，排除目录和index文件
        prefix = re.escape(self.config.mysql_binlog_prefix)
        files = [
            f for f in directory.rglob("{0}.*".format(self.config.mysql_binlog_prefix))
            if f.is_file() and f.relative_to(directory).parts[0] != "generation" and re.fullmatch(r"{0}\.\d+".format(prefix), f.name)
        ]
        
        if not files:
            self.fail("NO BINLOG FILES FOUND")
        
        # 解析binlog序号，处理异常
        numbered = []
        for f in files:
            try:
                num = int(f.name.rsplit(".", 1)[1])
                numbered.append((num, f))
            except (ValueError, IndexError):
                self.logger.warning("Invalid binlog filename format: {0}, skipping".format(f.name))
                continue
        
        if not numbered:
            self.fail("NO VALID BINLOG FILES FOUND")
        
        # 检查连续性
        numbered.sort(key=lambda item: item[0])
        nums = [item[0] for item in numbered]
        for i in range(1, len(nums)):
            if nums[i] != nums[i - 1] + 1:
                self.fail("BINLOG GAP {0}->{1}".format(nums[i - 1], nums[i]))
        
        return [f.name for _, f in numbered]
    
    def restore_audit(self, date=None):
        """生成恢复计划（dry-run）"""
        date = date or self.latest_full_date()
        if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            self.fail("INVALID BACKUP DATE")
        self.logger.info("RESTORE AUDITION {0}".format(date))
        if not getattr(self.config, 'offline', False):
            self._replication_state()
        self.checksum(date)

        plan = {
            "date": date,
            "generated": datetime.now().isoformat(),
            "full": None,
            "incr": [],
            "binlog": [],
            "status": "READY"
        }

        full, increments = self.backup_chain(date)
        self._offline_backup_check(full)
        plan['full'] = str(full)
        plan['incr'] = [str(path) for path in increments]

        binlog_dir = self._binlog_root(full)
        plan["binlog_root"] = str(binlog_dir)
        plan["binlog"] = self.check_binlog_sequence(binlog_dir)
        if self.config.mysql_container:
            if not self.manage_mysql_service('status'):
                self.fail('MySQL container must be running to audit its storage mapping')
            self._verify_container_mounts(plan)

        audit = self.config.audit_base / date
        audit.mkdir(parents=True, exist_ok=True)
        with open(str(audit / "restore.plan.json"), "w") as f:
            json.dump(plan, f, indent=2)

        self.logger.info("RESTORE AUDITION IS OK, PLAN GENERATED")
    
    def manage_mysql_service(self, action, timeout=30):
        """
        统一的MySQL服务管理函数（Linux/Unix only）
        
        Args:
            action: 操作类型，支持 'stop'、'start'、'restart'、'status'
            timeout: 超时时间（秒），默认30秒

        Returns:
            bool: 操作成功返回True，超时或失败返回False
        """
        valid_actions = ['stop', 'start', 'restart', 'status']
        if action not in valid_actions:
            self.logger.error("Invalid action '{0}'. Valid actions: {1}".format(action, ", ".join(valid_actions)))
            return False

        if self.config.mysql_container:
            return self._manage_mysql_container(action, timeout)

        # 检查服务是否存在
        try:
            self.run_cmd(["systemctl", "status", self.config.mysql_service], check=False)
        except RuntimeError:
            self.logger.warning("{0} service not found, assuming it's not running".format(self.config.mysql_service))
            return action == 'stop'

        self.logger.info("EXECUTING '{0}' FOR {1} (timeout={2}s)...".format(action.upper(), self.config.mysql_service, timeout))

        # 执行操作
        if action != 'status':
            self.run_cmd(["systemctl", action, self.config.mysql_service])

        # 确定期望的最终状态
        if action == 'stop':
            expected_state = "inactive"
        elif action == 'start':
            expected_state = "active"
        elif action == 'restart':
            expected_state = "active"
        else:  # status
            result = self.run_cmd(["systemctl", "is-active", self.config.mysql_service], check=False)
            current_state = result.strip()
            self.logger.info("SERVICE STATUS: {0}".format(current_state))
            return current_state == "active"

        # 检查状态变化，带进度条
        start_time = time.monotonic()
        check_interval = 1

        while time.monotonic() - start_time < timeout:
            elapsed = int(time.monotonic() - start_time)
            progress = min(100, int((elapsed / timeout) * 100))

            bar = "[" + "=" * (progress // 5) + " " * (20 - progress // 5) + "]"
            status_text = "{0}PING".format(action.upper()) if action.endswith('p') else "{0}ING".format(action.upper())
            sys.stdout.write("\rChecking: {0} {1}% ({2}/{3}s) - {4}...".format(bar, progress, elapsed, timeout, status_text))
            sys.stdout.flush()

            result = self.run_cmd(["systemctl", "is-active", self.config.mysql_service], check=False)
            current_state = result.strip()

            if current_state == expected_state:
                if action in ('start', 'restart'):
                    try:
                        self.mysql_cmd('SELECT 1;')
                    except RuntimeError:
                        time.sleep(check_interval)
                        continue
                sys.stdout.write("\n")
                self.logger.info("SERVICE {0} SUCCESSFUL in {1}s (state: {2})".format(action.upper(), elapsed, current_state))
                return True

            time.sleep(check_interval)

        # 超时处理
        sys.stdout.write("\n")
        result = self.run_cmd(["systemctl", "is-active", self.config.mysql_service], check=False)
        current_state = result.strip()

        if action == 'stop' and current_state == 'inactive':
            self.logger.info("SERVICE STOPPED (timed out but reached inactive state)")
            return True
        elif action in ['start', 'restart'] and current_state == 'active':
            try:
                self.mysql_cmd('SELECT 1;')
            except RuntimeError:
                self.logger.error('MySQL service is active but SQL readiness timed out')
                return False
            return True

        self.logger.error("SERVICE {0} TIMEOUT ({1}s). Current state: {2}".format(action.upper(), timeout, current_state))
        return False

    def _verify_container_mounts(self, plan):
        """Refuse a different daemon/volume mapping before touching MySQL data."""
        values = self.mysql_cmd('SELECT @@datadir, @@log_bin_basename, @@log_bin_index, @@socket;',
                                batch=True, skip_column_names=True).strip().split('\t')
        if len(values) != 4:
            raise RuntimeError('Unable to read MySQL container storage paths')
        if Path(values[1]).name != self.config.mysql_binlog_prefix:
            raise RuntimeError('Configured MySQL binlog prefix does not match the server')
        mounts = json.loads(self.run_cmd(['docker', 'inspect', '--type', 'container', '--format',
                                         '{{json .Mounts}}', self._mysql_container_id]))
        def local_path(server_path):
            path = Path(server_path)
            for mount in sorted(mounts, key=lambda m: len(m['Destination']), reverse=True):
                destination = Path(mount['Destination'])
                if path == destination or destination in path.parents:
                    if mount['Type'] != 'bind' or not mount['RW']:
                        break
                    return (Path(mount['Source']) / path.relative_to(destination)).resolve()
            raise RuntimeError('MySQL storage is not backed by a verified writable bind mount: ' + server_path)
        configured = [self.config.mysql_datadir, self.config.mysql_binlog_dir,
                      self.config.mysql_binlog_index, Path(self.config.mysql_socket)]
        server = [values[0], str(Path(values[1]).parent), values[2], values[3]]
        for remote, local in zip(server, configured):
            if local_path(remote) != local.resolve():
                raise RuntimeError('Configured MySQL path does not match container bind mount: ' + str(local))
        server_dir = str(Path(values[1]).parent)
        if plan.get('server_binlog_dir', server_dir) != server_dir:
            raise RuntimeError('Container binlog path changed since restore audit')
        plan['server_binlog_dir'] = server_dir

    def _manage_mysql_container(self, action, timeout):
        """Operate on one pinned container and distinguish running from SQL-ready."""
        target = getattr(self, '_mysql_container_id', self.config.mysql_container)
        def state():
            result = self.run_cmd(['docker', 'inspect', '--type', 'container', '--format',
                                   '{{.Id}} {{.State.Status}}', target]).strip().split()
            if len(result) != 2 or not re.fullmatch(r'[a-f0-9]{64}', result[0]):
                raise RuntimeError('Unable to verify MySQL container identity/state')
            return result
        identity, current = state()
        self._mysql_container_id = target = identity
        if current not in ('running', 'exited', 'created'):
            raise RuntimeError('MySQL container is not in a manageable state: ' + current)
        if action == 'status':
            return current == 'running'
        command = ['docker', action]
        if action in ('stop', 'restart'):
            command.extend(['--timeout', str(timeout)])
        self.run_cmd(command + [identity])
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, current = state()
            if action == 'stop' and current in ('exited', 'created'):
                return True
            if action in ('start', 'restart') and current == 'running':
                try:
                    self.mysql_cmd('SELECT 1;')
                    return True
                except RuntimeError:
                    pass
            time.sleep(1)
        self.logger.error('MySQL container %s did not reach %s within %ss', identity, action, timeout)
        return False
    
    def _load_restore_plan(self, date):
        """
        加载恢复计划
        
        Args:
            date: 备份日期
            
        Returns:
            tuple: (plan字典, plan_file路径)
        """
        if not date or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            self.fail("INVALID BACKUP DATE")
        plan_file = self.config.audit_base / date / "restore.plan.json"
        
        if not plan_file.exists():
            self.fail("NO RESTORE PLAN, RESTORE BLOCKED")
        
        with open(str(plan_file)) as f:
            plan = json.load(f)
        
        if plan["status"] != "READY":
            self.fail("PLAN INVALID")

        # The plan is an audit gate, not an instruction to follow arbitrary paths.
        if plan.get("date") != date:
            self.fail("PLAN DATE MISMATCH")
        base = self.config.backup_base.resolve()
        for key in ("full",):
            candidate = Path(plan.get(key, "")).resolve()
            if base not in candidate.parents:
                self.fail("PLAN PATH OUTSIDE BACKUP BASE")
            if not candidate.is_dir():
                self.fail("PLAN BACKUP DIRECTORY MISSING")
        for candidate_name in plan.get("incr", []):
            candidate = Path(candidate_name).resolve()
            if base not in candidate.parents:
                self.fail("PLAN PATH OUTSIDE BACKUP BASE")
            if not candidate.is_dir():
                self.fail("PLAN INCREMENTAL DIRECTORY MISSING")
        prefix = re.escape(self.config.mysql_binlog_prefix)
        if any(not re.fullmatch(r"{0}\.\d+".format(prefix), name) for name in plan.get("binlog", [])):
            self.fail("PLAN BINLOG NAME INVALID")

        full, increments = self.backup_chain(date)
        if Path(plan.get('binlog_root', self.config.backup_base / 'binlog')).resolve() != self._binlog_root(full).resolve():
            raise RuntimeError('Restore plan binlog generation does not match its full backup')
        if plan['full'] != str(full) or plan.get('incr', []) != [str(path) for path in increments]:
            raise RuntimeError('Backup chain changed since restore audit; generate a new plan')
        return plan, plan_file
    
    def _decompress_backup(self, plan):
        """
        解压压缩的备份文件
        Reference: https://docs.percona.com/percona-xtrabackup/8.4/prepare-compressed-backup.html
        
        Args:
            plan: 恢复计划字典
        """
        self.logger.info("DECOMPRESSING BACKUP FILES...")
        
        full_backup_path = Path(plan["full"])
        # 检查压缩文件：Percona XtraBackup 8.0.34+ 默认使用 .zst (ZSTD)
        # 旧版本可能使用 .qp (qpress)，也支持 .lz4
        # Reference: https://docs.percona.com/percona-xtrabackup/8.4/create-compressed-backup.html
        has_compressed = any(f.suffix in ['.zst', '.qp', '.lz4'] for f in full_backup_path.rglob('*') if f.is_file())
        
        if not has_compressed:
            self.logger.info("Backup is not compressed, skipping decompression")
            return
        
        self.logger.info("Detected compressed backup, decompressing...")
        
        # 解压全量备份
        self.run_cmd([
            "xtrabackup", "--decompress",
            "--parallel={0}".format(self.config.xtrabackup_parallel),
            "--remove-original",
            "--target-dir={0}".format(plan['full'])
        ])
        
        # 解压所有增量备份
        for inc in plan["incr"]:
            self.run_cmd([
                "xtrabackup", "--decompress",
                "--parallel={0}".format(self.config.xtrabackup_parallel),
                "--remove-original",
                "--target-dir={0}".format(inc)
            ])
        
        self.logger.info("Decompression completed")
    
    def _prepare_backup(self, plan):
        """
        准备备份文件（apply-log）
        Reference: https://docs.percona.com/percona-xtrabackup/8.4/prepare-incremental-backup.html
        
        Args:
            plan: 恢复计划字典
        """
        self.logger.info("PREPARING BACKUP FILES...")
        
        # 准备全量备份（使用--apply-log-only，为后续增量备份做准备）
        self.run_cmd([
            "xtrabackup", "--prepare", "--apply-log-only",
            "--use-memory={0}".format(self.config.xtrabackup_use_memory),
            "--target-dir", plan["full"]
        ])
        
        # 应用所有增量备份
        incr_list = plan["incr"]
        for i, inc in enumerate(incr_list):
            is_last = (i == len(incr_list) - 1)
            cmd = [
                "xtrabackup", "--prepare",
                "--use-memory={0}".format(self.config.xtrabackup_use_memory),
                "--target-dir", plan["full"],
                "--incremental-dir", inc
            ]
            # 除了最后一个增量备份，其他都使用--apply-log-only
            if not is_last:
                cmd.insert(2, "--apply-log-only")
            self.run_cmd(cmd)
        
        # 如果没有增量备份，执行最终prepare
        if not incr_list:
            self.run_cmd([
                "xtrabackup", "--prepare",
                "--use-memory={0}".format(self.config.xtrabackup_use_memory),
                "--target-dir", plan["full"]
            ])
    
    def _restore_datadir(self, plan):
        """Copy into empty destinations, retaining a rollback of only owned paths."""
        data = self.config.mysql_datadir
        logs = self.config.mysql_binlog_dir
        index = self.config.mysql_binlog_index
        if data.is_symlink() or not data.is_dir() or data.resolve() == Path('/'):
            raise RuntimeError('Restore requires an existing ordinary MySQL datadir')
        owner = data.stat()
        for name in ('auto.cnf', 'mysqld-auto.cnf'):
            identity_file = data / name
            if identity_file.is_symlink() or (identity_file.exists() and not identity_file.is_file()):
                raise RuntimeError('Refusing non-regular server identity or persisted configuration file')
        suffix = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        saved = data.with_name(data.name + '.bak_' + suffix)
        external = data.resolve() != logs.resolve() and data.resolve() not in logs.resolve().parents
        saved_logs = logs / ('.' + self.config.mysql_binlog_prefix + '.before_restore_' + suffix)
        pattern = re.compile(re.escape(self.config.mysql_binlog_prefix) + r'\.\d+')
        def binlog_files():
            return [path for path in logs.iterdir() if pattern.fullmatch(path.name)] + ([index] if index.exists() else [])
        if external and any(p.is_symlink() or not p.is_file() for p in binlog_files()):
            raise RuntimeError('Refusing non-regular external binlog/index files')
        moved = copying = False
        self._restore_restart_safe = False
        try:
            if external:
                saved_logs.mkdir()
                for path in binlog_files():
                    path.rename(saved_logs / path.name)
            data.rename(saved)
            moved = True
            data.mkdir(mode=0o750)
            logs.mkdir(parents=True, exist_ok=True)
            copying = True
            self.run_cmd([
                'xtrabackup', '--copy-back', '--target-dir', plan['full'], '--datadir', str(data),
                '--log-bin=' + str(logs / self.config.mysql_binlog_prefix),
                '--log-bin-index=' + str(index),
            ])
            # PXB excludes auto.cnf. Keep the target's UUID when restoring this
            # instance, rather than generating a new identity on every restore.
            if (saved / 'auto.cnf').is_file():
                shutil.copy2(str(saved / 'auto.cnf'), str(data / 'auto.cnf'))
            # Persisted variables are target configuration. A donor's startup
            # settings must not override this node's skip-start/read-only guard.
            if (saved / 'mysqld-auto.cnf').is_file():
                shutil.copy2(str(saved / 'mysqld-auto.cnf'), str(data / 'mysqld-auto.cnf'))
            elif (data / 'mysqld-auto.cnf').exists():
                (data / 'mysqld-auto.cnf').unlink()
            # PXB writes the copy-back process's path into an external index.
            # MySQL must instead read paths visible inside its own container.
            if self.config.mysql_container:
                server_dir = plan.get('server_binlog_dir')
                if not server_dir:
                    raise RuntimeError('Verified container binlog path is missing')
                names = [Path(line.strip()).name for line in index.read_text().splitlines() if line.strip()]
                if not names or any(not pattern.fullmatch(name) or not (logs / name).is_file() for name in names):
                    raise RuntimeError('Restored binlog index does not reference valid local files')
                temporary = index.with_name(index.name + '.restore-tmp')
                try:
                    temporary.write_text(''.join(str(Path(server_dir) / name) + '\n' for name in names))
                    os.replace(str(temporary), str(index))
                finally:
                    if temporary.exists():
                        temporary.unlink()
            identity = '{}:{}'.format(owner.st_uid, owner.st_gid)
            self.run_cmd(['chown', '-R', identity, str(data)])
            if external:
                for path in binlog_files():
                    self.run_cmd(['chown', identity, str(path)])
        except BaseException:
            # A failed copy-back must never restart on a partial/empty datadir.
            # If this rollback fails, the caller leaves MySQL stopped.
            if moved:
                if data.exists():
                    shutil.rmtree(str(data))
                saved.rename(data)
            if external and saved_logs.exists():
                if copying:
                    for path in binlog_files():
                        path.unlink()
                for path in saved_logs.iterdir():
                    path.rename(logs / path.name)
                saved_logs.rmdir()
            self._restore_restart_safe = True
            raise
        self._restore_restart_safe = True
        self.logger.info('Original datadir retained at %s', saved)
        if external:
            self.logger.info('Original binary logs retained at %s; other logs unchanged', saved_logs)

    def _binlog_replay_files(self, plan):
        """Start after the last physical snapshot, with every audited file present."""
        snapshot = Path((plan.get('incr') or [plan['full']])[-1])
        try:
            fields = (snapshot / 'xtrabackup_binlog_info').read_text().split()
            name, position = fields[0], int(fields[1])
        except (OSError, ValueError, IndexError) as error:
            raise RuntimeError('Physical snapshot binlog coordinates are missing or invalid') from error
        if position < 4 or name not in plan['binlog']:
            raise RuntimeError('Archived binlogs do not include the physical snapshot position')
        names = plan['binlog'][plan['binlog'].index(name):]
        numbers = [int(item.rsplit('.', 1)[1]) for item in names]
        if any(right != left + 1 for left, right in zip(numbers, numbers[1:])):
            raise RuntimeError('Archived binlog sequence has a gap after the snapshot')
        files = []
        for name in names:
            archive_root = Path(plan.get('binlog_root', self.config.backup_base / 'binlog'))
            matches = sorted(path for path in archive_root.rglob(name) if path.is_file() and path.relative_to(archive_root).parts[0] != "generation")
            if len(matches) != 1:
                raise RuntimeError('Archived binlog missing or ambiguous: ' + name)
            files.append(matches[0])
        return position, files

    def _apply_binlog(self, plan, binlog_start_time, binlog_stop_time):
        """
        应用binlog
        Reference: https://dev.mysql.com/doc/refman/8.0/en/point-in-time-recovery.html
        
        Args:
            plan: 恢复计划字典
            binlog_start_time: binlog恢复起始时间
            binlog_stop_time: binlog恢复结束时间（可选）
        """
        self.logger.info("APPLYING BINLOG...")
        
        if not binlog_start_time:
            self.fail("BINLOG START TIME REQUIRED! Use --binlog-start-time 'YYYY-MM-DD HH:MM:SS'")
        
        position, binlog_files = self._binlog_replay_files(plan)

        # 构建mysqlbinlog命令
        mysqlbinlog_cmd = ["mysqlbinlog", "--start-position=" + str(position)] + self.command_options("mysqlbinlog")
        mysqlbinlog_cmd.extend(["--start-datetime", binlog_start_time])
        
        if binlog_stop_time:
            mysqlbinlog_cmd.extend(["--stop-datetime", binlog_stop_time])
        
        mysqlbinlog_cmd.extend(str(path) for path in binlog_files)
        
        # 执行binlog恢复
        try:
            child_env = _env_for_system_subprocess()
            p1 = subprocess.Popen(
                mysqlbinlog_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
            )
            p2 = subprocess.Popen(
                [
                    "mysql",
                    "--user={0}".format(self.config.mysql_user),
                    "--password={0}".format(self.config.mysql_password),
                    "--socket={0}".format(self.config.mysql_socket)
                ] + self.command_options("mysql"),
                stdin=p1.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
            )
            # Parent must close its duplicate writer so mysqlbinlog observes SIGPIPE
            # when the mysql client exits early.
            if p1.stdout:
                p1.stdout.close()
            stdout, stderr = p2.communicate()
            p1_stderr = p1.stderr.read() if p1.stderr else b""
            p1_rc = p1.wait()
            if p1_rc != 0 or p2.returncode != 0:
                details = (p1_stderr + (stderr or b"")).decode(errors="replace").strip()
                raise RuntimeError("BINLOG APPLY FAILED{0}".format(": " + details if details else ""))
            self.logger.info("BINLOG APPLIED SUCCESSFULLY")
        
        except Exception as e:
            self.logger.error("BINLOG APPLY ERROR: {0}".format(str(e)))
            raise
    
    def _update_restore_plan(self, plan, plan_file, binlog_start_time, binlog_stop_time):
        """
        更新恢复计划状态
        
        Args:
            plan: 恢复计划字典
            plan_file: 计划文件路径
            binlog_start_time: binlog恢复起始时间
            binlog_stop_time: binlog恢复结束时间
        """
        plan["status"] = "COMPLETED"
        plan["restore_completed"] = datetime.now().isoformat()
        plan["binlog_start_time"] = binlog_start_time
        plan["binlog_stop_time"] = binlog_stop_time
        
        with open(str(plan_file), "w") as f:
            json.dump(plan, f, indent=2)
    
    @contextlib.contextmanager
    def restore_workspace(self, plan):
        """Prepare disposable copies; the raw backup chain remains reusable."""
        with tempfile.TemporaryDirectory(prefix='restore-work-', dir=str(self.config.backup_base)) as directory:
            working = dict(plan)
            paths = [plan['full']] + plan['incr']
            copies = []
            for number, original in enumerate(paths):
                source = Path(original)
                if any(path.is_symlink() for path in source.rglob('*')):
                    raise RuntimeError('Refusing symbolic links in the raw backup tree')
                target = Path(directory) / str(number)
                shutil.copytree(str(source), str(target))
                copies.append(str(target))
            working['full'], working['incr'] = copies[0], copies[1:]
            yield working

    def restore(self, date=None, binlog_start_time=None, binlog_stop_time=None,
                confirm_destructive=False, confirm_replication_isolated=False):
        """
        执行恢复（强制要求dry-run计划）

        Args:
            date: 备份日期，默认为最新全量备份
            binlog_start_time: binlog恢复起始时间（必填，格式: 'YYYY-MM-DD HH:MM:SS'）
            binlog_stop_time: binlog恢复结束时间（可选）
        """
        if not confirm_destructive:
            self.fail("DESTRUCTIVE RESTORE BLOCKED: pass --confirm-destructive-restore")
        date = date or self.latest_full_date()
        
        self.logger.info("=" * 60)
        self.logger.info("MYSQL RESTORE STARTING...", extra={'to_stdout': True})
        self.logger.info("=" * 60)
        
        # Validate the immutable audit plan before mutating the service/datadir.
        plan, plan_file = self._load_restore_plan(date)
        self._offline_backup_check(plan.get('full'))
        target_state = self._replication_restore_check(confirm_replication_isolated)
        donor_restore = self._donor_restore(plan.get('full'), target_state)
        # Reject a missing/broken replay client while the original server and
        # data are still intact, rather than discovering it after copy-back.
        self.run_cmd(['mysqlbinlog', '--version'])
        was_active = self.manage_mysql_service('status')
        if self.config.mysql_container:
            self._verify_container_mounts(plan)
        # Preparing modifies the backup. Keep raw inputs and the live source
        # intact until the working copy is ready for copy-back.
        with self.restore_workspace(plan) as working:
            self._decompress_backup(working)
            self._binlog_replay_files(working)
            self._prepare_backup(working)
            stopped = bool(getattr(self.config, 'offline', False))
            try:
                if was_active:
                    stopped = True  # An interrupt during stop is recoverable.
                    if not self.manage_mysql_service('stop', timeout=30):
                        self.fail('FAILED TO STOP MYSQL SERVICE')
                try:
                    self.mysql_cmd('SELECT 1;')
                except RuntimeError:
                    pass
                else:
                    raise RuntimeError('MySQL is still accepting SQL; refusing restore with this service configuration')
                if not donor_restore and not (self.config.backup_base / 'lock/restore-needs-full').exists():
                    self.binlog_backup(backall=True)
                self._restore_datadir(working)
                # Copy-back creates a new server history. Do not append it to
                # the old backup's archive cursor before taking a new full.
                self._restore_restart_safe = False
                (self.config.backup_base / 'lock/restore-needs-full').write_text('new full required after restore\n')
                self._restore_restart_safe = True
                if stopped and not self.manage_mysql_service('start', timeout=60):
                    self.fail('FAILED TO START MYSQL SERVICE')
                self._replication_restore_check(confirm_replication_isolated, restarted=True)
                replicated = self.config.mysql_topology != 'standalone'
                if replicated:
                    self.mysql_cmd('SET GLOBAL read_only=ON; SET GLOBAL super_read_only=OFF;')
                try:
                    self._apply_binlog(working, binlog_start_time, binlog_stop_time)
                finally:
                    if replicated:
                        self.mysql_cmd('SET GLOBAL super_read_only=ON;')
                self._update_restore_plan(plan, plan_file, binlog_start_time, binlog_stop_time)
            except BaseException:
                if stopped and getattr(self, '_restore_restart_safe', True) and not self.manage_mysql_service('status'):
                    try:
                        self.manage_mysql_service('start', timeout=60)
                    except Exception as restart_error:
                        self.logger.error('FAILED TO RESTORE MYSQL SERVICE: %s', restart_error)
                raise

        self.logger.info("=" * 60)
        self.logger.info("MYSQL RESTORE COMPLETED SUCCESSFULLY!", extra={'to_stdout': True})
        self.logger.info("=" * 60)
    
    def purge(self, backup_days=15, binlog_days=30):
        """清理旧备份"""
        cutoff = datetime.now().date() - timedelta(days=backup_days)
        # Keep the full base preceding the retention window: increments within
        # the window may still depend on it. Never inspect table-file mtimes.
        days = [path.parent.parent.parent.name for path in
                (self.config.backup_base / 'full').glob('*/*/backup/.backup_ok')]
        prior = sorted(day for day in days if day <= str(cutoff) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', day))
        keep_from = min(str(cutoff), prior[-1]) if prior else str(cutoff)
        for kind in ('full', 'incr'):
            for path in (self.config.backup_base / kind).iterdir():
                if path.is_dir() and not path.is_symlink() and re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.name) and path.name < keep_from:
                    shutil.rmtree(str(path))
            for name in ('latest_raw', 'latest'):
                link = self.config.backup_base / kind / name
                if link.is_symlink() and not link.exists():
                    link.unlink()
        binlog = self.config.backup_base / 'binlog'
        candidates = list(binlog.iterdir()) + list((binlog / 'generation').glob('*/*/*'))
        cutoff_time = time.time() - binlog_days * 86400
        for path in candidates:
            if (not path.is_symlink() and path.is_dir() and re.fullmatch(r'\d{4}-\d{2}-\d{2}', path.name)
                    and path.stat().st_mtime < cutoff_time):
                if binlog.resolve() not in path.resolve().parents:
                    raise RuntimeError('Refusing purge outside the binlog archive')
                shutil.rmtree(str(path))



def parse_arguments():
    """解析命令行参数，提供完整的帮助文档"""
    parser = argparse.ArgumentParser(
        description='MySQL Enterprise Backup Script - Production Ready',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
        epilog="""
示例用法:

  1. 全量备份:
     %(prog)s full -c /etc/mysql/backup.conf

  2. 增量备份:
     %(prog)s incr -c /etc/mysql/backup.conf

  3. Binlog备份:
     %(prog)s binlog -c /etc/mysql/backup.conf

  4. 校验备份:
     %(prog)s checksum -c /etc/mysql/backup.conf
     %(prog)s checksum -c /etc/mysql/backup.conf --date 2025-01-20

  5. 恢复前审计（dry-run）:
     %(prog)s restore-dry-run -c /etc/mysql/backup.conf
     %(prog)s restore-dry-run -c /etc/mysql/backup.conf --date 2025-01-20

  6. 执行恢复:
     %(prog)s restore -c /etc/mysql/backup.conf --confirm-destructive-restore --binlog-start-time '2025-01-20 10:00:00'
     %(prog)s restore -c /etc/mysql/backup.conf --confirm-destructive-restore --date 2025-01-20 --binlog-start-time '2025-01-20 10:00:00' --binlog-stop-time '2025-01-20 12:00:00'

  7. 清理旧备份:
     %(prog)s purge -c /etc/mysql/backup.conf
     %(prog)s purge -c /etc/mysql/backup.conf --backup-days 30 --binlog-days 60

配置文件格式 (JSON):
{
    "backup_base": "/backup/mysql",
    "mysql_user": "backup",
    "mysql_password": "your_password",
    "mysql_socket": "/var/lib/mysql/mysql.sock",
    "mysql_service": "mysqld",
    "mysql_datadir": "/var/lib/mysql",
    "mysql_binlog_prefix": "mysql-bin",
    "xtrabackup_parallel": 4,
    "xtrabackup_compress_threads": 4,
    "xtrabackup_use_memory": "1G",
    "xtrabackup_lock_ddl": "AUTO"
}

环境变量（配置文件密码为空时使用）:
  MYSQL_BACKUP_PASSWORD  - MySQL备份密码

兼容性:
  - MySQL 5.7 + matching Percona XtraBackup 2.4
  - MySQL 8.0 + matching Percona XtraBackup 8.0
  - MySQL 8.4 + matching Percona XtraBackup 8.4
  - MySQL 9.x physical backup is unsupported; use MigrationCli logical migration
  - Python 3.6+

原生参数（普通备份不必填写）：
  例如限制备份阶段 I/O：
    %(prog)s full -c /etc/mysql/backup.conf --native-option xtrabackup-backup=--throttle=100
  xtrabackup-backup 表示备份阶段；参数不会同时应用到 prepare 或解压阶段。
  多个参数重复写 --native-option；带值选项用 =，路径含空格时给整个值加引号。
  凭据、输出路径、一致性和恢复范围由工具管理，不能从此入口覆盖。
  完整阶段映射、JSON 配置示例和拒绝参数的替代入口：%(prog)s --help-native

官方文档参考:
  - https://docs.percona.com/percona-xtrabackup/8.4/
  - https://docs.percona.com/percona-xtrabackup/8.4/prepare-compressed-backup.html
  - https://docs.percona.com/percona-xtrabackup/8.4/prepare-incremental-backup.html
        """
    )
    
    parser.add_argument(
        'command',
        choices=['full', 'incr', 'binlog', 'checksum', 'restore-dry-run', 'restore', 'purge'],
        help='要执行的命令'
    )
    
    parser.add_argument(
        '-c', '--config',
        default=None,
        help='配置文件路径（JSON格式）'
    )
    
    parser.add_argument(
        '--date',
        default=None,
        help='备份日期（用于checksum、restore-dry-run、restore命令），格式: YYYY-MM-DD'
    )
    
    parser.add_argument(
        '--binlog-start-time',
        default=None,
        help='Binlog恢复起始时间（restore命令必填），格式: YYYY-MM-DD HH:MM:SS'
    )
    
    parser.add_argument(
        '--binlog-stop-time',
        default=None,
        help='Binlog恢复结束时间（restore命令可选），格式: YYYY-MM-DD HH:MM:SS'
    )

    parser.add_argument(
        '--confirm-destructive-restore',
        action='store_true',
        help='确认停止服务并覆盖数据目录（restore 必须显式指定）'
    )
    parser.add_argument(
        '--confirm-replication-isolated', action='store_true',
        help='复制拓扑恢复：确认已隔离应用及对端；要求持久 skip_replica_start=ON，工具不自动重接复制'
    )
    parser.add_argument(
        '--offline-restore', action='store_true',
        help='已停机的二进制实例：仅用于 checksum/restore-dry-run/restore；须填写 mysql_defaults_file，核验真实 systemd ExecStart 和启动参数'
    )
    
    parser.add_argument(
        '--backup-days',
        type=int,
        default=15,
        help='备份保留天数（purge命令），默认: 15'
    )
    
    parser.add_argument(
        '--binlog-days',
        type=int,
        default=30,
        help='Binlog保留天数（purge命令），默认: 30'
    )
    
    parser.add_argument("--help-native", action="version", version=native_option_help(),
                        help="查看原生参数的阶段说明、完整示例、配置保存及拒绝清单；无需连接数据库")
    parser.add_argument("--native-option", action="append", default=[], metavar="TARGET=--OPTION[=VALUE]",
                        help="给指定命令或阶段追加一个原生长参数；可重复。完整阶段名和示例见 --help-native")
    args = parser.parse_args()
    if args.offline_restore and args.command not in ('checksum', 'restore-dry-run', 'restore'):
        parser.error('--offline-restore is only valid for checksum, restore-dry-run and restore')
    try:
        args.native_options = parse_native_options(args.native_option)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main():
    """主函数"""
    try:
        args = parse_arguments()
        
        # 加载配置
        config = Config(args.config, native_options=args.native_options, offline=args.offline_restore)
        manager = BackupManager(config)
        
        # 确保目录存在并获取锁
        manager.ensure_dirs()
        lock_handle = manager.acquire_lock()
        
        try:
            # 执行命令
            if args.command == 'full':
                manager.full_backup()
            elif args.command == 'incr':
                manager.incr_backup()
            elif args.command == 'binlog':
                manager.binlog_backup()
            elif args.command == 'checksum':
                manager.checksum(args.date)
            elif args.command == 'restore-dry-run':
                manager.restore_audit(args.date)
            elif args.command == 'restore':
                if not args.binlog_start_time:
                    manager.fail("--binlog-start-time is required for restore command")
                manager.restore(
                    args.date, args.binlog_start_time, args.binlog_stop_time,
                    confirm_destructive=args.confirm_destructive_restore,
                    confirm_replication_isolated=args.confirm_replication_isolated
                )
            elif args.command == 'purge':
                manager.purge(args.backup_days, args.binlog_days)
        finally:
            # 释放锁
            if lock_handle:
                try:
                    fcntl.flock(lock_handle, fcntl.LOCK_UN)
                    os.close(lock_handle)
                except Exception as ex:
                    print("Exception occurs: {}, but ignored".format(ex))
                    
    except KeyboardInterrupt:
        print("Operation cancelled by user")
        sys.exit(130)
    except Exception as e:
        print("FATAL ERROR: {0}".format(e))
        import traceback
        print(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()

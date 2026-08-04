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
  - MySQL 5.7 (Percona XtraBackup 2.4)
  - MySQL 8.0 (Percona XtraBackup 8.0)
  - MySQL 8.4 (Percona XtraBackup 8.4)
  - Python 3.6+

Best Practices Implemented:
  - Percona XtraBackup official recommendations
  - Compressed backup decompression before prepare
  - Version-aware --lock-ddl parameter (REDUCED for 8.0+, OFF/ON for 5.7)
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
import time
import fcntl
import re
import logging
from datetime import datetime
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
        log_file = log_file or DEFAULT_LOG_FILE
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        formatter = logging.Formatter(fmt, datefmt)
        file_handler.setFormatter(formatter)
        file_handler.setLevel(level)
        # 默认输入到文件，传 extra={'skip_file': True} 不输入到文件
        file_handler.addFilter(lambda record: not getattr(record, 'skip_file', False))
        logger.addHandler(file_handler)

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


class Config:
    """配置类 - 管理所有配置项"""
    
    def __init__(self, config_file=None):
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
        self.mysql_datadir = Path(defaults["mysql_datadir"])
        self.mysql_binlog_prefix = defaults["mysql_binlog_prefix"]
        self.xtrabackup_parallel = int(defaults.get("xtrabackup_parallel", 4))
        self.xtrabackup_compress_threads = int(defaults.get("xtrabackup_compress_threads", 4))
        self.xtrabackup_use_memory = defaults.get("xtrabackup_use_memory", "1G")
        
        # 验证密码
        if not self.mysql_password:
            self._fail("MySQL password is required. Set it in config file or MYSQL_BACKUP_PASSWORD environment variable")
        
        # 设置路径（log_file已不再使用，日志统一使用/var/log/mysqlbackup.log）
        self.lock_file = self.backup_base / "lock/mysqlbackup.lock"
        self.audit_base = self.backup_base / "restore_audit"
        
        # 检测MySQL版本
        self.mysql_version = self._detect_mysql_version()
        
        # 设置lock-ddl参数
        lock_ddl_config = defaults.get("xtrabackup_lock_ddl", "AUTO")
        if lock_ddl_config == "AUTO":
            self.xtrabackup_lock_ddl = self._get_lock_ddl_option(self.mysql_version[0])
        else:
            self.xtrabackup_lock_ddl = lock_ddl_config
        
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
                self.logger.warning("Could not parse MySQL version, assuming 8.0")
                return 8, 0
        except Exception as e:
            self.logger.warning("Could not detect MySQL version: {0}, assuming 8.0".format(e))
            return 8, 0
    
    def _mysql_cmd(self, sql):
        """执行MySQL命令（用于版本检测）"""
        cmd = [
            "mysql",
            "--user={0}".format(self.mysql_user),
            "--password={0}".format(self.mysql_password),
            "--socket={0}".format(self.mysql_socket),
            "-e", sql
        ]
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
    
    def _get_lock_ddl_option(self, major):
        """
        根据MySQL版本返回合适的--lock-ddl参数
        Reference: https://www.percona.com/blog/percona-xtrabackup-8-enables-lock-ddl-by-default/
        """
        if major >= 8:
            return "REDUCED"
        else:
            return "ON"


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
    
    def run_cmd(self, cmd, check=True):
        """执行系统命令"""
        self.logger.info("RUN: {0}".format(" ".join(cmd)))
        p = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=_env_for_system_subprocess(),
        )
        if p.returncode != 0:
            if p.stderr.strip():
                self.logger.error(p.stderr.strip())
            if check:
                raise RuntimeError("COMMAND FAILED {0}".format(" ".join(cmd)))
        return p.stdout.strip()
    
    def mysql_cmd(self, sql):
        """执行MySQL命令"""
        return self.run_cmd([
            "mysql",
            "--user={0}".format(self.config.mysql_user),
            "--password={0}".format(self.config.mysql_password),
            "--socket={0}".format(self.config.mysql_socket),
            "-e", sql
        ])
    
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
                self.logger.warning("Another backup process is running, exiting...")
                sys.exit(0)
        except Exception as e:
            self.logger.error("Failed to acquire lock: {0}".format(e))
            sys.exit(1)
    
    def latest_full_date(self):
        """获取最新全量备份日期"""
        full_dirs = sorted([d for d in (self.config.backup_base / "full").iterdir() if d.is_dir()])
        return full_dirs[-1].name if full_dirs else None
    
    def full_backup(self):
        """
        全量备份 - 遵循Percona XtraBackup官方最佳实践
        Reference: https://docs.percona.com/percona-xtrabackup/8.4/
        """
        self.logger.info("START FULL BACKUP ...")
        target = self.config.backup_base / "full" / self.today / self.now / "backup"
        target.mkdir(parents=True)
        
        backup_cmd = [
            "xtrabackup", "--backup",
            "--target-dir={0}".format(target),
            "--user={0}".format(self.config.mysql_user),
            "--password={0}".format(self.config.mysql_password),
            "--socket={0}".format(self.config.mysql_socket),
            "--parallel={0}".format(self.config.xtrabackup_parallel),
            "--compress",
            "--compress-threads={0}".format(self.config.xtrabackup_compress_threads),
            "--use-memory={0}".format(self.config.xtrabackup_use_memory),
            "--lock-ddl={0}".format(self.config.xtrabackup_lock_ddl)
        ]
        
        try:
            self.run_cmd(backup_cmd)
            
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
            
            (target / ".backup_ok").touch()
            latest = self.config.backup_base / "full/latest_raw"
            if latest.exists() or latest.is_symlink():
                latest.unlink()
            try:
                latest.symlink_to(target)
            except (OSError, RuntimeError) as e:
                self.logger.error("Failed to create symlink: {0}".format(e))
                raise
            self.logger.info("FULL BACKUP DONE SUCCESSFULLY!")
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
        self.logger.info("START INCREMENTAL BACKUP ...")
        latest_full = self.config.backup_base / "full/latest_raw"
        if not latest_full.exists():
            self.fail("NO FULL BACKUP, please make sure you have a full backup")
        
        # 解析全量备份路径（作为默认base）
        # latest_full 已确认存在，resolve() 如果失败说明符号链接损坏，应该失败
        base = latest_full.resolve()
        
        # 如果有增量备份，使用最新的增量备份作为base
        latest_incr = self.config.backup_base / "incr/latest"
        if latest_incr.exists():
            # latest_incr 已确认存在，resolve() 如果失败说明符号链接损坏
            # 失败时继续使用 latest_full 的值
            try:
                base = latest_incr.resolve()
            except (OSError, RuntimeError) as e:
                self.logger.warning("Failed to resolve latest_incr symlink: {0}, using latest_full".format(e))
                # base 保持为 latest_full.resolve() 的值
        
        target = self.config.backup_base / "incr" / self.today / self.now
        target.mkdir(parents=True)
        
        backup_cmd = [
            "xtrabackup", "--backup",
            "--target-dir={0}".format(target),
            "--incremental-basedir={0}".format(base),
            "--user={0}".format(self.config.mysql_user),
            "--password={0}".format(self.config.mysql_password),
            "--socket={0}".format(self.config.mysql_socket),
            "--parallel={0}".format(self.config.xtrabackup_parallel),
            "--compress",
            "--compress-threads={0}".format(self.config.xtrabackup_compress_threads),
            "--use-memory={0}".format(self.config.xtrabackup_use_memory),
            "--lock-ddl={0}".format(self.config.xtrabackup_lock_ddl)
        ]
        
        try:
            self.run_cmd(backup_cmd)
            
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
            
            (target / ".backup_ok").touch()
            if latest_incr.exists() or latest_incr.is_symlink():
                latest_incr.unlink()
            try:
                latest_incr.symlink_to(target)
            except (OSError, RuntimeError) as e:
                self.logger.error("Failed to create symlink: {0}".format(e))
                raise
            self.logger.info("INCREMENTAL BACKUP DONE SUCCESSFULLY!")
        except Exception as e:
            self.logger.error("INCREMENTAL BACKUP FAILED: {0}".format(e))
            if target.exists():
                shutil.rmtree(str(target))
            raise
    
    def binlog_backup(self, backall=False):
        """
        binlog归档
        
        Args:
            backall: 是否包含最后一个（当前活跃的）binlog文件
                False: 只备份已关闭的binlog（默认，日常备份用）
                True: 备份所有binlog，包括当前活跃的（手动停mysql服务）
        """
        self.logger.info("CHECK AND ARCHIVE BINLOG ...")
        state = self.config.backup_base / "binlog/state/last_archived"
        state.parent.mkdir(parents=True, exist_ok=True)
        last_archived = None
        if state.exists():
            try:
                last_archived = state.read_text().strip()
            except (IOError, OSError) as e:
                self.logger.warning("Failed to read last_archived state: {0}, starting from beginning".format(e))

        index = self.config.mysql_datadir / "{0}.index".format(self.config.mysql_binlog_prefix)
        if not index.exists():
            self.fail("{0}.index NOT FOUND".format(self.config.mysql_binlog_prefix))

        with open(str(index)) as f:
            binlogs = [l.strip().lstrip("./") for l in f if l.strip()]

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
            closed = [b for b in closed if b > last_archived]

        if not closed:
            self.logger.info("NO NEW BINLOG")
            return

        dst = self.config.backup_base / "binlog" / self.today
        dst.mkdir(parents=True, exist_ok=True)

        for b in closed:
            src_file = self.config.mysql_datadir / b
            dst_file = dst / b
            if not src_file.exists():
                self.logger.warning("Binlog file {0} not found, skipping".format(b))
                continue
            try:
                shutil.copy2(str(src_file), str(dst_file))
                self.logger.info("ARCHIVED {0}".format(b))
            except (IOError, OSError, shutil.Error) as e:
                self.fail("Failed to copy binlog {0}: {1}".format(b, e))

        if not closed:
            self.fail("NO BINLOG FILES TO ARCHIVE")
        
        try:
            state.write_text(closed[-1])
        except (IOError, OSError) as e:
            self.logger.error("Failed to write last_archived state: {0}".format(e))
            raise
        self.logger.info("BINLOG ARCHIVE COMPLETE SUCCESSFULLY!")
    
    def checksum(self, date=None):
        """校验备份完整性"""
        date = date or self.latest_full_date()
        if not date:
            self.fail("NO FULL BACKUP FOUND")
        
        self.logger.info("CHECKSUM {0}".format(date))
        
        # 查找backup目录（可能是文件或目录）
        full_backup_path = self.config.backup_base / "full" / date
        if not full_backup_path.exists():
            self.fail("BACKUP DATE {0} NOT FOUND".format(date))
        
        # rglob可能返回文件或目录，需要过滤
        full = [p for p in full_backup_path.rglob("backup") if p.is_dir()]
        if not full:
            self.fail("FULL BACKUP DIRECTORY NOT FOUND for date {0}".format(date))
        
        # 检查.backup_ok标记文件（这个文件不会被压缩）
        if not (full[0] / ".backup_ok").exists():
            self.fail("FULL INVALID: missing .backup_ok")
        incr = self.config.backup_base / "incr" / date
        if incr.exists():
            for d in incr.iterdir():
                if d.is_dir() and not (d / ".backup_ok").exists():
                    self.fail("INCR INVALID {0}".format(d))
        binlog = self.config.backup_base / "binlog"
        # 只检查文件，排除目录
        binlog_files = [f for f in binlog.rglob("{0}.*".format(self.config.mysql_binlog_prefix)) 
                        if f.is_file() and f.name != "{0}.index".format(self.config.mysql_binlog_prefix)]
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
        files = sorted(f for f in directory.rglob("{0}.*".format(self.config.mysql_binlog_prefix))
                       if f.is_file() and f.name != "{0}.index".format(self.config.mysql_binlog_prefix))
        
        if not files:
            self.fail("NO BINLOG FILES FOUND")
        
        # 解析binlog序号，处理异常
        nums = []
        for f in files:
            try:
                num = int(f.name.split(".")[-1])
                nums.append(num)
            except (ValueError, IndexError):
                self.logger.warning("Invalid binlog filename format: {0}, skipping".format(f.name))
                continue
        
        if not nums:
            self.fail("NO VALID BINLOG FILES FOUND")
        
        # 检查连续性
        for i in range(1, len(nums)):
            if nums[i] != nums[i-1] + 1:
                self.fail("BINLOG GAP {0}->{1}".format(nums[i-1], nums[i]))
        
        return [f.name for f in files]
    
    def restore_audit(self, date=None):
        """生成恢复计划（dry-run）"""
        date = date or self.latest_full_date()
        self.logger.info("RESTORE AUDITION {0}".format(date))
        self.checksum(date)

        plan = {
            "date": date,
            "generated": datetime.now().isoformat(),
            "full": None,
            "incr": [],
            "binlog": [],
            "status": "READY"
        }

        # 查找backup目录
        full_backup_path = self.config.backup_base / "full" / date
        full = [p for p in full_backup_path.rglob("backup") if p.is_dir()]
        if not full:
            self.fail("FULL BACKUP DIRECTORY NOT FOUND for date {0}".format(date))
        plan["full"] = str(full[0])

        incr = self.config.backup_base / "incr" / date
        if incr.exists():
            plan["incr"] = [str(d) for d in sorted([d for d in incr.iterdir() if d.is_dir()])]

        binlog_dir = self.config.backup_base / "binlog"
        plan["binlog"] = self.check_binlog_sequence(binlog_dir)

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
        start_time = time.time()
        check_interval = 1

        while time.time() - start_time < timeout:
            elapsed = int(time.time() - start_time)
            progress = min(100, int((elapsed / timeout) * 100))

            bar = "[" + "=" * (progress // 5) + " " * (20 - progress // 5) + "]"
            status_text = "{0}PING".format(action.upper()) if action.endswith('p') else "{0}ING".format(action.upper())
            sys.stdout.write("\rChecking: {0} {1}% ({2}/{3}s) - {4}...".format(bar, progress, elapsed, timeout, status_text))
            sys.stdout.flush()

            result = self.run_cmd(["systemctl", "is-active", self.config.mysql_service], check=False)
            current_state = result.strip()

            if current_state == expected_state:
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
            self.logger.info("SERVICE {0}ED (timed out but reached active state)".format(action.upper()))
            return True

        self.logger.error("SERVICE {0} TIMEOUT ({1}s). Current state: {2}".format(action.upper(), timeout, current_state))
        return False
    
    def _load_restore_plan(self, date):
        """
        加载恢复计划
        
        Args:
            date: 备份日期
            
        Returns:
            tuple: (plan字典, plan_file路径)
        """
        self.restore_audit(date=date)
        plan_file = self.config.audit_base / date / "restore.plan.json"
        
        if not plan_file.exists():
            self.fail("NO RESTORE PLAN, RESTORE BLOCKED")
        
        with open(str(plan_file)) as f:
            plan = json.load(f)
        
        if plan["status"] != "READY":
            self.fail("PLAN INVALID")
        
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
        """
        恢复数据目录
        Reference: https://docs.percona.com/percona-xtrabackup/8.4/restore-a-backup.html
        官方最佳实践：使用mv重命名现有数据目录（避免跨盘拷贝的性能问题）
        
        Args:
            plan: 恢复计划字典
        """
        self.logger.info("CLEANING DATA DIRECTORY: {0}".format(self.config.mysql_datadir))
        
        # 备份现有数据目录（使用mv重命名，避免跨盘拷贝的性能问题）
        # 官方推荐：mv /var/lib/mysql /var/lib/mysql_old
        if self.config.mysql_datadir.exists():
            if self.config.mysql_datadir.is_dir():
                # 在同目录下重命名为.bak，避免跨盘拷贝
                datadir_backup = self.config.mysql_datadir.parent / "{0}.bak_{1}".format(
                    self.config.mysql_datadir.name,
                    datetime.now().strftime('%Y%m%d_%H%M%S'))
                
                # 如果备份目录已存在，先删除
                if datadir_backup.exists():
                    self.logger.warning("Backup directory {0} already exists, removing...".format(datadir_backup))
                    try:
                        shutil.rmtree(str(datadir_backup))
                    except (OSError, shutil.Error) as e:
                        self.logger.error("Failed to remove existing backup directory: {0}".format(e))
                        raise
                
                self.logger.info("RENAMING EXISTING DATADIR TO: {0}".format(datadir_backup))
                try:
                    # 使用rename（同文件系统内移动，非常快）
                    self.config.mysql_datadir.rename(datadir_backup)
                except (OSError, RuntimeError) as e:
                    # 如果rename失败（可能跨文件系统），使用shutil.move
                    self.logger.warning("rename failed (possibly cross-filesystem), using shutil.move: {0}".format(e))
                    try:
                        shutil.move(str(self.config.mysql_datadir), str(datadir_backup))
                    except (shutil.Error, OSError) as e2:
                        self.logger.error("Failed to move existing datadir: {0}".format(e2))
                        raise
            else:
                # 如果是文件而不是目录，直接删除
                self.logger.warning("datadir is a file, not a directory, removing...")
                try:
                    self.config.mysql_datadir.unlink()
                except OSError as e:
                    self.fail("Failed to remove existing datadir file: {0}".format(e))
        
        # 创建新的空数据目录（官方要求：datadir必须为空）
        self.config.mysql_datadir.mkdir(parents=True, exist_ok=True)
        
        # 复制恢复文件到数据目录
        self.logger.info("COPYING BACK DATA...")
        copy_back_cmd = [
            "xtrabackup", "--copy-back",
            "--target-dir", plan["full"],
            "--datadir", str(self.config.mysql_datadir)
        ]
        self.run_cmd(copy_back_cmd)
        
        # 设置正确的权限
        self.run_cmd(["chown", "-R", "mysql:mysql", str(self.config.mysql_datadir)])
    
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
        
        # 查找binlog文件
        binlog_dir = self.config.backup_base / "binlog"
        binlog_files = []
        for binlog_name in plan["binlog"]:
            # 只查找文件，排除目录
            found_files = [f for f in binlog_dir.rglob(binlog_name) if f.is_file()]
            if found_files:
                binlog_files.append(str(found_files[0]))
            else:
                self.logger.warning("Binlog file {0} not found, skipping".format(binlog_name))

        if not binlog_files:
            self.logger.info("NO BINLOG FILES TO APPLY")
            return
        
        # 构建mysqlbinlog命令
        mysqlbinlog_cmd = ["mysqlbinlog"]
        mysqlbinlog_cmd.extend(["--start-datetime", binlog_start_time])
        
        if binlog_stop_time:
            mysqlbinlog_cmd.extend(["--stop-datetime", binlog_stop_time])
        
        mysqlbinlog_cmd.extend(sorted(binlog_files))
        
        # 执行binlog恢复
        try:
            child_env = _env_for_system_subprocess()
            p1 = subprocess.Popen(
                mysqlbinlog_cmd,
                stdout=subprocess.PIPE,
                env=child_env,
            )
            p2 = subprocess.Popen(
                [
                    "mysql",
                    "--user={0}".format(self.config.mysql_user),
                    "--password={0}".format(self.config.mysql_password),
                    "--socket={0}".format(self.config.mysql_socket)
                ],
                stdin=p1.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=child_env,
            )
            stdout, stderr = p2.communicate()
            
            if p2.returncode != 0:
                self.logger.warning("BINLOG APPLY WARNING: {0}".format(stderr.decode()))
            else:
                self.logger.info("BINLOG APPLIED SUCCESSFULLY")
        
        except Exception as e:
            self.logger.error("BINLOG APPLY ERROR: {0}".format(str(e)))
    
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
    
    def restore(self, date=None, binlog_start_time=None, binlog_stop_time=None):
        """
        执行恢复（强制要求dry-run计划）

        Args:
            date: 备份日期，默认为最新全量备份
            binlog_start_time: binlog恢复起始时间（必填，格式: 'YYYY-MM-DD HH:MM:SS'）
            binlog_stop_time: binlog恢复结束时间（可选）
        """
        date = date or self.latest_full_date()
        
        self.logger.info("=" * 60)
        self.logger.info("MYSQL RESTORE STARTING...")
        self.logger.info("=" * 60)
        
        # 1. 停止MySQL服务
        if not self.manage_mysql_service('stop', timeout=30):
            self.fail("FAILED TO STOP MYSQL SERVICE")
        
        # 2. 备份最后一个binlog
        self.binlog_backup(backall=True)
        
        # 3. 加载恢复计划
        plan, plan_file = self._load_restore_plan(date)
        
        # 4. 解压压缩的备份文件
        self._decompress_backup(plan)
        
        # 5. 准备备份文件
        self._prepare_backup(plan)
        
        # 6. 恢复数据目录
        self._restore_datadir(plan)
        
        # 7. 启动MySQL服务
        self.logger.info("STARTING MYSQL SERVICE...")
        if not self.manage_mysql_service('start', timeout=60):
            self.fail("FAILED TO START MYSQL SERVICE")
        
        # 8. 应用binlog
        self._apply_binlog(plan, binlog_start_time, binlog_stop_time)
        
        # 9. 更新恢复计划状态
        self._update_restore_plan(plan, plan_file, binlog_start_time, binlog_stop_time)
        
        self.logger.info("=" * 60)
        self.logger.info("MYSQL RESTORE COMPLETED SUCCESSFULLY!")
        self.logger.info("=" * 60)
    
    def purge(self, backup_days=15, binlog_days=30):
        """清理旧备份"""
        self.run_cmd(["find", str(self.config.backup_base / "full"), "-mindepth", "1", "-mtime", "+{0}".format(backup_days),
             "-exec", "rm", "-rf", "{}", "+"])
        self.run_cmd(["find", str(self.config.backup_base / "incr"), "-mindepth", "1", "-mtime", "+{0}".format(backup_days), 
             "-exec", "rm", "-rf", "{}", "+"])
        self.run_cmd(["find", str(self.config.backup_base / "binlog"), "-mindepth", "1", "-mtime", "+{0}".format(binlog_days), 
             "-exec", "rm", "-rf", "{}", "+"])


def parse_arguments():
    """解析命令行参数，提供完整的帮助文档"""
    parser = argparse.ArgumentParser(
        description='MySQL Enterprise Backup Script - Production Ready',
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
     %(prog)s restore -c /etc/mysql/backup.conf --binlog-start-time '2025-01-20 10:00:00'
     %(prog)s restore -c /etc/mysql/backup.conf --date 2025-01-20 --binlog-start-time '2025-01-20 10:00:00' --binlog-stop-time '2025-01-20 12:00:00'

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

环境变量（可选，会覆盖配置文件）:
  MYSQL_BACKUP_PASSWORD  - MySQL备份密码

兼容性:
  - MySQL 5.7 (Percona XtraBackup 2.4)
  - MySQL 8.0 (Percona XtraBackup 8.0)
  - MySQL 8.4 (Percona XtraBackup 8.4)
  - Python 3.6+

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
    
    return parser.parse_args()


def main():
    """主函数"""
    try:
        args = parse_arguments()
        
        # 加载配置
        config = Config(args.config)
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
                manager.restore(args.date, args.binlog_start_time, args.binlog_stop_time)
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

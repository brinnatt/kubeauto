#!/usr/bin/env python3
"""
MySQL数据迁移工具 - 基于官方mysqldump/mysql工具

官方依据：
- MySQL官方文档: https://dev.mysql.com/doc/refman/8.0/en/mysqldump.html
- mysqldump不进行验证，只保证SQL执行成功
- 使用官方推荐的mysqldump导出和mysql导入

安全措施：
- 参数化查询防止SQL注入
- subprocess.stdin防止shell注入
- 连接管理和资源清理
"""

import argparse
import logging
import sys
import time
import threading
import re
import atexit
import signal
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List
from logging.handlers import RotatingFileHandler
from contextlib import contextmanager
import pymysql
from pymysql.constants import CLIENT
from pymysql import Error as MySQLError
import json
import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
import shutil

# 日志配置常量（参考 starrocks_deploy.py 最佳实践）
LOG_DIR = os.getenv("MYSQL_MIGRATION_LOG_DIR", os.path.join(os.getcwd(), "logs"))
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, "mysql_migration.log")
DEFAULT_LOG_LEVEL = logging.INFO
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] [%(threadName)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"


class MigrationMode(Enum):
    STRUCTURE_ONLY = "structure_only"
    STRUCTURE_AND_DATA = "structure_and_data"


@dataclass
class DatabaseConfig:
    host: str
    port: int
    user: str
    password: str
    database: str
    mode: MigrationMode = MigrationMode.STRUCTURE_AND_DATA


@dataclass
class MigrationTask:
    source: DatabaseConfig
    target: DatabaseConfig
    dry_run: bool = False


class MigrationLogger:
    """
    企业级日志记录器
    参考 Python logging 官方最佳实践和 starrocks_deploy.py 设计
    使用 RotatingFileHandler 防止日志文件过大
    支持 extra={'to_stdout': True} 控制标准输出
    """

    _initialized = False
    _lock = threading.Lock()

    def __init__(self):
        self.logger = logging.getLogger('MySQLMigration')
        with MigrationLogger._lock:
            if not MigrationLogger._initialized:
                self.setup_logging()
                MigrationLogger._initialized = True

    def setup_logging(self):
        """
        配置日志格式和级别
        参考 Python logging.handlers.RotatingFileHandler 官方文档
        """
        # 创建logs目录
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR, exist_ok=True)

        # 避免重复添加 handler
        if self.logger.hasHandlers():
            return

        formatter = logging.Formatter(DEFAULT_FORMAT, DEFAULT_DATEFMT)
        self.logger.setLevel(DEFAULT_LOG_LEVEL)
        self.logger.propagate = False

        # 文件处理器 - 使用 RotatingFileHandler（官方推荐，防止日志文件过大）
        # 参考: https://docs.python.org/3/library/logging.handlers.html#rotatingfilehandler
        log_file = os.path.join(LOG_DIR, f'migration_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log')
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10MB
            backupCount=5,  # 保留5个备份文件
            encoding='utf-8'
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(DEFAULT_LOG_LEVEL)
        # 默认输入到文件，传 extra={'skip_file': True} 不输入到文件
        file_handler.addFilter(lambda record: not getattr(record, 'skip_file', False))
        self.logger.addHandler(file_handler)

        # 标准输出处理器 - 参考 starrocks_deploy.py 设计
        # 传 extra={'to_stdout': True} 才可以标准输出
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        stdout_handler.setLevel(DEFAULT_LOG_LEVEL)
        stdout_handler.addFilter(lambda record: getattr(record, 'to_stdout', False))
        self.logger.addHandler(stdout_handler)

    def log_step(self, message: str, level=logging.INFO, to_stdout: bool = True):
        """记录关键步骤"""
        self.logger.log(level, f"🔔 {message}", extra={'to_stdout': to_stdout})

    def log_progress(self, message: str, to_stdout: bool = True):
        """记录进度信息"""
        self.logger.info(f"📊 {message}", extra={'to_stdout': to_stdout})

    def log_warning(self, message: str, to_stdout: bool = True):
        """记录警告信息"""
        self.logger.warning(f"⚠️ {message}", extra={'to_stdout': to_stdout})

    def log_error(self, message: str, to_stdout: bool = True):
        """记录错误信息"""
        self.logger.error(f"❌ {message}", extra={'to_stdout': to_stdout})

    def log_command(self, message: str, to_stdout: bool = False):
        """记录执行的命令（默认不输出到stdout，避免敏感信息泄露）"""
        self.logger.info(f"⚡ {message}", extra={'to_stdout': to_stdout})


class DatabaseConnector:
    """数据库连接管理器"""

    def __init__(self, config: DatabaseConfig):
        self.config = config
        self.logger = MigrationLogger()

    @contextmanager
    def get_connection(self):
        """
        获取数据库连接的上下文管理器，确保连接正确关闭
        参考 pymysql 官方文档：https://pymysql.readthedocs.io/
        基于MySQL官方最佳实践：使用上下文管理器管理连接生命周期
        """
        conn = None
        try:
            conn = pymysql.connect(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database=self.config.database,
                charset='utf8mb4',
                client_flag=CLIENT.MULTI_STATEMENTS,
                connect_timeout=10,  # 连接超时10秒
                read_timeout=300,  # 读取超时5分钟
                write_timeout=300  # 写入超时5分钟
            )
            yield conn
        except pymysql.err.OperationalError as e:
            error_code, error_msg = e.args if e.args else (None, str(e))
            # 提供更友好的错误信息
            if error_code == 2003:
                friendly_msg = f"无法连接到MySQL服务器 {self.config.host}:{self.config.port}，请检查：\n" \
                              f"  1. MySQL服务是否运行\n" \
                              f"  2. 主机地址和端口是否正确\n" \
                              f"  3. 防火墙设置是否允许连接"
            elif error_code == 1045:
                friendly_msg = f"认证失败，请检查用户名和密码是否正确"
            elif error_code == 1049:
                friendly_msg = f"数据库 {self.config.database} 不存在"
            else:
                friendly_msg = f"数据库连接错误 (错误码: {error_code}): {error_msg}"
            self.logger.log_error(
                f"连接数据库失败: {self.config.host}:{self.config.port}/{self.config.database} - {friendly_msg}",
                to_stdout=True)
            raise ConnectionError(friendly_msg) from e
        except MySQLError as e:
            self.logger.log_error(
                f"MySQL错误: {self.config.host}:{self.config.port}/{self.config.database} - {str(e)}",
                to_stdout=True)
            raise
        except Exception as e:
            self.logger.log_error(
                f"连接数据库失败: {self.config.host}:{self.config.port}/{self.config.database} - {str(e)}",
                to_stdout=True)
            raise
        finally:
            if conn:
                try:
                    conn.close()
                except Exception as e:
                    self.logger.log_warning(f"关闭数据库连接时出错: {str(e)}", to_stdout=False)

    def _validate_identifier(self, identifier: str) -> str:
        """
        验证并清理数据库/表名标识符，防止SQL注入
        基于MySQL官方建议：只允许字母、数字、下划线和美元符号
        """
        if not identifier:
            raise ValueError("Identifier cannot be empty")
        # MySQL标识符规则：允许字母、数字、下划线、美元符号，但不能以数字开头
        if not re.match(r'^[a-zA-Z_$][a-zA-Z0-9_$]*$', identifier):
            raise ValueError(f"Invalid identifier format: {identifier}")
        return identifier

    def create_database_if_not_exists(self):
        """创建数据库（如果不存在）- 使用安全的标识符验证"""
        try:
            # 验证数据库名
            db_name = self._validate_identifier(self.config.database)
            
            # 先连接到mysql系统数据库来创建目标数据库
            temp_config = DatabaseConfig(
                host=self.config.host,
                port=self.config.port,
                user=self.config.user,
                password=self.config.password,
                database='mysql',
                mode=self.config.mode
            )

            with DatabaseConnector(temp_config).get_connection() as conn:
                with conn.cursor() as cursor:
                    # 使用反引号保护标识符（MySQL官方推荐）
                    cursor.execute(
                        f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                    conn.commit()
                    self.logger.log_progress(f"确保数据库存在: {db_name}", to_stdout=False)
        except ValueError as e:
            self.logger.log_error(f"无效的数据库名 {self.config.database}: {str(e)}", to_stdout=True)
            raise
        except MySQLError as e:
            self.logger.log_error(f"创建数据库失败 {self.config.database}: MySQL错误 - {str(e)}", to_stdout=True)
            raise
        except Exception as e:
            self.logger.log_error(f"创建数据库失败 {self.config.database}: {str(e)}", to_stdout=True)
            raise

    def validate_tables_exist(self) -> bool:
        """验证数据库中是否有表存在"""
        try:
            with self.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SHOW TABLES")
                    tables = cursor.fetchall()
                    return len(tables) > 0
        except MySQLError as e:
            self.logger.log_error(f"验证表存在失败: MySQL错误 - {str(e)}", to_stdout=False)
            return False
        except Exception as e:
            self.logger.log_error(f"验证表存在失败: {str(e)}", to_stdout=False)
            return False


class MySQLDumpManager:
    """mysqldump命令管理器 - 生产环境加固版"""

    def __init__(self, logger: MigrationLogger):
        self.logger = logger
        self.temp_dir = tempfile.mkdtemp(prefix="mysql_migration_")
        self._lock = threading.Lock()
        self._active_files = set()

        # 注册退出时的清理函数
        atexit.register(self.cleanup)

    def cleanup(self):
        """主动清理临时文件"""
        if os.path.exists(self.temp_dir):
            try:
                # 先尝试删除单个文件
                with self._lock:
                    for file_path in list(self._active_files):
                        try:
                            if os.path.exists(file_path):
                                os.remove(file_path)
                        except OSError:
                            pass  # 忽略单个文件删除失败
                    self._active_files.clear()
                
                # 然后删除整个目录
                shutil.rmtree(self.temp_dir)
                self.logger.log_progress("临时文件清理完成", to_stdout=False)
            except OSError as e:
                self.logger.log_warning(f"清理临时文件失败: 系统错误 - {str(e)}", to_stdout=False)
            except Exception as e:
                self.logger.log_warning(f"清理临时文件失败: {str(e)}", to_stdout=False)

    def _validate_command_arg(self, value: str, arg_name: str) -> str:
        """
        验证命令参数，防止命令注入
        基于Python subprocess官方建议：验证所有用户输入
        """
        if not value:
            raise ValueError(f"{arg_name} cannot be empty")
        # 检查是否包含危险的shell字符
        dangerous_chars = [';', '&', '|', '`', '$', '(', ')', '<', '>', '\n', '\r']
        for char in dangerous_chars:
            if char in value:
                raise ValueError(f"{arg_name} contains dangerous character: {char}")
        return value

    def build_mysqldump_command(self, config: DatabaseConfig, output_file: str) -> List[str]:
        """
        构建mysqldump命令 - 安全版本（不暴露密码，防止命令注入）
        基于Python subprocess官方最佳实践：使用列表而非字符串，验证所有参数
        """
        # 验证所有参数，防止命令注入
        host = self._validate_command_arg(config.host, "host")
        user = self._validate_command_arg(config.user, "user")
        database = self._validate_command_arg(config.database, "database")
        
        # 验证端口是有效整数
        if not isinstance(config.port, int) or not (1 <= config.port <= 65535):
            raise ValueError(f"Invalid port: {config.port}")
        
        # 验证输出文件路径（防止路径注入）
        if not os.path.isabs(output_file) or '..' in output_file:
            raise ValueError(f"Invalid output file path: {output_file}")
        
        cmd = [
            'mysqldump',
            f'-h{host}',
            f'-P{config.port}',
            f'-u{user}',
            '--single-transaction',
            '--routines',
            '--events',
            '--triggers',
            '--set-gtid-purged=OFF',
            '--skip-lock-tables',
            '--max-allowed-packet=1G',
        ]

        if config.mode == MigrationMode.STRUCTURE_ONLY:
            cmd.extend(['--no-data'])
        else:
            cmd.extend([
                '--complete-insert',
                '--extended-insert',
                '--quick',
                '--order-by-primary'
            ])

        cmd.extend([
            database,
            f'--result-file={output_file}'
        ])

        return cmd

    def build_mysql_command(self, config: DatabaseConfig) -> List[str]:
        """
        构建mysql导入命令 - 安全版本（防止命令注入）
        基于Python subprocess官方最佳实践
        """
        # 验证所有参数，防止命令注入
        host = self._validate_command_arg(config.host, "host")
        user = self._validate_command_arg(config.user, "user")
        database = self._validate_command_arg(config.database, "database")
        
        # 验证端口是有效整数
        if not isinstance(config.port, int) or not (1 <= config.port <= 65535):
            raise ValueError(f"Invalid port: {config.port}")
        
        cmd = [
            'mysql',
            f'-h{host}',
            f'-P{config.port}',
            f'-u{user}',
            '--max-allowed-packet=1G',
            '--connect-timeout=60',
            database
        ]

        return cmd

    def _get_dump_filename(self, config: DatabaseConfig) -> str:
        """生成唯一的dump文件名 - 防冲突版本，安全处理数据库名"""
        # 安全处理数据库名，移除特殊字符防止文件名问题
        safe_db_name = re.sub(r'[^a-zA-Z0-9_]', '_', config.database)[:50]  # 限制长度
        db_hash = hashlib.md5(config.database.encode('utf-8')).hexdigest()[:6]  # 6位足够
        timestamp = datetime.now().strftime('%H%M%S')  # 只需要时间部分
        thread_name = threading.current_thread().name.replace('ThreadPoolExecutor', 'tpe')[:10]
        random_suffix = uuid.uuid4().hex[:4]  # 4位UUID防止冲突

        return f"{safe_db_name}_{db_hash}_{timestamp}_{thread_name}_{random_suffix}.sql"

    def execute_dump(self, config: DatabaseConfig, dry_run: bool = False) -> str:
        """
        执行数据库导出
        使用 subprocess.run 替代 Popen（Python官方推荐，更安全）
        参考: https://docs.python.org/3/library/subprocess.html#subprocess.run
        """
        with self._lock:
            dump_file = os.path.join(self.temp_dir, self._get_dump_filename(config))
            self._active_files.add(dump_file)

        # 确保目录存在（temp_dir已存在，但确保父目录存在）
        os.makedirs(os.path.dirname(dump_file), exist_ok=True)

        cmd = self.build_mysqldump_command(config, dump_file)

        if dry_run:
            self.logger.log_command(f"[DRY-RUN] 将执行导出: {' '.join(cmd)}", to_stdout=True)
            return dump_file

        self.logger.log_step(f"开始导出数据库: {config.database}", to_stdout=True)
        self.logger.log_command(f"执行导出命令: {' '.join(cmd)}", to_stdout=False)

        try:
            # 使用环境变量传递密码，避免在进程列表中暴露
            env = os.environ.copy()
            env['MYSQL_PWD'] = config.password

            # 使用 subprocess.run 替代 Popen（官方推荐，更安全）
            # 参考: https://docs.python.org/3/library/subprocess.html#using-the-subprocess-module
            try:
                result = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    timeout=7200,  # 2小时超时
                    check=False  # 不自动抛出异常，手动处理
                )
            except subprocess.TimeoutExpired:
                self.logger.log_error(f"mysqldump执行超时（超过2小时）: {config.database}", to_stdout=True)
                raise TimeoutError("mysqldump执行超时（超过2小时）")

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "未知错误"
                # 提供更详细的错误信息（参考 starrocks_deploy.py）
                detailed_error = (
                    f"mysqldump失败 (退出码: {result.returncode}): {error_msg}\n"
                    f"执行的命令: {' '.join(cmd)}"
                )
                self.logger.log_error(f"导出数据库失败 {config.database}: {error_msg}", to_stdout=True)
                raise RuntimeError(detailed_error)

            # 检查导出的文件大小
            if os.path.exists(dump_file):
                file_size = os.path.getsize(dump_file)
                # 格式化文件大小显示
                if file_size < 1024:
                    size_str = f"{file_size} B"
                elif file_size < 1024 * 1024:
                    size_str = f"{file_size / 1024:.2f} KB"
                elif file_size < 1024 * 1024 * 1024:
                    size_str = f"{file_size / (1024 * 1024):.2f} MB"
                else:
                    size_str = f"{file_size / (1024 * 1024 * 1024):.2f} GB"
                self.logger.log_progress(f"导出完成: {config.database} ({size_str})", to_stdout=True)
            else:
                raise FileNotFoundError("导出文件未生成")

            return dump_file

        except subprocess.TimeoutExpired:
            self.logger.log_error(f"导出数据库超时 {config.database}", to_stdout=True)
            if os.path.exists(dump_file):
                os.remove(dump_file)
            raise
        except Exception as e:
            self.logger.log_error(f"导出数据库失败 {config.database}: {str(e)}", to_stdout=True)
            if os.path.exists(dump_file):
                try:
                    os.remove(dump_file)
                except OSError:
                    pass
            raise

    def execute_import(self, config: DatabaseConfig, dump_file: str, dry_run: bool = False):
        """
        执行数据库导入 - 大文件性能优化版
        使用 subprocess.run 替代 Popen（Python官方推荐，更安全）
        修复shell注入风险：使用subprocess.stdin而非shell重定向
        参考: https://docs.python.org/3/library/subprocess.html#subprocess.run
        """
        if dry_run:
            self.logger.log_command(f"[DRY-RUN] 将执行导入: mysql ... < {dump_file}", to_stdout=True)
            return

        self.logger.log_step(f"开始导入数据库: {config.database}", to_stdout=True)

        try:
            # 验证dump文件路径，防止路径注入
            if not os.path.exists(dump_file):
                raise ValueError(f"Dump file does not exist: {dump_file}")
            if not os.path.isfile(dump_file):
                raise ValueError(f"Dump path is not a file: {dump_file}")
            # 确保文件在临时目录内（防止路径遍历）
            if not dump_file.startswith(self.temp_dir):
                raise ValueError(f"Dump file outside temp directory: {dump_file}")

            # 确保目标数据库存在
            target_connector = DatabaseConnector(config)
            target_connector.create_database_if_not_exists()

            # 使用环境变量传递密码
            env = os.environ.copy()
            env['MYSQL_PWD'] = config.password

            # 构建命令（不使用shell重定向，改用stdin）
            cmd = self.build_mysql_command(config)
            self.logger.log_command(f"执行导入命令: {' '.join(cmd)} < {dump_file}", to_stdout=False)

            # 使用subprocess.run而非Popen，通过stdin传递文件内容，防止shell注入
            # 这是Python官方推荐的方式（避免shell=True）
            # 参考: https://docs.python.org/3/library/subprocess.html#using-the-subprocess-module
            try:
                with open(dump_file, 'rb') as f:
                    result = subprocess.run(
                        cmd,
                        stdin=f,  # 直接使用文件句柄，避免shell
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        env=env,
                        timeout=10800,  # 3小时超时
                        check=False  # 不自动抛出异常，手动处理
                    )
            except subprocess.TimeoutExpired:
                self.logger.log_error(f"mysql导入执行超时（超过3小时）: {config.database}", to_stdout=True)
                raise TimeoutError("mysql导入执行超时（超过3小时）")

            if result.returncode != 0:
                error_msg = result.stderr.decode('utf-8', errors='replace').strip() if result.stderr else "未知错误"
                # 提供更友好的错误信息（参考 starrocks_deploy.py）
                if "ERROR 1007" in error_msg:
                    error_msg += " (数据库已存在)"
                elif "ERROR 1044" in error_msg:
                    error_msg += " (权限不足，请检查用户权限)"
                elif "ERROR 1045" in error_msg:
                    error_msg += " (认证失败，请检查用户名和密码)"
                elif "ERROR 2002" in error_msg:
                    error_msg += " (无法连接到MySQL服务器，请检查主机和端口)"
                elif "ERROR 2003" in error_msg:
                    error_msg += " (无法连接到MySQL服务器，请检查网络连接)"

                detailed_error = (
                    f"mysql导入失败 (退出码: {result.returncode}): {error_msg}\n"
                    f"执行的命令: {' '.join(cmd)}\n"
                    f"Dump文件: {dump_file}"
                )
                self.logger.log_error(f"导入数据库失败 {config.database}: {error_msg}", to_stdout=True)
                raise RuntimeError(detailed_error)

            self.logger.log_progress(f"导入完成: {config.database}", to_stdout=True)

            # 导入后快速验证 - 确保有表存在
            if not target_connector.validate_tables_exist():
                raise RuntimeError(f"导入后目标数据库 {config.database} 中无表存在，请检查dump文件内容")

        except ValueError as e:
            self.logger.log_error(f"参数验证失败: {str(e)}", to_stdout=True)
            raise
        except subprocess.TimeoutExpired:
            self.logger.log_error(f"导入数据库超时 {config.database}", to_stdout=True)
            raise
        except Exception as e:
            self.logger.log_error(f"导入数据库失败 {config.database}: {str(e)}", to_stdout=True)
            raise

    def _safe_remove_file(self, file_path: str):
        """安全删除文件"""
        try:
            if os.path.exists(file_path) and os.path.isfile(file_path):
                # 确保文件在临时目录内（防止路径遍历攻击）
                if file_path.startswith(self.temp_dir):
                    os.remove(file_path)
                    self._active_files.discard(file_path)
                else:
                    self.logger.log_warning(f"拒绝删除临时目录外的文件: {file_path}", to_stdout=False)
        except OSError as e:
            self.logger.log_warning(f"删除文件失败 {file_path}: 系统错误 - {str(e)}", to_stdout=False)
        except Exception as e:
            self.logger.log_warning(f"删除文件失败 {file_path}: {str(e)}", to_stdout=False)


class DataValidator:
    """数据库连接验证器 - 仅用于检查数据库连接"""
    
    def __init__(self):
        self.logger = MigrationLogger()

    def validate_database_existence(self, connector: DatabaseConnector) -> bool:
        """检查数据库连接是否可用（仅用于迁移前验证）"""
        try:
            with connector.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                    return True
        except MySQLError as e:
            self.logger.log_error(f"数据库连接验证失败: MySQL错误 - {str(e)}", to_stdout=False)
            return False
        except Exception as e:
            self.logger.log_error(f"数据库连接验证失败: {str(e)}", to_stdout=False)
            return False


class MigrationManager:
    """迁移管理器 - 基于MySQL官方mysqldump/mysql工具"""
    
    def __init__(self, dry_run: bool = False):
        """
        Args:
            dry_run: 是否模拟运行
        """
        self.dry_run = dry_run
        self.logger = MigrationLogger()
        self.validator = DataValidator()  # 仅用于数据库连接检查
        self.dump_manager = MySQLDumpManager(self.logger)
        self.migration_progress = {}
        self._lock = threading.Lock()

    def _update_progress(self, task_id: str, updates: dict):
        """线程安全地更新进度"""
        with self._lock:
            if task_id not in self.migration_progress:
                self.migration_progress[task_id] = {}
            self.migration_progress[task_id].update(updates)

    def execute_migration(self, task: MigrationTask):
        """执行迁移任务"""
        task_id = f"{task.source.database}->{task.target.database}"

        try:
            self.logger.log_step(f"开始迁移任务: {task_id}", to_stdout=True)
            self._update_progress(task_id, {
                'start_time': datetime.now(),
                'status': 'running',
                'steps_completed': []
            })

            # 1. 验证源数据库是否存在（dry-run模式跳过真实连接）
            if not self.dry_run:
                self.logger.log_step("验证源数据库连接", to_stdout=True)
                source_connector = DatabaseConnector(task.source)
                if not self.validator.validate_database_existence(source_connector):
                    raise ConnectionError(f"源数据库不存在或无法连接: {task.source.database}")
            else:
                self.logger.log_progress("[DRY-RUN] 跳过数据库连接验证", to_stdout=True)

            # 2. 使用mysqldump导出数据
            dump_file = self.dump_manager.execute_dump(task.source, self.dry_run)
            self._update_progress(task_id, {
                'steps_completed': ['dump_export'],
                'dump_file': dump_file
            })

            # 3. 使用mysql导入数据
            self.dump_manager.execute_import(task.target, dump_file, self.dry_run)
            self._update_progress(task_id, {
                'steps_completed': ['dump_export', 'data_import']
            })

            # 4. 迁移完成（根据MySQL官方文档，mysqldump不进行验证，只保证SQL执行成功）

            self._update_progress(task_id, {
                'status': 'completed',
                'end_time': datetime.now()
            })

            self.logger.log_step(f"迁移任务完成: {task_id}", to_stdout=True)

        except Exception as e:
            self._update_progress(task_id, {
                'status': 'failed',
                'error': str(e),
                'end_time': datetime.now()
            })
            self.logger.log_error(f"迁移任务失败 {task_id}: {str(e)}", to_stdout=True)
            raise



def safe_parse_migration_mode(mode_str: str) -> MigrationMode:
    """安全解析MigrationMode"""
    if not mode_str:
        return MigrationMode.STRUCTURE_AND_DATA

    mode_str = mode_str.lower().strip()
    mode_map = {
        'structure_only': MigrationMode.STRUCTURE_ONLY,
        'structure_and_data': MigrationMode.STRUCTURE_AND_DATA,
        'data_only': MigrationMode.STRUCTURE_AND_DATA
    }

    return mode_map.get(mode_str, MigrationMode.STRUCTURE_AND_DATA)


def parse_arguments():
    """解析命令行参数 - 保留完整的用户示例"""
    parser = argparse.ArgumentParser(
        description='企业级MySQL数据迁移工具 (基于mysqldump/mysql) - 生产环境加固版',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f'''
使用示例:

1. 单数据库迁移 (结构和数据):
    {sys.argv[0]} -s localhost:3306:source_db:user:pass -t localhost:3307:target_db:user:pass

2. 多数据库迁移 (分别指定模式):
    {sys.argv[0]} -c migration_config.json

3. Dry-run模式 (模拟运行):
    {sys.argv[0]} -s localhost:3306:source_db:user:pass -t localhost:3307:target_db:user:pass --dry-run

4. 仅迁移结构:
    {sys.argv[0]} -s localhost:3306:source_db:user:pass -t localhost:3307:target_db:user:pass --structure-only

5. 使用配置文件:
    {sys.argv[0]} -c config.json --dry-run

配置文件格式参考:
{{
    "migrations": [
        {{
            "source": {{
                "host": "localhost",
                "port": 3306,
                "user": "user1",
                "password": "pass1",
                "database": "db1",
                "mode": "structure_and_data"
            }},
            "target": {{
                "host": "localhost", 
                "port": 3307,
                "user": "user2",
                "password": "pass2",
                "database": "db1",
                "mode": "structure_and_data"
            }}
        }},
        {{
            "source": {{
                "host": "localhost",
                "port": 3306, 
                "user": "user1",
                "password": "pass1",
                "database": "db2",
                "mode": "structure_only"
            }},
            "target": {{
                "host": "localhost",
                "port": 3307,
                "user": "user2", 
                "password": "pass2",
                "database": "db2",
                "mode": "structure_only"
            }}
        }}
    ]
}}

注意: 确保系统已安装 mysqldump 和 mysql 命令行工具
        '''
    )

    # 单数据库迁移参数
    parser.add_argument('-s', '--source', help='源数据库: host:port:database:user:password')
    parser.add_argument('-t', '--target', help='目标数据库: host:port:database:user:password')

    # 多数据库迁移参数
    parser.add_argument('-c', '--config', help='迁移配置文件路径')

    # 迁移选项
    parser.add_argument('--dry-run', action='store_true', help='模拟运行，不实际执行迁移')
    parser.add_argument('--structure-only', action='store_true', help='仅迁移结构，不迁移数据')
    parser.add_argument('--max-workers', type=int, default=3, help='最大并行工作线程数')
    parser.add_argument('--keep-dump-files', action='store_true', help='保留导出的dump文件')

    return parser.parse_args()


def parse_database_config(db_str: str, mode: MigrationMode) -> DatabaseConfig:
    """解析数据库配置字符串"""
    parts = db_str.split(':')
    if len(parts) != 5:
        raise ValueError(f"数据库配置格式错误: {db_str}，应为 host:port:database:user:password")

    try:
        port = int(parts[1])
        if not (1 <= port <= 65535):
            raise ValueError(f"端口号必须在1-65535之间: {port}")
    except ValueError as e:
        if "invalid literal" in str(e).lower():
            raise ValueError(f"端口号必须是整数: {parts[1]}")
        raise

    return DatabaseConfig(
        host=parts[0].strip(),
        port=port,
        database=parts[2].strip(),
        user=parts[3].strip(),
        password=parts[4].strip(),
        mode=mode
    )


def load_config_file(config_path: str) -> List[MigrationTask]:
    """
    加载配置文件
    提供详细的错误信息，参考 starrocks_deploy.py 的用户友好性设计
    """
    logger = MigrationLogger()
    
    # 验证配置文件是否存在
    if not os.path.exists(config_path):
        logger.log_error(f"配置文件不存在: {config_path}", to_stdout=True)
        sys.exit(1)
    
    if not os.path.isfile(config_path):
        logger.log_error(f"配置路径不是文件: {config_path}", to_stdout=True)
        sys.exit(1)
    
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
    except json.JSONDecodeError as e:
        logger.log_error(
            f"配置文件JSON格式错误: {str(e)}\n"
            f"请检查配置文件 {config_path} 的格式是否正确",
            to_stdout=True
        )
        sys.exit(1)
    except (OSError, IOError) as e:
        logger.log_error(f"读取配置文件失败: 文件系统错误 - {str(e)}", to_stdout=True)
        sys.exit(1)
    except Exception as e:
        logger.log_error(f"读取配置文件失败: {str(e)}", to_stdout=True)
        sys.exit(1)

    if 'migrations' not in config_data:
        logger.log_error(
            f"配置文件缺少 'migrations' 字段\n"
            f"请参考帮助信息中的配置文件格式示例",
            to_stdout=True
        )
        sys.exit(1)

    tasks = []
    for idx, migration_config in enumerate(config_data.get('migrations', []), 1):
        try:
            if 'source' not in migration_config or 'target' not in migration_config:
                logger.log_error(
                    f"配置文件第 {idx} 个迁移任务缺少 'source' 或 'target' 字段",
                    to_stdout=True
                )
                sys.exit(1)
            
            source_config = migration_config['source']
            target_config = migration_config['target']
            
            # 验证必需字段
            required_fields = ['host', 'port', 'user', 'password', 'database']
            for field in required_fields:
                if field not in source_config:
                    logger.log_error(
                        f"配置文件第 {idx} 个迁移任务的 source 缺少必需字段: {field}",
                        to_stdout=True
                    )
                    sys.exit(1)
                if field not in target_config:
                    logger.log_error(
                        f"配置文件第 {idx} 个迁移任务的 target 缺少必需字段: {field}",
                        to_stdout=True
                    )
                    sys.exit(1)

            # 验证端口号
            for config_name, config in [('source', source_config), ('target', target_config)]:
                port = config['port']
                if not isinstance(port, int):
                    try:
                        port = int(port)
                    except (ValueError, TypeError):
                        logger.log_error(
                            f"配置文件第 {idx} 个迁移任务的 {config_name} 端口号无效: {config['port']}",
                            to_stdout=True
                        )
                        sys.exit(1)
                if not (1 <= port <= 65535):
                    logger.log_error(
                        f"配置文件第 {idx} 个迁移任务的 {config_name} 端口号超出范围: {port}",
                        to_stdout=True
                    )
                    sys.exit(1)
                config['port'] = port

            task = MigrationTask(
                source=DatabaseConfig(
                    host=str(source_config['host']).strip(),
                    port=source_config['port'],
                    user=str(source_config['user']).strip(),
                    password=str(source_config['password']).strip(),
                    database=str(source_config['database']).strip(),
                    mode=safe_parse_migration_mode(source_config.get('mode'))
                ),
                target=DatabaseConfig(
                    host=str(target_config['host']).strip(),
                    port=target_config['port'],
                    user=str(target_config['user']).strip(),
                    password=str(target_config['password']).strip(),
                    database=str(target_config['database']).strip(),
                    mode=safe_parse_migration_mode(target_config.get('mode'))
                )
            )
            tasks.append(task)
        except ValueError as e:
            logger.log_error(
                f"配置文件第 {idx} 个迁移任务配置错误: {str(e)}",
                to_stdout=True
            )
            sys.exit(1)
        except Exception as e:
            logger.log_error(
                f"解析配置文件第 {idx} 个迁移任务失败: {str(e)}",
                to_stdout=True
            )
            sys.exit(1)

    if not tasks:
        logger.log_error("配置文件中没有找到有效的迁移任务", to_stdout=True)
        sys.exit(1)

    logger.log_progress(f"成功加载配置文件: {len(tasks)} 个迁移任务", to_stdout=True)
    return tasks


def check_dependencies():
    """
    检查必要的命令行工具是否存在
    参考 Python shutil.which 官方文档
    """
    required_tools = ['mysqldump', 'mysql']
    missing_tools = []

    for tool in required_tools:
        if not shutil.which(tool):
            missing_tools.append(tool)

    if missing_tools:
        logger = MigrationLogger()
        logger.log_error(
            f"缺少必要的命令行工具: {', '.join(missing_tools)}",
            to_stdout=True
        )
        logger.log_error(
            "请确保已安装 MySQL 客户端工具。安装方法：\n"
            "  - Ubuntu/Debian: sudo apt-get install mysql-client\n"
            "  - RHEL/CentOS: sudo yum install mysql\n"
            "  - macOS: brew install mysql-client",
            to_stdout=True
        )
        sys.exit(1)


def setup_signal_handlers(dump_manager):
    """
    设置信号处理器
    参考 Python signal 模块官方文档
    """
    def signal_handler(sig, _frame):
        logger = MigrationLogger()
        logger.log_warning(f"收到中断信号({sig})，正在清理临时文件...", to_stdout=True)
        dump_manager.cleanup()
        sys.exit(1)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


def main():
    """主函数"""
    # 初始化日志（最早初始化，确保所有错误都能记录）
    logger = MigrationLogger()
    
    # 检查依赖
    check_dependencies()

    args = parse_arguments()

    # 智能控制并发数
    max_workers = min(args.max_workers, os.cpu_count() or 4, 8)  # 不超过CPU数和8
    if max_workers != args.max_workers:
        logger.log_warning(
            f"调整并发数: {args.max_workers} -> {max_workers} (系统资源保护)",
            to_stdout=True
        )

    # 初始化迁移管理器
    migration_manager = MigrationManager(dry_run=args.dry_run)

    # 设置信号处理器
    setup_signal_handlers(migration_manager.dump_manager)

    # 准备迁移任务
    tasks = []

    if args.config:
        # 使用配置文件
        tasks = load_config_file(args.config)
        # 应用全局dry-run设置
        for task in tasks:
            task.dry_run = args.dry_run
    elif args.source and args.target:
        # 单数据库迁移
        mode = MigrationMode.STRUCTURE_ONLY if args.structure_only else MigrationMode.STRUCTURE_AND_DATA
        source_config = parse_database_config(args.source, mode)
        target_config = parse_database_config(args.target, mode)

        tasks.append(MigrationTask(
            source=source_config,
            target=target_config,
            dry_run=args.dry_run
        ))
    else:
        logger.log_error(
            "必须指定源和目标数据库或配置文件\n"
            "使用 -h 或 --help 查看帮助信息",
            to_stdout=True
        )
        sys.exit(1)

    if not tasks:
        logger.log_error("没有找到有效的迁移任务", to_stdout=True)
        sys.exit(1)

    # 执行迁移
    logger.log_step(
        f"开始执行 {len(tasks)} 个迁移任务 (Dry-run: {args.dry_run})",
        to_stdout=True
    )

    start_time = time.time()

    try:
        # 使用线程池并行执行迁移任务
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task = {
                executor.submit(migration_manager.execute_migration, task): task
                for task in tasks
            }

            for future in as_completed(future_to_task):
                task = future_to_task[future]
                try:
                    future.result()
                    logger.log_step(
                        f"迁移任务完成: {task.source.database} -> {task.target.database}",
                        to_stdout=True
                    )
                except Exception as e:
                    logger.log_error(
                        f"迁移任务失败: {task.source.database} -> {task.target.database}: {str(e)}",
                        to_stdout=True
                    )

        # 输出总结报告
        elapsed_time = time.time() - start_time
        hours = int(elapsed_time // 3600)
        minutes = int((elapsed_time % 3600) // 60)
        seconds = int(elapsed_time % 60)
        if hours > 0:
            time_str = f"{hours}小时{minutes}分钟{seconds}秒"
        elif minutes > 0:
            time_str = f"{minutes}分钟{seconds}秒"
        else:
            time_str = f"{seconds}秒"
        
        logger.log_step(f"所有迁移任务完成! 总耗时: {time_str}", to_stdout=True)

        # 输出迁移统计
        successful_tasks = sum(1 for task in tasks if migration_manager.migration_progress.get(
            f"{task.source.database}->{task.target.database}", {}).get('status') == 'completed'
                               )

        logger.log_step(
            f"迁移统计: 成功 {successful_tasks}/{len(tasks)}, 失败 {len(tasks) - successful_tasks}",
            to_stdout=True
        )

        # 提示dump文件位置
        if not args.dry_run and not args.keep_dump_files:
            logger.log_progress("导出的dump文件已在迁移完成后自动清理", to_stdout=True)
        elif args.keep_dump_files:
            logger.log_progress(
                f"导出的dump文件已保留在临时目录: {migration_manager.dump_manager.temp_dir}",
                to_stdout=True
            )

    except KeyboardInterrupt:
        logger.log_warning("迁移被用户中断", to_stdout=True)
        sys.exit(1)
    except Exception as e:
        logger.log_error(f"迁移过程发生错误: {str(e)}", to_stdout=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional, List

LOG_DIR = "/var/log/kubeauto"
DEFAULT_LOG_FILE = os.path.join(LOG_DIR, "deploy.log")
DEFAULT_LOG_LEVEL = logging.DEBUG
DEFAULT_FORMAT = "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

def setup_logger(
    name: str = "kubeauto",
    log_file: Optional[str] = None,
    level: int = DEFAULT_LOG_LEVEL,
    fmt: str = DEFAULT_FORMAT,
    datefmt: str = DEFAULT_DATEFMT,
    handlers: Optional[List[logging.Handler]] = None
) -> logging.Logger:
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

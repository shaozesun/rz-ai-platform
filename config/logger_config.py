import logging
import sys
from pathlib import Path
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from config.trace_id import trace_id_var

LOG_DIR = Path(__file__).parent.parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)


class TraceIdFilter(logging.Filter):
    """从 contextvars 注入 trace_id 到日志记录"""
    def filter(self, record):
        record.trace_id = trace_id_var.get()
        return True


def setup_logging(level='INFO'):
    """初始化全局日志配置"""
    fmt = logging.Formatter(
        '%(asctime)s | %(levelname)-5s | %(trace_id)s | '
        '%(name)s:%(lineno)d | %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )

    tf = TraceIdFilter()

    # 控制台
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG)
    console.addFilter(tf)

    # 文件 — 按天轮转
    file_handler = TimedRotatingFileHandler(
        LOG_DIR / 'app.log', when='midnight', interval=1,
        backupCount=30, encoding='utf-8',
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)
    file_handler.addFilter(tf)

    # 错误日志
    error_handler = RotatingFileHandler(
        LOG_DIR / 'error.log', maxBytes=10 * 1024 * 1024,
        backupCount=10, encoding='utf-8',
    )
    error_handler.setFormatter(fmt)
    error_handler.setLevel(logging.ERROR)
    error_handler.addFilter(tf)

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)
    root.addHandler(error_handler)

    # 抑制第三方日志
    logging.getLogger('pymongo').setLevel(logging.WARNING)
    logging.getLogger('redis').setLevel(logging.WARNING)

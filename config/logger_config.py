import glob
import logging
import os
import sys
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler
from config.trace_id import trace_id_var

LOG_DIR = Path(__file__).parent.parent / 'logs'
LOG_DIR.mkdir(exist_ok=True)


class TraceIdFilter(logging.Filter):
    """从 contextvars 注入 trace_id 到日志记录"""
    def filter(self, record):
        record.trace_id = trace_id_var.get()
        return True


class DailyFileHandler(logging.FileHandler):
    """多进程安全的日切日志处理器。

    不做文件 rename 轮转，而是每天直接写入 app.log.YYYY-MM-DD。
    多进程各自 open('a') 追加写入，O_APPEND 保证原子性，消除
    TimedRotatingFileHandler 在多 worker 下的轮转竞态。
    """

    def __init__(self, log_dir, prefix='app.log', backup_count=30, encoding='utf-8'):
        self._log_dir = Path(log_dir)
        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._prefix = prefix
        self._backup_count = backup_count
        self._today = None
        super().__init__('/dev/null', 'a', encoding=encoding, delay=True)

    def emit(self, record):
        today = time.strftime('%Y-%m-%d')
        if today != self._today:
            self._today = today
            new_path = str(self._log_dir / f'{self._prefix}.{today}')
            if self.baseFilename != new_path:
                if self.stream:
                    self.stream.close()
                    self.stream = None
                self.baseFilename = new_path
                self._cleanup()
        if not self.stream:
            self.stream = self._open()
        try:
            logging.StreamHandler.emit(self, record)
        except Exception:
            self.handleError(record)

    def _cleanup(self):
        pattern = str(self._log_dir / f'{self._prefix}.*')
        files = sorted(
            [f for f in glob.glob(pattern) if not f.endswith('.lock')],
            key=os.path.getmtime,
        )
        while len(files) > self._backup_count:
            try:
                os.remove(files.pop(0))
            except OSError:
                pass


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

    # 文件 — 按天写入，多进程安全
    file_handler = DailyFileHandler(
        LOG_DIR, prefix='app.log', backup_count=30, encoding='utf-8',
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

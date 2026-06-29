"""
RAG 链路耗时打点
"""
import logging
import time
from contextlib import contextmanager
from typing import Optional

_metrics_logger = logging.getLogger("rag.metrics")


@contextmanager
def rag_stage(stage_name: str, logger: Optional[logging.Logger] = None):
    log = logger or _metrics_logger
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        log.debug("[RAG Stage] %s took %.1fms", stage_name, elapsed_ms)

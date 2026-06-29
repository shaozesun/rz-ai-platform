"""业务审计日志"""
import logging

_audit_logger = logging.getLogger("audit")


def log_upload(user_id: str, group_id: str, filename: str, chunks: int, trace_id: str = "-") -> None:
    _audit_logger.info(
        'event=FILE_UPLOAD user_id=%s group_id=%s filename=%s chunks=%d trace_id=%s',
        user_id, group_id, filename, chunks, trace_id,
    )


def log_delete(user_id: str, group_id: str, filename: str, vectors: int, trace_id: str = "-") -> None:
    _audit_logger.info(
        'event=FILE_DELETE user_id=%s group_id=%s filename=%s vectors=%d trace_id=%s',
        user_id, group_id, filename, vectors, trace_id,
    )


def log_query(user_id: str, group_id: str, query: str, result_count: int, trace_id: str = "-") -> None:
    truncated = query[:200] if query else ""
    _audit_logger.info(
        'event=QUERY user_id=%s group_id=%s query=%s result_count=%d trace_id=%s',
        user_id, group_id, truncated, result_count, trace_id,
    )

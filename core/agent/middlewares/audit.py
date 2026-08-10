"""MongoDB 审计日志 — 记录每次 Agent Run 的关键信息"""

import json
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


async def save_agent_audit_log(
  user_id: str,
  session_id: str,
  question: str,
  answer: str,
  tool_calls: list[dict],
  tokens_used: dict,
  duration_ms: int,
  stopped_by_guard: Optional[str] = None,
):
  """写入 agent_runs 集合"""
  try:
    from config.mongodb_conn import mongodb_manager
    import asyncio

    doc = {
      'user_id': user_id,
      'session_id': session_id,
      'question': question[:500],
      'answer': answer[:2000] if answer else '',
      'tool_calls': json.dumps(tool_calls, ensure_ascii=False),
      'tokens_in': tokens_used.get('input', 0),
      'tokens_out': tokens_used.get('output', 0),
      'duration_ms': duration_ms,
      'stopped_by_guard': stopped_by_guard,
      'created_at': datetime.utcnow(),
    }

    await asyncio.to_thread(
      mongodb_manager.sync_db.agent_runs.insert_one,
      doc,
    )
  except Exception:
    logger.exception('agent audit log 写入失败')

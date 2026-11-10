"""通道通用：消费 `AgentService().stream()` 的 SSE 事件流。

各通道「怎么把回复发出去」不同（飞书节流刷卡片 / 微信缓冲终稿 / 企微逐段流式），
但「怎么读事件」一致：解析 `data:` 行，产出 `(event, data)`。非 Web 场景（IM、
定时任务）复用同一套解析。
"""

import json
from typing import AsyncIterator


def parse_sse(chunk: str) -> dict | None:
  """解析一行 SSE 文本为 dict；非 data 行或解析失败返回 None。"""
  line = chunk.strip()
  if not line.startswith('data:'):
    return None
  try:
    return json.loads(line[5:].strip())
  except Exception:
    return None


async def iter_agent_events(
  svc, *, message: str, thread_id: str, user_id: str, session_id: str, permissions,
) -> AsyncIterator[tuple[str, dict]]:
  """跑一轮 Agent，产出 `(event, data)`；event 形如 `text` / `error` / `done`。

  `svc` 为 `AgentService` 实例（不在此处构造，便于调用方复用单例）。
  """
  async for chunk in svc.stream(
    message=message,
    thread_id=thread_id,
    user_id=user_id,
    session_id=session_id,
    permissions=permissions,
  ):
    payload = parse_sse(chunk)
    if payload is None:
      continue
    yield payload.get('event') or '', (payload.get('data') or {})

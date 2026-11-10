"""DanglingToolCallMiddleware — 修补悬空 tool 调用与孤儿 tool 结果（对齐 DeerFlow）

**悬空**：`AIMessage.tool_calls` 声明了调用，但消息历史里没有对应的 `ToolMessage`。
成因是 run 在「模型已提交 AIMessage、ToolNode 尚未执行」的窗口里被打断（异常、
SSE 取消、超时）。实测 rz 库里出现过 8 处 / 5 个会话。

**孤儿**：反过来，`ToolMessage` 存在但找不到声明它的 `AIMessage.tool_call`。成因是
上游 AIMessage 被压缩/分支切掉。

两者都会被严格的 OpenAI 兼容后端拒绝（HTTP 400），宽松的后端（当前 vLLM）会容忍
但让模型看到「我说要调用工具却没人回结果」的半截历史。

修法（照搬 DeerFlow `_build_patched_messages`，见
deer-flow/agents/middlewares/dangling_tool_call_middleware.py:429-503）：
1. 建索引：`tool_call_id → deque[ToolMessage]`、全部声明的 tool_call id 集合
2. 顺序重建消息列表：tool 结果**归位到它的 AIMessage 之后**；配对不到的补一条合成
   ToolMessage；找不到声明者的孤儿结果**丢弃**
3. 没有变化就完全不动（零变异，保住 prefix cache）

为什么必须用 `awrap_model_call`：`abefore_model` 返回的 state update 会被
`add_messages` reducer **追加到列表末尾**，补的 ToolMessage 就会跑到错误位置
（DeerFlow 同款注释，:16-18）。本中间件只改本次模型调用（`request.override`），
**不写回 state** —— 所以既幂等、也不污染 checkpoint，现存的坏会话无需手工清洗，
用户下次发消息即自愈。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# 合成消息措辞：让模型知道这次调用没结果，可以重试（对齐 DeerFlow 默认档）
_INTERRUPTED_CONTENT = (
  '[该工具调用被中断，未返回结果。如需该数据，请重新调用对应工具。]'
)


def _tool_calls_of(message: Any) -> list[dict]:
  """取消息上合法的 tool_calls（dict 形态、带 id）。"""
  calls = getattr(message, 'tool_calls', None) or []
  return [c for c in calls if isinstance(c, dict) and c.get('id')]


def build_patched_messages(messages: list) -> list | None:
  """重建消息列表：补齐悬空调用、丢弃孤儿结果、把 tool 结果归位到其 AIMessage 之后。

  无任何变化时返回 None（调用方据此跳过 override，保 prefix cache）。
  """
  tool_results: dict[str, deque] = defaultdict(deque)
  for msg in messages:
    if isinstance(msg, ToolMessage):
      tool_results[msg.tool_call_id].append(msg)

  declared: set = set()
  for msg in messages:
    for tc in _tool_calls_of(msg):
      declared.add(tc['id'])

  patched: list = []
  added = 0
  dropped = 0
  for msg in messages:
    if isinstance(msg, ToolMessage):
      if msg.tool_call_id in declared:
        continue  # 稍后紧跟在它的 AIMessage 之后重发
      # 孤儿：声明它的 AIMessage 已不在请求里（如被压缩切掉）。仅在本次请求里丢弃，
      # 不改持久状态 —— 严格后端会因「tool 结果没有对应调用」直接 400。
      dropped += 1
      continue

    patched.append(msg)

    for tc in _tool_calls_of(msg):
      tc_id = tc['id']
      queue = tool_results.get(tc_id)
      existing = queue.popleft() if queue else None
      if existing is not None:
        patched.append(existing)
      else:
        patched.append(ToolMessage(
          content=_INTERRUPTED_CONTENT,
          tool_call_id=tc_id,
          name=tc.get('name') or 'unknown',
          status='error',
        ))
        added += 1

  if added == 0 and dropped == 0 and patched == messages:
    return None
  if added or dropped:
    logger.warning(
      '悬空 tool_call 修补: 补齐 %d 条、丢弃孤儿 %d 条（仅作用于本次模型调用）',
      added, dropped,
    )
  return patched


class DanglingToolCallMiddleware(AgentMiddleware):
  """在每次模型调用前把消息序列修成 provider 接受的合法形态。"""

  async def awrap_model_call(self, request, handler):
    patched = build_patched_messages(list(request.messages or []))
    if patched is None:
      return await handler(request)
    return await handler(request.override(messages=patched))

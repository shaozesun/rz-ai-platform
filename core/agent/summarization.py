"""Agent 上下文摘要压缩工具函数 — 供 SummarizationMiddleware 使用"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)

# 默认摘要 prompt（参照 DeerFlow 默认 prompt：提取关键事实、意图、操作、待解决问题）
DEFAULT_SUMMARY_PROMPT = (
  '请将以下对话历史压缩为一段中文摘要，不超过 500 字。'
  '摘要需包含：关键事实、用户意图、已完成的操作、待解决的问题。'
  '只输出摘要本身，不要加任何前缀或解释。'
)


def parse_trigger(trigger_str: str) -> list[dict]:
  """解析触发条件字符串。

  'tokens:32000,fraction:0.8' → [{'type':'tokens','value':32000}, {'type':'fraction','value':0.8}]
  """
  result = []
  if not trigger_str.strip():
    return result
  for part in trigger_str.split(','):
    part = part.strip()
    if ':' not in part:
      continue
    type_name, _, val = part.partition(':')
    type_name = type_name.strip()
    val = val.strip()
    try:
      if type_name in ('tokens', 'messages'):
        result.append({'type': type_name, 'value': int(val)})
      elif type_name == 'fraction':
        result.append({'type': type_name, 'value': float(val)})
    except (ValueError, TypeError):
      logger.warning('解析 trigger 条件失败: %s', part)
  return result


def parse_keep(keep_str: str) -> dict:
  """解析保留策略。

  'messages:10' → {'type':'messages', 'value':10}
  """
  if ':' not in keep_str:
    return {'type': 'messages', 'value': 10}
  type_name, _, val = keep_str.partition(':')
  type_name = type_name.strip()
  val = val.strip()
  try:
    if type_name in ('tokens', 'messages'):
      return {'type': type_name, 'value': int(val)}
    if type_name == 'fraction':
      return {'type': type_name, 'value': float(val)}
  except (ValueError, TypeError):
    pass
  return {'type': 'messages', 'value': 10}


def estimate_tokens(messages: list) -> int:
  """粗略估算消息列表的 token 数（字符数 / 2）。

  覆盖 content 与 AIMessage.tool_calls 参数 —— 工具调用参数常为大段 JSON，
  漏计会让摘要触发严重滞后（实测上下文涨满 262k 窗口才触发）。
  """
  total = 0
  for m in messages:
    content = ''
    if isinstance(m, dict):
      c = m.get('content', '') or ''
      content = c if isinstance(c, str) else str(c)
      for tc in m.get('tool_calls', []) or []:
        total += len(str(tc.get('args') or {}) if isinstance(tc, dict) else str(getattr(tc, 'args', {})))
    else:
      c = getattr(m, 'content', '') or ''
      content = c if isinstance(c, str) else str(c)
      for tc in getattr(m, 'tool_calls', None) or []:
        total += len(str(getattr(tc, 'args', {})))
    total += len(content)
  return total // 2


def should_summarize(
  messages: list,
  summary: str,
  triggers: list[dict],
) -> bool:
  """检查是否命中任一 trigger（OR 逻辑），参照 DeerFlow 触发判定。

  Args:
    messages: 当前消息列表
    summary: 已有的摘要文本
    triggers: 解析后的触发条件列表
  """
  if not triggers:
    return False

  total_tokens = estimate_tokens(messages)
  if summary:
    total_tokens += estimate_tokens([{'content': summary}])
  message_count = len(messages)

  for trigger in triggers:
    t = trigger['type']
    v = trigger['value']
    if t == 'tokens' and total_tokens > v:
      logger.info('摘要触发: tokens=%d > %d', total_tokens, v)
      return True
    if t == 'messages' and message_count > v:
      logger.info('摘要触发: messages=%d > %d', message_count, v)
      return True
    # fraction 需要模型 max_tokens，这里用 128k 作为常见默认值
    if t == 'fraction' and total_tokens > int(128000 * v):
      logger.info('摘要触发: tokens=%d > %d (fraction=%s)', total_tokens, int(128000 * v), v)
      return True

  return False


def _determine_cutoff_index(messages: list, keep: dict) -> int:
  """按 keep 策略计算安全切分点（保留最近 N 条，不拆开 AI/Tool 对）。

  对齐 langchain SummarizationMiddleware._determine_cutoff_index。返回 0 表示
  无需压缩（消息数未超 keep）。
  """
  k_type = keep.get('type', 'messages')
  k_value = keep.get('value', 10)
  if k_type == 'messages':
    target = len(messages) - int(k_value)
  elif k_type == 'tokens':
    count, tokens = 0, 0
    for m in reversed(messages):
      tokens += estimate_tokens([m])
      count += 1
      if tokens >= k_value:
        break
    target = len(messages) - count
  else:  # fraction：保留最近 (1-value) 部分，即压缩前 fraction 部分
    target = int(len(messages) * (1 - k_value))
  if target <= 0:
    return 0
  return _find_safe_cutoff_point(messages, target)


def _find_safe_cutoff_point(messages: list, cutoff_index: int) -> int:
  """对齐 langchain _find_safe_cutoff_point：切分点落在 ToolMessage 上时回退
  包含其对应的 AIMessage(tool_calls)，避免拆散 AI/Tool 对。

  回退找不到匹配 AIMessage 时，前进跳过所有 ToolMessage（边缘兜底）。
  """
  if cutoff_index >= len(messages) or not isinstance(messages[cutoff_index], ToolMessage):
    return cutoff_index

  tool_call_ids: set = set()
  idx = cutoff_index
  while idx < len(messages) and isinstance(messages[idx], ToolMessage):
    if messages[idx].tool_call_id:
      tool_call_ids.add(messages[idx].tool_call_id)
    idx += 1

  for i in range(cutoff_index - 1, -1, -1):
    msg = messages[i]
    if isinstance(msg, AIMessage) and msg.tool_calls:
      ids = {tc.get('id') for tc in msg.tool_calls if tc.get('id')}
      if tool_call_ids & ids:
        return i
  return idx


def _trim_messages_for_summary(messages: list, max_tokens: int) -> list:
  """截取最近的消息喂给摘要模型，预算内尽量多含（从后往前），绝不返回空。

  不再用 langchain trim_messages：它带 start_on='human' 要求结果以 HumanMessage
  结尾，尾部是 ToolMessage（工具结果常超大）时直接返回空 → 整轮压缩被跳过。
  这里从后向前收，单条装不下就截断其 content（保留首尾），保证非空输入必返回
  非空。仅用于限制「喂给摘要生成模型」的输入大小，不是删除范围。
  """
  if max_tokens <= 0 or not messages:
    return messages
  result: list = []
  tokens = 0
  for m in reversed(messages):
    t = estimate_tokens([m])
    if tokens + t <= max_tokens:
      result.insert(0, m)
      tokens += t
      continue
    # 剩余预算装不下整条：截断 content 装入（预算足够时），然后停止
    budget_chars = (max_tokens - tokens) * 2
    if budget_chars > 0:
      result.insert(0, _truncate_message(m, budget_chars))
    break
  return result


def _truncate_message(message: Any, char_limit: int) -> Any:
  """把单条消息 content（str）截断到 char_limit 字符，保留首尾两段。"""
  if isinstance(message, dict):
    content = message.get('content') or ''
    if isinstance(content, str) and len(content) > char_limit:
      head = char_limit * 3 // 4
      tail = max(char_limit // 4, 1)
      return {
        **message,
        'content': content[:head] + '\n...(截断)...\n' + content[-tail:],
      }
    return message
  content = getattr(message, 'content', '') or ''
  if isinstance(content, str) and len(content) > char_limit:
    head = char_limit * 3 // 4
    tail = max(char_limit // 4, 1)
    return message.model_copy(
      update={'content': content[:head] + '\n...(截断)...\n' + content[-tail:]},
    )
  return message

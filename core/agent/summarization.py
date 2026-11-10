"""Agent 上下文摘要压缩工具函数 — 供 SummarizationMiddleware 使用"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

logger = logging.getLogger(__name__)

# 历史摘要消息的固定前缀：压缩时作为 HumanMessage 重放，按内容识别。
# 唯一定义在这里（中间件 import），避免两处字面量漂移。
SUMMARY_PREFIX = '[历史对话摘要]'

# 默认摘要 prompt（参照 DeerFlow 默认 prompt：提取关键事实、操作、待解决问题）
DEFAULT_SUMMARY_PROMPT = (
  '请将以下对话历史压缩为一段简洁的中文摘要，不超过 300 字。'
  '摘要需包含：关键事实、已完成的操作、待解决的问题。'
  '注意：只记录已发生的事实与操作，**不要断言或推断用户的当前意图**'
  '（用户意图以最新消息为准，可能随时改变）。只输出摘要本身，不要加任何前缀或解释。'
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
  """粗略估算消息列表的 token 数（字符数 / 2）—— 仅在模型上报 token 不可用时兜底。

  覆盖 content 与 AIMessage.tool_calls 参数 —— 工具调用参数常为大段 JSON，
  漏计会让摘要触发严重滞后（实测上下文涨满 262k 窗口才触发）。

  为什么不用 langchain 的 count_tokens_approximately（DeerFlow 走的是这个）：它的
  `chars_per_token` 默认 4.0（按英文），中文约 1 字 1 token，对本项目这种「中英混排
  JSON」会低估约 2 倍；而它的 usage 自校准缩放被硬夹在 [1.0, 1.25]（langchain_core
  /messages/utils.py 的 `min(1.25, max(1.0, scale_factor))`）救不回来。实测本项目的
  字符//2 反而更准：摘要调用按估算预算 4000 截断时，端点上报的 tokens_in 是
  4349/4564/4717/4896（含约 250 token 提示词），误差 ≤20%。
  真正的口径由 `reported_total_tokens()` 保证，本函数只是兜底。
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


def reported_total_tokens(messages: list) -> int | None:
  """最近一条 AIMessage 上报的真实 token 数（usage_metadata.total_tokens），无则 None。

  对齐 DeerFlow `_should_summarize_based_on_reported_tokens`，简化掉多 provider 的
  别名匹配（rz 单网关）。total_tokens = 该次请求的 prompt + 输出，正好约等于下一次
  请求的 prompt 大小，是最贴近真实的「上下文占用」口径。

  依赖 model_gateway.as_langchain_model(stream_usage=True)：不开该参数时流式响应
  不带 usage，AIMessage.usage_metadata 恒为 None，本函数返回 None（自动回退估算器）。
  """
  for m in reversed(messages):
    if isinstance(m, dict):
      if (m.get('type') or m.get('role')) != 'ai':
        continue
      usage = m.get('usage_metadata')
    else:
      if getattr(m, 'type', None) != 'ai':
        continue
      usage = getattr(m, 'usage_metadata', None)
    if not isinstance(usage, dict):
      continue
    total = usage.get('total_tokens')
    if isinstance(total, int) and not isinstance(total, bool) and total > 0:
      return total
  return None


def model_window_tokens() -> int:
  """模型上下文窗口（真实 token 数），fraction 类条件据此换算。

  懒加载 settings，避免本模块（纯函数）在导入期依赖配置。
  """
  from config.settings import settings
  return int(getattr(settings, 'AGENT_MODEL_MAX_INPUT_TOKENS', 262144) or 262144)


def trigger_token_budget() -> int | None:
  """当前配置下压缩触发的 token 阈值（前端「上下文 X%」分母用）。

  多条件 OR 时取最小的那个 —— 最先命中的必然是它。只有 messages 条件时返回 None。
  """
  from config.settings import settings
  budgets: list[int] = []
  for trigger in parse_trigger(getattr(settings, 'AGENT_SUMMARIZE_TRIGGER', '') or ''):
    if trigger['type'] == 'tokens':
      budgets.append(int(trigger['value']))
    elif trigger['type'] == 'fraction':
      budgets.append(int(model_window_tokens() * trigger['value']))
  return min(budgets) if budgets else None


def _has_summary_message(messages: list) -> bool:
  """messages 里是否已有压缩时重放的 `[历史对话摘要]` 消息。"""
  for m in messages:
    content = m.get('content') if isinstance(m, dict) else getattr(m, 'content', None)
    if isinstance(content, str) and content.startswith(SUMMARY_PREFIX):
      return True
  return False


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
  # 压缩后会重放一条 `[历史对话摘要]` 消息，summary 的文本已经包含在 messages 里；
  # 再补加一次就是重复计数（摘要按「旧摘要 + --- + 新摘要」线性拼接，压缩次数越多
  # 重复量越大，会让估算口径提前触发）。只有 messages 里缺这条消息时才补。
  if summary and not _has_summary_message(messages):
    total_tokens += estimate_tokens([{'content': summary}])
  message_count = len(messages)
  # 模型上报的真实 token（有则优先，命中即触发；对齐 DeerFlow 用 >=）
  reported = reported_total_tokens(messages)

  for trigger in triggers:
    t = trigger['type']
    v = trigger['value']
    if t == 'messages':
      if message_count > v:
        logger.info('摘要触发: messages=%d > %d', message_count, v)
        return True
      continue
    # fraction = 模型窗口的比例（对齐 langchain：max_input_tokens × fraction）
    threshold = int(v) if t == 'tokens' else int(model_window_tokens() * v)
    if reported is not None and reported >= threshold:
      logger.info('摘要触发: 模型上报 tokens=%d >= %d (%s=%s)', reported, threshold, t, v)
      return True
    if total_tokens > threshold:
      logger.info('摘要触发: 估算 tokens=%d > %d (%s=%s)', total_tokens, threshold, t, v)
      return True

  return False


def _keep_count_by_tokens(messages: list, target_tokens: int) -> int:
  """从后往前累加 token，返回「保留尾部约 target_tokens」对应的保留条数。"""
  count, tokens = 0, 0
  for m in reversed(messages):
    tokens += estimate_tokens([m])
    count += 1
    if tokens >= target_tokens:
      break
  return count


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
    target = len(messages) - _keep_count_by_tokens(messages, int(k_value))
  else:
    # fraction：保留「模型窗口 × value」的 token 量（对齐 langchain 的 fraction
    # keep 语义）。此前误按消息条数比例切，与 token 无关。
    target = len(messages) - _keep_count_by_tokens(
      messages, int(model_window_tokens() * k_value),
    )
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

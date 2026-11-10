"""SummarizationMiddleware — 上下文自动压缩（对齐 langchain / DeerFlow）

DeerFlow 扩展 langchain 内置 SummarizationMiddleware；其正确的压缩方式是
`RemoveMessage(id=REMOVE_ALL_MESSAGES)` 全删 + 只重放保留的活动尾部
（见 langchain/agents/middleware/summarization.py:399-405、deer-flow
summarization_middleware.py:603-625）。

历史 bug：旧实现把「喂给摘要模型的输入子集」当成了「删除集合」，每次只删
≤trim_tokens 的 2~4 条，其余旧消息全留在 state，上下文跨轮净增，最终涨满
262k 窗口触发 400。本实现对齐 langchain 流程：
1. 触发判定：token 超阈值
2. 安全切分：保留最近 N 条（keep 策略），不拆散 AI/Tool 对
3. 摘要生成：只喂被删消息的有界输入（trim_tokens_to_summarize）
4. 全删 + 重放 summary 消息 + preserved_messages，写入 state.summary

保留 `model_gateway.chat` 生成摘要（纯 HTTP，不向 astream_events 泄漏流式
文本），避免 langchain 内置版 self.model.ainvoke 需 TAG_NOSTREAM + 前端过滤。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from core.agent.history_archive import capture
from core.agent.middlewares.durable_context import EPHEMERAL_FLAG
from core.agent.summarization import (
  DEFAULT_SUMMARY_PROMPT,
  SUMMARY_PREFIX,
  _determine_cutoff_index,
  _trim_messages_for_summary,
  parse_keep,
  parse_trigger,
  should_summarize,
)

logger = logging.getLogger(__name__)

# 摘要输入的角色标签：必须区分工具返回，否则工具结果会被摘要模型当成「AI 说的话」
# 而被合理改写（langchain 用 get_buffer_string 分角色）。
_ROLE_LABELS = {
  'human': '用户',
  'user': '用户',
  'ai': 'AI',
  'tool': '工具返回',
  'system': '系统',
}

def _content_text(message: Any) -> str:
  """取消息可见文本；非字符串内容用 JSON 序列化（避免 str() 出 Python repr）。"""
  if isinstance(message, dict):
    content = message.get('content')
  else:
    content = getattr(message, 'content', None)
  if isinstance(content, str):
    return content
  if content is None:
    return ''
  try:
    return json.dumps(content, ensure_ascii=False)
  except (TypeError, ValueError):
    return str(content)


def _is_summary_message(message: Any) -> bool:
  """是否为上一轮压缩重放的历史摘要消息。"""
  return _content_text(message).startswith(SUMMARY_PREFIX)


def _current_thread_id() -> str:
  """当前会话 thread_id：优先取请求上下文，取不到再问 LangGraph 的 runnable config。"""
  from core.agent.user_context import get_current_thread_id
  thread_id = get_current_thread_id()
  if thread_id:
    return thread_id
  try:
    from langgraph.config import get_config
    return (get_config().get('configurable', {}) or {}).get('thread_id') or ''
  except Exception:
    return ''


def _is_real_user_message(message: Any) -> bool:
  """本轮真实用户消息（排除历史摘要消息与 ephemeral 注入块）。"""
  if not isinstance(message, HumanMessage) or _is_summary_message(message):
    return False
  kwargs = getattr(message, 'additional_kwargs', None) or {}
  return not kwargs.get(EPHEMERAL_FLAG)


def _protected_tool_names(raw: str) -> set[str]:
  """解析 AGENT_SUMMARIZE_PROTECTED_TOOLS（逗号分隔的工具名）。"""
  return {n.strip() for n in (raw or '').split(',') if n.strip()}


def _turn_head_index(messages: list) -> int | None:
  """本轮起点下标（最后一条真实用户消息），找不到返回 None。"""
  for i in range(len(messages) - 1, -1, -1):
    if _is_real_user_message(messages[i]):
      return i
  return None


def _rescue_messages(messages: list, *, protected_tools: set[str]) -> list:
  """挑出压缩时必须保留的消息（保持原相对顺序）。

  - 本轮最新用户消息
  - 受保护工具所在的**完整 tool 轮次**：命中的 ToolMessage、产出它的 AIMessage，
    以及该 AIMessage 其余 tool_calls 的配对 ToolMessage。

  只保留命中的那一条会让并行调用产生悬空 ToolMessage（被
  DanglingToolCallMiddleware 补成「调用被中断」，模型会白重跑一次），所以按
  「AI 消息的全部 tool_calls」闭包式保留，保证 tool_call ↔ ToolMessage 一一对应。
  """
  if not messages:
    return []

  keep_ids: set[int] = set()
  for m in reversed(messages):
    if _is_real_user_message(m):
      keep_ids.add(id(m))
      break

  if protected_tools:
    matched_ids: set = set()
    for m in messages:
      if isinstance(m, ToolMessage) and getattr(m, 'name', None) in protected_tools:
        keep_ids.add(id(m))
        if m.tool_call_id:
          matched_ids.add(m.tool_call_id)
    if matched_ids:
      # 命中受保护工具的 AIMessage：把它全部 tool_call id 纳入保留集，
      # 再按这个集合找回所有配对 ToolMessage（并行调用不会只留一半结果）。
      round_ids = set(matched_ids)
      for m in messages:
        calls = getattr(m, 'tool_calls', None) or []
        if any(tc.get('id') in matched_ids for tc in calls):
          keep_ids.add(id(m))
          round_ids.update(tc.get('id') for tc in calls if tc.get('id'))
      for m in messages:
        if isinstance(m, ToolMessage) and m.tool_call_id in round_ids:
          keep_ids.add(id(m))

  return [m for m in messages if id(m) in keep_ids]


def _message_key(message: Any) -> tuple:
  """消息身份键：优先用 LangChain 消息 id，无 id（含 dict）时回退对象身份。

  不能只用 `message.id`：dict 消息都没有 id，会把不同消息当成同一条吞掉。
  """
  if isinstance(message, dict):
    msg_id = message.get('id')
  else:
    msg_id = getattr(message, 'id', None)
  return ('id', msg_id) if msg_id else ('obj', id(message))


def _dedupe_keep_order(messages: list) -> list:
  """按身份去重，保持首次出现顺序。

  `_rescue_messages` 与本轮消息切片会 rescue 到同一条用户消息，直接拼接会让
  它在 preserved 里出现两次（实测 `['u2','a1','m1','m2','u2']`）。
  """
  seen: set = set()
  result: list = []
  for m in messages:
    key = _message_key(m)
    if key in seen:
      continue
    seen.add(key)
    result.append(m)
  return result


class SummarizationMiddleware(AgentMiddleware):
  """上下文摘要压缩 Middleware。

  在 before_model 中检查 token 阈值，命中则：
  1. 安全切分旧消息/保留尾部（keep + AI/Tool 对保护）
  2. 调 LLM 生成摘要（输入有界）
  3. RemoveMessage 全删旧消息，只重放 summary 消息 + 保留尾部
  4. 更新 state.summary（供 DurableContextMiddleware 下轮注入）
  """

  async def abefore_model(
    self, state: dict[str, Any], runtime: Any,
  ) -> dict[str, Any] | None:
    """在每次模型调用前检查是否需要压缩上下文。

    Args:
      state: 当前 agent state
      runtime: LangGraph runtime context

    Returns:
      state update dict（全删旧消息 + 注入摘要消息 + 保留尾部 + 更新 summary），
      或 None 跳过
    """
    from config.settings import settings

    if not settings.AGENT_SUMMARIZE_ENABLED:
      return None

    triggers = parse_trigger(settings.AGENT_SUMMARIZE_TRIGGER)
    if not triggers:
      return None

    messages = list(state.get('messages', []))
    existing_summary = state.get('summary', '') or ''

    if not should_summarize(messages, existing_summary, triggers):
      return None

    keep = parse_keep(settings.AGENT_SUMMARIZE_KEEP)
    cutoff_index = _determine_cutoff_index(messages, keep)
    if cutoff_index <= 0:
      return None

    messages_to_summarize = messages[:cutoff_index]
    preserved_messages = messages[cutoff_index:]
    if not messages_to_summarize:
      return None

    # 上一轮压缩重放的历史摘要消息不能再进摘要模型：它不携带新信息，且会让
    # 「摘要的摘要」逐轮放大误差。其内容已由 existing_summary 合并进新摘要
    # （见下方拼接），输出里由新的 summary_msg 取代。
    dropped_total = len(messages_to_summarize)
    messages_to_summarize = [
      m for m in messages_to_summarize if not _is_summary_message(m)
    ]
    if not messages_to_summarize:
      return None

    # 按精确对象 rescue 关键消息（对齐 DeerFlow `latest_user_id` + 参考类结果保护）：
    # 1. 本轮最新用户消息 —— 它通常位于列表开头、正好落在删除区间里，删掉后模型
    #    只能靠摘要转述继续答题（DeerFlow 明说：按 id rescue，不能往前挪 cutoff，
    #    否则会连带保留早期 AI/Tool 轮，导致首轮长分析永远压不掉）；
    # 2. 受保护工具所在的完整 tool 轮次 —— 字典类数据（公司/园区/部门列表）
    #    体量小、价值高，是「园区名↔companyId 映射」这类权威对照的唯一来源；
    #    命中时连同该 AIMessage 的全部 tool_calls/配对 ToolMessage 一起保留，
    #    并行调用才不会留下悬空 ToolMessage。
    rescued = _rescue_messages(
      messages_to_summarize,
      protected_tools=_protected_tool_names(settings.AGENT_SUMMARIZE_PROTECTED_TOOLS),
    )

    # 3. 本轮全部消息（最后一条真实用户消息起，落在删除区间内的那些）——
    #    阈值降到 tokens:32000 后，重工具轮次（实测一轮 21 次调用）会在**工具循环
    #    中途**就命中压缩；只靠上面 1/2 两条 rescue，本轮前几次工具结果会被删掉，
    #    模型这一轮就答不下去了（历史 incident：园区名说错）。
    #    跨轮不受影响：新一轮的用户消息是列表最后一条，永远在 cutoff 之后，
    #    上一轮消息照常可压 —— 即「压缩只发生在轮与轮之间」。
    #    注意 1 与 3 会 rescue 到同一条用户消息，必须按身份去重，否则 preserved
    #    里会出现两次（重复内容白占 token，还会让模型看到重复提问）。
    turn_head = _turn_head_index(messages)
    if turn_head is not None and turn_head < cutoff_index:
      rescued = [*messages[turn_head:cutoff_index], *rescued]
    rescued = _dedupe_keep_order(rescued)
    if rescued:
      rescued_ids = {id(m) for m in rescued}
      messages_to_summarize = [
        m for m in messages_to_summarize if id(m) not in rescued_ids
      ]
      preserved_messages = [*rescued, *preserved_messages]
      logger.info('压缩保留 %d 条关键消息（本轮全部消息/受保护工具结果）', len(rescued))
      if not messages_to_summarize:
        return None

    # 摘要生成输入有界（仅用于 LLM 生成摘要，不是删除范围）；trim_messages
    # 允许部分裁剪，单条再大也不会导致整轮跳过
    trimmed = _trim_messages_for_summary(
      messages_to_summarize, settings.AGENT_SUMMARIZE_TRIM_TOKENS,
    )
    if not trimmed:
      return None
    summary_input = _build_summary_input(trimmed)
    prompt = settings.AGENT_SUMMARIZE_PROMPT or DEFAULT_SUMMARY_PROMPT

    try:
      from core.model_gateway import model_gateway
      new_summary = await model_gateway.chat(
        messages=[
          {'role': 'system', 'content': prompt},
          {'role': 'user', 'content': summary_input},
        ],
        temperature=0.3,
      )
      if not new_summary or not new_summary.strip():
        logger.warning('摘要模型返回空结果，跳过压缩')
        return None
    except Exception as e:
      logger.warning('摘要生成失败，跳过压缩: %s', e)
      return None

    if existing_summary:
      new_summary = f'{existing_summary}\n---\n{new_summary}'

    summary_msg = HumanMessage(
      content=f'{SUMMARY_PREFIX}\n{new_summary}\n[/历史对话摘要]'
    )

    # 摘要成功产出后才归档被删消息的原文（对齐 DeerFlow：为一段不存在的摘要
    # flush 归档，会在下一轮重复做一遍）。归档不可用时不影响压缩本身。
    task_history = None
    thread_id = _current_thread_id()
    if thread_id:
      try:
        task_history = capture(thread_id, messages_to_summarize)
      except Exception as e:  # 归档失败不该拖垮本轮对话
        logger.warning('历史归档失败（跳过）: %s', e)

    logger.info(
      '摘要完成: 删除 %d 条旧消息（含 %d 条历史摘要消息，已合并不重复摘要）'
      ' → %d 字摘要, 保留 %d 条, 归档 %s',
      dropped_total, dropped_total - len(messages_to_summarize),
      len(new_summary), len(preserved_messages),
      (task_history or {}).get('status', 'skipped'),
    )

    return {
      # 核心修复：全删 + 只重放保留尾部（对齐 langchain/DeerFlow）
      'messages': [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_msg, *preserved_messages],
      'summary': new_summary,
      **({'task_history': task_history} if task_history else {}),
    }


def _build_summary_input(messages: list) -> str:
  """构建摘要输入文本（分角色标注，工具结果带工具名）。

  工具结果必须与 AI 发言区分：标成「AI」会让摘要模型把平台原始数据当成模型的
  话而合理改写/概括（langchain 用 get_buffer_string 分角色，见其
  agents/middleware/summarization.py）。
  """
  lines = ['待压缩的对话：']
  for m in messages:
    if isinstance(m, dict):
      role = m.get('role', m.get('type', 'unknown'))
      tool_name = m.get('name')
    else:
      role = getattr(m, 'type', 'unknown')
      tool_name = getattr(m, 'name', None)
    label = _ROLE_LABELS.get(str(role).lower(), str(role))
    if label == _ROLE_LABELS['tool'] and tool_name:
      label = f'{label}:{tool_name}'
    lines.append(f'[{label}]: {_content_text(m)}')
  return '\n'.join(lines)

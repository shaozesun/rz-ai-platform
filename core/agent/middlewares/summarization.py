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

import logging
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from core.agent.summarization import (
  DEFAULT_SUMMARY_PROMPT,
  _determine_cutoff_index,
  _trim_messages_for_summary,
  parse_keep,
  parse_trigger,
  should_summarize,
)

logger = logging.getLogger(__name__)


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

    # 摘要生成输入有界（仅用于 LLM 生成摘要，不是删除范围）；trim_messages
    # 允许部分裁剪，单条再大也不会导致整轮跳过
    trimmed = _trim_messages_for_summary(
      messages_to_summarize, settings.AGENT_SUMMARIZE_TRIM_TOKENS,
    )
    if not trimmed:
      return None
    summary_input = _build_summary_input(trimmed, existing_summary)
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
      content=f'[历史对话摘要]\n{new_summary}\n[/历史对话摘要]'
    )

    logger.info(
      '摘要完成: 删除 %d 条旧消息 → %d 字摘要, 保留 %d 条',
      len(messages_to_summarize), len(new_summary), len(preserved_messages),
    )

    return {
      # 核心修复：全删 + 只重放保留尾部（对齐 langchain/DeerFlow）
      'messages': [RemoveMessage(id=REMOVE_ALL_MESSAGES), summary_msg, *preserved_messages],
      'summary': new_summary,
    }


def _build_summary_input(messages: list, existing_summary: str) -> str:
  """构建摘要输入文本。"""
  lines = []
  if existing_summary:
    lines.append(f'已有摘要：\n{existing_summary}\n')
  lines.append('待压缩的对话：')
  for m in messages:
    role = 'unknown'
    content = ''
    if hasattr(m, 'type') and hasattr(m, 'content'):
      role = m.type
      content = m.content if isinstance(m.content, str) else str(m.content)
    elif isinstance(m, dict):
      role = m.get('role', m.get('type', 'unknown'))
      content = m.get('content', '') or ''
    role_label = '用户' if role in ('human', 'user') else 'AI'
    lines.append(f'[{role_label}]: {content}')
  return '\n'.join(lines)

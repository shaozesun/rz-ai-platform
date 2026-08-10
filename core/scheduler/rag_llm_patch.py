"""Monkey-patch RagService._get_llm() 使所有 RAG LLM 调用经过 Scheduler。

RAG 服务绕过 ModelGateway 自行创建 ChatOpenAI 实例，因此需要通过
猴子补丁拦截 _get_llm() 返回值，包装其底层方法以实现调度和并发控制。

通过 contextvars 传递当前用户角色，API 层使用 set_chat_role() 设置。
"""

import contextvars
import logging
from typing import Any, AsyncIterator
from langchain_openai import ChatOpenAI

from core.scheduler.enums import TaskType, RoleLevel
from core.scheduler.scheduler import TaskScheduler, SlotNotAvailable

logger = logging.getLogger(__name__)

_WRAPPED_ATTR = '_scheduler_wrapped'

# Context variable: 当前对话请求的用户角色
_chat_role: contextvars.ContextVar[RoleLevel] = contextvars.ContextVar(
  'chat_role', default=RoleLevel.USER
)


def set_chat_role(role: RoleLevel) -> contextvars.Token:
  """设置当前上下文中的对话用户角色，返回 token 用于恢复。"""
  return _chat_role.set(role)


def reset_chat_role(token: contextvars.Token):
  """恢复之前的角色。"""
  _chat_role.reset(token)


def _is_wrapped(obj: Any) -> bool:
  return getattr(obj, _WRAPPED_ATTR, False)


def _mark_wrapped(obj: Any):
  setattr(obj, _WRAPPED_ATTR, True)


def patch_rag_service(scheduler: TaskScheduler):
  """替换 RagService._get_llm 使其返回经过 Scheduler 包装的 ChatOpenAI。

  包装后：
  - invoke() (查询改写/意图分类) → scheduler.schedule(TaskType.CHAT)
  - astream() (主对话生成) → scheduler.schedule_stream(TaskType.CHAT)
  """
  from service.rag.pipeline.rag_service import RagService

  original_get_llm = RagService._get_llm

  def scheduled_get_llm(self, temperature: float = 0.3):
    llm = original_get_llm(self, temperature)

    if _is_wrapped(llm):
      return llm

    _wrap_llm(llm, scheduler)
    _mark_wrapped(llm)
    return llm

  RagService._get_llm = scheduled_get_llm
  logger.info('RagService._get_llm 已注入 Scheduler 包装')


def _wrap_llm(llm: ChatOpenAI, scheduler: TaskScheduler):
  """包装 ChatOpenAI 实例的底层方法。"""
  _wrap_agenerate(llm, scheduler)
  _wrap_astream(llm, scheduler)


def _wrap_agenerate(llm: ChatOpenAI, scheduler: TaskScheduler):
  """包装异步非流式调用（查询改写等）。"""
  original_agenerate = llm._agenerate

  async def scheduled_agenerate(messages, *args, **kwargs):
    async def call():
      return await original_agenerate(messages, *args, **kwargs)

    return await scheduler.schedule(
      task_type=TaskType.CHAT,
      coro=call(),
      role=_chat_role.get(),
    )

  llm._agenerate = scheduled_agenerate


def _wrap_astream(llm: ChatOpenAI, scheduler: TaskScheduler):
  """包装异步流式调用（主对话生成）。"""
  original_astream = llm._astream

  async def scheduled_astream(
    messages, *args, **kwargs
  ) -> AsyncIterator[Any]:
    async def stream():
      async for chunk in original_astream(messages, *args, **kwargs):
        yield chunk

    gen = stream()
    try:
      async for chunk in scheduler.schedule_stream(
        task_type=TaskType.CHAT,
        stream_coro=gen,
        role=_chat_role.get(),
      ):
        yield chunk
    except SlotNotAvailable:
      # 流式请求无槽位时，通过 chunk 返回错误信息
      yield type('_ErrorChunk', (), {
        'content': '[系统繁忙] 当前使用人数较多，请稍后重试',
      })()

  llm._astream = scheduled_astream

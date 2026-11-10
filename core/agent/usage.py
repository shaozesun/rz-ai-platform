"""LangChain 链路的 LLM 用量记账 —— 补上 `model_gateway` 记账的盲区。

## 为什么需要它

`llm_usage` 原先只在 `model_gateway.chat()/chat_stream()/vision()` 里写
（`core/model_gateway.py` 的 `log_llm_usage`），而**两条主链路直接用 LangChain 的
`ChatOpenAI`，绕过了这几个方法**：

- Agent 对话：`model_gateway.as_langchain_model()`（意图规划 + 图形主循环 + 工具循环）
- RAG 对话：`service/rag/pipeline/rag_service.py` 自己 new 的 `ChatOpenAI`

结果仪表盘「算力消耗」少算至少一个数量级。2026-09-28 实测：某周 16 轮 Agent 对话，
`llm_usage` 里只有 17 条、且全是**摘要压缩**的用量（`caller='chat'`），
对话本身的 token 一个都没记上。

本模块让 LangChain 的每次 LLM 调用也写进同一张表，用 `caller='agent'` / `'rag'`
与既有来源（`chat`/`vision`/`fire_safety`/`agent_video_*`）区分。

## 为什么用**同步** handler

RAG 链路里有 `llm.invoke(...)`（同步链）。`AsyncCallbackHandler` 的 `on_llm_end`
在同步链上会被 LangChain 的 `handle_event` **收集成协程然后直接丢弃**
（同步上下文不 await 那个协程）—— 那些调用永远记不上账。同步 `BaseCallbackHandler`
在同步链和异步链**都会被调用**，是唯一两边都覆盖的写法。

## 不会重复计数

摘要压缩走 `model_gateway.chat()`（裸 HTTP 客户端，不经过 LangChain），所以这个回调
看不到它 —— 它由 `log_llm_usage(caller='chat')` 单独记账。两条路互不相交。
"""

import asyncio
import logging

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


def _extract_usage(response) -> tuple[int, int, str] | None:
  """从 LLMResult 取出 (tokens_in, tokens_out, model)；取不到返回 None。

  优先 `ChatGeneration.message.usage_metadata`（LangChain 标准位置，依赖
  `as_langchain_model(stream_usage=True)` / 流式请求带 `include_usage`），
  回退 `llm_output['token_usage']`（部分后端只填这里）。
  """
  tin = tout = None
  model = ''

  try:
    gen = response.generations[0][0]
  except (AttributeError, IndexError, TypeError):
    gen = None

  msg = getattr(gen, 'message', None)
  um = getattr(msg, 'usage_metadata', None) or {}
  if um:
    tin = um.get('input_tokens')
    tout = um.get('output_tokens')
  rm = getattr(msg, 'response_metadata', None) or {}
  model = rm.get('model_name') or rm.get('model') or ''

  if tin is None:
    lo = getattr(response, 'llm_output', None) or {}
    tu = lo.get('token_usage') or lo.get('usage') or {}
    tin = tu.get('prompt_tokens', tu.get('input_tokens'))
    tout = tu.get('completion_tokens', tu.get('output_tokens'))
    model = model or lo.get('model_name', '')

  if tin is None and tout is None:
    return None
  return int(tin or 0), int(tout or 0), model


class UsageLoggingHandler(BaseCallbackHandler):
  """每次 LLM 调用结束后把用量写进 `llm_usage`（一次调用一条记录，无状态可共享）。"""

  def __init__(self, caller: str):
    super().__init__()
    self.caller = caller

  def on_llm_end(self, response, **kwargs) -> None:
    # 懒 import：core.model_gateway 在 as_langchain_model 里反向引用本模块，
    # 模块级 import 会成环
    from core.model_gateway import log_llm_usage, log_llm_usage_sync

    parsed = _extract_usage(response)
    if parsed is None:
      logger.debug('LLM 用量回调：响应里没有 usage，跳过 caller=%s', self.caller)
      return

    tokens_in, tokens_out, model = parsed
    try:
      loop = asyncio.get_running_loop()
    except RuntimeError:
      # 纯同步上下文（无运行中的事件循环）：直接插库
      log_llm_usage_sync(self.caller, model, tokens_in, tokens_out)
    else:
      # 异步上下文：交给 loop，别在回调里阻塞模型调用
      loop.create_task(log_llm_usage(self.caller, model, tokens_in, tokens_out))

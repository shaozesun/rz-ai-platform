"""统一模型网关 — 所有 LLM 调用唯一入口"""

import time
import hashlib
import asyncio
from datetime import datetime
from typing import AsyncIterator, Optional
from openai import AsyncOpenAI
from config.settings import settings
import logging
from config.trace_id import get_trace_id

logger = logging.getLogger(__name__)


class ModelGateway:
  """统一 LLM 网关 (单例)"""

  _instance: Optional['ModelGateway'] = None

  def __new__(cls):
    if cls._instance is None:
      cls._instance = super().__new__(cls)
      cls._instance._initialized = False
    return cls._instance

  def __init__(self):
    if self._initialized:
      return
    self._initialized = True

    # 对话客户端
    self._chat_client = AsyncOpenAI(
      api_key=settings.LLM_API_KEY,
      base_url=settings.LLM_BASE_URL,
      timeout=settings.LLM_TIMEOUT,
      max_retries=settings.LLM_MAX_RETRIES,
    ) if settings.LLM_BASE_URL else None

    # 视觉客户端 (可能指向不同端点)
    vision_base = settings.VISION_BASE_URL or settings.LLM_BASE_URL
    vision_key = settings.VISION_API_KEY or settings.LLM_API_KEY
    self._vision_client = AsyncOpenAI(
      api_key=vision_key,
      base_url=vision_base,
      timeout=settings.LLM_TIMEOUT,
      max_retries=settings.LLM_MAX_RETRIES,
    ) if vision_base else None

    # Embedding 客户端
    emb_base = settings.EMBEDDING_BASE_URL or settings.LLM_BASE_URL
    emb_key = settings.EMBEDDING_API_KEY or settings.LLM_API_KEY
    self._embedding_client = AsyncOpenAI(
      api_key=emb_key,
      base_url=emb_base,
      timeout=settings.LLM_TIMEOUT,
      max_retries=settings.LLM_MAX_RETRIES,
    ) if emb_base else None

    self._cache: dict[str, tuple[float, str]] = {}
    self._cache_max = 128

  def _cache_key(self, *parts: str) -> str:
    return hashlib.sha256('\x00'.join(parts).encode()).hexdigest()

  async def chat(
    self,
    messages: list[dict],
    model: str = '',
    temperature: float = 0.7,
    max_tokens: int = 4096,
    json_mode: bool = False,
    cache: bool = False,
    timeout: float | None = None,
    caller: str = 'chat',
    enable_thinking: bool = False,
  ) -> str:
    """文本对话 — 返回完整回复"""
    client = self._chat_client
    if not client:
      raise RuntimeError('LLM 未配置 (LLM_BASE_URL)')

    model_name = model or settings.LLM_MODEL
    trace_id = get_trace_id()
    start = time.time()
    effective_timeout = timeout if timeout is not None else settings.LLM_TIMEOUT

    # 缓存 (仅确定性场景)
    if cache:
      cache_key = self._cache_key('chat', model_name, str(messages))
      if cache_key in self._cache:
        cached_time, cached_val = self._cache[cache_key]
        if time.time() - cached_time < 3600:
          return cached_val

    kwargs = {
      'model': model_name,
      'messages': messages,
      'temperature': temperature,
      'max_tokens': max_tokens,
    }
    if json_mode:
      kwargs['response_format'] = {'type': 'json_object'}
    if not enable_thinking:
      kwargs['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}

    try:
      response = await asyncio.wait_for(
        client.chat.completions.create(**kwargs),
        timeout=effective_timeout,
      )
    except asyncio.TimeoutError:
      msg = f'chat timeout after {effective_timeout}s'
      logger.error(msg)
      raise asyncio.TimeoutError(msg)
    except Exception as e:
      logger.error(f'chat error: {e}')
      raise

    elapsed = time.time() - start
    content = response.choices[0].message.content or ''
    usage = response.usage
    logger.info(
      f'model={model_name} '
      f'elapsed={elapsed:.2f}s '
      f'tokens_in={usage.prompt_tokens if usage else "?"} '
      f'tokens_out={usage.completion_tokens if usage else "?"} '
    )

    if usage:
      asyncio.ensure_future(_log_usage(
        user_id=None, model=model_name,
        tokens_in=usage.prompt_tokens, tokens_out=usage.completion_tokens,
        caller=caller,
      ))

    if cache:
      self._cache[cache_key] = (time.time(), content)
      if len(self._cache) > int(self._cache_max * 1.5):
        # LRU 淘汰
        sorted_items = sorted(self._cache.items(), key=lambda x: x[1][0])
        for old_key, _ in sorted_items[:len(self._cache) - self._cache_max]:
          del self._cache[old_key]

    return content

  async def stream(
    self,
    messages: list[dict],
    model: str = '',
    temperature: float = 0.7,
    max_tokens: int = 4096,
    caller: str = 'rag',
  ) -> AsyncIterator[str]:
    """流式对话 — 异步生成器"""
    client = self._chat_client
    if not client:
      raise RuntimeError('LLM 未配置')

    model_name = model or settings.LLM_MODEL
    trace_id = get_trace_id()
    start = time.time()

    try:
      stream = await client.chat.completions.create(
        model=model_name,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        stream=True,
        stream_options={'include_usage': True},
      )
      usage = None
      async for chunk in stream:
        if chunk.usage:
          usage = chunk.usage
        delta = chunk.choices[0].delta
        if delta.content:
          yield delta.content
    except Exception as e:
      logger.error(f'stream error: {e}')
      raise

    elapsed = time.time() - start
    if usage:
      asyncio.ensure_future(_log_usage(
        user_id=None, model=model_name,
        tokens_in=usage.prompt_tokens,
        tokens_out=usage.completion_tokens,
        caller=caller,
      ))
    logger.info(
      f'model={model_name} stream '
      f'elapsed={elapsed:.2f}s '
      f'tokens_in={usage.prompt_tokens if usage else "?"} '
      f'tokens_out={usage.completion_tokens if usage else "?"}'
    )

  async def vision(
    self,
    system_prompt: str,
    user_text: str,
    images: list[str],
    model: str = '',
    temperature: float = 0.3,
    max_tokens: int = 4096,
  ) -> str:
    """多模态视觉理解 — 图片 + 文本"""
    client = self._vision_client
    if not client:
      raise RuntimeError('视觉模型未配置 (VISION_BASE_URL)')

    model_name = model or settings.VISION_MODEL
    trace_id = get_trace_id()
    start = time.time()

    content: list[dict] = [{'type': 'text', 'text': user_text}]
    for img in images:
      # img 可以是 base64 字符串或 URL
      if img.startswith('http://') or img.startswith('https://'):
        content.append({'type': 'image_url', 'image_url': {'url': img}})
      else:
        # base64 data URI
        if not img.startswith('data:'):
          img = f'data:image/jpeg;base64,{img}'
        content.append({'type': 'image_url', 'image_url': {'url': img}})

    messages = [
      {'role': 'system', 'content': system_prompt},
      {'role': 'user', 'content': content},
    ]

    try:
      response = await asyncio.wait_for(
        client.chat.completions.create(
          model=model_name,
          messages=messages,
          temperature=temperature,
          max_tokens=max_tokens,
          extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        ),
        timeout=settings.LLM_TIMEOUT,
      )
    except asyncio.TimeoutError:
      logger.error(f'vision timeout after {settings.LLM_TIMEOUT}s')
      raise
    except Exception as e:
      logger.error(f'vision error: {e}')
      raise

    elapsed = time.time() - start
    content_text = response.choices[0].message.content or ''
    usage = response.usage
    logger.info(
      f'model={model_name} vision '
      f'elapsed={elapsed:.2f}s images={len(images)} '
      f'tokens_in={usage.prompt_tokens if usage else "?"}'
    )

    if usage:
      asyncio.ensure_future(_log_usage(
        user_id=None, model=model_name,
        tokens_in=usage.prompt_tokens, tokens_out=usage.completion_tokens or 0,
        caller='vision',
      ))

    return content_text

  async def embedding(self, texts: list[str], model: str = '') -> list[list[float]]:
    """文本向量化"""
    client = self._embedding_client
    if not client:
      raise RuntimeError('Embedding 模型未配置 (EMBEDDING_BASE_URL)')

    model_name = model or settings.EMBEDDING_MODEL
    trace_id = get_trace_id()
    start = time.time()

    try:
      response = await client.embeddings.create(
        model=model_name,
        input=texts,
      )
    except Exception as e:
      logger.error(f'embedding error: {e}')
      raise

    elapsed = time.time() - start
    embeddings = [d.embedding for d in response.data]
    logger.info(
      f'model={model_name} embedding '
      f'elapsed={elapsed:.2f}s count={len(texts)}'
    )

    return embeddings

  async def batch_score(
    self,
    query: str,
    documents: list[str],
    model: str = '',
    temperature: float = 0.0,
  ) -> list[float]:
    """批量打分 — 用于 cross-encoder 重排序

    返回每个文档与 query 的相关性分数 (0-1)
    """
    client = self._chat_client
    if not client:
      raise RuntimeError('LLM 未配置')

    model_name = model or settings.LLM_MODEL
    trace_id = get_trace_id()

    # 并发评分
    async def score_one(doc: str) -> float:
      messages = [
        {'role': 'system', 'content': (
          '你是一个相关性评分专家。评分规则:\n'
          '- 完全相关: 1.0\n- 高度相关: 0.8\n- 部分相关: 0.5\n'
          '- 低相关: 0.2\n- 不相关: 0.0\n'
          '请只输出一个 0.0 到 1.0 之间的数字，不要任何解释。'
        )},
        {'role': 'user', 'content': f'查询: {query}\n\n文档: {doc[:500]}'},
      ]
      try:
        result = await client.chat.completions.create(
          model=model_name,
          messages=messages,
          temperature=temperature,
          max_tokens=10,
        )
        text = result.choices[0].message.content or '0'
        try:
          return min(1.0, max(0.0, float(text.strip())))
        except ValueError:
          return 0.0
      except Exception:
        return 0.0

    scores = await asyncio.gather(*[score_one(d) for d in documents])
    logger.info(
      f'batch_score model={model_name} '
      f'queries={len(documents)} avg_score={sum(scores)/len(scores):.3f}'
    )
    return scores

  def as_langchain_model(self, model_name: str | None = None):
    """返回 LangChain BaseChatModel，供 langgraph Agent 使用"""
    from langchain_openai import ChatOpenAI

    name = model_name or settings.LLM_MODEL
    return ChatOpenAI(
      base_url=str(self._chat_client.base_url) if self._chat_client else '',
      api_key=settings.LLM_API_KEY,
      model=name,
      temperature=settings.LLM_TEMPERATURE,
      timeout=settings.LLM_TIMEOUT,
      max_retries=settings.LLM_MAX_RETRIES,
      extra_body={'chat_template_kwargs': {'enable_thinking': False}},
    )


async def _log_usage(user_id, model, tokens_in, tokens_out, caller):
    """Fire-and-forget 写入 LLM 用量到 MongoDB（用同步客户端避免事件循环绑定问题）"""
    import asyncio
    try:
      from config.mongodb_conn import mongodb_manager
      await asyncio.to_thread(
        mongodb_manager.sync_db.llm_usage.insert_one,
        {
          'user_id': user_id,
          'model': model,
          'tokens_in': tokens_in,
          'tokens_out': tokens_out,
          'caller': caller,
          'created_at': datetime.utcnow(),
        },
      )
    except Exception:
      logger.warning('LLM 用量写入失败 caller=%s', caller, exc_info=True)


model_gateway = ModelGateway()

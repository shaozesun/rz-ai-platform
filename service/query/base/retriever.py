"""能力语义检索 —— 海量能力收敛的关键（平台无关，跨平台统一索引）。

Agent 不直接面对全部能力，而是先用一句查询语义检索出最相关的 top-k 能力，
再让 LLM 从少数候选中选择。借鉴 langgraph-bigtool 的 retrieve_tools 模式与
`query -> list[id]` 极简签名，检索复用 core.model_gateway.embedding，不引入
LangGraph Store 第二套 embedding。

从 service/query/dcim/retriever.py 上移到 base：逻辑本就平台无关，唯一改动是
索引源从单平台 all_capabilities 改为 base.platform 的跨平台聚合。能力 id 全局
唯一（带平台前缀），单一索引天然隔离多平台，无需分平台建库。
"""

import logging
import math

from service.query.base.platform import all_capabilities

logger = logging.getLogger(__name__)

# 能力向量缓存：capability_id -> embedding 向量
_CACHE: dict[str, list[float]] = {}
_CACHE_IDS: list[str] = []


def _cosine(a: list[float], b: list[float]) -> float:
  dot = sum(x * y for x, y in zip(a, b))
  na = math.sqrt(sum(x * x for x in a))
  nb = math.sqrt(sum(y * y for y in b))
  if na == 0 or nb == 0:
    return 0.0
  return dot / (na * nb)


def _index_text(cap) -> str:
  """构建用于检索的文本：名称 + 描述，名称加权在前。"""
  return f'{cap.name}。{cap.description}'


async def ensure_index() -> None:
  """确保能力向量索引已构建（惰性，首次检索时触发）。

  假设平台在启动阶段完成注册，首次检索时能力集已就绪。
  """
  if _CACHE:
    return
  from core.model_gateway import model_gateway

  caps = all_capabilities()
  if not caps:
    logger.warning('retriever: 无已注册能力，索引为空')
    return
  texts = [_index_text(c) for c in caps]
  vectors = await model_gateway.embedding(texts)
  for cap, vec in zip(caps, vectors):
    _CACHE[cap.id] = vec
    _CACHE_IDS.append(cap.id)
  logger.info('retriever 索引构建完成 count=%d', len(_CACHE))


async def search_capabilities(query: str, k: int = 8) -> list[str]:
  """按查询语义检索最相关的能力 id。

  Args:
    query: 用户查询或意图描述。
    k: 返回条数上限。

  Returns:
    capability_id 列表，按相关度降序；能力总数不足 k 时返回全部。
  """
  await ensure_index()
  if not _CACHE_IDS:
    return []
  from core.model_gateway import model_gateway

  q_vec = (await model_gateway.embedding([query]))[0]
  scored = [(cid, _cosine(q_vec, _CACHE[cid])) for cid in _CACHE_IDS]
  scored.sort(key=lambda x: x[1], reverse=True)
  return [cid for cid, _ in scored[:k]]


def reset_index() -> None:
  """清空索引缓存（平台变更或测试时用）。"""
  _CACHE.clear()
  _CACHE_IDS.clear()

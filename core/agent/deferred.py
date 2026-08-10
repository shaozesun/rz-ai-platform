"""Deferred Tool 机制 — 海量工具收敛（移植 DeerFlow，检索换成 embedding 语义）。

思路（B' 方案，融合两家之长）：
- DeerFlow 的机制：工具是真 BaseTool，全进 ToolNode 供执行；但平时把未 promote 的
  工具 schema 从 bind_tools 里过滤掉，LLM 只在 prompt 里看到工具名。LLM 调 tool_search
  发现工具 → 返回完整 schema 并记入 state['promoted'] → 之后才能带强 schema 调用。
- rz 的改进：tool_search 的检索不用 DeerFlow 原生的正则匹配，改用
  service/query/dcim/retriever.py 的 embedding 语义检索（命中同义/意图更准）。

参考源码：~/PycharmProjects/deer-flow/backend/packages/harness/deerflow/
  agents/middlewares/deferred_tool_filter_middleware.py + tools/builtins/tool_search.py
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from functools import cached_property
from typing import Annotated

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import AgentState, ModelRequest
from langchain_core.messages import ToolMessage
from langchain_core.tools import BaseTool, InjectedToolCallId, tool
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from typing_extensions import TypedDict

logger = logging.getLogger(__name__)


class PromotedTools(TypedDict):
  """已 promote 的工具集，按 catalog_hash 隔离。"""

  catalog_hash: str
  names: list[str]


def merge_promoted(existing: PromotedTools | None, new: PromotedTools | None) -> PromotedTools | None:
  """promote 状态 reducer（移植 DeerFlow）：names 累积去重，catalog_hash 变则整体替换。

  - new 空 → 保留 existing（该节点未触碰）。
  - catalog_hash 变 → 整体替换，丢弃旧 names（防目录漂移后旧名暴露错工具）。
  - 同 catalog_hash → union names 去重保序（多次 tool_search 累积可见工具）。
  """
  if not new:
    return existing
  if existing is None or existing.get('catalog_hash') != new['catalog_hash']:
    return {'catalog_hash': new['catalog_hash'], 'names': list(dict.fromkeys(new['names']))}
  return {
    'catalog_hash': existing['catalog_hash'],
    'names': list(dict.fromkeys(existing['names'] + new['names'])),
  }


class DeferredState(AgentState):
  """扩展 AgentState，加 promoted 字段供 deferred filter 读、tool_search 写。"""

  promoted: Annotated[PromotedTools | None, merge_promoted]


@dataclass(frozen=True)
class DeferredCatalog:
  """不可变的 deferred 工具目录。工具名 → schema 检索。"""

  tools: tuple[BaseTool, ...]

  @cached_property
  def names(self) -> frozenset[str]:
    return frozenset(t.name for t in self.tools)

  @cached_property
  def hash(self) -> str:
    """目录指纹，用于 scope promotion，防 schema 漂移后暴露旧工具。"""
    canon = [
      {'name': t.name, 'schema': convert_to_openai_function(t)}
      for t in sorted(self.tools, key=lambda t: t.name)
    ]
    blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:16]

  def by_name(self, name: str) -> BaseTool | None:
    for t in self.tools:
      if t.name == name:
        return t
    return None


class DeferredToolFilterMiddleware(AgentMiddleware):
  """对 LLM 隐藏未 promote 的工具 schema，拦截未 promote 的调用。

  移植自 DeerFlow DeferredToolFilterMiddleware，保留其核心：
  - wrap_model_call：从 request.tools 过滤掉未 promote 的 deferred 工具。
  - wrap_tool_call：拦截调用未 promote 工具，提示先 tool_search。
  promote 状态存 state['promoted']，按 catalog_hash 隔离。
  """

  # 声明扩展 state：create_agent 会合并此 schema，使 promoted 字段生效。
  # 缺此声明则 tool_search 的 Command(update={'promoted':...}) 会被丢弃。
  state_schema = DeferredState

  def __init__(self, deferred_names: frozenset[str], catalog_hash: str | None):
    super().__init__()
    self._deferred = deferred_names
    self._catalog_hash = catalog_hash

  def _promoted(self, state) -> set[str]:
    promoted = (state or {}).get('promoted')
    if promoted and promoted.get('catalog_hash') == self._catalog_hash:
      return set(promoted.get('names') or [])
    return set()

  def _hidden(self, state) -> set[str]:
    return set(self._deferred) - self._promoted(state)

  def _filter_tools(self, request: ModelRequest) -> ModelRequest:
    if not self._deferred:
      return request
    hide = self._hidden(request.state)
    if not hide:
      return request
    active = [t for t in request.tools if getattr(t, 'name', None) not in hide]
    return request.override(tools=active)

  def _blocked_msg(self, request: ToolCallRequest) -> ToolMessage | None:
    if not self._deferred:
      return None
    name = str(request.tool_call.get('name') or '')
    if not name or name not in self._hidden(request.state):
      return None
    tid = str(request.tool_call.get('id') or 'missing_tool_call_id')
    return ToolMessage(
      content=(f"错误：工具 '{name}' 尚未加载。请先调用 tool_search 发现并加载该工具的参数定义，再重试。"),
      tool_call_id=tid,
      name=name,
      status='error',
    )

  # 仅定义 async 版：rz Agent 全程走 astream_events（异步执行）。若同时定义同步版，
  # langchain 在异步路径可能误调同步 handler，对 async 工具返回未 await 的协程 →
  # "Unsupported message type: coroutine"。只留 awrap_* 避免此坑。

  async def awrap_model_call(self, request, handler):
    return await handler(self._filter_tools(request))

  async def awrap_tool_call(self, request, handler):
    blocked = self._blocked_msg(request)
    return blocked if blocked is not None else await handler(request)


def build_tool_search(catalog: DeferredCatalog, top_k: int = 8) -> BaseTool:
  """构建 tool_search 工具：embedding 语义检索 + 返回 schema + promote。

  与 DeerFlow 的差异：检索复用 service/query/dcim/retriever.py 的语义检索
  （embedding + 余弦），而非 DeerFlow 原生正则匹配。命中后返回完整 OpenAI
  schema 并把工具名写入 state['promoted']，使其对 LLM 可见、可调。
  """
  catalog_hash = catalog.hash

  @tool
  async def tool_search(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """根据自然语言查询，发现并加载可用于回答的工具。

    系统提示的 <available-tools> 里只列出工具名。调用本工具按语义检索匹配的工具，
    返回其完整参数定义；返回后这些工具即可调用。

    Args:
      query: 查询意图，如 'A7机房有哪些告警'、'机房容量还剩多少'
    """
    from service.query.base.retriever import search_capabilities
    from service.query.base.tools_factory import capability_to_tool_name

    cap_ids = await search_capabilities(query, k=top_k)
    # capability_id → 工具名 → catalog 里的真工具
    matched = []
    for cid in cap_ids:
      t = catalog.by_name(capability_to_tool_name(cid))
      if t is not None:
        matched.append(t)

    if not matched:
      content, names = f'未找到匹配 "{query}" 的工具', []
    else:
      content = json.dumps(
        [convert_to_openai_function(t) for t in matched],
        indent=2, ensure_ascii=False,
      )
      names = [t.name for t in matched]

    return Command(update={
      'promoted': {'catalog_hash': catalog_hash, 'names': names},
      'messages': [ToolMessage(content=content, tool_call_id=tool_call_id, name='tool_search')],
    })

  return tool_search


def build_deferred_setup(deferred_tools: list[BaseTool], top_k: int = 8):
  """从 deferred 工具集组装：catalog + tool_search + filter middleware。

  Returns:
    (all_tools, middleware, deferred_names, catalog_hash)
    - all_tools：deferred 工具 + tool_search（都交给 agent 的 ToolNode 执行）
    - middleware：DeferredToolFilterMiddleware 实例
    - deferred_names：需在 prompt 里以 <available-tools> 列出的工具名
    - catalog_hash：catalog 指纹，供编排层预 promote 时按此隔离
  """
  catalog = DeferredCatalog(tuple(deferred_tools))
  search_tool = build_tool_search(catalog, top_k=top_k)
  mw = DeferredToolFilterMiddleware(catalog.names, catalog.hash)
  all_tools = list(deferred_tools) + [search_tool]
  return all_tools, mw, catalog.names, catalog.hash


def deferred_tools_prompt(deferred_names: frozenset[str]) -> str:
  """生成 <available-tools> 提示段，只列工具名，供 LLM 知道有哪些可 tool_search。"""
  if not deferred_names:
    return ''
  names = '\n'.join(sorted(deferred_names))
  return (
    '\n\n<available-tools>\n'
    '以下工具可用，但需先调用 tool_search 加载其参数定义后才能调用：\n'
    f'{names}\n'
    '</available-tools>'
  )

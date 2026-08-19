"""Deferred Tool 机制 — 海量工具收敛（移植 DeerFlow，检索换成 embedding 语义）。

思路（B' 方案，融合两家之长）：
- DeerFlow 的机制：工具是真 BaseTool，全进 ToolNode 供执行；但平时把未 promote 的
  工具 schema 从 bind_tools 里过滤掉，LLM 只在 prompt 里看到分类清单。LLM 调
  tool_search 发现工具 → 返回发现卡片并记入 state['promoted'] → 之后才能带强
  schema 调用。
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


def _truncate(text: str, limit: int) -> str:
  """截断到 limit 字符内（含省略号），超长补 …（发现卡片里描述只留摘要）。"""
  text = text or ''
  return text if len(text) <= limit else text[:limit - 1] + '…'


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
  """构建 tool_search 工具：embedding 语义检索 + 返回发现卡片 + promote。

  与 DeerFlow 的差异：检索复用 service/query/base/retriever.py 的语义检索
  （embedding + 余弦），而非 DeerFlow 原生正则匹配。命中后返回紧凑「发现卡片」
  （P-3：不再返回完整 OpenAI schema——被 promote 的工具下一轮 model request 里
  LangGraph 会自动带上它的完整 schema，这里只需告诉 LLM 发现了什么、可选哪个，
  避免一次塞 3000-6000 token 进上下文），并把工具名写入 state['promoted']。
  """
  catalog_hash = catalog.hash

  @tool
  async def tool_search(query: str, tool_call_id: Annotated[str, InjectedToolCallId]) -> Command:
    """根据自然语言查询，发现并加载可用于回答的工具。

    系统提示的 <available-tools> 里列出各平台的分类清单。调用本工具按语义检索
    匹配的工具，返回其名称/简述/参数名；返回后这些工具即可调用（参数详说明在
    下一轮模型请求里会随工具 schema 一起提供）。

    Args:
      query: 查询意图，如 'A7机房有哪些告警'、'机房容量还剩多少'
    """
    from core.agent.user_context import get_current_user_id
    from service.query.base.platform import get_capability
    from service.query.base.retriever import search_capabilities
    from service.query.base.tools_factory import capability_to_tool_name

    cap_ids = await search_capabilities(query, k=top_k)
    current_user = get_current_user_id()
    # capability_id → 工具名 → catalog 里的真工具
    matched = []
    for cid in cap_ids:
      cap = get_capability(cid)
      # 用户自建技能只对本人生效：非本人技能在检索结果里过滤掉（不 promote、不进
      # ToolMessage），实现运行期按用户隔离
      if cap is not None and cap.owner and cap.owner != current_user:
        continue
      t = catalog.by_name(capability_to_tool_name(cid))
      if t is not None:
        matched.append(t)

    if not matched:
      content, names = f'未找到匹配 "{query}" 的工具', []
    else:
      content = json.dumps(
        [{
          'name': t.name,
          'description': _truncate(t.description, 80),
          'params': list(getattr(t.args_schema, 'model_fields', {}).keys()),
        } for t in matched],
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


def deferred_tools_prompt(manifest: str) -> str:
  """生成 <available-tools> 提示段，渲染分类清单（P-2：不再是全量工具名列表）。

  清单只告诉 LLM「有哪些平台、各有哪些分类、每类覆盖什么」，引导它组织 tool_search
  的搜索 query（按自然语言描述意图即可命中，无需提前知道确切工具名）。
  """
  if not manifest:
    return ''
  return (
    '\n\n<available-tools>\n'
    '平台工具默认隐藏，需先调用 tool_search 加载参数定义后再调用。'
    '用自然语言描述要查的内容（如「查郭春磊的部门」「A7 机房告警」）。\n'
    f'{manifest}\n'
    '</available-tools>'
  )

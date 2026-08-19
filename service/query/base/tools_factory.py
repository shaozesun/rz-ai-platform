"""把能力注册表动态生成为真 LangChain 工具（带 args_schema 强校验，平台无关）。

从 service/query/dcim/tools_factory.py 上移到 base：逻辑本就平台无关，唯一改动是
工具执行体的后端 caller 不再硬 import dcim client，而是注册时经 get_caller(cap.id)
按能力定位其平台 caller（闭包绑定）。这样同一份工厂可为 DCIM/mgmt/... 所有平台
生成工具，运行时无需前缀路由。

设计目的（B' 方案）：
- 每条 Capability → 一个 StructuredTool，参数由 params 动态生成 pydantic schema，
  让 LLM 调用时受强类型校验。
- 生成的工具交给 deferred filter：平时对 LLM 隐藏 schema，tool_search 命中后才暴露。
"""

import json

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from config.settings import settings
from core.agent.user_context import get_current_user_id
from service.query.base.capability import Capability
from service.query.base.platform import Caller, all_capabilities, get_caller

# 截断标记：告知 LLM 结果被截断，引导其缩小范围或分页补查；沙箱开启时额外提示
# 物化全量做分析（防单条工具结果撑爆上下文，同时让超长分析型查询自然路由到沙箱）
_SANDBOX_HINT = '分页查询，或调用 analysis_load 物化全量后用 analysis_exec 分析'
_HINT = _SANDBOX_HINT if settings.AGENT_ANALYSIS_ENABLED else '分页查询'
_TRUNCATE_MARKER = f'\n…（结果过长已截断，实际 {{n}} 字符；如需更多请{_HINT}）'
_RECORD_TRUNCATE_MARKER = f'\n…（结果共 {{total}} 条，已保留前 {{kept}} 条完整记录；如需更多请{_HINT}）'

# 工具名前缀：capability_id 'dcim.query_alarm' → 工具名 'dcim__query_alarm'
# （工具名不能带点，用双下划线替代，反向可还原 capability_id）
_NAME_SEP = '__'


def capability_to_tool_name(capability_id: str) -> str:
  return capability_id.replace('.', _NAME_SEP)


def tool_name_to_capability(tool_name: str) -> str:
  return tool_name.replace(_NAME_SEP, '.')


def _build_args_schema(cap: Capability):
  """按 Capability.params 生成 pydantic 模型作为 args_schema。

  params 形如 {'room': '说明', 'severity': '说明'}，全部作为可选 str 字段，
  说明写进 Field.description 供 LLM 理解。
  """
  fields = {
    name: (str, Field(default='', description=desc))
    for name, desc in cap.params.items()
  }
  # 模型名用 capability 派生，避免重名
  model_name = f'Args_{cap.id.replace(".", "_")}'
  return create_model(model_name, **fields)


def _records_lists(obj) -> list[list]:
  """收集结果里所有「list of dict」值（即记录列表），供记录级截断。

  记录级截断只从列表末尾弹掉**完整记录**，避免按字符硬切把记录从中间切开
  （否则 LLM 只拿到首条完整数据，表格只显示第一行 + 省略行）。
  """
  out: list[list] = []
  if isinstance(obj, dict):
    for v in obj.values():
      if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
        out.append(v)
      else:
        out.extend(_records_lists(v))
  elif isinstance(obj, list):
    for v in obj:
      out.extend(_records_lists(v))
  return out


def _make_tool(cap: Capability, caller: Caller) -> StructuredTool:
  """把一条 Capability 包装成 StructuredTool，闭包绑定其平台 caller。"""

  async def _run(**kwargs) -> str:
    # 过滤掉空串参数，交给 caller（与门面工具行为一致）
    params = {k: v for k, v in kwargs.items() if v not in ('', None)}

    # clamp 分页参数：LLM 可能乱填大 page_size（如 204），把单条工具结果撑爆上下文
    max_page = settings.AGENT_MAX_PAGE_SIZE
    for key in ('page_size', 'pageSize'):
      if key in params:
        try:
          if int(params[key]) > max_page:
            params[key] = str(max_page)
        except (ValueError, TypeError):
          params[key] = str(max_page)

    try:
      # 补传 on_behalf_of=当前用户：skills 平台 caller 据此隔离用户自建技能
      # （其它平台 caller 未使用该参数，无副作用）
      result = await caller(cap.id, params, on_behalf_of=get_current_user_id())
      # 紧凑序列化（去 indent）：同预算下能装更多完整记录
      text = json.dumps(result, ensure_ascii=False, separators=(',', ':'))

      # 单条工具结果进上下文前封顶，防 400 context too long。
      # 超预算先做记录级截断（弹掉末尾完整记录，不按字符硬切），再字符级兜底——
      # 避免从记录中间切开，导致 LLM 只拿到首条完整数据、表格只显示一行。
      max_chars = settings.AGENT_MAX_TOOL_OUTPUT_CHARS
      if max_chars > 0 and len(text) > max_chars:
        lists = _records_lists(result)
        original_total = sum(len(lst) for lst in lists)
        while len(text) > max_chars and sum(len(lst) for lst in lists) > 1:
          for lst in reversed(lists):
            if lst:
              lst.pop()
              break
          text = json.dumps(result, ensure_ascii=False, separators=(',', ':'))
        kept = sum(len(lst) for lst in lists)
        if kept < original_total:
          text += _RECORD_TRUNCATE_MARKER.format(total=original_total, kept=kept)

      # 字符级最终兜底（单条记录极宽仍超限时）
      if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars] + _TRUNCATE_MARKER.format(n=len(text))
      return text
    except Exception as e:  # 走 raw 通道，未包装异常中间件，需自行兜底防崩
      return json.dumps({
        'error': f'能力 "{cap.id}" 执行失败: {e}',
        'suggestion': '请稍后重试或联系管理员',
      }, ensure_ascii=False)

  # 纯 async：ToolNode 在异步环境执行；不提供 sync func，避免 asyncio.run 在
  # 已运行事件循环里崩溃并泄漏协程。
  return StructuredTool.from_function(
    coroutine=_run,
    name=capability_to_tool_name(cap.id),
    description=cap.description,
    args_schema=_build_args_schema(cap),
  )


def build_all_tools() -> list[StructuredTool]:
  """把所有已注册平台的能力生成为工具列表，每个工具闭包绑定其平台 caller。

  未找到 caller 的能力会被跳过（正常情况不应发生：注册平台时 caller 必填）。
  """
  tools: list[StructuredTool] = []
  for cap in all_capabilities():
    caller = get_caller(cap.id)
    if caller is None:
      continue
    tools.append(_make_tool(cap, caller))
  return tools

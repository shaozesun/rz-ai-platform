"""编排层 —— 多标签编排的 Step 2（工具路由）。

拿意图识别器（query_planner）产出的 IntentPlan，按 intent_labels 决定预 promote
哪些工具组，产出 agent 的 initial_state。核心思想：路由到具体工具靠**查绑定表**
（deterministic），不靠语义搜索；LLM 后续只负责填参数。命中标签组**全部**预 promote
（不按查询类型截断）；真正的多轮/多工具执行由 agent 在 ReAct 循环里自决。

双重保险的代码层：intent_labels 先过滤（只保留绑定表里真实存在的），过滤后为空
则返回 None → 不预 promote → 走 tool_search fallback。因 Literal 约束模型本不会
输出非法标签，此处过滤是防御性兜底（含词汇表未定/模型异常等边界）。
"""

import logging

from core.agent.query_planner import IntentPlan

logger = logging.getLogger(__name__)


def route_to_tools(plan: IntentPlan, catalog_hash: str) -> dict | None:
  """按 IntentPlan 查绑定表，产出预 promote 的 PromotedTools。

  Args:
    plan: 意图识别结果。
    catalog_hash: 当前 deferred catalog 的指纹，promote 需按此隔离。

  Returns:
    PromotedTools 形态 dict {'catalog_hash', 'names'}；无可 promote 时返回 None
    （调用方据此走 tool_search fallback）。
  """
  from service.query.base.intent_bindings import build_intent_bindings
  from service.query.base.tools_factory import capability_to_tool_name

  bindings = build_intent_bindings()
  if not bindings:
    return None  # 词汇表未定义，无绑定可查

  # 代码层过滤：只保留绑定表里真实存在的标签（防御性兜底）
  valid_labels = [l for l in plan.intent_labels if l in bindings]
  if not valid_labels:
    return None  # 空标签 → tool_search fallback

  # 聚合命中能力 id（去重保序）——命中的标签组全部预 promote，不做截断
  cap_ids: list[str] = []
  for label in valid_labels:
    cap_ids.extend(bindings[label])
  cap_ids = list(dict.fromkeys(cap_ids))

  if not cap_ids:
    return None

  names = [capability_to_tool_name(cid) for cid in cap_ids]
  logger.info(
    'route_to_tools labels=%s → promote %d 工具',
    valid_labels, len(names),
  )
  return {'catalog_hash': catalog_hash, 'names': names}


# 关键词 → category 标签（绑定词汇，见 intent_bindings.py 回退 category）。
# 覆盖口语简写（故障单/变更单/阈值单等）；过度命中安全——正则只走「恰好 1 类」的
# 快路径，命中错的最差 = 主 LLM tool_search 兜底（不劣于无 plan）。
_CATEGORY_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
  # 工单类（work_order）：各类型「工单/单/裸词」全收
  (('待办工单', '我的工单', '我的待办', '我的已办', '待办', '已办', '已发起', '抄送',
    '变更工单', '变更单', '变更', '故障工单', '故障单', '故障', '问题工单', '问题单',
    '事件工单', '事件单', '事件', '阈值工单', '阈值单', '阈值', '供应商', '工单'), 'work_order'),
  # 组织人事（org，含培训/人员/部门）
  (('人员', '员工', '部门', '分部', '负责人', '培训', '名单', '档案', '职位', '工号',
    '我的信息', '我的部门'), 'org'),
  (('巡检',), 'inspection'),
  (('设备台账', '设备', '台账'), 'device'),
  (('维护计划', '维护', '保养', '维修'), 'maintenance'),
  (('告警', '报警', '温度'), 'alarm'),
  (('容量', '机柜'), 'capacity'),
  (('资产',), 'asset'),
  (('文档', '制度', 'SOP', '知识库', '标准', '规范'), 'document'),
  (('评审',), 'review'),
  (('配置',), 'config'),
]


def regex_route(message: str) -> list[str]:
  """正则快路径：扫消息，返回命中的 category 标签列表。

  命中多个返回多个（供调用方判断：仅恰好 1 类才走快路径）；无命中返回空。
  """
  matched: list[str] = []
  for keywords, label in _CATEGORY_KEYWORDS:
    if any(k in message for k in keywords):
      matched.append(label)
  return matched


# 常用查询 → 精准能力表：命中即只 promote 这条查询真正要用的几个工具（含部门/公司解析），
# 替代“命中类别就整组 promote”。本轮只覆盖工单相关 7 组。
_PROMOTE_MAX = 12
_RESOLVERS = ['mgmt.query_pen_company_dept', 'mgmt.query_company_list', 'mgmt.query_my_info']
_QUERY_TOOLS: list[tuple[tuple[str, ...], list[str]]] = [
  (('变更单', '变更工单', '变更'),
   ['mgmt.query_change_ticket', 'mgmt.query_change_ticket_detail', *_RESOLVERS]),
  (('事件单', '事件工单', '事件'),
   ['mgmt.query_event_ticket', 'mgmt.query_event_ticket_detail', *_RESOLVERS]),
  (('故障单', '故障工单', '故障'),
   ['mgmt.query_breakdown_ticket', 'mgmt.query_breakdown_ticket_detail', *_RESOLVERS]),
  (('问题单', '问题工单', '问题'),
   ['mgmt.query_question_ticket', 'mgmt.query_question_ticket_detail', *_RESOLVERS]),
  (('待办', '我的工单', '我的待办', '已办', '已发起', '抄送'),
   ['mgmt.query_pending_ticket', 'mgmt.query_my_info']),
  (('阈值单', '阈值工单', '阈值'),
   ['mgmt.query_threshold_ticket', 'mgmt.query_threshold_ticket_detail', *_RESOLVERS]),
  (('供应商工单', '供应商'),
   ['mgmt.query_supplier_ticket', 'mgmt.query_supplier_ticket_detail', *_RESOLVERS]),
]


def match_query_caps(message: str) -> list[str]:
  """扫常用表，返回命中能力 id（有序去重并集；多命中=多意图，均保留）。

  全局上限 _PROMOTE_MAX：超限优先保留各命中条目的“主查询工具”，再补 detail/解析类。
  """
  hit_entries = [(kws, caps) for kws, caps in _QUERY_TOOLS if any(k in message for k in kws)]
  if not hit_entries:
    return []
  out: list[str] = []
  for _, caps in hit_entries:
    for c in caps:
      if c not in out:
        out.append(c)
  if len(out) > _PROMOTE_MAX:
    mains = [caps[0] for _, caps in hit_entries]
    out = ([c for c in mains if c in out] + [c for c in out if c not in mains])[:_PROMOTE_MAX]
  return out


async def build_initial_state(message: str, catalog_hash: str, history: dict | None = None) -> dict:
  """跑完整 Step1+Step2，产出 agent 的 initial_state（含预 promote）。

  Args:
    message: 用户消息。
    catalog_hash: 当前 deferred catalog 指纹。
    history: 最近对话上下文 {'user_messages': [...], 'tools': [...]}，透传给 LLM plan
      供省略/延续追问（正则未命中的）判断指代；正则命中的完整查询不用历史。

  Returns:
    initial_state dict；无预 promote 时为空 dict（agent 正常走 tool_search）。
  """
  # 1) 常用查询精准表：命中即只 promote 该查询要用的几个工具（含解析类），首轮直接可用
  caps = match_query_caps(message)
  if caps:
    from service.query.base.tools_factory import capability_to_tool_name
    names = [capability_to_tool_name(c) for c in caps]
    logger.info('route_to_tools(常用表) → promote %d 工具 %s', len(names), caps)
    return {'promoted': {'catalog_hash': catalog_hash, 'names': names}}
  # 2) 兜底：类别正则恰好命中 1 个 → 整组 promote（非工单类仍走这里）
  matched = regex_route(message)
  if len(matched) == 1:
    plan = IntentPlan(intent_labels=matched)
    promoted = route_to_tools(plan, catalog_hash)
    return {'promoted': promoted} if promoted else {}
  # 未命中 / 命中多个（复合/模糊/省略延续/多主题）→ LLM plan 兜底，带最近上下文
  plan = await plan_query_safe(message, history)
  promoted = route_to_tools(plan, catalog_hash)
  if promoted is None:
    return {}
  return {'promoted': promoted}


async def plan_query_safe(message: str, history: dict | None = None) -> IntentPlan:
  """plan_query 的薄封装（plan_query 自身已兜底，这里保留扩展点）。"""
  from core.agent.query_planner import plan_query
  return await plan_query(message, history)

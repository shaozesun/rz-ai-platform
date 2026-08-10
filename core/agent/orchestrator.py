"""编排层 —— 多标签编排的 Step 2（工具路由）。

拿意图识别器（query_planner）产出的 IntentPlan，按 query_type + intent_labels
决定预 promote 哪些工具组，产出 agent 的 initial_state。核心思想：路由到具体
工具靠**查绑定表**（deterministic），不靠语义搜索；LLM 后续只负责填参数。

编排策略（对齐方案 query_type 四型）：
- single / parallel：查绑定表拿命中能力组 → 预 promote，LLM 直接调（快路径）。
- detect：不预 promote，交给 ReAct + tool_search 自主探索（慢路径）。
- chain：只预 promote 第一批命中能力，后续由结果驱动二次补查。

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

  # detect：模糊排查，不预设工具，交给 ReAct 探索
  if plan.query_type == 'detect':
    return None

  bindings = build_intent_bindings()
  if not bindings:
    return None  # 词汇表未定义，无绑定可查

  # 代码层过滤：只保留绑定表里真实存在的标签（防御性兜底）
  valid_labels = [l for l in plan.intent_labels if l in bindings]
  if not valid_labels:
    return None  # 空标签 → tool_search fallback

  # 聚合命中能力 id（去重保序）
  cap_ids: list[str] = []
  for label in valid_labels:
    cap_ids.extend(bindings[label])
  cap_ids = list(dict.fromkeys(cap_ids))

  # chain：仅取第一批，后续由结果驱动二次补查
  if plan.query_type == 'chain':
    cap_ids = cap_ids[:3]

  if not cap_ids:
    return None

  names = [capability_to_tool_name(cid) for cid in cap_ids]
  logger.info(
    'route_to_tools type=%s labels=%s → promote %d 工具',
    plan.query_type, valid_labels, len(names),
  )
  return {'catalog_hash': catalog_hash, 'names': names}


async def build_initial_state(message: str, catalog_hash: str) -> dict:
  """跑完整 Step1+Step2，产出 agent 的 initial_state（含预 promote）。

  Args:
    message: 用户消息。
    catalog_hash: 当前 deferred catalog 指纹。

  Returns:
    initial_state dict；无预 promote 时为空 dict（agent 正常走 tool_search）。
  """
  plan = await plan_query_safe(message)
  promoted = route_to_tools(plan, catalog_hash)
  if promoted is None:
    return {}
  return {'promoted': promoted}


async def plan_query_safe(message: str) -> IntentPlan:
  """plan_query 的薄封装（plan_query 自身已兜底，这里保留扩展点）。"""
  from core.agent.query_planner import plan_query
  return await plan_query(message)

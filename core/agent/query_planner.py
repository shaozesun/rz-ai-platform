"""意图识别器 —— 多标签编排的 Step 1（LLM 结构化输出）。

用一次快速 LLM 调用，把用户问题解析成结构化的 IntentPlan：
- query_type：single/parallel/detect/chain，决定编排策略。
- domains：命中哪些平台（驱动路由，闭合枚举）。
- intent_labels：命中哪些意图标签（驱动路由，闭合枚举）。
- subtasks/focus：自由文本，仅供 LLM 理解上下文，不驱动代码判断。

可迭代性关键：结构化输出的枚举值域**动态**从注册表派生（domains 来自已注册
平台，intent_labels 来自 base.intent_bindings），而非硬编码 Literal。分类词汇表
未定前留空，定了在能力上填 intent_labels 即自动生效，无需改本文件。

双重保险：
1. prompt 层 —— with_structured_output + 动态枚举，约束模型只能选合法值。
2. 代码层 —— plan_query 返回前再过滤一次非法/空值（见 orchestrator），脏数据
   直接走 tool_search fallback，绝不硬塞进路由。
"""

import logging
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field, create_model

logger = logging.getLogger(__name__)

QueryType = Literal['single', 'parallel', 'detect', 'chain']


@dataclass
class IntentPlan:
  """意图识别结果（内部稳定契约，与 LLM 动态输出 schema 解耦）。

  Attributes:
    query_type: 查询类型，决定编排策略。
    domains: 命中的平台名列表（如 ['dcim', 'mgmt']）。
    intent_labels: 命中的意图标签列表（闭合词汇表内）。
    subtasks: 拆解出的子问题描述，自由文本。
    focus: 用户核心诉求，自由文本。
  """

  query_type: str = 'detect'
  domains: list[str] = field(default_factory=list)
  intent_labels: list[str] = field(default_factory=list)
  subtasks: list[str] = field(default_factory=list)
  focus: str = ''


def _build_output_model(domains: list[str], labels: list[str]) -> type[BaseModel]:
  """按当前注册表状态动态构造结构化输出模型。

  domains/labels 非空时用 Literal 锁死值域；为空时退化为 list[str]（Literal
  不允许空枚举），此时靠代码层过滤兜底。query_type 始终是固定四值。
  """
  # domains 字段：有平台则 Literal 锁死，否则自由 str
  if domains:
    domain_type = list[Literal[tuple(domains)]]  # type: ignore[valid-type]
  else:
    domain_type = list[str]

  # intent_labels 字段：词汇表已定义则 Literal 锁死，否则自由 str
  if labels:
    label_type = list[Literal[tuple(labels)]]  # type: ignore[valid-type]
  else:
    label_type = list[str]

  return create_model(
    'IntentPlanOut',
    query_type=(QueryType, Field(description=(
      '查询类型：single=单域简单查询；parallel=多域各自独立结果；'
      'detect=模糊异常排查需先探测；chain=需分步推理的复杂分析'
    ))),
    domains=(domain_type, Field(default_factory=list, description='涉及的平台')),
    intent_labels=(label_type, Field(default_factory=list, description='命中的意图标签')),
    subtasks=(list[str], Field(default_factory=list, description='拆解出的子问题，自然语言')),
    focus=(str, Field(default='', description='用户核心诉求一句话概括')),
  )


def _build_prompt(message: str, domains: list[str], labels: list[str]) -> str:
  """构造意图识别提示词，把合法值域显式列进 prompt（prompt 层约束）。"""
  domain_hint = '、'.join(domains) if domains else '（暂无已注册平台）'
  label_hint = '、'.join(labels) if labels else '（意图词汇表尚未定义，intent_labels 请留空）'
  return (
    '你是运维 Agent 的意图识别器。分析用户问题，输出结构化意图。\n\n'
    '规则：\n'
    '1. query_type 判断：\n'
    '   - single：只涉及一个域的简单查询\n'
    '   - parallel：明确同时需要多个域的独立结果\n'
    '   - detect：模糊的异常排查，需先探测再聚焦\n'
    '   - chain：需分步骤推理的复杂分析\n'
    f'2. domains 只能从以下平台中选：{domain_hint}\n'
    f'3. intent_labels 只能从以下标签中选：{label_hint}\n'
    '4. subtasks：把问题拆成可执行的子问题（自然语言，可多条）\n'
    '5. focus：一句话概括用户到底想要什么\n\n'
    f'用户问题：{message}'
  )


async def plan_query(message: str, history: list | None = None) -> IntentPlan:
  """对用户问题做意图识别，返回结构化 IntentPlan。

  失败（LLM 异常/无平台）时返回 detect 型空 plan，使编排层走 tool_search
  fallback，不阻断主链路。

  Args:
    message: 用户消息。
    history: 预留的历史上下文（暂未使用）。

  Returns:
    IntentPlan；异常时为 detect 型空 plan。
  """
  from core.model_gateway import model_gateway
  from service.query.base.platform import registered_platforms
  from service.query.base.intent_bindings import all_intent_labels

  domains = registered_platforms()
  labels = all_intent_labels()

  try:
    out_model = _build_output_model(domains, labels)
    llm = model_gateway.as_langchain_model()
    structured = llm.with_structured_output(out_model)
    prompt = _build_prompt(message, domains, labels)
    result = await structured.ainvoke(prompt)

    plan = IntentPlan(
      query_type=result.query_type,
      domains=list(result.domains or []),
      intent_labels=list(result.intent_labels or []),
      subtasks=list(result.subtasks or []),
      focus=result.focus or '',
    )
    logger.info(
      'plan_query type=%s domains=%s labels=%s subtasks=%d',
      plan.query_type, plan.domains, plan.intent_labels, len(plan.subtasks),
    )
    return plan
  except Exception as e:
    logger.warning('plan_query 失败，退化为 detect 空 plan: %s', e)
    return IntentPlan(query_type='detect', focus=message)

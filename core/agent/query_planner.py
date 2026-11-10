"""意图识别器 —— 多标签编排的 Step 1（LLM 结构化输出）。

用一次快速 LLM 调用，把用户问题解析成结构化的 IntentPlan：
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


@dataclass
class IntentPlan:
  """意图识别结果（内部稳定契约，与 LLM 动态输出 schema 解耦）。

  Attributes:
    domains: 命中的平台名列表（如 ['dcim', 'mgmt']）。
    intent_labels: 命中的意图标签列表（闭合词汇表内）。
    subtasks: 拆解出的子问题描述，自由文本。
    focus: 用户核心诉求，自由文本。
  """

  domains: list[str] = field(default_factory=list)
  intent_labels: list[str] = field(default_factory=list)
  subtasks: list[str] = field(default_factory=list)
  focus: str = ''


def _build_output_model(domains: list[str], labels: list[str]) -> type[BaseModel]:
  """按当前注册表状态动态构造结构化输出模型。

  domains/labels 非空时用 Literal 锁死值域；为空时退化为 list[str]（Literal
  不允许空枚举），此时靠代码层过滤兜底。
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
    domains=(domain_type, Field(default_factory=list, description='涉及的平台')),
    intent_labels=(label_type, Field(default_factory=list, description='命中的意图标签')),
    subtasks=(list[str], Field(default_factory=list, description='拆解出的子问题，自然语言')),
    focus=(str, Field(default='', description='用户核心诉求一句话概括')),
  )


def _build_history_block(history: dict | None) -> str:
  """构造最近对话上下文段（中性事实：近几轮用户问题 + 工具名）。

  仅供意图识别判断「当前问题是否在延续上一轮」（省略追问如「剩余数据」）；不含任何
  意图断言，避免污染。无历史或内容为空时返回空串。
  """
  if not history:
    return ''
  parts: list[str] = []
  umsgs = history.get('user_messages') or []
  tools = history.get('tools') or []
  if umsgs:
    parts.append('近几轮用户问题：\n' + '\n'.join(f'- {m}' for m in umsgs))
  if tools:
    parts.append('最近使用过的工具：\n' + '\n'.join(f'- {t}' for t in tools))
  if not parts:
    return ''
  return (
    '\n\n【最近对话上下文（仅作中性参考）】\n'
    '下面是此前几轮的用户问题与用过的工具，用于判断当前问题是不是省略了主语的延续追问'
    '（如「剩余数据」「还有呢」）。\n'
    + '\n'.join(parts)
  )


def _build_prompt(message: str, domains: list[str], labels: list[str], history: dict | None = None) -> str:
  """构造意图识别提示词，把合法值域显式列进 prompt（prompt 层约束）。

  平台/标签注入中文语义（复用 manifest 的 _PLATFORM_LABELS/_CATEGORY_LABELS），
  避免 LLM 面对 mgmt/org/skill 这类英文缩写无法理解而误判（如「制冷专业人员」
  被猜成 skill 而非 org）。规则区强调按语义归类，勿按字面。
  """
  from service.query.base.manifest import (
    build_category_manifest,
    build_label_hints,
    platform_definition,
    platform_display_name,
  )

  # 每个平台带**定位**一句 —— 多个平台功能语义重叠时（如综合管理平台与 DCIM 都有"设备"），
  # 只给平台名 LLM 分不清该问谁；给出"这个平台是干什么的"它才选得对。
  domain_hint = '\n'.join(
    f'     - {d}（{platform_display_name(d)}）：{platform_definition(d)}'
    for d in domains
  ) if domains else '     （暂无已注册平台）'

  if labels:
    hints = build_label_hints(labels)
    label_lines = '\n'.join(f'     - {label}（{hints[label]}）' for label in labels)
    label_hint = f'\n{label_lines}'
  else:
    label_hint = '（意图词汇表尚未定义，intent_labels 请留空）'

  manifest = build_category_manifest()
  history_block = _build_history_block(history)

  return (
    '你是运维 Agent 的意图识别器。分析用户问题，输出结构化意图。\n\n'
    '规则：\n'
    f'1. domains 只能从以下平台中选（**按各平台的"定位"判断这个问题该问谁**，可多选）：\n'
    f'{domain_hint}\n'
    f'2. intent_labels 只能从以下标签中选：{label_hint}\n'
    '3. 按语义归类，勿按字面猜：如「制冷专业人员」属组织人事（org），不是技能（skill）\n'
    '4. subtasks：把问题拆成可执行的子问题（自然语言，可多条）\n'
    '5. focus：一句话概括用户到底想要什么\n'
    '6. 若当前问题是延续上一轮的省略追问（本身不含明确对象/类别，如「剩余数据」「还有呢」），'
    '结合【最近对话上下文】判断它指代哪个工具/类别；若当前问题能独立说明要查什么，'
    '以当前问题为准，忽略最近上下文。\n'
    + history_block
    + (f'\n平台能力分类参考（用于理解各平台/标签含义）：\n{manifest}\n' if manifest else '')
    + f'\n用户问题：{message}'
  )


async def plan_query(message: str, history: dict | None = None) -> IntentPlan:
  """对用户问题做意图识别，返回结构化 IntentPlan。

  失败（LLM 异常/无平台）时返回 detect 型空 plan，使编排层走 tool_search
  fallback，不阻断主链路。

  Args:
    message: 用户消息。
    history: 最近对话上下文 {'user_messages': [...], 'tools': [...]}，供省略/
      延续追问（如「剩余数据」）判断指代哪个工具/类别；完整独立查询以当前问题为准。

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
    prompt = _build_prompt(message, domains, labels, history=history)
    result = await structured.ainvoke(prompt)

    plan = IntentPlan(
      domains=list(result.domains or []),
      intent_labels=list(result.intent_labels or []),
      subtasks=list(result.subtasks or []),
      focus=result.focus or '',
    )
    logger.info(
      'plan_query domains=%s labels=%s subtasks=%d',
      plan.domains, plan.intent_labels, len(plan.subtasks),
    )
    return plan
  except Exception as e:
    logger.warning('plan_query 失败，退化为空 plan（走 tool_search）: %s', e)
    return IntentPlan(focus=message)

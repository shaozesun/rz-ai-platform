"""计划生成器 —— 计划模式（plan mode）的执行计划产出。

计划模式下，用户发消息后先不调工具，而是用一次 LLM 结构化调用产出「执行计划」
（目标 + 步骤列表），前端渲染成可编辑步骤卡片，用户确认后才真正执行。本模块只负责
生成计划，不执行任何工具。

计划基于 build_category_manifest() 的可用能力分类清单生成，让步骤可落地到实际
能力（而非空泛步骤）；LLM 失败时兜底返回单步计划，不阻断主链路（对齐
query_planner.plan_query 的兜底风格）。
"""

import logging

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class PlanStep(BaseModel):
  """执行计划中的单个步骤。"""

  title: str = Field(..., description='步骤标题（简短）')
  description: str = Field(..., description='这步做什么 / 查什么 / 产出什么')


class ExecutionPlan(BaseModel):
  """用户可见的执行计划。"""

  goal: str = Field(..., description='一句话概括本次任务目标')
  steps: list[PlanStep] = Field(..., description='按执行顺序排列的步骤列表')


class InterviewOption(BaseModel):
  """需求访谈中的单个候选选项。"""

  value: str = Field(..., description='选项回传值（简短，如 A区/上月/pdf）')
  label: str = Field(..., description='选项展示文本')
  recommended: bool = Field(False, description='是否推荐该选项（每个问题最多一个推荐）')


class InterviewQuestion(BaseModel):
  """需求访谈中的一个待确认问题。"""

  id: str = Field(..., description='问题唯一标识（如 scope/range/device/format）')
  question: str = Field(..., description='问题文本')
  options: list[InterviewOption] = Field(..., description='候选选项，2~4 个')


class PlanInterview(BaseModel):
  """计划前需求澄清的结构化输出。"""

  needs_clarification: bool = Field(..., description='是否需要对用户澄清后才能产出计划')
  questions: list[InterviewQuestion] = Field(
    default_factory=list, description='需要澄清的问题列表（needs_clarification=true 时非空）',
  )


def plan_to_markdown(plan: 'ExecutionPlan') -> str:
  """把执行计划渲染成 markdown 文本（计划轮落库展示用）。"""
  lines = ['**执行计划**', '', f'目标：{plan.goal}', '']
  for i, step in enumerate(plan.steps, 1):
    lines.append(f'{i}. **{step.title}**：{step.description}')
  return '\n'.join(lines)


def _build_prompt(message: str, manifest: str, answers: dict | None = None) -> str:
  """构造计划生成提示词，附可用能力分类清单约束步骤可落地。

  answers: 访谈轮已确认的约束（{问题 id: 用户选择}），规划时强制遵循。
  """
  manifest_block = manifest if manifest else '（暂无已注册平台能力）'
  confirmed = ''
  if answers:
    lines = '\n'.join(f'- {k}：{v}' for k, v in answers.items())
    confirmed = (
      '\n\n已确认约束（规划步骤时必须遵循，不要重复确认）：\n'
      f'{lines}'
    )
  return (
    '你是运维 Agent 的计划制定器。用户准备执行一个任务，请先产出执行计划，'
    '不要调用任何工具、不要执行，只输出计划。\n\n'
    '规则：\n'
    '1. goal：一句话概括用户到底想要什么。\n'
    '2. steps：把任务拆成 2~6 个可执行的步骤，按先后顺序排列。\n'
    '3. 每个步骤 title 简短（≤12 字），description 说清这步查什么数据 / 做什么分析 / '
    '产出什么，用完整句子并以句号结尾。\n'
    '4. 步骤要基于下方「可用能力清单」组织，落到实际能查的能力上，不要空泛。\n'
    '5. 涉及数据拉取→统计分析→图表→报告（文件）这类链路时，明确拆出每一步。\n'
    '6. 简单问题 1 步即可，不要为了凑步骤强行拆分。\n'
    f'7. 必须完整输出全部步骤，禁止截断。\n\n'
    f'可用能力清单：\n{manifest_block}\n'
    f'{confirmed}\n\n'
    f'用户问题：{message}'
  )


def _build_interview_prompt(message: str, manifest: str) -> str:
  """构造需求澄清提示词：产出「是否需要澄清 + 待确认问题列表」。"""
  manifest_block = manifest if manifest else '（暂无已注册平台能力）'
  return (
    '你是运维 Agent 的需求澄清器。用户准备执行一个任务，但在开始前可能有几个'
    '不明确的、可选的选择维度（例如：范围/机房/设备、时间范围、统计维度、'
    '产出形式（图表/报告/看板）等）。\n\n'
    '判断规则：\n'
    '1. 不澄清也能直接给出靠谱计划 → needs_clarification=false，questions 留空。\n'
    '2. 存在影响计划走向的不确定维度 → needs_clarification=true，列出必须确认的 1~4 个问题。\n'
    '3. 每个问题给 2~4 个具体选项，且只能有一个 recommended=true（基于用户上下文的最优默认）。\n'
    '4. options 的 value 简短（如 A区/上月/pdf），label 完整可读。\n'
    '5. 用户消息里已明确的信息不要再问。\n'
    '6. 只输出结构化结果，不要任何多余文字。\n\n'
    f'可用能力清单：\n{manifest_block}\n\n'
    f'用户问题：{message}'
  )


# 截断的强信号：描述以「续写字符」结尾（连接词/助词/逗号），正常完整句子以句号等收尾
_TRUNC_TAIL = set('，、；,;的以将并在中与和了着')

def _looks_truncated(plan: ExecutionPlan) -> bool:
  """启发式判断计划是否被截断：单步且描述以续写字符结尾 → 疑似截断。

  截断是 LLM 输出提前终止的间歇性问题（HTTP 200 合法 JSON，但内容不完整），
  schema 校验抓不到，只能靠内容形态兜底（实测截断例「调用 mgmt 文档能力，以」
  即以助词「以」收尾）。多步骤正常计划、完整句子不被误判。
  """
  if not plan.steps:
    return True
  if len(plan.steps) > 1:
    return False
  desc = (plan.steps[0].description or '').strip()
  if not desc:
    return False
  return desc[-1] in _TRUNC_TAIL


async def generate_plan(
  message: str,
  manifest: str,
  answers: dict | None = None,
) -> ExecutionPlan:
  """对用户问题生成结构化执行计划。

  Args:
    message: 用户消息。
    manifest: 可用能力分类清单（build_category_manifest() 的返回值）。
    answers: 访谈轮已确认的约束（{问题 id: 用户选择}），None 表示未经过访谈。

  Returns:
    ExecutionPlan；LLM 异常时兜底返回单步计划。
  """
  from core.model_gateway import model_gateway

  prompt = _build_prompt(message, manifest, answers)
  try:
    llm = model_gateway.as_langchain_model()
    structured = llm.with_structured_output(ExecutionPlan)
    plan = await structured.ainvoke(prompt)
    if _looks_truncated(plan):
      # 截断重试：0 温度 + 明确「完整输出」要求，仍失败则接受结果（兜底在下方）
      logger.info('generate_plan 疑似截断，重试一次（temperature=0）')
      retry = model_gateway.as_langchain_model(temperature=0.0).with_structured_output(ExecutionPlan)
      retry_prompt = prompt + '\n\n注意：上轮输出被截断。本轮必须完整输出全部步骤，每个 description 用完整句子并以句号结尾，禁止截断。'
      plan = await retry.ainvoke(retry_prompt)
    if not plan.steps:
      plan = ExecutionPlan(
        goal=plan.goal or message,
        steps=[PlanStep(title='直接执行', description=message)],
      )
    logger.info('generate_plan goal=%s steps=%d', plan.goal, len(plan.steps))
    return plan
  except Exception as e:
    logger.warning('generate_plan 失败，兜底单步计划: %s', e)
    return ExecutionPlan(
      goal=message,
      steps=[PlanStep(title='直接执行', description=message)],
    )


async def generate_interview(message: str, manifest: str) -> PlanInterview:
  """对用户问题做需求澄清：产出待确认问题列表（或判定无需澄清）。

  Args:
    message: 用户消息。
    manifest: 可用能力分类清单（build_category_manifest() 的返回值）。

  Returns:
    PlanInterview；LLM 异常时视为无需澄清（agent_service 会退化为直接生成计划）。
  """
  from core.model_gateway import model_gateway

  try:
    llm = model_gateway.as_langchain_model(temperature=0.2)
    structured = llm.with_structured_output(PlanInterview)
    prompt = _build_interview_prompt(message, manifest)
    interview = await structured.ainvoke(prompt)
    # 修复：needs_clarification=true 却没产出问题 → 视为无需澄清，避免死循环
    if interview.needs_clarification and not interview.questions:
      logger.warning('generate_interview 声明需澄清但无问题，降级为无需澄清')
      interview = PlanInterview(needs_clarification=False)
    logger.info(
      'generate_interview needs=%s questions=%d',
      interview.needs_clarification, len(interview.questions),
    )
    return interview
  except Exception as e:
    logger.warning('generate_interview 失败，视为无需澄清: %s', e)
    return PlanInterview(needs_clarification=False)

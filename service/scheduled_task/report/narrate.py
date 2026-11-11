"""报告的解读文字 —— 全部 LLM 工作都在这里（**无工具、单轮、可降级**）。

设计要点：
- **数字全部由平台算好**（`stats.json` + `extras`），提示词明令不得修改/新增/推算任何数字；
  模型只负责「异常点 → 可能原因 → 可执行的改进建议」这类**解读**。
- **分章调用**（2026-10-15 改）：原来一次调用写 8 章，`max_tokens=3000` 还要分给摘要与行动计划，
  结果每章只剩一两句（用户实测反馈"图下面没有说明"）。现在：
  ① 一次调用写「执行摘要 + 行动计划」；② 每章一次调用，各自 token 充足、只带本章数据与写作要求。
  变成 9 次调用（对比 agent 路径的 ~25 次），报告耗时约 40s → 2 分钟。
- **多喂聚合料**（`stats['extras']`，几百字符）：只给总量与占比时模型写不出归因，只能给空话。
- **失败不影响报告**：任何一次调用失败只让那一章缺文字（渲染层给占位），PDF 照出。
"""

import json
import logging
import re

from service.scheduled_task.report.facts import build_facts, impact_level, is_weekly
from service.scheduled_task.report import (
  compare_guard, meta_guard, name_guard, speculation_guard, trend_guard,
)
from service.scheduled_task.report.causes import (
  CAUSE_KEYWORDS, CHANGE_CAUSE_KEYWORDS, QUESTION_CAUSE_KEYWORDS,
  THRESHOLD_CAUSE_KEYWORDS, classify,
)
from service.scheduled_task.report.common import (
  DEPT_TABLE_LIMIT, _human_minutes, _volume_verdict,
)
from service.scheduled_task.report.compare_guard import collect_deltas, collect_numbers

try:
  # 繁简归一（纯 Python，无编译依赖）。缺依赖时**不得让报告失败**，退化为直通 + 一次告警。
  from zhconv import convert as _zh_convert
except ImportError:              # 镜像未重建 / 本地未装
  _zh_convert = None

logger = logging.getLogger(__name__)

_NUM_RE = re.compile(r'\d+(?:\.\d+)?')
# 结构性数字（不是数据）：百分位标签、图号、章节号、0（"0 起"是合法表达）
_STRUCTURAL = {'0', '50', '90', '95',
               '1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11', '12', '13'}

# 模型偶发把中文术语译回英文（2026-10-27 实测：「高等级」→「高 Severity」、
# 「流程负责人」→「流程 Owner」）。确定性归一，且**必须在 digest 之前**做 ——
# 执行摘要与行动计划吃的是各章要点原文，一处英文会被复述三次。
_TERM_FIXES = (
  (re.compile(r'\s*[Ss]everity\s+[Ll]evel\s*'), '严重程度'),
  (re.compile(r'\s*[Ss]everity\s*'), '等级'),
  (re.compile(r'\s*[Oo]wner\s*'), '负责人'),
)

# 正文里允许出现的拉丁词（专有名词/通用缩写），其余视为中英混排（只告警，不改文本）
_LATIN_ALLOW = {'UPS', 'SLA', 'AZ', 'IT', 'ID', 'PDF', 'HTML', 'CSV', 'vs', 'Ping'}
_LATIN_RE = re.compile(r'[A-Za-z]{2,}')


# 生僻书面语换常用词（2026-10-29 用户指出报告里出现「亟待提升」，这类字看着像繁体/生僻字）。
# 长词在前，否则先被单字规则吃掉；只换字词、不碰数字，不影响两道数字闸。
_PLAIN_FIXES = (
  (re.compile(r'亟待|亟需|亟须'), '急需'),
  (re.compile(r'亟'), '急'),
)

_ZH_WARNED = False


def _to_simplified(text: str) -> str:
  """繁体→简体（zhconv）。缺依赖时只告警一次、原样返回 —— 解读不能因此失败。"""
  global _ZH_WARNED
  if not text:
    return text
  if _zh_convert is None:
    if not _ZH_WARNED:
      logger.warning('zhconv 未安装：报告解读的繁体字不会归一（pip install zhconv）')
      _ZH_WARNED = True
    return text
  return _zh_convert(text, 'zh-cn')


def _normalize_terms(text: str) -> str:
  """把模型夹带的英文运维术语归一回中文（连同紧邻空格，避免「高 等级 风险」），
  并把生僻书面语换成常用词。"""
  out = str(text or '')
  for pattern, repl in _PLAIN_FIXES:
    out = pattern.sub(repl, out)
  for pattern, repl in _TERM_FIXES:
    out = pattern.sub(repl, out)
  return out


def _audit_latin(text: str) -> None:
  """中英混排自检（**只记日志、不拦报告**）—— 词表没覆盖的新混排词靠它被发现。"""
  extras = sorted({w for w in _LATIN_RE.findall(text or '') if w not in _LATIN_ALLOW})
  if extras:
    logger.warning('报告解读里出现英文术语（疑为中英混排）: %s', extras[:20])


# 句式模板化审计：模型会把提示词里的一个词整章复述（2026-10-27 实测 29/75 条以「判定为」收尾）。
# 「判定」单独不列，避免与「判定为」重复计数。
_TEMPLATE_WORDS = ('判定为', '属于', '表明', '显示', '反映', '呈现为', '可视为')


def _template_repeats(text: str) -> dict[str, int]:
  """统计固定引导词的出现次数（纯函数，供自检直接断言）。"""
  return {w: n for w in _TEMPLATE_WORDS if (n := str(text or '').count(w))}


def _audit_template(text: str, label: str = '') -> None:
  """句式模板化自检（**只记日志、不改写文本**）—— 靠概率改不掉的重复，先在日志里暴露。"""
  hot = {w: n for w, n in _template_repeats(text).items() if n >= 3}
  if hot:
    logger.warning('报告解读：%s 句式模板化（同一引导词重复）: %s',
                   label or '正文', '，'.join(f'「{w}」×{n}' for w, n in hot.items()))

_MAX_SUMMARY = 8
# 改进方向是「有就写、没有就不写」：上限 3 条（2026-10-27 从 9 收紧——上限 9 时模型必然凑满）
_MAX_ACTIONS = 3
# 周报详情的解读预算：条数、单字段字数、总字符上限（超出即停止追加，首条必留）
_WEEKLY_DETAIL_ROWS = 30
_WEEKLY_DETAIL_CHARS = 150
_WEEKLY_DETAIL_BUDGET = 8000
_JSON_MODE_OK = True      # 上游若不支持 response_format，`_call` 首次失败后自动置 False
# 只认这三档（渲染按这三档分组、P0 用橙黄标记）；模型给别的值一律丢弃
_PRIORITIES = ('P0', 'P1', 'P2')

# 整份报告的定位（2026-10-27 用户反馈）：是**周期性运行评价**，不是整改方案。
# 三条链路的 system 提示词都要带这一段 —— 缺一处，那一处就退回"给每个维度凑一条建议"。
_EVAL_FRAME = (
  '本报告是**周期性运行评价**：读者要判断"这段时间运行得怎么样"，不是要一份整改方案。\n'
  '评价 = 事实 + 水平/方向（改善 / 持平 / 恶化）+ 依据；'
  '数据不足以支撑结论时，就如实说明数据不足，**不要补一个解释**。\n'
)

_RULES = (
  '硬性规则：\n'
  '1. **不得修改、新增、推算任何数字**。你写到的每个数字都必须与给定 JSON 完全一致；'
  '没有的数字一个字都不要出现（例如"持续时长""误报率"数据里根本没有）。'
  '**P50 / P90 / P95 与「均值/平均值」是不同口径，不得互称**（把 P50 写成"平均"或反之都算错），'
  '引用时照抄 JSON 的键名。\n'
  '2. **不要做任何单位换算或四则运算**（分钟→小时、两数相除得倍数或合计占比、求和相减……都不许）。'
  '给定 JSON 里的时长已经是**可读串**（超过 60 分钟即小时，如「32.6 小时」），照抄即可，'
  '不要改写成分钟；需要新角度时换一种说法。\n'
  '   （实测教训：模型把"P95=3937.9 分钟"写成"超过 65 分钟"——实为 65.6 小时，'
  '这种单位错误会直接毁掉报告可信度。）\n'
  '3. 不要复述表格里的数字罗列，要写**表格说不出来的判断**。\n'
  '   跨期比较**只能引用 JSON 里已给出的变化字段**（「单量变化（本期−上期）」「占比变化'
  '（百分点，本期−上期）」）；没有这些字段就是没有可比数据，不要自己相减，'
  '更**不得拿本期占比去减上期单量**（两者量纲不同）。\n'
  '   **「验收时长」与「影响时长」是两个口径**（前者＝流程走完用了多久，后者＝业务受影响'
  '多久），**不得混用、不得加总或互相换算**，也不得用其中一个去推断另一个。\n'
  '4. 确需给改进方向时要落到**可执行动作 + 责任维度**，不要写"加强管理"这类空话；'
  '没有数据依据就不要提动作。\n'
  '5. **全篇使用简体中文与常用词**：不得出现繁体字，不要用生僻书面语'
  '（改用「急需」「有待」这类常用词）；不得夹用英文（如 Severity / Owner / level），'
  '专有名词（UPS、SLA、P50/P95、A1/A2 分部、M1-204 机房编号）除外。\n'
  '6. 只输出 JSON，不要 markdown 代码块、不要任何解释文字。'
)

_ACTION_SYSTEM = (
  '你是数据中心运维分析报告的文字撰写助手。你会收到统计 JSON（含**各章要点**、可归因切片、'
  '高频问题详情）。\n'
  + _EVAL_FRAME +
  '本章是「综合评价与改进方向」：\n'
  '  · `总体评价`：4~6 句，**先写积极方向、再写需关注方向** —— 前 2~3 句点名本期'
  '达标或改善的维度（可引用给定 JSON 里已有的指标与数字，**照抄不换算**；章首'
  '「本期整体表现」已写过的可换角度展开，不要整句复述），**不得新增表外的成绩**；'
  '再用 1~2 句说明最需要关注的方向（如实写明改善 / 持平 / 待改进；单量扩大写「规模扩大」，'
  '不写「恶化」）；末句可给整体定位。'
  '本期确实没有可写的达标项时才略过积极句，不要硬夸；\n'
  '  · `行动计划`：**只写确有数据证据的改进项，按优先级分档，0~3 条（可以是 0 条）** ——'
  '没有需要专项干预的项就留空数组，**不得凑数**；各章已写明为正向（改善/达标）的维度不要硬凑动作。\n'
  '  {TIMEBUX}；优先级**只允许 P0 / P1 / P2**。\n'
  '  **每条必须是「体系化动作」**——专项治理 / 机制 / 标准 / 规则 / 资源保障 / 平台或流程建设；\n'
  '  **禁止写成"对某部门某 N 单逐单清理"这类工单级执行项**：那是日常工单管理，不是行动计划。\n'
  '  可按需覆盖三类视角（不必凑满）：① 专业/设备体系（专项排查、预防性维护）'
  '② 规则与数据质量（告警阈值与去重规则、级别/关闭时间填报标准）'
  '③ 机制与能力（园区对标、备件与供应保障、平台或流程建设）。\n'
  '  注意与各章要点衔接：行动计划要针对**各章的负向发现**给动作，不要另起炉灶。\n'
  '  数据里若带「风险分析」：改进项**优先对应其中等级为 高/中的风险项**（写明对应哪一项、'
  '按它给的排查方向落地成体系化动作），不要另提表外的动作；风险分析为空时通常也不该有动作。\n'
  '  数据里若带「本期运行亮点」：只点名正文与「本期整体表现」**都还没覆盖的维度**'
  '（数字照抄，不换算）；**不得新增表外的成绩**。\n'
  '  体系化动作写完后，**用括号附本期数据锚点**作为依据（数字只能取自给定 JSON）；'
  '成因要有证据（取自高频问题详情/成因归类，注意「成因归类」是**按单**计数），'
  '不要从事件名猜。\n\n'
  + _RULES + '\n\n'
  '输出 JSON：{"总体评价": "...", '
  '"行动计划": [{"优先级": "P0|P1|P2", "行动项": "...", "责任维度": "...", "时限": "..."}]}'
)

# 行动计划时限按周期生成（月报文案保持原样）：只替换这一句，正文里的 JSON 花括号不受影响。
_ACTION_TIMEBUX = {
  False: 'P0 立即执行（即刻~1 周内）／P1 本月完成／P2 下季度完成',
  True: 'P0 立即执行（3 天内）／P1 本周完成／P2 下周完成',
}


def _action_system(weekly: bool) -> str:
  return _ACTION_SYSTEM.replace('{TIMEBUX}', _ACTION_TIMEBUX[weekly])

# 执行摘要**最后**生成：输入是各章要点 + 行动计划 —— 这才是"分析完之后提炼的本篇要点"。
# 之前它是最先跑的（那时各章还没生成），手上只有总量统计 → 只能复述卡片，这是结构问题不是措辞问题。
_SUMMARY_SYSTEM = (
  '你是数据中心运维分析报告的文字撰写助手。你会收到**各章的分析要点**、**行动计划**与总体统计。\n'
  + _EVAL_FRAME +
  '请把它们提炼成「执行摘要」——**本篇的关键发现**（面向管理层），4~6 条。'
  '写成本期**运行评价**（总体表现 + 主要结构与变化），不是整改清单；'
  '没有负向发现就如实写"运行平稳"，不需要找出问题：\n'
  '  · 每条 = 一个**结论**（改善 / 恶化 / 结构性特征 / 因果），**如实写明方向**；'
  '用给定数字支撑结论。\n'
  '    **本期改善的指标要明确指出**（如"验收超时率降至 8.3%，为近几期最好"）——'
  '报告是客观记录，不是问题清单，不要只挑问题说。\n'
  '  · **不要复述指标卡片**：总量、级别/专业/园区的占比已在第 2 章卡片里展示，摘要里不再念一遍。\n'
  '  · 要**跨章提炼**：挑最值得管理层先知道的（如"验收超时率降至…""积压卡在验收环节而非现场处置"'
  '"同一机房反复出现同一告警"）。\n'
  '  · 不要与行动计划重复：行动计划是"要做什么"，摘要是"发现了什么"。\n'
  '  · 第 1 条可一句话交代范围与总量作背景，其余每一条都必须是发现。\n'
  '  · **不得把单量下降解读为数据问题**：数据里没有缺失/异常证据时，下降就是下降'
  '（见「综合评分」的规模控制口径）。\n'
  '  · 数据里若带「风险分析」：点出**本期最高风险等级**与**主要风险项（最多 2 项）**；'
  '没有达到中风险阈值就不提风险，也不要重复表里的判据。\n'
  '  · 数据里若带「本期运行亮点」：4~6 条里**至少 1 条写明本期表现好/改善的维度**'
  '（数字照抄），且**不得自行新增表外的亮点**；亮点与风险并存时两条都要说。\n\n'
  + _RULES + '\n\n'
  '输出 JSON：{"执行摘要": ["..."]}'
)

_SECTION_SYSTEM = (
  '你是数据中心运维分析报告的文字撰写助手，**只写一章**的解读。\n'
  + _EVAL_FRAME +
  '本章要求：{brief}\n'
  '写作规则（务必遵守）：\n'
  '1. **分点写，一条一句话**，禁止堆成一大段。\n'
  '2. 每条 = **现象（带数字）+ 评价（方向或水平）**（改善/恶化/持平/风险）。'
  '**不强制附带动作**：只有数据明确说明需要干预时才补一句改进方向（不写责任人、'
  '不写工单级执行项）；该维度正常或持平时就写正常/持平，**不必硬凑动作**，也不必凑一个"风险"。\n'
  '   **不要每条都套同一句式**（如逐条以「判定为…」收尾），同一章里同一个引导词最多出现 2 次，'
  '换用「显示 / 表明 / 反映 / 属于 / 呈现为」等自然表述——重复的模板句读起来生硬。\n'
  '3. **特征落到园区或部门**：「统计范围」是全国的，写各园区/各部门的差异；'
  '范围是单个园区的，写各**部门**的差异。**差异真实存在时才点名**，没有差异就写'
  '「各园区表现接近」，不要为了凑一条差异去找园区。**各园区体量不同（设备台数、机房数量各异），'
  '绝对单量不可互比高低**；只比占比结构、级别构成与各自环比。\n'
  '4. **本章每张图都要给 1~2 条「图表解析」**（JSON 键写 `图说明`）：**直接下判断，不要描述图形**'
  ' —— 禁止"该图/如图/图中/图显示/图表显示/可见"这类说法，也别写颜色、条柱长短、象限位置、'
  '堆叠比例这些读者一眼能看到的东西。每条按「现象（带数字）→ 与基准对比（上一期 / 各园区或部门'
  '均值 / 与其他园区或部门的差异）→ **写明方向**（改善/恶化/持平）→ 仅当负向且有依据时'
  '补一句改进方向」组织，'
  '且**不得与「本章要点」重复** —— 解析只写这张图单独才说得清的差异。本章没有图时给空对象。\n'
  '5. **本章数据里若有「数据事实」段，每一句都必须写明依据**：要点与图表解析的每一条都写成 '
  '{{"文本": "…", "依据": ["F3"]}}；「依据」只能取自本章「数据事实」的编号（形如 F1、F2），'
  '或本章数据的键名；**每一条都必须能指回上述编号（平台会逐句核对留档）** —— 写不出依据就别写这一句。'
  '**本章数据里没有「数据事实」段时（质量/重复/根因等章），无需写依据，'
  '直接给结论句字符串即可**。\n'
  '6. 数字只能引用给定 JSON 里的，不得换算或推算。**跨期比较只能引用 JSON 里已给出的'
  '「单量变化（本期−上期）」「占比变化（百分点，本期−上期）」**；没有这些字段说明没有'
  '可比数据。不得拿本期占比减上期单量（量纲不同），也不得把两个占比自行相减后写成 %。\n'
  '7. 要给**原因与方向判断**（改善 / 恶化 / 持平，如"若现场确认不优化，复杂故障下定位可能超'
  ' 10 分钟"），判断必须建立在给定数据之上；数据里没有的字段一律不许出现。'
  '**不得为了凑出风险而怀疑数据本身**：只有数据里明确给出缺失/异常/采集问题的证据时，'
  '才可以提及漏报或采集失效；否则单量下降就是正常结论。'
  '**也不得因为某园区单量小就推断监测粒度粗 / 漏报 / 上报偏保守** —— '
  '平台没有设备台数等规模口径，分不清"体量小"和"上报少"。\n'
  '   **不要写「本章缺少…」「数据未提供…」「无法判断…」这类说明** —— 报告不提供的项'
  '统一在末章「数据说明」列出，本正文只讲已有的数据。\n'
  '8. **全篇使用中文术语**，不得夹用英文（如 Severity / Owner / level）；'
  '专有名词（UPS、SLA、P50/P95、A1/A2 分部、M1-204 机房编号）除外。\n'
  '9. 只输出 JSON，不要 markdown 代码块。\n'
  '\n'
  '输出 JSON：{{"要点": [{{"文本": "3~5 条本章结论", "依据": ["F1"]}}], '
  '"图说明": {{"<本章图表的键>": [{{"文本": "1~2 条", "依据": ["F2"]}}]}}}}'
  '（无「数据事实」的章：条目可直接给字符串、省略「依据」）'
)

# 每章的写作要求（**键＝spec.roles 里的角色**，不是章号）—— 角色相同的章两类共用。
# `{level_label}`/`{duration_label}`/`{quality_metrics}` 由 narrate 用 spec 插值。
_BRIEFS: dict[str, str] = {
  'trend': '规模与趋势分析：总量、环比增减、日峰值与低谷。**先如实写明本期相对上一期是收缩、'
           '扩大还是持平**（单量下降是正向，见本章「单量方向」口径），再说清波动特征；'
           '单量扩大属规模控制扣分项，如实写「规模扩大」即可，**不要写成「恶化 / 失控」**'
           '——那类措辞只留给超时率、紧急占比、未关闭这类有证据的负向信号。'
           '原因只在数据支持时给（如负载/巡检节奏），需要盯的时段只在出现负向信号时提。'
           '**任何两天之间的大小关系、增减量、倍数，只能直接引用本章「数据事实」中的原句'
           '（逐日对位句、排序句、峰谷句）；不得自行比较任意两天，不得做减法或倍数换算。**',
  'level': '{level_label}分布，以及**级别 × {level_duration_label}**的联合情况：各级数量与占比、'
           '各级{level_duration_label}分位、等级段（1-2 级 vs 3-4 级）的差异。要说清分布形态'
           '（集中/两极化），以及**高等级单的{level_duration_label}是否更长**（级别数字越小等级'
           '越高）；判断只依据本章「数据事实」里的对比句与分位句，不自行比较、不做减法。'
           '**数据支持时才指出异常，否则如实写分布形态**，不要预设有问题，也不给处置能力评级。'
           '若本章与「处理时效」章同时出现影响时长与验收时长：两者**分开陈述、不混用、不换算**'
           '（高低不一致本身是信息）。',
  # 基础文案不带维度名：本章的堆叠图有的是「园区 × 级别」（故障/变更），有的是
  # 「园区 × 专业」（事件/问题）—— 具体维度由装配处按 spec 声明追加，避免写成另一种图的解读。
  'park': '园区横向对比：各园区数量、占比与环比增减（**只能引用给好的「单量变化」'
          '「占比变化（百分点）」**），以及**各园区构成**（横向堆叠条：每个园区一条横条，'
          '横向看结构差异）。'
          '**体量不同的园区不互比绝对单量**（设备台数与机房数量各异，单量低不等于有问题、'
          '更不等于漏报）；比较**占比结构、构成与各自环比**，如实说明哪个园区的变化方向不同、'
          '哪个园区同档占比明显更高；没有明显差异就写「各园区表现接近」。',
  'specialty': '专业分布：各专业数量与占比、与上一期的变化（**只能引用给好的「单量变化」'
               '「占比变化（百分点）」，不要自己相减**）。要说清最集中的专业及其典型问题，'
               '以及跨专业协同点。',
  'equipment': '专业与设备类型分布：各专业数量与占比，以及**故障最集中的设备类型**'
               '（帕累托图：哪几类设备贡献了大部分故障；散点图：横轴＝故障单量、纵轴＝处理时长 P50，'
               '右上象限＝"高频且高耗时"，最值得关注）。要说清 Top 设备类型指向什么'
               '（是共性短板还是常态磨损）、专业集中度说明什么；数据支持时才提维护方向。',
  'dept': '故障部门分布与环比：故障单量最多的部门/分部及其占比，以及**各部门环比增减**'
          '（发散条形：本期 − 上期，增为橙、减为绿）。要说清故障在部门上的集中度、'
          '**哪些部门在恶化/哪些在改善**；差异只在数据支持时才展开，没有恶化就如实写改善/持平。',
  'risk': '工单质量与现存风险：超 1 天未处理完占比、驳回率、关闭率，以及现存风险清单'
          '（未关闭 / 长时间未处理 / 重复故障三类）。要说清风险规模、哪类风险的处置更紧迫、'
          '涉及哪些代表工单；数据支持时才指出先处理哪一类。',
  'duration': '处理时效分析（口径＝**{duration_label}**）：P50/P90/P95、超时/超期单数、最慢部门、最长单。'
              '要说清卡在哪一档、哪些部门最慢、是现场处置慢还是流程环节积压。'
              '数据里若另有「影响时长」（详情口径）：说明**业务影响的持续时间**分布'
              '（多数单影响多久、最长多久、各级别差异），与验收时长**分开陈述、不混用、'
              '不换算**；两者高低不一致本身是信息（如验收慢但影响短）。',
  'quality': '工单质量分析（仅 {quality_metrics} 三项）：说明各项指标的实际水平与较上一期的变化，'
             '**本期优于上一期时如实说明改善**；确有异常再讲影响。',
  'repeat': '重复性问题聚类：重复次数最高的几类、涉及哪些机房。按重复次数排出前 3 类并说明特征'
            '（是设备/机房的共性，还是个别单的巧合）。'
            '数据里若带「关联告警」（DCIM 告警）：可引用它说明该类重复事件对应哪条告警、'
            '当时的告警值与越限档位；**不得换算单位、不做四则运算**。'
            '未关联到告警时如实写「未关联到 DCIM 告警」，**不得推断为没有告警**'
            '（覆盖范围说明由平台给出）。',
  'root_cause': '高频问题的根因分析：依据给定的详情（原因/处置原文、设备品牌型号等）。'
                '要把根因归类（负载/设备/采集/流程/备件）并归纳共性原因；'
                '取不到详情的如实说明数据不足。'
                '数据里若带「成因归类」「详情质量」「影响时长最长单」：先按**成因分布**说清'
                '本期主要原因类型（按单计数，不要写成关键词命中次数），'
                '再点出**影响时长最长的那几单**处置过程上的共性（如都在等备件、都走了二次派单），'
                '并如实说明填报质量（原因是否笼统/总结是否套话）。'
                '**要整体归纳，不要逐单复述**；某类只有 1 单时不要上升为"普遍现象"。',
  'change_type': '变更类型与紧急度：计划型/非计划型/紧急变更的数量与占比、与上一期的变化'
                 '（**只能引用给好的「单量变化」「占比变化（百分点）」**），'
                 '以及**紧急变更在等级构成与各园区上的集中点**。本章分解图从上一期总量'
                 '**逐类型推到本期总量**：先点名贡献最大的类型（增量主要来自哪类、有没有哪类'
                 '在减少），再讲紧急变更的集中点。要说清非计划与紧急变更占比高'
                 '意味着什么（计划性差 / 审批与排期被动），点名紧急占比最高或恶化的园区；'
                 '数据支持时才提把非计划转计划的方向。',
  'flow': '阶段与状态流转（口径＝**当前快照**，不是流转史）：阶段×状态联合分布、'
          '驳回/撤回/退回/延期中单数、审批与验收时长及超时情况、未关闭积压与卡点。'
          '要说清**卡在哪个环节**（审批期 vs 实施期 vs 验收期）、哪个部门/园区最慢、'
          '是流程环节积压还是现场执行慢；积压按「统计期末 − 计划结束」的天数看。',
  'schedule': '执行时效与偏差（本章是重点）：计划时长与实际时长的差异（含散点图：'
              '横轴计划、纵轴实际、对角线为计划＝实际，点在下方＝实际用时更短）、开始偏差、'
              '按时完成率、延迟分档（提前/按时/延迟各档）、以及延迟集中在**哪类变更、哪个园区**。'
              '要说清变更窗口预估是否准确（计划短于实际＝估时偏乐观）、'
              '哪类变更最容易延期；散点只讲**实际用时相对计划窗口的长短**，'
              '**不等于延期**（延期按完成时间对比计划结束判定）；数据支持时才提排期方向。'
              '「尚未完成实施」的单是**还没执行**（实施/等待期在制），不是数据缺失或采集问题，'
              '不得写成填报缺陷、采集失败或数据缺失。',
  'change_cause': '变更根因与风险控制分析：依据给定的代表单详情原文（变更原因/变更方案/'
                  '风险识别/风险控制措施/回退方案/执行总结）。要把变更原因归类'
                  '（容量扩容/硬件更换/版本配置/线路割接/施工窗口/风险与回退/审批协调），'
                  '评估风险控制措施与回退方案是否**具体可执行**（是否写清了回退触发条件与验证手段），'
                  '并结合「详情质量」说明依据具体的单数、原因笼统与模板化总结的单数，'
                  '对取不到详情的如实说明。',

  # ---- 问题工单专属 ----
  'level_stage': '问题级别与处理阶段：各级别的问题量，以及**每个级别当前卡在哪个阶段**'
                 '（级别×阶段矩阵）。要说清未关闭问题向哪些级别、哪些阶段集中，'
                 '哪些级别长期停在处理或验收环节（数据支持时才指出优先处理对象）。',
  'aging': '存量老化与滞留分析（仅统计**期末仍未关闭**的问题）：滞留时长的分档、P50/P90、'
           '超 {aging_over} 天未关闭规模，以及**各园区/部门的滞留结构差异**与滞留最久的单。'
           '口径是「统计期末 − 问题发生时间」，不是真实解决时长。要说清老化集中在哪个空间、'
           '哪一档，给风险判断（数据支持时才指出优先清理的档位）。',
  'park_specialty': '园区与专业分布：各园区/部门与各专业的问题量、占比与环比变化'
                    '（**只能引用给好的「单量变化」「占比变化（百分点）」**），'
                    '以及**各园区 × 专业构成**（横向堆叠条：各园区的专业结构差异）。'
                    '要点名变化最大的空间、专业集中度，'
                    '如实说明**哪个园区明显偏向某个专业**（没有就写结构接近）。',
  'problem_objects': '问题对象分布：涉及设备/系统 Top10 与字段填写覆盖情况'
                     '（自由文本按标点切分，同一张单的多个对象分别计数，合计可大于单量）。'
                     '要说清问题集中在哪些对象类型、是否存在共性；数据支持时才指出重点关注对象。',
  'entry_lag': '录入及时性（建单时间 − 问题发生时间）：滞后分档、P50/P90、'
               '负滞后与时间缺失的单量。要说清录入是否及时、滞后集中在哪一档、'
               '对存量老化与统计可信度的影响（数据支持时才提填报要求）。',
  'question_cause': '问题根因与整改跟踪：依据给定的代表单详情原文'
                    '（问题描述/处置过程/处置总结）。要把根因归类'
                    '（设备硬件/动力环境/监控告警/配置变更/流程人为/备件供应），'
                    '归纳共性原因；结合「详情质量」说明依据具体/原因笼统/总结疑似复制的单数，'
                    '并对取不到详情的如实说明数据不足。',

  # ---- 阈值工单专属 ----
  'threshold_intent': '调整意图与方向：按固定关键词归类的意图分布、与上一期变化'
                      '（**只能引用给好的「单量变化」「占比变化（百分点）」**）、未识别单量，'
                      '以及**调整意图 × 园区**结构（热力图：哪种意图集中在哪个园区）。'
                      '要说清主流调整方向意味着什么（如大规模加电/下电、关闭屏蔽），'
                      '交叉维度要说清集中在哪个「意图×园区」组合，'
                      '可能存在哪些风险（数据支持时才指出需关注的组合）；未识别如实说明、不做推断。',
  'threshold_scope': '专业与园区分布、发起人集中度：各专业/空间单量，发起人 Top10、'
                     '首位占比与发起人数，以及**主要发起人 × 调整意图**。要说清阈值调整'
                     '是否集中在少数人或少数专业、这些发起人的调整偏好是什么，'
                     '集中带来什么影响（口径不一致、变更失控）；数据支持时才提制度方向。',
  'hot_items': '热点对象与批量调整：调整对象 Top10、名称片段数分档与最大片段数。'
               '要说清是否在做整批调整、热点对象被反复调整意味着什么，'
               '以及批量调整的风险（数据支持时才提管控方向）。口径是名称片段数，不是设备精确台数。',
  'lead_time': '计划提前期分析（计划开始 − 创建）：提前期分档、P50/P90、负值与缺失单量。'
               '要说清提前期是否充足、集中在哪一档、过短意味着什么风险'
               '（审批与准备不足）；数据支持时才提计划纪律方向。',
  'execution': '执行达成情况：计划/实际实施时长的分位、按时执行率、实际开始偏差分档、'
               '各专业按时率。要说清实际是否晚于计划（估时偏乐观/窗口不足）、'
               '哪些专业最差；数据支持时才提窗口评估方向。',
  'threshold_flow': '当前阶段与积压（统计时点快照，不是流转史）：阶段/状态分布、'
                    '未关闭单量与其中已超期单数、驳回与审批中的单量、超期最久的单。'
                    '要说清积压卡在哪个环节、哪些单已超期，以及积压规模是否在扩大。',
  'threshold_cause': '代表单调整依据（本章重点是**填报质量**）：依据给定的代表单详情原文'
                     '（调整原因/涉及设备/处置总结）。要把调整原因归类'
                     '（误报干扰/业务容量变化/设备配置变更/采集异常/检修维护/环境变化/审批流程），'
                     '**评估调整依据是否具体、处置总结是否只是复述原因**（「详情质量」给出按规则'
                     '识别的单数：依据具体/原因笼统/疑似复制/模板化/未填），据此评价填报质量；'
                     '对取不到详情的如实说明数据不足。',
}

# 报告类型专属的关注维度（只给行动计划/执行摘要用；不按类型加就沿用各章要点）
_FOCUS: dict[str, str] = {
  'change': '变更计划性与实施窗口（非计划/紧急变更占比）、审批与验收效率、'
            '计划 vs 实际偏差与按时完成率、风险控制与回退预案质量',
  'question': '存量老化与录入及时性（滞留分档与超期规模）、问题复发与对象集中度、'
              '级别×阶段的卡点、录入滞后对处置与统计的影响',
  'threshold': '调整意图与合规性（加电/下电/关闭屏蔽的规模）、发起人集中与批量调整风险、'
               '计划提前期与执行偏差、超期积压',
}


def _allowed_numbers(*sources) -> set:
  """本章/本轮数据里出现过的全部数字（含结构性数字）——跨期比较句的判定基准。"""
  allowed: set = set(_STRUCTURAL)
  for src in sources:
    collect_numbers(src, allowed)
  return allowed


def _delta_numbers(*sources) -> set:
  """数据里**真变化量**字段的数字——「增加/减少 N」里的 N 只能取自这里。"""
  deltas: set = set()
  for src in sources:
    collect_deltas(src, deltas)
  return deltas


def _audit_numbers(text: str, *allowed_sources) -> None:
  """解读里的数字自检（**只记日志，不拦报告**）—— 固定口径的报告里一个错数字就毁掉可信度。"""
  allowed = _allowed_numbers(*allowed_sources)
  extras = sorted({n for n in _NUM_RE.findall(text or '') if n not in allowed})
  if extras:
    logger.warning('报告解读里出现统计中不存在的数字（疑为模型自行换算/推算）: %s', extras[:20])


def _trim_stats(stats: dict) -> dict:
  """整体统计摘要（给「执行摘要 + 行动计划」用）。键名一律中文、**口径中性**（两类通用）。

  为什么中性：这些键只是**给模型看的标签**，写「验收时长」会让故障报告收到事件单的口径词，
  模型照抄进正文就串了类型。时长与质量两块的子键一律透传 stats 里**实际存在**的项
  （两类各自有的超时/超期键都带出去，谁有值谁被引用）。
  """
  accept = stats.get('accept') or {}
  out = {
    '窗口': stats.get('window'),
    '总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
    '环比': stats.get('mom'),
    '级别分布': stats.get('level'),
    '专业分布': _compare_rows(stats.get('specialty')),
    '园区分布': _compare_rows(stats.get('park'), 'name'),
    # 分位一律是可读串（>60 分钟成小时）：见 `_duration_view` 的注释
    '处理时效': {**_duration_view(accept), **{k: v for k, v in {
      '超时率': accept.get('timeout_rate') if accept.get('timeout_rate') is not None
               else accept.get('over_1d_rate'),
      '超时单数': accept.get('timeout_n') if accept.get('timeout_n') is not None
                 else accept.get('over_1d_n'),
    }.items() if v is not None}},
    '工单质量': {k: v for k, v in (stats.get('quality') or {}).items() if v is not None},
    '重复问题': stats.get('repeat') or [],
  }
  # 综合评分带「含义」列（含「规模控制：下降满分」）——行动计划与执行摘要都要据此
  # 判方向，否则会把改善当问题写成"要治理"。
  card = (stats.get('scorecard') or {}).get('rows')
  if card:
    out['综合评分（本期 vs 上一期，越高越好）'] = card
  # 变更单专属块：只在 stats 真有这些键时输出（事件/故障的输出逐字不变）
  ct = stats.get('change_type')
  if ct:
    out['变更类型'] = {'类型分布': ct.get('rows'),
                     '紧急占比': ct.get('emergency_rate'), '非计划占比': ct.get('unplanned_rate'),
                     '类型×等级': ct.get('by_level')}
  fl = stats.get('flow')
  if fl:
    out['流程与状态'] = {'阶段快照': fl.get('stage'), '状态快照': fl.get('status'),
                       '审批时长': _duration_view(fl.get('approve')),
                       '验收时长': _duration_view(fl.get('accept')),
                       '异常单': fl.get('exceptions'),
                       '未关闭单数': (fl.get('open') or {}).get('n'),
                       '其中已超期': (fl.get('open') or {}).get('overdue_n')}
  sc = stats.get('schedule')
  if sc:
    out['计划执行'] = {'计划时长': sc.get('planned'), '实际时长': sc.get('actual'),
                     '按时率': sc.get('on_time_rate'), '延迟率': sc.get('late_rate'),
                     '完成偏差分档': sc.get('delay_hist')}
  # 问题单专属块：只在 stats 真有这些键时输出（其余类型的输出逐字不变）
  ls = stats.get('level_stage')
  if ls:
    out['级别与阶段'] = {'级别×阶段': ls.get('rows'), '阶段': ls.get('stages')}
  ag = stats.get('aging')
  if ag:
    # 周报的超期口径是 over_days/over_n/over_label；月报仍是 over_30_n。
    # 两组**不能同时给**：周报里 over_30_n 恒为 0，模型会写成「超 30 天 0 起」。
    aging_keys = (('open_n', 'rate', 'over_days', 'over_n', 'over_label')
                  if is_weekly(stats) else ('open_n', 'rate', 'over_30_n'))
    out['存量老化'] = _pick(ag, aging_keys + ('p50', 'p90', 'mean',
                                              'max', 'readable', 'bins', 'counts'))
  el = stats.get('entry_lag')
  if el:
    out['录入及时性'] = _pick(el, ('valid', 'invalid_n', 'missing_n', 'p50', 'p90',
                                  'mean', 'max', 'readable', 'bins', 'counts'))
  ob = stats.get('problem_objects')
  if ob:
    out['问题对象'] = {'Top': ob.get('rows'), '覆盖单数': ob.get('covered_n'),
                     '未填单数': ob.get('blank_n'), '对象数': ob.get('classes')}
  # 阈值单专属块
  it = stats.get('intent')
  if it:
    out['阈值调整意图'] = {'分布': it.get('rows'), '未识别单数': it.get('unrecognized_n')}
  ld = stats.get('lead')
  if ld:
    out['计划提前期'] = _pick(ld, ('valid', 'invalid_n', 'missing_n', 'p50', 'p90',
                                  'mean', 'max', 'readable', 'hist'))
  ex = stats.get('execution')
  if ex:
    out['执行达成'] = _pick(ex, ('sched_valid', 'exec_valid', 'invalid_exec_n',
                                'missing_exec_n', 'on_time_n', 'on_time_base',
                                'on_time_rate', 'start_delay', 'planned', 'actual',
                                'by_specialty'))
  tf = stats.get('threshold_flow')
  if tf:
    out['阈值阶段与积压'] = {'阶段快照': tf.get('stage'), '状态快照': tf.get('status'),
                         '未关闭': tf.get('open'), '驳回单数': tf.get('rejected_n'),
                         '审批中单数': tf.get('pending_approval_n')}
  return out


def _pick(block: dict, keys: tuple) -> dict:
  """取块里**实际存在**的键（None 去掉，避免模型对着空值脑补）。"""
  return {k: block.get(k) for k in keys if block.get(k) is not None}


def _quality_view(details: dict) -> dict | None:
  """详情质检结果 → 喂模型的计数块；没有详情（n=0）时返回 None（不给空块）。"""
  q = (details or {}).get('quality') or {}
  if not q.get('n'):
    return None
  return {k: q.get(k) for k in ('n', 'specific_n', 'copy_n', 'vague_reason_n',
                                'boilerplate_n', 'no_summary_n')
          } | {'字段填写汇总': q.get('summary_fields') or []}


def _impact_view(details: dict) -> dict | None:
  """影响时长（详情口径）→ 喂模型的指标块；不可用或没有详情时返回 None（不给空块）。

  时长文案由平台算好（`readable`），模型只照抄，不自己做单位换算。
  """
  imp = (details or {}).get('impact') or {}
  if not imp:
    return None
  if not imp.get('available'):
    return {'可用': False, '说明': imp.get('reason')}
  hist = imp.get('hist') or {}
  # 按级别的分位表已上移到第 4 章（`_section_payload` 的 level 分支），此处不再重复下发
  return {'可用': True, '覆盖': f'{imp["valid"]}/{imp["n"]} 单',
          '未填单数': imp['missing'],
          'P50': imp['readable']['p50'], 'P90': imp['readable']['p90'],
          '最长': imp['readable']['max'], '影响不足1小时占比': imp['short_pct'],
          '分档（单数）': dict(zip(hist.get('labels') or [], hist.get('counts') or []))}


def _alarm_link_view(details: dict) -> dict | None:
  """重复类目的关联告警 → 喂模型的精简块（不下发整张阈值表）。

  模型只需要「哪类重复事件对应哪条告警、当时值多少、越了哪档」；
  四档阈值是渲染层给读者复核用的，进上下文只会诱导它做数值比较。
  """
  from service.scheduled_task.report import alarm_link

  link = (details or {}).get('alarm_links') or {}
  clusters = [c for c in (link.get('clusters') or []) if isinstance(c, dict) and c.get('alarms')]
  if not link.get('available') or not clusters:
    return None
  rows = []
  for cluster in clusters:
    occurrence = cluster.get('occurrence') or {}
    total = int(cluster.get('total') or 0)
    rows.append({
      '名称': cluster.get('name'),
      '次数': cluster.get('count'),
      '代表单': occurrence.get('ticketNo') or '',
      '关联次数': f'{int(cluster.get("matched_n") or 0)}/{total}' if total else '',
      '关联条数': len(cluster.get('alarms') or []),
      '告警': [{
        '时间': a.get('time') or '',
        '等级': a.get('level') or '',
        '设备测点': ' / '.join(x for x in (a.get('device'), a.get('point')) if x),
        '告警值': alarm_link.format_value(a.get('value'), a.get('unit') or ''),
        '越限档': '、'.join(a.get('breached') or []) or '未越限',
      } for a in (cluster.get('alarms') or [])],
    })
  return {'已关联类别': link.get('linked_n'), '各类目': rows,
          '覆盖说明': link.get('note') or ''}


def _trim_extras(stats: dict) -> dict:
  """喂给「执行摘要 + 行动计划」的**可归因切片**（最慢部门/最长验收单/超时分档/环比归因…）。

  为什么行动计划也需要这些：它原先只拿总量与占比（`_trim_stats`），于是只能把聚合复述成
  建议（"制冷问题突出 → 专项排查"）。对照旧 agent 版报告，它的建议能点到「超过 159 小时的
  24 起」这类**具体对象** —— 那种切片平台算得出，不该让模型自己去探索（也不该让它编）。
  """
  return stats.get('extras') or {}


# 成因关键词表与按单归类已迁到 `report/causes.py`（`impact.py` 也要用，避免循环依赖）。
# 这里保留原名 re-export，现有调用点与单测无需改动。
_CAUSE_KEYWORDS = CAUSE_KEYWORDS
_CHANGE_CAUSE_KEYWORDS = CHANGE_CAUSE_KEYWORDS
_QUESTION_CAUSE_KEYWORDS = QUESTION_CAUSE_KEYWORDS
_THRESHOLD_CAUSE_KEYWORDS = THRESHOLD_CAUSE_KEYWORDS
_cause_keywords = classify


def _duration_view(block: dict | None) -> dict:
  """审批/验收时长块 → 只带可引用的指标（空值键去掉，避免模型脑补）。

  **分位/均值/最长一律下发可读串**（超过 60 分钟已是小时）：键名写成 `P50分钟` 时模型会
  原样照抄「1958.8 分钟」，而提示词又禁止它自己换算 —— 用户 2026-10-29 明确要求长时长用小时。
  """
  b = block or {}
  out = {k: b[k] for k in ('valid', 'rows', 'timeout_rate', 'timeout_n', 'timeout_base')
         if b.get(k) is not None}
  for k in ('p50', 'p90', 'p95', 'mean', 'max'):
    text = (b.get('readable') or {}).get(k) or _human_minutes(b.get(k))
    if text != '—':
      out[k] = text
  return out


def _compare_rows(rows, name_key: str = 'label') -> list[dict]:
  """具名计数行 → **带单位的对比视图**（键名把量纲写死，模型无从混淆）。

  为什么不再原样下发：原字段是 `pct`（本期占比）和 `prev`（上一期**计数**），模型拿它们
  相减写出「智能化占比 40%，较上期增加 13%」（真值 40% vs 48.2%，−8.2 个百分点）。
  变化量（`prev_pct`/`delta_count`/`delta_pp`）由 compute 侧的 `common._add_compare` 算好；
  这里只做**改键名 + 补单位**，不再做任何计算。

  无上一期数据的行**不给上期键**（而不是给 0）：`prev=0` 会被读成「上一期占比 0%」。
  """
  out = []
  for r in rows or []:
    item = {
      '名称': r.get(name_key) or r.get('label') or r.get('name'),
      '本期单量': r.get('count'),
      '本期占比%': r.get('pct'),
    }
    if r.get('prev') is not None and r.get('prev_pct') is not None:
      item['上期单量'] = r.get('prev')
      item['上期占比%'] = r.get('prev_pct')
    if r.get('delta_count') is not None:
      item['单量变化（本期−上期）'] = r.get('delta_count')
    if r.get('delta_pp') is not None:
      item['占比变化（百分点，本期−上期）'] = r.get('delta_pp')
    out.append(item)
  return out


def _section_payload(stats: dict, details: dict, spec, role: str) -> dict:
  """每章只带它自己用得到的数字（越小越不容易被抄错，也越省 token）。

  关键：**每个有图的章都要带一份"园区维度"的数字** —— 否则模型写不出"园区特征"，
  只能给放之四海而皆准的空话（用户实测反馈过这一点）。
  **按角色分派**：角色相同则两类共用，差异只在 spec 声明里。

  ⚠️ **对齐型结构一律不直接喂**（`日趋势`/`园区×专业`/`机房×级别矩阵`/`处理时长区间分布`/
  `设备类型.cum_pct`）：它们要靠位置对齐才读得懂，实测被模型读成"惠州A3分部 4 级密集"（实为 0）。
  这些改由 `facts.build_facts()` 摊平成带编号的自述句（`数据事实`）；**具名结构原样保留**。
  """
  extras = stats.get('extras') or {}
  accept = stats.get('accept') or {}
  scope = (stats.get('window') or {}).get('scope') or ''
  weekly = is_weekly(stats)
  if role == 'trend':
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '环比': stats.get('mom'),
         '空间环比变动最大': (extras.get('园区环比变动最大')
                           or extras.get('部门环比变动最大'))}
    # 单量方向判据：平台已有口径（下降＝正向，与第 2 章「规模控制」同源）。
    # **必须下发**——只给无符号的「环比 −4」，模型会按运维报告的先验把它当异常解释
    # （2026-10-27 实测：26/51 份报告把单量下降写成「疑似漏报/采集失效」）。
    verdict = _volume_verdict(stats.get('total'), stats.get('prev_total'),
                              getattr(spec, 'count_unit', '起'))
    if verdict:
      d['单量方向'] = verdict
    # 月报才有；周报 payload 不出现这两个键（解读输入逐字不变）
    if stats.get('rhythm'):
      d['月内节奏'] = stats['rhythm']
    if stats.get('mom_top'):
      d['环比变化最大'] = stats['mom_top']
  elif role == 'level':
    d = {'总量': stats.get('total'), '级别分布': stats.get('level'),
         '各园区级别构成': extras.get('各园区级别构成')}
    bl = impact_level(stats, details)
    if bl:
      # 周报的本章图与表都是**影响时长**（详情口径），下发同源数据；与「处理时效」章的
      # 验收时长不可换算（两者高低不一致本身就是信息）。
      d['级别影响时长（详情口径，与验收时长不可换算）'] = {
        '按级别': bl.get('stats') or [],
        '分档（单数）': dict(zip(bl.get('bins') or [],
                              [sum(col) for col in zip(*bl.get('matrix'))])),
      }
  elif role == 'park':
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '园区分布': _compare_rows(stats.get('park'), 'name'),
         '各园区级别构成': extras.get('各园区级别构成'),
         # 单园区查询没有 companyName，归因切片退回部门维度（键名随之改变）
         '空间环比变动最大': (extras.get('园区环比变动最大')
                           or extras.get('部门环比变动最大')),
         # 平台没有设备台数/机房数量等规模口径 —— 跨园区绝对单量不可比。
         # 与 `common._volume_verdict` 同思路：把平台已知的判断直接告诉模型，
         # 否则最小体量的园区必然被解释成"监测粒度粗/漏报"（2026-10-27 实测）。
         '横向对比口径': {
           '说明': '各园区设备规模/机房数量不同，绝对单量不可横向比高低',
           '可比维度': '占比结构、级别构成、各自环比变化',
           '禁止': '不得因单量低推断漏报/监测粒度粗/上报偏保守'}}
    if getattr(spec, 'park_specialty_table', False):
      # 专业分布已并入本章（事件单）：总量表就是上面「园区 × 专业」堆叠图的列合计，
      # 同一批数据只说一遍，不给模型两套可相减的口径。
      d['各专业总量'] = _compare_rows(stats.get('specialty'))
    dept = stats.get('dept') or {}
    if dept.get('rows'):
      # 单园区报告：园区维度退化成部门维度（全国级不写 dept 键 → 本节逐字不变）
      # **与第 5 章表格同一切片**（前 `DEPT_TABLE_LIMIT` 行）：否则要点会讲到表里看不到的部门
      rows = dept['rows'][:DEPT_TABLE_LIMIT]
      d['部门分布'] = _compare_rows(rows, 'name')
      # 只给单量与两类 P50：三个率已在第 7/8/9 章各自说过，重复下发会让模型再讲一遍
      d['部门效率'] = [{'部门': r['name'], '单数': r['count'],
                        '审批P50': _human_minutes(r.get('approve_p50')),
                        '实施P50': _human_minutes(r.get('exec_p50'))}
                       for r in rows]
  elif role == 'specialty':
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '专业分布': _compare_rows(stats.get('specialty'))}
  elif role == 'equipment':
    eq = stats.get('equipment') or {}
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '专业分布': _compare_rows(stats.get('specialty')),
         '设备类型': {'rows': eq.get('rows') or [], 'classes': eq.get('classes') or 0,
                      '频次与时长': eq.get('scatter') or []}}
  elif role == 'dept':
    dept = stats.get('dept') or {}
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '部门（含本期/上期/环比增减）': _compare_rows(dept.get('rows'), 'name'),
         '部门数': dept.get('classes') or 0}
  elif role == 'risk':
    d = {'工单质量': stats.get('quality'), '现存风险': stats.get('risk')}
  elif role == 'duration':
    d = {'处理时效': {k: accept.get(k) for k in
                    ('valid', 'rows', 'p50', 'p90', 'p95', 'mean', 'max', 'readable',
                     'timeout_rate', 'timeout_n', 'timeout_base', 'over_1d_rate', 'over_1d_n')},
         '按级别的时长分位': accept.get('by_level_stat'),
         '各园区时长': extras.get('各园区时长'),
         '最慢部门': extras.get('最慢部门'),
         '最长单': extras.get('最长验收单') or extras.get('最长处理单')}
    # 影响时长是**详情口径**，与上面的验收时长并列给出（提示词明令不得混用/换算）
    iv = _impact_view(details)
    if iv:
      d['影响时长（详情口径，与验收时长不可换算）'] = iv
  elif role == 'quality':
    d = {'工单质量': stats.get('quality'),
         '指标口径': {label: desc for _, label, desc in spec.quality_rows}}
  elif role == 'repeat':
    d = {'重复问题': stats.get('repeat') or [],
         '重复问题类别数': (stats.get('quality') or {}).get('repeat_classes')}
    alarm_block = _alarm_link_view(details)
    if alarm_block:
      d['关联告警'] = alarm_block
  elif role == 'root_cause':
    d = {'高频问题详情': _trim_details(details, weekly)}
    # 事件周报的「整体归纳」素材（月报不取全量详情 → 这些块自然为空、不出现）
    causes = _cause_keywords(details)
    if causes.get('可归类'):
      d['成因归类（按单，首命中；仅统计所附详情原文）'] = causes
    quality = _quality_view(details)
    if quality:
      d['详情质量（确定性规则）'] = quality
    imp = (details or {}).get('impact') or {}
    if imp.get('available') and imp.get('long_tail'):
      d['影响时长最长单'] = {'覆盖': f'{imp["valid"]}/{imp["n"]} 单',
                        'P50': imp['readable']['p50'], 'P90': imp['readable']['p90'],
                        '最长': imp['readable']['max'],
                        '影响不足1小时占比': imp['short_pct'],
                        '单列表（处置过程/结果已截断）': imp['long_tail']}
  elif role == 'change_type':
    ct = stats.get('change_type') or {}
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '变更类型': _compare_rows(ct.get('rows')),
         '计划型单数': ct.get('planned_n'), '计划型占比': ct.get('planned_rate'),
         '非计划单数': ct.get('unplanned_n'), '非计划占比': ct.get('unplanned_rate'),
         '紧急单数': ct.get('emergency_n'), '紧急占比': ct.get('emergency_rate'),
         '类型×等级': ct.get('by_level'), '各园区紧急占比': ct.get('by_park'),
         '各园区级别构成': extras.get('各园区级别构成')}
  elif role == 'flow':
    fl = stats.get('flow') or {}
    op = fl.get('open') or {}
    # 阶段/状态合并成一张联合表下发：两张边际表是同一批单的两个视角，分开给模型会让它讲两遍
    d = {'阶段与状态（当前快照）': fl.get('stage_status'),
         '审批时长': _duration_view(fl.get('approve')),
         '验收时长': _duration_view(fl.get('accept')),
         '异常单': fl.get('exceptions'),
         '未关闭': {'单数': op.get('n'), '占比': op.get('rate'),
                    '已过计划结束时间单数': op.get('overdue_n'),
                    '距计划结束天数Top（正＝已超期，负＝未到期）': op.get('rows')}}
    # 单园区专属（全国级 flow 里没有这三个键 → 本节逐字不变）
    if fl.get('durations'):
      d['各环节耗时'] = fl['durations']
    if fl.get('by_level_duration'):
      d['分等级时效'] = [{'等级': r['等级'], '单数': r['单数'],
                        '审批P50': _human_minutes(r.get('审批P50分钟')),
                        '实施P50': _human_minutes(r.get('实施P50分钟')),
                        '验收P50': _human_minutes(r.get('验收P50分钟')),
                        '按时率%': r.get('按时率')}
                       for r in fl['by_level_duration']]
    if fl.get('exception_rates'):
      d['流程异常率'] = fl['exception_rates']
  elif role == 'schedule':
    sc = stats.get('schedule') or {}
    pts = sc.get('scatter') or []
    shorter = sum(1 for p, a in pts if a <= p)
    sd = sc.get('start_delay') or {}
    d = {'计划时长': sc.get('planned'), '实际时长': sc.get('actual'),
         '开始延迟': {'延迟单数': sd.get('late_n'), '延迟率%': sd.get('late_rate'),
                      '延迟中位': _human_minutes(sd.get('p50')),
                      'P90延迟': _human_minutes(sd.get('p90'))},
         '按时完成': {'按时单数': sc.get('on_time_n'), '参与统计': sc.get('on_time_base'),
                      '按时率': sc.get('on_time_rate'), '延迟单数': sc.get('late_n'),
                      '延迟率': sc.get('late_rate'), '提前单数': sc.get('early_n')},
         '完成偏差分档': sc.get('delay_hist'), '按变更类型按时率': sc.get('by_type'),
         '各园区按时率': sc.get('by_park'), '延迟最严重': sc.get('worst'),
         # 键名必须自解释：原来叫「实施时间缺失」，模型据此写成「实施时间数据缺失 29 条
         # 导致无法纳入时效分析」—— 这 29 单是**尚未完成实施**（在制），不是数据缺失
         '数据可用性': {
           '计划时间可统计': sc.get('sched_valid'),
           '实施时间可统计': sc.get('exec_valid'),
           '实施时间非法（实施结束早于开始，已剔除）': sc.get('invalid_exec_n'),
           '尚未完成实施（无实施起止时间，不计入时效）': sc.get('missing_exec_n'),
         }}
    if pts:
      d['计划与实际实施时长（散点图）'] = {
        '可对比单数': len(pts),
        '实际不长于计划窗口的单数': shorter,
        '实际长于计划窗口的单数': len(pts) - shorter,
        '口径说明': '对角线为计划＝实际，点在下方＝实际用时更短；'
                    '长于计划窗口不等于延期，按时与否见「完成偏差分档」。'}
  elif role == 'change_cause':
    d = {'变更单详情': _trim_details(details, weekly),
         '成因归类（按单，首命中；仅统计所附详情原文）':
           _cause_keywords(details, _CHANGE_CAUSE_KEYWORDS),
         '详情质量（确定性规则）': _quality_view(details)}
  # ---- 问题工单专属 ----
  elif role == 'level_stage':
    ls = stats.get('level_stage') or {}
    d = {'总量': stats.get('total'),
         '级别与阶段': {'阶段': ls.get('stages'), '级别×阶段（行）': ls.get('rows')}}
  elif role == 'aging':
    d = {'存量老化': stats.get('aging'),
         '各园区老化构成': extras.get('各园区老化构成'),
         '最老未关闭单': extras.get('滞留最久未关闭单') or extras.get('最老未关闭单')}
  elif role == 'park_specialty':
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '园区分布': _compare_rows(stats.get('park'), 'name'),
         '专业分布': _compare_rows(stats.get('specialty')),
         '空间环比变动最大': (extras.get('园区环比变动最大')
                           or extras.get('部门环比变动最大')),
         '各园区老化构成': extras.get('各园区老化构成')}
  elif role == 'problem_objects':
    d = {'问题对象': stats.get('problem_objects')}
  elif role == 'entry_lag':
    d = {'录入及时性': stats.get('entry_lag')}
  elif role == 'question_cause':
    d = {'问题单详情': _trim_details(details, weekly),
         '成因归类（按单，首命中；仅统计所附详情原文）':
           _cause_keywords(details, _QUESTION_CAUSE_KEYWORDS),
         '详情质量（确定性规则）': _quality_view(details)}
  # ---- 阈值工单专属 ----
  elif role == 'threshold_intent':
    it = stats.get('intent') or {}
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '调整意图': _compare_rows(it.get('rows')),
         '未识别单数': it.get('unrecognized_n')}
  elif role == 'threshold_scope':
    ini = stats.get('initiator') or {}
    d = {'总量': stats.get('total'), '上一期总量': stats.get('prev_total'),
         '专业分布': _compare_rows(stats.get('specialty')),
         '园区分布': _compare_rows(stats.get('park'), 'name'),
         '发起人': {'Top10': ini.get('top'), '发起人数': ini.get('classes'),
                    '首位占比': ini.get('top_share'),
                    '未解析单数': ini.get('unknown_n')},
         # 具名交叉表（发起人 Top5 × 意图）：每格自带行列名，原样给不会读错
         '主要发起人×调整意图': stats.get('initiator_intent'),
         '空间环比变动最大': (extras.get('园区环比变动最大')
                           or extras.get('部门环比变动最大'))}
  elif role == 'hot_items':
    d = {'总量': stats.get('total'), '热点对象': stats.get('hot_items'),
         '批量规模': stats.get('batch')}
  elif role == 'lead_time':
    d = {'总量': stats.get('total'), '计划提前期': stats.get('lead'),
         '最长提前期单': extras.get('最长提前期单')}
  elif role == 'execution':
    d = {'总量': stats.get('total'), '执行达成': stats.get('execution')}
  elif role == 'threshold_flow':
    fl = stats.get('threshold_flow') or {}
    d = {'阶段分布（当前快照）': fl.get('stage'), '状态分布（当前快照）': fl.get('status'),
         '未关闭': fl.get('open'), '驳回单数': fl.get('rejected_n'),
         '审批中单数': fl.get('pending_approval_n')}
  elif role == 'threshold_cause':
    d = {'阈值单详情': _trim_details(details, weekly),
         '成因归类（按单，首命中；仅统计所附详情原文）':
           _cause_keywords(details, _THRESHOLD_CAUSE_KEYWORDS),
         '详情质量（确定性规则）': _quality_view(details)}
  else:
    d = {'统计': _trim_stats(stats)}
  d['统计范围'] = scope or '全国各园区'
  facts = build_facts(stats, spec, role, details)
  if facts:                          # 无对齐型结构的章（级别/质量/重复/根因…）不带这个键
    d['数据事实'] = facts
  return d


def _trim_details(details: dict, weekly: bool = False) -> list[dict]:
  """详情**透传白名单已裁好的字段**（去掉编号/时间这类噪音键，保留根因相关原文）。

  不写死字段名（原来挑「事件名称/原因分析/…」）：两类工单的详情字段名不同，写死就把故障单
  的信息全过滤掉了。白名单（`tools/analysis_tools.py` 的 `_DETAIL_FIELDS`）已完成裁剪与去噪。

  周报取的是**本期全部工单**详情（可达上百条）：放宽到 30 条 × 150 字，并设总字符预算，
  防止单章输入被撑爆；月报仍是 3 条 × 300 字，行为不变。
  """
  rows = (details or {}).get('rows', []) or []
  if weekly:
    # 周报按全量串行取详情，失败行只有编号、不含任何可归纳的内容，
    # 不能让它们占掉 30 条名额（PDF 侧仍会如实列出「详情未取到」）。
    rows = [r for r in rows if (r.get('fields') or {})] or rows
  max_rows = _WEEKLY_DETAIL_ROWS if weekly else 3
  cell = _WEEKLY_DETAIL_CHARS if weekly else 300
  out, used = [], 0
  for item in rows[:max_rows]:
    f = item.get('fields') or {}
    d = {k: (str(v)[:cell]) for k, v in f.items()
         if v and '编号' not in k and '时间' not in k}
    d = d or {'工单编号': item.get('ticketNo', '')}
    size = sum(len(str(v)) for v in d.values())
    # 预算之外停止追加（首条必留：预算再小也要有内容）
    if weekly and out and used + size > _WEEKLY_DETAIL_BUDGET:
      break
    out.append(d)
    used += size
  return out


# ==================== 第 10 章长文本归纳 ====================
# 只对 `spec.detail_condense_fields` 声明的字段做（目前仅阈值单的「调整原因/处置总结」）：
# 这两列常是整页的机房/机柜枚举，原样列（或截断加省略号）读者抓不到结论。
_SUMMARY_MIN_SRC = 60       # 原文短于这个长度就不用归纳
_SUMMARY_MAX_LEN = 60       # 摘要上限（提示词要求 50 字，校验放宽到 60）
_SUMMARY_BATCH = 20         # 一次调用最多归纳的单元格数
_SUMMARY_SRC_CHARS = 600    # 单条原文送入上限（够读出对象、数量与动作）
_SUMMARY_DIGITS = re.compile(r'\d+')

# 注意：不要叫 `_SUMMARY_SYSTEM` —— 上面「执行摘要」已有同名常量，覆盖后摘要章会拿到
# 本提示词、返回 {"摘要":…} 而取不到「执行摘要」键（实测三次重试全空）。
_CONDENSE_SYSTEM = (
  '你是数据中心运维报告的编辑。用户给你若干条工单详情里的长文本（调整原因 / 处置总结），'
  '请把每一条压缩成**一句话**中文摘要（不超过 50 字）：\n'
  '1. 保留关键对象（哪几间机房、哪些设备/机柜/配电室）、数量、动作与结果；\n'
  '2. **不得新增原文里没有的事实或数字** —— 原文没有的数量不要写；\n'
  '3. 不得使用省略号（… 或 ...）；\n'
  '4. 只输出 JSON：{"摘要":[{"编号":"工单编号","字段":"字段名","文本":"摘要句"}]}'
)


def _summary_ok(text: str, source: str) -> bool:
  """摘要校验：短、无省略号、数字全部能在原文里找到、且确实比原文短。

  数字闸是硬闸：原文是"共 12 个机柜"，摘要写成"共 20 个"必须整条丢掉——
  与 `_audit_numbers` 同思路，但这里能直接拒绝（回退全文即可，不影响报告）。
  """
  text = (text or '').strip()
  if not text or len(text) > _SUMMARY_MAX_LEN or len(text) >= len(source):
    return False
  if '…' in text or '...' in text:
    return False
  return set(_SUMMARY_DIGITS.findall(text)) <= set(_SUMMARY_DIGITS.findall(source))


async def summarize_details(details: dict, spec) -> dict[str, dict[str, str]]:
  """把第 10 章逐单表的长文本归纳成一句话；返回 `{工单编号: {字段: 摘要}}`。

  **失败一律返回已有结果**（缺的单元格渲染层回退原文全文），不让报告因归纳失败而缺内容。
  """
  fields = tuple(getattr(spec, 'detail_condense_fields', ()) or ())
  if not fields:
    return {}
  todo = []
  for item in (details or {}).get('rows') or []:
    no = str(item.get('ticketNo') or '').strip()
    f = item.get('fields') or {}
    if not no:
      continue
    for field in fields:
      val = re.sub(r'\s+', ' ', str(f.get(field) or '')).strip()
      if len(val) > _SUMMARY_MIN_SRC:
        todo.append({'编号': no, '字段': field, '原文': val[:_SUMMARY_SRC_CHARS]})
  if not todo:
    return {}

  out: dict[str, dict[str, str]] = {}
  bad = 0
  for start in range(0, len(todo), _SUMMARY_BATCH):
    batch = todo[start:start + _SUMMARY_BATCH]
    got = await _call(_CONDENSE_SYSTEM, {'待归纳': batch}, retries=1,
                      label=f'详情归纳#{start // _SUMMARY_BATCH + 1}', max_tokens=3000)
    source = {(b['编号'], b['字段']): b['原文'] for b in batch}
    for row in got.get('摘要') or []:
      if not isinstance(row, dict):
        continue
      key = (str(row.get('编号') or '').strip(), str(row.get('字段') or '').strip())
      text = str(row.get('文本') or '').strip()
      if key not in source:                 # 模型编的编号/字段，直接丢
        continue
      if not _summary_ok(text, source[key]):
        bad += 1
        continue
      out.setdefault(key[0], {})[key[1]] = text
  logger.info('详情归纳：待归纳 %d 条，成功 %d 条，校验未过 %d 条',
              len(todo), sum(len(v) for v in out.values()), bad)
  return out


def _loads(raw: str) -> dict:
  """宽容解析：剥掉可能的 markdown 围栏，取第一个 `{` 到最后一个 `}`。

  **繁简归一做在解析之前**（不只各出口）：① 模型偶尔把 JSON 键写成繁体（`要點`/`總體評價`），
  不先归一会因键名不匹配整章静默丢空；② 三道守卫（趋势/跨期/猜测）的词表都是简体，
  先归一才拦得住繁体版的句子。转换幂等，只作用于模型文本，不碰 stats/源数据。
  """
  text = _to_simplified((raw or '').strip())
  if text.startswith('```'):
    text = text.split('\n', 1)[-1]
    if text.rstrip().endswith('```'):
      text = text.rstrip()[:-3]
  start, end = text.find('{'), text.rfind('}')
  if start < 0 or end <= start:
    raise ValueError('响应里没有 JSON 对象')
  return json.loads(text[start:end + 1])


def _validate(data: dict) -> dict:
  """只保留契约里的字段与类型（摘要 + 总体评价 + 行动计划）。"""
  summary = [str(x) for x in (data.get('执行摘要') or []) if str(x).strip()][:_MAX_SUMMARY]
  overall = str(data.get('总体评价') or '').strip()
  actions = []
  for row in (data.get('行动计划') or [])[:_MAX_ACTIONS]:
    if not isinstance(row, dict):
      continue
    m = re.search(r'P[0-9]', str(row.get('优先级', '') or ''))
    pri = m.group(0) if m else ''
    if pri not in _PRIORITIES:
      continue          # 越界值直接丢（实测模型会自己造 P3，而渲染只认三档）
    actions.append({'优先级': pri,
                    **{k: str(row.get(k, '')) for k in ('行动项', '责任维度', '时限')}})
  return {'执行摘要': summary, '总体评价': overall, '行动计划': actions}


async def _call(system: str, payload: dict, *, retries: int, label: str,
                max_tokens: int = 2000) -> dict:
  """一次 LLM 调用 + 宽容解析 + 重试；解析不出来返回 {}（不抛）。

  走 `json_mode`（上游 `response_format={'type':'json_object'}`）**从根上约束成合法 JSON**
  —— 实测行动计划入参含详情原文（引号/换行），模型会把引号直接写进字符串且不转义，产出
  坏 JSON（`Expecting ',' delimiter`），三次重试全坏就整章空。`json_mode` 让它不可能坏。
  上游若不支持该参数（`chat` 抛异常），置 `_JSON_MODE_OK=False` 后自动退回普通模式 —— 只回到
  现状，不会更差。
  """
  from core.model_gateway import model_gateway

  global _JSON_MODE_OK
  messages = [{'role': 'system', 'content': system},
              {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, indent=1)}]
  for attempt in range(retries + 1):
    try:
      raw = await model_gateway.chat(messages=messages, temperature=0.3,
                                     max_tokens=max_tokens, caller='scheduled_report',
                                     json_mode=_JSON_MODE_OK)
      out = _loads(raw)
      if out:
        return out
      logger.warning('报告解读 %s 返回空内容（第 %d 次）', label, attempt + 1)
    except Exception as e:
      if _JSON_MODE_OK:
        _JSON_MODE_OK = False
        logger.warning('报告解读 json_mode 不可用，改回普通模式：%s', str(e)[:120])
      logger.warning('报告解读 %s 生成失败（第 %d 次）: %s', label, attempt + 1, str(e)[:200])
  return {}


async def _call_validated(system: str, payload: dict, *, retries: int, label: str, pick) -> list:
  """重复调用，直到 `pick(_validate(响应))` 非空。

  与 `_call` 的分工：`_call` 只兜「解析失败」；本函数**还兜「解析成功但校验后为空」**
  —— 行动计划的 `优先级` 不在 P0/P1/P2 时 `_validate` 会把整行丢掉，可能出现"响应合法、
  结果 0 条"（实测两份报告各约 1/10~1/20 概率空章）。两种情况都算本次没用，整轮重试。
  `pick` 从校验后的 dict 取目标字段（如 `lambda d: d['行动计划']`）。
  """
  for attempt in range(retries + 1):
    val = pick(_validate(await _call(system, payload, retries=0, label=f'{label}#{attempt + 1}')))
    if val:
      return val
    logger.warning('报告解读 %s 第 %d 次无有效内容（解析失败或字段全被丢弃）', label, attempt + 1)
  return []


def _validate_section(data: dict, chart_keys: list[str], allowed: set,
                      require_evidence: bool = True,
                      trend_index: dict | None = None,
                      allowed_numbers: set | None = None,
                      deltas: set | None = None,
                      allow_data_suspicion: bool = True,
                      allowed_orgs: set | None = None) -> tuple[dict, int]:
  """单章输出规范化 + 四道**数值类**闸；返回 `(结果, 丢弃条数)`。

  契约：要点与图说明的每一条都是 `{"文本": "…", "依据": ["F3"]}`，`依据` 只能取自
  `allowed`（本章「数据事实」的编号 ∪ 本章数据的键名）。**依据缺失/编号不存在只记日志、
  不丢句**（2026-10-30 降级，与 name_guard 同口径）：它是形式检查，查不出数值关系真假，
  却把大量带本章真实数字的分析句删掉 —— 唯一保留逐章日志的一天（2026-10-19）里，
  13 条丢弃中 12 条来自这道闸，删的是「2 级事件 P50 时长仅比 4 级事件快 3 小时…」
  这类实质结论。真正的数值错误由 trend_guard / compare_guard 拦。
  校验后**拍平回 `str`**，与渲染层契约完全一致（渲染层无需改动）。

  `require_evidence=False` 用于**没有「数据事实」的章**（质量/重复/根因…）：这些章的数据
  本就是具名的或纯文本，连依据日志也不必打。故这些章**保持自由文本**：只做规范化。
  """
  dropped = 0

  def _keep(items) -> list[str]:
    nonlocal dropped
    if isinstance(items, str):
      items = [items]
    out = []
    for it in items or []:
      if isinstance(it, dict):
        text = str(it.get('文本') or '').strip()
        raw = it.get('依据')
        ev = [str(x) for x in (raw if isinstance(raw, list) else ([raw] if raw else []))]
      else:                                   # 裸字符串＝没写依据
        text, ev = str(it).strip(), []
      if not text:
        continue
      # 依据只是**审计信号**，不是删除条件：缺依据/编号不存在只记日志、句子照留。
      # 这道闸曾被当成幻觉防线，实测误杀率最高 —— 它查的是编号在不在 `allowed` 里，
      # 而 2026-10-15 那类「5 起写成高于 6 起」的依据编号是存在的，它一次也拦不住。
      # 数字真伪交给下面的 trend_guard / compare_guard。
      if require_evidence and (not ev or any(e not in allowed for e in ev)):
        logger.warning('报告解读：无依据/依据不存在的句子（仅记录，不丢句）（依据=%s）：%s',
                       '、'.join(ev) or '无', text[:80])
      # 趋势章：日期→数值的**可证明为假**的比较整条丢弃（依据编号查不出数值关系真假）
      bad = trend_guard.violations(text, trend_index)
      if bad:
        logger.warning('报告解读：丢弃与趋势数据矛盾的句子（%s）：%s',
                       '；'.join(bad), text[:80])
        dropped += 1
        continue
      # 跨期比较句：数字必须在本章数据里出现过；「增加/减少 N」里的 N 还必须是**真变化量**
      # （模型把「本期占比 40%」减「上期计数 27」写成「较上期 +13%」，依据编号照样存在，
      # 只能按数字出处拦；光看"出现过"也拦不住「占比 40%，较上期增加 40%」——40 出现过
      # 但根本不是变化量）
      claims = compare_guard.violations(text, allowed_numbers, deltas) if allowed_numbers else []
      if claims:
        logger.warning('报告解读：丢弃跨期比较数字无出处的句子（越界数字 %s）：%s',
                       '；'.join(','.join(n for n in extra) for _, extra in claims), text[:80])
        dropped += 1
        continue
      # 把「单量下降」推测成「漏报/采集失效」：报告没有缺失证据时整句丢弃
      # （两道数字闸都拦不住——这类猜测一个数字都没有）
      guesses = speculation_guard.violations(text, allow_data_suspicion)
      if guesses:
        logger.warning('报告解读：丢弃无依据的数据可靠性猜测：%s', text[:80])
        dropped += 1
        continue
      # 「本章缺少…所以无法判断」：报告不提供的项统一在末章列出（用户明确不要这类元抱怨）
      if meta_guard.violations(text):
        logger.warning('报告解读：丢弃「数据不可得→无法判断」的自我否定句：%s', text[:80])
        dropped += 1
        continue
      # 点名的机构若不在本章载荷里，**只记日志、不丢句**。2026-10-30 复核发现这道闸的
      # 起因事件本身就是误杀（`B1M4分部`/`技术保障部` 都是数据里的真实部门，只是没进
      # 前 12 行表），而它在本轮 24 份报告里删掉了 55 句真部门论述 —— 见
      # docs/problems/report/report-invented-org-names.md「十」。
      if allowed_orgs is not None:
        invented = name_guard.violations(text, allowed_orgs)
        if invented:
          logger.warning('报告解读：点名了载荷外机构名（仅记录，不丢句）（%s）：%s',
                         '、'.join(invented), text[:80])
      # 术语归一必须在 `digest` 之前（摘要/行动计划吃各章要点原文，一处英文被复述三次）
      out.append(_normalize_terms(text))
    return out

  points = _keep(data.get('要点'))[:6]
  notes: dict[str, list[str]] = {}
  raw = data.get('图说明') or {}
  if isinstance(raw, dict):
    for key in chart_keys:                    # 只收本章真实存在的图，防止模型编键名
      items = _keep(raw.get(key))[:3]
      if items:
        notes[key] = items
  return {'要点': points, '图说明': notes}, dropped


async def narrate(stats: dict, details: dict, spec, *, retries: int = 2) -> dict:
  """写解读：**分章调用**。任何一章失败只让该章缺文字，绝不把报告整体判失败。"""
  out: dict = {'执行摘要': [], '总体评价': '', '章节': {}, '行动计划': []}
  count = len(spec.sections)
  weekly = is_weekly(stats)
  # 样本过少时不出图（compute 侧已早退）：图表对 1~2 条记录读不出分布，
  # 也不能再向模型要「图说明」，否则它会对着不存在的图编解析。
  suppressed = bool(stats.get('charts_suppressed'))
  # 成因关键词表按工单类型选（变更讲"为什么要动"、问题讲"为什么成为问题"、
  # 阈值讲"为什么要调阈值"，三者不是同一套词汇）
  if 'change_cause' in spec.roles:
    cause_table = _CHANGE_CAUSE_KEYWORDS
  elif 'question_cause' in spec.roles:
    cause_table = _QUESTION_CAUSE_KEYWORDS
  elif 'threshold_cause' in spec.roles:
    cause_table = _THRESHOLD_CAUSE_KEYWORDS
  else:
    cause_table = None
  causes = _cause_keywords(details, cause_table)
  metrics = '、'.join(label for _, label, _ in spec.quality_rows)
  dropped_total = 0
  # 趋势的确定性索引：只对「日期→数值」的篇章启用（其它章的键名不同，喂进来也没意义）
  trend_index = trend_guard.build_index(stats.get('trend'),
                                        getattr(spec, 'count_unit', '起'))
  # 数据里确有缺失/非法/剔除的记录时，才允许把下降解释成数据问题（否则一律当猜测丢弃）
  allow_suspicion = speculation_guard.has_data_quality_evidence(stats)

  # ① 逐章解读（**必须最先做**：后面的行动计划与执行摘要都要引用各章要点）
  # 按 **spec.roles 的角色**分派（不是按章号）—— 角色不在 _BRIEFS 里的章（摘要/卡片/行动计划/
  # 数据说明）由渲染层确定性产出，不需要 LLM 文字。
  for no, role in enumerate(spec.roles, 1):
    brief = _BRIEFS.get(role)
    if not brief or no > count:
      continue
    brief = brief.format(
      level_label=spec.level_label, duration_label=spec.duration_label,
      # 周报第 4 章改按影响时长（详情口径）时，brief 里的「时长」跟着换，否则会让模型
      # 拿着影响时长的图去讲验收时长
      level_duration_label=('影响时长' if impact_level(stats, details)
                            else spec.duration_label),
      quality_metrics=metrics,
      # 问题单周报的超期口径是 3 天（月报 30 天）；其他类型无 aging 块，回退 30
      aging_over=(stats.get('aging') or {}).get('over_days') or 30)
    if role == 'park':
      # 堆叠图的维度按 spec 声明写清（事件＝专业、故障/变更＝级别），否则模型会把一种图
      # 套成另一种来讲（基础 brief 刻意不带维度名，见 `_BRIEFS['park']` 的注释）。
      if any(c.key == 'park_level' for c in spec.charts):
        brief += (f'\n本章另有「各园区{spec.level_label}结构」堆叠条：每个园区一条横条、'
                  '按级别堆叠，横向看结构差异；哪个园区高等级占比明显更高要点名。')
      if getattr(spec, 'park_specialty_table', False):
        brief += ('\n本章的堆叠图是「各园区 × 专业构成」，另附「各专业总量」表'
                  '（就是堆叠图的列合计，不要自己相减）：点出哪些专业为主、'
                  '专业在园区间是否集中。')
      if (stats.get('dept') or {}).get('rows'):
        # 单园区报告的本章没有园区、只有部门——不能被讲成"园区之间"的差异
        brief += ('\n仅有单个园区时，本章改用**部门维度**（各部门单量对比图 + 部门效率对比表）：'
                  '说明单量最大的部门、部门间单量结构与审批/实施 P50 的差异，'
                  '**不要写成园区之间的比较**；P50 样本少于 3 单的部门不下结论。')
    if role == 'flow' and (stats.get('flow') or {}).get('durations'):
      brief += ('\n本章另附「各环节耗时」表（审批/实施/验收/全流程）与「分等级时效」**图**：'
                '各环节口径不同、**不可相加**，全流程不含关闭期（平台没有关闭时间戳）；'
                '要说清哪一环节最耗时、哪个等级更慢，异常率口径是当前状态快照。')
    if weekly and role in ('root_cause', 'change_cause', 'question_cause',
                           'threshold_cause'):
      brief += ('\n本周报已取到本期全部工单详情，请归纳共性问题与处置模式，'
                '不要逐单复述。')
    if role == 'trend' and not weekly and stats.get('rhythm'):
      brief += ('\n本期是**月度报告**：另看「月内节奏」（按自然周、工作日 vs 周末）'
                '与「环比变化 TOP」。比较各周只能用**日均**（首末周天数可能不足 7 天），'
                '不得用单量比较长短不一的周；变化最大项直接引用数据事实，不做减法或换算。')
    # 月报专属图（如月内节奏）不进周报的图表清单，否则模型会对着不存在的图编解析
    charts = [] if suppressed else [c for c in spec.charts
                                    if c.section == no
                                    and (not c.monthly_only or not weekly)]
    if suppressed:
      brief += ('\n本期样本过少，本章不提供图表：不要输出「图说明」字段，'
                '只写有数据支撑的要点。')
    data = _section_payload(stats, details, spec, role)
    facts = data.get('数据事实') or []
    # 可引用的依据 = 本章「数据事实」编号 ∪ 本章数据键名（`统计范围` 只是范围说明、不算依据）
    allowed = ({f['编号'] for f in facts} | (set(data) - {'统计范围', '数据事实'}))
    sec_payload = {
      '报告名': spec.title, '章节': spec.sections[no - 1],
      # 把本章的图表清单给模型：它要按这些**键**逐张回「图说明」（渲染层按同一个键就近插入）
      # 标题按口径切换（与 render 同一判据）：周报第 4 章画的是影响时长，仍报「验收时长构成」
      # 会让模型照着旧标题写出与图不一致的解析
      '本章图表': [{'键': c.key, '标题': (
        c.weekly_title if (c.weekly_title and impact_level(stats, details)) else c.title)}
        for c in charts],
      '本章数据': data,
    }
    got = await _call(_SECTION_SYSTEM.format(brief=brief), sec_payload,
                      retries=retries, label=f'第 {no} 章')
    sec, dropped = _validate_section(
      got, [c.key for c in charts], allowed,
      require_evidence=bool(facts),
      trend_index=trend_index if role == 'trend' else None,
      allowed_numbers=_allowed_numbers(sec_payload),
      deltas=_delta_numbers(sec_payload),
      allow_data_suspicion=allow_suspicion,
      allowed_orgs=name_guard.allowed_from_payload(sec_payload))
    dropped_total += dropped
    if dropped:
      logger.info('第 %d 章（%s）：丢弃 %d 条（趋势矛盾/比较数字越界/可靠性猜测/自我否定）',
                  no, role, dropped)
    if sec['要点'] or sec['图说明']:
      _audit_numbers(' '.join(sec['要点'])
                     + ' '.join(' '.join(v) for v in sec['图说明'].values()), sec_payload)
      _audit_latin(' '.join(sec['要点'])
                   + ' '.join(' '.join(v) for v in sec['图说明'].values()))
      _audit_template(' '.join(sec['要点']), label=f'第 {no} 章')
      out['章节'][str(no)] = sec

  # 各章要点的摘要（给后两步当输入）
  digest = [{'章节': spec.sections[int(k) - 1], '要点': v['要点']}
            for k, v in sorted(out['章节'].items(), key=lambda kv: int(kv[0]))]

  # ② 行动计划（吃：各章要点 + 可归因切片 + 详情/成因关键词）
  act_payload = {'报告名': spec.title, '各章要点': digest,
                 '统计': _trim_stats(stats), '可归因切片': _trim_extras(stats),
                 '高频问题详情': _trim_details(details, weekly)}
  if spec.ticket in _FOCUS:                 # 只有变更报告带（原有两类输出不变）
    act_payload['报告类型'] = spec.title
    act_payload['本报告关注维度'] = _FOCUS[spec.ticket]
  if causes:
    act_payload['成因归类（按单，首命中；仅统计所附详情的原文）'] = causes
  quality = _quality_view(details)
  if quality:                       # 让"依据填报规范"这类建议能落到具体单数上
    act_payload['详情质量（确定性规则）'] = quality
  # 风险分析：改进项由它驱动（只喂达到阈值的行，空表不喂 —— 免得模型对着空数组编风险）
  risk = (details or {}).get('risk_analysis') or {}
  if risk.get('rows'):
    act_payload['风险分析'] = risk
  # 本期整体表现：平台已确定性写好并印在章首；模型换角度展开、不复述原句，空串不喂
  highlights = (details or {}).get('highlights') or {}
  if highlights.get('rows'):
    act_payload['本期运行亮点'] = highlights['rows']
  if highlights.get('summary'):
    act_payload['本期整体表现'] = highlights['summary']
  # 不走 `_call_validated`：改进项**允许为 0 条**，按"空即重试"会把正常结果重跑三次
  act_val = _validate(await _call(_action_system(weekly), act_payload, retries=retries,
                                  label='行动计划'))
  # 白名单只用于**日志审计**：命中也不删条目（同 `_validate_section` 的口径，
  # 撤闸原因见 docs/problems/report/report-invented-org-names.md「十」）
  act_orgs = name_guard.allowed_from_payload(act_payload)
  # 综合评价与行动计划吃各章要点原文，同样在这里做术语归一
  out['总体评价'] = _normalize_terms(act_val['总体评价'])
  kept_items, invented_items = [], []
  for a in act_val['行动计划']:
    item = {k: _normalize_terms(v) for k, v in a.items()}
    text = ' '.join(str(v) for v in item.values())
    invented = name_guard.violations(text, act_orgs)
    if invented:
      invented_items.append((text[:60], '、'.join(invented)))
    kept_items.append(item)
  if invented_items:
    logger.warning('行动计划点名了载荷外机构名（仅记录，不丢条目）：%s', invented_items[:3])
  out['行动计划'] = kept_items
  # 综合评价也可能把"下降"写成"漏报"：与摘要同一道猜测闸（无依据的整段丢弃）
  if out['总体评价']:
    kept, guessed = speculation_guard.filter_texts([out['总体评价']], allow_suspicion)
    if guessed:
      logger.warning('总体评价丢弃无依据的数据可靠性猜测：%s', [t[:40] for t, _ in guessed])
    out['总体评价'] = kept[0] if kept else ''
  # 自我否定句（「本章缺少…无法判断」）：整体置空（同猜测闸的处理方式）
  if out['总体评价'] and meta_guard.violations(out['总体评价']):
    logger.warning('总体评价丢弃「数据不可得→无法判断」的句子：%s', out['总体评价'][:60])
    out['总体评价'] = ''
  if out['总体评价']:
    invented = name_guard.violations(out['总体评价'], act_orgs)
    if invented:
      logger.warning('总体评价点名了载荷外机构名（仅记录，不置空）（%s）：%s',
                     '、'.join(invented), out['总体评价'][:60])
  # 建议类文本只记日志、不删（它可能复述多章事实，误删代价大于一句措辞不准）
  act_texts = [' '.join(str(v) for v in a.values()) for a in out['行动计划']]
  _, act_bad = trend_guard.filter_texts(act_texts, trend_index)
  if act_bad:
    logger.warning('行动计划里有与趋势数据矛盾的表述（保留）：%s', act_bad[:3])
  _, act_claims = compare_guard.filter_texts(act_texts, _allowed_numbers(act_payload),
                                             _delta_numbers(act_payload))
  if act_claims:
    logger.warning('行动计划里有跨期比较数字无出处的表述（保留）：%s', act_claims[:3])
  _, act_guess = speculation_guard.filter_texts(act_texts, allow_suspicion)
  if act_guess:
    logger.warning('行动计划里有把单量下降当数据问题的表述（保留）：%s',
                   [t[:40] for t, _ in act_guess[:3]])
  act_text = ' '.join([out['总体评价']] + [' '.join(str(v) for v in a.values())
                                           for a in out['行动计划']])
  _audit_numbers(act_text, _trim_stats(stats), _trim_extras(stats),
                 _trim_details(details, weekly), causes)
  _audit_latin(act_text)

  # ③ 执行摘要（**最后做**）：输入是各章要点 + 行动计划 —— 这才是"分析完之后提炼的本篇要点"。
  # 顺序很关键：最早实现里摘要是**最先**跑的（那时各章还没生成），手上只有总量统计，
  # 于是只能复述指标卡片（用户实测反馈"只是把卡片信息复述了一遍"）。这是结构问题，不是措辞问题。
  sum_payload = {'报告名': spec.title, '各章要点': digest,
                 '行动计划': out['行动计划'], '统计': _trim_stats(stats)}
  if risk.get('rows'):
    sum_payload['风险分析'] = risk
  if highlights.get('rows'):
    sum_payload['本期运行亮点'] = highlights['rows']
  if spec.ticket in _FOCUS:
    sum_payload['报告类型'] = spec.title
    sum_payload['本报告关注维度'] = _FOCUS[spec.ticket]
  summary = await _call_validated(_SUMMARY_SYSTEM, sum_payload, retries=retries,
                                  label='执行摘要', pick=lambda d: d['执行摘要'])
  # 摘要可能把某章错误的比较"再复述一遍"（它在章内已被丢弃就不会，但这里兜一道）
  summary, sum_claims = compare_guard.filter_texts(summary, _allowed_numbers(sum_payload),
                                                   _delta_numbers(sum_payload))
  if sum_claims:
    logger.warning('执行摘要丢弃跨期比较数字无出处的句子：%s', sum_claims[:3])
  out['执行摘要'], sum_bad = trend_guard.filter_texts(summary, trend_index)
  if sum_bad:
    logger.warning('执行摘要丢弃与趋势数据矛盾的句子：%s', sum_bad[:3])
  out['执行摘要'], sum_guess = speculation_guard.filter_texts(
    out['执行摘要'], allow_suspicion)
  if sum_guess:
    logger.warning('执行摘要丢弃无依据的数据可靠性猜测：%s',
                   [t[:40] for t, _ in sum_guess[:3]])
  out['执行摘要'] = [_normalize_terms(t) for t in out['执行摘要']]
  _audit_numbers(' '.join(out['执行摘要']), sum_payload)
  _audit_latin(' '.join(out['执行摘要']))
  _audit_template(' '.join(out['执行摘要']), label='执行摘要')

  logger.info('报告解读完成：章节 %d/%d（图说明 %d 张）、行动计划 %d 条、摘要 %d 条、'
              '丢弃（各道闸）%d 条',
              len(out['章节']), sum(1 for r in spec.roles if r in _BRIEFS),
              sum(len(v['图说明']) for v in out['章节'].values()),
              len(out['行动计划']), len(out['执行摘要']), dropped_total)
  return out

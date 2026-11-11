"""分类清单（<available-tools>）生成 —— 从已注册能力聚合平台 → 分类 → 域。

P-2 修复：`<available-tools>` 不再列全量工具名（100 工具名 ≈ 3000+ token，
且 LLM 用不上确切名字），改为「有哪些平台、各有哪些分类、每类覆盖什么」的
分类清单（~250 token）。LLM 不需要提前知道工具名——tool_search 是语义检索，
按自然语言描述意图即可命中；清单的作用是引导它组织搜索 query。

分类词汇（category/domain）与意图路由共享同一套（intent_bindings 的 category
回退），保证 prompt 里看到的分类与绑定表一致。
"""

from config.settings import settings
from service.query.base.platform import all_capabilities, is_mode_gated

# 平台中文名（新增平台时补一行；未登记则回退用平台 id）
_PLATFORM_LABELS = {
  'dcim': '数据中心',
  'dcim_meta': '动力环境数据监控',
  'mgmt': '综合管理平台',
  'security': '安防平台',
  'skill': '技能',
  'browser': '页面操作',
  'web': '联网',
}

# 平台「定位」一句话 —— **只讲这个平台是干什么的**，不讲它现在接了多少数据。
#
# 为什么必须有：多个平台的功能会**语义重叠**（如综合管理平台与 DCIM 都有"设备"），
# 而路由的类别词汇是**跨平台**的（`device` 同时装着两边的设备能力），所以它们会被一起
# promote 到模型面前 —— **模型只能靠这段定义判断该问谁**。
#
# 为什么按"定位"写、而不是"只覆盖 X"：排他式表述会**过时**（DCIM 的告警/异常只是
# 我方尚未接入，不是它没有），而且把平台说小了。定位写清楚，加功能不用改这段话。
# 实际能调什么由 `<available-tools>` 的能力清单约束（该清单已声明"只列已注册能力"）——
# 定位讲"平台是什么"，清单讲"我现在能做什么"，两者分工。
_PLATFORM_DEFINITIONS = {
  'mgmt': (
    '按部门组织的**全量设备台账**（覆盖配电 / 暖通 / 弱电 / 消防四类系统）、'
    '工单（变更 / 事件 / 故障 / 问题 / 阈值 / 供应商服务；动环告警工单见 dcim_meta）、'
    '人员组织、巡检、设备维护计划、文档制度'
  ),
  'dcim_meta': (
    '动环监测（动力环境监控）。可按 **园区 → 楼宇 → 房间 → 设备 → 点位** 的层级，'
    '查看各项运行状态、数据指标分析、告警事件以及**告警处置工单**'
    '（谁处理、响应多快、处理结果是什么）；'
    '另含 **PUE / 能耗统计**（按园区、楼宇与时间口径查 PUE 及各项耗电量）'
  ),
  'skill': '数据分析、图表、看板、报告、联网调研等方法论，以及文档处理与技能管理',
  'browser': (
    '打开内网系统页面（动环监控 / 综合管理平台）并操作：按预置配方筛选后**截图**、'
    '抽取页面表格与正文；或逐步打开页面、点击元素、读取页面文本、截图或**录制页面视频**。'
    '用于"看看这个页面 / 截图给我 / 点进去看看 / 录一段"；要精确数据请用对应平台的数据查询能力'
  ),
  # web 是模式门控平台，**永不进** <available-tools> / planner 词汇表（见 platform.MODE_GATED_PLATFORMS），
  # 这条定位只给工具中心页面展示用。
  'web': '联网检索与网页抓取，仅在「联网调研」任务模式或开启「联网搜索」时可用',
}

# browser 仅配方模式（BROWSER_EXPLORE_ENABLED 关）时的定位：不提交互浏览，
# 否则 planner 会以为有此能力、进而去找一个没注册的工具。
_BROWSER_RECIPE_ONLY = (
  '打开内网系统页面（动环监控 / 综合管理平台）：按预置配方筛选后**截图**，'
  '并抽取页面表格与正文。'
  '用于"看看这个页面 / 截图给我"；要精确数据请用对应平台的数据查询能力'
)

# browser 仅交互模式（BROWSER_ENABLED 关，配方尚未校准）时的定位：同理不提交
# 配方截图，否则模型会去找一个没注册的 browser__<recipe> 工具。
_BROWSER_EXPLORE_ONLY = (
  '打开内网系统页面（动环监控 / 综合管理平台）并逐步操作：打开页面、点击元素、'
  '读取页面文本、截图或**录制页面视频**。'
  '用于"看看这个页面 / 截图给我 / 点进去看看 / 录一段"；'
  '要精确数据请用对应平台的数据查询能力'
)

# 分类中文名（L2，按平台定义的闭合词汇；未登记则回退用 category 原值）
_CATEGORY_LABELS = {
  'org': '组织人事',
  'device': '设备',
  'work_order': '工单',
  'inspection': '巡检',
  'risk': '隐患',
  'report': '周报',
  'alarm': '告警',
  'asset': '资产',
  'capacity': '容量',
  'energy': '能耗',
  'maintenance': '维护',
  'analysis': '数据分析',
  'chart': '图表',
  'document': '文档',
  'config': '配置',
  'skill': '技能',
  'review': '评审',
  'dashboard': '看板',
  'page': '页面操作',
}

# 域中文名（L3，仅展示用；未登记则回退用 domain 原值）
_DOMAIN_LABELS = {
  'person': '人员',
  'dept': '部门',
  'company': '公司',
  'device': '设备',
  'space': '空间',
  'series': '时序数据',
  'fault': '故障',
  'work_order': '工单',
  'inspection': '巡检',
  'risk': '隐患',
  'report': '周报',
  'alarm': '告警',
  'asset': '资产',
  'capacity': '容量',
  'pue': 'PUE',
  'chart': '图表',
  'document': '文档',
  'config': '配置',
  'skill': '技能',
  'review': '评审',
  'dashboard': '看板',
  'capture': '页面捕获',
  'explore': '交互浏览',
}


def _category_entry(category: str, domains: list[str]) -> str:
  """渲染单个分类条目：`分类名(域1/域2)`；域与分类同义或为空时省略括号。

  e.g. org → '组织人事(人员)'；device（域只有 设备）→ '设备'。
  """
  label = _CATEGORY_LABELS.get(category, category)
  if domains and not (len(domains) == 1 and domains[0] == label):
    return f'{label}({"/".join(domains)})'
  return label


def _public_skill_parents() -> list:
  """公开技能的父能力（skill.chart 这种；子能力是 skill.chart.line）。

  用户自建技能（owner 非空）不进清单——它们按用户隔离，由 tool_search 按需命中。
  """
  return [
    cap for cap in all_capabilities()
    if cap.id.startswith('skill.') and not cap.owner and cap.id.count('.') == 1
  ]


def public_skill_tool_names() -> set[str]:
  """公开父技能对应的工具名（skill.chart → skill__chart），供 agent 构建时排除 deferred。"""
  return {cap.id.replace('.', '__') for cap in _public_skill_parents()}


def _skill_entry(cap) -> str:
  """技能条目：`技能名（一句话描述）`。名字用目录名（=调用时要找的名字）。

  描述只取首句、且在首个「（」处截断——frontmatter 的描述常带一长串括号补充，
  按长度硬截会截进括号里，渲染出 `xxx（…（echarts…` 这种破括号。
  """
  name = cap.id.split('.', 1)[1]
  desc = (cap.description or '').strip().split('。')[0].split('（')[0].strip()
  if len(desc) > 32:
    desc = desc[:32].rstrip('，、；; ') + '…'
  return f'{name}（{desc}）' if desc else name


def skill_usage_hint() -> str:
  """技能使用提示（追加在 <available-tools> 之后）。

  技能正文（SKILL.md：选型引导、分析目的→数据形状→图型对照、字段规范）**不进提示词**——
  那会撑爆上下文；但模型必须先知道"有这么个技能、里面写着怎么做"才会去调它。实测不提示时
  5 天只调过 1 次 skill.chart，技能等于不存在。父技能工具不隐藏，可直接调用。
  """
  if not _public_skill_parents():
    return ''
  return (
    '\n\n<skills>\n'
    '做**数据分析 / 图表 / 看板 / 自定义技能**类任务前，先调用对应技能工具（工具名 skill__*，'
    '如 skill__analysis、skill__chart）取它的工作流与规范，**严格照做**再动手：技能里写了'
    '「分析目的→数据形状→pandas 写法」对照、选型引导、各图型字段规范、报告/看板模板等，'
    '不读它会做错。\n'
    '可选图型（按数据特征选，别只用柱/饼）：line area dual_axes column bar pie donut '
    'treemap histogram boxplot violin radar scatter funnel sankey flow_diagram venn '
    'word_cloud liquid mind_map organization_chart network_graph fishbone_diagram '
    'pin_map spreadsheet\n'
    '</skills>'
  )


_MANIFEST_ANCHOR = (
  '本清单只列**已注册**的能力，是「你能做什么」的**权威来源**：被问「你能查什么 / 能做什么」时'
  '以本清单与你的工具 schema 为准，不要凭记忆列举（历史版本里写死的能力清单可能已下线）。'
)


def build_category_manifest() -> str:
  """生成 <available-tools> 的分类清单正文（不含外层标签）。

  Returns:
    锚定语 + 每平台一行的清单字符串；无分类能力时返回空串。形如：
      - mgmt（综合管理平台）：组织人事(人员) / 设备 / 工单 / 巡检 / 隐患 / 周报
      - dcim（数据中心）：告警 / 资产 / 容量
  """
  # platform -> 有序 {category: [去重后的域中文名, ...]}（保持注册序 + 分类首见序）
  by_platform: dict[str, dict[str, list[str]]] = {}
  for cap in all_capabilities():
    if not cap.category or cap.owner:
      continue  # 无分类或用户自建技能不进清单（按需 tool_search 命中）
    if is_mode_gated(cap.id):
      continue  # 模式门控平台（如 web）不进清单：否则等于向所有模式泄露「有联网这回事」
    platform = cap.id.split('.', 1)[0]
    cats = by_platform.setdefault(platform, {})
    domains = cats.setdefault(cap.category, [])
    dlabel = _DOMAIN_LABELS.get(cap.domain or '', cap.domain)
    if dlabel and dlabel not in domains:
      domains.append(dlabel)

  if not by_platform:
    return ''

  lines = []
  for platform, cats in by_platform.items():
    label = _PLATFORM_LABELS.get(platform, platform)
    # 平台定位放在首行 —— 多个平台功能语义重叠时（如都有"设备"），模型靠它判断该问谁
    definition = platform_definition(platform)
    head = f'- {platform}（{label}）：{definition}' if definition else f'- {platform}（{label}）'
    if platform == 'skill':
      # 技能平台特殊处理：按 category/domain 折叠会把 chart 的 40+ 子能力压成一个词「图表」，
      # 模型因此不知道有哪些技能可用（实测 5 天只调过 1 次 skill.chart）。改为列**技能名 +
      # 一句话描述**，让技能在常驻清单里可见、可被指认。
      entries = [_skill_entry(cap) for cap in _public_skill_parents()]
      if entries:
        lines.append(f'{head}\n    可用技能：{" / ".join(entries)}')
      continue
    parts = [_category_entry(category, domains) for category, domains in cats.items()]
    lines.append(f'{head}\n    可查分类：{" / ".join(parts)}')
  return _MANIFEST_ANCHOR + '\n' + '\n'.join(lines)


def platform_display_name(name: str) -> str:
  """平台中文名，未登记回退用平台 id。"""
  return _PLATFORM_LABELS.get(name, name)


def platform_definition(name: str) -> str:
  """平台的「定位」一句话（干什么的）。未登记返回空串。

  browser 按两个开关返回三种措辞（配方 + 交互 / 仅配方 / 仅交互）——定位里写了
  不存在的工具，会让 planner 去 tool_search 找它。
  """
  if name == 'browser' and not settings.BROWSER_EXPLORE_ENABLED:
    return _BROWSER_RECIPE_ONLY
  if name == 'browser' and not settings.BROWSER_ENABLED:
    return _BROWSER_EXPLORE_ONLY
  return _PLATFORM_DEFINITIONS.get(name, '')


def category_display_name(category: str) -> str:
  """分类中文名，未登记回退用 category 原值。"""
  return _CATEGORY_LABELS.get(category, category)


def domain_display_name(domain: str) -> str:
  """域中文名（L3），未登记回退用 domain 原值。"""
  return _DOMAIN_LABELS.get(domain, domain)


def build_label_hints(labels: list[str]) -> dict[str, str]:
  """生成每个意图标签的中文语义提示（供意图识别 prompt 使用，让 LLM 看懂标签含义）。

  Returns:
    {label: '中文名：域词1/域词2'}；域词从绑定表反查能力的 domain，经
    _DOMAIN_LABELS 映射去重，域为空时只返回中文名。
    自维护：新增能力（category/domain）自动带出提示，无需手工维护。
    例：org → '组织人事：人员/部门/公司'；skill → '技能'（skill_creator 无 domain）。
  """
  from service.query.base.intent_bindings import build_intent_bindings
  from service.query.base.platform import get_capability

  bindings = build_intent_bindings()
  hints: dict[str, str] = {}
  for label in labels:
    cn = category_display_name(label)
    domains: list[str] = []
    for cid in bindings.get(label, []):
      cap = get_capability(cid)
      if cap and cap.domain:
        d = _DOMAIN_LABELS.get(cap.domain, cap.domain)
        if d not in domains:
          domains.append(d)
    # 域词与分类同名（如 工单/图表/设备）则省略，避免「工单（工单：工单）」噪音
    domains = [d for d in domains if d != cn]
    hints[label] = f'{cn}：{"、".join(domains)}' if domains else cn
  return hints

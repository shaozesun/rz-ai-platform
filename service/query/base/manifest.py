"""分类清单（<available-tools>）生成 —— 从已注册能力聚合平台 → 分类 → 域。

P-2 修复：`<available-tools>` 不再列全量工具名（100 工具名 ≈ 3000+ token，
且 LLM 用不上确切名字），改为「有哪些平台、各有哪些分类、每类覆盖什么」的
分类清单（~250 token）。LLM 不需要提前知道工具名——tool_search 是语义检索，
按自然语言描述意图即可命中；清单的作用是引导它组织搜索 query。

分类词汇（category/domain）与意图路由共享同一套（intent_bindings 的 category
回退），保证 prompt 里看到的分类与绑定表一致。
"""

from service.query.base.platform import all_capabilities

# 平台中文名（新增平台时补一行；未登记则回退用平台 id）
_PLATFORM_LABELS = {
  'dcim': '数据中心',
  'mgmt': '综合管理平台',
  'security': '安防平台',
  'skill': '技能',
}

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
  'maintenance': '维护',
  'chart': '图表',
  'document': '文档',
  'config': '配置',
  'skill': '技能',
}

# 域中文名（L3，仅展示用；未登记则回退用 domain 原值）
_DOMAIN_LABELS = {
  'person': '人员',
  'dept': '部门',
  'company': '公司',
  'device': '设备',
  'fault': '故障',
  'work_order': '工单',
  'inspection': '巡检',
  'risk': '隐患',
  'report': '周报',
  'alarm': '告警',
  'asset': '资产',
  'capacity': '容量',
  'chart': '图表',
  'document': '文档',
  'config': '配置',
  'skill': '技能',
}


def _category_entry(category: str, domains: list[str]) -> str:
  """渲染单个分类条目：`分类名(域1/域2)`；域与分类同义或为空时省略括号。

  e.g. org → '组织人事(人员)'；device（域只有 设备）→ '设备'。
  """
  label = _CATEGORY_LABELS.get(category, category)
  if domains and not (len(domains) == 1 and domains[0] == label):
    return f'{label}({"/".join(domains)})'
  return label


def build_category_manifest() -> str:
  """生成 <available-tools> 的分类清单正文（不含外层标签）。

  Returns:
    每平台一行的清单字符串；无分类能力时返回空串。形如：
      - mgmt（综合管理平台）：组织人事(人员) / 设备 / 工单 / 巡检 / 隐患 / 周报
      - dcim（数据中心）：告警 / 资产 / 容量
  """
  # platform -> 有序 {category: [去重后的域中文名, ...]}（保持注册序 + 分类首见序）
  by_platform: dict[str, dict[str, list[str]]] = {}
  for cap in all_capabilities():
    if not cap.category or cap.owner:
      continue  # 无分类或用户自建技能不进清单（按需 tool_search 命中）
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
    parts = [_category_entry(category, domains) for category, domains in cats.items()]
    label = _PLATFORM_LABELS.get(platform, platform)
    lines.append(f'- {platform}（{label}）：{" / ".join(parts)}')
  return '\n'.join(lines)

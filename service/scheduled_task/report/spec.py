"""报告规格声明 —— 报告长什么样全在这里，流程不在。

`workflow.py` 是一条**通用**流水线（取数→统计→出图→排版→解读），它只读这份声明。
所以「加一类报告」是加一份 `ReportSpec` + 一份同契约的统计实现（如 `event_compute.py`），
不是再写一条流程。

字段口径的来源与实证（2026-10-14 用真实数据逐项核过，写在 `fields` 里供第 13 章渲染）：

- `level` 取值为 `0`–`4`（`0级`–`4级`）。接口文档没写方向，但业务《事件等级表》规定**数字越小
  等级越高**（0 级最高）；报告按此口径陈述，但**不给处置结果评级**（等级判据属平台硬写，
  2026-10-28 用户明确要求撤掉那类评分）。上一版 agent 报告编过"3级=紧急/4级=未知"，是错的。
- 事件单**没有任何结束/恢复/关闭时间**（`deleteTime` 恒为 0，那是软删标记），所以
  「告警持续时长」算不出来 → 「处理时效」一章用 **验收时长**（`checkEndApprove.approveTime`）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Kpi:
  """指标卡片：`key` 由统计实现计算，`label` 是卡片标题。"""
  key: str
  label: str


@dataclass(frozen=True)
class Chart:
  """一张图：`key` 由统计实现绘制，`file` 是 `charts/<file>.png`，`section` 是归属章节号。

  `width`：**显示宽度上限**（CSS max-width，如 `'62%'`）。必须在这里限，因为渲染层是
  `img { max-width: 100% }` —— 图缩小后在 PDF 里会被**拉伸回满宽**，等于没缩
  （环形图踩过：图本身小，页面上却占满一整行）。不填＝满宽。

  ⚠️ **同类图（同一图型 / 同一 figsize）必须共用同一 `width`**：漏填不会报错，只会在
  PDF 里一宽一窄（变更报告的等级/阶段两张环形图踩过，实测 62% vs 100%）。
  """
  key: str
  file: str
  title: str
  section: int
  width: str = ''
  # 月报专属图（如「月内周节奏」）：周报即使 stats 里没这个键也不显示占位说明
  monthly_only: bool = False
  # 周报改用**详情口径**时的标题（如事件单级别章：周报按「影响时长」、月报按「验收时长」）。
  # 只有该口径的数据**真的存在**时 `render` 才用它，否则仍用 `title`（不虚标口径）。
  weekly_title: str = ''
  # 本章维度所需的数据键：该键为空时**连占位说明都不出**（全国报告没有部门/园区维度）。
  # 只影响「图是否出现」，不改章节结构。
  requires_key: str = ''


# 专业术语表：第 13 章「术语说明」按各报告的 `glossary` 选取（唯一来源，避免五处重复）
TERM_GLOSSARY: dict[str, str] = {
  'P50': '中位数：把统计值从小到大排序，处在正中间的那个值，一半的数据比它小。',
  'P90': '90 分位：把统计值从小到大排序，90% 的数据小于等于它；用来描述"较慢的那部分"。',
  'P95': '95 分位：把统计值从小到大排序，95% 的数据小于等于它；比 P90 更接近极端情况。',
  '分位': '把一组数值从小到大排序后按比例取的位置值，用来描述"多数情况"与"最差情况"，'
          '不受个别极端值的干扰。',
  '环比': '本期与上一期（长度相同的紧邻时段）对比，如本周比上周、本月比上月。',
  '按时率': '在应完成的工单中，按时完成的比例；未回填完成时间的不计入分母。',
  '超时率': '在可判定超时的工单中，超出时限的比例；未回填记录不计入分母。',
  '帕累托': '按数量从多到少排序并累加占比的图（80/20 法则），用于找出贡献最大的少数对象。',
  '老化': '仍未关闭的工单从发生到统计期末滞留的天数；只算存量，不含已关闭单的处理时长。',
  '未关闭': '统计时点阶段不等于关闭期的工单；阶段是当前快照，不是期内流转过程。',
  '周内节奏': '把一个月的日期按自然周（周一为界）与工作日/周末分组，看各周日均单量的差异；'
              '首末周天数可能不足 7 天，比较各周一律用日均，不用单量。',
  '验收时长': '从事件发生到验收完成的时长（列表口径），是流程走完的时间，不是告警持续时长。',
  '影响时长': '平台登记的该事件对业务的实际影响时间（详情口径），与验收时长分属两个口径，'
              '不能互相换算，也不能当成告警持续时长。',
}


@dataclass(frozen=True)
class ReportSpec:
  ticket: str                                # 工单类型 id（与 report_templates.TICKETS 对齐）
  capability: str                            # 取数能力 id
  title: str                                 # 报告名（不含空间/周期后缀）
  filename: str                              # 产物文件名前缀（**必须纯 ASCII**）
  data_source: str                           # 第 13 章「数据来源」的**业务表述**
  sections: tuple[str, ...]                  # 章节标题（顺序即产出顺序）
  # **章节语义角色**（与 sections 等长）：渲染层与 narrate 按它分派 —— 角色相同的章两类共用，
  # 不同的给独立角色（如故障的「专业与设备类型分布」= equipment_specialty）。**加一类报告＝
  # 加一份声明，不改渲染/解读代码**。固定角色：summary/kpi/action/notes（不走注册表）。
  roles: tuple[str, ...]
  kpi: tuple[Kpi, ...]                       # 指标卡片
  charts: tuple[Chart, ...]                  # 图表清单
  data_notes: tuple[str, ...]                # 第 13 章口径说明（**业务语言，不写字段名/实现**）
  # 质量行：(rate_key, 标签, 口径)。计数取 `rate_key.replace('_rate','_n')`、
  # 分母取同名 `_base`（无则总量）—— 数据都在 stats['quality'] 里，渲染层不写死行。
  # 2026-10-29 起这些行由 `risks._quality_rows` 并入「质量与风险分析」章（不再单独成章），
  # 阈值与排查方向在 `risks._QUALITY_RULES`，本字段只提供标签与口径。
  quality_rows: tuple[tuple[str, str, str], ...]
  # 第 7 章时长行：(token, 标签)。token ∈ 闭集 {'valid','p50','p90','p95','mean_max',
  # 'timeout','over_1d'}，由渲染层映射取值/格式化 —— 两类只差 label 与末行
  duration_rows: tuple[tuple[str, str], ...]
  repeat_title: str                          # 第 9 章表格首列头（聚类的口径名，两类不同）
  detail_columns: tuple[str, ...]            # 第 10 章要展示的详情字段标签（两类字段名不同）
  unavailable: tuple[str, ...]               # **显式声明拿不到的项**（防止编造）
  # 以下是带默认值的（dataclass 要求排在所有无默认值字段之后）
  level_label: str = '事件级别'               # 第 4 章的表头用词（故障单是「故障级别」）
  duration_label: str = '验收时长'            # 处理时效章的时长口径名（故障单是「故障处理时长」）
  # 计数单位（变更单是「单」，事件/故障是「起」）—— 正文里的量词由它决定
  count_unit: str = '起'
  max_rows: int = 20000
  # 周报逐单详情表用的**紧凑列**（3 列，逐单列出本期全部工单）；空则回退 detail_columns。
  # 月报不读这个字段，仍用 detail_columns 的全文表格。
  weekly_detail_columns: tuple[str, ...] = ()
  # 第 10 章详情质检口径：(原因字段, 总结字段)。空＝不做质检（事件/故障保持原行为）。
  # 质检是**启发式提示**（是否重复/笼统/模板化），措辞统一为「按规则识别」。
  quality_fields: tuple[str, str] = ()
  # 第 10 章「详情字段汇总」：对这些字段统计填写单数与高频取值（空＝不汇总）。
  detail_summary_fields: tuple[str, ...] = ()
  # 第 10 章逐单表里**需要平台归纳**的详情字段（空＝按原文截断）。阈值单的「调整原因/处置总结」
  # 常是整页枚举（机房/机柜清单），原样列读者抓不到结论 —— 由 LLM 压成一句话；
  # 归纳失败或校验不过时回退全文（不再截断加省略号）。
  detail_condense_fields: tuple[str, ...] = ()
  # 详情口径的**影响时长**分析（时长章小节 + 根因章长尾/成因）。空＝不分析。
  # 非空时 `workflow` 会在取到详情后挂 `details['impact']`；只有详情接口有这个字段，
  # 列表口径的验收时长不能替代（两者不能互相换算）。
  impact_fields: tuple[str, ...] = ()
  # 园区章是否追加「各专业总量」表（事件单：专业分布已并入园区章，总量表就是园区×专业
  # 堆叠图的列合计）。**必须精确门控** —— 故障报告也有 `park` 角色，但它的专业在
  # 「专业与设备类型分布」章，第 5 章不得出现专业表。
  park_specialty_table: bool = False
  # 第 7 章重复清单 + 关联告警是否整体放附录（正文只留一句带跳转的指路）。
  # 只有事件报告开启：它还要挂每个类目的 DCIM 告警子表，正文会被撑得很长。
  repeat_appendix: bool = False
  # 第 13 章「术语说明」选取的词条（键必须存在于 `TERM_GLOSSARY`）
  glossary: tuple[str, ...] = ()


# ⚠️ 第 13 章是给**管理层**看的「数据说明」，只写业务口径。
# 字段名（occurrenceTime / checkEndApprove…）、编码表、`ast.literal_eval` 这类实现细节
# 属于开发文档，**不要写进报告**（第一版写成了"数据库字段映射表"，用户当场指出不该出现）。
_EVENT_DATA_NOTES: tuple[str, ...] = (
  '「处理时效」统计的是**验收时长**（从事件发生到验收完成），**不是告警的持续时长** —— '
  '事件单不记录告警恢复/结束时间，所以把验收时长当作告警持续时间来读会被严重高估。',
  '「事件级别」依据业务《事件等级表》：数字越小等级越高（0 级最高，本期实际出现的级别见'
  '第 4 章表格）。级别只表示事件的业务定级，**不构成对处置结果的评价**。',
  '「重复问题」按事件名称聚类，名称先做去首尾空格处理（源数据里同一条告警存在名称带前导空格的'
  '重复录入），同名出现 2 次及以上计为重复。',
  '「质量与风险」一章只包含验收超时率、驳回率、重复告警占比这三项；'
  '未达中风险阈值的项仍照常列出（指标值不隐藏），只是等级记「—」。',
  '「关键指标综合评分」为**确定性打分**（0–100，越高越好）：规模控制＝100−单量环比增幅'
  '（增长扣分、持平或下降满分）；响应时效＝100−验收超时率；处置质量＝100−驳回率；'
  '重复管控＝100−重复问题占比。上一期按同一口径计算，阶段/状态为当前快照。',
)


EVENT_SPEC = ReportSpec(
  ticket='event',
  capability='mgmt.query_event_ticket',
  title='事件工单分析报告',
  filename='event-report',
  data_source='综合管理平台 · 事件工单',
  sections=(
    '执行摘要',
    '各项指标统计卡片',
    '规模与趋势分析',
    '事件级别分布',
    # 2026-10-29 用户：「专业分布 在园区分布里显示了，去掉专业分布部分或者并入」——
    # 专业总量表就是园区×专业堆叠图的列合计，独立成章无新维度，故并入本章。
    '园区与专业分布',
    '处理时效分析',
    '重复性问题聚类',
    '高频高发问题根因分析',
    '质量与风险分析',
    '综合评价与改进方向',
    '数据说明',
  ),
  roles=('summary', 'kpi', 'trend', 'level', 'park', 'duration',
         'repeat', 'root_cause', 'risk_analysis', 'action', 'notes'),
  kpi=(
    Kpi('total', '工单总量'),
    Kpi('daily_avg', '日均'),
    Kpi('mom', '环比上期'),
    Kpi('top_level', '最多级别'),
    Kpi('top_specialty', '专业集中度'),
    Kpi('park_top2', '园区集中度'),
    Kpi('accept_timeout', '验收超时率'),
    Kpi('repeat', '重复问题'),
  ),
  charts=(
    Chart('scorecard', '00_scorecard', '关键指标综合评分（本期 vs 上一期）', 2, '60%'),
    Chart('trend', '01_trend', '工单量日趋势（本期 vs 上一期）', 3),
    Chart('month_rhythm', '01b_month_rhythm',
          '月内周节奏（工作日 vs 周末 日均单量）', 3, monthly_only=True),
    Chart('level', '02_level', '事件级别分布', 4),
    # 周报改按**影响时长**（详情口径）画：用户在 2026-10-28 明确要求联合分析看业务影响时长；
    # 月报只取代表单详情，退回验收时长口径（标题随之回退，见 `Chart.weekly_title`）。
    Chart('level_duration', '02b_level_duration', '级别 × 验收时长构成', 4,
          weekly_title='级别 × 影响时长构成'),
    Chart('park', '03_park', '各园区工单量（本期 vs 上一期）', 5),
    Chart('park_specialty', '04_park_specialty', '各园区 × 专业构成', 5),
    Chart('accept_hist', '06_accept_hist', '验收时长区间分布', 7),
    Chart('dc_level', '07_dc_level', '机房 × 事件级别（热力图）', 7),
    Chart('accept_box', '08_accept_box', '验收时长分布（按事件级别）', 7),
  ),
  level_label='事件级别', duration_label='验收时长',
  park_specialty_table=True,
  glossary=('P50', 'P90', 'P95', '分位', '环比', '超时率', '周内节奏', '验收时长', '影响时长'),
  data_notes=_EVENT_DATA_NOTES,
  quality_rows=(
    ('timeout_rate', '验收超时率', '验收环节超出时限的工单占比'),
    ('reject_rate', '驳回率', '验收被驳回的工单占比'),
    ('repeat_rate', '重复告警占比', '同名事件发生 2 次及以上的工单占比'),
  ),
  duration_rows=(
    ('valid', '参与统计的验收单'),
    ('p50', '验收时长 P50'), ('p90', '验收时长 P90'), ('p95', '验收时长 P95'),
    ('mean_max', '均值 / 最长'), ('timeout', '验收超时'),
  ),
  repeat_title='重复问题（按名称聚类）',
  detail_columns=('事件名称', '原因分析', '处理结果', '影响时长'),
  weekly_detail_columns=('事件名称', '原因分析', '处理结果'),
  # 详情口径的深度分析（2026-10-28 加）：影响时长分布/长尾单/全量成因归纳。
  # 事件单的列表里没有这几项，全部来自详情接口；只有周报取全量详情，月报取代表单。
  impact_fields=('影响时长', '原因分析', '处理过程', '处理结果'),
  quality_fields=('原因分析', '处理结果'),
  detail_summary_fields=('原因分析', '处理过程', '处理结果'),
  repeat_appendix=True,
  unavailable=(
    '**告警持续时长**：事件单不记录告警的恢复/结束时间，因此无法给出真正的告警持续时长。'
    '报告中有两个时长口径：列表的**验收时长**（流程走完用了多久）与详情的**影响时长**'
    '（业务受影响多久），两者都**不等于**告警持续时长，也**不能互相换算**。',
    '**误报率 / 自愈率 / 准确率 / 漏报率**：源数据里没有误报、自愈、人工确认等标记，'
    '因此这些指标一律不提供（也不做推测）。',
  ),
)

# 故障报告刻意与事件报告**不同构**：故障单的数据长项（设备类型 / 发生地点 / 真处理时长 /
# 品牌型号备件）决定它该用一套专属维度，而不是把事件报告的骨架换几个标签。事件报告把
# 专业并入园区章（2026-10-29）、重复问题仍独立成章；故障报告把专业并入级别章、把重复并进
# 「现存风险」，腾出的章节给设备类型、发生地点、处理时效对比。
# 2026-10-29 起两类报告的**质量指标不再单独成章**（与「风险分析」是同一组指标的两份视图），
# 统一并进「质量与风险分析」一章：质量行恒列（未达阈值等级记「—」），风险项接在其后。
_BREAKDOWN_NOTES: tuple[str, ...] = (
  '「处理时效」统计的是**故障处理时长**（从故障发生到处理完成，平台已有该字段），是真正的'
  '现场处理耗时，与事件单只能用「验收时长」不同。报告另用**验收时长**做对比（回答"卡在处理'
  '还是卡在验收"），但验收环节**经常未回填**，缺值时该对比只给处理时长并注明验收数据不可得。',
  '「故障级别」采用平台上登记的故障级别（2–4 级）。平台未定义级别数值的大小方向，'
  '因此本报告只按级别陈列分布，**不代为判断哪一级更严重**。',
  '「重复故障」按**同类设备在同一个地点反复故障**聚类（设备类型 × 发生地点，出现 2 次及以上）'
  '—— 故障名称几乎不重名，按名称聚类会永远为空；列表没有设备唯一编号，故以"设备类型 @ 地点"'
  '作为治理对象的代理。',
  '「故障设备类型」为平台登记的设备类别（如冷水机组/UPS蓄电池/冷却塔/视频监控系统），'
  '已由服务端解码成中文；第 9 章的设备品牌/型号、更换备件与原因分析来自工单详情原文。',
  '「现存风险」为确定性清单：未关闭、长时间未处理（超 3 天）、重复故障三类，每类给出单数与'
  '代表工单，作为「质量与风险分析」一章的第二张子表。',
  '「质量与风险」一章的指标只包含超 1 天未处理完占比、驳回率、关闭率这三项；'
  '未达中风险阈值的项仍照常列出（指标值不隐藏），只是等级记「—」。',
  '「关键指标综合评分」为**确定性打分**（0–100，越高越好）：规模控制＝100−单量环比增幅'
  '（增长扣分、持平或下降满分）；处理时效＝100−处理时长超 1 天的单占比；'
  '闭环质量＝关闭率×0.6＋(100−驳回率)×0.4；重复故障管控＝100−重复故障占比；'
  '高危故障管控＝100−4 级及以上且未关闭单占比。上一期按同一口径计算。',
)

BREAKDOWN_SPEC = ReportSpec(
  ticket='breakdown',
  capability='mgmt.query_breakdown_ticket',
  title='故障工单分析报告',
  filename='breakdown-report',
  data_source='综合管理平台 · 故障工单',
  level_label='故障级别',
  duration_label='故障处理时长',
  sections=(
    '执行摘要',
    '各项指标统计卡片',
    '故障规模与趋势',
    '故障级别分布',
    '园区横向对比',
    '专业与设备类型分布',
    '故障部门分布与环比',
    '处理时效分析',
    '故障根因与设备质量分析',
    '质量与风险分析',
    '综合评价与改进方向',
    '数据说明',
  ),
  roles=('summary', 'kpi', 'trend', 'level', 'park', 'equipment', 'dept', 'duration',
         'root_cause', 'risk_analysis', 'action', 'notes'),
  kpi=(
    Kpi('total', '故障总量'),
    Kpi('daily_avg', '日均'),
    Kpi('mom', '环比上期'),
    Kpi('top_level', '最多级别'),
    Kpi('equipment_top', '故障最多设备'),
    Kpi('park_top2', '园区集中度'),
    Kpi('p90_duration', '处理时长 P90'),
    Kpi('dept_top', '最多故障部门'),
  ),
  charts=(
    Chart('scorecard', '00_scorecard', '关键指标综合评分（本期 vs 上一期）', 2, '60%'),
    Chart('trend', '01_trend', '故障单量日趋势（本期 vs 上一期）', 3),
    Chart('month_rhythm', '01b_month_rhythm',
          '月内周节奏（工作日 vs 周末 日均单量）', 3, monthly_only=True),
    Chart('level', '02_level', '故障级别分布', 4),
    Chart('park', '03_park', '各园区故障单量（本期 vs 上一期）', 5),
    # 各园区 × 故障级别结构（横向堆叠条）：一眼横向比较"哪个园区的级别结构更重"
    Chart('park_level', '09_park_level', '各园区故障级别结构', 5),
    # 故障单的标志性图：帕累托（哪几类设备贡献了大部分故障）+ 频次×时长散点（治理优先级）
    Chart('equipment_pareto', '04_equipment_pareto', '故障设备类型 Top10（帕累托）', 6),
    Chart('equipment_scatter', '05_equipment_scatter', '设备类型：故障频次 × 处理时长', 6),
    Chart('dept', '06_dept_bar', '各部门故障环比增减（本期 vs 上一期）', 7),
    Chart('duration_hist', '07_duration_hist', '故障处理时长区间分布', 8),
    Chart('duration_compare', '08_duration_compare', '各园区处理时长 vs 验收时长（P50）', 8),
  ),
  glossary=('P50', 'P90', 'P95', '分位', '环比', '帕累托', '未关闭', '超时率', '周内节奏'),
  data_notes=_BREAKDOWN_NOTES,
  quality_rows=(
    ('over_1d_rate', '超 1 天未处理完占比', '处理时长超过 1 天的故障单占比'),
    ('reject_rate', '驳回率', '被驳回的故障单占比'),
    ('closed_rate', '关闭率', '已关闭（阶段＝故障关闭）的故障单占比'),
  ),
  duration_rows=(
    ('valid', '参与统计的故障单'),
    ('p50', '故障处理时长 P50'), ('p90', '故障处理时长 P90'), ('p95', '故障处理时长 P95'),
    ('mean_max', '均值 / 最长'), ('over_1d', '超 1 天未处理完'),
  ),
  repeat_title='重复故障（设备类型 @ 发生地点）',
  detail_columns=('故障名称', '设备品牌', '设备型号', '更换备件', '原因分析'),
  weekly_detail_columns=('故障名称', '设备品牌', '原因分析'),
  unavailable=(
    '**真正的 SLA 达标率**：平台没有 SLA 目标时限字段，因此本报告不提供"是否达标"的判定，'
    '只给处理时长的分布与内部分档（超 1 天 / 超 3 天）。',
    '**单台设备的可靠性（MTBF / 故障率）**：列表没有设备唯一编号，无法按单台设备统计，'
    '重复故障以"设备类型 @ 发生地点"为代理。',
    '**误报率 / 自愈率 / 准确率**：源数据里没有误报、自愈、人工确认等标记，一律不提供。',
    '**与事件工单的关联**：本报告不提供故障单与事件单的关联分析。',
  ),
)

# 变更报告与事件/故障**不同构**：变更单的独特价值不在"哪类问题最多"，而在**变更管理的过程质量**
# —— 计划性（紧急/非计划占比）、流程卡点（阶段/状态、审批与验收时长）、计划准确性
# （计划 vs 实际、按时完成率）、风险控制（详情里的风险识别/控制措施/回退方案）。
# 故新增 change_type / flow / schedule / change_cause 四个专属角色。
_CHANGE_DATA_NOTES: tuple[str, ...] = (
  '「本期变更」按**计划实施开始时间**归属统计期：每张变更单只归入其计划开始所在的那一期，'
  '因此各期之间不会重复计数。平台列表接口按"计划窗口与查询期有交集"返回记录，'
  '服务端会把计划开始不在本期的记录剔除（剔除数量见本章表格）。',
  '「变更等级」采用平台登记的风险等级（1 低风险 / 2 中风险 / 3 高风险）；报告只按登记等级'
  '陈列分布，不代为判定风险高低是否合理。',
  '「变更类型」优先采用平台的新字段（计划型 / 非计划型 / 紧急变更）；该字段为空的老数据'
  '沿用旧字段的中文值。',
  '「阶段」「状态」是**统计时点的当前快照**，不是本期内的流转过程 —— 平台列表没有阶段历史。'
  '对已经结束的历史统计期，多数变更单会显示为关闭期。',
  '「审批时长」「验收时长」取自平台回填的审批与验收记录；「超时」采用平台打的超时标记。'
  '未回填的记录不计入对应时长统计。',
  '「按时完成」＝实施结束时间不晚于计划结束时间，且两个时间都已回填；未回填实施结束时间的'
  '变更单不参与按时率计算。',
  '「当前未关闭」＝阶段不等于关闭期；积压天数按**统计期末**与计划结束时间的差值计算。',
  '第 10 章的变更原因、变更方案、风险识别、风险控制措施与回退方案来自**代表单详情原文**，'
  '只覆盖报告取到的那几张代表单，不是全量统计。',
  '「关键指标综合评分」为**确定性打分**（0–100，越高越好）：规模控制＝100−单量环比增幅'
  '（增长扣分、持平或下降满分）；计划达成＝按时完成率；审批时效＝100−审批超时率；'
  '闭环质量＝(100−未关闭占比)×0.6＋(100−驳回率)×0.4；紧急变更管控＝100−紧急变更占比。'
  '上一期按同一口径计算，阶段/状态为当前快照。',
)

CHANGE_SPEC = ReportSpec(
  ticket='change',
  capability='mgmt.query_change_ticket',
  title='变更工单分析报告',
  filename='change-report',
  data_source='综合管理平台 · 变更工单',
  count_unit='单',
  level_label='变更等级',
  duration_label='审批/验收时长',
  sections=(
    '执行摘要',
    '各项指标统计卡片',
    '变更规模与趋势',
    '变更等级分布',
    '园区横向对比',
    '专业分布',
    '变更类型与紧急度',
    '阶段与状态流转',
    '执行时效与偏差',
    '变更根因与风险控制分析',
    '风险分析',
    '综合评价与改进方向',
    '数据说明',
  ),
  roles=('summary', 'kpi', 'trend', 'level', 'park', 'specialty', 'change_type', 'flow',
         'schedule', 'change_cause', 'risk_analysis', 'action', 'notes'),
  kpi=(
    Kpi('total', '变更总量'),
    Kpi('daily_avg', '日均'),
    Kpi('mom', '环比上期'),
    Kpi('top_level', '最多等级'),
    Kpi('emergency', '紧急变更占比'),
    Kpi('on_time', '按时完成率'),
    Kpi('approve_timeout', '审批超时率'),
    Kpi('backlog', '未关闭变更'),
  ),
  # 等级图是环形图，需显式限宽（漏填＝满宽，PDF 里比同章其他图大一圈）。
  # 「当前阶段分布」图 2026-10-29 整体删除（用户：这张图没必要），第 8 章只留阶段/状态表。
  charts=(
    Chart('scorecard', '00_scorecard', '关键指标综合评分（本期 vs 上一期）', 2, '60%'),
    Chart('trend', '01_trend', '变更单量日趋势（本期 vs 上一期）', 3),
    Chart('month_rhythm', '01b_month_rhythm',
          '月内周节奏（工作日 vs 周末 日均单量）', 3, monthly_only=True),
    Chart('level', '02_level', '变更等级分布', 4, '62%'),
    Chart('park', '03_park', '各园区变更单量（本期 vs 上一期）', 5,
          requires_key='park'),
    Chart('park_level', '04_park_level', '各园区变更等级结构', 5,
          requires_key='park'),
    Chart('change_type', '05_change_type', '各变更类型单量环比变化分解', 7),
    Chart('approve_hist', '07_approve_hist', '审批时长区间分布', 8),
    Chart('delay_hist', '08_delay_hist', '实施完成偏差分布（相对计划结束）', 9),
    Chart('plan_actual', '11_plan_actual', '各变更单计划与实际实施时长', 9),
    Chart('on_time_by_park', '09_on_time_by_park', '各园区按时完成率', 9,
          requires_key='park'),
    Chart('park_specialty', '10_park_specialty', '各园区 × 专业构成', 6,
          requires_key='park'),
    # 单园区专属（全国级没有 dept 键 → 两张图都不出、也不留占位）
    Chart('dept', '12_dept', '各部门变更单量（本期 vs 上一期）', 5,
          requires_key='dept'),
    Chart('stage_duration', '13_stage_duration', '各变更等级审批 / 实施 / 验收时长（P50）', 8,
          requires_key='dept'),
  ),
  glossary=('环比', '按时率', '超时率', '未关闭', 'P50', 'P90', '周内节奏'),
  data_notes=_CHANGE_DATA_NOTES,
  quality_rows=(),
  duration_rows=(),
  repeat_title='',
  detail_columns=('项目名称', '变更原因', '风险控制措施', '回退方案'),
  weekly_detail_columns=('项目名称', '变更原因', '风险控制措施'),
  quality_fields=('变更原因', '风险控制措施'),
  detail_summary_fields=('影响范围',),
  unavailable=(
    '**变更成功率 / 失败率 / 实际回退率**：平台没有记录"变更是否成功""是否实际执行了回退"'
    '的闭环标记（回退方案只是计划中的预案，不代表发生过），因此不提供成功率、失败率与回退率。',
    '**变更引发的事件**：变更单与事件工单之间没有关联字段，无法归属，**平台数据不支持，'
    '需与事件工单联动后补充**。',
    '**各阶段停留时长（全量）与 SLA 达标率**：列表只有当前阶段快照与审批/验收时长，'
    '没有逐阶段的流转历史；平台也没有 SLA 目标时限字段，故不判定是否达标。',
    '**重复变更（同一设备/系统短期内多次变更）**：变更单没有设备标识字段，无法按设备归并，'
    '平台数据不支持。',
  ),
)

# 问题报告与前三类**不同构**：问题单的价值在**存量健康度**——问题压了多久、录入有多慢、
# 哪些问题在反复发生。故新增 level_stage / aging / problem_objects / entry_lag 四个专属角色；
# 不设「处理时效/验收时长」章（列表验收字段实测基本未回填）。
_QUESTION_DATA_NOTES: tuple[str, ...] = (
  '「本期问题」按**问题发生时间**归属统计期：列表接口按发生时间筛选，服务端再按同一口径'
  '归属，保证各期不重复计数；极少数缺发生时间的记录用创建时间补位，并在本章注明补位条数。',
  '「问题级别」采用平台上登记的问题级别（0–4 级）；平台未定义级别数值的大小方向，'
  '报告只按登记级别陈列分布，**不代为判断哪一级更严重**。',
  '「处理阶段」是**统计时点的当前快照**，不是期内的流转过程——平台列表没有阶段历史。'
  '「未关闭」＝当前阶段不是关闭期。',
  '「存量老化」只统计**期末仍未关闭**的问题：滞留天数＝统计期末 − 问题发生时间，'
  '不是真实的解决时长（平台没有关闭/解决时间）。',
  '「录入滞后」＝建单时间 − 问题发生时间；负值属于补录或脏数据，已剔除并单独计数。',
  '「涉及设备/系统」来自自由文本字段，按中英文标点切分后计数；同一张单里的多个对象'
  '会分别计入，因此合计可大于问题单量。',
  '「重复问题」按问题名称聚类，名称先做去首尾与连续空白折叠（源数据里同名存在前导空格），'
  '同名出现 2 次及以上计为重复。',
  '第 10 章的问题描述与处置总结来自**代表单详情原文**，只覆盖报告取到的那几张代表单，'
  '不是全量统计。',
  '「关键指标综合评分」为**确定性打分**（0–100，越高越好）：规模控制＝100−单量环比增幅'
  '（增长扣分、持平或下降满分）；闭环进度＝已关闭占比；老化管控＝100−未关闭单中超期滞留'
  '占比；录入及时按录入滞后 P90 分档（≤1 小时 100、≤24 小时 85、≤72 小时 70、'
  '≤168 小时 55，更长 40）；重复管控＝100−重复问题占比。上一期按同一口径计算。',
)

QUESTION_SPEC = ReportSpec(
  ticket='question',
  capability='mgmt.query_question_ticket',
  title='问题工单分析报告',
  filename='question-report',
  data_source='综合管理平台 · 问题工单',
  count_unit='单',
  level_label='问题级别',
  duration_label='录入滞后',
  sections=(
    '执行摘要',
    '各项指标统计卡片',
    '问题规模与趋势',
    '问题级别与处理阶段',
    '存量老化与滞留分析',
    '园区与专业分布',
    '问题对象分布',
    '录入及时性分析',
    '重复问题与复发',
    '根因与整改跟踪',
    '风险分析',
    '综合评价与改进方向',
    '数据说明',
  ),
  roles=('summary', 'kpi', 'trend', 'level_stage', 'aging', 'park_specialty',
         'problem_objects', 'entry_lag', 'repeat', 'question_cause', 'risk_analysis',
         'action', 'notes'),
  kpi=(
    Kpi('total', '问题总量'),
    Kpi('daily_avg', '日均'),
    Kpi('mom', '环比上期'),
    Kpi('top_level', '最多级别'),
    Kpi('backlog', '未关闭问题'),
    Kpi('aged_30', '超 30 天未关闭'),
    Kpi('entry_lag_p90', '录入滞后 P90'),
    Kpi('repeat', '重复问题'),
  ),
  charts=(
    Chart('scorecard', '00_scorecard', '关键指标综合评分（本期 vs 上一期）', 2, '60%'),
    Chart('trend', '01_trend', '问题单量日趋势（本期 vs 上一期）', 3),
    Chart('month_rhythm', '01b_month_rhythm',
          '月内周节奏（工作日 vs 周末 日均单量）', 3, monthly_only=True),
    Chart('level_stage', '02_level_stage', '问题级别 × 当前处理阶段', 4),
    Chart('aging_hist', '03_aging_hist', '未关闭问题滞留时长分布', 5),
    Chart('dc_aging', '04_dc_aging', '各园区未关闭问题滞留结构', 5),
    Chart('park', '05_park', '各园区问题单量（本期 vs 上一期）', 6),
    Chart('specialty', '06_specialty', '各专业问题单量', 6),
    Chart('equipment_top', '07_equipment_top', '涉及设备/系统 Top10', 7),
    Chart('entry_lag_hist', '08_entry_lag_hist', '问题录入滞后分布（发生 → 建单）', 8),
    Chart('repeat_top', '09_repeat_top', '重复问题 Top10', 9),
    Chart('park_specialty', '10_park_specialty', '各园区 × 专业构成', 6),
  ),
  glossary=('P50', 'P90', '分位', '环比', '老化', '未关闭', '周内节奏'),
  data_notes=_QUESTION_DATA_NOTES,
  quality_rows=(),
  duration_rows=(),
  repeat_title='重复问题（按名称聚类）',
  detail_columns=('问题名称', '问题来源', '涉及设备', '问题描述', '处置总结'),
  weekly_detail_columns=('问题名称', '问题描述', '处置总结'),
  quality_fields=('问题描述', '处置总结'),
  detail_summary_fields=('问题来源', '涉及设备'),
  unavailable=(
    '**真实的解决/关闭时长**：问题单不记录关闭或解决时间，只能统计「当前仍未关闭」的'
    '滞留天数，不能给出已关闭问题的实际处理时长。',
    '**问题与事件/故障的关联数**：问题单与事件单、故障单之间没有关联字段，无法归属，'
    '因此不提供"该问题引发了哪些事件/故障"的统计。',
    '**SLA 达标率**：平台没有 SLA 目标时限字段，不判定是否达标，只给滞留时长的分布与分档。',
    '**误报 / 有效性标记**：源数据里没有误报、有效性等标记，一律不提供（也不做推测）。',
    '**全量历史存量**：列表接口按统计期返回，报告不能给出统计期之前遗留的完整存量规模。'
    '「未关闭」只针对本期问题单。',
  ),
)

# 阈值报告与前四类**不同构**：阈值单没有等级、没有审批时长，价值在**调整意图、发起集中度、
# 热点对象与批量规模、计划提前期、执行达成**。故新增 threshold_intent / threshold_scope /
# hot_items / lead_time / execution / threshold_flow / threshold_cause 七个专属角色。
_THRESHOLD_DATA_NOTES: tuple[str, ...] = (
  '「本期阈值单」按**计划实施开始时间**归属统计期。平台列表接口按"计划窗口与查询期有交集"'
  '返回记录，一张计划跨窗口的单会在多个窗口重复返回；服务端按计划开始时间归属，'
  '保证只计一次（剔除数量见本章表格）。',
  '「调整意图」与「调整对象类型」由问题名称按**固定关键词表**归类，先命中先归类；'
  '无法归类的一律记入「未识别」，报告不做推断。',
  '「计划提前期」＝计划实施开始时间 − 创建时间；负值属于补录或脏数据，已剔除并单独计数。',
  '「批量规模」是问题名称里**并列对象的片段数**（按顿号、逗号、分号、斜杠切分），'
  '用于比较"逐项微调"与"整批调整"，**不是设备精确台数**。',
  '「按时执行」＝实施结束时间不晚于计划结束时间，且两个时间都已回填；未回填实施结束时间的'
  '阈值单不参与按时率计算。',
  '「阶段」「状态」是**统计时点的当前快照**，不是期内的流转过程——平台列表没有阶段历史。'
  '「未关闭」＝当前阶段不是关闭期；超期天数只统计已经超过计划结束时间的单。',
  '「发起人」取自工单的发起人信息；解析不到姓名的记录记入「未标注」，不猜测归属。',
  '「关键指标综合评分」为**确定性打分**（0–100，越高越好）：规模控制＝100−单量环比增幅'
  '（增长扣分、持平或下降满分）；计划前瞻按提前期 P50 分档（≥1 天 100、≥8 小时 85、'
  '≥4 小时 70、≥2 小时 55、≥1 小时 40、不足 1 小时 20）；执行达成＝按时执行率；'
  '闭环质量＝关闭率×0.6＋(100−驳回率)×0.4；高危管控＝100−高风险意图'
  '（取消阈值/关闭屏蔽）占比；数据规范＝调整意图可识别率、发起人解析率、'
  '计划与实施时间完整率的平均值。上一期按同一口径计算，阶段/状态为当前快照。',
  '第 10 章的调整原因、涉及设备与处置总结来自**代表单详情原文**，只覆盖报告取到的那几张'
  '代表单，不是全量统计。',
)

THRESHOLD_SPEC = ReportSpec(
  ticket='threshold',
  capability='mgmt.query_threshold_ticket',
  title='阈值工单分析报告',
  filename='threshold-report',
  data_source='综合管理平台 · 阈值工单',
  count_unit='单',
  level_label='',
  duration_label='计划提前期',
  sections=(
    '执行摘要',
    '各项指标统计卡片',
    '阈值工单规模与趋势',
    '阈值调整意图与方向',
    '专业与园区分布',
    '热点对象与批量调整',
    '计划提前期分析',
    '执行达成情况',
    '当前阶段与积压',
    '代表单调整依据',
    '风险分析',
    '综合评价与改进方向',
    '数据说明',
  ),
  roles=('summary', 'kpi', 'trend', 'threshold_intent', 'threshold_scope', 'hot_items',
         'lead_time', 'execution', 'threshold_flow', 'threshold_cause', 'risk_analysis',
         'action', 'notes'),
  kpi=(
    Kpi('total', '阈值工单总量'),
    Kpi('daily_avg', '日均'),
    Kpi('mom', '环比上期'),
    Kpi('intent_top', '主要调整方向'),
    Kpi('lead_p50', '计划提前期 P50'),
    Kpi('on_time', '按时执行率'),
    Kpi('initiator_top', '发起人集中度'),
    Kpi('hot_share', '最热调整对象'),
  ),
  charts=(
    Chart('scorecard', '00_scorecard', '关键指标综合评分（本期 vs 上一期）', 2, '60%'),
    Chart('trend', '01_trend', '阈值工单量日趋势（本期 vs 上一期）', 3),
    Chart('month_rhythm', '01b_month_rhythm',
          '月内周节奏（工作日 vs 周末 日均单量）', 3, monthly_only=True),
    Chart('intent', '02_intent', '各调整意图工单量（本期 vs 上一期）', 4),
    Chart('specialty', '03_specialty', '各专业阈值工单量', 5),
    Chart('initiator', '04_initiator', '发起人 Top10', 5),
    Chart('object_types', '05_object_types', '调整对象类型分布', 6),
    Chart('batch_hist', '06_batch_hist', '批量规模分布（名称并列对象片段数）', 6),
    Chart('lead_hist', '07_lead_hist', '计划提前期分布', 7),
    Chart('start_delay_hist', '08_start_delay_hist', '实际开始偏差分布（相对计划开始）', 8),
    Chart('on_time_by_specialty', '09_on_time_by_specialty', '各专业按时执行率', 8),
    Chart('intent_park', '10_intent_park', '调整意图 × 园区（热力图）', 4),
  ),
  glossary=('P50', '分位', '环比', '按时率', '超时率', '周内节奏'),
  data_notes=_THRESHOLD_DATA_NOTES,
  quality_rows=(),
  duration_rows=(),
  repeat_title='',
  detail_columns=('项目名称', '调整原因', '涉及设备', '维护时长', '处置总结'),
  weekly_detail_columns=('项目名称', '调整原因', '处置总结'),
  quality_fields=('调整原因', '处置总结'),
  detail_summary_fields=('涉及设备',),
  detail_condense_fields=('调整原因', '处置总结'),
  unavailable=(
    '**审批/验收时长与超时率**：阈值工单列表没有审批、验收时长字段，因此不提供这两项，'
    '也不做推测。',
    '**阈值是否真正生效 / 是否被回退**：平台没有"调整后是否生效""是否回退"的闭环标记，'
    '报告只统计计划与执行的达成情况，不判定业务效果。',
    '**告警抑制效果（误报下降）**：阈值单与告警数据之间没有关联字段，无法量化调整前后'
    '告警量的变化。',
    '**SLA 达标率**：平台没有 SLA 目标时限字段，不判定是否达标，只给计划提前期与'
    '执行偏差的分布。',
    '**批量调整的完整审批链**：列表只有当前阶段快照，批量调整的逐项审批过程无法还原；'
    '第 10 章只取代表单详情原文。',
  ),
)

_SPECS: dict[str, ReportSpec] = {
  EVENT_SPEC.ticket: EVENT_SPEC,
  BREAKDOWN_SPEC.ticket: BREAKDOWN_SPEC,
  CHANGE_SPEC.ticket: CHANGE_SPEC,
  QUESTION_SPEC.ticket: QUESTION_SPEC,
  THRESHOLD_SPEC.ticket: THRESHOLD_SPEC,
}


def get_report_spec(template_id: str | None) -> ReportSpec | None:
  """预设 key（`{ticket}__{park}__{period}`）→ 规格；没登记的实现返回 None（走原 agent 路径）。"""
  if not template_id:
    return None
  parts = template_id.split('__')
  if len(parts) != 3:
    return None
  return _SPECS.get(parts[0])

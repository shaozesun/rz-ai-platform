"""报告规格声明的自检（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_report_spec`

不依赖外部数据/网络。守住三条**跨五类报告**的约定：

1. 每类报告都在第 2 章（`kpi`）声明了 `scorecard` 雷达图，且**必须带 `width`**
   —— 渲染层是 `img { max-width: 100% }`，不显式限宽就会被拉伸回满宽（用户反馈过"太大"）。
2. 每类报告都有一条「综合评分」口径说明（第 13 章），评分不是黑箱。
3. `glossary` 里的每个词条都必须存在于 `TERM_GLOSSARY`（否则第 13 章静默少一行）。
4. 环形图（会限宽的那类）必须显式声明 `width` —— 变更报告的等级图曾漏填、PDF 里
   比同章其他图大一圈（用户实测反馈）。阶段图 2026-10-29 已被用户否掉、整体删除。
5. 每类报告都有「风险分析」章，且**紧贴**在「综合评价与改进方向」之前（改进项由它驱动），
   `action`/`notes` 仍固定在最后两位。
6. 目录锚点与章节一一对应，且 CSS 带 `bookmark-level` / `target-counter`（PDF 书签与页码）。
7. 声明了详情分析（`impact_fields`/`quality_fields`/`detail_summary_fields`）的报告，字段名必须
   真在 `_DETAIL_FIELDS` 白名单里 —— 写错不会报错，只会静默取到空值、那几章没有数据。
8. 事件/故障两类报告的质量指标**不再单独成章**（2026-10-29 与「风险分析」合并）；
   事件报告的「专业分布」也**并入园区章**（2026-10-29，用户：专业在园区分布里已经显示了）：
   事件 11 章、故障 12 章；`'告警质量分析'`/`'专业分布'` 不在事件 sections 里；
   末章前的顺序为「本期整体表现（确定性）→ 总体评价 → 改进项」。
"""

import dataclasses

from core.sandbox.report_style import RZ_CSS
from service.scheduled_task.report.common import _bar_offsets, _bar_width
from service.scheduled_task.report.render import build_html
from service.scheduled_task.report.spec import (
  EVENT_SPEC, _SPECS, TERM_GLOSSARY, get_report_spec,
)
from tools.analysis_tools import _DETAIL_FIELDS

_ALL = tuple(_SPECS.values())


def _scorecard_charts(spec):
  return [c for c in spec.charts if c.key == 'scorecard']


def test_every_spec_has_scorecard_chart_on_chapter_two():
  for spec in _ALL:
    charts = _scorecard_charts(spec)
    assert len(charts) == 1, f'{spec.ticket}: scorecard 图应恰有 1 张，实际 {len(charts)}'
    chart = charts[0]
    assert chart.section == 2, f'{spec.ticket}: scorecard 应归第 2 章（指标卡片章），实际 {chart.section}'
    # 显式限宽是"雷达图太大"的修复点，不能只靠 figsize
    assert chart.width, f'{spec.ticket}: scorecard 必须声明 width（如 60%）'


def test_change_donut_chart_keeps_width():
  """变更报告仅剩等级图是环形图，必须限宽（漏填＝满宽，PDF 里比同章其他图大一圈）。

  2026-10-22 用户实测反馈过两张环形图一 62% 一漏填、PDF 里一大一小；
  2026-10-29 阶段图改横向条形后又被用户否掉（"这张图没必要"）→ 整体删除，
  第 8 章只留阶段/状态表，本断言只锁等级图。
  """
  spec = _SPECS['change']
  widths = {c.key: c.width for c in spec.charts if c.key in ('level', 'stage')}
  assert set(widths) == {'level'}, f'change: 阶段图已删除，只应有等级图声明 {widths}'
  assert widths['level'], f'change: 等级环形图必须限宽（实际 {widths}）'


def test_every_spec_explains_scoring_in_data_notes():
  for spec in _ALL:
    notes = '\n'.join(spec.data_notes)
    assert '综合评分' in notes, f'{spec.ticket}: 口径说明缺少「综合评分」规则'
    assert '100' in notes, f'{spec.ticket}: 评分规则应写明分值口径'


def test_glossary_keys_all_defined():
  for spec in _ALL:
    missing = [t for t in spec.glossary if t not in TERM_GLOSSARY]
    assert not missing, f'{spec.ticket}: 未定义的术语 {missing}'
  # 每类报告都要有术语说明（至少 P50/P90 这类分位词总会出现一个）
  for spec in _ALL:
    assert spec.glossary, f'{spec.ticket}: glossary 为空，第 13 章不会有「术语说明」'


def test_every_spec_has_one_monthly_rhythm_chart_on_chapter_three():
  """月报专属节奏图：五类各恰一张，归第 3 章（规模与趋势），且只有它标 `monthly_only`。"""
  for spec in _ALL:
    monthly = [c for c in spec.charts if c.monthly_only]
    assert len(monthly) == 1, f'{spec.ticket}: monthly_only 图应恰有 1 张，实际 {len(monthly)}'
    chart = monthly[0]
    assert chart.key == 'month_rhythm', f'{spec.ticket}: 月报专属图 key={chart.key}'
    assert chart.section == 3, f'{spec.ticket}: 节奏图应归第 3 章，实际 {chart.section}'
    assert chart.title, f'{spec.ticket}: 节奏图缺标题'


def test_month_rhythm_glossary_defined_and_referenced():
  """「周内节奏」必须在词典里，且五类报告都引用（第 13 章要有这条说明）。"""
  assert '周内节奏' in TERM_GLOSSARY, 'TERM_GLOSSARY 缺少「周内节奏」词条'
  for spec in _ALL:
    assert '周内节奏' in spec.glossary, f'{spec.ticket}: glossary 未引用「周内节奏」'


def test_get_report_spec_resolves_all_presets():
  for spec in _ALL:
    for park in ('nationwide', '廊坊'):
      got = get_report_spec(f'{spec.ticket}__{park}__weekly')
      assert got is spec, f'{spec.ticket}__{park}__weekly 解析失败'
  assert get_report_spec('unknown__nationwide__weekly') is None
  assert get_report_spec('event__nationwide') is None


def test_every_spec_has_risk_analysis_before_action():
  """风险分析章必须紧贴「综合评价与改进方向」之前：改进项由它驱动，中间不许插别的章。"""
  for spec in _ALL:
    assert len(spec.sections) == len(spec.roles), (
      f'{spec.ticket}: sections({len(spec.sections)}) 与 roles({len(spec.roles)}) 长度不等')
    assert 'risk_analysis' in spec.roles, f'{spec.ticket}: 缺 risk_analysis 角色'
    i = spec.roles.index('risk_analysis')
    assert spec.sections[i] == ('质量与风险分析' if spec.ticket in ('event', 'breakdown')
                                else '风险分析'), f'{spec.ticket}: 章节名不对'
    assert spec.roles[i + 1] == 'action', f'{spec.ticket}: 风险分析后面应紧跟 action'
    assert spec.sections[i + 1] == '综合评价与改进方向', f'{spec.ticket}: action 章名不对'
    assert spec.roles[-2:] == ('action', 'notes'), f'{spec.ticket}: action/notes 必须是最后两位'


def test_toc_anchors_match_sections_and_css_has_bookmarks():
  """目录条目 = 章节数、锚点可跳转；CSS 具备 PDF 书签与页码能力。"""
  spec = dataclasses.replace(EVENT_SPEC, sections=('执行摘要', '数据说明'),
                             roles=('summary', 'notes'), charts=(), kpi=())
  stats = {'total': 3, 'window': {'cur': ['2026-10-05', '2026-10-11'],
                                  'prev': ['2026-09-28', '2026-10-04']}}
  html = build_html(stats, {}, {}, spec, set())
  assert '<nav class="toc">' in html
  assert html.count('href="#sec-') == 2, html.count('href="#sec-')
  assert html.count('class="section"') == 2
  assert 'id="sec-1"' in html and 'href="#sec-1"' in html
  for need in ('bookmark-level', 'target-counter(attr(href), page)', 'leader('):
    assert need in RZ_CSS, f'RZ_CSS 缺少 {need}'


def _detail_html(period: str, parks: list | None = None) -> str:
  """极简事件 spec（摘要 / 根因 / 数据说明）+ 3 条逐单详情，用来验附录分流。

  `parks` 给定时按行写 `park`（全国口径），供「园区列」用例断言。
  """
  spec = dataclasses.replace(EVENT_SPEC, sections=('执行摘要', '根因', '数据说明'),
                             roles=('summary', 'root_cause', 'notes'), charts=(), kpi=())
  stats = {'total': 3,
           'window': {'period': period, 'cur': ['2026-10-19', '2026-10-25'],
                      'prev': ['2026-10-12', '2026-10-18']}}
  rows = [
    {'ticketNo': f'E{i}',
     'fields': {'事件名称': f'测试事件{i}', '原因分析': '原因', '处理结果': '处理'}}
    for i in (1, 2, 3)]
  if parks:
    for row, park in zip(rows, parks):
      row['park'] = park
  return build_html(stats, {'rows': rows}, {}, spec, set())


def test_weekly_detail_table_goes_to_appendix():
  """2026-10-29 用户要求：周报的全量逐单长表（实测 178 行）挪到文末附录。

  正文章号不变，附录无编号、排在最后一章之后，目录补一条同锚点条目。
  """
  html = _detail_html('weekly')
  assert 'id="sec-appendix"' in html, '周报有逐单详情时必须出附录'
  assert html.index('id="sec-appendix"') > html.index('id="sec-3"'), '附录必须在最后一章之后'
  chapter = html[html.index('id="sec-2"'):html.index('id="sec-3"')]
  assert '测试事件' not in chapter, '逐单长表不应留在根因章'
  assert '见文末附录' in chapter, '根因章要留一句指路'
  appendix = html[html.index('id="sec-appendix"'):]
  for i in (1, 2, 3):
    assert f'测试事件{i}' in appendix, f'附录缺少第 {i} 单'
  assert '共 3 条' in appendix
  assert 'href="#sec-appendix"' in html, '目录要补附录条目'
  assert html.count('href="#sec-') == 4, html.count('href="#sec-')
  assert '.appendix' in RZ_CSS and 'break-before: page' in RZ_CSS, '附录要单独起页'


def test_monthly_detail_table_stays_in_chapter():
  """月报只有 5 条代表单，表格短，留在根因章、不出附录。"""
  html = _detail_html('monthly')
  assert 'sec-appendix' not in html
  chapter = html[html.index('id="sec-2"'):html.index('id="sec-3"')]
  assert '测试事件1' in chapter
  assert html.count('href="#sec-') == 3


def test_nationwide_detail_table_has_park_column():
  """全国口径的逐单详情表前置「园区」列（用户 2026-11-02：区分不出来园区）。

  数据源与第 5 章园区表一致（列表记录的 `companyName`）；园区级取数没有该字段 → 不出列。
  """
  parks = ['润泽科技发展有限公司', '广东润惠科技发展有限公司', '惠州润信科技发展有限公司']
  weekly = _detail_html('weekly', parks=parks)
  appendix = weekly[weekly.index('id="sec-appendix"'):]
  assert '<th>园区</th>' in appendix
  for i, park in enumerate(parks, 1):
    # 园区列必须不折行（2026-11-09：后面三列是 120 字长文本，它曾被挤成一字一行）
    assert f'<td class="nw">{park}</td><td>测试事件{i}</td>' in appendix, (park, i)

  monthly = _detail_html('monthly', parks=parks)
  chapter = monthly[monthly.index('id="sec-2"'):monthly.index('id="sec-3"')]
  assert '<th>园区</th>' in chapter
  assert '<td class="nw">润泽科技发展有限公司</td><td>测试事件1</td>' in chapter

  # 只修首列：同一行的正文列不带 nw
  assert '<td class="nw">测试事件1</td>' not in appendix

  # 园区级报告（没有 park）→ 不出园区列，表格逐字不变（连 nw 都不出现）
  assert '<th>园区</th>' not in _detail_html('weekly')
  assert '<th>园区</th>' not in _detail_html('monthly')
  assert 'class="nw"' not in _detail_html('weekly')


def test_bar_width_narrows_on_few_categories():
  """柱宽按类别数自适应：类别少时不许再是 matplotlib 默认的 0.8。

  原来 4 个类别（事件级别分布）在满宽图上就是 4 块 36mm 的大色块（用户截图反馈）。
  """
  assert _bar_width(3) < _bar_width(6) < _bar_width(12), (
    _bar_width(3), _bar_width(6), _bar_width(12))
  assert _bar_width(4) <= 0.45, _bar_width(4)      # 本次修复的核心
  assert _bar_width(12) <= 0.7, _bar_width(12)     # 类别多时仍连贯，不放任到 1.0
  # 分组柱：整组不超出单序列宽度，组内各根柱子有缝
  assert _bar_width(5, 2) * 2 <= _bar_width(5) + 0.01
  offs = _bar_offsets(5, 2)
  assert len(offs) == 2 and offs[0] < offs[1]
  assert abs(offs[0] + offs[1]) < 1e-9             # 居中对称
  assert _bar_offsets(5) == [0.0]                  # 单序列不偏移


def test_event_level_duration_chart_on_chapter_four():
  """第 4 章必须有「级别 × 验收时长」联合分析图；周报也要出，故不能标 `monthly_only`。"""
  charts = [c for c in EVENT_SPEC.charts if c.key == 'level_duration']
  assert len(charts) == 1, f'event: level_duration 图应恰有 1 张，实际 {len(charts)}'
  chart = charts[0]
  assert chart.section == 4, f'event: 联合分析图应归第 4 章，实际 {chart.section}'
  assert not chart.monthly_only, 'event: 联合分析图周报/月报都出，不应标 monthly_only'
  assert chart.file == '02b_level_duration', chart.file
  assert chart.title and '验收时长' in chart.title, chart.title
  # 周报改按**影响时长**（详情口径，用户 2026-10-28 明确要求）：标题随口径切换；
  # `title` 仍写验收时长（月报只取代表单详情，退回列表口径）
  assert chart.weekly_title and '影响时长' in chart.weekly_title, chart.weekly_title
  assert '验收时长' not in chart.weekly_title, chart.weekly_title
  others = [c.key for c in EVENT_SPEC.charts if c.weekly_title and c.key != 'level_duration']
  assert not others, f'其余图不按详情口径切换标题：{others}'


def test_event_level_chapter_has_no_percentile_table():
  """第 4 章**不出各级 P50/P90/最长表**（用户 2026-10-29 要求删掉）。

  档位构成在「级别 × 影响时长构成」图上已能读出，再列一张分位表是重复信息、还把章末要点
  挤到最后；分位数据仍下发给解读层（`facts.impact_level`），只是不印。
  """
  stats = {'total': 3, 'level': [{'label': '3级', 'count': 3, 'pct': 100.0}],
           'window': {'period': 'weekly', 'cur': ['2026-10-19', '2026-10-25'],
                      'prev': ['2026-10-12', '2026-10-18']}}
  details = {'rows': [], 'impact': {
    'available': True,
    'by_level': {'levels': ['3级'],
                 'bins': ['≤30分', '30分-2时', '2-8时', '8-24时', '>1天'],
                 'matrix': [[3, 0, 0, 0, 0]],
                 'stats': [{'级别': '3级', '单数': 3, '占比': 100.0, 'P50分钟': 12.0,
                            'P90分钟': 20.0, '最长分钟': 25.0}]}}}
  spec = dataclasses.replace(EVENT_SPEC, sections=('事件级别分布', '数据说明'),
                             roles=('level', 'notes'), kpi=())
  html = build_html(stats, details, {}, spec, set())
  chapter = html[html.index('id="sec-1"'):html.index('id="sec-2"')]
  assert '级别 × 影响时长构成' in chapter, '读图说明应在（证明走的是详情口径）'
  for gone in ('<th>P50</th>', '<th>P90</th>', '<th>最长</th>'):
    assert gone not in chapter, f'第 4 章仍印着分位表列 {gone}'
  assert '工单量' in chapter, '级别分布表必须保留'


def test_only_event_declares_impact_fields():
  """影响时长分析只属事件单（详情接口才有该字段）；其余四类声明为空，第 7/10 章行为不变。"""
  assert EVENT_SPEC.impact_fields == ('影响时长', '原因分析', '处理过程', '处理结果'), \
    EVENT_SPEC.impact_fields
  others = [s.ticket for s in _ALL if s is not EVENT_SPEC and s.impact_fields]
  assert not others, f'只有事件单声明 impact_fields，这些也有：{others}'
  # 新口径必须在术语表里，否则第 13 章不会解释「影响时长」
  assert '影响时长' in EVENT_SPEC.glossary, EVENT_SPEC.glossary


def test_event_declares_detail_quality_and_summary_fields():
  """第 10 章的整体归纳靠这三组声明：质检（原因+结果）、字段汇总（原因/过程/结果）。"""
  assert EVENT_SPEC.quality_fields == ('原因分析', '处理结果'), EVENT_SPEC.quality_fields
  assert EVENT_SPEC.detail_summary_fields == ('原因分析', '处理过程', '处理结果'), \
    EVENT_SPEC.detail_summary_fields


def test_declared_detail_fields_exist_in_detail_whitelist():
  """spec 声明的详情字段名必须真在 `_DETAIL_FIELDS` 里（约定：详情能力 = 列表能力 + `_detail`）。

  字段名写错**不会报错**：`fetch_row_details` 只会给你空串 → 影响时长/质检/汇总静默全空，
  报告照样出、只是这几章没数据。所以在规格层钉住「声明的名字 = 详情真的取的字段」。
  """
  for spec in _ALL:
    fields = set(spec.quality_fields) | set(spec.detail_summary_fields) | set(spec.impact_fields)
    if not fields:
      continue
    detail_cap = f'{spec.capability}_detail'
    assert detail_cap in _DETAIL_FIELDS, f'{spec.ticket}: 详情白名单缺 {detail_cap}'
    allowed = {f[0] for f in _DETAIL_FIELDS[detail_cap]}
    missing = sorted(fields - allowed)
    assert not missing, f'{spec.ticket}: 详情字段未登记 {missing}（白名单：{sorted(allowed)}）'


def test_merged_quality_chapter_for_event_and_breakdown():
  """质量三项并入「质量与风险分析」：事件 11 章、故障 12 章，旧的质量章已消失。

  用户 2026-10-29：「告警质量和风险那不都是一个维度吗，你不觉得重复吗」——
  同一组指标两份视图 → 合并成一章，质量行恒列（等级可为「—」）。
  同日事件报告再把「专业分布」并入园区章（见下一条），故事件比故障少一章。
  """
  ev = _SPECS['event']
  assert len(ev.sections) == len(ev.roles) == 11, ev.sections
  assert '告警质量分析' not in ev.sections and '专业分布' not in ev.sections, ev.sections
  assert 'quality' not in ev.roles and 'specialty' not in ev.roles, ev.roles
  assert ev.sections[8] == '质量与风险分析', ev.sections[8]
  assert ev.roles[8] == 'risk_analysis', ev.roles[8]

  bd = _SPECS['breakdown']
  assert len(bd.sections) == len(bd.roles) == 12, bd.sections
  assert '工单质量与现存风险' not in bd.sections, bd.sections
  assert 'quality' not in bd.roles and 'risk' not in bd.roles, bd.roles
  assert bd.sections[9] == '质量与风险分析', bd.sections[9]
  assert bd.roles[9] == 'risk_analysis', bd.roles[9]
  # 其余三类不分质量章，章节数不受影响
  for key in ('change', 'question', 'threshold'):
    assert len(_SPECS[key].sections) == 13, _SPECS[key].sections


def test_event_specialty_merged_into_park_chapter():
  """事件报告：第 5 章改名「园区与专业分布」，专业表由 `park_specialty_table` 门控。

  重复证据：专业总量表就是「各园区 × 专业」堆叠图的列合计，无新维度。
  **故障报告同样有 `park` 角色，但它第 5 章不得出现专业表**（专业在设备章）。
  """
  ev = EVENT_SPEC
  assert ev.sections[4] == '园区与专业分布', ev.sections[4]
  assert ev.roles[4] == 'park', ev.roles[4]
  assert ev.park_specialty_table is True
  # 两张图仍在第 5 章（园区 vs 上一期 + 园区 × 专业）
  sec5 = {c.key: c.section for c in ev.charts if c.section == 5}
  assert sec5 == {'park': 5, 'park_specialty': 5}, sec5
  # 其他报告不并专业表
  for key in ('breakdown', 'change', 'question', 'threshold'):
    assert _SPECS[key].park_specialty_table is False, key

  # 单园区（无 park 数据）也必须印专业表：专业构成不依赖跨园区对比
  spec = dataclasses.replace(ev, sections=('园区与专业分布',), roles=('park',),
                             charts=(), kpi=())
  stats = {'total': 2, 'specialty': [{'label': '制冷', 'count': 2, 'prev': 1,
                                      'pct': 100}]}
  html = build_html(stats, {}, {}, spec, set())
  assert '无跨园区对比数据' in html and '各专业总量' in html, html
  # 不声明的类型（故障）同样数据下不出专业表
  bd_spec = dataclasses.replace(_SPECS['breakdown'], sections=('园区与专业分布',),
                                roles=('park',), charts=(), kpi=())
  assert '各专业总量' not in build_html(stats, {}, {}, bd_spec, set())


def test_action_chapter_leads_with_positive_summary():
  """章首顺序：本期整体表现（确定性）→ 总体评价（LLM）→ 改进项。

  用户要求「综合评价里先说积极的」：正面段不依赖 LLM，模型挂掉也照出。2026-10-29 再改版式：
  它是**正文结论**，用正常字号 + 淡绿底整块（此前误用 `.note` 9pt 灰字，用户反馈「小字」），
  且逐句一个 `<p>`。
  """
  spec = dataclasses.replace(
    EVENT_SPEC, sections=('执行摘要', '综合评价与改进方向', '数据说明'),
    roles=('summary', 'action', 'notes'), charts=(), kpi=())
  stats = {'total': 3, 'window': {'period': 'weekly',
                                  'cur': ['2026-10-19', '2026-10-25'],
                                  'prev': ['2026-10-12', '2026-10-18']}}
  sentences = ['单量 3 起，与上一期持平，规模可控', '驳回率 4%，处置规范']
  details = {'highlights': {'sentences': sentences, 'summary': '。'.join(sentences) + '。'}}
  html = build_html(stats, details, {'总体评价': '整体平稳。', '行动计划': []}, spec, set())
  i_pos = html.index('本期整体表现')
  i_overall = html.index('整体平稳')
  i_none = html.index('本期各维度未发现需要专项干预的项')
  assert i_pos < i_overall < i_none, (i_pos, i_overall, i_none)
  # 旧的亮点表已撤掉
  assert '本期运行亮点' not in html, '亮点表应已被确定性结论段替换'
  # 正面段是整块（绿条），逐句成 <p>，不再是 .note 小字
  assert '<p class="panel-head">本期整体表现</p>' in html, html[:400]
  assert '<div class="sum-panel">' in html, '正面段缺绿色整块容器'
  for s in sentences:
    assert f'<p>{s}。</p>' in html, s
  assert 'class="note">单量' not in html, '正面段不应用 .note（9pt 灰字）'
  # 总体评价与数值句同一块绿底，块内不分隔（用户 2026-10-30）
  i_panel = html.index('<div class="sum-panel">')
  panel = html[i_panel:html.index('</div>', i_panel)]
  assert '整体平稳。' in panel, panel
  assert '<hr' not in panel and 'sum-divider' not in panel, '块内不应有分隔线'
  # 只有总体评价、没有 highlights 时也要出绿块
  html3 = build_html(stats, {}, {'总体评价': '整体平稳。', '行动计划': []}, spec, set())
  assert '<div class="sum-panel"><p>整体平稳。</p></div>' in html3, html3[:400]
  # 两者都没有 → 不出空绿块
  html4 = build_html(stats, {}, {'行动计划': []}, spec, set())
  assert 'sum-panel' not in html4 and 'panel-head' not in html4
  # 兼容只给 summary 的调用方：按句号切回逐句
  html2 = build_html(stats, {'highlights': {'summary': '。'.join(sentences) + '。'}},
                     {'总体评价': '整体平稳。', '行动计划': []}, spec, set())
  for s in sentences:
    assert f'<p>{s}。</p>' in html2, s


def test_scoring_note_points_to_last_chapter():
  """「评分规则见第 N 章」随章节数变化（合并后事件/故障是 12 章，不能写死 13）。"""
  spec = dataclasses.replace(
    EVENT_SPEC, sections=('各项指标统计卡片', '数据说明'), roles=('kpi', 'notes'),
    charts=(), kpi=())
  stats = {'total': 1, 'window': {'period': 'weekly'},
           'scorecard': {'rows': [{'维度': '规模控制', '含义': '口径', '本期': 100,
                                   '上一期': 96, '变化': '+4'}]}}
  html = build_html(stats, {}, {}, spec, set())
  assert f'评分规则见第 {len(spec.sections)} 章' in html, '章号未随章节数生成'
  assert '评分规则见第 13 章' not in html, '章号写死了 13'


def test_change_single_park_dept_charts_appended_at_end():
  """单园区报告的部门维度：两张图**追加在末尾**（既有图号 00~11 不能漂移），按 `dept` 门控。

  单园区预设只有 1 个园区 → 第 5 章整章与 4 张园区图退化成空占位；部门是园区内部唯一
  可比的横向维度，用它补位。
  """
  spec = _SPECS['change']
  assert len(spec.sections) == 13, spec.sections
  keys = [c.key for c in spec.charts]
  assert keys[-2:] == ['dept', 'stage_duration'], keys
  dept, dur = spec.charts[-2], spec.charts[-1]
  assert (dept.section, dur.section) == (5, 8), (dept.section, dur.section)
  assert dept.requires_key == dur.requires_key == 'dept', (dept, dur)
  # 四张园区图改成 park 门控：单园区报告不再出现「本次无可用数据，未生成」
  park_keys = {c.key for c in spec.charts if c.requires_key == 'park'}
  assert park_keys == {'park', 'park_level', 'on_time_by_park', 'park_specialty'}, park_keys
  # 其余图不受门控（全国级与单园区都画）
  assert all(not c.requires_key for c in spec.charts
             if c.key not in park_keys | {'dept', 'stage_duration'}), spec.charts
  # 第 9 章散点改回线性坐标 → 标题不再写「双对数」
  scatter = next(c for c in spec.charts if c.key == 'plan_actual')
  assert '双对数' not in scatter.title and '计划与实际' in scatter.title, scatter.title
  # `requires_key` 默认空 → 其余四类报告行为完全不变
  for key in ('event', 'breakdown', 'question', 'threshold'):
    assert all(not c.requires_key for c in _SPECS[key].charts), key


def test_change_unavailable_documents_missing_links():
  """用户点名要写明：变更引发事件 / 重复变更都因平台数据不支持而拿不到。"""
  notes = '\n'.join(_SPECS['change'].unavailable)
  assert '变更引发的事件' in notes, notes
  assert '事件工单联动后补充' in notes, notes
  assert '重复变更' in notes and '设备标识' in notes, notes


def test_single_park_change_park_chapter_falls_back_to_departments():
  """单园区第 5 章：园区图不出（也不留占位），改用部门哑铃图 + 部门横比表。"""
  base = _SPECS['change']
  spec = dataclasses.replace(
    base, sections=('园区分布',), roles=('park',), kpi=(),
    charts=tuple(dataclasses.replace(c, section=1)
                 for c in base.charts if c.key in ('park', 'dept')))
  dept = {'classes': 2, 'rows': [
    {'name': 'A分部', 'count': 2, 'prev': 1, 'delta': 1, 'pct': 66.7,
     'approve_p50': 30, 'exec_p50': 120},
    {'name': 'B分部', 'count': 1, 'prev': 0, 'delta': 1, 'pct': 33.3,
     'approve_p50': None, 'exec_p50': None}]}
  existing = {'05_park', '12_dept'}
  html = build_html({'total': 3, 'dept': dept}, {}, {}, spec, existing)
  assert '部门效率对比' in html and 'A分部' in html, html[:600]
  assert '各部门变更单量' in html, '部门哑铃图应正常出图'
  assert '各园区变更单量' not in html, '单园区不该出园区图'
  assert '本次无可用数据' not in html, '不该留「未生成」占位'
  assert '本次统计范围为单个园区' not in html, '有部门维度补位时不再只说"单园区"'
  # 时长一律走可读串（模型与渲染层不自己换单位）
  assert '30 分钟' in html and '2 小时' in html, html[:600]
  # 7 列：单量结构与两类 P50；紧急占比/按时率/驳回率已在第 7/8/9 章，不再并排重复
  assert '紧急占比' not in html and '驳回率' not in html and '按时率' not in html, html[:600]
  assert html.count('<td>—</td>') >= 2, '样本不足的 P50 应显示「—」'

  # 全国帧（有 park 键、没有 dept 键）：部门表与部门图都不出，园区表照旧
  html2 = build_html({'total': 3, 'park': [{'name': '廊坊', 'count': 3, 'prev': 2,
                                            'pct': 100}]}, {}, {}, spec, existing)
  assert '部门效率对比' not in html2 and '各部门变更单量' not in html2, html2[:600]
  assert '各园区变更单量' in html2, html2[:600]
  # 既没有园区也没有部门（理论上不会发生）：退回原有的单园区说明
  html3 = build_html({'total': 0}, {}, {}, spec, set())
  assert '本次统计范围为单个园区' in html3, html3[:400]


def main() -> int:
  """无框架跑法：逐个跑测试函数，打印结果并返回退出码。"""
  tests = [(n, f) for n, f in sorted(globals().items())
           if n.startswith('test_') and callable(f)]
  failed = []
  for name, fn in tests:
    try:
      fn()
      print(f'  OK    {name}')
    except AssertionError as e:
      failed.append(name)
      print(f'  FAIL  {name}: {e}')
    except Exception as e:                        # noqa: BLE001 — 自检脚本要报告一切异常
      failed.append(name)
      print(f'  ERROR {name}: {type(e).__name__}: {e}')
  print(f'\n{len(tests) - len(failed)}/{len(tests)} 通过'
        + (f'，失败：{failed}' if failed else ''))
  return 1 if failed else 0


if __name__ == '__main__':
  raise SystemExit(main())

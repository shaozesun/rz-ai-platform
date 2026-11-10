"""「数据事实 + 依据校验」的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_report_facts`

**不依赖任何外部数据/网络**。合成数据刻意构造成能抓住这次实测事故的形状：

1. **对齐矩阵不能直接喂模型**：`惠州A3分部 = [0,4,10,0]` 与名字相近的 `A3分部 = [1,5,10,1]`
   放在同一份数据里 —— 真实报告就是在这里读出「惠州A3分部 4 级密集」（实为 0）。
   事实化后**每一格坐标写全**，且零格由「其余 N 个组合为 0 单」兜底，模型无从猜。
2. **帕累托的 `cum_pct` 是平行数组**：折进同一条后，累计占比不可能挂到别的设备类型上。
3. **依据校验**：缺依据、引用了不存在编号的句子**整条丢弃**；合规的保留并**拍平成 str**。
4. **确定性**：同一份输入跑两次，结果必须逐字节一致。
"""

import json

from service.scheduled_task.report.common import (
  DEPT_TABLE_LIMIT, _date_axis, _month_rhythm, _mom_top)
from service.scheduled_task.report.facts import _dept_facts, build_facts
from service.scheduled_task.report.narrate import _validate_section
from service.scheduled_task.report.spec import (
  BREAKDOWN_SPEC, EVENT_SPEC, QUESTION_SPEC, THRESHOLD_SPEC)


# 事故原型的复刻：两个名字只差前缀，4 级一列一个 1 一个 0
_DC_LEVEL = {
  'dcs': ['A3分部', '惠州A3分部'],
  'levels': ['1级', '2级', '3级', '4级'],
  'matrix': [[1, 5, 10, 1], [0, 4, 10, 0]],
}

_EQUIPMENT = {
  'rows': [{'name': '冷水机组', 'count': 3, 'pct': 27.3},
           {'name': 'UPS蓄电池', 'count': 2, 'pct': 18.2}],
  'pareto': [{'name': '冷水机组', 'count': 3, 'pct': 27.3},
             {'name': 'UPS蓄电池', 'count': 2, 'pct': 18.2}],
  'cum_pct': [27.3, 45.5],
  'classes': 4,
}


def _facts(stats: dict, role: str, spec=BREAKDOWN_SPEC) -> list[str]:
  return [f['事实'] for f in build_facts(stats, spec, role)]


def test_heatmap_covers_nonzero_cells_only():
  """热力图：**只逐格列非零格**（坐标写全），零格靠「其余 N 个组合为 0 单」兜底。"""
  got = _facts({'dc_level': _DC_LEVEL}, 'duration', EVENT_SPEC)
  for cell in ('A3分部 1级 1 单', 'A3分部 4级 1 单',
               '惠州A3分部 2级 4 单', '惠州A3分部 3级 10 单'):
    assert cell in got, (cell, got)
  # 0 格不得以任何形式出现（它就是被读错的那一格）
  assert not any('惠州A3分部 4级' in t for t in got), got
  assert not any('惠州A3分部 1级' in t for t in got), got
  # 2 行 × 4 列 = 8 格，非零 6 格 → 其余 2 格为 0
  assert '其余 2 个机房×级别组合为 0 单' in got, got
  assert '机房×级别分布（2 行 × 4 列）' in got, got


def test_similar_names_never_mix():
  """**回归闸**（2026-10-15 事故）：只差前缀的两个机房名，事实各带全坐标、不会互相串。"""
  got = _facts({'dc_level': _DC_LEVEL}, 'duration', EVENT_SPEC)
  # 两条 3级 事实必须各自带完整机房名，不能只有"3级 10 单"
  assert 'A3分部 3级 10 单' in got, got
  assert '惠州A3分部 3级 10 单' in got, got
  assert sum(1 for t in got if t.endswith('3级 10 单')) == 2, got
  # 4 级只有 A3分部 有，且事实里的数字来自**它自己那一行**
  four = [t for t in got if '4级' in t]
  assert four == ['A3分部 4级 1 单'], four


def test_pareto_folds_cum_pct():
  """帕累托：`cum_pct` 折进同一条 → 累计占比与设备类型绑死，不可能挂错行。"""
  got = _facts({'equipment': _EQUIPMENT}, 'equipment_specialty')
  assert '冷水机组 3 单（占 27.3%，累计 27.3%）' in got, got
  assert 'UPS蓄电池 2 单（占 18.2%，累计 45.5%）' in got, got
  assert '共 4 类设备，上图仅列出故障最多的 2 类' in got, got


def test_facts_limited_to_declared_charts():
  """事实**只服务报告真会画出来的图**：故障报告已无热力图/园区×专业图，其时长章不得再给机房×级别事实。"""
  got = _facts({'dc_level': _DC_LEVEL}, 'duration')       # 默认 BREAKDOWN_SPEC
  assert not any('机房×级别' in t for t in got), got
  assert not any(c.key in ('dc_level', 'park_specialty') for c in BREAKDOWN_SPEC.charts)


def test_hist_lists_every_bin():
  """直方图：档位**全列**（含 0 单档）——档位与计数绑在一句里。"""
  stats = {'accept': {'hist': {'labels': ['<15分', '15-30分', '30-60分'],
                               'counts': [3, 0, 2]}}}
  got = _facts(stats, 'duration')
  assert '<15分 3 单' in got and '15-30分 0 单' in got and '30-60分 2 单' in got, got
  assert got[0] == '故障处理时长区间分布（共 3 档）', got


def test_trend_binds_date_and_value():
  """日趋势：日期与数值绑在一句里，且增减量由平台算好（模型不用做减法）。"""
  stats = {'trend': {'labels': ['10-05', '10-06'], 'cur': [2, 3], 'prev': [1, 5],
                     'prev_days': ['2026-10-05', '2026-10-06'],
                     'peak_day': '2026-10-06', 'peak_n': 3,
                     'low_day': '2026-10-05', 'low_n': 2}}
  got = _facts(stats, 'trend', EVENT_SPEC)
  assert '本期共 5 起，上一期 6 起（减少 1 起）' in got, got
  assert '10-05：本期 2 起，上一期同日 1 起（增加 1 起）' in got, got
  assert '10-06：本期 3 起，上一期同日 5 起（减少 2 起）' in got, got
  assert '本期单量由高到低：10-06 3 起 > 10-05 2 起' in got, got
  assert '峰值日 10-06（3 起）；低谷日 10-05（2 起）' in got, got


def test_trend_facts_zero_days_and_ties():
  """零单日必须兜底列出；峰/谷并列时全部列出（只报一天会误导）。"""
  stats = {'trend': {'labels': ['10-05', '10-06', '10-07'], 'cur': [4, 0, 4],
                     'prev': [], 'prev_days': [],
                     'peak_day': '2026-10-05', 'peak_n': 4,
                     'low_day': '2026-10-06', 'low_n': 0}}
  got = _facts(stats, 'trend', EVENT_SPEC)
  assert '10-06：本期 0 起（上一期无对位日期）' in got, got
  assert '无单日期：10-06（共 1 天，均为 0 起）' in got, got
  assert '峰值日 10-05、10-07（各 4 起）；低谷日 10-06（0 起）' in got, got


def test_trend_facts_uniform_series():
  """全平趋势（周报常见：多数日期 0 单或每天都 2 单）→ 只给平铺事实，不写峰谷句。

  全 0 时「峰值日=低谷日=所有日期」会自相矛盾，模型据此写出的峰谷判断全是编的。
  """
  for vals in ([0, 0, 0], [2, 2, 2]):
    stats = {'trend': {'labels': ['10-05', '10-06', '10-07'], 'cur': vals,
                       'prev': [], 'prev_days': []}}
    got = _facts(stats, 'trend', EVENT_SPEC)
    assert f'每日均为 {vals[0]} 起（共 3 天）' in got, got
    assert not any('峰值日' in t or '低谷日' in t for t in got), got
    assert not any('由高到低' in t for t in got), got


def test_is_weekly_reads_window():
  """周期判定只看 stats['window']['period']；缺 window 按月报处理（老 stats 兼容）。"""
  from service.scheduled_task.report.facts import is_weekly
  assert is_weekly({'window': {'period': 'weekly'}})
  assert not is_weekly({'window': {'period': 'monthly'}})
  assert not is_weekly({}) and not is_weekly(None)


def test_trend_facts_missing_prev_position_not_zero():
  """月报 31 天 vs 上期 30 天：末位是「无对位日期」，不能写成「上一期 0 起」。"""
  stats = {'trend': {'labels': ['10-30', '10-31'], 'cur': [1, 2],
                     'prev': [3, 0], 'prev_days': ['2026-09-30', ''],
                     'peak_day': '2026-10-31', 'peak_n': 2,
                     'low_day': '2026-10-30', 'low_n': 1}}
  got = _facts(stats, 'trend', EVENT_SPEC)
  assert '10-30：本期 1 起，上一期同日 3 起（减少 2 起）' in got, got
  assert '10-31：本期 2 起（上一期无对位日期）' in got, got
  assert '本期共 3 起，上一期 3 起（持平）' in got, got


def test_park_specialty_matrix_becomes_cells():
  """园区×专业（两串平行名字）→ 逐格事实；空矩阵不崩。"""
  ps = {'parks': ['A园区'], 'specialties': ['制冷', '智能化'],
        'matrix': [[3, 0]]}
  got = _facts({'park_specialty': ps}, 'park', EVENT_SPEC)
  assert 'A园区 制冷 3 单' in got, got
  assert not any('A园区 智能化' in t for t in got), got
  assert '其余 1 个园区×专业组合为 0 单' in got, got
  # 缺列/空块 → 空事实，不抛
  assert _facts({'park_specialty': {'parks': [], 'specialties': [], 'matrix': []}},
                'park', EVENT_SPEC) == []


def test_park_level_matrix_becomes_cells():
  """园区×级别（横向堆叠条的数据源）→ 逐格事实（对齐型结构必须事实化）；空矩阵不崩。"""
  pl = {'parks': ['A园区', 'B园区'], 'levels': ['2级', '3级', '4级'],
        'matrix': [[1, 3, 0], [0, 1, 3]]}
  got = _facts({'park_level': pl}, 'park')                # 默认 BREAKDOWN_SPEC
  for cell in ('A园区 2级 1 单', 'A园区 3级 3 单', 'B园区 3级 1 单', 'B园区 4级 3 单'):
    assert cell in got, (cell, got)
  assert not any('A园区 4级' in t for t in got), got       # 0 格不得出现
  assert '其余 2 个园区×级别组合为 0 单' in got, got
  assert _facts({'park_level': {'parks': [], 'levels': [], 'matrix': []}}, 'park') == []


def test_park_level_facts_scoped_to_park_role():
  """对齐型事实**按章节角色收限**：变更报告的「园区×等级」图在 park 章，
  不得漏进 specialty 章（否则模型在专业章写出离题的园区×等级结论）。"""
  from service.scheduled_task.report.spec import CHANGE_SPEC

  pl = {'parks': ['A园区'], 'levels': ['低风险', '中风险', '高风险'],
        'matrix': [[1, 0, 3]]}
  assert 'A园区 高风险 3 单' in _facts({'park_level': pl}, 'park', CHANGE_SPEC)
  assert _facts({'park_level': pl}, 'specialty', CHANGE_SPEC) == []
  # 事件报告 2026-10-29 起专业并入园区章：事实归 park 角色（图仍在第 5 章），
  # 事件已无 specialty 角色 → 该角色取不到事实
  ps = {'parks': ['A园区'], 'specialties': ['制冷'], 'matrix': [[2]]}
  assert _facts({'park_specialty': ps}, 'park', EVENT_SPEC) == [
    '园区×专业分布（1 行 × 1 列）', 'A园区 制冷 2 单']
  assert _facts({'park_specialty': ps}, 'specialty', EVENT_SPEC) == []
  # 变更报告的专业章仍靠同样的图拿事实（`_r_specialty` 保留路径）
  assert _facts({'park_specialty': ps}, 'specialty', CHANGE_SPEC) == [
    '园区×专业分布（1 行 × 1 列）', 'A园区 制冷 2 单']


def test_named_roles_have_no_facts():
  """具名型结构（级别/专业/重复…）读不出错，**不进事实层**（维度不收限）。"""
  stats = {'level': [{'label': '3级', 'count': 4, 'pct': 50.0}],
           'quality': {'reject_n': 1, 'reject_rate': 12.5},
           'repeat': [{'name': 'X', 'count': 2}]}
  for role in ('level', 'quality', 'repeat', 'root_cause'):
    assert build_facts(stats, EVENT_SPEC, role) == [], role


def test_deterministic():
  """同一份输入 → 事实列表逐字节一致。"""
  stats = {'dc_level': _DC_LEVEL, 'equipment': _EQUIPMENT}
  a = build_facts(stats, BREAKDOWN_SPEC, 'duration')
  b = build_facts(stats, BREAKDOWN_SPEC, 'duration')
  assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


def test_question_level_stage_facts_scoped():
  """问题单：级别×阶段矩阵 → 逐格事实，且只进 level_stage 章。"""
  ls = {'levels': ['3级', '4级'], 'stages': ['处理期', '关闭期'],
        'matrix': [[1, 2], [0, 3]]}
  got = _facts({'level_stage': ls}, 'level_stage', QUESTION_SPEC)
  assert '3级 处理期 1 单' in got and '3级 关闭期 2 单' in got, got
  assert '4级 关闭期 3 单' in got and not any('4级 处理期' in t for t in got), got
  assert '其余 1 个级别×阶段组合为 0 单' in got, got
  assert _facts({'level_stage': ls}, 'aging', QUESTION_SPEC) == []


def test_question_aging_facts_bands_and_matrix():
  """问题单：滞留分档逐档一句 + 园区×滞留档矩阵逐格（都在 aging 章）。"""
  stats = {'aging': {'bins': ['≤7天', '8-14天'], 'counts': [4, 1]},
           'dc_aging': {'dcs': ['A分部', 'B分部'], 'bins': ['≤7天', '8-14天'],
                        'matrix': [[3, 0], [1, 1]]}}
  got = _facts(stats, 'aging', QUESTION_SPEC)
  assert '未关闭问题滞留时长分布（共 2 档）' in got, got
  assert '≤7天 4 单' in got and '8-14天 1 单' in got, got
  assert 'A分部 ≤7天 3 单' in got and 'B分部 8-14天 1 单' in got, got
  assert not any('A分部 8-14天' in t for t in got), got
  assert '其余 1 个空间×滞留档组合为 0 单' in got, got


def test_question_entry_lag_facts():
  """问题单：录入滞后分档全列（含 0 单档）。"""
  stats = {'entry_lag': {'bins': ['≤1时', '1-24时'], 'counts': [0, 3]}}
  got = _facts(stats, 'entry_lag', QUESTION_SPEC)
  assert got[0] == '录入滞后分布（共 2 档）', got
  assert '≤1时 0 单' in got and '1-24时 3 单' in got, got


def test_threshold_lead_and_execution_facts():
  """阈值单：计划提前期直方与开始偏差分档 → 逐档一句，互不串章。"""
  stats = {'lead': {'hist': {'labels': ['≤1天', '1-3天'], 'counts': [2, 1]}},
           'execution': {'start_delay': {'hist': [
             {'档位': '按时', '单数': 3}, {'档位': '延迟<1时', '单数': 1}]}}}
  lead = _facts(stats, 'lead_time', THRESHOLD_SPEC)
  assert lead[0] == '计划提前期分布（共 2 档）', lead
  assert '≤1天 2 单' in lead and '1-3天 1 单' in lead, lead
  ex = _facts(stats, 'execution', THRESHOLD_SPEC)
  assert ex[0] == '实际开始相对计划开始的偏差分布（共 2 档）', ex
  assert '按时 3 单' in ex and '延迟<1时 1 单' in ex, ex
  assert _facts({'lead': stats['lead']}, 'execution', THRESHOLD_SPEC) == []


def test_validate_section_keeps_uncited():
  """依据缺失 / 假编号 / 裸字符串 **只记日志、不丢句**（2026-10-30 降级）。

  唯一保留逐章日志的一天（2026-10-19）里，13 条丢弃中 12 条来自依据校验，删掉的是
  带本章真实数字的分析句；而它抓不到数值关系错误（2026-10-15 事故的依据编号是存在的）。
  """
  data = {
    '要点': [{'文本': 'A园区 4 单最多', '依据': ['F1']},
             {'文本': '这条没写依据'},
             {'文本': '这条引用不存在的编号', '依据': ['F9']},
             {'文本': '这条引用本章数据的键名', '依据': ['级别分布']}],
    '图说明': {'level': [{'文本': '3级最多', '依据': ['F2']}, '裸字符串']},
  }
  sec, dropped = _validate_section(data, ['level'], {'F1', 'F2', '级别分布'})
  assert sec == {'要点': ['A园区 4 单最多', '这条没写依据',
                         '这条引用不存在的编号', '这条引用本章数据的键名'],
                 '图说明': {'level': ['3级最多', '裸字符串']}}, sec
  assert dropped == 0, dropped


def test_validate_section_ignores_unknown_chart_keys():
  """未知图键一律丢弃（模型编键名）。"""
  data = {'图说明': {'编造图': [{'文本': 'x', '依据': ['F1']}]}}
  sec, _ = _validate_section(data, ['level'], {'F1'})
  assert sec == {'要点': [], '图说明': {}}, sec


def test_validate_section_drops_trend_contradiction():
  """趋势章：有依据但与数值关系相反（5 起写成"高于"6 起）的句子照样丢弃。"""
  from service.scheduled_task.report.trend_guard import build_index

  idx = build_index({'labels': ['10-05', '10-11'], 'cur': [6, 5],
                     'prev': [], 'prev_days': []})
  data = {'要点': [{'文本': '10月11日高于10月5日', '依据': ['F1']},
                   {'文本': '10月11日低于10月5日', '依据': ['F2']}],
          '图说明': {}}
  sec, dropped = _validate_section(data, [], {'F1', 'F2'}, trend_index=idx)
  assert sec['要点'] == ['10月11日低于10月5日'], sec
  assert dropped == 1, dropped


def test_validate_section_free_text_when_no_facts():
  """无「数据事实」的章（质量/重复…）：自由文本**不被丢弃**（实测旧行为会把整章清空）。"""
  data = {'要点': ['全国关闭率 87.5%，需下钻部门核实', {'文本': '驳回率为 0', '依据': []}],
          '图说明': {'level': ['裸字符串说明']}}
  sec, dropped = _validate_section(data, ['level'], set(), require_evidence=False)
  assert sec['要点'] == ['全国关闭率 87.5%，需下钻部门核实', '驳回率为 0'], sec
  assert sec['图说明'] == {'level': ['裸字符串说明']}, sec
  assert dropped == 0, dropped


class _Ax:
  """只记录刻度调用的假坐标轴（`_date_axis` 只碰 `set_xticks/set_xticklabels`）。"""

  def __init__(self):
    self.ticks: list = []
    self.labels: list = []
    self.kw: dict = {}

  def set_xticks(self, idx):
    self.ticks = list(idx)

  def set_xticklabels(self, labels, **kw):
    self.labels = list(labels)
    self.kw = kw


def test_date_axis_thins_month_and_keeps_week_intact():
  """月报 31 个标签抽稀到 ≤9 且首尾必在；周报 7 个标签**原样不动**（零旋转）。"""
  month = [f'10-{d:02d}' for d in range(1, 32)]
  ax = _Ax()
  _date_axis(ax, month)
  assert len(ax.ticks) <= 9, ax.ticks
  assert ax.ticks[0] == 0 and ax.ticks[-1] == 30, ax.ticks
  assert ax.labels[0] == '10-01' and ax.labels[-1] == '10-31', ax.labels
  assert ax.kw.get('rotation') == 45, ax.kw

  week = ['10-01', '10-02', '10-03', '10-04', '10-05', '10-06', '10-07']
  ax = _Ax()
  _date_axis(ax, week)
  assert ax.ticks == list(range(7)), ax.ticks
  assert ax.labels == week, ax.labels
  assert 'rotation' not in ax.kw, ax.kw


def test_month_rhythm_buckets_sum_to_total():
  """周桶与工作日/周末拆分都必须回到总数（2026-10 首周四天、末周六天）。"""
  axis = [f'2026-10-{d:02d}' for d in range(1, 32)]
  trend = {'cur': [1] * 31, 'labels': [d[5:] for d in axis]}
  window = {'cur': ['2026-10-01 00:00:00', '2026-10-31 23:59:59']}
  r = _month_rhythm(trend, window, '起')
  assert r['total'] == 31, r['total']
  assert sum(w['count'] for w in r['weeks']) == 31
  assert sum(w['days'] for w in r['weeks']) == 31
  assert r['weeks'][0]['days'] == 4 and r['weeks'][0]['label'] == '第1周', r['weeks'][0]
  assert r['weeks'][-1]['days'] == 6 and r['weeks'][-1]['label'] == '第5周', r['weeks'][-1]
  work, rest = r['split']
  assert work['days'] + rest['days'] == 31
  assert work['count'] + rest['count'] == 31
  assert work['avg'] == 1.0 and rest['avg'] == 1.0
  assert r['weeks'][1]['avg'] == 1.0
  assert [d['label'] for d in r['dow']][0] == '周一'
  assert sum(d['count'] for d in r['dow']) == 31
  assert not _month_rhythm({'cur': []}, window), '无日趋势数据应返回空'


def test_mom_top_sorts_skips_and_truncates():
  """按 |增减| 降序；`prev is None` 与双零行跳过；top 截断；占比变化按行带。"""
  stats = {
    'park': {'rows': [
      {'label': '廊坊', 'count': 12, 'prev': 5, 'delta_count': 7, 'delta_pp': 3.1},
      {'label': '平湖', 'count': 4, 'prev': 4, 'delta_count': 0, 'delta_pp': 0.0},
      {'label': '惠州', 'count': 3, 'prev': None}]},
    'level': [
      {'label': '1级', 'count': 1, 'prev': 9, 'delta_count': -8},
      {'label': '2级', 'count': 0, 'prev': 0, 'delta_count': 0}],
  }
  got = _mom_top(stats, [('园区', 'park'), ('级别', 'level')])
  assert [r['项目'] for r in got] == ['1级', '廊坊', '平湖'], got
  assert got[0]['增减'] == -8
  assert got[1]['占比变化'] == 3.1
  assert '占比变化' not in got[0], got[0]
  assert _mom_top(stats, [('园区', 'park')], top=1)[0]['项目'] == '廊坊'


_RHYTHM = {
  'weeks': [
    {'label': '第1周', 'start': '2026-10-01', 'end': '2026-10-04', 'days': 4,
     'count': 10, 'avg': 2.5, 'work_avg': 2, 'rest_avg': 3.3},
    {'label': '第2周', 'start': '2026-10-05', 'end': '2026-10-11', 'days': 7,
     'count': 21, 'avg': 3, 'work_avg': 3.1, 'rest_avg': 2.5}],
  'split': [{'label': '工作日', 'days': 21, 'count': 60, 'avg': 2.9},
            {'label': '周末', 'days': 10, 'count': 30, 'avg': 3}],
  'dow': [{'label': f'{d}', 'count': n} for d, n in
          zip(('周一', '周二', '周三', '周四', '周五', '周六', '周日'),
              (8, 7, 9, 12, 11, 3, 4))],
  'total': 90, 'unit': '起',
}

_MOM_TOP = [
  {'维度': '园区', '项目': '廊坊', '本期': 12, '上一期': 5, '增减': 7,
   '占比变化': 3.1},
  {'维度': '级别', '项目': '1级', '本期': 4, '上一期': 4, '增减': 0},
]


def test_trend_facts_append_rhythm_and_mom_top_with_continuous_numbers():
  """月报：趋势事实之后接节奏/环比事实，**编号章内连续**（F1…Fn），文案含日均与增减量。"""
  stats = {
    'window': {'cur': ['2026-10-01 00:00:00', '2026-10-31 23:59:59'],
               'period': 'monthly'},
    'trend': {'labels': ['10-01', '10-02'], 'cur': [2, 3], 'prev': [1, 1],
              'prev_days': ['10-01', '10-02']},
    'rhythm': _RHYTHM,
    'mom_top': _MOM_TOP,
  }
  facts = build_facts(stats, EVENT_SPEC, 'trend')
  assert [f['编号'] for f in facts] == [f'F{i}' for i in range(1, len(facts) + 1)]
  text = '\n'.join(f['事实'] for f in facts)
  assert '月内节奏' in text and '日均 2.5 起' in text, text
  assert '工作日日均 2、周末日均 3.3' in text, text
  assert '按星期单量由高到低' in text, text
  assert '环比变化 TOP' in text and '+7 起' in text, text
  assert '占比变化 +3.1 个百分点' in text, text
  assert '持平' in text, text


def test_trend_facts_weekly_unchanged_without_month_keys():
  """周报 stats 没有 rhythm/mom_top 键：事实只到日趋势为止（输出逐字不变）。"""
  stats = {'window': {'cur': ['2026-10-05', '2026-10-11'], 'period': 'weekly'},
           'trend': {'labels': ['10-05', '10-06'], 'cur': [2, 3],
                     'prev': [1, 1], 'prev_days': ['10-05', '10-06']}}
  text = '\n'.join(f['事实'] for f in build_facts(stats, EVENT_SPEC, 'trend'))
  assert '月内节奏' not in text and '环比变化 TOP' not in text, text


def test_dept_facts_capped_like_table():
  """部门事实**只列前 12 个**（与第 5 章表格同一切片），首句说明总数。

  2026-10-29 事故：第 5 章表格只印 12 行，解读却点名了第 13/16 名的部门 —— 读者在表里找不到。
  """
  rows = [{'name': f'D{i}部', 'count': 20 - i, 'prev': 1, 'delta': 19 - i,
           'pct': 5.0, 'approve_p50': 60, 'exec_p50': 120} for i in range(16)]
  got = _dept_facts({'classes': 16, 'rows': rows})
  assert len(got) == 1 + DEPT_TABLE_LIMIT, got
  assert got[0] == '部门维度共 16 个部门，按本期单量降序列出前 12 个', got[0]
  assert 'D12部' not in '\n'.join(got), got
  # 不超过上限时保持原句（周报以外的单园区场景逐字不变）
  small = _dept_facts({'classes': 3, 'rows': rows[:3]})
  assert small[0] == '部门维度共 3 个部门（按本期单量降序）', small[0]


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

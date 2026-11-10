"""问题工单报告统计的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_question_compute`

**不依赖任何外部数据/网络**。合成数据刻意覆盖问题报告独有的坑：

1. **列表接口按「问题发生时间」筛选**：统计期归属以 `occurrenceTime` 为准，
   缺失时用 `createTime` 补位（并单独计数）；两个时间都缺失才算「缺时间」。
2. **存量老化只算未关闭**（stage ≠ 关闭期），口径＝统计期末 − 发生时间。
3. **录入滞后＝建单 − 发生**：负值是补录/脏数据，剔除并计数。
4. **级别 × 阶段矩阵**的行合计必须等于该级别单量（未标注阶段单独成列）。
5. **设备字段是自由文本**：按中英文标点切分后计数，同一单的多个对象分别计入。
6. **重复问题按名称聚类**：名称先去首尾、折叠连续空白。
7. **确定性**：同一份输入跑两次，结果必须逐字节一致。
"""

import json

import pandas as pd

from service.scheduled_task.report.question_compute import compute_stats

_WINDOW = {'cur': ('2026-08-21', '2026-08-27'), 'prev': ('2026-08-14', '2026-08-20')}


def _ms(month_day: str, hour: int) -> int:
  """北京时间某天的整点 → 毫秒时间戳。"""
  ts = pd.Timestamp(f'2026-{month_day} {hour:02d}:00:00', tz='Asia/Shanghai')
  return int(ts.timestamp() * 1000)


def _fixture() -> pd.DataFrame:
  """9 行本期合成数据（1 条窗口外、1 条缺时间、1 条创建时间窗口外的诱饵）。"""
  nan = float('nan')
  return pd.DataFrame([
    # 编号  发生时间          创建时间          级别 阶段 状态 专业 园区  部门   问题名称            涉及设备
    ['Q-1', _ms('08-21', 9), _ms('08-21', 10), 4, 2, 1, 1, '廊坊', 'A分部',
     'UPS蓄电池告警', 'UPS、电池'],
    ['Q-2', _ms('08-22', 9), _ms('08-23', 9), 3, 3, 1, 2, '平湖', 'B分部',
     '空调冷凝水漏水', '空调；漏水'],
    ['Q-3', _ms('08-23', 9), _ms('08-23', 10), 4, 5, 1, 1, '廊坊', 'A分部',
     'UPS蓄电池告警', 'UPS'],
    # 缺发生时间 → 用创建时间补位进本期（fallback_n 计 1）
    ['Q-4', nan, _ms('08-24', 9), 2, 2, 2, 4, '廊坊', 'C分部', ' 门禁  通讯中断 ', '门禁'],
    # 发生时间在上期窗口 → 窗口外（不能因为创建时间在本期就算进来）
    ['Q-5', _ms('08-10', 9), _ms('08-20', 9), 1, 5, 1, 1, '廊坊', 'A分部', '旧问题', ''],
    # 两个时间都缺 → missing_start
    ['Q-6', nan, nan, 1, 5, 1, 1, '廊坊', 'A分部', '无时间问题', ''],
    # 无发生时间、创建时间在窗口外 → 属于别的统计期，不算缺失
    ['Q-7', nan, _ms('08-01', 9), 1, 5, 1, 1, '廊坊', 'A分部', '更早创建', ''],
    # 创建早于发生（负滞后）→ 剔除并计数；名称带多余空白，归一后参与重复聚类
    ['Q-8', _ms('08-25', 9), _ms('08-24', 9), 4, 7, 1, 2, '惠州', 'D分部',
     '    精密空调  高温告警', ''],
    # 第三次出现同名问题
    ['Q-9', _ms('08-26', 9), _ms('08-26', 10), 3, 2, 1, 1, '平湖', 'B分部',
     'UPS蓄电池告警', 'UPS'],
  ], columns=['ticketNo', 'occurrenceTime', 'createTime', 'level', 'stage', 'status',
              'specialty', 'companyName', 'dc', 'projectName', 'involveEquipment'])


def _prev() -> pd.DataFrame:
  return pd.DataFrame([
    ['P-1', _ms('08-15', 9), _ms('08-15', 10), 4, 5, 1, 1, '廊坊', 'A分部', 'UPS蓄电池告警'],
    ['P-2', _ms('08-18', 9), _ms('08-18', 10), 2, 2, 1, 2, '平湖', 'B分部', '空调高温'],
  ], columns=['ticketNo', 'occurrenceTime', 'createTime', 'level', 'stage', 'status',
              'specialty', 'companyName', 'dc', 'projectName'])


def _stats():
  return compute_stats(_fixture(), _prev(), _WINDOW)


def test_scope_by_occurrence_with_create_fallback():
  """按发生时间归属；缺失时用创建时间补位；两个都没有才算缺失。"""
  sf = _stats()['scope_filter']
  assert sf['raw'] == 9 and sf['kept'] == 6, sf
  assert sf['out_of_window'] == 2, sf          # Q-5（发生窗口外）、Q-7（创建窗口外）
  assert sf['missing_start'] == 1, sf          # Q-6
  assert sf['fallback_n'] == 1, sf             # Q-4
  assert sf['missing_label'] == '缺问题发生时间', sf


def test_trend_full_window_axis():
  t = _stats()['trend']
  assert len(t['labels']) == 7 and len(t['cur']) == 7 and len(t['prev']) == 7, t
  assert t['labels'][0] == '08-21' and t['labels'][-1] == '08-27', t['labels']
  # 补位单（Q-4）没有发生时间，不进日趋势
  assert t['cur'] == [1, 1, 1, 0, 1, 1, 0], t['cur']
  assert t['prev'] == [0, 1, 0, 0, 1, 0, 0], t['prev']


def test_level_distribution_and_stage_matrix():
  s = _stats()
  assert {r['label']: r['count'] for r in s['level']} == {
    '2级': 1, '3级': 2, '4级': 3}, s['level']
  ls = s['level_stage']
  assert ls['levels'] == ['2级', '3级', '4级'], ls
  assert ls['stages'] == ['处理期', '验收期', '派发期', '关闭期'], ls['stages']
  mat = dict(zip(ls['levels'], ls['matrix']))
  assert mat['2级'] == [1, 0, 0, 0], mat
  assert mat['3级'] == [1, 1, 0, 0], mat
  assert mat['4级'] == [1, 0, 1, 1], mat
  assert [r['合计'] for r in ls['rows']] == [1, 2, 3], ls['rows']


def test_aging_only_open_tickets():
  ag = _stats()['aging']
  assert ag['open_n'] == 5, ag           # Q-1/2/4/8/9（Q-3 已关闭）
  assert ag['rate'] == 83.3, ag
  assert ag['bins'][0] == '≤7天' and len(ag['bins']) == 6, ag['bins']
  # Q-4 没有发生时间 → 不参与滞留统计，其余 4 单全部落在 ≤7 天
  assert ag['counts'] == [4, 0, 0, 0, 0, 0], ag['counts']
  assert ag['over_30_n'] == 0, ag
  da = _stats()['dc_aging']
  assert da['dcs'] == ['平湖', '廊坊', '惠州'], da['dcs']
  sums = {name: sum(row) for name, row in zip(da['dcs'], da['matrix'])}
  assert sums == {'平湖': 2, '惠州': 1, '廊坊': 1}, sums


def test_entry_lag_bands_and_invalid():
  el = _stats()['entry_lag']
  assert el['valid'] == 4, el            # Q-1/Q-2/Q-3/Q-9
  assert el['invalid_n'] == 1, el        # Q-8 负滞后
  assert el['missing_n'] == 1, el        # Q-4 缺发生时间
  assert el['bins'] == ['≤1时', '1-24时', '1-3天', '3-7天', '>7天'], el['bins']
  assert el['counts'] == [0, 3, 1, 0, 0], el['counts']
  assert el['readable']['p50'] == '1 小时', el


def test_problem_objects_split_and_coverage():
  ob = _stats()['problem_objects']
  assert {r['name']: r['count'] for r in ob['rows']} == {
    'UPS': 3, '电池': 1, '空调': 1, '漏水': 1, '门禁': 1}, ob['rows']
  assert ob['classes'] == 5, ob
  assert ob['covered_n'] == 5 and ob['blank_n'] == 1, ob


def test_repeat_normalizes_whitespace():
  rep = _stats()['repeat']
  assert len(rep) == 1, rep
  assert rep[0]['name'] == 'UPS蓄电池告警' and rep[0]['count'] == 3, rep
  assert rep[0]['parks'] == ['平湖', '廊坊'], rep


def test_repeat_parks_fallback_to_window_scope():
  """单园区列表不带 `companyName` → `涉及园区` 回退到 `window['scope']`（不能空白）。"""
  cur = _fixture().drop(columns=['companyName'])
  s = compute_stats(cur, _prev().drop(columns=['companyName']),
                    {**_WINDOW, 'scope': '廊坊园区'})
  assert s['repeat'][0]['parks'] == ['廊坊园区'], s['repeat']


def test_quality_and_kpi():
  s = _stats()
  q = s['quality']
  assert q['close_n'] == 1 and q['close_rate'] == 16.7, q
  assert q['reject_n'] == 1 and q['reject_rate'] == 16.7, q
  assert q['open_n'] == 5 and q['open_rate'] == 83.3, q
  assert q['repeat_n'] == 3 and q['repeat_classes'] == 1, q
  assert q['repeat_rate'] == 50.0, q
  assert set(s['kpi']) == {'total', 'daily_avg', 'mom', 'top_level', 'backlog',
                           'aged_30', 'entry_lag_p90', 'repeat'}, set(s['kpi'])
  assert s['kpi']['total']['value'] == '6', s['kpi']
  assert s['kpi']['top_level']['value'] == '4级', s['kpi']['top_level']
  assert s['kpi']['backlog']['value'] == '5', s['kpi']['backlog']


def test_detail_targets_deterministic_and_unique():
  nos = _stats()['detail_targets']
  assert len(nos) == 5 and len(set(nos)) == len(nos), nos
  # 滞留最久的未关闭单优先（Q-1：08-21 发生、未关闭）
  assert nos[0] == 'Q-1', nos
  assert 'Q-2' in nos and 'Q-8' in nos, nos


def test_weekly_aging_bands_and_kpi():
  """周报：老化档位只到 7 天，超期口径＝3 天；KPI 标签与数据说明随周期变化。"""
  s = compute_stats(_fixture(), _prev(), {**_WINDOW, 'period': 'weekly'})
  ag = s['aging']
  assert ag['bins'] == ['≤1天', '1-3天', '3-5天', '5-7天'], ag['bins']
  # Q-1（≈6.6 天）、Q-2（≈5.6 天）落 5-7；Q-8（≈2.6）、Q-9（≈1.6）落 1-3
  assert ag['counts'] == [0, 2, 0, 2], ag['counts']
  assert ag['over_days'] == 3 and ag['over_n'] == 2, ag
  assert ag['over_label'] == '超 3 天未关闭', ag
  assert ag['over_30_n'] == 0, ag          # 7 天窗口内不可能有超 30 天的单
  assert s['kpi']['aged_30']['value'] == '2', s['kpi']['aged_30']
  assert s['kpi_labels'] == {'aged_30': '超 3 天未关闭'}, s['kpi_labels']
  assert any('超 3 天未关闭' in n for n in s['notes_extra']), s['notes_extra']
  # 园区×档矩阵列数跟随档位
  assert len(s['dc_aging']['bins']) == 4, s['dc_aging']


def test_monthly_has_no_weekly_extras():
  """月报输出逐字不变：没有 kpi_labels / notes_extra，老化仍是 6 档。"""
  s = _stats()
  assert 'kpi_labels' not in s and 'notes_extra' not in s, s.keys()
  assert len(s['aging']['bins']) == 6 and s['aging']['over_days'] == 30, s['aging']
  assert s['kpi']['aged_30']['value'] == str(s['aging']['over_n']), s['kpi']


def test_park_specialty_matrix():
  """园区×专业：矩阵合计等于本期单量（各项均已归类到已知专业）。"""
  s = _stats()
  ps = s['park_specialty']
  assert len(ps['parks']) == 3 and len(ps['specialties']) == 3, ps
  assert len(ps['matrix']) == len(ps['parks']), ps
  assert all(len(row) == len(ps['specialties']) for row in ps['matrix']), ps
  assert sum(sum(row) for row in ps['matrix']) == s['total'], ps


def test_weekly_detail_all_lists_every_ticket():
  """周报逐单详情：本期全部 ticketNo（保序去重）；月报不产出该字段内容。"""
  weekly = compute_stats(_fixture(), _prev(), {**_WINDOW, 'period': 'weekly'})
  assert weekly['detail_all'] == ['Q-1', 'Q-2', 'Q-3', 'Q-4', 'Q-8', 'Q-9'], \
    weekly['detail_all']
  assert _stats()['detail_all'] == [], '月报 detail_all 为空（消费方只在周报读取）'


def test_without_prev_period():
  s = compute_stats(_fixture(), pd.DataFrame(), _WINDOW)
  assert s['prev_total'] == 0 and s['mom']['pct'] is None, s['mom']
  assert s['trend']['prev'] == [0] * 7, s['trend']


def test_missing_optional_columns_not_crash():
  df = _fixture().drop(columns=['involveEquipment', 'companyName'])
  s = compute_stats(df, df.iloc[0:0], _WINDOW)
  assert s['total'] == 6, s['total']
  assert s['problem_objects']['rows'] == [], s['problem_objects']
  # 没有 companyName 时退回部门（dc）维度，而不是整块消失
  assert {r['name'] for r in s['park']} == {'A分部', 'B分部', 'C分部', 'D分部'}, s['park']


def test_small_sample_suppresses_charts_and_trims_decimals():
  """本期只有 1 单：不出图（charts_suppressed），占比/环比写成整数而非 100.0% / -50.0%。"""
  one = _fixture().iloc[[0]].copy()          # Q-1：08-21 发生，落在本期
  s = compute_stats(one, _prev(), _WINDOW)
  assert s['total'] == 1 and s['charts_suppressed'] is True, s
  assert s['level'][0]['pct'] == 100, s['level']
  assert s['kpi']['mom']['sub'] == '-50%', s['kpi']['mom']
  assert _stats()['charts_suppressed'] is False, '3 单及以上正常出图'


def test_deterministic():
  a = compute_stats(_fixture(), _prev(), _WINDOW)
  b = compute_stats(_fixture(), _prev(), _WINDOW)
  assert json.dumps(a, ensure_ascii=False, default=str) == \
         json.dumps(b, ensure_ascii=False, default=str)


def test_scorecard_dimensions_and_meanings():
  """综合评分：5 个业务维度、0~100、含义非空，且与本章统计口径同源。"""
  s = _stats()
  sc = s['scorecard']
  assert [r['维度'] for r in sc['rows']] == \
         ['规模控制', '闭环进度', '老化管控', '录入及时', '重复管控'], sc
  assert len(sc['cur']) == len(sc['prev']) == 5, sc
  assert all(0 <= v <= 100 for v in sc['cur'] + sc['prev']), sc
  assert all(r['含义'].strip() for r in sc['rows']), sc
  # 规模控制交换参数：本期 2→6 增幅扣到 0，上一期 6→2 下降给满分
  assert (sc['rows'][0]['本期'], sc['rows'][0]['上一期']) == (0, 100), sc
  ag, q = s['aging'], s['quality']
  assert sc['cur'][1] == float(q['close_rate']), sc
  over_rate = round(ag['over_n'] / ag['open_n'] * 100, 1) if ag['open_n'] else 0.0
  assert sc['cur'][2] == round(100 - over_rate, 1), sc
  # 录入滞后 P90=17.1 小时 → 落 ≤24h 档（85 分）
  assert s['entry_lag']['p90'] == 17.1, s['entry_lag']
  assert sc['cur'][3] == 85, sc
  assert sc['cur'][4] == round(100 - q['repeat_rate'], 1), sc


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

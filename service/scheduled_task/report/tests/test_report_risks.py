"""质量与风险分析自检（纯 assert，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_report_risks`

守住五件事：

1. **只算不编**：键缺失 / 分母为 0 就**不出这一行**（不给 0、不猜）。
2. **判据与等级同源**：`判据` 文案由同一组阈值生成，等级必然可复核（印进 PDF 供读者复核）。
3. **质量行恒列**（2026-10-29 起事件/故障的质量指标并入本章）：未达中风险阈值的行仍照常列出，
   只是等级记「—」，指标值不隐藏 —— 低于阈值不是"没有数据"。
4. **五类各有自己的信号**（故障另补「高风险未关闭」与「重复故障占比」，变更看紧急与按时，
   问题看老化与录入滞后，阈值看执行与批量规模）。
5. **排序与上限**：补充风险项高等级在前（同级保留定义顺序）、最多 2 行（有质量行时）
   或 4 行（无质量行的类型）；**质量行不占**补充项名额。
"""

from service.scheduled_task.report import risks as R
from service.scheduled_task.report.spec import (
  BREAKDOWN_SPEC, CHANGE_SPEC, EVENT_SPEC, QUESTION_SPEC, THRESHOLD_SPEC,
)

_EVENT_HIGH = {
  'total': 100,
  'quality': {'timeout_rate': 55, 'timeout_n': 55, 'timeout_base': 100,
              'reject_rate': 25, 'reject_n': 25,
              'repeat_rate': 40, 'repeat_n': 40},
  'repeat': [{'name': 'A', 'count': 12}, {'name': 'B', 'count': 30}],
}

_CHANGE_STATS = {
  'total': 100,
  'change_type': {'emergency_rate': 12},
  'schedule': {'on_time_rate': 80},
  'quality': {'open_n': 10},
}

_QUESTION_STATS = {
  'total': 40,
  'aging': {'over_label': '3 天', 'over_rate': 35, 'over_n': 14, 'open_n': 40},
  'entry_lag': {'p90': 30},
  'quality': {'close_n': 3, 'close_base': 10, 'repeat_n': None, 'repeat_base': 40},
}

_THRESHOLD_STATS = {
  'total': 100,
  'execution': {'on_time_rate': 78},
  'quality': {'reject_n': 25},
  'threshold_flow': {'open': {'n': 5}},
  'batch': {'max': 12},
  'initiator': {'top_share': 60},
}


def _by_name(rows: list[dict]) -> dict:
  return {r['风险项']: r for r in rows}


def test_event_all_high_levels():
  got = R.evaluate(_EVENT_HIGH, EVENT_SPEC)
  rows = _by_name(got['rows'])
  assert got['overall'] == '高', got
  assert rows['验收超时率']['等级'] == '高'          # 55% ≥50%
  assert rows['验收超时率']['判据'] == '≥50% 高 / 20–50% 中 / <20% 不列'
  assert rows['驳回率']['等级'] == '高'              # 25% ≥20%
  assert rows['重复告警占比']['等级'] == '高'        # 40% ≥30%


def test_event_row_display_and_repeat_top():
  rows = _by_name(R.evaluate(_EVENT_HIGH, EVENT_SPEC)['rows'])
  assert rows['验收超时率']['本期表现'] == '55%', rows['验收超时率']
  assert rows['驳回率']['本期表现'] == '25%'
  assert '（25 / 100 单）' in rows['驳回率']['口径'], rows['驳回率']
  # 重复项落到具体告警名：次数最多的那条
  assert '最集中为「B」30 次' in rows['重复告警占比']['口径'], rows['重复告警占比']


def test_event_mid_level_and_dash_rows():
  """质量行是**三行恒列**：低于阈值的那行不消失，等级记「—」。"""
  stats = {'total': 100,
           'quality': {'timeout_rate': 25, 'reject_rate': 5, 'repeat_rate': 20}}
  got = R.evaluate(stats, EVENT_SPEC)
  assert [r['风险项'] for r in got['rows']] == ['验收超时率', '驳回率', '重复告警占比'], got
  assert [r['等级'] for r in got['rows']] == ['中', '—', '中'], got
  assert got['overall'] == '中'


def test_below_threshold_keeps_rows_with_dash():
  stats = {'total': 100,
           'quality': {'timeout_rate': 10, 'reject_rate': 3, 'repeat_rate': 5}}
  got = R.evaluate(stats, EVENT_SPEC)
  assert [r['等级'] for r in got['rows']] == ['—', '—', '—'], got
  assert got['overall'] == '—'


def test_breakdown_quality_and_extra_rows_order():
  stats = {'total': 100,
           'quality': {'over_1d_rate': 45, 'over_1d_n': 45,
                       'reject_rate': 5, 'reject_n': 5,
                       'closed_n': 85, 'closed_rate': 85,
                       'repeat_n': 12},
           'risk': {'high_open_n': 7}}
  got = R.evaluate(stats, BREAKDOWN_SPEC)
  names = [r['风险项'] for r in got['rows']]
  # 三行质量按声明顺序在前（未达阈值记「—」），补充项按高→中排在其后
  assert names == ['超 1 天未处理完占比', '驳回率', '关闭率',
                   '高风险未关闭故障', '重复故障占比'], names
  rows = _by_name(got['rows'])
  assert rows['超 1 天未处理完占比']['等级'] == '高'   # 45% ≥40%
  assert rows['驳回率']['等级'] == '—'                # 5% 未达中阈值
  assert rows['关闭率']['等级'] == '中'               # 85% → 80~90 区间（越低越差）
  assert rows['关闭率']['判据'] == '<80% 高 / 80–90% 中 / ≥90% 不列'
  assert rows['高风险未关闭故障']['本期表现'] == '7 单'
  assert rows['高风险未关闭故障']['判据'] == '≥5 单 高 / 1–4 单 中 / 0 单 不列'
  assert got['overall'] == '高'


def test_extra_rows_capped_but_quality_rows_not():
  """补充风险项最多 2 行，**质量行不占名额**；无质量行的类型仍是 4 行上限。"""
  event = {'total': 100,
           'quality': {'timeout_rate': 55, 'reject_rate': 25, 'repeat_rate': 40}}
  assert len(R.evaluate(event, EVENT_SPEC)['rows']) == 3
  breakdown = {'total': 100,
               'quality': {'over_1d_rate': 45, 'reject_rate': 25, 'closed_rate': 50,
                           'repeat_n': 30},
               'risk': {'high_open_n': 9}, 'repeat': [{'name': 'X', 'count': 30}]}
  got = R.evaluate(breakdown, BREAKDOWN_SPEC)
  assert len(got['rows']) == 3 + 2, got['rows']


def test_breakdown_empty_stats_has_no_rows():
  assert R.evaluate({}, BREAKDOWN_SPEC) == {'rows': [], 'overall': '—'}


def test_change_low_rule_is_inverse():
  rows = _by_name(R.evaluate(_CHANGE_STATS, CHANGE_SPEC)['rows'])
  late = rows['未按时完成率']
  assert late['本期表现'] == '20%', late            # on_time 80% → 未按时 20%
  assert late['等级'] == '高'                       # ≥15%
  assert late['判据'] == '≥15% 高 / 5–15% 中 / <5% 不列'
  assert rows['紧急变更占比']['等级'] == '中'        # 12% → 10%~20%
  assert rows['未关闭工单']['等级'] == '中'          # 10% ≥10%


def test_change_missing_on_time_skips_row():
  stats = {'total': 100, 'change_type': {}, 'schedule': {}, 'quality': {}}
  rows = R.evaluate(stats, CHANGE_SPEC)['rows']
  assert rows == [], rows


def test_question_aging_and_lag():
  rows = _by_name(R.evaluate(_QUESTION_STATS, QUESTION_SPEC)['rows'])
  aging = rows['存量老化（超 3 天）']               # 口径名取自 aging.over_label
  assert aging['本期表现'] == '14 / 40 单未关闭（35%）'
  assert aging['等级'] == '高'                      # ≥30%
  assert rows['录入滞后 P90']['本期表现'] == 'P90 30 小时'
  assert rows['录入滞后 P90']['等级'] == '高'       # ≥24 小时
  assert rows['未关闭工单']['本期表现'] == '7 / 10（70%）未关闭'
  assert '重复问题占比' not in rows                 # repeat_n 缺失 → 不出这一行


def test_question_lag_mid_level():
  stats = {'entry_lag': {'p90': 10}}
  rows = _by_name(R.evaluate(stats, QUESTION_SPEC)['rows'])
  assert rows['录入滞后 P90']['等级'] == '中'       # 8~24 小时
  assert rows['录入滞后 P90']['判据'] == '≥24 小时 高 / 8–24 小时 中 / <8 小时 不列'


def test_threshold_rows_and_overall():
  got = R.evaluate(_THRESHOLD_STATS, THRESHOLD_SPEC)
  rows = _by_name(got['rows'])
  assert got['overall'] == '高', got
  assert rows['未按时执行率']['等级'] == '高'        # on_time 78% → 未按时 22%
  assert rows['审批驳回率']['等级'] == '高'          # 25%
  assert rows['发起人首位集中度']['等级'] == '高'    # 60% ≥50%
  assert rows['最大批量调整规模']['本期表现'] == '12 处并列'
  assert rows['最大批量调整规模']['等级'] == '中'    # 10~19 处
  assert '未关闭工单' not in rows                   # 5% 未达中风险阈值


def test_overall_is_highest_level():
  assert R.evaluate({'batch': {'max': 12}}, THRESHOLD_SPEC)['overall'] == '中'
  assert R.evaluate({}, EVENT_SPEC)['overall'] == '—'


def test_unknown_ticket_has_no_rows():
  class _Spec:
    ticket = 'supplier'
  assert R.evaluate(_EVENT_HIGH, _Spec()) == {'rows': [], 'overall': '—'}


def test_zero_base_skips_share_rows():
  rows = R.evaluate({'total': 0, 'quality': {'reject_n': 0, 'repeat_n': 0}},
                    EVENT_SPEC)['rows']
  assert rows == [], rows


def test_helpers():
  assert R._t(0) == '0' and R._t(12.5) == '12.5' and R._t(50.0) == '50'
  assert R._share(1, 3) == 33.3 and R._share(11, 11) == 100
  assert R._share(0, 0) is None
  # 越低越差（关闭率）：<80 高 / 80–90 中 / ≥90 不列
  assert R._level_low(79.9, 80, 90) == '高'
  assert R._level_low(85, 80, 90) == '中'
  assert R._level_low(90, 80, 90) is None
  assert R._level_low(None, 80, 90) is None


def main() -> int:
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

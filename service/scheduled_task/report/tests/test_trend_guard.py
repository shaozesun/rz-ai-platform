"""日趋势「写反大小关系」拦截闸的口径断言（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_trend_guard`

**不依赖任何外部数据/网络**。样例是 2026-10-15 故障报告的真实形态：
本期 10-05 是 6 起、10-11 是 5 起，正文却写「10 月 11 日……高于 10 月 5 日水平」。

规则只拦**可证明为假**的句子：日期解析不到、区间/百分比、上一期无对位数据一律放行。
"""

import json

from service.scheduled_task.report.trend_guard import (
  build_index, filter_texts, violations)

# 真实事故数据的形状（本期 6 天有单、10-06 为 0；上一期按位置对位）
_TREND = {
  'labels': ['10-05', '10-06', '10-07', '10-08', '10-09', '10-10', '10-11'],
  'cur': [6, 0, 10, 9, 12, 14, 5],
  'prev': [7, 9, 7, 5, 2, 10, 4],
  'prev_days': ['2026-09-28', '2026-09-29', '2026-09-30', '2026-10-01',
                '2026-10-02', '2026-10-03', '2026-10-04'],
  'peak_day': '2026-10-10', 'peak_n': 14,
  'low_day': '2026-10-06', 'low_n': 0,
}

_INCIDENT = ('10 月 11 日数据回落至 5 起，虽低于本期峰值但高于 10 月 5 日水平')


def _idx():
  return build_index(_TREND, '起')


def test_incident_sentence_dropped():
  """事故原句：10-11（5 起）并不高于 10-05（6 起）→ 必须被拦。"""
  bad = violations(_INCIDENT, _idx())
  assert bad, '事故原句没有被拦截'


def test_corrected_sentence_kept():
  """改成「低于 10 月 5 日」就与数据一致 → 放行。"""
  text = '10 月 11 日数据回落至 5 起，虽低于本期峰值但也低于 10 月 5 日水平'
  assert violations(text, _idx()) == [], violations(text, _idx())


def test_true_comparisons_kept():
  assert violations('10月5日高于10月11日', _idx()) == []
  assert violations('10月11日低于10月5日', _idx()) == []
  assert violations('10月11日5起少于10月5日6起', _idx()) == []


def test_date_format_variants():
  """模型会混用 `10月5日`/`10-05`/`10/05`/全角数字，都要认。"""
  for text in ('10月11日高于10月5日', '10-11高于10-05',
               '10/11高于10/05', '１０月１１日高于１０月５日'):
    assert violations(text, _idx()), text


def test_same_day_delta():
  """较上一期同日：10-11 是 5 vs 4（增加 1），写成减少或增加 2 必须被拦。"""
  assert violations('10-11较上一期同日增加1起', _idx()) == []
  assert violations('10-11较上一期同日减少1起', _idx())
  assert violations('10-11较上一期同日增加2起', _idx())


def test_same_day_delta_without_direction_not_flagged():
  """「较上一期同日14起」里的 14 是**基数**不是差值（2026-10-30 修误杀）。

  旧正则的方向词是可选组，会把基数当差值，判「实际 -12，文中 14」——生产日志里出现过。
  方向词缺失时一律放行；带方向词（含「骤减」这类强化词）才校验差值。
  """
  idx = build_index({'labels': ['10-17'], 'cur': [2], 'prev': [14],
                     'prev_days': ['2026-10-10']})
  for text in ('10-17单日2起较上一期同日14起骤减12起',
               '10-17单日2起较上一期同日14起',
               '10-17较上一期同日骤减12起',
               '10-17较上一期同日减少12起'):
    assert violations(text, idx) == [], (text, violations(text, idx))
  assert violations('10-17较上一期同日骤减3起', idx)   # 真差值写错仍要拦
  assert violations('10-17较上一期同日增加12起', idx)  # 方向写反也要拦


def test_explicit_subtraction():
  """「较 10-05 的 6 起减少 1 起」= 5，正确；引用的基数或差值错了都要拦。"""
  assert violations('10-11较10-05的6起减少1起', _idx()) == []
  assert violations('10-11较10-05的6起减少2起', _idx())
  assert violations('10-11较10-05的9起减少1起', _idx())
  assert violations('10-11比10-05少1起', _idx()) == []
  assert violations('10-11比10-05少2起', _idx())


def test_peak_low_attribution():
  assert violations('10-05 单量最多', _idx())          # 实为 6 起，峰值 14
  assert violations('10-05为峰值', _idx())
  assert violations('10-10 单量最多', _idx()) == []
  assert violations('10-11 最少', _idx())              # 实为 5 起，谷值是 10-06 的 0
  assert violations('10-06 最少', _idx()) == []
  # 紧邻搭配才认：「峰值但也低于 10-05」不能被读成「峰值是 10-05」
  assert violations('10-11低于本期峰值，也低于10-05水平', _idx()) == []
  # 并列：10-07 与 10-10 都是 14 起，说哪一个是峰值都放行
  tie = dict(_TREND, cur=[6, 0, 14, 9, 12, 14, 5])
  assert violations('10-10 峰值', build_index(tie)) == []
  assert violations('10-07 峰值', build_index(tie)) == []


def test_multiples():
  assert violations('较上一期同日翻六倍', _idx())
  assert violations('10-05是上一期同日的7倍', _idx())    # 6/7≈0.86，不是 7 倍
  assert violations('10-10是上一期同日的5倍', _idx())    # 14/10=1.4，不是 5 倍
  # 与数据对得上的倍数放行
  assert violations('10-10是上一期同日的1.4倍', _idx()) == []


def test_multiple_without_date_not_guessed():
  """无日期锚点、也不是总量口径的倍数不得拿日单量总量去比。

  2026-10-24 实测误杀：摘要里「A3 分部审批 P50 … 是全场均值的 7.5 倍」被丢弃，
  理由「倍数不符（实际 1.27 倍）」——1.27 是本单元测试数据的 56/44（日趋势总量比），
  而句子比的是时长。倍数的基数对不上就不该猜。
  """
  text = 'A3 分部审批 P50 时长高达 873.3 分钟，是全场均值（约 115 分钟）的 7.5 倍'
  assert violations(text, _idx()) == [], violations(text, _idx())


def test_multiple_on_period_total_still_checked():
  """句子明说总量口径时仍用总量比核对（56/44≈1.27）：对得上放行，对不上拦。"""
  assert violations('本期总量是上一期的1.27倍', _idx()) == []
  assert violations('本期总量是上一期的7.5倍', _idx())


def test_ranges_and_percentages_kept():
  assert violations('10-05~10-11 共 56 起，占总量的 100%', _idx()) == []
  assert violations('10-06 单量为 0，占比 0.0%', _idx()) == []


def test_missing_prev_position_is_not_zero():
  """月报 31 天 vs 上期 30 天：末位无对位日期 → 不做「较上一期同日」判定。"""
  idx = build_index({'labels': ['10-30', '10-31'], 'cur': [1, 2],
                     'prev': [3, 0], 'prev_days': ['2026-09-30', '']})
  assert violations('10-31较上一期同日增加2起', idx) == []
  assert violations('10-31低于10-30', idx)            # 2 < 1 为假


def test_ambiguous_subject_not_flagged():
  """句中出现多个候选主语时不猜（只拦能唯一确定主语的比较）。"""
  text = '10-05为6起，10-11为5起，高于10-09水平'
  assert violations(text, _idx()) == []


def test_empty_index_passes():
  assert violations(_INCIDENT, {}) == []
  assert violations(_INCIDENT, None) == []
  assert filter_texts([_INCIDENT], None) == ([_INCIDENT], [])


def test_filter_texts():
  kept, dropped = filter_texts(['10-11低于10-05', _INCIDENT], _idx())
  assert kept == ['10-11低于10-05'], kept
  assert len(dropped) == 1 and dropped[0][0] == _INCIDENT, dropped


def test_deterministic():
  a = violations(_INCIDENT, _idx())
  b = violations(_INCIDENT, _idx())
  assert json.dumps(a, ensure_ascii=False) == json.dumps(b, ensure_ascii=False)


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

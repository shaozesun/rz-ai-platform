"""影响时长分析（详情口径）的自检（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_impact`

**不依赖外部数据/网络**。覆盖：

1. **单位解析**：平台给的是可读串（`'0天 0小时 19分钟'`，空格有无不稳定），
   按天/小时/分钟/秒累加成分钟；认不出必须是 `None` 而不是 0（0 与"没填"不能混）。
2. **降级**：有效样本 < 3 时 `available=False` 并给出「X/N 单有值」。
3. **分布与矩阵**：直方图计数之和 = 有效单数；级别矩阵只统计有级别的行。
4. **长尾单**：按影响时长降序、最多 5 条，字段按声明截断。
5. **成因按单首命中**：一张单只投 1 票。
6. **分位口径**与 pandas 默认一致（与第 4 章可并读）。
"""

import pandas as pd

from service.scheduled_task.report.common import _IMPACT_LADDER
from service.scheduled_task.report.impact import (
  LONG_TAIL_TOP, _IMPACT_LABELS, _impact_bin, _parse_minutes, _quantile, analyze,
)


def _row(no: str, dur, **fields) -> dict:
  f = {'影响时长': dur, '事件名称': f'事件{no}', '原因分析': '空调停机',
       '处理过程': '现场检查并重启', '处理结果': '已恢复'}
  f.update(fields)
  return {'ticketNo': no, 'fields': f}


def _details(rows: list[dict]) -> dict:
  return {'rows': rows}


def test_parse_minutes_variants():
  assert _parse_minutes('0天0小时3分钟') == 3.0
  assert _parse_minutes('0天 0小时 19分钟') == 19.0      # 空格有无不稳定
  assert _parse_minutes('1天2小时30分钟') == 1590.0
  assert _parse_minutes('2小时') == 120.0
  assert _parse_minutes('0天0小时30秒') == 0.5
  assert _parse_minutes('0天0小时0分钟') == 0.0           # 真实的 0，不是缺失
  assert _parse_minutes('') is None
  assert _parse_minutes(None) is None
  assert _parse_minutes('19') is None                     # 裸数字不猜单位
  assert _parse_minutes('未填写') is None


def test_quantile_matches_pandas():
  vals = [3, 19, 47, 200, 1500, 4000]
  s = pd.Series(vals)
  for q in (0.5, 0.9):
    assert _quantile(vals, q) == round(float(s.quantile(q)), 1), (q, _quantile(vals, q))


def test_degrade_below_min_valid():
  out = analyze(_details([_row('T-1', '0天0小时1分钟'), _row('T-2', '未填写'),
                          _row('T-3', '')]))
  assert out['available'] is False and out['valid'] == 1 and out['n'] == 3, out
  assert '1/3' in out['reason'], out


def test_hist_sum_and_level_matrix():
  det = _details([
    _row('T-1', '0天0小时10分钟', 级别='3'),
    _row('T-2', '0天0小时50分钟', 级别='3'),
    _row('T-3', '0天3小时0分钟', 级别='4'),
    _row('T-4', '2天0小时0分钟', 级别=''),        # 无级别：进整体分布、不进矩阵
  ])
  out = analyze(det)
  assert out['available'] and out['valid'] == 4
  assert sum(out['hist']['counts']) == 4, out['hist']
  bl = out['by_level']
  assert bl['levels'] == ['3级', '4级'], bl['levels']
  assert sum(sum(r) for r in bl['matrix']) == 3, bl['matrix']
  assert [r['单数'] for r in bl['stats']] == [2, 1], bl['stats']
  # 占比分母是「有级别的行数」（3 单，不含无级别的 T-4），合计 100 —— 与第 4 章验收口径同构
  assert [r['占比'] for r in bl['stats']] == [66.7, 33.3], bl['stats']
  assert sum(r['占比'] for r in bl['stats']) == 100


def test_long_tail_sorted_and_truncated():
  rows = [_row(f'T-{i}', f'0天0小时{i}分钟') for i in range(1, 9)]
  rows.append(_row('T-X', '3天0小时0分钟', 处理过程='过' * 500))
  out = analyze(_details(rows))
  tail = out['long_tail']
  assert len(tail) == LONG_TAIL_TOP, len(tail)
  assert tail[0]['编号'] == 'T-X' and tail[0]['影响时长'] == '3天0小时0分钟'
  assert len(tail[0]['过程摘要']) == 200
  mins = [int(t['影响时长'].replace('0天0小时', '').replace('分钟', ''))
          for t in tail[1:]]
  assert mins == sorted(mins, reverse=True), mins


def test_causes_count_tickets_not_hits():
  det = _details([
    _row('T-1', '0天0小时5分钟', 原因分析='厂家备件未到，供应商发货延迟'),
    _row('T-2', '0天0小时6分钟', 原因分析='机房漏水'),
    _row('T-3', '0天0小时7分钟', 原因分析='未说明'),
  ])
  causes = analyze(det)['causes']
  assert causes.get('厂家/备件/更换') == 1, causes
  assert causes.get('温度/湿度/漏水') == 1, causes
  assert causes['可归类'] == 2 and causes['未归入'] == 1, causes


def test_impact_bands_match_color_ladder():
  """影响时长 5 档（左闭右开）与 `common._IMPACT_LADDER` 的 5 色**一一对应**。

  档数多于色数时 `_stacked_barh` 会循环取色 → 最慢一档拿到最冷的深蓝（2026-10-29 用户
  直接指定了这 5 个色值，别再换）；档数少了则末端颜色根本用不上。
  """
  assert len(_IMPACT_LABELS) == len(_IMPACT_LADDER) == 5, (_IMPACT_LABELS, _IMPACT_LADDER)
  assert _IMPACT_LADDER == ('#3561A7', '#80B2D3', '#D9F0F6', '#EC7743', '#CB2920'), \
    _IMPACT_LADDER
  # 边界左闭右开：30 分钟落第二档、24 小时落最后一档（与验收口径的切法一致）
  assert _impact_bin(0) == '≤30分' and _impact_bin(29.9) == '≤30分'
  assert _impact_bin(30) == '30分-2时'
  assert _impact_bin(1439.9) == '8-24时' and _impact_bin(1440) == '>1天'
  assert _impact_bin(24 * 60 * 30) == '>1天'


def test_short_pct_uses_hour_boundary():
  det = _details([_row('T-1', '0天0小时59分钟'), _row('T-2', '0天1小时0分钟'),
                  _row('T-3', '0天0小时30分钟')])
  out = analyze(det)
  assert out['short_pct'] == 66.7, out['short_pct']


if __name__ == '__main__':
  tests = [(name, fn) for name, fn in sorted(globals().items())
           if name.startswith('test_') and callable(fn)]
  failed = 0
  for name, fn in tests:
    try:
      fn()
      print(f'  OK    {name}')
    except AssertionError as exc:
      failed += 1
      print(f'  FAIL  {name}: {exc}')
  print(f'\n{len(tests) - failed}/{len(tests)} 通过')
  raise SystemExit(1 if failed else 0)

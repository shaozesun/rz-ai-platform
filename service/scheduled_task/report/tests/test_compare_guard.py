"""跨期比较的字段与数字闸（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_compare_guard`

守住 2026-10-23 实测事故的修复（全国故障报告）：解读层把「本期占比 40%」和
「上一期**计数** 27」相减，写出「智能化占比 40%，较上期增加 13%」——真值是
40% vs 48.2%，即 **−8.2 个百分点**。两道防线：

1. **数据层**（`common._add_compare` / `narrate._compare_rows`）：每行补 `prev_pct` /
   `delta_count` / `delta_pp`，并改成带单位、带方向的中文键名下发；上期分母必须是
   **该块自己的上一期总量**，不是本期总量，也不能用「上一期计数」冒充占比。
2. **校验层**（`compare_guard`）：带「较上期/环比/同比」口吻的句子，每个数字都要能在
   本章数据里找到出处，否则整句丢弃。无上一期数据时不给上期键（给 0 会被读成 0%）。
"""

from service.scheduled_task.report.common import _add_compare, _delta_text
from service.scheduled_task.report.compare_guard import (
  collect_deltas, collect_numbers, filter_texts, violations,
)
from service.scheduled_task.report.narrate import _compare_rows

# 全国故障报告的真实数字：本期 50 单 / 上一期 56 单
_ROWS = [
  {'label': '供配电', 'count': 14, 'pct': 28, 'prev': 10},
  {'label': '制冷', 'count': 16, 'pct': 32, 'prev': 19},
  {'label': '智能化', 'count': 20, 'pct': 40, 'prev': 27},
]


def test_compare_fields_use_previous_total():
  """`prev_pct` 的分母是上一期总量（56），`delta_pp` 是百分点差，不是「占比−计数」。"""
  rows = [dict(r) for r in _ROWS]
  _add_compare(rows, 56)
  chem = next(r for r in rows if r['label'] == '制冷')
  assert chem['prev_pct'] == 33.9, chem
  assert chem['delta_count'] == -3, chem
  assert chem['delta_pp'] == -1.9, chem
  ai = next(r for r in rows if r['label'] == '智能化')
  # 旧事故的错法是 40 − 27 = 13；正确值是 40 − 48.2 = −8.2
  assert ai['prev_pct'] == 48.2, ai
  assert ai['delta_pp'] == -8.2, ai
  assert ai['delta_count'] == -7, ai


def test_compare_fields_absent_without_previous_period():
  """无上一期数据：三个字段必须是 None（写 0 会被解读成「上一期占比 0%」）。"""
  rows = [{'label': '制冷', 'count': 4, 'pct': 100, 'prev': None}]
  _add_compare(rows, 0)
  assert rows[0]['prev_pct'] is None, rows[0]
  assert rows[0]['delta_count'] is None, rows[0]
  assert rows[0]['delta_pp'] is None, rows[0]


def test_compare_view_keys_carry_units():
  """下发给模型的键名要带方向与量纲，且没有上期数据时不出现上期键。"""
  rows = [dict(r) for r in _ROWS]
  _add_compare(rows, 56)
  view = _compare_rows(rows)
  ai = next(r for r in view if r['名称'] == '智能化')
  assert ai['本期占比%'] == 40
  assert ai['上期占比%'] == 48.2
  assert ai['单量变化（本期−上期）'] == -7
  assert ai['占比变化（百分点，本期−上期）'] == -8.2
  # 没有任何「上期…」键：模型没有上期数字可拿去相减
  bare = _compare_rows([{'label': '制冷', 'count': 4, 'pct': 100, 'prev': None,
                         'prev_pct': None, 'delta_count': None, 'delta_pp': None}])
  assert bare == [{'名称': '制冷', '本期单量': 4, '本期占比%': 100}], bare


def test_collect_numbers_includes_abs():
  """数据里的 `delta_count = -7`，报告写「减少 7 起」——正则抓到 7，必须能对上。"""
  allowed = collect_numbers({'单量变化': -7, '占比变化': -8.2})
  assert '7' in allowed and '-7' in allowed, allowed
  assert '8.2' in allowed and '-8.2' in allowed, allowed


def test_guard_blocks_count_minus_share():
  """事故原句：允许数字里没有 13（40−27 的差），必须被拦。"""
  allowed = collect_numbers({'专业分布': _ROWS_text()})
  bad = violations('智能化占比 40%，较上期增加 13%，是本期占比最高的专业。', allowed)
  assert len(bad) == 1, bad
  assert bad[0][1] == ['13'], bad


def test_guard_passes_provided_delta():
  """按给好的变化字段复述（占比与百分点都在数据里）→ 放行。"""
  rows = [dict(r) for r in _ROWS]
  _add_compare(rows, 56)
  allowed = collect_numbers(_compare_rows(rows))
  assert violations('智能化占比 40%，较上期下降 8.2 个百分点。', allowed) == []


def test_guard_ignores_within_period_comparison():
  """同章内部对比（园区 vs 全国均值）没有跨期口吻 → 不拦，哪怕数字不在数据里。"""
  allowed = collect_numbers({'园区分布': [{'名称': '廊坊', '本期占比%': 17.5}]})
  assert violations('廊坊占比 17.5%，高于全国均值 5.7 个百分点。', allowed) == []


def test_guard_allows_reduction_phrasing():
  """`delta_count = -7` 时，「较上期减少 7 起」是正确表述，不能被负号误杀。"""
  allowed = collect_numbers({'单量变化（本期−上期）': -7})
  assert violations('制冷较上期减少 7 起。', allowed) == []


def test_delta_text_zero_has_no_plus():
  """环比为 0 时不能渲染成「+0」（会被读成"增加了 0"）；非零仍带符号。"""
  assert _delta_text(0) == '0'
  assert _delta_text(-0) == '0'
  assert _delta_text(3) == '+3'
  assert _delta_text(-3) == '-3'


def test_guard_blocks_share_reused_as_delta():
  """「占比 40%，较上期增加 40%」：40 在本期占比里出现过，但**不是变化量** → 必须拦。

  这是仅有"数字出现过"时拦不住的漏网（2026-10-23 集成检查实测）。
  """
  rows = [dict(r) for r in _ROWS]
  _add_compare(rows, 56)
  from service.scheduled_task.report.narrate import _compare_rows
  view = _compare_rows(rows)
  allowed, deltas = collect_numbers(view), collect_deltas(view)
  bad = violations('智能化占比 40%，较上期增加 40%。', allowed, deltas)
  assert len(bad) == 1 and bad[0][1] == ['40'], bad


def test_guard_allows_direction_with_real_delta():
  """按真变化量复述（-7 单 / -8.2 个百分点）→ 放行。"""
  rows = [dict(r) for r in _ROWS]
  _add_compare(rows, 56)
  from service.scheduled_task.report.narrate import _compare_rows
  view = _compare_rows(rows)
  allowed, deltas = collect_numbers(view), collect_deltas(view)
  assert violations('智能化占比较上期下降 8.2 个百分点，单量减少 7 起。', allowed, deltas) == []


def test_guard_allows_target_value_after_direction_word():
  """「由 83.2 提升至 100 分」是**目标值**不是变化量。

  2026-10-27 实测：执行摘要里「规模控制评分由 83.2 提升至 100 分，整体改善」被误判成
  「增加 100」而整句丢弃 —— 恰好丢掉了要去偏的那类正向结论。
  """
  payload = {'总量': 167, '上一期总量': 195,
             '环比': {'prev_total': 195, 'delta': -28, 'pct': -14.4},
             '综合评分': [{'维度': '规模控制', '本期': 100, '上一期': 83.2}]}
  allowed, deltas = collect_numbers(payload), collect_deltas(payload)
  text = ('本期全国工单总量 167 起，环比减少 28 起，规模控制评分由 83.2 提升至 100 分，'
          '整体运行负荷显著改善。')
  assert violations(text, allowed, deltas) == [], violations(text, allowed, deltas)
  # 真变化量写错仍要拦（不能因为放开「至」就把这道门整个拆掉）
  assert violations('单量较上期减少 28 起。', allowed, {'7'}) != []


def test_collect_deltas_excludes_previous_total():
  """`环比` 块是 `{prev_total, delta, pct}` —— 上一期总量不是变化量（否则「增加 56」能过闸）。"""
  payload = {'总量': 50, '上一期总量': 56,
             '环比': {'prev_total': 56, 'delta': -6, 'pct': -10.7}}
  deltas = collect_deltas(payload)
  assert '6' in deltas and '-6' in deltas, deltas
  assert '56' not in deltas, deltas
  assert violations('本期较上期增加 56 起。', collect_numbers(payload), deltas) != []


def test_collect_deltas_reads_facts_text():
  """平台自己算好的变化句（趋势事实「（较上一期同日减少 2 起）」）要算合法变化量。"""
  deltas = collect_deltas({'数据事实': ['10-11：本期 5 起，上一期同日 7 起（减少 2 起）']})
  assert '2' in deltas, deltas


def test_filter_texts_drops_only_offending():
  """只丢越界的那一条，其余保留。"""
  allowed = collect_numbers({'占比变化（百分点，本期−上期）': -8.2,
                             '本期占比%': 40, '上期占比%': 48.2})
  kept, bad = filter_texts([
    '智能化占比 40%，较上期增加 13%。',
    '智能化占比较上期下降 8.2 个百分点。',
    '本月总量 50 单。',
  ], allowed)
  assert kept == ['智能化占比较上期下降 8.2 个百分点。', '本月总量 50 单。'], kept
  assert len(bad) == 1 and bad[0][1] == ['13'], bad


def _ROWS_text() -> list[dict]:
  """事故当期的原始 payload（只有计数、没有上期占比）——复原"数据里确实没有 13"。"""
  return [{'名称': r['label'], '本期单量': r['count'], '本期占比%': r['pct'],
           '上期单量': r['prev']} for r in _ROWS]


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

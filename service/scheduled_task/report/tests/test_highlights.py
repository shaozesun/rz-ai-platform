"""正面结论自检（纯 assert，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_highlights`

守住七件事：

1. **11 类判据各自命中/不命中**：单量、风险总体、业务影响、闭环、按时、高等级占比、
   重复、规范、紧急变更、录入及时、详情填报（2026-10-29 用户要求「正面评价多说一些」，
   判据从 3 类扩到 11 类，上限 5 句）。
2. **无信号就不出句**：键缺失、无上一期基数、分母为 0、未达阈值 → 该句不出现；
   一句都不命中返回空串（不硬夸）。
3. **不重复**：风险总句命中时不再逐条说「率值类」指标；「规模控制」不重复列；
   有驳回率时「处置质量」不参与判定；闭环句命中时规范句跳过「闭环质量」。
4. **阈值与 `risks.py` 同源**：指标正好等于中风险线时，风险表必须出「中」行、
   正面句必须**不**出现 —— 两处阈值漂移时本用例先失败（不靠复制数字）。
5. **`rows` 行序固定、最多 3 行**（规模 → 影响 → 评分）；「上一期」缺失时不印「（上一期 — 分）」。
6. **`sentences` 是确定性结论段**：句序固定、上限 5 句、每句至少含一个数字（数据锚点），
   `summary` 就是逐句加句号的拼接。
7. **效率句的口径优先级**：有影响时长就用它（业务影响），否则退回「响应时效 / 处理时效」评分。
"""

from service.scheduled_task.report import highlights as H
from service.scheduled_task.report import risks as R
from service.scheduled_task.report.spec import (
  BREAKDOWN_SPEC, CHANGE_SPEC, EVENT_SPEC, QUESTION_SPEC, THRESHOLD_SPEC,
)


def _names(rows: list[dict]) -> list[str]:
  return [r['亮点项'] for r in rows]


def _card(*items) -> dict:
  return {'scorecard': {'rows': [
    {'维度': dim, '含义': '', '本期': cur, '上一期': prev} for dim, cur, prev in items]}}


def _sent(stats: dict, details, spec) -> list[str]:
  return H.evaluate(stats, details, spec)['sentences']


def _level(*pairs) -> dict:
  return {'level': [{'label': f'{lv}级', 'count': n} for lv, n in pairs]}


def test_all_three_hit_in_fixed_order():
  stats = {'total': 195, 'prev_total': 209,
           'mom': {'prev_total': 209, 'delta': -14, 'pct': -6.7},
           'scorecard': {'rows': [
             {'维度': '规模控制', '本期': 100, '上一期': 100},
             {'维度': '响应时效', '本期': 100, '上一期': 92},
             {'维度': '处置质量', '本期': 96, '上一期': 96},
             {'维度': '重复管控', '本期': 72, '上一期': 70}]}}
  det = {'impact': {'available': True, 'short_pct': 86, 'readable': {'p50': '12 分钟'}}}
  rows = H.evaluate(stats, det, EVENT_SPEC)['rows']
  assert _names(rows) == ['单量规模', '业务影响', '综合评分'], _names(rows)
  assert rows[0]['本期表现'] == '195 起，较上一期减少 14 起（-6.7%）', rows[0]
  assert rows[1]['本期表现'] == '影响不足 1 小时的单占 86%（P50 12 分钟）', rows[1]
  # 降序取前 2，且「规模控制」不重复列（它已由第一行表达）
  assert rows[2]['本期表现'] == '响应时效 100 分（上一期 92 分）、处置质量 96 分（上一期 96 分）', rows[2]
  assert '规模控制' not in rows[2]['本期表现']


def test_volume_flat_is_positive():
  stats = {'total': 120, 'prev_total': 120,
           'mom': {'prev_total': 120, 'delta': 0, 'pct': 0}}
  rows = H.evaluate(stats, None, EVENT_SPEC)['rows']
  assert _names(rows) == ['单量规模'], rows
  assert rows[0]['本期表现'] == '120 起，与上一期持平（120 起）', rows[0]


def test_volume_growth_not_a_highlight():
  stats = {'total': 140, 'prev_total': 120,
           'mom': {'prev_total': 120, 'delta': 20, 'pct': 16.7}}
  assert H.evaluate(stats, None, EVENT_SPEC)['rows'] == []
  assert _sent(stats, None, EVENT_SPEC) == []


def test_volume_needs_prev_base():
  # 无上一期基数：没有对比就评判等于编成绩（分母 0 不出行）
  stats = {'total': 5, 'prev_total': 0, 'mom': {'prev_total': 0, 'delta': 5, 'pct': None}}
  assert H.evaluate(stats, None, EVENT_SPEC)['rows'] == []


def test_impact_threshold_boundary():
  det_ok = {'impact': {'available': True, 'short_pct': 70, 'readable': {'p50': '30 分钟'}}}
  det_low = {'impact': {'available': True, 'short_pct': 69.9, 'readable': {'p50': '2 小时'}}}
  assert _names(H.evaluate({}, det_ok, EVENT_SPEC)['rows']) == ['业务影响']
  assert H.evaluate({}, det_low, EVENT_SPEC)['rows'] == []
  # 影响时长不可用（样本不足/未填）→ 不做分布，也不出亮点
  assert H.evaluate({}, {'impact': {'available': False}}, EVENT_SPEC)['rows'] == []


def test_scorecard_threshold_and_missing_prev():
  rows = H.evaluate(_card(('响应时效', 80, None), ('重复管控', 79, 90)), None,
                    EVENT_SPEC)['rows']
  assert _names(rows) == ['综合评分'], rows
  assert rows[0]['本期表现'] == '响应时效 80 分', rows[0]        # 上一期缺失不印「— 分」


def test_no_signal_at_all():
  assert H.evaluate({}, None, EVENT_SPEC)['rows'] == []
  assert H.evaluate({'total': 0, 'prev_total': 0}, {}, EVENT_SPEC)['rows'] == []
  assert H.evaluate({}, {'quality': {}, 'impact': {}}, EVENT_SPEC) == {
    'rows': [], 'summary': '', 'sentences': []}


def test_max_three_rows():
  stats = {'total': 10, 'prev_total': 20,
           'mom': {'prev_total': 20, 'delta': -10, 'pct': -50},
           'scorecard': {'rows': [{'维度': f'维度{i}', '本期': 90 + i, '上一期': 80}
                                  for i in range(4)]}}
  det = {'impact': {'available': True, 'short_pct': 95, 'readable': {'p50': '5 分钟'}}}
  rows = H.evaluate(stats, det, EVENT_SPEC)['rows']
  assert len(rows) == 3, rows
  assert rows[2]['本期表现'].count('、') == 1                  # 评分项最多 2 个维度


def test_volume_sentence_includes_daily_avg():
  stats = {'total': 195, 'daily_avg': 27.9,
           'mom': {'prev_total': 209, 'delta': -14, 'pct': -6.7}}
  assert _sent(stats, None, EVENT_SPEC) == [
    '单量 195 起，日均 27.9 起，较上一期减少 14 起（-6.7%），规模可控'], _sent(
      stats, None, EVENT_SPEC)


def test_summary_joins_three_sentences():
  stats = {'total': 195, 'mom': {'prev_total': 209, 'delta': -14, 'pct': -6.7},
           'quality': {'reject_rate': 9.7}}
  stats.update(_card(('处置质量', 96, 90)))
  det = {'impact': {'available': True, 'short_pct': 86, 'readable': {'p50': '12 分钟'}}}
  result = H.evaluate(stats, det, EVENT_SPEC)
  assert result['sentences'] == [
    '单量 195 起，较上一期减少 14 起（-6.7%），规模可控',
    '影响不足 1 小时的单占 86%（P50 12 分钟），诊断、通报与现场处置及时',
    '驳回率 9.7%，处置规范'], result['sentences']
  assert result['summary'] == ''.join(f'{s}。' for s in result['sentences'])
  # 「处置质量」＝100−驳回率，与驳回率是同一件事的两种写法：有驳回率就不再列，避免说两遍
  assert '处置质量' not in result['summary'], result['summary']


def test_summary_quality_dim_used_when_no_reject_rate():
  stats = _card(('处置质量', 96, 90))
  assert _sent(stats, None, EVENT_SPEC) == ['处置质量 96 分，处置规范']


def test_summary_skips_missing_sentences():
  """没有上期基数、没有影响时长、没有质量数据 → 一句都不出（不硬夸）。"""
  assert H.evaluate({'total': 5}, None, EVENT_SPEC)['summary'] == ''
  # 只有单量有据：剩下两句不出现
  summary = H.evaluate({'total': 120, 'mom': {'prev_total': 120, 'delta': 0}},
                       None, EVENT_SPEC)['summary']
  assert summary == '单量 120 起，与上一期持平（120 起），规模可控。', summary


def test_summary_efficiency_falls_back_to_timing_score():
  """影响时长不可用（非事件单 / 样本不足）→ 用「处理时效」评分表达同一件事。"""
  stats = {'total': 50, 'mom': {'prev_total': 50, 'delta': 0},
           'scorecard': {'rows': [{'维度': '处理时效', '本期': 84}]}}
  summary = H.evaluate(stats, {'impact': {'available': False}}, EVENT_SPEC)['summary']
  assert '处理时效 84 分，处置及时' in summary, summary


def test_summary_reject_mirror_not_a_basis_when_reject_present():
  """有驳回率时「处置质量」（= 100 − 驳回率）不参与判定：10.8% 不该写成「处置规范」。"""
  stats = _card(('处置质量', 89.2, 90))
  stats['quality'] = {'reject_rate': 10.8}
  assert '处置规范' not in H.evaluate(stats, None, EVENT_SPEC)['summary']
  # 驳回率达标时照常出句，且不再重复列「处置质量」
  stats['quality'] = {'reject_rate': 4}
  assert _sent(stats, None, EVENT_SPEC) == ['驳回率 4%，处置规范']


def test_risk_sentence_replaces_rate_sentences():
  """风险表整体无高/中项时，只说一句总括，不再逐条说闭环/按时/规范/紧急。"""
  stats = {'total': 100, 'mom': {'prev_total': 110, 'delta': -10, 'pct': -9.1},
           'quality': {'open_n': 0, 'reject_rate': 3},
           'schedule': {'on_time_n': 48, 'on_time_base': 48, 'on_time_rate': 100},
           'change_type': {'emergency_n': 0, 'emergency_rate': 0}}
  det = {'risk_analysis': {'rows': [{'风险项': '未按时完成率', '等级': '—'},
                                    {'风险项': '紧急变更占比', '等级': '—'}],
                           'overall': '—'}}
  sentences = _sent(stats, det, CHANGE_SPEC)
  assert sentences == [
    '单量 100 单，较上一期减少 10 单（-9.1%），规模可控',
    '质量与风险 2 项指标均未触及中、高风险阈值，运行状态平稳'], sentences
  joined = ''.join(sentences)
  for word in ('未关闭', '按时', '驳回', '紧急'):
    assert word not in joined, joined


def test_risk_sentence_needs_rows():
  """`overall == '—'` 但一行可算指标都没有 → 不出（那不是「平稳」，是没算）。"""
  det = {'risk_analysis': {'rows': [], 'overall': '—'}}
  assert _sent({}, det, EVENT_SPEC) == []


def test_closed_sentence_change_and_question():
  assert _sent({'total': 48, 'quality': {'open_n': 2}}, None, CHANGE_SPEC) == [
    '未关闭 2 单（4.2%），闭环完成良好']
  # 未关闭正好 10% → 风险表记「中」，正面句不出（见 test_thresholds_match_risks_boundary）
  assert _sent({'total': 50, 'quality': {'open_n': 5}}, None, CHANGE_SPEC) == []
  assert _sent({'quality': {'close_n': 19, 'close_base': 20}}, None, QUESTION_SPEC) == [
    '未关闭 1 单（5%），闭环完成良好']
  assert _sent({'quality': {'close_n': 18, 'close_base': 20}}, None, QUESTION_SPEC) == []
  assert _sent({'total': 40, 'threshold_flow': {'open': {'n': 1}}}, None,
               THRESHOLD_SPEC) == ['未关闭 1 单（2.5%），闭环完成良好']


def test_closed_sentence_breakdown_uses_closed_rate():
  """故障用关闭率（risks 的中风险线是「<90%」），不是未关闭计数。"""
  ok = {'total': 40, 'quality': {'closed_rate': 95, 'closed_n': 38}}
  assert _sent(ok, None, BREAKDOWN_SPEC) == ['关闭率 95%（38/40 单），闭环完成良好']
  # 正好 90% 时 risks 不列（<90 才算中），正面句照出 —— 两边一致
  edge = {'total': 40, 'quality': {'closed_rate': 90, 'closed_n': 36}}
  assert _sent(edge, None, BREAKDOWN_SPEC) == ['关闭率 90%（36/40 单），闭环完成良好']
  low = {'total': 40, 'quality': {'closed_rate': 89.9, 'closed_n': 36}}
  assert _sent(low, None, BREAKDOWN_SPEC) == []


def test_on_time_sentence_change_and_threshold():
  ok = {'total': 48, 'schedule': {'on_time_n': 47, 'on_time_base': 48, 'on_time_rate': 97.9}}
  assert _sent(ok, None, CHANGE_SPEC) == ['变更按时完成 47/48 单（97.9%），按期完成']
  # 未按时正好 5%（= risks 中风险线）→ 不出
  assert _sent({'total': 48, 'schedule': {'on_time_rate': 95}}, None, CHANGE_SPEC) == []
  thr = {'total': 30, 'execution': {'on_time_n': 28, 'on_time_base': 30, 'on_time_rate': 93.3}}
  assert _sent(thr, None, THRESHOLD_SPEC) == [
    '阈值调整按时执行 28/30 单（93.3%），按期执行']
  # 未按时正好 10% → 不出
  assert _sent({'total': 30, 'execution': {'on_time_rate': 90}}, None, THRESHOLD_SPEC) == []


def test_high_level_sentence():
  ok = _level((1, 4), (2, 29), (3, 110), (4, 52))          # 33 / 195 = 16.9%
  assert _sent(ok, None, EVENT_SPEC) == ['1–2 级事件 33 起（16.9%），仍以 3–4 级为主']
  over = _level((1, 20), (2, 30), (3, 50))                 # 50%
  assert _sent(over, None, EVENT_SPEC) == []
  zero = _level((3, 20), (4, 10))
  assert _sent(zero, None, EVENT_SPEC) == [
    '本期未出现 1–2 级高等级事件（30 起均在 3–4 级）']
  bd = _level((1, 2), (2, 8), (3, 40), (4, 50))            # 故障说「故障」不说「事件」
  assert _sent(bd, None, BREAKDOWN_SPEC) == ['1–2 级故障 10 起（10%），仍以 3–4 级为主']


def test_repeat_sentence():
  assert _sent({'quality': {'repeat_n': 12, 'repeat_rate': 6.2}}, None, EVENT_SPEC) == [
    '重复类事件涉及 12 单（6.2%），同一项反复出现的不多']
  # 事件正好 15% → risks 记「中」→ 不出
  assert _sent({'quality': {'repeat_n': 30, 'repeat_rate': 15}}, None, EVENT_SPEC) == []
  assert _sent({'quality': {'repeat_n': 4, 'repeat_rate': 9}}, None, BREAKDOWN_SPEC) == [
    '重复故障涉及 4 单（9%），同一项反复出现的不多']
  assert _sent({'quality': {'repeat_n': 5, 'repeat_rate': 10}}, None, BREAKDOWN_SPEC) == []
  assert _sent({'quality': {'repeat_n': 3, 'repeat_rate': 7.5}}, None, QUESTION_SPEC) == [
    '重复问题涉及 3 单（7.5%），同一项反复出现的不多']


def test_emergency_sentence():
  ok = {'change_type': {'emergency_n': 6, 'emergency_rate': 3.2}}
  assert _sent(ok, None, CHANGE_SPEC) == ['紧急变更 6 单（3.2%），变更计划性强']
  assert _sent({'change_type': {'emergency_rate': 10}}, None, CHANGE_SPEC) == []


def test_entry_lag_sentence():
  assert _sent({'entry_lag': {'p90': 2.1}}, None, QUESTION_SPEC) == [
    '录入滞后 P90 2.1 小时，建单及时']
  assert _sent({'entry_lag': {'p90': 8}}, None, QUESTION_SPEC) == []


def test_detail_fill_sentence():
  """填报完整度独立成句：无空填且「依据具体」≥60%；有任一空填就不夸。"""
  det = {'quality': {'n': 178, 'no_summary_n': 0, 'specific_n': 128}}
  assert _sent({}, det, EVENT_SPEC) == [
    '处置总结 178 单全部填写，其中带具体依据的单占 71.9%']
  low = {'quality': {'n': 178, 'no_summary_n': 0, 'specific_n': 64}}
  assert _sent({}, low, EVENT_SPEC) == []                  # 36% → 不硬夸
  missing = {'quality': {'n': 178, 'no_summary_n': 1, 'specific_n': 128}}
  assert _sent({}, missing, EVENT_SPEC) == []


def test_max_five_sentences():
  """11 类判据全中时截到 5 句，且按固定顺序（规模 → 效率 → 闭环 → 按时 → 规范）。"""
  stats = {'total': 100, 'daily_avg': 3.3,
           'mom': {'prev_total': 110, 'delta': -10, 'pct': -9.1},
           'quality': {'open_n': 2, 'reject_rate': 4},
           'schedule': {'on_time_n': 97, 'on_time_base': 100, 'on_time_rate': 97},
           'change_type': {'emergency_n': 2, 'emergency_rate': 2},
           'scorecard': {'rows': [{'维度': '处理时效', '本期': 90}]}}
  sentences = _sent(stats, None, CHANGE_SPEC)
  assert len(sentences) == 5, sentences
  assert sentences[0].startswith('单量 100 单')
  assert '处理时效 90 分' in sentences[1]
  assert '未关闭' in sentences[2]
  assert '按时完成' in sentences[3]
  assert '驳回率 4%' in sentences[4]
  assert '紧急变更' not in ''.join(sentences), sentences     # 第 6 句被截掉


def test_every_sentence_has_a_data_anchor():
  stats = {'total': 100, 'daily_avg': 3.3,
           'mom': {'prev_total': 110, 'delta': -10, 'pct': -9.1},
           'quality': {'open_n': 2, 'reject_rate': 4, 'repeat_n': 3, 'repeat_rate': 3},
           'schedule': {'on_time_n': 97, 'on_time_base': 100, 'on_time_rate': 97},
           'change_type': {'emergency_n': 2, 'emergency_rate': 2},
           'scorecard': {'rows': [{'维度': '处理时效', '本期': 90}]}}
  det = {'risk_analysis': {'rows': [{'风险项': 'x', '等级': '中'}], 'overall': '中'},
         'quality': {'n': 20, 'no_summary_n': 0, 'specific_n': 15}}
  sentences = _sent(stats, det, CHANGE_SPEC)
  assert sentences, '至少要出一句'
  for s in sentences:
    assert any(ch.isdigit() for ch in s), s


def test_thresholds_match_risks_boundary():
  """指标正好压在中风险线上时：`risks` 必须记「中/高」，正面句必须不出现。

  这是两处阈值「同源」的唯一实证 —— 谁改了阈值却又没同步，本用例先炸。
  """
  cases = [
    (CHANGE_SPEC, {'total': 50, 'quality': {'open_n': 5}}, '未关闭'),          # 10%
    (CHANGE_SPEC, {'total': 50, 'schedule': {'on_time_rate': 95}}, '按期完成'),  # 未按时 5%
    (CHANGE_SPEC, {'total': 50, 'change_type': {'emergency_rate': 10}}, '紧急变更'),
    (EVENT_SPEC, {'total': 50, 'quality': {'repeat_rate': 15}}, '重复类事件'),
    (QUESTION_SPEC, {'total': 50, 'entry_lag': {'p90': 8}}, '录入滞后'),
    (THRESHOLD_SPEC, {'total': 50, 'execution': {'on_time_rate': 90}}, '按期执行'),  # 未按时 10%
    (BREAKDOWN_SPEC, {'total': 50, 'quality': {'closed_rate': 89.9}}, '关闭率'),
  ]
  for spec, stats, word in cases:
    overall = R.evaluate(stats, spec)['overall']
    assert overall in ('高', '中'), (spec.ticket, stats, overall)
    assert word not in ''.join(_sent(stats, None, spec)), (spec.ticket, stats)


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

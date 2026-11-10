"""报告语气去偏的自检（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_speculation_guard`

守住三件事：

1. **猜测闸**（`speculation_guard`）：把「单量下降」写成「漏报/采集失效」的句子，
   在报告数据没有缺失证据时必须被丢弃；有证据、带否定、或只有一侧词的一律放行。
2. **结构**：方向判据真的下发给模型了（`单量方向` / `综合评分`），且提示词不再要求
   「每条必须带建议动作」「每档固定 3 条」——2026-10-27 实测 26/51 份报告把下降
   写成数据问题的直接诱因。
3. **文字归一**：繁体→简体在 `_loads` 解析前完成（`zhconv`），生僻书面语
   （亟待/亟需/亟须）确定性换成「急需」——2026-10-29 用户指出报告里出现「亟待提升」。
"""

from service.scheduled_task.report import narrate, speculation_guard as sg
from service.scheduled_task.report.common import _volume_verdict
from service.scheduled_task.report.spec import EVENT_SPEC

_DECREASING = {
  'total': 4, 'prev_total': 8, 'mom': -4,
  'extras': {},
  'window': {'period': 'weekly'},
  'scorecard': {'rows': [{'维度': '规模控制', '含义': '持平或下降满分',
                        '本期': 100, '上一期': 90}], 'cur': [100], 'prev': [90]},
}


def test_volume_verdict_directions():
  down = _volume_verdict(4, 8, '起')
  assert down['方向'] == '下降' and '减少 4 起' in down['说明'], down
  up = _volume_verdict(10, 8, '单')
  assert up['方向'] == '上升' and '增加 2 单' in up['说明'], up
  flat = _volume_verdict(8, 8)
  assert flat['方向'] == '持平' and '持平' in flat['说明'], flat
  # 口径必须与综合评分的「规模控制」一致，否则模型拿到的判据自相矛盾
  assert '下降为正向' in down['口径']
  # 增长≠运行恶化（用户 2026-10-30）：口径要求如实写「规模扩大」
  assert '规模扩大' in up['口径'] and '恶化' in up['口径']


def test_volume_verdict_skips_without_base():
  assert _volume_verdict(4, None) is None
  assert _volume_verdict(4, 0) is None
  assert _volume_verdict('x', 8) is None


def test_guard_drops_decrease_plus_suspicion():
  text = ('10-21 单量由上一期 8 起降至本期 4 起（减少 4 起），'
          '需确认该日是否存在因系统屏蔽或流程变更导致的漏报风险')
  assert sg.violations(text), '下降 + 漏报 且无缺失证据，应被判定为猜测'


def test_guard_catches_negative_delta_wording():
  # 实测原句：没有"减少"二字，靠负号数值表达下降
  text = '惠州润信科技发展有限公司单量变化为 -16 单，则存在监控数据采集失效风险'
  assert sg.violations(text)


def test_guard_keeps_negated_sentences():
  text = '本期单量下降 4 起，未发现漏报情况'
  assert not sg.violations(text)


def test_guard_keeps_sentences_without_suspicion():
  text = '本期单量下降 4 起，处置效率同步提升'
  assert not sg.violations(text)


def test_guard_keeps_suspicion_without_decrease():
  # 只有猜测、没有减量：可能是对真实缺失的陈述，保守放行
  assert not sg.violations('个别接口存在采集失效，已联系厂商处理')


def test_guard_keeps_when_evidence_present():
  text = '本期单量下降 4 起，可能受缺失记录影响'
  assert not sg.violations(text, allow_data_suspicion=True)


def test_evidence_detection():
  assert sg.has_data_quality_evidence({'scope_filter': {'missing_start': 2}})
  assert not sg.has_data_quality_evidence({'scope_filter': {'missing_start': 0}})
  assert sg.has_data_quality_evidence(
    {'schedule': {'数据可用性': {'实施时间缺失': 1}}})
  assert not sg.has_data_quality_evidence({'total': 5, 'prev_total': 8})
  assert not sg.has_data_quality_evidence({'quality': {'reject_n': 3}})


def test_filter_texts_batch():
  items = ['本期单量下降 4 起，疑似漏报', '本期超时率降至 8.3%']
  kept, dropped = sg.filter_texts(items, allow_data_suspicion=False)
  assert kept == [items[1]] and len(dropped) == 1 and dropped[0][1], (kept, dropped)


def test_validate_section_applies_guard():
  data = {'要点': ['本期单量下降 4 起，需确认是否存在漏报风险',
                 '本期超时率降至 8.3%，为近几期最好']}
  got, dropped = narrate._validate_section(
    data, [], set(), require_evidence=False, allow_data_suspicion=False)
  assert dropped == 1 and got['要点'] == [data['要点'][1]], (got, dropped)
  # 报告确有缺失证据时不拦
  got2, dropped2 = narrate._validate_section(
    data, [], set(), require_evidence=False, allow_data_suspicion=True)
  assert dropped2 == 0 and len(got2['要点']) == 2


def test_trend_payload_carries_volume_verdict():
  payload = narrate._section_payload(_DECREASING, {}, EVENT_SPEC, 'trend')
  assert payload['单量方向']['方向'] == '下降', payload.get('单量方向')


def test_trim_stats_carries_scorecard():
  stats = narrate._trim_stats(_DECREASING)
  rows = stats.get('综合评分（本期 vs 上一期，越高越好）') or []
  assert rows and rows[0]['维度'] == '规模控制', rows


def test_prompts_no_longer_force_actions_on_every_bullet():
  assert '每档 3 条' not in narrate._ACTION_SYSTEM
  assert '不必硬凑动作' in narrate._SECTION_SYSTEM
  assert '不得为了凑出风险而怀疑数据本身' in narrate._SECTION_SYSTEM
  assert '改善' in narrate._SUMMARY_SYSTEM


def test_briefs_do_not_preset_anomaly():
  assert '异常升高或降低的园区' not in narrate._BRIEFS['park']
  assert '体量不同' in narrate._BRIEFS['park']
  assert '针对性建议' not in narrate._BRIEFS['park']
  assert '指出哪项异常' not in narrate._BRIEFS['quality']
  assert '单量下降是正向' in narrate._BRIEFS['trend']


def test_briefs_do_not_ask_for_recommendations():
  """整份报告是**运行评价**，不是整改方案：brief 里不应再出现提意见类措辞。"""
  banned = ('建议', '治理', '排查', '改造')
  hits = {key: [w for w in banned if w in brief]
          for key, brief in narrate._BRIEFS.items()}
  hits = {k: v for k, v in hits.items() if v}
  assert not hits, f'brief 仍在要求提意见：{hits}'


def test_prompts_repositioned_as_evaluation():
  assert '周期性运行评价' in narrate._SECTION_SYSTEM
  assert '周期性运行评价' in narrate._SUMMARY_SYSTEM
  assert '周期性运行评价' in narrate._ACTION_SYSTEM
  assert '不强制附带动作' in narrate._SECTION_SYSTEM
  assert '0~3 条' in narrate._ACTION_SYSTEM


def test_guard_drops_low_volume_as_monitoring_granularity():
  """2026-10-27 用户原句：低单量被硬解释成数据问题。"""
  text = ('浙江泽悦各专业单量均低于 5 单，总量显著低于其他四个园区，'
          '判定为运行状态平稳或监测粒度较粗，建议核实是否漏报')
  assert sg.violations(text), '低单量 + 监测粒度 + 漏报 且无缺失证据，应被丢弃'
  got, dropped = narrate._validate_section(
    {'要点': [text, '各园区占比结构接近，本期运行平稳']},
    [], set(), require_evidence=False, allow_data_suspicion=False)
  assert dropped == 1 and got['要点'] == ['各园区占比结构接近，本期运行平稳']


def test_guard_keeps_low_volume_without_suspicion():
  # 只有减量一侧词，不得误杀
  assert not sg.violations('浙江泽悦本期 4 起，低于其他园区，运行平稳')


def test_park_payload_carries_comparison_caveat():
  stats = {'total': 10, 'prev_total': 12, 'park': [], 'extras': {}}
  payload = narrate._section_payload(stats, {}, EVENT_SPEC, 'park')
  caveat = payload.get('横向对比口径') or {}
  assert '不可横向比高低' in caveat.get('说明', ''), payload.keys()
  assert '漏报' in caveat.get('禁止', '')


def test_normalize_terms_severity():
  """2026-10-27 实测：模型把「高等级」译成「高 Severity」，正文出现中英混排。"""
  got = narrate._normalize_terms('重庆区域判定为高 Severity 风险集中')
  assert got == '重庆区域判定为高等级风险集中', got
  assert narrate._normalize_terms('Severity Level 为 2 级') == '严重程度为 2 级'


def test_normalize_terms_owner():
  got = narrate._normalize_terms('流程 Owner/运维管理部负责闭环')
  assert got == '流程负责人/运维管理部负责闭环', got


def test_normalize_urgent_wording():
  """2026-10-29 用户指出报告里出现「亟待提升」：生僻书面语确定性换成常用词。"""
  got = narrate._normalize_terms('告警质量亟待提升，亟需补齐数据盲区')
  assert got == '告警质量急需提升，急需补齐数据盲区', got
  assert narrate._normalize_terms('亟须建立联动机制') == '急需建立联动机制'


def test_loads_simplifies_traditional():
  """繁简归一做在 `_loads` 解析**之前**：键与值都要变简体，且早于三道守卫。"""
  if narrate._zh_convert is None:
    print('    SKIP  zhconv 未安装，跳过繁简归一断言')
    return
  data = narrate._loads('{"要點": ["告警質量高發，二級复核"]}')
  assert list(data) == ['要点'], data
  assert data['要点'] == ['告警质量高发，二级复核'], data


def test_validate_section_normalizes_latin_terms():
  """归一必须发生在 `_validate_section` 出口：摘要与行动计划吃的是这里的原文。"""
  data = {'要点': ['判定为高 Severity 风险集中，需由流程 Owner 跟进']}
  got, dropped = narrate._validate_section(data, [], set(), require_evidence=False)
  assert dropped == 0 and len(got['要点']) == 1, (got, dropped)
  assert not narrate._LATIN_RE.search(got['要点'][0]), got['要点'][0]


def test_prompts_do_not_mandate_one_connector():
  """2026-10-27 实测：提示词写「现象 → **判定**」「判定为负向/正向」，模型逐条照抄，
  75 条要点里 29 条以「判定为」收尾（改前基线 0 条）。方向判断要留、固定句式要去掉。"""
  assert '判定方向' not in narrate._SECTION_SYSTEM
  assert '判定为**负向**' not in narrate._SECTION_SYSTEM
  assert '同一个引导词' in narrate._SECTION_SYSTEM
  assert '如实写明方向' in narrate._SUMMARY_SYSTEM
  assert '先如实写明本期' in narrate._BRIEFS['trend']


def test_template_repeats_count():
  """只记日志的模板化审计：同一引导词 ≥3 次才报警。"""
  assert narrate._template_repeats('判定为A，判定为B，判定为C')['判定为'] == 3
  assert narrate._template_repeats('显示A，表明B，属于C') == {'属于': 1, '表明': 1, '显示': 1}
  assert all(n < 3 for n in narrate._template_repeats('显示A，表明B').values())


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

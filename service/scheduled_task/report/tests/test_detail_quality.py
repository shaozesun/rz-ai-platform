"""详情质检与成因归类口径的自检（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_detail_quality`

**不依赖任何外部数据/网络**。覆盖：

1. **疑似复制**：处置总结与调整原因高度重合（相似度 ≥ 0.7 或为连续子串）要能识别；
   但只写「已完成」这类套话、与原因无关的，归「总结模板化」而不是「复制」。
2. **原因笼统**：不足 10 字或整句为「无/暂无/按需/按要求/日常巡检」等无信息量表述。
3. **依据具体**：原因非空、不笼统，且含设备/机房/数值证据。
4. **字段汇总**：按 spec 声明的字段统计填写单数与 Top 取值。
5. **成因归类按单计数**：一张单只投 1 票（首命中），不再按关键词命中次数计数
   （2026-10-18 事故：11 单被写成「共 23 单」）。
"""

from types import SimpleNamespace

from service.scheduled_task.report.narrate import _cause_keywords
from service.scheduled_task.report.quality import assess_details

_SPEC = SimpleNamespace(quality_fields=('调整原因', '处置总结'),
                        detail_summary_fields=('涉及设备',))


def _details(rows: list[dict]) -> dict:
  return {'rows': [{'ticketNo': no, 'fields': fields} for no, fields in rows]}


def test_copy_detection():
  reason = 'UPS蓄电池内阻偏高，连续3天触发告警，需调整阈值'
  q = assess_details(_details([['T-1', {'调整原因': reason, '处置总结': reason}]]), _SPEC)
  assert q['n'] == 1 and q['copy_n'] == 1, q
  row = q['rows'][0]
  assert '疑似复制' in row['flags'] and row['specific'], row


def test_summary_substring_of_reason_counts_as_copy():
  reason = '空调回风温度探头漂移，误报频繁'
  summary = '探头漂移'                     # 连续子串且 ≥6 字？此处 4 字 → 不算
  q = assess_details(_details([['T-1', {'调整原因': reason, '处置总结': summary}]]), _SPEC)
  assert q['copy_n'] == 0, q                # 短子串不判复制（避免误伤正常概括）
  q2 = assess_details(_details([['T-1', {'调整原因': reason,
                                         '处置总结': '空调回风温度探头漂移'}]]), _SPEC)
  assert q2['copy_n'] == 1, q2


def test_vague_reason():
  q = assess_details(_details([
    ['T-1', {'调整原因': '按要求', '处置总结': '已调整'}],
    ['T-2', {'调整原因': '暂无', '处置总结': ''}],
  ]), _SPEC)
  assert q['vague_reason_n'] == 2, q
  assert q['specific_n'] == 0, q
  assert q['no_summary_n'] == 1, q


def test_boilerplate_summary():
  reason = '惠州园区空调冷量不足，新增2台精密空调后需下调回风温度阈值'
  q = assess_details(_details([['T-1', {'调整原因': reason, '处置总结': '已完成'}]]), _SPEC)
  assert q['boilerplate_n'] == 1 and q['copy_n'] == 0, q
  # 带数字/单位的总结不算模板化（有可核查的事实）
  q2 = assess_details(_details([['T-2', {'调整原因': reason,
                                         '处置总结': '已完成，回风温度稳定在24℃'}]]), _SPEC)
  assert q2['boilerplate_n'] == 0, q2


def test_specific_evidence():
  q = assess_details(_details([
    ['T-1', {'调整原因': '3号机房UPS电池组内阻升高至120%', '处置总结': '已下调阈值'}],
    ['T-2', {'调整原因': '业务调整', '处置总结': '已关闭屏蔽'}],
  ]), _SPEC)
  assert q['specific_n'] == 1, q
  assert q['rows'][0]['specific'] and not q['rows'][1]['specific'], q['rows']


def test_summary_fields_aggregation():
  details = _details([
    ['T-1', {'调整原因': '原因一', '处置总结': '', '涉及设备': 'UPS'}],
    ['T-2', {'调整原因': '原因二', '处置总结': '', '涉及设备': 'UPS'}],
    ['T-3', {'调整原因': '原因三', '处置总结': '', '涉及设备': '空调'}],
    ['T-4', {'调整原因': '原因四', '处置总结': '', '涉及设备': '  '}],
  ])
  sf = assess_details(details, _SPEC)['summary_fields']
  assert len(sf) == 1 and sf[0]['field'] == '涉及设备', sf
  assert sf[0]['filled_n'] == 3, sf
  assert sf[0]['top'][0] == {'value': 'UPS', 'n': 2}, sf


def test_no_quality_fields_keeps_counts_zero():
  spec = SimpleNamespace(quality_fields=(), detail_summary_fields=())
  q = assess_details(_details([['T-1', {'任意': '文本'}]]), spec)
  assert q['n'] == 0 and q['copy_n'] == 0 and q['rows'] == [], q


def test_cause_keywords_count_tickets_not_hits():
  """同一张单命中同一类别多次只算 1 单；按表顺序首命中；无命中记「未归入」。"""
  table = {'A类': ('温度', '湿度'), 'B类': ('漏水',)}
  details = _details([
    ['T-1', {'描述': '温度异常，湿度也偏高，温度再次告警'}],   # A 类（多次命中仍 1 票）
    ['T-2', {'描述': '现场漏水，湿度偏大'}],                    # 首命中 A 类（表顺序优先）
    ['T-3', {'描述': '与上述无关的事项'}],                      # 未归入
  ])
  out = _cause_keywords(details, table)
  assert out == {'A类': 2, '可归类': 2, '未归入': 1}, out


def test_cause_keywords_empty_details():
  assert _cause_keywords({}, {'A类': ('温度',)}) == {'可归类': 0, '未归入': 0}


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

"""第 10 章长文本归纳的校验闸与适用范围（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_detail_summary`

**不依赖任何外部数据/网络**。覆盖两件事：

1. `_summary_ok` 的四道闸 —— 摘要必须短、无省略号、**不能出现原文没有的数字**、且确实比原文短
   （模型编造数字是这类压缩最常见的失真，必须整条丢弃、回退原文）。
2. 归纳**只对声明了 `detail_condense_fields` 的规格生效**（目前只有阈值单）：
   其他四类调用 `summarize_details` 立刻返回空，不产生任何网络调用。
"""

import asyncio
import types

from service.scheduled_task.report.narrate import _SUMMARY_MAX_LEN, _summary_ok, summarize_details
from service.scheduled_task.report.spec import (
  BREAKDOWN_SPEC, CHANGE_SPEC, EVENT_SPEC, QUESTION_SPEC, THRESHOLD_SPEC,
)

_SRC = ('按照上电工单需求，B1-M4-201/202/203/204/301/302机房机柜加电进行阈值设置，'
        '201机房1-1#、1-2#、2-1#、2-2#、3-1#、3-2#、4-1#、4-2#列头柜，'
        '202机房1-1#、1-2#、2-1#、2-2#列头柜，累计 36 个列头柜，操作已完成。')


def test_accepts_faithful_summary():
  txt = '对 B1-M4 六间机房（201/202/203/204/301/302）的 36 个列头柜完成加电阈值设置'
  assert _summary_ok(txt, _SRC), txt


def test_rejects_invented_number():
  txt = '对 B1-M4 六间机房的 40 个列头柜完成加电阈值设置'
  assert not _summary_ok(txt, _SRC), '原文没有 40，必须丢弃（数字闸）'


def test_rejects_ellipsis():
  txt = '对 B1-M4 六间机房的列头柜完成加电阈值设置…'
  assert not _summary_ok(txt, _SRC)
  assert not _summary_ok('对 B1-M4 六间机房的列头柜完成阈值设置...', _SRC)


def test_rejects_long_or_empty():
  assert not _summary_ok('', _SRC)
  assert not _summary_ok('阈' * (_SUMMARY_MAX_LEN + 1), _SRC)


def test_rejects_not_shorter_than_source():
  short = '机柜加电阈值设置完成'
  assert not _summary_ok(short, short), '不缩短原文的“摘要”没有意义'


def test_only_threshold_declares_condense_fields():
  assert THRESHOLD_SPEC.detail_condense_fields == ('调整原因', '处置总结'), \
    THRESHOLD_SPEC.detail_condense_fields
  for spec in (EVENT_SPEC, BREAKDOWN_SPEC, CHANGE_SPEC, QUESTION_SPEC):
    assert spec.detail_condense_fields == (), f'{spec.ticket} 不应启用长文本归纳'


def test_no_network_call_without_condense_fields():
  """未声明字段的规格：即便详情里有超长文本，也直接返回空（不调模型）。"""
  details = {'rows': [{'ticketNo': 'T-1', 'fields': {'原因分析': '长' * 200}}]}
  assert asyncio.run(summarize_details(details, types.SimpleNamespace())) == {}
  assert asyncio.run(summarize_details(details, EVENT_SPEC)) == {}


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

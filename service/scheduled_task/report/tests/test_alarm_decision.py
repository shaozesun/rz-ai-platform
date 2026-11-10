"""事件单 ↔ DCIM 告警「判定表」的自检（纯 Python，可直接跑）。

跑法：`python -m service.scheduled_task.report.tests.test_alarm_decision`

**不连 Mongo**：存储层用内存桩；`AlarmDecisionStore` 的 fail-open 用抛异常的
假 `mongodb_manager` 验证。覆盖：

1. 键 = `(工单号, 发生时间, 口径)`：同单同时间、不同口径不串用（W45 事故回归闸）；
2. 缺工单号 / 缺发生时间 → 不产键（不落表）；
3. 口径归一：园区名/短名 → slug；空/全国 → `nationwide`；
4. `is_reusable`：版本或覆盖指纹变了视为未命中；`unknown` 等非落表状态不可复用；
   `no_alarm` 在 `recheck_after` 前每次重查、之后永久复用；
5. `build_decision`：临时状态不产文档；`no_alarm` 的重查时刻取
   `max(now+30min, 发生时间+24h)`；
6. `decision_view` 带出名称/部门（对话展示用）；
7. 存储层读写失败 fail-open（`load`→{}、`query`→[]、`save` 不抛）。
"""

import asyncio
from datetime import datetime, timedelta, timezone

from service.scheduled_task.report import alarm_decision as d

_TS = int(datetime(2026, 11, 2, 14, 3, tzinfo=timezone.utc).timestamp())
_NOW = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)


def _doc(**over) -> dict:
  doc = d.build_decision(
    ticket_no='T1', occurrence_ts=_TS, scope='nationwide', status='matched',
    name='市电中断', dc='A1分部', match='time',
    alarm={'cluster': '廊坊A', 'event_log_id': 'G1'}, point_sig=('廊坊A', 'd1', 'P1'),
    alarm_ts=_TS + 10, alarm_snapshot={'device': 'UPS-01'}, now=_NOW)
  doc.update(over)
  return doc


# ── 1. 键含口径 ──
def test_key_includes_scope():
  nationwide = d.decision_id('T1', _TS, 'nationwide')
  langfang = d.decision_id('T1', _TS, '廊坊')
  assert nationwide != langfang
  assert d.decision_id('T1', _TS, '全国') == nationwide
  assert d.decision_id('T1', _TS, '廊坊园区') == langfang
  # 不同发生时间 / 不同工单号也不串
  assert d.decision_id('T1', _TS + 1, 'nationwide') != nationwide
  assert d.decision_id('T2', _TS, 'nationwide') != nationwide


# ── 2. 缺字段不产键 ──
def test_key_requires_ticket_and_time():
  assert d.decision_id('', _TS, 'nationwide') is None
  assert d.decision_id('T1', None, 'nationwide') is None
  assert d.decision_id('T1', 0, 'nationwide') is None
  assert d.decision_id('T1', 'abc', 'nationwide') is None
  assert d.decision_id('T1', str(_TS), 'nationwide') == d.decision_id(
    'T1', _TS, 'nationwide')          # 数字字符串等价


# ── 3. 口径归一 ──
def test_normalize_scope():
  assert d.normalize_scope('廊坊园区') == '廊坊'
  assert d.normalize_scope('廊坊') == '廊坊'
  assert d.normalize_scope('平湖') == '平湖'
  assert d.normalize_scope('') == 'nationwide'
  assert d.normalize_scope(None) == 'nationwide'
  assert d.normalize_scope('全国') == 'nationwide'
  assert d.normalize_scope('nationwide') == 'nationwide'


# ── 4. 复用判定 ──
def test_is_reusable_version_and_fingerprint():
  assert d.is_reusable(_doc(), _NOW) is True
  assert d.is_reusable(None, _NOW) is False
  assert d.is_reusable(_doc(rule_version=d.RULE_VERSION + 1), _NOW) is False
  assert d.is_reusable(_doc(coverage_fingerprint='旧范围'), _NOW) is False
  assert d.is_reusable(_doc(status='unknown'), _NOW) is False
  assert d.is_reusable(_doc(status='no_time'), _NOW) is False


def test_no_alarm_recheck_window():
  doc = d.build_decision(
    ticket_no='T1', occurrence_ts=_TS, scope='nationwide', status='no_alarm',
    reason='±2 分钟内没有告警', now=_NOW)
  # 发生时间 14:03 + 24h 晚于 now+30min → 取 24h 那条
  assert doc['recheck_after'] == datetime.fromtimestamp(
    _TS + d.NO_ALARM_RECHECK_SECONDS, tz=timezone.utc)
  assert d.is_reusable(doc, _NOW) is False                 # 还没到重查时刻
  assert d.is_reusable(doc, doc['recheck_after'] - timedelta(seconds=1)) is False
  assert d.is_reusable(doc, doc['recheck_after']) is True   # 到点后永久复用

  # 老事件（发生时间已过 24h）→ 按 now+30min 兜底
  old = _TS - 30 * 86400
  doc2 = d.build_decision(ticket_no='T1', occurrence_ts=old, scope='nationwide',
                          status='no_alarm', now=_NOW)
  assert doc2['recheck_after'] == _NOW + timedelta(
    seconds=d.NO_ALARM_MIN_RECHECK_SECONDS)


# ── 5. 组装 ──
def test_build_decision_statuses():
  for status in ('unknown', 'no_time', ''):
    assert d.build_decision(ticket_no='T1', occurrence_ts=_TS, scope='廊坊',
                            status=status, now=_NOW) is None
  # 缺工单号 → 不产文档（即便状态可落表）
  assert d.build_decision(ticket_no='', occurrence_ts=_TS, scope='廊坊',
                          status='matched', now=_NOW) is None
  doc = _doc()
  assert doc['_id'] == d.decision_id('T1', _TS, 'nationwide')
  assert doc['scope'] == 'nationwide' and doc['status'] == 'matched'
  assert doc['point_sig'] == ['廊坊A', 'd1', 'P1']
  assert doc['recheck_after'] is None
  assert doc['decided_at'] == _NOW


def test_build_decision_normalizes_scope():
  doc = d.build_decision(ticket_no='T1', occurrence_ts=_TS, scope='廊坊园区',
                         status='not_covered', now=_NOW)
  assert doc['scope'] == '廊坊' and doc['_id'] == d.decision_id('T1', _TS, '廊坊')


# ── 6. 视图 ──
def test_decision_view():
  view = d.decision_view(_doc())
  assert view['status'] == 'matched' and view['match'] == 'time'
  assert view['name'] == '市电中断' and view['dc'] == 'A1分部'
  assert view['alarm'] == {'cluster': '廊坊A', 'event_log_id': 'G1'}
  assert view['point_sig'] == ('廊坊A', 'd1', 'P1')
  assert view['alarm_ts'] == _TS + 10
  # 缺字段不炸
  assert d.decision_view({})['status'] == ''


# ── 7. 存储层 fail-open ──
class _BrokenDb:
  def __getitem__(self, name):
    return self

  def find(self, *a, **kw):
    raise RuntimeError('mongo down')

  def sort(self, *a, **kw):
    return self

  def limit(self, *a, **kw):
    return self

  async def bulk_write(self, *a, **kw):
    raise RuntimeError('mongo down')


class _BrokenManager:
  db = _BrokenDb()


def test_store_fail_open():
  saved = d.mongodb_manager
  d.mongodb_manager = _BrokenManager
  try:
    store = d.AlarmDecisionStore()
    assert asyncio.run(store.load(['k1'])) == {}
    assert asyncio.run(store.query(ticket_no='T1')) == []
    asyncio.run(store.save([_doc()]))        # 不抛
  finally:
    d.mongodb_manager = saved


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

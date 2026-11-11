"""实时写入攒批自检（纯 assert，不连库，非零退出码）。

    python -m service.dcim_ingest.tests.test_realtime_batch

锁住六条：
  1. 窗口未到：不写库、不 commit（数据留在缓冲里）
  2. 到时间阈值：**恰好**一次 write + 一次 commit，顺序 write → commit
  3. 到行数上限：同上，且写入行数 = 摄入行数（**绝不截断**）
  4. 写库失败：不 commit、异常上抛、缓冲不清空
  5. 同设备多采样：一条语句、多个 VALUES 行，语句数**不随行数线性增长**
  6. 整批失败不推进零阶保持状态；重试的 SQL 与首次调用逐字一致
"""
from __future__ import annotations

import json
import threading
import time

from config.settings import settings
from service.dcim_ingest import consumers
from service.dcim_ingest.consumers import RealtimeConsumer
from service.dcim_ingest.models import DeviceSample
from service.dcim_ingest.wide_writer import WideWriter

_COLUMNS = ['ts', 'c_a', 'c_b', 'report_ts', 'quality']
_MAPPING = {'A': ('g0', 'c_a'), 'B': ('g0', 'c_b')}


# ── 窗口参数（测试里直接改 settings，退出时还原） ──
class _Window:
  def __init__(self, seconds: float, max_rows: int = 30000):
    self.seconds = seconds
    self.max_rows = max_rows

  def __enter__(self):
    self.old = (
      settings.DCIM_REALTIME_FLUSH_SECONDS,
      settings.DCIM_REALTIME_FLUSH_MAX_ROWS,
    )
    settings.DCIM_REALTIME_FLUSH_SECONDS = float(self.seconds)
    settings.DCIM_REALTIME_FLUSH_MAX_ROWS = int(self.max_rows)
    return self

  def __exit__(self, *_exc):
    settings.DCIM_REALTIME_FLUSH_SECONDS, settings.DCIM_REALTIME_FLUSH_MAX_ROWS = self.old
    return False


# ── 假 Kafka 报文：`_ingest` 被替换，但 `_loop` 仍会 `json.loads(value())` ──
class _Msg:
  def __init__(self, payload: dict):
    self._v = json.dumps(payload).encode()

  def error(self):
    return None

  def topic(self):
    return 't'

  def partition(self):
    return 0

  def offset(self):
    return 0

  def value(self):
    return self._v


def _msg(n: int) -> _Msg:
  """一条报文 → `_ingest` 往缓冲里塞 n 个样本（内容对 flush 语义无关）。"""
  return _Msg({'n': n})


class _ScriptedConsumer:
  """按脚本返回批次；脚本用完后置 `stop`，让 `_loop` 走退出分支。"""

  def __init__(self, batches, stop):
    self.batches = list(batches)
    self.stop = stop
    self.polls = 0
    self.order: list[str] = []
    self.on_poll = None          # on_poll(已完成轮数)：在循环中途断言

  def consume(self, num_messages=None, timeout=None):
    if self.on_poll:
      self.on_poll(self.polls)
    if not self.batches:
      self.stop.set()
      return []
    self.polls += 1
    return self.batches.pop(0)

  def commit(self, asynchronous=True):
    self.order.append('commit')


class _FakeStore:
  """只认 `_flush` 用到的口子：写设备行 / 写 PUE / 语句计数。"""

  def __init__(self, order: list[str], fail: bool = False):
    self.order = order
    self.fail = fail
    self.writes: list[int] = []
    self.statements = 0
    self.pue_statements = 0

  def write_devices(self, samples) -> None:
    if self.fail:
      raise RuntimeError('TDengine HTTP 500 Internal Server Error')
    self.writes.append(len(samples))
    self.statements += 1
    self.order.append('write')

  def write_pue(self, samples) -> int:
    if not samples:
      return 0
    if self.fail:
      raise RuntimeError('TDengine HTTP 500 Internal Server Error')
    self.pue_statements += 1
    return len(samples)


class _TestConsumer(RealtimeConsumer):
  """不走 Kafka 报文解析：直接把 `{'n': k}` 变成 k 个样本进缓冲。"""

  def __init__(self, stop):
    super().__init__(
      cluster='廊坊A', kind='realtime', topic='t',
      consumer_config={}, dsn='', batch_size=500, stop_event=stop,
    )

  def _ingest(self, raw, meta, buf) -> None:
    buf.extend(object() for _ in range(int(raw.get('n', 0))))


def _run(batches, check=None):
  """跑一遍 `_loop`；`check(polls, store, order)` 在每轮 poll 前调用。"""
  stop = threading.Event()
  consumer = _TestConsumer(stop)
  order: list[str] = []
  store = _FakeStore(order)
  fake = _ScriptedConsumer(batches, stop)
  fake.order = order
  if check:
    fake.on_poll = lambda n: check(n, store, order)
  consumer._loop(store, fake)
  return store, fake, order


# ── 1 ──
def _check_window_holds():
  with _Window(5.0):
    def check(n, store, _order):
      if n == 2:
        assert store.writes == [], f'窗口未到就写库了：{store.writes}'

    store, _fake, order = _run([[_msg(3)], [_msg(4)]], check)
  # 退出时补刷：窗口里剩下的 7 行必须写掉，否则只能等重启后重放
  assert store.writes == [7], store.writes
  assert order == ['write', 'commit'], order


# ── 2 ──
def _check_time_threshold():
  with _Window(0.05):
    def check(n, store, order):
      if n == 1:
        time.sleep(0.08)              # 让「距上次 flush」超过窗口
      elif n == 2:
        assert store.writes == [5], f'应恰好写一次 5 行：{store.writes}'
        assert order == ['write', 'commit'], f'顺序必须是 write → commit：{order}'

    store, fake, order = _run([[_msg(3)], [_msg(2)]], check)
  assert store.writes == [5], store.writes        # 退出时缓冲已空，不再补刷
  assert order == ['write', 'commit'], order
  assert fake.polls == 2, fake.polls


# ── 3 ──
def _check_row_cap():
  with _Window(999.0, max_rows=5):
    store, _fake, order = _run([[_msg(3)], [_msg(3)]])
  # 上限只决定「什么时候写」，**不截断**：摄入 6 行就写 6 行
  assert store.writes == [6], store.writes
  assert order == ['write', 'commit'], order


# ── 4 ──
def _check_failure_keeps_buffer():
  old = consumers._DB_MAX_RETRY
  consumers._DB_MAX_RETRY = 1            # 不真等 2/4/6/8 秒退避
  try:
    stop = threading.Event()
    consumer = _TestConsumer(stop)
    order: list[str] = []
    store = _FakeStore(order, fail=True)
    fake = _ScriptedConsumer([], stop)
    fake.order = order
    buf = [object(), object()]
    raised = False
    try:
      consumer._flush(store, fake, buf)
    except RuntimeError:
      raised = True
    assert raised, '写库失败没有上抛，上层不会重试'
    assert order == [], f'写库失败却提交了 offset：{order}'
    assert len(buf) == 2, f'失败后缓冲被清空（=数据丢失）：{len(buf)}'
    assert consumer.total == 0, consumer.total
  finally:
    consumers._DB_MAX_RETRY = old


# ── 5 ──
class _FakePg:
  def connect(self) -> None:
    pass

  def close(self) -> None:
    pass

  def devices_meta(self, guids, cluster=None) -> dict:
    return {g: {'device_type': 'T1', 'space_guid': 'S1'} for g in guids}

  def allocate_columns_locked(self, device_type, codes, commit: bool = False) -> dict:
    return {code: _MAPPING[code] for code in codes}

  def commit(self) -> None:
    pass

  def rollback(self) -> None:
    pass


class _FakeTd:
  def __init__(self, hook=None):
    self.hook = hook
    self.executed: list[str] = []
    self.attempted: list[str] = []

  def connect(self) -> None:
    pass

  def close(self) -> None:
    pass

  def ensure_stable(self, stable, cols) -> None:
    pass

  def stable_columns(self, stable) -> list[str]:
    return list(_COLUMNS)

  def exec_sql(self, sql: str) -> None:
    self.attempted.append(sql)
    err = self.hook(sql) if self.hook else None
    if err is not None:
      raise err
    self.executed.append(sql)


def _writer(td: _FakeTd, workers: int = 1) -> WideWriter:
  writer = WideWriter(_FakePg(), td, cluster='廊坊A', cluster_code='LF-', workers=workers)
  writer.connect()
  return writer


def _sample(ts: int, **values) -> DeviceSample:
  return DeviceSample(device_guid='G1', idc='', ts=ts, report_ts=None, values=values)


def _check_multi_row_single_statement():
  """同一设备两个时刻：一条语句、两个 VALUES 行 —— 攒批的收益就在这里。"""
  two = [_sample(1000, A=1.0, B=2.0), _sample(2000, A=3.0, B=4.0)]

  td = _FakeTd()
  writer = _writer(td)
  try:
    rows = writer.write(list(two))
  finally:
    writer.close()
  assert rows == 2, rows
  assert len(td.executed) == 1, f'两行应合并成一条语句：{len(td.executed)}'
  assert writer.statements == 1, writer.statements
  sql = td.executed[0]
  assert 'd_lf_g1 USING' in sql, sql[:80]
  assert sql.split('VALUES', 1)[1].count(') (') == 1, f'VALUES 行数不对：{sql[-120:]}'

  # 对照：分两批各写一行 → 两条语句。语句数不随行数线性增长才是攒批的意义
  td2 = _FakeTd()
  writer2 = _writer(td2)
  try:
    writer2.write([two[0]])
    writer2.write([two[1]])
  finally:
    writer2.close()
  assert writer2.statements == 2, writer2.statements
  assert len(td2.executed) == 2, len(td2.executed)


# ── 6 ──
def _check_failed_batch_keeps_state():
  state = {'fail': True}

  def hook(_sql):
    return RuntimeError('TDengine HTTP 500 Internal Server Error') if state['fail'] else None

  # 第二行会带出第一行的 A（零阶保持）——用它检验「失败后再写」不会凭空多出 A
  samples = [_sample(1000, A=1.0), _sample(2000, B=2.0)]

  td = _FakeTd(hook=hook)
  writer = _writer(td)
  raised = False
  try:
    writer.write(list(samples))
  except RuntimeError:
    raised = True
  assert raised, '瞬时错误没有上抛'
  assert writer._last == {}, f'失败却推进了零阶保持状态：{writer._last}'

  state['fail'] = False
  try:
    assert writer.write(list(samples)) == 2
  finally:
    writer.close()

  # 重试的 SQL 必须与首次调用逐字一致（状态没被提前推进）
  assert td.attempted and td.executed[:1] == td.attempted[:1], (
    f'重试 SQL 与首次不一致：\n{td.attempted[:1]}\n{td.executed[:1]}'
  )

  # 对照：全新 writer（状态为空）写同一批，结果必须与本 writer 的重试一致
  td2 = _FakeTd()
  writer2 = _writer(td2)
  try:
    writer2.write(list(samples))
  finally:
    writer2.close()
  assert td2.executed == td.executed, '重试结果与「首次即成功」不一致'


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('_check_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'{len(tests)} 项通过：攒批窗口 / 上限不截断 / 失败不提交 / 状态延后提交')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

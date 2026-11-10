"""并行下发自检（纯 assert，不连库，非零退出码）。

    python -m service.dcim_ingest.tests.test_parallel_write

锁住 `WideWriter` 第三步改造的四条规则：
  1. 并发度 = `workers`（>1 且不超过上限），不是串行
  2. 并行与串行**产出完全一致**（SQL 集合、行数）
  3. 失败分类：设备级错误只丢那一台；瞬时错误必须**抛出**（交给上层整批重试，不能静默丢）
  4. 子表名冲突（设备改过 device_type）自动换名，且下一批直接用新名
"""
from __future__ import annotations

import threading
import time

from service.dcim_ingest import wide_writer
from service.dcim_ingest.models import DeviceSample
from service.dcim_ingest.wide_writer import WideWriter

_COLUMNS = ['ts', 'c_a', 'c_b', 'report_ts', 'quality']
_MAPPING = {'A': ('g0', 'c_a'), 'B': ('g0', 'c_b')}


class _FakePg:
  """只认 WideWriter 用到的两个方法。"""

  def __init__(self, device_type: str = 'T1'):
    self.device_type = device_type

  def connect(self) -> None:
    pass

  def close(self) -> None:
    pass

  def devices_meta(self, guids, cluster=None) -> dict:
    return {g: {'device_type': self.device_type, 'space_guid': 'S1'} for g in guids}

  def allocate_columns_locked(self, device_type, codes, commit: bool = False) -> dict:
    return {code: _MAPPING[code] for code in codes}

  def commit(self) -> None:
    pass

  def rollback(self) -> None:
    pass


class _FakeTd:
  """记录 SQL、测并发峰值；`hook(sql)` 返回异常对象则抛它（模拟 TDengine 报错）。"""

  def __init__(self, hook=None, delay: float = 0.0):
    self.hook = hook
    self.delay = delay
    self.executed: list[str] = []
    self.active = 0
    self.max_active = 0
    self._lock = threading.Lock()

  def connect(self) -> None:
    pass

  def close(self) -> None:
    pass

  def ensure_stable(self, stable: str, cols: dict) -> None:
    pass

  def stable_columns(self, stable: str) -> list[str]:
    return list(_COLUMNS)

  def exec_sql(self, sql: str) -> None:
    with self._lock:
      self.active += 1
      self.max_active = max(self.max_active, self.active)
    try:
      if self.delay:
        time.sleep(self.delay)
      err = self.hook(sql) if self.hook else None
      if err is not None:
        raise err
      with self._lock:
        self.executed.append(sql)
    finally:
      with self._lock:
        self.active -= 1


def _samples(guids, ts: int = 1_000) -> list[DeviceSample]:
  return [
    DeviceSample(device_guid=g, idc='', ts=ts, report_ts=None,
                 values={'A': float(i), 'B': 2.0})
    for i, g in enumerate(guids)
  ]


def _writer(td: _FakeTd, *, workers: int, device_type: str = 'T1') -> WideWriter:
  writer = WideWriter(
    _FakePg(device_type), td, cluster='廊坊A', cluster_code='LF-', workers=workers,
  )
  writer.connect()
  return writer


def _one_device_per_chunk():
  """把「每条语句最多几台设备」压到 1，才能在自检里造出多个 chunk。"""
  return _DevicesPerInsert(1)


class _DevicesPerInsert:
  def __init__(self, value: int):
    self.value = value

  def __enter__(self):
    self.old = wide_writer._DEVICES_PER_INSERT
    wide_writer._DEVICES_PER_INSERT = self.value
    return self

  def __exit__(self, *_exc):
    wide_writer._DEVICES_PER_INSERT = self.old
    return False


def _check_concurrency():
  guids = [f'G{i}' for i in range(12)]
  td = _FakeTd(delay=0.02)
  writer = _writer(td, workers=4)
  try:
    with _one_device_per_chunk():
      rows = writer.write(_samples(guids))
  finally:
    writer.close()
  assert rows == 12, rows
  assert td.max_active > 1, f'没有并行（max_active={td.max_active}）'
  assert td.max_active <= 4, f'超出并发上限（max_active={td.max_active}）'
  # 子表名必须带园区代号（`d_<代号>_<guid>`），否则跨园区同名 guid 会串 tag
  assert all('d_lf_g' in s for s in td.executed), td.executed[:2]


def _check_parallel_equals_serial():
  guids = [f'G{i}' for i in range(8)]
  sqls = []
  rows = []
  for workers in (1, 4):
    td = _FakeTd()
    writer = _writer(td, workers=workers)
    try:
      with _one_device_per_chunk():
        rows.append(writer.write(_samples(guids)))
    finally:
      writer.close()
    sqls.append(sorted(td.executed))
  assert rows == [8, 8], rows
  assert sqls[0] == sqls[1], '并行与串行的 SQL 不一致'


def _check_device_isolation():
  """设备级错误（Too many columns）只丢那一台，同批其它照写、不抛异常。"""
  guids = ['G1', 'G2', 'G3']

  def hook(sql: str):
    return RuntimeError('TDengine 报错: Too many columns') if 'd_lf_g2 USING' in sql else None

  td = _FakeTd(hook=hook)
  writer = _writer(td, workers=3)
  try:
    with _one_device_per_chunk():
      rows = writer.write(_samples(guids))
  finally:
    writer.close()
  assert rows == 2, rows
  assert writer.failed_devices == 1, writer.failed_devices
  assert not any('d_lf_g2 USING' in s for s in td.executed)


def _check_transient_error_propagates():
  """瞬时错误（HTTP 500）必须从 write() 抛出 —— 否则上层那套退避重试永远不会触发。"""
  td = _FakeTd(hook=lambda _sql: RuntimeError('TDengine HTTP 500 Internal Server Error'))
  writer = _writer(td, workers=3)
  raised = False
  try:
    with _one_device_per_chunk():
      writer.write(_samples(['G1', 'G2']))
  except RuntimeError:
    raised = True
  finally:
    writer.close()
  assert raised, 'write() 吞掉了瞬时错误'
  assert writer.failed_devices == 0, '瞬时错误不能当设备故障跳过'


def _check_subtable_remap():
  """子表绑在旧超级表 → 换名重试成功，且下一批直接用新名（不再撞一次）。"""
  calls: list[str] = []

  def hook(sql: str):
    calls.append(sql)
    conflict = 'already exists in other stables'
    if 'INSERT INTO d_lf_g1 USING' in sql:
      return RuntimeError(f'TDengine 报错: Table {conflict}')
    return None

  td = _FakeTd(hook=hook)
  writer = _writer(td, workers=2)
  try:
    with _one_device_per_chunk():
      rows1 = writer.write(_samples(['G1']))
      rows2 = writer.write(_samples(['G1'], ts=2_000))
  finally:
    writer.close()

  assert rows1 == 1 and rows2 == 1, (rows1, rows2)
  assert writer.failed_devices == 0, writer.failed_devices
  assert writer.subtable_remaps == 1, writer.subtable_remaps
  assert 'd_lf_g1 USING' not in td.executed[-1], '第二批仍在用旧子表名'
  # 第二批只发一条语句（直接命中新名），没有再走「合并失败 → 逐台 → 换名」那串
  second_batch = calls[3:]
  assert len(second_batch) == 1, second_batch


def _check_mass_failure_reraises():
  """一批 6 台里 4 台设备级失败（>50%）→ 抛错整批重试，不能静默丢掉大半批。"""
  guids = [f'G{i}' for i in range(6)]

  def hook(sql: str):
    bad = any(f'd_lf_g{i}.' in sql or f'd_lf_g{i} USING' in sql for i in range(4))
    return RuntimeError('TDengine 报错: Too many columns') if bad else None

  td = _FakeTd(hook=hook)
  writer = _writer(td, workers=2)
  raised = False
  try:
    writer.write(_samples(guids))            # 6 台同批（默认 200 台上限）
  except RuntimeError:
    raised = True
  finally:
    writer.close()
  assert raised, '大批量设备级失败被静默跳过'

  # 对照：同样 6 台，只有 1 台坏 → 只丢那一台，不整批抛错
  def hook_one(sql: str):
    return (RuntimeError('TDengine 报错: Too many columns')
            if 'd_lf_g0 USING' in sql else None)

  td2 = _FakeTd(hook=hook_one)
  writer2 = _writer(td2, workers=2)
  try:
    rows = writer2.write(_samples(guids))
  finally:
    writer2.close()
  assert rows == 5, rows
  assert writer2.failed_devices == 1, writer2.failed_devices


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('_check_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'{len(tests)} 项通过：并行下发 / 失败分类 / 子表换名')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

"""测点拆组自检（纯 assert，不碰数据库，非零退出码）。

    python -m service.dcim_ingest.tests.test_group_split

锁住五条规则（`td_schema` 的命名 + `repository.allocate_columns_locked` 的分配
+ `client._merge_rows_by_key` 的读侧合并）：
  1. g0 沿用 `st_<type>`，g1 起是 `st_<type>_g<n>`（存量表名零迁移）
  2. 每组最多 `GROUP_MAX_COLUMNS` 个测点列，装满换下一组
  3. 存量编码**永不换组**（历史数据按组落在各自超级表里）
  4. 同一超级表内列名唯一（跨组的列名也唯一，读侧才能只按列名合并）
  5. 读侧同 `(时间, 设备)` 的多组行必须并成一行，`total` 取各组最大值
"""
import sys

from service.dcim_ingest import td_schema
from service.dcim_ingest.repository import DcimRepository

MAX = td_schema.GROUP_MAX_COLUMNS


class _FakeCursor:
  """只认 allocate_columns_locked 用到的三条语句。"""

  def __init__(self, store: dict):
    self.store = store          # {point_code: (group, col)}
    self.rows: list[dict] = []
    self.inserted: list[tuple[str, str, str]] = []
    self.stables: list[tuple[str, str]] = []

  def __enter__(self):
    return self

  def __exit__(self, *_exc):
    return False

  def execute(self, sql: str, params=()):
    self.rows = []
    if 'FROM dcim_td_column' in sql:
      self.rows = [
        {'point_code': code, 'td_column': col, 'point_group': group}
        for code, (group, col) in sorted(self.store.items())
      ]
    return None

  def executemany(self, sql: str, params_seq):
    if 'INSERT INTO dcim_td_column' in sql:
      for _dt, code, col, group in params_seq:
        self.inserted.append((code, col, group))
        self.store.setdefault(code, (group, col))
    elif 'INSERT INTO dcim_td_stable' in sql:
      for _dt, group, stable in params_seq:
        self.stables.append((group, stable))
    return None

  def fetchall(self):
    return self.rows


class _FakeConn:
  def __init__(self, cursor: _FakeCursor):
    self._cursor = cursor
    self.committed = 0
    self.rolled_back = 0

  def cursor(self):
    return self._cursor

  def commit(self):
    self.committed += 1

  def rollback(self):
    self.rolled_back += 1


def _repo(known: dict) -> tuple[DcimRepository, _FakeCursor, _FakeConn]:
  """造一个不连库的仓储：`known` = {point_code: (group, col)} 作为已有映射。"""
  cur = _FakeCursor(dict(known))
  conn = _FakeConn(cur)
  repo = object.__new__(DcimRepository)
  repo._dsn = ''
  repo._pool = None
  repo._conn = conn
  repo._stack = None
  return repo, cur, conn


def test_stable_name() -> None:
  # g0 沿用存量表名，任何 gN 都不改 g0 的名字
  assert td_schema.stable_name('00000000-00AG') == 'st_00000000_00ag'
  assert td_schema.stable_name('00000000-00AG', 'g0') == 'st_00000000_00ag'
  assert td_schema.stable_name('00000000-00AG', 'g1') == 'st_00000000_00ag_g1'
  assert td_schema.stable_name('00WG', 'g12') == 'st_00wg_g12'
  # 子表名带组后缀，否则同一设备的 g0/g1 会撞名
  base = td_schema.subtable_name('a-b-c')
  assert td_schema.subtable_name('a-b-c', 'g0') == base
  assert td_schema.subtable_name('a-b-c', 'g1') == f'{base}_g1'
  assert base != td_schema.subtable_name('a-b-c', 'g1')
  # 园区代号进子表名（代号里的 `-` 归一成 `_`）：同 guid 不同园区不再撞名
  assert td_schema.subtable_name('a-b-c', cluster_code='LF-') == 'd_lf_a_b_c'
  assert td_schema.subtable_name('a-b-c', 'g1', 'LF-') == 'd_lf_a_b_c_g1'
  assert td_schema.subtable_name('a-b-c', cluster_code='A2M1') == 'd_a2m1_a_b_c'
  # 换名形态沿用同一前缀
  variant = td_schema.subtable_variant('a-b-c', 'g0', 'T1', 'CQ-')
  assert variant.startswith('d_cq_a_b_c_'), variant


def test_next_group() -> None:
  assert td_schema.next_group('g0') == 'g1'
  assert td_schema.next_group('g9') == 'g10'
  assert td_schema.next_group('g10') == 'g11'
  assert td_schema.next_group('') == 'g1'
  assert td_schema.next_group('weird') == 'g1'


def test_overflow_allocates_next_group() -> None:
  """零存量 + 超容量线的编码 → 前面的进 g0，溢出的进 g1。"""
  repo, cur, conn = _repo({})
  codes = [f'P{i:05d}' for i in range(MAX + 5)]
  out = repo.allocate_columns_locked('T1', codes)

  assert len(out) == MAX + 5
  g0 = [c for c, (g, _col) in out.items() if g == 'g0']
  g1 = [c for c, (g, _col) in out.items() if g == 'g1']
  assert len(g0) == MAX, len(g0)
  assert len(g1) == 5, len(g1)
  # 分配按 sorted(codes) 顺序填组，溢出的正是字典序最大的 5 个
  assert sorted(g1) == sorted(codes)[-5:]
  # 只登记两组，且组名是对的
  assert dict(cur.stables)['g0'] == 'st_t1'
  assert dict(cur.stables)['g1'] == 'st_t1_g1'
  # 默认提交（写入侧显式传 commit=False 才由调用方提交）
  assert conn.committed == 1 and conn.rolled_back == 0


def test_existing_codes_never_move_group() -> None:
  """g0 装满后，存量编码留在 g0，新编码进 g1。"""
  full = {f'P{i:05d}': ('g0', f'p{i:05d}') for i in range(MAX)}
  repo, _cur, _conn = _repo(full)
  out = repo.allocate_columns_locked('T2', ['P00000', 'NEW_A', 'NEW_B'])

  assert len(out) == MAX + 2
  assert out['P00000'] == ('g0', 'p00000')     # 存量不动
  assert out['NEW_A'][0] == 'g1'
  assert out['NEW_B'][0] == 'g1'
  assert sum(1 for g, _c in out.values() if g == 'g0') == MAX


def test_column_names_unique_across_groups() -> None:
  """归一化撞名的编码（含跨组）必须拿到不同列名 —— 读侧只按列名合并行。"""
  full = {f'P{i:05d}': ('g0', f'p{i:05d}') for i in range(MAX)}
  repo, _cur, _conn = _repo(full)
  out = repo.allocate_columns_locked('T3', ['HT Temp', 'HT-Temp', 'ht_temp'])

  cols = [col for _g, col in out.values()]
  assert len(cols) == len(set(cols)), '列名重复：读侧合并会互相覆盖'
  # 三组新编码都进 g1（g0 已满），列名依次去重
  assert {g for code, (g, _c) in out.items() if code.startswith('HT')} == {'g1'}
  new_cols = {out['HT Temp'][1], out['HT-Temp'][1], out['ht_temp'][1]}
  assert new_cols == {'ht_temp', 'ht_temp_2', 'ht_temp_3'}, new_cols


def test_commit_false_leaves_transaction_open() -> None:
  """写入侧要先建 TDengine 表再提交映射：commit=False 不得自己提交。"""
  repo, _cur, conn = _repo({})
  repo.allocate_columns_locked('T4', ['A'], commit=False)
  assert conn.committed == 0


def test_allocation_is_deterministic() -> None:
  """回滚后重放要分到同样的列名与分组（否则映射与历史数据对不上）。"""
  codes = ['b', 'a', 'c', 'a-2']
  first, _c1, _n1 = _repo({})
  second, _c2, _n2 = _repo({})
  assert first.allocate_columns_locked('T5', codes) == \
      second.allocate_columns_locked('T5', list(reversed(codes)))


def test_merge_rows_by_key() -> None:
  """读侧合并：同 (时间, 设备) 的多组行必须并成**一行**（否则出的表按组数翻倍）。"""
  from service.query.dcim_meta.client import _merge_rows_by_key

  t1, t2 = '2026-10-26 10:00:00', '2026-10-26 10:05:00'
  parts = [
    {'columns': ['ts', 'device_guid', 'ht_temp'], 'records': [
      {'ts': t1, 'device_guid': 'g1', 'ht_temp': 21.5},
      {'ts': t2, 'device_guid': 'g1', 'ht_temp': None}]},
    {'columns': ['ts', 'device_guid', 'ht_hum', 'dev_guid'], 'records': [
      # 后一组对**别的组**的列是空值 → 不能盖掉前面已经取到的 21.5
      {'ts': t1, 'device_guid': 'g1', 'ht_temp': '', 'ht_hum': 40},
      {'ts': t2, 'device_guid': 'g1', 'ht_hum': 41}]},
  ]
  out = _merge_rows_by_key(parts, time_key='ts', total=2)

  assert len(out['records']) == 2, out['records']
  assert out['total'] == 2                  # 各组 total 相同 → 取 max，不能相加
  # 列取并集、保持首次出现顺序
  assert out['columns'] == ['ts', 'device_guid', 'ht_temp', 'ht_hum', 'dev_guid']
  assert out['records'][0]['ht_temp'] == 21.5, out['records'][0]
  assert out['records'][0]['ht_hum'] == 40
  assert out['records'][1]['ht_hum'] == 41

  # 跨园区：guid 只在园区内唯一 → 同 (ts, guid) 但 cluster_name 不同是**两台设备**，
  # 合并会把它们并成一行、列互相污染（单园区记录不带 cluster_name，键退化为空串，行为不变）
  cross = [
    {'columns': ['ts', 'cluster_name', 'device_guid', 'ht_temp'], 'records': [
      {'ts': t1, 'cluster_name': 'A', 'device_guid': 'g1', 'ht_temp': 21.5}]},
    {'columns': ['ts', 'cluster_name', 'device_guid', 'ht_hum'], 'records': [
      {'ts': t1, 'cluster_name': 'B', 'device_guid': 'g1', 'ht_hum': 40}]},
  ]
  cross_out = _merge_rows_by_key(cross, time_key='ts', total=2)
  assert len(cross_out['records']) == 2, cross_out['records']
  assert cross_out['records'][0]['cluster_name'] == 'A'
  assert cross_out['records'][1]['cluster_name'] == 'B'
  assert 'ht_hum' not in cross_out['records'][0]
  assert 'ht_temp' not in cross_out['records'][1]


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'{len(tests)} 项通过')
  return 0


if __name__ == '__main__':
  sys.exit(main())

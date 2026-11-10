"""多进程分片 + 稀疏列热路径自检（纯 assert，不连库，非零退出码）。

    python -m service.dcim_ingest.tests.test_shard_columns

锁住两件事：
  1. `shard_slice` 按**扁平序号轮询**分片 —— 同一园区（尤其廊坊A）的多条实时线程
     必须散到尽可能多的进程上，否则扩进程等于没扩；
  2. 改写后的 `_used_columns` / `build_insert_body` 与**逐列扫描**的旧实现**逐字一致**
     （列清单顺序严格等于 DESCRIBE 列序；`code_to_col`/`col_index` 预计算只影响速度）。
"""
from __future__ import annotations

import random

from service.dcim_ingest.config import shard_slice
from service.dcim_ingest.models import DeviceSample
from service.dcim_ingest.realtime import (
  _used_columns, build_insert_body,
)

_TAGS = ['廊坊A', 'G1', 'g0', 'SP1', 'TYPE', '']


def _sample(ts: int, values: dict, report_ts: int | None = None) -> DeviceSample:
  return DeviceSample(device_guid='G1', idc='', ts=ts, report_ts=report_ts, values=values)


def _legacy_used_columns(columns, col_to_code, samples) -> list[str]:
  """旧实现原样保留（逐列扫描 DESCRIBE）—— 新实现必须与它逐字一致。"""
  codes = {code for s in samples for code, v in s.values.items() if v is not None}
  has_report = any(s.report_ts is not None for s in samples)
  picked: list[str] = []
  for col in columns:
    if col == 'ts':
      picked.append(col)
    elif col == 'report_ts':
      if has_report:
        picked.append(col)
    elif col != 'quality' and col in col_to_code and col_to_code[col] in codes:
      picked.append(col)
  if not picked:
    picked = ['ts']
  if picked == ['ts']:
    pad = next((c for c in columns if c not in ('ts', 'report_ts', 'quality')), None)
    if pad:
      picked.append(pad)
  return picked


def _check_shard_round_robin() -> None:
  items = list(range(23))
  for shards in (1, 2, 3, 6):
    parts = [shard_slice(items, i, shards) for i in range(shards)]
    assert sorted(x for p in parts for x in p) == items, (shards, parts)
    if shards > 1:
      sizes = [len(p) for p in parts]
      assert max(sizes) - min(sizes) <= 1, sizes


def _check_same_cluster_threads_spread() -> None:
  """模拟「11 园区 × 8 条实时线程」的扁平清单：廊坊A 那 8 条要落到 6 个进程。"""
  workers = [f'cfg-{i}' for i in range(33)]
  lf_a = [f'lf-A-{i}' for i in range(8)]
  workers += lf_a
  workers += [f'real-{i}' for i in range(80)]
  procs = {i: shard_slice(workers, i, 6) for i in range(6)}
  landing = {name: [i for i, p in procs.items() if name in p][0] for name in lf_a}
  assert len(set(landing.values())) == 6, landing


def _check_used_columns_matches_legacy() -> None:
  rnd = random.Random(20261102)
  for case in range(400):
    n_cols = rnd.randint(0, 30)
    cols = [f'c_{i}' for i in range(n_cols)]
    # 列序打乱（真实 DESCRIBE 里 ts 在最前、report_ts/quality 在最后，这里只验证「按列序输出」）
    columns = ['ts'] + cols + ['report_ts', 'quality']
    rnd.shuffle(columns)
    mapped = rnd.sample(cols, rnd.randint(0, n_cols))
    col_to_code = {col: f'P{int(col[2:])}' for col in mapped}
    code_to_col = {code: col for col, code in col_to_code.items()}

    samples = []
    for k in range(rnd.randint(0, 4)):
      codes = rnd.sample(list(col_to_code.values()), rnd.randint(0, len(col_to_code)))
      values = {c: (None if rnd.random() < 0.15 else float(rnd.randint(-9, 9))) for c in codes}
      if rnd.random() < 0.2:
        values['UNKNOWN'] = 1.0                 # 未映射编码：两边都必须忽略
      report_ts = 999 if rnd.random() < 0.3 else None
      samples.append(_sample(1_000 + k, values, report_ts))

    want = _legacy_used_columns(columns, col_to_code, samples)
    got_plain = _used_columns(columns, col_to_code, samples)
    got_cached = _used_columns(
      columns, col_to_code, samples, code_to_col=code_to_col,
      col_index={col: i for i, col in enumerate(columns)},
    )
    assert got_plain == want, (case, 'plain', got_plain, want)
    assert got_cached == want, (case, 'cached', got_cached, want)

    body_plain = build_insert_body(
      stable='st_x', subtable='d_g1', tag_values=_TAGS,
      columns=columns, col_to_code=col_to_code, samples=samples,
    )
    body_cached = build_insert_body(
      stable='st_x', subtable='d_g1', tag_values=_TAGS,
      columns=columns, col_to_code=col_to_code, samples=samples,
      code_to_col=code_to_col, col_index={col: i for i, col in enumerate(columns)},
    )
    assert body_plain == body_cached, (case, body_plain, body_cached)


def main() -> int:
  tests = [v for k, v in sorted(globals().items()) if k.startswith('_check_')]
  for fn in tests:
    fn()
    print(f'  ok  {fn.__name__}')
  print(f'{len(tests)} 项通过：分片轮询 / 稀疏列与旧实现逐字一致')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

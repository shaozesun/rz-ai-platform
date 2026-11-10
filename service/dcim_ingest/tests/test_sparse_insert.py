"""稀疏列写入自检（纯 assert，不连库，非零退出码）。

    python -m service.dcim_ingest.tests.test_sparse_insert

锁住 `build_insert_body` 的一条规则：**只写本批真实采样的列**，
未列出的列交给 TDengine 填 NULL（与旧写法「逐列写 'NULL'」等价，但 SQL 小两个数量级）。
"""
from service.dcim_ingest.models import DeviceSample
from service.dcim_ingest.realtime import (
  _used_columns, build_insert_body, build_multi_device_insert,
)

_COLS = ['ts', 'c_a', 'c_b', 'c_c', 'report_ts', 'quality']
_C2C = {'c_a': 'A', 'c_b': 'B', 'c_c': 'C'}
_TAGS = ['廊坊A', 'G1', 'g0', 'SP1', 'TYPE', '']


def _s(ts=1_000, values=None, report_ts=None):
  return DeviceSample(device_guid='G1', idc='', ts=ts, report_ts=report_ts,
                      values=values or {})


def _body(samples):
  return build_insert_body(
    stable='st_x', subtable='d_g1', tag_values=_TAGS,
    columns=_COLS, col_to_code=_C2C, samples=samples,
  )


def _cols_of(body: str) -> list[str]:
  """从 `TAGS(...) (a,b) VALUES` 里取出列清单。"""
  tail = body.split('TAGS(', 1)[1]
  cols_part = tail.split(') (', 1)[1].split(') VALUES', 1)[0]
  return cols_part.split(',')


def _check_only_sampled_columns():
  body = _body([_s(values={'A': 1.0})])
  assert _cols_of(body) == ['ts', 'c_a'], _cols_of(body)
  assert ') VALUES (1000,1.0)' in body, body


def _check_report_ts_only_when_present():
  assert 'report_ts' not in _cols_of(_body([_s(values={'A': 1.0})]))

  body = _body([_s(values={'A': 1.0}, report_ts=999)])
  assert _cols_of(body) == ['ts', 'c_a', 'report_ts'], _cols_of(body)
  assert 'VALUES (1000,1.0,999)' in body, body

  # 同批只有部分行带 report_ts → 列在清单里，缺的那行写 NULL
  body = _body([_s(ts=1_000, values={'A': 1.0}, report_ts=999),
                _s(ts=2_000, values={'A': 2.0})])
  assert _cols_of(body) == ['ts', 'c_a', 'report_ts'], _cols_of(body)
  assert 'VALUES (1000,1.0,999) (2000,2.0,NULL)' in body, body


def _check_union_and_null_cells():
  body = _body([_s(ts=1_000, values={'A': 1.0}),
                _s(ts=2_000, values={'B': 2.0})])
  assert _cols_of(body) == ['ts', 'c_a', 'c_b'], _cols_of(body)
  assert 'VALUES (1000,1.0,NULL) (2000,NULL,2.0)' in body, body


def _check_none_and_quality_never_written():
  assert _cols_of(_body([_s(values={'A': None, 'B': 2.0})])) == ['ts', 'c_b']
  # quality 恒为 NULL（对方报文没给），不占列清单
  for vals in ({'A': 1.0}, {'A': None}):
    assert 'quality' not in _cols_of(_body([_s(values=vals)]))


def _check_empty_group_writes_one_placeholder():
  """拆组后本组没有该设备的测点 → 仍写一行（各组行数对齐），补一个占位列。"""
  body = _body([_s(values={'UNKNOWN': 1.0})])
  assert _cols_of(body) == ['ts', 'c_a'], _cols_of(body)
  assert 'VALUES (1000,NULL)' in body, body


def _check_each_fragment_has_its_own_columns():
  b1 = _body([_s(values={'A': 1.0})])
  b2 = build_insert_body(
    stable='st_x', subtable='d_g2', tag_values=['廊坊A', 'G2', 'g0', 'SP1', 'TYPE', ''],
    columns=_COLS, col_to_code=_C2C, samples=[_s(values={'B': 2.0})],
  )
  merged = build_multi_device_insert([b1, b2])
  assert merged.count('INSERT INTO') == 1, merged
  assert '(ts,c_a) VALUES' in merged and '(ts,c_b) VALUES' in merged, merged


def _check_used_columns_pure():
  """纯函数：不改动入参。"""
  samples = [_s(values={'A': 1.0})]
  before = [dict(s.values) for s in samples]
  _used_columns(_COLS, _C2C, samples)
  assert [dict(s.values) for s in samples] == before


def main():
  _check_only_sampled_columns()
  _check_report_ts_only_when_present()
  _check_union_and_null_cells()
  _check_none_and_quality_never_written()
  _check_empty_group_writes_one_placeholder()
  _check_each_fragment_has_its_own_columns()
  _check_used_columns_pure()
  print('sparse insert OK: 只写真实采样的列，其余交 TDengine 填 NULL')


if __name__ == '__main__':
  main()

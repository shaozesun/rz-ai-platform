"""PUE / 能耗统计自检（纯 assert，不联网、不连库）。

    python -m service.dcim_ingest.tests.test_pue

锁住三件事：
1. **属性目录**：108 个全名（18 指标 × 6 口径）都能解析出正确列，认不出只计数不抛；
2. **报文解析**：秒级时间戳换算、过旧时间戳回退到达时刻、0/未来值丢弃、同包混装口径分组；
3. **SQL 构造**：一栋楼两个口径 → 两张子表、tag 顺序与 `PUE_TAGS` 一致、稀疏列只列有值列、
   查询筛选片段（`building IN` / `time_type IN` / 时间窗 / 单引号转义）。
"""
import time

from service.dcim_ingest.models import parse_pue_samples, pue_stats, ts_stats
from service.dcim_ingest.pue import (
  PUE_TAGS, _pue_filters, build_pue_insert, build_pue_series, pue_subtable, unwrap_last_row,
)
from service.dcim_ingest.pue_catalog import (
  FULL_NAME_TO_COLUMN, METRICS, METRIC_COLUMNS, TIME_TYPES, resolve_metric,
)

_NOW = int(time.time() * 1000)
_DAY_MS = 86_400_000


def _pack(points: list[dict], building: str = 'P001_A009') -> dict:
  return {'guid': '00000000-0PUE-0000-0000-000000000009', 'status': 1,
          'building': building, 'buliding': True, 'points': points}


def _point(code: str, value: float, ts) -> dict:
  return {'guid': code, 'standardId': code, 'value': value, 'timestamp': ts, 'mgr_obj_id': ''}


def _check_catalog():
  assert len(METRIC_COLUMNS) == 18, METRIC_COLUMNS
  assert len(TIME_TYPES) == 6, TIME_TYPES
  assert len(FULL_NAME_TO_COLUMN) == 108, len(FULL_NAME_TO_COLUMN)
  # 每个全名都要真能解析出**同一个列**（目录里写错一处就会在这里暴露）
  for full, col in FULL_NAME_TO_COLUMN.items():
    hit = resolve_metric(full)
    assert hit is not None, full
    tt, metric = hit
    assert metric.column == col, (full, col, metric.column)
    assert tt.code in (1, 2, 3, 4, 5, 6), (full, tt)
  assert resolve_metric('PUE_Real_IT_Value')[0].code == 1
  assert resolve_metric('PUE_Year_Other_value')[0].code == 6
  assert resolve_metric('PUE_Real_IT_Value')[1].column == 'it_value'
  # 列名不能撞 TDengine 保留字（`value` 就是因此改成 `pue` 的）
  assert 'value' not in METRIC_COLUMNS
  assert METRIC_COLUMNS[0] == 'pue'
  assert all(m.column == m.column.lower() for m in METRICS)
  # 大小写不敏感（上游 standardId 实测是 `PUE_Real_IT_Value`）
  assert resolve_metric('pue_real_it_value') is not None
  assert resolve_metric('PUE_Real_Nope_Value') is None
  assert resolve_metric('') is None
  assert resolve_metric('Real_IT_Value') is None     # 缺 PUE_ 前缀


def _check_is_pue_and_parse():
  pue_stats(), ts_stats()
  samples = parse_pue_samples(_pack([
    _point('PUE_Real_IT_Value', 21015.0, _NOW // 1000),
    _point('PUE_Real_Value', 1.35, _NOW // 1000),
    _point('PUE_Real_Unknown_Value', 1.0, _NOW // 1000),
  ]))
  assert len(samples) == 1, samples
  s = samples[0]
  assert s.building == 'P001_A009'
  assert s.time_type == 1
  assert s.pue_guid.startswith('00000000-0PUE')
  assert s.ts == (_NOW // 1000) * 1000           # 秒 → 毫秒
  assert s.values == {'it_value': 21015.0, 'pue': 1.35}, s.values
  stats = pue_stats()
  assert stats['unknown_code'] == 1, stats


def _check_no_building_and_empty():
  pue_stats()
  assert parse_pue_samples(_pack([_point('PUE_Real_Value', 1.3, _NOW // 1000)], building='')) == []
  assert pue_stats()['no_building'] == 1
  assert parse_pue_samples(_pack([])) == []


def _check_bad_and_old_ts():
  pue_stats()
  old_s = (_NOW - 120 * _DAY_MS) // 1000        # 超出 KEEP（90 天）
  samples = parse_pue_samples(_pack([
    _point('PUE_Real_Value', 1.3, 0),           # 哨兵 0
    _point('PUE_Real_IT_Value', 1.0, -5),       # 负数
    _point('PUE_Hour_Value', 1.4, (_NOW + 3 * _DAY_MS) // 1000),   # 未来
    _point('PUE_Month_Value', 1.5, old_s),      # 过旧 → 回退到达时刻
  ]))
  stats = pue_stats()
  assert stats['bad_ts'] == 3, stats
  assert stats['ts_fallback'] == 1, stats
  assert len(samples) == 1, samples                # 只有月统计那条留下来
  assert samples[0].time_type == 5
  assert samples[0].ts > _NOW - 60_000, samples[0].ts   # 回退到「现在」附近
  assert samples[0].values == {'pue': 1.5}


def _check_mixed_time_types():
  """Real 与 Hour 在整点同一秒到达 → 必须分成两条样本（否则同子表同 ts 互相覆盖）。"""
  pue_stats()
  same_s = _NOW // 1000
  samples = parse_pue_samples(_pack([
    _point('PUE_Real_IT_Value', 21015.0, same_s),
    _point('PUE_Real_Value', 1.35, same_s),
    _point('PUE_Hour_Total_Value', 5.0, same_s),
    _point('PUE_Hour_Value', 1.41, same_s),
  ]))
  assert len(samples) == 2, samples
  by_type = {s.time_type: s for s in samples}
  assert by_type[1].values == {'it_value': 21015.0, 'pue': 1.35}
  assert by_type[2].values == {'total_value': 5.0, 'pue': 1.41}
  assert by_type[1].ts == by_type[2].ts


def _insert_of(samples) -> str:
  return build_pue_insert(
    cluster='廊坊A', cluster_code='lf', building='P001_A009',
    space_guid='sg-1', pue_guid='g-1', samples=samples,
  )


def _tags_of(sql: str) -> list[str]:
  """从 `TAGS(...)` 里按顺序取值（带引号的字段去掉引号）。"""
  body = sql.split('TAGS(', 1)[1].split(')', 1)[0]
  out = []
  for cell in body.split(','):
    cell = cell.strip()
    out.append(cell[1:-1] if cell.startswith("'") else cell)
  return out


def _check_insert_two_subtables():
  pue_stats()
  samples = parse_pue_samples(_pack([
    _point('PUE_Real_IT_Value', 21015.0, _NOW // 1000),
    _point('PUE_Real_Value', 1.35, _NOW // 1000),
    _point('PUE_Hour_Value', 1.4, _NOW // 1000),
  ]))
  sql = _insert_of(samples)
  assert sql.startswith('INSERT INTO '), sql
  assert pue_subtable('lf', 'P001_A009', 1) in sql, sql
  assert pue_subtable('lf', 'P001_A009', 2) in sql, sql
  assert sql.count('USING st_pue') == 2, sql
  # tag 顺序 = PUE_TAGS 的声明顺序（错位会静默把楼宇写进 cluster_name）
  first = sql.split('USING st_pue', 1)[1]
  tags = _tags_of(first)
  assert len(tags) == len(PUE_TAGS), (tags, PUE_TAGS)
  assert tags[0] == '廊坊A' and tags[1] == 'P001_A009'
  assert tags[2] == '1'                      # 时间口径代号
  assert tags[3] == 'sg-1' and tags[4] == 'g-1'
  # 稀疏列：只列本口径真有值的列 + ts 恒在
  assert '(ts,pue,it_value) VALUES' in first, first
  assert 'total_value' not in first, first
  # 子表名带园区代号（TD 子表名全库唯一，guid 只在园区内唯一）
  assert pue_subtable('lf', 'P001_A009', 1).startswith('d_lf_pue_'), sql


def _check_insert_skips_null_metrics():
  from service.dcim_ingest.models import PueSample

  sql = _insert_of([PueSample(building='P001_A009', time_type=1, pue_guid='g-1',
                              ts=_NOW, values={'pue': 1.3})])
  assert '(ts,pue) VALUES' in sql, sql
  assert sql.count('NULL') == 0, sql


def _check_filters():
  # 时间窗 + 楼宇 + 口径 + 单引号转义
  where = _pue_filters(
    cluster="廊坊A'X", buildings=['P001_A009'], time_types=[1, 2],
    start_ms=1, end_ms=2,
  )
  # 单引号按仓库既有口径转义（`realtime._q` 用反斜杠，不是双写）
  assert "cluster_name = '廊坊A\\'X'" in where, where
  assert "building IN ('P001_A009')" in where, where
  assert 'time_type IN (1,2)' in where, where
  assert 'ts >= 1 AND ts <= 2' in where, where
  # park 展开成代号列表
  where = _pue_filters(clusters=['廊坊A', '廊坊B1M1'])
  assert "cluster_name IN ('廊坊A','廊坊B1M1')" in where, where
  # 时序查询必须带时间窗
  try:
    build_pue_series(cluster='廊坊A')
    raise AssertionError('应报错：时序查询缺少时间窗')
  except ValueError as e:
    assert '时间窗' in str(e), e
  # 未知指标列不能拼进 SQL
  try:
    build_pue_series(columns=['nope'], cluster='廊坊A', start_ms=1, end_ms=2)
    raise AssertionError('应报错：未知指标列')
  except ValueError as e:
    assert 'nope' in str(e), e


def _check_unwrap_last_row():
  recs = unwrap_last_row([{
    'building': 'P001_A009', 'time_type': 1,
    'last_row(ts)': '2026-11-10T02:00:00Z', 'last_row(pue)': 1.3,
  }])
  assert recs == [{'building': 'P001_A009', 'time_type': 1,
                   'ts': '2026-11-10T02:00:00Z', 'pue': 1.3}], recs


def main() -> int:
  _check_catalog()
  _check_is_pue_and_parse()
  _check_no_building_and_empty()
  _check_bad_and_old_ts()
  _check_mixed_time_types()
  _check_insert_two_subtables()
  _check_insert_skips_null_metrics()
  _check_filters()
  _check_unwrap_last_row()
  print('PUE 自检通过（9 项）')
  return 0


if __name__ == '__main__':
  raise SystemExit(main())

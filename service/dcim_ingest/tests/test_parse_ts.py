"""实时数据时间戳校验自检（纯 assert，不联网、不连库）。

    python -m service.dcim_ingest.tests.test_parse_ts

锁住 `models._normalize_ts` / `parse_device_samples` 的规则 —— 起因是 2026-11-08 生产事故：
对方某测点推 `timestamp=0`，TDengine 按保留期（KEEP=90 天）**整条 INSERT 拒写**，
而错误被当成瞬时故障，整批重试 5 次 → 断开消费 → 重放同一批 → 该分区位点永久卡死。

- 秒级（10 位）epoch 归一为毫秒；
- 0 / 负数 / 早于保留期 / 未来值 **只丢该测点**，同包其它测点照常保留；
- `report_time` 同样校验，非法写 `None`（它是 TIMESTAMP 列，写 0 一样会被拒）；
- 空 `points` 仍返回 `[]`（不是错误）。
"""
import time

from service.dcim_ingest.models import parse_device_samples, ts_stats

_NOW = int(time.time() * 1000)
_DAY_MS = 86_400_000


def _pack(points: list[dict]) -> dict:
  return {'device_id': 'dev-1', 'idc': 'P001', 'points': points}


def _check_normal():
  ts_stats()                                   # 清零计数
  samples = parse_device_samples(_pack([
    {'point_code': 'a', 'value': 1, 'timestamp': _NOW, 'report_time': _NOW},
    {'point_code': 'b', 'value': 2.5, 'timestamp': _NOW, 'report_time': _NOW},
  ]))
  assert len(samples) == 1, samples
  assert samples[0].device_guid == 'dev-1'
  assert samples[0].ts == _NOW
  assert samples[0].report_ts == _NOW
  assert samples[0].values == {'a': 1.0, 'b': 2.5}
  assert ts_stats() == {'bad': 0, 'seconds': 0}


def _check_zero_ts_is_dropped():
  """毒报文原形：同一包里 `AC_Alarm_EEPROM_Fault` 的 timestamp/report_time 都是 0。"""
  ts_stats()
  samples = parse_device_samples(_pack([
    {'point_code': 'AC_Alarm_EEPROM_Fault', 'value': 0, 'timestamp': 0, 'report_time': 0},
    {'point_code': 'AC_Status_Comm', 'value': 0, 'timestamp': _NOW, 'report_time': _NOW},
  ]))
  assert len(samples) == 1, samples
  assert samples[0].ts == _NOW
  assert 'AC_Alarm_EEPROM_Fault' not in samples[0].values
  assert samples[0].values == {'AC_Status_Comm': 0.0}
  assert ts_stats()['bad'] == 1

  # 全包都坏 → 空列表（按空包处理，不报错）
  ts_stats()
  assert parse_device_samples(_pack([
    {'point_code': 'x', 'value': 1, 'timestamp': 0},
  ])) == []
  assert ts_stats()['bad'] == 1


def _check_seconds_normalized():
  ts_stats()
  sec = _NOW // 1000
  samples = parse_device_samples(_pack([
    {'point_code': 'pue', 'value': 1.5, 'timestamp': sec},
  ]))
  assert len(samples) == 1
  assert samples[0].ts == sec * 1000, samples[0].ts
  assert ts_stats()['seconds'] == 1


def _check_out_of_window():
  ts_stats()
  old = _NOW - 91 * _DAY_MS                     # 超过 KEEP（90 天）
  future = _NOW + 2 * _DAY_MS
  samples = parse_device_samples(_pack([
    {'point_code': 'old', 'value': 1, 'timestamp': old},
    {'point_code': 'future', 'value': 1, 'timestamp': future},
    {'point_code': 'neg', 'value': 1, 'timestamp': -1},
    {'point_code': 'ok', 'value': 1, 'timestamp': _NOW},
  ]))
  assert [s.ts for s in samples] == [_NOW]
  assert set(samples[0].values) == {'ok'}
  assert ts_stats()['bad'] == 3


def _check_bad_report_time():
  ts_stats()
  samples = parse_device_samples(_pack([
    {'point_code': 'a', 'value': 1, 'timestamp': _NOW, 'report_time': 0},
  ]))
  assert len(samples) == 1 and samples[0].ts == _NOW
  assert samples[0].report_ts is None

  # 非数字 / 缺失时间戳 → 丢该点（既有行为）
  ts_stats()
  assert parse_device_samples(_pack([
    {'point_code': 'a', 'value': 1, 'timestamp': 'abc'},
    {'point_code': 'b', 'value': 1},
  ])) == []
  assert ts_stats()['bad'] == 1


def _check_empty_points():
  assert parse_device_samples(_pack([])) == []


def main():
  _check_normal()
  _check_zero_ts_is_dropped()
  _check_seconds_normalized()
  _check_out_of_window()
  _check_bad_report_time()
  _check_empty_points()
  print('parse_ts OK: 秒级归一 / 0与越界只丢该测点 / report_time 兜底 / 空包不是错误')


if __name__ == '__main__':
  main()

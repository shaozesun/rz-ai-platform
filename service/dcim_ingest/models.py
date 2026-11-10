"""DCIM 配置数据解析：原始报文字段 → 落库字段。

字段口径来自对方 **9/29 版**《DCIM Kafka 接口文档》：

- 空间 `ems-config-space-rz`：guid/tag/path/name/idc/node_type(=1)/space_type/parent_id
- 设备 `ems-config-device-rz`：guid/path/name/idc/node_type(=2)/device_type/**device_name**/parent_id/extend_position
- 点位 `ems-config-point-rz`：id/guid/parent_id/name/idc/node_type(=3)/point_type/unit/status_map
  + 4 个告警阈值 **hint_alarm / general_alarm / important_alarm / urgent_alarm**（嵌套：maxUse/minUse/maxAlarm/minAlarm/maxEvent/minEvent）

> 注意 `device_name` 是**设备类型名称**（如「精密空调」），不是设备名称 —— 设备名称是 `name`。
> 阈值字段是 9/29 版新增的；旧版文档没有，所以早期实现未提取（原始报文一直在 `payload` 里，未丢）。

另有事件型数据（同样进 PG，不进 TDengine）：
- 告警事件 `ems-alarm-data-rz`（§8）：`parse_alarm`
- 告警工单 `ems-alarm-order-rz`（§10）：`parse_alarm_order`（实推报文比文档多带了空间/设备/测点字段，见函数注释）

`ems-real-data-rz` 里还**混着 PUE / 能耗统计报文**（没有 `device_id`、按楼宇推 18 个指标）：
`is_pue_message` / `parse_pue_samples`，编码目录见 `pue_catalog.py`，落独立超级表 `st_pue`。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from config.settings import settings

from .pue_catalog import resolve_metric

logger = logging.getLogger(__name__)

# 时间戳校验计数（秒级归一 / 被丢弃的坏值）。只用于日志暴露对方数据质量，
# 不影响落库语义；`consumers._progress_extra` 每次打印 lag 时读取并清零。
_TS_STATS: dict[str, int] = {'bad': 0, 'seconds': 0}

# 秒级 epoch 的合理区间（10 位）：2001-09 ~ 2286。落在这个区间的值不可能是毫秒。
_SECONDS_LOW, _SECONDS_HIGH = 10 ** 9, 10 ** 10
_DAY_MS = 86_400_000


def ts_stats() -> dict[str, int]:
  """读取并**清零**时间戳校验计数（多线程累加，数值本身是近似值，只用于观测）。"""
  out = dict(_TS_STATS)
  for k in _TS_STATS:
    _TS_STATS[k] = 0
  return out


def _epoch_ms(value: Any) -> tuple[int, str] | None:
  """原始时间戳 → `(毫秒, 'ms'|'s')`；类型转不了返回 `None`（**不做区间校验**）。

  秒级判定按量级（10 位即秒）：实时数据是毫秒，PUE 统计报文实测是秒。
  """
  try:
    t = int(value)
  except (TypeError, ValueError):
    return None
  unit = 'ms'
  if _SECONDS_LOW <= t < _SECONDS_HIGH:
    t *= 1000
    unit = 's'
  return t, unit


def _normalize_ts(value: Any, *, now_ms: int, max_age_days: int) -> tuple[int, str] | None:
  """`timestamp` / `report_time` → `(毫秒, 'ms'|'s')`；不可用返回 `None`。

  **为什么必须校验**：TDengine 对早于库保留期（KEEP=90 天）的时间戳**整条 INSERT 拒写**
  （`Timestamp data out of range`），而拒的是合并后的整条语句 —— 对方只要有一个测点推
  `timestamp=0`，该消费线程就会卡在那条消息上永久重放（2026-11-08 生产实测）。
  所以：秒级统一换算，0 / 负数 / 早于保留期 / 晚于明天一律当坏值丢弃（只丢该测点）。
  """
  norm = _epoch_ms(value)
  if norm is None:
    return None
  t, unit = norm
  if t <= 0 or t < now_ms - max_age_days * _DAY_MS or t > now_ms + _DAY_MS:
    return None
  return t, unit

# status_map 可能出现的分隔符：'1=开门;0=关门' / 中文分号 / 逗号
_STATUS_SEP = (';', '；', ',', '，')


class SkippedRecord(Exception):
  """**主动丢弃**的报文 —— 与「解析失败」区分开。

  区别决定去向：解析失败进 DLQ 留证；这一类是**已知无用的噪声**，只计数、**不入 DLQ**
  —— 配置类 topic 是**周期性推送**，入 DLQ 会把表刷爆（见 `consumers.run` 的捕获分支）。
  """


def _reject_truncated_guid(guid: str, kind: str) -> None:
  """guid 首段为空（以 `-`/`.` 开头）= 系统前缀被截断 → 丢弃。

  ⚠️ 2026-10-12 事故：DCIM 对**同一个空间推了两份** —— 完整的 `RZYWBXSJ-P006-…`
  和**前缀被砍掉**的 `-P006-…`。设备只挂在完整那份上，残缺那份是**查不到任何设备的
  空壳**；而 `-`(0x2D) < `R`(0x52) 会让它在 `ORDER BY path, name, guid` 里排到前面
  → 模型拿它去查设备得 0 条 →「空间 → 设备 → 测点」整条链在第一步就断，
  表现为「问机房有哪些设备/点位，全空」。

  DCIM 的 guid 结构是 `<系统前缀>-<园区>-…`（空间 `RZYWBXSJ-…`、设备 `CHNLFB01-…`），
  首段不该为空 —— 空即残缺。
  """
  if guid[:1] in ('-', '.'):
    raise SkippedRecord(f'{kind} guid 首段为空（前缀被截断）：{guid!r}')


@dataclass
class ParsedRecord:
  """一条待落库记录：当前态字段 + 历史表所需信息。"""

  guid: str
  op: str                      # upsert / delete
  values: dict[str, Any]       # 当前态表字段（不含 payload）
  payload: dict[str, Any]      # 原始报文（存 jsonb，供历史与追溯）


def parse_status_map(raw: Any) -> dict[str, str] | None:
  """把 `1=开门;0=关门` 解析为 {'1': '开门', '0': '关门'}；解析不出返回 None。"""
  if not raw or not isinstance(raw, str):
    return None
  sep = next((s for s in _STATUS_SEP if s in raw), None)
  if sep is None:
    return None
  out: dict[str, str] = {}
  for part in raw.split(sep):
    if '=' not in part:
      continue
    k, v = part.split('=', 1)
    k, v = k.strip(), v.strip()
    if k:
      out[k] = v
  return out or None


@dataclass(frozen=True)
class DeviceSample:
  """**一台设备在一个时刻的一组测点值** —— 宽表（一设备一子表、测点做列）的一行。

  为什么不按「一个测点一个值」展开：那种窄表里同一时刻的各测点是**不同的行**，
  要对齐得做 k 路 self-join 或应用层 pivot（TDengine 3.3 能做 self-join，
  但路数随测点数增长 —— 实测 799 万行上一次 self-join 取 3 行均耗 0.55s）。
  按设备成行则**同一行的各列天然同刻**。
  """

  device_guid: str
  idc: str                # 数据中心标识（对方推的常是空串）
  ts: int                 # 采集时间（毫秒）
  report_ts: int | None   # 上报 Kafka 时间（毫秒），报文没给就是 None
  values: dict[str, float]  # {point_code: value} —— 落库时按列映射写进对应列


def parse_device_samples(raw: dict) -> list[DeviceSample]:
  """实时数据 `ems-real-data-rz` → [DeviceSample, ...]。

  **一条消息 = 一个设备的 N 个测点值**（按设备组包）：

      {"device_id": "...", "idc": "...",
       "points": [{"point_code": "HT_Temp", "value": 35.7,
                   "timestamp": 1790665219000, "report_time": 1790665219000}]}

  `point_code` 是**测点编码**，拼上 `device_id` 才等于 PG 里 `dcim_points.guid`
  （契约：`point.guid = 设备 guid + '.' + 点位 id`）—— 这条契约是跨库关联的唯一依据。

  **为什么按 timestamp 分组**：实测同一消息内各测点的 `timestamp` 相同（就是一行），
  但万一对方把不同时刻的值混在一条消息里，分组能保证不串行。

  宽松处理：整包缺 `device_id` / `points` 才算坏包；**单个 point 缺关键字段则跳过那一条**，
  不让一个坏点毁掉整包（实时数据量大，整包失败代价高）。

  `points` 是**空数组**时返回 `[]`（**不是错误**）：对方按设备组包推送，设备这一刻没有有效
  测点就会推空数组 —— 实测约 4% 的包是这样。当成错误会让这 4% 一直在日志里刷"解析失败"。

  **时间戳校验**（见 `_normalize_ts`）：秒级归一为毫秒；0/负数/早于 KEEP/未来值只丢该测点。
  不校验的代价是对方一个坏值就能让整条消费线程永久卡死（整条合并 INSERT 被拒 → 重试 → 重放）。
  """
  device_id = str(raw.get('device_id') or '').strip()
  if not device_id:
    raise ValueError('实时数据缺少 device_id')
  points = raw.get('points')
  if not isinstance(points, list):
    raise ValueError('实时数据的 points 不是数组')
  if not points:
    return []                       # 空包：正常现象，直接跳过

  idc = str(raw.get('idc') or '')
  now_ms = int(time.time() * 1000)
  max_age_days = max(1, int(settings.DCIM_TS_MAX_AGE_DAYS or 90))
  grouped: dict[int, dict[str, float]] = {}
  report_at: dict[int, int | None] = {}
  for p in points:
    if not isinstance(p, dict):
      continue
    code = str(p.get('point_code') or '').strip()
    val = p.get('value')
    ts = p.get('timestamp')
    if not code or val is None or ts is None:
      continue                      # 坏点跳过，不整包失败
    norm = _normalize_ts(ts, now_ms=now_ms, max_age_days=max_age_days)
    if norm is None:
      _TS_STATS['bad'] += 1
      continue                      # 时间戳不可用：只丢该测点（0/过旧/未来）
    t, unit = norm
    if unit == 's':
      _TS_STATS['seconds'] += 1
    try:
      v = float(val)
    except (TypeError, ValueError):
      continue
    grouped.setdefault(t, {})[code] = v
    if t not in report_at:
      rep = p.get('report_time')
      norm_rep = (_normalize_ts(rep, now_ms=now_ms, max_age_days=max_age_days)
                  if rep is not None else None)
      report_at[t] = norm_rep[0] if norm_rep else None

  return [
    DeviceSample(device_guid=device_id, idc=idc, ts=t,
                 report_ts=report_at.get(t), values=vals)
    for t, vals in sorted(grouped.items())
  ]


# PUE 报文的数据质量计数（未知编码 / 坏时间戳 / 时间戳回退 / 缺楼宇）。
# 与 `_TS_STATS` 一样只做观测：消费线程每次打印 lag 时读取并清零。
_PUE_STATS: dict[str, int] = {
  'unknown_code': 0, 'bad_ts': 0, 'ts_fallback': 0, 'no_building': 0,
}


def pue_stats() -> dict[str, int]:
  """读取并**清零** PUE 报文计数（同 `ts_stats`）。"""
  out = dict(_PUE_STATS)
  for k in _PUE_STATS:
    _PUE_STATS[k] = 0
  return out


@dataclass(frozen=True)
class PueSample:
  """**一栋楼、一个时间口径、一个时刻**的 PUE/能耗统计 —— `st_pue` 的一行。

  PUE 报文没有 `device_id`，而是「伪设备」（`guid` 形如 `00000000-0PUE-…`）按楼宇推
  18 个指标。`time_type` 必须参与子表划分：Real 与 Hour 会在**整点同一秒**到达，
  同一 `(楼宇, ts)` 若不分口径就会互相覆盖。
  """

  building: str             # 空间 `idc` 编码（如 `P001_A009`）
  time_type: int            # 时间口径代号（`TimeType.code`：1 实时 … 6 年）
  pue_guid: str             # 报文级 guid（伪设备标识，落 tag 备查）
  ts: int                   # 采集时刻（毫秒）
  values: dict[str, float]  # {TDengine 列名: 值}


def is_pue_message(raw: dict) -> bool:
  """是否是 **PUE / 能耗统计报文**（而不是常规「一台设备一组测点」）。

  判据：没有 `device_id`、有 `building`、`points` 是数组。常规实时报文一定带 `device_id`，
  两类报文因此可以互斥分流（见 `consumers.RealtimeConsumer._ingest`）。
  """
  if raw.get('device_id'):
    return False
  if not str(raw.get('building') or '').strip():
    return False
  return isinstance(raw.get('points'), list)


def parse_pue_samples(raw: dict) -> list[PueSample]:
  """PUE / 能耗统计报文 → [PueSample, ...]。

  报文形态（实测）：

      {"guid": "00000000-0PUE-...", "status": 1, "building": "P001_A009", "buliding": true,
       "points": [{"guid": "PUE_Real_IT_Value", "standardId": "PUE_Real_IT_Value",
                   "value": 21015.0, "timestamp": 1794274440, "mgr_obj_id": ""}]}

  编码 = `standardId`（缺则 `guid`），格式 `PUE_<时间口径>_<指标后缀>`（见 `pue_catalog`）；
  `timestamp` 是**秒**；`building` 是空间 `idc` 编码；`buliding`（拼写如此）是布尔标志，忽略。

  **绝不抛异常**：认不出的编码 / 坏值只计数后跳过 —— 实时链路里一条脏数据不能让整批报文失败。

  时间戳：只因「过旧」被 `_normalize_ts` 拒时**回退到到达时刻**并计数。年/月统计点的期初
  时间戳会早于 TDengine 的 KEEP（90 天），严格丢弃等于每年/每月只有最后十天才写得进去；
  回退仍能保留「当期统计值」本身。0 / 负数 / 未来值照旧丢弃。
  """
  building = str(raw.get('building') or '').strip()
  if not building:
    _PUE_STATS['no_building'] += 1
    return []
  points = raw.get('points')
  if not isinstance(points, list) or not points:
    return []

  pue_guid = str(raw.get('guid') or '').strip()
  now_ms = int(time.time() * 1000)
  max_age_days = max(1, int(settings.DCIM_TS_MAX_AGE_DAYS or 90))
  oldest = now_ms - max_age_days * _DAY_MS
  grouped: dict[tuple[int, int], dict[str, float]] = {}
  for p in points:
    if not isinstance(p, dict):
      continue
    code = str(p.get('standardId') or p.get('guid') or '').strip()
    resolved = resolve_metric(code)
    if resolved is None:
      _PUE_STATS['unknown_code'] += 1
      continue
    time_type, metric = resolved
    try:
      value = float(p.get('value'))
    except (TypeError, ValueError):
      continue
    norm = _epoch_ms(p.get('timestamp'))
    if norm is None:
      _PUE_STATS['bad_ts'] += 1
      continue
    ts, unit = norm
    if unit == 's':
      _TS_STATS['seconds'] += 1
    if ts <= 0 or ts > now_ms + _DAY_MS:
      _PUE_STATS['bad_ts'] += 1
      continue
    if ts < oldest:
      _PUE_STATS['ts_fallback'] += 1
      ts = now_ms
    grouped.setdefault((time_type.code, ts), {})[metric.column] = value

  return [
    PueSample(building=building, time_type=code, pue_guid=pue_guid, ts=ts, values=values)
    for (code, ts), values in sorted(grouped.items())
  ]


def _json_or_none(value: Any) -> dict | None:
  """告警阈值字段是**嵌套对象**（EmsAlarmThreshold）；null/空串/异常类型一律存 None。"""
  return value if isinstance(value, dict) else None


def _is_delete(raw: dict) -> bool:
  """删除判定（语义待与 DCIM 确认）：显式删除标记，或 op/deleted 字段。"""
  if raw.get('deleted') is True or raw.get('is_deleted') is True:
    return True
  op = str(raw.get('op') or raw.get('operation') or '').lower()
  return op in ('delete', 'deleted', 'remove')


def parse_space(raw: dict) -> ParsedRecord:
  guid = str(raw.get('guid') or '').strip()
  if not guid:
    raise ValueError('空间记录缺少 guid')
  _reject_truncated_guid(guid, '空间')
  return ParsedRecord(
    guid=guid,
    op='delete' if _is_delete(raw) else 'upsert',
    values={
      'tag': raw.get('tag'),
      'path': raw.get('path'),
      'name': raw.get('name'),
      'idc': raw.get('idc'),
      'node_type': raw.get('node_type'),
      'space_type': raw.get('space_type'),
      'parent_guid': raw.get('parent_id') or None,
    },
    payload=raw,
  )


def parse_device(raw: dict) -> ParsedRecord:
  guid = str(raw.get('guid') or '').strip()
  if not guid:
    raise ValueError('设备记录缺少 guid')
  _reject_truncated_guid(guid, '设备')
  return ParsedRecord(
    guid=guid,
    op='delete' if _is_delete(raw) else 'upsert',
    values={
      'name': raw.get('name'),
      'path': raw.get('path'),
      'idc': raw.get('idc'),
      'node_type': raw.get('node_type'),
      'device_type': raw.get('device_type'),
      # device_name = 设备**类型**名称（如「精密空调」），不是设备名称（那是 name）
      'device_name': raw.get('device_name'),
      'extend_position': raw.get('extend_position'),
      'space_guid': raw.get('parent_id') or None,
    },
    payload=raw,
  )


def parse_point(raw: dict) -> ParsedRecord:
  device_guid = str(raw.get('parent_id') or '').strip()
  point_id = raw.get('id')
  # 契约：point.guid = 设备 guid + '.' + 点位 id（缺失时按此规则补齐）
  guid = str(raw.get('guid') or '').strip()
  if not guid and device_guid and point_id is not None:
    guid = f'{device_guid}.{point_id}'
  if not guid:
    raise ValueError('点位记录缺少 guid（且无法由 parent_id + id 推导）')
  _reject_truncated_guid(guid, '点位')
  status_raw = raw.get('status_map')
  return ParsedRecord(
    guid=guid,
    op='delete' if _is_delete(raw) else 'upsert',
    values={
      'point_id': None if point_id is None else str(point_id),
      'name': raw.get('name'),
      'idc': raw.get('idc'),
      'node_type': raw.get('node_type'),
      'point_type': raw.get('point_type'),
      'unit': raw.get('unit'),
      'status_map_raw': status_raw if isinstance(status_raw, str) else None,
      'status_map': parse_status_map(status_raw),
      'device_guid': device_guid or None,
      # 4 个告警阈值（9/29 版新增）：嵌套结构，整体存 JSONB；
      # 展开成 24 列不合适，查询时用 alarm->'...' 取
      'hint_alarm': _json_or_none(raw.get('hint_alarm')),
      'general_alarm': _json_or_none(raw.get('general_alarm')),
      'important_alarm': _json_or_none(raw.get('important_alarm')),
      'urgent_alarm': _json_or_none(raw.get('urgent_alarm')),
    },
    payload=raw,
  )


def parse_alarm(raw: dict) -> ParsedRecord:
  """告警事件（`ems-alarm-data`）。

  **同一 `event_log_id` 会推三条**（触发 1 / 刷新 2 / 恢复 3），而落库是 upsert
  → 三态**自然串成当前态表里的一行**：谁最后推、谁就是最新状态。

  ⚠️ `remove_timestamp` / `remove_value` 在**非恢复态一律给 0**（**哨兵值**）——
  判断「是否已恢复」**只能看 `status == 3`，不能看 `!= 0`**：
  设备真实恢复值本身就可能是 0（同阈值那次 `maxAlarm: 0` 是真上限的坑）。

  字段名用**表头口径**（`space_guid` / `device_guid` / `point_guid`）；
  对方文档 §8 的示例 JSON 里还写着旧名（`building` / `device_id` / `point_id`），
  那是**示例没跟着改**（已与对方确认实推是表头这版）。
  """
  event_log_id = str(raw.get('event_log_id') or '').strip()
  if not event_log_id:
    raise ValueError('告警记录缺少 event_log_id')
  _reject_truncated_guid(event_log_id, '告警')
  status = str(raw.get('status') or '').strip()
  is_recover = status == '3'
  return ParsedRecord(
    # 复用 `guid` 槽位存 `event_log_id`：共用落库路径（repository.apply_batch）的主键列就叫 guid
    guid=event_log_id,
    op='upsert',
    values={
      'space_guid': raw.get('space_guid') or None,
      'status': status or None,
      'alarm_type': raw.get('alarm_type'),
      'device_guid': raw.get('device_guid') or None,
      'device_name': raw.get('device_name'),
      'point_guid': raw.get('point_guid') or None,
      'point_name': raw.get('point_name'),
      'alarm_level': raw.get('alarm_level'),
      # 实推报文键是 **`NormalRange`**（大写，已核 payload）—— 只读小写会让列恒为 NULL
      'normal_range': raw.get('NormalRange') or raw.get('normal_range'),
      'content': raw.get('content'),
      'alarm_time': raw.get('timestamp'),
      'alarm_value': raw.get('value'),
      # 只有恢复态才写；非恢复态写 NULL —— **别把哨兵 0 落进去**
      'remove_time': raw.get('remove_timestamp') if is_recover else None,
      'remove_value': raw.get('remove_value') if is_recover else None,
    },
    payload=raw,
  )


def _int_or_none(value: Any) -> int | None:
  """尽力转 int（对方偶尔把数字发成字符串）；转不了返回 None。"""
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


def _epoch_seconds(value: Any) -> int | None:
  """时间戳归一成**秒级** epoch；未填（0/空）返回 None。

  告警工单的 `responseTime`/`startTime`/`alarmTime` 实测是**秒**（1792479166），
  但同一接口族里实时数据用毫秒 —— 按量级判定（> 1e11 视为毫秒），避免上游改口径后写错列。
  0 是「未填」的哨兵，不落 0（否则算响应时长会得到 1970 年）。
  """
  n = _int_or_none(value)
  if n is None or n <= 0:
    return None
  return n // 1000 if n > 100_000_000_000 else n


def parse_alarm_order(raw: dict) -> ParsedRecord:
  """告警工单（`ems-alarm-order-rz`）—— §10。

  **同一 `orderNo` 会多次推送**（处理中 → 已处理，字段逐步填全），落库是 upsert
  → 自然收敛成「该工单的最新状态」，历史表留每次推送的原文。

  ⚠️ **实推报文比文档全**（2026-10-20 实测）：文档只列了 orderNo/eventLogId/status/
  responder/responseTime/star/…，实际还带 `addrId`/`addrName`（空间）、
  `mgrObjId`/`mgrObjName`（设备）、`mgrObjAttributeId`/`mgrObjAttributeName`（测点）、
  `level`/`content`/`value`/`alarmTime`。前 6 个正是跨表关联与报告需要的，故映射成
  `space_guid`/`device_guid`/`point_guid` 等**与 `dcim_alarms` 同名**的列（同库可直接 JOIN）。
  """
  order_no = str(raw.get('orderNo') or '').strip()
  if not order_no:
    raise ValueError('告警工单缺少 orderNo')
  return ParsedRecord(
    # 复用 `guid` 槽位存 `orderNo`：共用落库路径（repository.apply_batch）的主键列就叫 guid
    guid=order_no,
    op='upsert',
    values={
      'event_log_id': str(raw.get('eventLogId') or '').strip() or None,
      'status': _int_or_none(raw.get('status')),
      'state': _int_or_none(raw.get('state')),
      'alarm_level': _int_or_none(raw.get('level')),
      'space_guid': str(raw.get('addrId') or '').strip() or None,
      'space_name': raw.get('addrName'),
      'device_guid': str(raw.get('mgrObjId') or '').strip() or None,
      'device_name': raw.get('mgrObjName'),
      'point_guid': str(raw.get('mgrObjAttributeId') or '').strip() or None,
      'point_name': raw.get('mgrObjAttributeName'),
      'content': raw.get('content'),
      'alarm_value': raw.get('value'),
      'alarm_time': _epoch_seconds(raw.get('alarmTime')),
      'response_time': _epoch_seconds(raw.get('responseTime')),
      'star': _int_or_none(raw.get('star')),
      'responder': raw.get('responder'),
      'writer': raw.get('writer'),
      'handler': raw.get('handler'),
      'start_time': _epoch_seconds(raw.get('startTime')),
      'end_time': _epoch_seconds(raw.get('endTime')),
      'process': raw.get('process'),
      'process_result': _int_or_none(raw.get('processResult')),
      'processing_operator': raw.get('processingOperator'),
      'processing_operator_name': raw.get('processingOperatorName'),
      'processed_operator': raw.get('processedOperator'),
      'processed_operator_name': raw.get('processedOperatorName'),
      'processed_operator_time': _epoch_seconds(raw.get('processedOperatorTime')),
    },
    payload=raw,
  )


# 解析器与当前态表/历史表名的映射
# 注意：`real`（实时数据）**不在这里** —— 它返回的是「点值列表」而不是单条 ParsedRecord，
# 落库目标也是 TDengine 而不是 PG，所以走独立分支（见 consumers.py 与 realtime.py）。
PARSERS = {
  'space': parse_space,
  'device': parse_device,
  'point': parse_point,
  'alarm': parse_alarm,      # 告警事件（触发/刷新/恢复）—— 进 PG，不进 TDengine
  'alarm_order': parse_alarm_order,   # 告警工单（人工处理）—— 进 PG
}
# (当前态表, 历史表)。历史表为 None = 不记历史。
# 配置类（空间/设备/点位）**不记历史**：上游把"变更推送"做成了**每小时全量重推**，
# 于是历史表记的不是变更而是快照 —— 实测点位历史 430 万行里 77% 与上一条逐字节相同，
# 单栋楼约 7 GB/天，而全仓库没有任何读取方（2026-10-21 移除）。
# 告警类保留：一条告警一行、增长极慢，"何时触发/何时被谁处理"是真会被追问的追溯需求。
TABLES = {
  'space': ('dcim_spaces', None),
  'device': ('dcim_devices', None),
  'point': ('dcim_points', None),
  'alarm': ('dcim_alarms', 'dcim_alarms_history'),
  'alarm_order': ('dcim_alarm_orders', 'dcim_alarm_orders_history'),
}

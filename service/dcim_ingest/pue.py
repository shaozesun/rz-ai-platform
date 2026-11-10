"""PUE / 能耗统计数据的 TDengine 落库与查询 SQL。

**为什么独立超级表 `st_pue`**（不掺进按 `device_type` 分的设备宽表）：

- PUE 报文**没有 `device_id`**，是「伪设备 + 楼宇」推 18 个指标，没有 `device_type`
  可用来选表；硬塞进宽表就得凭空造一个设备类型，语义与查询层都对不上；
- 18 个指标是**固定集合**（见 `pue_catalog`），与设备测点那种「随类型增长」的列集合
  是两种模型，共用一张表会让 `dcim_td_column` 的映射语义失真。

**子表按 (楼宇 × 时间口径) 分**：TDengine 同一 `(子表, ts)` 是整行覆盖，而 Real（约 10 秒
一条）与 Hour（整点快照）**会在整点同一秒到达** —— 不分口径就会互相覆盖丢数据。
子表名沿用 `td_schema.subtable_name` 的归一化/长度规则（`d_<园区代号>_pue_<楼宇>_<口径>`），
园区代号进表名是因为**子表名全库唯一、tag 创建即固定**（同设备宽表那条既有约束）。

⚠️ 指标列叫 `pue` 不叫 `value`：`value` 是 TDengine 保留字（`td_schema._RESERVED`）。
"""

from __future__ import annotations

import logging
import re
import threading
import time

from config.settings import settings
from service.dcim_ingest.models import PueSample
from service.dcim_ingest.pue_catalog import METRIC_COLUMNS
from service.dcim_ingest.realtime import (
  COL_TS, RealtimeRepository, _AGG_FUNCS, _INTERVAL_RE, _num, _q, sql_literal,
)
from service.dcim_ingest.repository import DcimRepository
from service.dcim_ingest.td_schema import subtable_name

logger = logging.getLogger(__name__)

PUE_STABLE = 'st_pue'

# 18 个指标列（固定集合，顺序即建表顺序，见 `pue_catalog.METRIC_COLUMNS`）
PUE_COLUMNS: dict[str, str] = {col: 'DOUBLE' for col in METRIC_COLUMNS}

# `st_pue` 自己的 tag —— **不改 `realtime.TAG_COLUMNS`**：设备宽表的 tag 顺序是全库契约，
# 500 万子表改 tag 是破坏性操作（实测曾把 taosd 打满且不回落）。
PUE_TAGS: tuple[tuple[str, str], ...] = (
  ('cluster_name', 'BINARY(32)'),
  ('building', 'BINARY(64)'),      # 空间 `idc` 编码，如 `P001_A009`
  ('time_type', 'TINYINT'),        # 1 实时 / 2 小时 / 3 日 / 4 周 / 5 月 / 6 年
  ('space_guid', 'BINARY(64)'),    # PG `dcim_spaces.guid`（按 building 反查，可能为空）
  ('pue_guid', 'BINARY(64)'),      # 报文级 guid（伪设备标识）
)

# 建表是幂等的，但**并发 CREATE STABLE 会撞 `Conflict transaction not completed`** ——
# 多消费线程同时首次写入时必须串行化（进程内一把锁；跨进程由 `ensure_realtime_schema` 预先建好）
_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

# building → space_guid 的负缓存 TTL：配置类 topic 可能晚于实时数据到达，
# 永久缓存空值会让该楼宇永远没有 space_guid
_SPACE_MISS_TTL = 300.0


def pue_subtable(cluster_code: str, building: str, time_type: int) -> str:
  """(园区, 楼宇, 时间口径) → 子表名 `d_<园区代号>_pue_<楼宇>_<口径>`。"""
  return subtable_name(f'PUE_{building}_{int(time_type)}', '', cluster_code)


def build_pue_insert(
  *,
  cluster: str,
  cluster_code: str,
  building: str,
  space_guid: str,
  pue_guid: str,
  samples: list[PueSample],
) -> str:
  """一栋楼的 PUE 样本 → 一条 `INSERT INTO`（每个时间口径一个片段，纯函数便于单测）。

  同一栋楼的一批样本可能跨口径（Real 与 Hour 在整点同批到达），所以按口径拆成多个
  `<子表> USING st_pue TAGS(...) (列清单) VALUES(...)` 片段，再拼成一条语句 ——
  **一个 INSERT INTO 后面可以跟多个片段**，各片段可各带自己的列清单（同宽表的合并写法）。

  列清单只列本片段**真有值**的指标列（`ts` 恒在），未列出的列 TDengine 自动填 NULL。
  """
  if not samples:
    raise ValueError('PUE 写入需要至少一条样本')
  by_type: dict[int, list[PueSample]] = {}
  for s in samples:
    by_type.setdefault(int(s.time_type), []).append(s)

  blocks = []
  for time_type in sorted(by_type):
    rows = sorted(by_type[time_type], key=lambda s: s.ts)
    cols = [c for c in METRIC_COLUMNS if any(c in s.values for s in rows)]
    cols = [COL_TS] + cols
    tags = ','.join(_q(t) for t in (
      cluster, building, str(int(time_type)), space_guid, pue_guid,
    ))
    values = []
    for s in rows:
      cells = [str(int(s.ts))] + [_num(s.values.get(c)) for c in cols[1:]]
      values.append('(' + ','.join(cells) + ')')
    subtable = pue_subtable(cluster_code, building, time_type)
    blocks.append(
      f'{subtable} USING {PUE_STABLE} TAGS({tags}) '
      f'({",".join(cols)}) VALUES ' + ' '.join(values)
    )
  return 'INSERT INTO ' + ' '.join(blocks)


def ensure_pue_schema(td: RealtimeRepository | None = None) -> None:
  """建 `st_pue`（幂等）。传 `td` 复用调用方的连接；不传则自建一个短连接。

  在 `ensure_realtime_schema()` 里**起消费线程之前**调一次 —— 避免多进程并发建表。
  """
  own = td is None
  repo = td or RealtimeRepository()
  if own:
    repo.connect()
  try:
    repo.ensure_stable(PUE_STABLE, PUE_COLUMNS, tags=PUE_TAGS)
  finally:
    if own:
      repo.close()


class PueWriter:
  """PUE / 能耗统计的落库器（每个实时消费线程各持一个实例，量小、串行写即可）。

  与 `WideWriter` 的差异：不查设备档案、不做零阶保持、不需要线程池 —— 一批通常只有
  一栋楼的一两行。**异常一律抛出**，由 `RealtimeConsumer._flush` 做整批退避重试。
  """

  def __init__(
    self, pg: DcimRepository, td: RealtimeRepository, *,
    cluster: str, cluster_code: str = '',
  ) -> None:
    self._pg = pg
    self._td = td
    self._cluster = cluster
    self._cluster_code = cluster_code
    self._space: dict[str, str] = {}            # building -> space_guid（命中永久缓存）
    self._space_miss_at: dict[str, float] = {}  # building -> 上次确认缺失的时刻
    self.unknown_buildings = 0                  # 查不到空间档案的楼宇数（观测用）
    self.statements = 0                         # 已下发的 INSERT 语句数（观测用）

  # ── 连接（与 WideWriter 的接口对齐，_RealtimeStore 统一转发） ──
  def connect(self) -> None:
    self._pg.connect()
    self._td.connect()

  def close(self) -> None:
    self._td.close()
    self._pg.close()

  def write(self, samples: list[PueSample]) -> int:
    """写一批 PUE 样本，返回**实际写入的行数**（每行 = 一栋楼一个口径一个时刻）。"""
    if not samples:
      return 0
    self._ensure_schema()

    # 同一 (楼宇, 口径, 时刻) 的多条样本合并成一行（后到者覆盖同名列，与 TDengine 语义一致）
    merged: dict[tuple[str, int, int], dict[str, float]] = {}
    for s in samples:
      merged.setdefault((s.building, s.time_type, s.ts), {}).update(s.values)
    guid_of: dict[str, str] = {}
    for s in samples:
      if s.pue_guid and not guid_of.get(s.building):
        guid_of[s.building] = s.pue_guid

    by_building: dict[str, list[PueSample]] = {}
    for (building, time_type, ts), values in merged.items():
      by_building.setdefault(building, []).append(PueSample(
        building=building, time_type=time_type, pue_guid=guid_of.get(building, ''),
        ts=ts, values=values,
      ))

    rows = 0
    for building, batch in by_building.items():
      space_guid = self._space_guid(building)
      if not space_guid:
        self.unknown_buildings += 1
      self._td.exec_sql(build_pue_insert(
        cluster=self._cluster, cluster_code=self._cluster_code, building=building,
        space_guid=space_guid, pue_guid=guid_of.get(building, ''), samples=batch,
      ))
      self.statements += 1
      rows += len(batch)
    return rows

  def _ensure_schema(self) -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
      return
    with _SCHEMA_LOCK:
      if _SCHEMA_READY:
        return
      self._td.ensure_stable(PUE_STABLE, PUE_COLUMNS, tags=PUE_TAGS)
      _SCHEMA_READY = True
      logger.info('[%s] TDengine 超级表已建：%s（%d 列）',
                  self._cluster, PUE_STABLE, len(PUE_COLUMNS))

  def _space_guid(self, building: str) -> str:
    """楼宇 `idc` 编码 → `dcim_spaces.guid`。命中永久缓存，未命中缓存 `_SPACE_MISS_TTL`。"""
    hit = self._space.get(building)
    if hit is not None:
      return hit
    miss_at = self._space_miss_at.get(building)
    if miss_at is not None and time.time() - miss_at < _SPACE_MISS_TTL:
      return ''
    try:
      rows = self._pg.find_spaces(self._cluster, idc=building, limit=2)
    except Exception:                        # noqa: BLE001 —— 解析不到不阻塞写入，只是少个 tag
      # 连接与 `WideWriter` 共用：失败后必须回滚，否则事务留在 aborted 态会拖垮下一批写入
      try:
        self._pg.rollback()
      except Exception:                      # noqa: BLE001
        pass
      logger.warning('[%s] 楼宇空间解析失败（本次按空 space_guid 写）：%s',
                     self._cluster, building, exc_info=True)
      return ''
    guid = next((str(r['guid']) for r in rows if r.get('guid')), '')
    if guid:
      self._space[building] = guid
      self._space_miss_at.pop(building, None)
    else:
      self._space_miss_at[building] = time.time()
    return guid


# ── 只读侧：查询 SQL 构造（显式列清单，不用 `*`） ──

def _check_columns(columns: list[str] | None) -> list[str]:
  """指标列白名单校验；空 = 全部 18 列。**列名会拼进 SQL**，只认目录里的。"""
  if not columns:
    return list(METRIC_COLUMNS)
  out: list[str] = []
  for col in columns:
    if col not in PUE_COLUMNS:
      raise ValueError(f'未知 PUE 指标列 {col!r}（可选：{"/".join(METRIC_COLUMNS)}）')
    if col not in out:
      out.append(col)
  return out


def _pue_filters(
  *,
  cluster: str | None = None,
  clusters: list[str] | None = None,
  buildings: list[str] | None = None,
  space_guid: str | None = None,
  time_types: list[int] | None = None,
  start_ms: int | None = None,
  end_ms: int | None = None,
  require_window: bool = False,
) -> str:
  """PUE 查询的 WHERE 子句（原始值 / 计数 / 聚合**共用**，否则 `total` 与数据对不上）。

  `clusters` 是「按园区名查」展开出来的一组代号（`cluster_name IN (...)`）；
  与 `cluster` 二选一，同时给以 `cluster` 为准（与调用层 `_scope` 的口径一致）。
  """
  clauses: list[str] = []
  if start_ms and end_ms:
    clauses.append(f'ts >= {int(start_ms)} AND ts <= {int(end_ms)}')
  elif require_window:
    raise ValueError(
      'PUE 时序查询必须带时间窗：请提供 start_ms / end_ms（直接传日期即可）'
    )
  if cluster:
    clauses.append(f'cluster_name = {sql_literal(cluster)}')
  elif clusters:
    clauses.append('cluster_name IN (' + ','.join(sql_literal(c) for c in clusters) + ')')
  if space_guid:
    clauses.append(f'space_guid = {sql_literal(space_guid)}')
  if buildings:
    clauses.append('building IN (' + ','.join(sql_literal(b) for b in buildings) + ')')
  if time_types:
    clauses.append('time_type IN (' + ','.join(str(int(t)) for t in time_types) + ')')
  return ' AND '.join(clauses)


def _keys(cross_park: bool) -> str:
  """分组/排序键：楼宇身份只在园区内唯一 → 跨园区必须带 `cluster_name`。"""
  return 'cluster_name, building, time_type' if cross_park else 'building, time_type'


def build_pue_last_row(
  *,
  columns: list[str] | None = None,
  cluster: str | None = None,
  clusters: list[str] | None = None,
  buildings: list[str] | None = None,
  space_guid: str | None = None,
  time_types: list[int] | None = None,
) -> str:
  """每个「楼宇 × 口径」的最新一行（`last_row(...)` + 按 tag 分组）。

  与设备宽表同一口径：**必须分组**（超级表上单用 `LAST_ROW()` 只返回整体一行），
  跨园区还要连 `cluster_name` 一起分组。
  """
  cols = _check_columns(columns)
  exprs = ', '.join(f'last_row({c})' for c in [COL_TS] + cols)
  keys = _keys(not cluster)
  where = _pue_filters(
    cluster=cluster, clusters=clusters, buildings=buildings, space_guid=space_guid,
    time_types=time_types,
  )
  sql = f'SELECT {keys}, {exprs} FROM {PUE_STABLE}'
  if where:
    sql += ' WHERE ' + where
  return f'{sql} GROUP BY {keys}'


def unwrap_last_row(records: list[dict]) -> list[dict]:
  """去掉 `last_row(x)` 的包装，还原成 `{列名: 值}`（纯函数）。"""
  out: list[dict] = []
  for rec in records:
    out.append({
      k[len('last_row('):-1] if k.startswith('last_row(') and k.endswith(')') else k: v
      for k, v in rec.items()
    })
  return out


def build_pue_series(
  *,
  columns: list[str] | None = None,
  cluster: str | None = None,
  clusters: list[str] | None = None,
  buildings: list[str] | None = None,
  space_guid: str | None = None,
  time_types: list[int] | None = None,
  start_ms: int | None = None,
  end_ms: int | None = None,
  limit: int = 5000,
  offset: int = 0,
) -> str:
  """原始值查询（分页）。排序键必须含全部分组键，否则翻页会漂行。"""
  cols = _check_columns(columns)
  keys = _keys(not cluster)
  where = _pue_filters(
    cluster=cluster, clusters=clusters, buildings=buildings, space_guid=space_guid,
    time_types=time_types, start_ms=start_ms, end_ms=end_ms, require_window=True,
  )
  return (f'SELECT {keys}, {COL_TS}, {",".join(cols)} FROM {PUE_STABLE}'
          f' WHERE {where} ORDER BY {COL_TS}, {keys}'
          f' LIMIT {int(limit)} OFFSET {int(offset)}')


def build_pue_agg(
  *,
  columns: list[str] | None = None,
  interval: str,
  agg: str = 'avg',
  cluster: str | None = None,
  clusters: list[str] | None = None,
  buildings: list[str] | None = None,
  space_guid: str | None = None,
  time_types: list[int] | None = None,
  start_ms: int | None = None,
  end_ms: int | None = None,
  limit: int = 5000,
  offset: int = 0,
) -> str:
  """按时间窗聚合（结果 =「每楼宇 × 每口径 × 每窗口」一行，时间列是 `_wstart`）。

  聚合列用 `AS <列名>` 回原名：否则模型看到的是 `avg(pue)` 这种表达式名，无法对应指标。
  """
  if agg not in _AGG_FUNCS:
    raise ValueError(f'不支持的聚合函数 {agg!r}（可选：{"/".join(_AGG_FUNCS)}）')
  if not _INTERVAL_RE.match(str(interval)):
    raise ValueError(f'时间窗写法不对：{interval!r}（形如 10m / 1h / 1d）')
  cols = _check_columns(columns)
  keys = _keys(not cluster)
  where = _pue_filters(
    cluster=cluster, clusters=clusters, buildings=buildings, space_guid=space_guid,
    time_types=time_types, start_ms=start_ms, end_ms=end_ms, require_window=True,
  )
  exprs = ', '.join(f'{agg}({c}) AS {c}' for c in cols)
  return (f'SELECT _wstart, {keys}, count(*) AS n, {exprs} FROM {PUE_STABLE}'
          f' WHERE {where} PARTITION BY {keys} INTERVAL({interval})'
          f' ORDER BY _wstart, {keys} LIMIT {int(limit)} OFFSET {int(offset)}')


def build_pue_count(
  *,
  cluster: str | None = None,
  clusters: list[str] | None = None,
  buildings: list[str] | None = None,
  space_guid: str | None = None,
  time_types: list[int] | None = None,
  start_ms: int | None = None,
  end_ms: int | None = None,
) -> str:
  """与 `build_pue_series` **同一套 WHERE** 的总行数（上层据此判断翻页结束）。"""
  where = _pue_filters(
    cluster=cluster, clusters=clusters, buildings=buildings, space_guid=space_guid,
    time_types=time_types, start_ms=start_ms, end_ms=end_ms, require_window=True,
  )
  return f'SELECT count(*) AS n FROM {PUE_STABLE} WHERE {where}'

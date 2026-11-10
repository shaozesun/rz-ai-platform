"""DCIM 能力调用器 —— 把「跨库两段式查询」封装成一次性调用。

**为什么是两段式**（PG 定范围 → TDengine 取数 → 应用层合并）：

  - PG 没有连 TDengine 的 FDW，**跨库 JOIN 做不到**（这是硬约束）

  但跨库关联其实不需要 JOIN：TDengine 的超级表 tag 里冗余了
  `cluster_name` / `device_guid` / `space_guid` / `device_type`
  （见 `service/dcim_ingest/realtime.py` 的 `TAG_COLUMNS`），
  所以「某机房/某类型设备的曲线」可以**直接打在 TDengine 上**。
  真正必须先去 PG 的只有「按中文名/空间名筛」这类。

**同步 IO 一律走 `asyncio.to_thread`**：psycopg 与 httpx(同步) 都是阻塞的，
直接 await 会把事件循环卡住 —— 而这条链路跑在 SSE 对话里，卡住就是"模型不回应"。
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

try:                                    # 缺 tzdata（精简镜像）时回退固定 UTC+8
  from zoneinfo import ZoneInfo
except ImportError:                     # pragma: no cover
  ZoneInfo = None                # type: ignore[assignment]

from config.settings import settings
from service.dcim_ingest.pue import (
  build_pue_agg, build_pue_count, build_pue_last_row, build_pue_series, unwrap_last_row,
)
from service.dcim_ingest.pue_catalog import (
  COLUMN_LABELS, resolve_column, resolve_time_type, time_type_label,
)
from service.dcim_ingest.realtime import (
  RealtimeRepository, build_series_count, build_series_query, interval_to_ms, sql_literal,
)
from service.dcim_ingest.repository import DcimRepository
from service.dcim_ingest.td_schema import stable_name
from service.query.dcim_meta.capabilities import PARK_NAMES, cluster_codes, park_of

logger = logging.getLogger(__name__)


class DcimMetaError(Exception):
  """能力层可预期的错误（参数缺失/查不到），会被工具层转成给模型看的提示。"""


def _require(params: dict, *names: str) -> list:
  """取必填参数并做非空校验 —— 报错信息要**能指导模型怎么补**，不是只报缺字段。"""
  out = []
  for n in names:
    v = params.get(n)
    if v is None or (isinstance(v, str) and not v.strip()):
      raise DcimMetaError(f'缺少必填参数 `{n}`')
    out.append(v)
  return out


_NO_CLUSTER = ('', '*', 'all', '全部', '所有', '不限', 'any')


def _cluster(params: dict) -> str:
  """园区代号 —— **可选**。返回空串表示「跨所有园区」。

  ⚠️ 为什么从"必填"改成可选（2026-10-13 实测）：
  模型**无从知道哪个园区有数据**，只能拿能力描述里的代号清单一个个试；
  而它有"一批并行发几条"的自我限制 → **试到一半就停**，数据在后面的园区会被漏掉。
  实测：描述里给了 11 个代号，模型只扫了前 6 个（`廊坊A…佛山`），
  恰好有数据的 `廊坊B1M2` 排第 3 才蒙对 —— 若排在第 7 位就是**报"没有数据"**。

  不传园区时记录**自带 `cluster` 列**，模型一眼看得出数据在哪个园区，
  一次调用就够，不需要"猜 + 扫"。
  """
  raw = str(params.get('cluster') or '').strip()
  return '' if raw.lower() in _NO_CLUSTER else raw


def _scope(params: dict) -> str | list[str] | None:
  """`cluster` / `park` → 仓储层的园区过滤值（单个代号 / 代号列表 / None=跨园区）。

  `cluster` 优先（精确代号）；没传就按 `park` 展开该园区下**全部代号**（SQL 走 `= ANY`），
  这样「某园区」一次查询、`summary` 的 P50/P90 才是整园的。

  ⚠️ 未知园区**报错**，绝不退化成「不过滤」——否则「某园区没有超时单」会变成
  「全平台单子」，比报错更坏（同 `_timeout_minutes` 的理由）。
  """
  cluster = _cluster(params)
  if cluster:
    return cluster
  park = str(params.get('park') or '').strip()
  if not park:
    return None
  codes = [c for c in cluster_codes() if c.startswith(park)]
  if not codes:
    raise DcimMetaError(f'未知园区 park={park}（可选：{"、".join(PARK_NAMES)}）')
  return codes


def _paged(records: list, total: int) -> dict:
  """统一返回形状：`records` + `total`。

  `total` **必须给**：`analysis_load` 有它就用 `len(records) >= total` 判断翻页结束；
  没有它只能靠"这一页不满"判断 —— 每页都满额时会**反复拉同一批直到上限**。
  """
  return {'records': records, 'total': int(total)}


# 分页上限（防呆）：**直接调工具**时 `tools_factory` 会把 page_size 钳到
# `AGENT_MAX_PAGE_SIZE`(20)；而 **`analysis_load` 是内部直调 caller**，会传
# `AGENT_ANALYSIS_PAGE_SIZE`(500)，不受那个钳制。所以这里**原样接受**上层给的 page_size，
# 只做一个防呆上限，别自作主张按 20 再钳一次（那样 analysis_load 要发 500 次请求）。
_MAX_PAGE_SIZE = 5000

# 定向查点位（给了 keyword）时可以附带 `device_guid`/`device_type` 的命中条数上限。
# 这两个字段每条要多吃 ~145 字符，而工具输出上限 12,000 —— 54 测点的机房查询带上就会顶爆，
# 所以只对"按名字精选"的小结果集附加（那正是"接着查这个点位的曲线"的场景）。
_DEVICE_FIELDS_MAX_ROWS = 30

# 跨类型时序查询的上限。一次查询跨 N 个设备类型 = N 条 SQL（超级表不能合并），
# 所以要有护栏。**按类型数封顶，而不是按总行数** —— 真实机房一个房间十几类设备很正常
# （温湿度/配电/UPS/空调/消防/门禁/漏水…），8 会天天撞上；而 32 次往返在实测的 0.68ms 下
# 才 20ms，根本不是瓶颈。
_MAX_CROSS_TYPES = 32
# 跨类型时**每类型**的取数上限（全局排序才能正确翻页，所以每类都要多取一些）。
# 与 _MAX_CROSS_TYPES 相乘构成内存上界（32 × 20000 = 64 万行），且只有深翻页才够得着。
_CROSS_FETCH_CAP = 20000


def _page(params: dict, *, default_size: int) -> tuple[int, int]:
  """从 params 取 `page`/`page_size` → `(limit, offset)`。

  工具 schema 是**全可选 str**（`tools_factory._build_args_schema` 的既有约定），
  所以这里要转 int 并对非法值容错。
  """
  try:
    page = max(1, int(str(params.get('page') or 1)))
  except (TypeError, ValueError):
    page = 1
  try:
    size = int(str(params.get('page_size') or default_size))
  except (TypeError, ValueError):
    size = default_size
  size = max(1, min(size, _MAX_PAGE_SIZE))
  return size, (page - 1) * size


# 展示时区：DCIM 出参**一律渲染成北京时间**。
# TDengine REST 的 `ts` 是 UTC ISO（`...Z`），PG 告警时间是 10 位 epoch 秒 ——
# 之前两者都原样给模型，模型按本地读就正好早 8 小时（2026-10-17 实测）。
try:
  _DISPLAY_TZ = ZoneInfo('Asia/Shanghai') if ZoneInfo else timezone(timedelta(hours=8))
except Exception:                       # noqa: BLE001 —— 缺 tzdata 时回退固定 UTC+8
  _DISPLAY_TZ = timezone(timedelta(hours=8))

_TIME_FMT = '%Y-%m-%d %H:%M:%S'


def _local_time(value):
  """时间 → `YYYY-MM-DD HH:MM:SS`（Asia/Shanghai）；认不出就原样返回。

  接受：`datetime`、epoch 秒/毫秒（10 位 / 13 位）、ISO8601（带 `Z` 或偏移）。
  约定：**naive 一律按 UTC**（TDengine 口径）；`0` 是对方未恢复时的哨兵 → `None`。
  """
  if value is None or value == '':
    return value
  try:
    if isinstance(value, datetime):
      dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
      return dt.astimezone(_DISPLAY_TZ).strftime(_TIME_FMT)
    if isinstance(value, bool):         # bool 是 int 子类，别当时间
      return value
    if isinstance(value, (int, float)) or (
      isinstance(value, str) and value.strip().lstrip('-').isdigit()
    ):
      num = float(value)
      if num <= 0:                      # 1970 哨兵
        return None
      seconds = num / 1000.0 if num >= 1e12 else num
      return datetime.fromtimestamp(seconds, _DISPLAY_TZ).strftime(_TIME_FMT)
    if isinstance(value, str):
      dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
      if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
      return dt.astimezone(_DISPLAY_TZ).strftime(_TIME_FMT)
  except (TypeError, ValueError, OverflowError, OSError):
    return value                        # 一条脏数据不能炸整批
  return value


def _epoch_seconds(value):
  """告警入参时间 → **epoch 秒**（列是秒级；模型常给毫秒或 ISO）。

  与 `_local_time` 的区别：这里 naive ISO 按**本地墙钟**解释（用户输入语义），
  而不是 UTC —— 用户说「10-17 0 点」指的是北京时间。
  """
  if value is None or value == '':
    return None
  try:
    if isinstance(value, datetime):
      dt = value if value.tzinfo else value.replace(tzinfo=_DISPLAY_TZ)
      return int(dt.timestamp())
    if isinstance(value, bool):
      return None
    if isinstance(value, (int, float)) or (
      isinstance(value, str) and value.strip().lstrip('-').isdigit()
    ):
      num = float(value)
      if num <= 0:
        return None
      return int(num / 1000.0) if num >= 1e12 else int(num)
    if isinstance(value, str):
      dt = datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
      if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_DISPLAY_TZ)
      return int(dt.timestamp())
  except (TypeError, ValueError, OverflowError, OSError):
    return None
  return None


_BARE_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _epoch_seconds_end(value):
  """结束时间：**裸日期**（`2026-10-20`）按「含当天整天」处理。

  模型说「10 月 20 日」只会传日期，而 `_epoch_seconds` 把它解析成当天 00:00，
  配 SQL 的 `<=` 等于把当天数据**全部排除**（查回 0 行后模型会报「没有超时单」）。
  带时分的值（`2026-10-20 12:00`）按用户原意，不再补。
  """
  sec = _epoch_seconds(value)
  if sec is None:
    return None
  if isinstance(value, str) and _BARE_DATE_RE.match(value.strip()):
    return sec + 86399
  return sec


def _epoch_ms(value):
  """时间入参 → **epoch 毫秒**（TDengine 时间列）。

  与 `_epoch_seconds` 同一套解析：naive ISO 按**北京墙钟**（`_DISPLAY_TZ`），
  接受毫秒 / 秒 / `2026-10-19` / `2026-10-19 00:00:00` 等 ISO 写法。

  ⚠️ 为什么要有它：原先 `query_series` 只收毫秒戳，逼模型自己算 epoch ——
  实测它把「2026-10-19」算成了 2025 的毫秒值（差一年），查回 0 行后误判「接口没数据」。
  """
  sec = _epoch_seconds(value)
  return None if sec is None else int(sec) * 1000


# PG 连接池（进程级单例，懒建）。
#
# 为什么需要：`_Backend` 每次调用都建一个 PG 连接 —— `psycopg.connect()` 要走 TCP 握手
# + 认证（本机几毫秒、跨机几十毫秒）。一次对话几次查询看不出来，但**查询量上来时
# 建连接就是第一瓶颈**（`psycopg-pool` 本来就在依赖里，此前没用上）。
#
# 池大小取 4：`client.call` 走 `asyncio.to_thread`（默认线程池 32 个），
# 但 DCIM 查询是「一次对话几次」的量级，4 条足够；且**单机 PG 连接数要省着用**。
_PG_POOL: Any = None
_PG_POOL_LOCK = threading.Lock()


def _pg_pool() -> Any:
  global _PG_POOL
  if _PG_POOL is None:
    with _PG_POOL_LOCK:
      if _PG_POOL is None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        # kwargs 会透传给 psycopg.connect —— 行工厂必须在这里配好，
        # 否则池里的连接拿到的是元组行，`row['name']` 全炸
        _PG_POOL = ConnectionPool(
          settings.DCIM_PG_DSN, min_size=1, max_size=4,
          kwargs={'row_factory': dict_row}, open=True,
        )
  return _PG_POOL


# TDengine 客户端（进程级单例，懒建）。
#
# 为什么复用：`_attach_latest` / 跨类型查询会**一个 device_type 打一次**，
# 每次新建 client 等于每次重做 TCP 握手；而 `stable_columns()` 的 DESCRIBE 缓存
# 也随实例走 —— 不复用就等于每次调用重查一遍列。`httpx.Client` 自带连接池、线程安全。
_TD_REPO: Any = None
_TD_LOCK = threading.Lock()


def _td_repo() -> Any:
  global _TD_REPO
  if _TD_REPO is None:
    with _TD_LOCK:
      if _TD_REPO is None:
        repo = RealtimeRepository()
        repo.connect()
        _TD_REPO = repo
  return _TD_REPO


class _Backend:
  """一次调用内的两条连接：PG 从**池**里借（用完归还），TDengine 复用进程级单例。

  ⚠️ TD 单例**不能在这里 close** —— 关了下次调用就没 client 了。它靠 `_columns_cache`
  的 TTL 感知超级表加列（见 `RealtimeRepository.stable_columns`）。
  """

  def __init__(self) -> None:
    self.pg = DcimRepository(settings.DCIM_PG_DSN, pool=_pg_pool())
    self.td = _td_repo()

  def __enter__(self) -> '_Backend':
    self.pg.connect()
    return self

  def __exit__(self, *exc) -> None:
    self.pg.close()


def _resolve_space_guid(
  b: '_Backend', cluster: str | list[str] | None, space: str,
) -> dict:
  """空间名 → 唯一空间行（`cluster` / `name` / `guid`）。**多义或不命中一律报错，绝不猜。**

  为什么必须报错而不是挑一个（同 `_scope` 对未知园区的口径）：猜错是**静默换范围** ——
  「A1」挑成 A10 后，用户拿到的条数看着合理、其实不是他要的那栋楼，比报错难发现得多。

  不给园区时跨全库解析：同一短代号（A1）在廊坊A 与重庆都有，这时要回候选让调用方补
  `cluster` / `park`，而不是随便落一个园区。
  """
  hits = b.pg.find_space_roots(cluster, space)
  if not hits:
    raise DcimMetaError(
      f'没有匹配空间名「{space}」的空间，请用 query_space_tree 确认名称（园区 / 楼宇 / 房间）'
    )
  if len(hits) > 1:
    shown = '；'.join(
      f"{h.get('cluster')}/{h.get('name')}（guid={h.get('guid')}）" for h in hits[:10]
    )
    more = f' 等 {len(hits)} 个' if len(hits) > 10 else ''
    raise DcimMetaError(
      f'空间名「{space}」匹配到多个空间：{shown}{more}。'
      '请补 cluster（或 park）限定园区，或加词收窄（如「A1 101机房」）；'
      '已知具体 guid 时可直接传 space_guid'
    )
  return hits[0]


def _echo_space(row: dict | None) -> dict | None:
  """解析结果回带 —— 让模型能说清范围是「廊坊A / 廊坊园区A1数据中心」，不用猜。"""
  if not row:
    return None
  return {'cluster': row.get('cluster'), 'name': row.get('name'), 'guid': row.get('guid')}


# ── 各能力的同步实现（在线程里跑） ──

def _query_space_tree(params: dict) -> dict:
  cluster = _cluster(params)
  keyword = str(params['keyword']) if params.get('keyword') else None
  idc = str(params['idc']) if params.get('idc') else None
  top_only = not keyword
  limit, offset = _page(params, default_size=200)
  with _Backend() as b:
    rows = b.pg.find_spaces(
      cluster, keyword=keyword, idc=idc, top_only=top_only,
      limit=limit, offset=offset,
    )
    # total 与 find **同一套条件**（repository 里共用 `_space_filters`），否则翻页会漏
    total = b.pg.count_spaces(cluster, keyword=keyword, idc=idc, top_only=top_only)
  return _paged(rows, total)


def _query_devices(params: dict) -> dict:
  cluster = _cluster(params)
  space_guid = str(params['space_guid']) if params.get('space_guid') else None
  device_type = str(params['device_type']) if params.get('device_type') else None
  keyword = str(params['keyword']) if params.get('keyword') else None
  limit, offset = _page(params, default_size=200)
  with _Backend() as b:
    rows = b.pg.find_devices(
      cluster, space_guid=space_guid, device_type=device_type, keyword=keyword,
      limit=limit, offset=offset,
    )
    total = b.pg.count_devices(
      cluster, space_guid=space_guid, device_type=device_type, keyword=keyword,
    )
  return _paged(rows, total)


def _truthy(raw) -> bool:
  """工具 schema 是全可选 str，布尔参数进来是字符串。"""
  return str(raw or '').strip().lower() in ('1', 'true', 'yes', 'y', '是')


def _latest_values(
  b: '_Backend', cluster: str, rows: list[dict],
) -> tuple[dict[str, dict], dict[str, dict[str, str]]]:
  """测点定义 → `(每设备最新一行, 每类型的 point_code→td_column)` —— 跨库两段式的第二段。

  第一段（PG）已经一次 JOIN 出了「空间 → 设备 → 测点定义（含单位/阈值）」，
  这里拿其中的 `(cluster, device_guid)` 去 TDengine 取**每台设备最新一行**
  （宽表下一行 = 各测点最新值），再按 `(device_guid, point_code → td_column)` 对齐 ——
  `dcim_td_column` 这张映射表就是为此存在的。

  ⚠️ **按 `(cluster, guid)` 取**：guid 只在园区内唯一，跨园区查询时两个园区可能有
  同一个 guid。`last_rows` 的键就是设备对，这里原样保留。

  ⚠️ **按 device_type 分组查**：宽表按设备类型分超级表，一种类型一条 SQL
  （是按**类型**条数、不是按设备条数，通常 1~3 条）。
  取值失败**不影响定义**（定义是主，值是补），只记日志。
  """
  by_type: dict[str, list[str]] = {}
  for r in rows:
    dt = str(r.get('device_type') or '')
    guid = str(r.get('device_guid') or '')
    if not dt or not guid:
      continue
    guids = by_type.setdefault(dt, [])
    if guid not in guids:
      guids.append(guid)

  latest: dict[tuple[str, str], dict] = {}   # (cluster, device_guid) -> {列名: 值}
  col_of: dict[str, dict[str, str]] = {}     # device_type -> {point_code: td_column}
  for dt, guids in by_type.items():
    try:
      by_group = b.pg.td_columns_by_group(dt)     # {point_group: {point_code: td_column}}
    except Exception as e:                        # noqa: BLE001
      logger.warning('取列映射失败（device_type=%s）：%s', dt, e)
      continue
    # 列名在同一 device_type 内**跨组唯一**（`UNIQUE(device_type, td_column)`，迁移时特意保留），
    # 所以各组的最新值可以直接并进同一份 `{列名: 值}`，`_point_value` 不用关心组。
    col_of[dt] = {code: col for mapping in by_group.values() for code, col in mapping.items()}
    for point_group in sorted(by_group) or ['g0']:
      try:
        one = b.td.last_rows(stable_name(dt, point_group), cluster=cluster, device_guids=guids)
      except Exception as e:              # noqa: BLE001 —— 取值失败不能把定义也丢了
        logger.warning('取最新值失败（device_type=%s 组 %s）：%s', dt, point_group, e)
        continue
      for key, vals in one.items():       # 不能 `latest.update(one)`：那会整份盖掉前一组的列
        latest.setdefault(key, {}).update(vals)
  return latest, col_of


def _point_value(r: dict, latest: dict, col_of: dict) -> tuple[Any, Any]:
  """一条测点定义 → `(最新值, 最新时间)`。列映射对不上（该类型还没进时序库）时给 (None, None)。"""
  col = col_of.get(str(r.get('device_type') or ''), {}).get(str(r.get('point_id') or ''))
  key = (str(r.get('cluster') or ''), str(r.get('device_guid') or ''))
  vals = latest.get(key) or {}
  return (vals.get(col) if col else None), vals.get('ts')


# 4 档告警的字段名（与 `_LEVELS` 同序：从重到轻，但这里只要键名）
_ALARM_COLUMNS = (
  ('hint', 'hint_alarm'), ('general', 'general_alarm'),
  ('important', 'important_alarm'), ('urgent', 'urgent_alarm'),
)


def _alarms(r: dict) -> dict:
  """4 个阈值对象 → **紧凑的** `{档位: {min, max}}`，**只列配置了的档位**。

  为什么会胖：原来 4 个对象各 6 个键、整体塞进每条记录 —— 实测 54 个测点的记录里
  阈值占 **21,588 字符（51%）**，而同类型设备的阈值本来**完全一样**（54 份重复拷贝）。
  而其中 `maxUse`/`minUse` 只是**开关**、`maxEvent`/`minEvent` 是**告警编码**
  （如 `HJ00200004`）不是界限值 —— 都不是"多少算高"的答案。所以只留 `min`/`max`。

  ⚠️ **判据必须用 `maxUse`/`minUse`**，不能看 `maxAlarm` 是否为 0（与 `_breach` 同一套语义，
  生产数据 432 条档位记录零反例）：未配置的档位是 `maxUse:0,minUse:0,maxAlarm:0,minAlarm:0`，
  而通断类测点 `maxUse:1,maxAlarm:0` 的 **0 是真上限**。
  """
  out: dict[str, dict[str, Any]] = {}
  for level, col in _ALARM_COLUMNS:
    a = r.get(col)
    if not isinstance(a, dict):
      continue
    lo = a.get('minAlarm') if _enabled(a.get('minUse')) else None
    hi = a.get('maxAlarm') if _enabled(a.get('maxUse')) else None
    if lo is None and hi is None:
      continue                      # 这一档没配 → 整档不出现
    # 用 `[下限, 上限]` 而不是 `{"min":..,"max":..}`：省 21 字符/条 —— 而 54 条正好卡在
    # 12,000 的边上（用对象写法 12,624，超了 5%，模型就会少拿几条）。顺序在能力描述里写明。
    out[level] = [lo, hi]
  return out


def _slim_point(
  r: dict, *, with_latest: bool, latest_value: Any = None, latest_ts: Any = None,
  include_device: bool = False, include_cluster: bool = False,
) -> dict:
  """把一条测点定义压成模型要的**瘦形状**（实测 788 → ~155 字符）。

  为什么必须瘦：工具输出上限是 `AGENT_MAX_TOOL_OUTPUT_CHARS`(12,000)，而 54 个测点的原形状是
  **42,570 字符（超 3.5 倍）** → 工具层会从末尾弹记录，**模型只拿到 15/54 条**
  （实测：一个机房问"最新数据"，模型只能列出 5 行、还说不对条数）。
  而**有用的"当前值"只占 3%**，51% 是上面说的阈值重复拷贝。

  丢弃规则（每条都有依据）：

    - `guid`（3,024 字符）—— = `device_guid + '.' + point_id`，**可推导**；内部对齐用的是 `point_id`
    - `cluster`（1,026）—— 调用方传进来的入参，恒定；**跨园区查询时不恒定**（`include_cluster=True`
      才保留，否则同名设备分不清是哪个园区的）
    - `idc` —— **非空才带**（实测全空，不给"无脑丢"：将来有值也不丢）
    - `status_map` —— **非空才带**（空映射 `{"0":"","1":""}` 零信息）
    - 阈值 → `_alarms()` 的紧凑形状

  **按用途分两种形状**（实测驱动，不是设计洁癖）：

    - `with_latest=True`（**看现状**）→ **不带** `device_guid` / `device_type`。
      这两个字段一个 65、一个 80 字符，而且**同一台设备的每个测点都重复一遍**
      （54 条里要占 4,300+ 字符 = 24%）—— 而"看现状"根本不需要它们。
      要接着看历史时，模型调一次 `query_devices`（每设备一行、不重复）就能拿到这两个参数。
    - `with_latest=False`（**查定义**）→ 保留，因为那正是"接着拿它去查数据"的场景，
      而且通常条数少（按 keyword 精选），装得下。
    - `include_device=True`（**定向查点位**：给了 keyword 且命中不多）→ 即使 with_latest
      也带上。这条链路下一步就是 `query_series`（要 `device_guid` + `device_type`），
      不带的话模型会把 `idc` 当 guid 用（2026-10-22 实测：查「1-16 电流」近 5 分钟曲线
      第一枪就传了 `P001_A001`，0 行，然后开始瞎猜设备名）。
  """
  out: dict[str, Any] = {
    'device_name': r.get('device_name'),
    'point_id': r.get('point_id'),
    'name': r.get('name'),
    'unit': r.get('unit'),
    'point_type': r.get('point_type'),
  }
  if not with_latest or include_device:
    out['device_guid'] = r.get('device_guid')
    out['device_type'] = r.get('device_type')
  if include_cluster:
    out['cluster'] = r.get('cluster')
  if r.get('idc'):
    out['idc'] = r['idc']
  sm = r.get('status_map')
  if isinstance(sm, dict) and any(str(v).strip() for v in sm.values()):
    out['status_map'] = sm            # 只有真有映射（如 {"0":"正常","1":"中断"}）才带
  alarms = _alarms(r)
  if alarms:
    out['alarms'] = alarms
  if with_latest:
    # 数值**四舍五入到 4 位小数**：TDengine 回来的是 DOUBLE，而上游本是 float32，
    # 于是常见 `37.20000076293945` 这种二进制噪声 —— 17 个字符里有 13 个是垃圾。
    # 既省字符（54 条省 ~700），又让结果可读（模型原本也得自己round成 37.2 才敢写给你）。
    # 非数值（状态码/文本）原样保留。
    try:
      out['latest_value'] = round(float(latest_value), 4)
    except (TypeError, ValueError):
      out['latest_value'] = latest_value
    out['latest_ts'] = _local_time(latest_ts)
  return out


def _query_points(params: dict) -> dict:
  cluster = _cluster(params)
  space_guid = str(params['space_guid']) if params.get('space_guid') else None
  device_guid = str(params['device_guid']) if params.get('device_guid') else None
  keyword = str(params['keyword']) if params.get('keyword') else None
  limit, offset = _page(params, default_size=300)
  with_latest = _truthy(params.get('with_latest'))
  with _Backend() as b:
    # 传 space_guid 时仓库侧走**递归子空间 + 一次 JOIN**（含包间/机柜里的设备）
    rows = b.pg.find_points(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      limit=limit, offset=offset,
    )
    total = b.pg.count_points(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
    )
    # 只在要最新值时才去 TDengine 取（否则白跑几趟往返）
    latest, col_of = _latest_values(b, cluster, rows) if (rows and with_latest) else ({}, {})
  # 定向查点位（按 keyword，且命中不多）时带 device_guid/device_type —— 下一步查曲线要用，
  # 少了它模型只能瞎猜（见 _slim_point）。命中多时不带，否则 54 测点会把输出顶到上限。
  include_device = bool(keyword) and total <= _DEVICE_FIELDS_MAX_ROWS
  include_cluster = not cluster          # 跨园区时 guid 不唯一，记录必须带园区
  records = []
  for r in rows:
    v, ts = _point_value(r, latest, col_of) if with_latest else (None, None)
    records.append(_slim_point(r, with_latest=with_latest, latest_value=v, latest_ts=ts,
                               include_device=include_device, include_cluster=include_cluster))
  return _paged(records, total)


def _as_list(raw) -> list[str] | None:
  """逗号分隔字符串 / 数组 → 去空列表（`device_guids`、`point_ids` 共用）。

  工具 schema 是**全可选 str**（`tools_factory._build_args_schema` 的既有约定），
  所以模型传进来的会是「逗号分隔的字符串」，不会是数组 —— 两种都收。
  """
  if not raw:
    return None
  if isinstance(raw, list):
    out = [str(g).strip() for g in raw]
  else:
    out = [g.strip() for g in str(raw).split(',')]
  return [g for g in out if g] or None


def _match_point_columns(
  mapping: dict[str, str], point_ids: list[str],
) -> tuple[list[str], list[str]]:
  """`{td_column: point_code}` + 请求的 point_id → `(要选的列, 未注册的 point_id)`。

  大小写不敏感：PG 里 point_id 是 `PDU_1I01L`，TDengine 列名是小写 `pdu_1i01l`。
  """
  lookup: dict[str, str] = {}
  for col, code in mapping.items():
    lookup[str(code)] = col
    lookup[str(code).lower()] = col
  cols: list[str] = []
  missing: list[str] = []
  for pid in point_ids:
    col = lookup.get(pid) or lookup.get(pid.lower())
    if col is None:
      missing.append(pid)
    elif col not in cols:
      cols.append(col)
  return cols, missing


def _unknown_guids(guids: list[str], meta: dict[str, dict]) -> list[str]:
  """请求里 PG 查不到的设备 guid（纯函数，便于离线断言）。"""
  return [g for g in guids if g not in meta]


def _validate_guids(b: '_Backend', guids: list[str], cluster: str = '') -> None:
  """设备 guid 必须在 PG 里有档案 —— 否则 TDengine 侧只会**静默返回 0 行**。

  2026-10-22 实测：模型把测点的 `idc`（`P001_A001`）当 `device_guids` 传，
  `_scope_types` 查不到就返回空结果，模型当成"没数据"继续猜了 3 轮设备名。
  报错要把**怎么拿到真 guid** 写清楚，否则模型只会换个猜法。

  `guid` 只在园区内唯一：给了园区就按园区查，跨园区查询才不过滤。
  跨园区时同名 guid 会返回多行，只要**任一行**有 device_type 就能定位超级表。
  """
  rows = b.pg.devices_meta_rows(guids, cluster=cluster or None)
  found = {str(r.get('guid') or '') for r in rows}
  unknown = [g for g in guids if g not in found]
  if unknown:
    listed = '、'.join(unknown[:5]) + ('…' if len(unknown) > 5 else '')
    raise DcimMetaError(
      f'设备 guid 不存在：{listed} —— 先用 query_points / query_devices 取真实 guid'
      '（形如 `03160041-00AF-FFFF-0012-0001FFFF0001`），别拿测点的 idc / 空间编码当 guid'
    )
  typed = {str(r.get('guid') or '') for r in rows if str(r.get('device_type') or '')}
  no_type = [g for g in guids if g not in typed]
  if no_type:
    raise DcimMetaError(
      '这些设备在档案里没有 device_type，无法定位超级表：'
      f'{"、".join(no_type[:5])}'
    )


def _resolve_point_columns(
  b: '_Backend', device_type: str, point_ids: list[str],
) -> list[str]:
  """point_id → TDengine 列名（走 PG 的 `dcim_td_column`）；有不认识的直接报错给模型。"""
  mapping = b.pg.td_columns(device_type)
  cols, missing = _match_point_columns(mapping, point_ids)
  if missing:
    known = sorted({str(c) for c in mapping.values()})[:8]
    raise DcimMetaError(
      f'设备类型 {device_type} 下没有测点 {"、".join(missing)}（列映射 `dcim_td_column` 未注册）。'
      f'可用的 point_id 示例：{"、".join(known) or "（空）"} —— 请先用 query_points 取 point_id'
    )
  return cols


def _agg_total(n_devices: int, start_ms: int, end_ms: int, interval: str) -> int:
  """聚合路径的 `total` = **设备数 × 窗口数**（纯函数，便于离线断言）。

  ⚠️ **这必须是「上界」，不能是「下界」** —— `analysis_load` 的判断是
  `if total is not None: 只有 len(records) >= total 才停`，`len(页) < page_size` 那个兜底
  **在有 total 时会被跳过**（`tools/analysis_tools.py`）。所以：
    - 取**上界** → `>= total` 永不成立，靠 `OFFSET` 越界返回 0 行终止（实测确认返回 0 行），
      代价只是多一次空页请求 ✓
    - 取**下界** → 半路提前停 → **静默丢数据** ✗

  为什么它确实是上界：窗口数按区间整除算（实际有数据的窗口 ≤ 它）；设备数取自 PG 的
  `dcim_devices`（**定义的**设备 ≥ TD 里真有数据的设备）。

  ⚠️ `n_devices` 必须是**不同 `(cluster, guid)` 的对数**，不是 `len(guids)`：
  guid 只在园区内唯一，跨园区时同一个 guid 可能在两个园区各有一台设备，
  按去重后的 guid 数会少算一半 → `total` 变成下界 → `analysis_load` 提前停。

  为什么不用一条 count SQL：实测这个版本的 TDengine **两种子查询计数都不支持** ——
  `count(*) FROM (窗口查询)` 报 `No valid function in window query`、
  `count(DISTINCT device_guid)` 报语法错。所以只能算出来。
  """
  win_ms = interval_to_ms(interval)
  windows = int((end_ms - start_ms) // win_ms) + 1
  return max(0, int(n_devices)) * max(0, windows)


def _series_one(
  b: '_Backend', device_type: str, *, guids: list[str] | None, interval: str, agg: str,
  limit: int, offset: int, start_ms: int, end_ms: int, cluster: str,
  space_guid: str | None, point_cols: list[str] | None = None,
  point_group: str = 'g0',
) -> dict:
  """**单个超级表**的时序取数（`(device_type, point_group)` 定位一张表）。

  两条路径**都分页**，但 `total` 的来路不同：

  - 原始值路径：单独一条 count SQL，与取数**共用同一套 WHERE**（`realtime._series_filters`）
  - 聚合路径：结果集是「每设备 × 每窗口一行」，要数的是**分组数**，而 TDengine 不支持
    `count(*) FROM (窗口查询)` → 只能用 `_agg_total()` 算上界（见其 docstring）

  `point_cols` 非空时只选这几个测点列（+ `ts`/`device_guid`，原始路径）—— 宽表一行上千列，
  不选列的话单行就能撑满工具输出上限，曲线根本拼不出来。
  """
  stable = stable_name(device_type, point_group)
  common = dict(start_ms=start_ms, end_ms=end_ms, cluster=cluster,
                device_guids=guids, space_guid=space_guid)
  if interval:
    data = b.td.agg_series(stable=stable, interval=interval, agg=agg,
                           limit=limit, offset=offset, columns=point_cols, **common)
    if guids and cluster:
      n_devices = len(guids)                  # 单园区内 guid 唯一
    elif guids:
      # 跨园区：同一 guid 可能对应两台设备，必须按 (cluster, guid) 数
      n_devices = sum(
        1 for r in b.pg.devices_meta_rows(guids)
        if str(r.get('device_type') or '') == device_type
      )
    else:
      n_devices = b.pg.count_devices(
        cluster, space_guid=space_guid, device_type=device_type,
      )
    data['total'] = _agg_total(n_devices, start_ms, end_ms, interval)
  else:
    raw_cols = ['ts', 'device_guid', *point_cols] if point_cols else None
    data = b.td.query(build_series_query(stable=stable, limit=limit, offset=offset,
                                         columns=raw_cols, **common))
    data['total'] = int(b.td.query(build_series_count(stable=stable, **common))['records'][0]['n'])
  # 时间**在数据层渲染成北京时间**（原始路径 `ts`、聚合路径 `_wstart`、采集时间 `report_ts`）
  for rec in data.get('records') or []:
    for key in ('ts', '_wstart', 'report_ts'):
      if key in rec:
        rec[key] = _local_time(rec[key])
  return data


def _series_for_type(
  b: '_Backend', device_type: str, *, guids: list[str] | None, interval: str, agg: str,
  limit: int, offset: int, start_ms: int, end_ms: int, cluster: str,
  space_guid: str | None, point_cols: list[str] | None = None,
) -> dict:
  """一个设备类型的取数 = **按测点组分别查表，再把同一时刻的行合并回一行**。

  超大设备类型的测点被拆到 `st_<type>`(g0) / `st_<type>_g1`…（TDengine 单表 4,096 列封顶）。
  写侧每组各写一整行、各组行数因此一致 → 每组按**同样的 limit/offset** 取即可，
  合并后行数仍是 limit；`total` 取各组**最大值**（同口径；相加会把「一台设备一个时刻」
  算成 N 行，翻页就会提前停）。
  """
  by_group = b.pg.td_columns_by_group(device_type)     # {point_group: {point_code: td_column}}
  col_to_group = {col: g for g, mapping in by_group.items() for col in mapping.values()}
  if point_cols:
    groups: dict[str, list[str]] = {}
    for col in point_cols:
      point_group = col_to_group.get(col)
      if point_group:
        groups.setdefault(point_group, []).append(col)
  else:
    groups = {point_group: [] for point_group in (by_group or {'g0': {}})}   # 空 = 该组全列

  parts: list[dict] = []
  total = 0
  for point_group in sorted(groups):
    one = _series_one(
      b, device_type, point_group=point_group, guids=guids, interval=interval, agg=agg,
      limit=limit, offset=offset, start_ms=start_ms, end_ms=end_ms, cluster=cluster,
      space_guid=space_guid, point_cols=groups[point_group] or None,
    )
    total = max(total, int(one.get('total') or 0))
    parts.append(one)
  if not parts:
    return _paged([], 0)
  if len(parts) == 1:
    return parts[0]
  return _merge_rows_by_key(parts, time_key='_wstart' if interval else 'ts', total=total)


def _merge_rows_by_key(parts: list[dict], *, time_key: str, total: int) -> dict:
  """把**同一 `(时间, cluster_name, device_guid)` 的多组行合并成一行**（列取并集，非空值优先）。

  为什么必须合并：写侧每组各写一整行（一张超级表的列集固定，拆组就等于拆表），
  所以一台设备一个时刻在 TDengine 里是 N 行、每行只有自己那组的列。不合并的话
  `query_series` 的输出会**按组数翻倍**，而且每行一半列是空 —— 表和曲线都会错。

  ⚠️ 键必须带 `cluster_name`：guid 只在园区内唯一，跨园区时两个园区的同名设备
  是**两台设备**，按 `(时间, guid)` 合并会把它们并成一行。单园区时 `cluster_name`
  是常量（记录里没有时取空串），结果与只按 guid 合并一致。

  `time_key`：原始路径 `ts`、聚合路径 `_wstart`，都已在 `_series_one` 里渲染成北京时间
  字符串（字典序 = 时间序），所以可直接按字符串排序。
  """
  columns: list[str] = []
  rows: dict[tuple[str, str, str], dict] = {}
  for part in parts:
    for col in part.get('columns') or []:
      if col not in columns:
        columns.append(col)
    for rec in part.get('records') or []:
      key = (str(rec.get(time_key) or ''), str(rec.get('cluster_name') or ''),
             str(rec.get('device_guid') or ''))
      row = rows.get(key)
      if row is None:
        rows[key] = dict(rec)
        continue
      for k, v in rec.items():
        if v not in (None, ''):
          row[k] = v
  return {'columns': columns, 'records': [rows[k] for k in sorted(rows)],
          'total': int(total)}


def _tag_type(part: dict, device_type: str) -> dict:
  """给一批结果补 `device_type` —— 跨类型合并后不标来源就分不清哪行是哪类设备。"""
  records = part.get('records') or []
  for r in records:
    r['device_type'] = device_type
  columns = list(part.get('columns') or [])
  if 'device_type' not in columns:
    columns.append('device_type')
  return {'columns': columns, 'records': records, 'total': int(part.get('total') or 0)}


def _merge_parts(parts: list[dict], *, time_key: str, total: int) -> dict:
  """合并多个超级表的结果：列取**并集**（按首次出现顺序），
  行按 `(时间, cluster_name, device_guid)` 排序（跨园区时 guid 不唯一）。

  时间列已在 `_series_one` 里渲染成 `YYYY-MM-DD HH:MM:SS`，字典序与时间序一致，直接按字符串排。
  """
  columns: list[str] = []
  records: list[dict] = []
  for p in parts:
    for c in p.get('columns') or []:
      if c not in columns:
        columns.append(c)
    records.extend(p.get('records') or [])
  records.sort(key=lambda r: (
    str(r.get(time_key) or ''), str(r.get('cluster_name') or ''),
    str(r.get('device_guid') or ''),
  ))
  return {'columns': columns, 'records': records, 'total': int(total)}


def _scope_types(
  b: '_Backend', *, cluster: str, space_guid: str | None, guids: list[str] | None,
) -> dict[str, list[str] | None]:
  """未给 `device_type` 时，反查「要打哪几张超级表」→ `{device_type: 该类型限定的 guid 或 None}`。

  超级表**按设备类型分**，不知道类型就不知道打哪张表 —— 所以必须有空间或设备来收窄范围。
  """
  if guids:
    # 不能用 dict 按 guid 索引：跨园区同名 guid 可能属于**不同设备类型**，
    # 后者会覆盖前者 → 只查一张超级表、另一类设备的数据静默消失。
    rows = b.pg.devices_meta_rows(guids, cluster=cluster or None)
    out: dict[str, list[str]] = {}
    for r in rows:
      dt = str(r.get('device_type') or '')
      g = str(r.get('guid') or '')
      if dt and g and g not in out.setdefault(dt, []):
        out[dt].append(g)
    return out
  if space_guid:
    # 空间路径**不去 PG 拼设备 guid**：宽表 tag 里冗余了 space_guid，直接在 TDengine 上筛更省
    return {dt: None for dt in b.pg.device_types(cluster, space_guid=space_guid)}
  raise DcimMetaError(
    '未指定 `device_type` 时，需给出 `space_guid`（按机房查）或 `device_guids` 以确定查哪类设备'
  )


def _query_series(params: dict) -> dict:
  cluster = _cluster(params)
  device_type = str(params['device_type']) if params.get('device_type') else None
  start_ms = _epoch_ms(_require(params, 'start_ms')[0])
  end_ms = _epoch_ms(_require(params, 'end_ms')[0])
  if start_ms is None or end_ms is None:
    raise DcimMetaError(
      'start_ms / end_ms 无法解析为时间：请直接传日期（如 `2026-10-19` 或 '
      '`2026-10-19 00:00:00`，北京时间），也接受毫秒/秒时间戳'
    )
  guids = _as_list(params.get('device_guids'))
  point_ids = _as_list(params.get('point_ids'))
  space_guid = str(params['space_guid']) if params.get('space_guid') else None
  interval = str(params.get('interval') or '').strip()
  agg = str(params.get('agg') or 'avg')
  limit, offset = _page(params, default_size=2000)

  with _Backend() as b:
    if guids:
      # **显式**分支也要校验：TD 侧遇到不存在的 guid 同样只是 0 行，不会报错
      _validate_guids(b, guids, cluster)
    if device_type:
      point_cols = _resolve_point_columns(b, device_type, point_ids) if point_ids else None
      return _series_for_type(
        b, device_type, guids=guids, interval=interval, agg=agg, limit=limit, offset=offset,
        start_ms=start_ms, end_ms=end_ms, cluster=cluster, space_guid=space_guid,
        point_cols=point_cols,
      )
    # 跨类型：一个类型一条 SQL，再在应用层合并（TDengine 不能跨超级表 JOIN）
    scope = _scope_types(b, cluster=cluster, space_guid=space_guid, guids=guids)
    if not scope:
      return _paged([], 0)
    if point_ids:
      # 测点列按设备类型各自映射，跨类型合并前必须知道是哪一类（point_id 只在该类里有意义）
      if len(scope) > 1:
        raise DcimMetaError(
          '指定 `point_ids` 时需同时给出 `device_type`（或只给 `device_guids`，'
          f'当前范围含 {len(scope)} 种设备类型）'
        )
      device_type = next(iter(scope))
      point_cols = _resolve_point_columns(b, device_type, point_ids)
      return _series_for_type(
        b, device_type, guids=scope[device_type], interval=interval, agg=agg,
        limit=limit, offset=offset, start_ms=start_ms, end_ms=end_ms, cluster=cluster,
        space_guid=space_guid, point_cols=point_cols,
      )
    if len(scope) > _MAX_CROSS_TYPES:
      # 防呆兜底。真实机房一个房间十几类设备很正常，所以这个值给得很宽 ——
      # 真正的成本是「类型数 × 每类取数」，见下面的 _CROSS_FETCH_CAP
      raise DcimMetaError(
        f'该范围含 {len(scope)} 种设备类型（上限 {_MAX_CROSS_TYPES}），请指定 `device_type`'
        '或缩小空间范围（每类设备是独立的超级表）'
      )
    # ⚠️ 两条路径统一成「每类型多取 (offset+limit) → 全局排序 → 再切片」。
    # 不能"每类型各回一页"再拼 —— 那样第 2 页是把各类型自己的第 2 页拼起来，
    # 与「全局第 2 页」根本不是一回事（与第 1 页会重叠、也会漏行）。
    #
    # 为什么不按"总行数"再分摊每类取数：那会让 fetch < offset+limit，
    # 于是切片落在被截断的区间上 → **又变成静默截断**（正是本次要修的问题）。
    # 所以保持精确语义，把上界交给「类型数 × 每类上限」：
    # 最坏 32 × 20000 = 64 万行，且只有深翻页（模型显式要 page=50 这种）才够得着；
    # analysis_load 侧另有 AGENT_ANALYSIS_MAX_ROWS(10000) 兜底。
    fetch = min(limit + offset, _CROSS_FETCH_CAP)
    time_key = '_wstart' if interval else 'ts'
    parts, total = [], 0
    for dt, g in sorted(scope.items()):
      one = _series_for_type(b, dt, guids=g, interval=interval, agg=agg, limit=fetch, offset=0,
                             start_ms=start_ms, end_ms=end_ms, cluster=cluster,
                             space_guid=space_guid)
      total += one['total']
      parts.append(_tag_type(one, dt))
    merged = _merge_parts(parts, time_key=time_key, total=total)
    merged['records'] = merged['records'][offset:offset + limit]
    return merged


# ── PUE / 能耗统计（独立超级表 `st_pue`，建表/写入/SQL 见 `service/dcim_ingest/pue.py`） ──

def _pue_time_types(params: dict) -> list[int]:
  """`time_type` → 口径代号列表（缺省 `real`=1）。多个用逗号分隔，中文也认。

  ⚠️ 默认只取**实时**口径：小时/日/月是对方按期末推的统计快照，与实时值不是一个量纲，
  混在一起取会让模型把「期末总量」当「当前值」。
  """
  raw = str(params.get('time_type') or '').strip()
  if not raw:
    return [1]
  out: list[int] = []
  for token in re.split(r'[,，\s]+', raw):
    if not token:
      continue
    tt = resolve_time_type(token)
    if tt is None:
      raise DcimMetaError(
        f'未知时间口径 time_type={token!r}（可选：real/hour/day/week/month/year，也可传中文）'
      )
    if tt.code not in out:
      out.append(tt.code)
  return out or [1]


def _pue_columns(params: dict) -> list[str] | None:
  """`metrics` → 指标列（None = 全部 18 列）。列名 / 中文名 / 属性后缀都认。"""
  raw = str(params.get('metrics') or '').strip()
  if not raw:
    return None
  out: list[str] = []
  for token in re.split(r'[,，\s]+', raw):
    if not token:
      continue
    col = resolve_column(token)
    if col is None:
      raise DcimMetaError(
        f'未知指标 metrics={token!r}（可传列名如 `it_value`、中文名如 `IT耗电量`）'
      )
    if col not in out:
      out.append(col)
  return out or None


def _pue_space_names(b: '_Backend', rows: list[dict]) -> dict[tuple[str, str], str]:
  """`(cluster, building)` → 空间全名（**展示字段**；查不到留空，不影响数值）。

  楼宇 `idc`（`P001_A009`）对人没意义，用户看的是「廊坊园区A1数据中心」。
  """
  cache: dict[tuple[str, str], str] = {}
  for rec in rows:
    key = (str(rec.get('cluster') or ''), str(rec.get('building') or ''))
    if not key[0] or not key[1] or key in cache:
      continue
    try:
      hits = b.pg.find_spaces(key[0], idc=key[1], limit=2)
    except Exception:                       # noqa: BLE001 —— 少个展示名不该让整次查询失败
      hits = []
    cache[key] = str(hits[0].get('name') or '') if hits else ''
  return cache


def _query_pue(params: dict) -> dict:
  """PUE / 能耗统计查询。

  三种取数形态：
    - `latest=true`：每个「楼宇 × 口径」的最新一行（问「现在 PUE 多少」）；
    - 带 `interval`：按时间窗聚合（长区间必须给，实时值约 10 秒一条）；
    - 否则：原始序列分页（必须带时间窗，与 `query_series` 同一口径）。
  """
  scope = _scope(params)                    # 代号 / 代号列表（park 展开）/ None
  cluster = scope if isinstance(scope, str) else None
  clusters = scope if isinstance(scope, list) else None
  buildings = _as_list(params.get('building'))
  columns = _pue_columns(params)
  time_types = _pue_time_types(params)
  latest = _truthy(params.get('latest'))
  interval = str(params.get('interval') or '').strip()
  agg = str(params.get('agg') or 'avg').strip() or 'avg'
  limit, offset = _page(params, default_size=2000)
  space = str(params['space']) if params.get('space') else None
  space_guid = str(params['space_guid']) if params.get('space_guid') else None

  start_ms = end_ms = None
  if not latest:
    if not params.get('start_ms') or not params.get('end_ms'):
      raise DcimMetaError(
        'PUE 时序查询必须给时间窗：请传 `start_ms` / `end_ms`（直接传日期，如 `2026-10-19`，'
        '北京时间）；只看当前值请传 `latest=true`'
      )
    start_ms, end_ms = _epoch_ms(params['start_ms']), _epoch_ms(params['end_ms'])
    if start_ms is None or end_ms is None:
      raise DcimMetaError(
        'start_ms / end_ms 无法解析为时间：请直接传日期（如 `2026-10-19`），也接受毫秒/秒时间戳'
      )

  resolved = None
  with _Backend() as b:
    if space and not space_guid:
      resolved = _resolve_space_guid(b, scope, space)
      space_guid = str(resolved.get('guid') or '') or None
    common = dict(
      cluster=cluster, clusters=clusters, buildings=buildings,
      space_guid=space_guid, time_types=time_types,
    )
    if latest:
      rows = unwrap_last_row(
        b.td.query(build_pue_last_row(columns=columns, **common))['records'])
      total = len(rows)
      rows = rows[offset:offset + limit]
    elif interval:
      try:
        sql = build_pue_agg(
          columns=columns, interval=interval, agg=agg,
          start_ms=start_ms, end_ms=end_ms, **common,
        )
      except ValueError as e:
        raise DcimMetaError(str(e)) from e
      rows = b.td.query(sql)['records']
      # 聚合的分组数**不是已知量**（算不出 `_agg_total` 那种上界），给假的小总数会让
      # `analysis_load` 提前停；满页时给 offset+limit 让它再翻一页，不满页给真实结束位置
      total = offset + (limit if len(rows) >= limit else len(rows))
    else:
      rows = b.td.query(build_pue_series(
        columns=columns, start_ms=start_ms, end_ms=end_ms,
        limit=limit, offset=offset, **common,
      ))['records']
      total = int(b.td.query(build_pue_count(
        **common, start_ms=start_ms, end_ms=end_ms,
      ))['records'][0]['n'])

    for rec in rows:                        # 单园区时 SQL 不带 cluster_name → 用入参补
      cl = str(rec.pop('cluster_name', '') or cluster or '')
      if cl:
        rec['cluster'] = cl
        rec['park'] = park_of(cl)
      if 'time_type' in rec:
        rec['time_type'] = time_type_label(rec.get('time_type'))
      for key in ('ts', '_wstart'):
        if key in rec:
          rec[key] = _local_time(rec.get(key))
    names = _pue_space_names(b, rows)
    for rec in rows:
      name = names.get((str(rec.get('cluster') or ''), str(rec.get('building') or '')))
      if name:
        rec['space_name'] = name

  out = _paged(rows, total)
  out['metric_labels'] = {c: COLUMN_LABELS[c] for c in (columns or COLUMN_LABELS)}
  if latest:
    out['latest'] = True
  echo = _echo_space(resolved)
  if echo:
    out['space'] = echo
  return out


def _check_consistency(params: dict) -> dict:
  """核对 TDengine 与 PG 两侧对得上对不上。

  宽表下"关联键"不再是逐点 guid，而是**设备 guid + 超级表的列**，所以两边各查一次：
    - 设备：TD 里出现过的 `device_guid` vs PG 的设备定义
    - 列：TD 超级表的数据列 vs PG `dcim_td_column` 注册的列
  """
  cluster = _cluster(params)
  device_type = str(params['device_type']) if params.get('device_type') else None
  with _Backend() as b:
    # ⚠️ 用**不带 limit** 的取数：`find_devices` 的 limit 一旦被超过就静默截断，
    #    总表有、时序库也有的设备会被误报成"只有定义没有数值"（百万设备下必然踩到）
    types = [device_type] if device_type else b.pg.device_types(cluster)
    single = bool(cluster)

    def _label(key: tuple[str, str]) -> str:
      """单园区输出 guid（与旧行为一致），跨园区输出 `cluster/guid`。"""
      return key[1] if single else f'{key[0]}/{key[1]}'

    report = []
    for dt in sorted(set(types)):
      # 设备身份是 `(cluster, guid)` —— 跨园区只比 guid 时，两个园区各有一台同名设备、
      # 其中一个在时序库缺数据，集合差集为空，缺失会被**掩盖**。
      if single:
        defined = {('', g) for g in b.pg.device_guids(cluster, device_type=dt)}
      else:
        defined = set(b.pg.device_keys(cluster, device_type=dt))
      by_group = b.pg.td_columns_by_group(dt)
      # 不给园区 = 跨园区巡检 → 不加 cluster_name 过滤（给了才拼）
      # ⚠️ cluster 来自模型，必须转义后再拼（不转义 = 注入 + 绕过园区隔离）
      span = f' WHERE cluster_name = {sql_literal(cluster)}' if cluster else ''
      # 超大类型的测点拆成了多张超级表 → **按组逐表比对**，报告里带上组名才定位得到
      for point_group in sorted(by_group) or ['g0']:
        stable = stable_name(dt, point_group)
        try:
          if single:
            records = b.td.query(
              f'SELECT DISTINCT device_guid FROM {stable}{span}')['records']
            seen = {('', str(row.get('device_guid') or '')) for row in records}
          else:
            records = b.td.query(
              f'SELECT DISTINCT cluster_name, device_guid FROM {stable}{span}')['records']
            seen = {(str(row.get('cluster_name') or ''),
                     str(row.get('device_guid') or '')) for row in records}
          cols_td = set(b.td.stable_columns(stable)) - {'ts', 'report_ts', 'quality'}
        except Exception as e:                    # noqa: BLE001 —— 表还没建等，算"没数据"
          logger.info('一致性巡检：%s 无时序表/查不到（%s）', stable, e)
          seen, cols_td = set(), set()
        cols_pg = set((by_group.get(point_group) or {}).values())
        report.append({
          'device_type': dt,
          'point_group': point_group,
          'stable': stable,
          'devices_defined': len(defined),
          'devices_with_data': len(seen),
          'devices_only_in_td': [_label(k) for k in sorted(seen - defined)[:20]],
          'devices_only_in_pg': [_label(k) for k in sorted(defined - seen)[:20]],
          'columns_only_in_td': sorted(cols_td - cols_pg),
          'columns_only_in_pg': sorted(cols_pg - cols_td),
        })
  return {'records': report, 'total': len(report)}


# 4 档告警，**从重到轻**排列（命中即定级：一个测点只在最重的那档出现一次）
_LEVELS: tuple[tuple[str, str, str], ...] = (
  ('urgent', '紧急', 'urgent_alarm'),
  ('important', '重要', 'important_alarm'),
  ('general', '一般', 'general_alarm'),
  ('hint', '提示', 'hint_alarm'),
)


def _levels_since(only: str) -> tuple[tuple[str, str, str], ...]:
  """`only` 档**及以上**（如 `important` → 紧急 + 重要）。给了未知档位直接报错给模型看。

  中英文都收（`紧急` = `urgent`）—— 能力描述是中文，模型直接给中文档位很正常，
  为此白跑一轮太亏。
  """
  if not only:
    return _LEVELS
  key = {label: k for k, label, _ in _LEVELS}.get(only, only)
  for i, (k, _, _) in enumerate(_LEVELS):
    if k == key:
      return _LEVELS[:i + 1]
  raise DcimMetaError(
    f'未知的告警档位 {only!r}（可选：{"/".join(k for k, _, _ in _LEVELS)}）'
  )


def _enabled(raw: Any) -> bool | None:
  """`maxUse`/`minUse` 这类**开关字段** → True/False；字段整个缺失返回 None（区别于"值为 0"）。"""
  if raw is None or (isinstance(raw, str) and not raw.strip()):
    return None
  try:
    return float(raw) != 0
  except (TypeError, ValueError):
    return None


def _breach(value: float, thr: Any) -> tuple[str, float] | None:
  """值是否越界 → `(方向, 越过的阈值)`。

  ⚠️ **一个边界是否生效，看的是同级的 `maxUse` / `minUse`，不是 `maxAlarm` 是不是 0。**
  这是在生产数据上验出来的（432 条档位记录，零反例）：

    - 未配置的档位长这样：`maxUse:0, minUse:0, maxAlarm:0, minAlarm:0`
    - `maxUse:0` 从不出现在 `maxAlarm≠0` 上；`maxUse:1` 却有 `maxAlarm=0` 的

  所以两种做法都会错：**"0 就是上限"** 会把每个未配置的测点都报成越限（噪声淹没信号）；
  **"非零才算配置"** 又正好漏掉通断类测点（`HT_Status_Comm` 的 `maxAlarm=0` 是**真上限**：
  0=正常，超过就告警）。`maxUse`/`minUse` 是唯一能区分这两种 0 的信号。

  `maxEvent`/`minEvent` 是告警**编码**（实测是 `'HJ00200004'`），不参与比较。
  """
  if not isinstance(thr, dict):
    return None
  for use_key, alarm_key, direction, cmp in (
    ('maxUse', 'maxAlarm', '高于上限', lambda v, t: v > t),
    ('minUse', 'minAlarm', '低于下限', lambda v, t: v < t),
  ):
    if alarm_key not in thr:
      continue
    try:
      bound = float(thr[alarm_key])
    except (TypeError, ValueError):
      continue                      # 给了但不是数（如只配了 maxEvent 文案）→ 无法比较
    flag = _enabled(thr.get(use_key))
    if flag is None:                # 开关字段缺失（对方换报文）→ 退回"数值非零才算"
      flag = bound != 0
    if flag and cmp(value, bound):
      return direction, bound
  return None


def _query_over_threshold(params: dict) -> dict:
  """**当前值越过告警阈值**的测点清单（越限巡检）。

  阈值只在 PG（`dcim_points` 的 4 档 JSONB），当前值只在 TDengine —— 在这里跨库比完再给结论。
  ⚠️ **返回不分页**：候选（有阈值的测点）按上限扫一遍，`total` = 命中数。
  真正的"命中集合"要跨库比完才知道，分页会给出互相矛盾的页。
  """
  cluster = _cluster(params)
  space_guid = str(params['space_guid']) if params.get('space_guid') else None
  device_guid = str(params['device_guid']) if params.get('device_guid') else None
  keyword = str(params['keyword']) if params.get('keyword') else None
  only = str(params.get('level') or '').strip().lower()
  levels = _levels_since(only)

  with _Backend() as b:
    rows = b.pg.find_points_with_threshold(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      levels=[k for k, _, _ in levels], limit=_MAX_PAGE_SIZE,
    )
    candidates = b.pg.count_points_with_threshold(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      levels=[k for k, _, _ in levels],
    )
    latest, col_of = _latest_values(b, cluster, rows)

  hits: list[dict] = []
  for r in rows:
    value, ts = _point_value(r, latest, col_of)
    try:
      num = float(value)
    except (TypeError, ValueError):
      continue                    # 非数值（通断/状态点、或还没采到值）不参与阈值比较
    for key, label, field in levels:
      hit = _breach(num, r.get(field))
      if hit:
        item = {
          'device_name': r.get('device_name'),
          'device_type': r.get('device_type'),
          'device_guid': r.get('device_guid'),
          'point_name': r.get('name'),
          'point_id': r.get('point_id'),
          'unit': r.get('unit'),
          'value': num,
          'ts': _local_time(ts),
          'level': label,
          'level_key': key,
          'direction': hit[0],
          'threshold': hit[1],
        }
        if not cluster:
          item['cluster'] = r.get('cluster')   # 跨园区时 guid 不唯一，必须带上园区
        hits.append(item)
        break                     # 已按从重到轻判过，最重一档命中即定级

  out = _paged(hits, len(hits))
  out['scanned'] = len(rows)
  if candidates > len(rows):
    out['note'] = (f'带阈值的候选测点共 {candidates} 个，超过单次扫描上限 {_MAX_PAGE_SIZE}，'
                   '本次只扫了前一部分；请用 space_guid 缩小范围')
  return out


# 告警记录的码值 → 中文（**原位替换**，不新增 `*_label` 重复字段）
_ALARM_STATUS_LABELS = {'1': '触发', '2': '刷新', '3': '恢复'}
_ALARM_LEVEL_LABELS = {1: '提示', 2: '一般', 3: '重要', 4: '紧急'}
_ALARM_TYPE_LABELS = {0: '普通告警', 1: '通讯告警'}


def _decode_alarm(r: dict) -> None:
  """把告警行里的裸码值换成中文，省得模型自己查表（也治"字段少"的观感）。"""
  status = str(r.get('status') or '')
  if status in _ALARM_STATUS_LABELS:
    r['status'] = _ALARM_STATUS_LABELS[status]
  level = r.get('alarm_level')
  if isinstance(level, int) and level in _ALARM_LEVEL_LABELS:
    r['alarm_level'] = _ALARM_LEVEL_LABELS[level]
  alarm_type = r.get('alarm_type')
  if isinstance(alarm_type, int) and alarm_type in _ALARM_TYPE_LABELS:
    r['alarm_type'] = _ALARM_TYPE_LABELS[alarm_type]


def _query_alarms(params: dict) -> dict:
  """**对方系统推的告警事件**清单（触发 / 刷新 / 恢复三态）。

  和 `query_over_threshold` 的分工（两者极易被混用）：
  - 那个是**我们算的**「此刻值越没越限」—— 没有生命周期、没有恢复状态；
  - 这个是**对方告警引擎的事件** —— 同一 `event_log_id` 的三态**串成当前态表里的一条**。

  ⚠️ 「在跑的」只能按 `status <> '3'` 判：未恢复时对方把解除时间/值给成**哨兵 0**，
  而设备**真实恢复值本身也可能是 0** —— 用 `!= 0` 判必错（同阈值那次 `maxAlarm:0` 的坑）。

  `unresponded_min=N` = **发生超过 N 分钟、仍未解除、且没有任何处置工单**的告警
  （= 还没人响应）。与 `query_alarm_orders.response_timeout_min`（已响应但慢）是两回事。
  """
  cluster = _scope(params)
  limit, offset = _page(params, default_size=200)
  space_guid = str(params['space_guid']) if params.get('space_guid') else None
  space = str(params['space']) if params.get('space') else None
  device_guid = str(params['device_guid']) if params.get('device_guid') else None
  keyword = str(params['keyword']) if params.get('keyword') else None
  level = str(params['level']) if params.get('level') else None
  status = str(params['status']) if params.get('status') else 'active'
  # 列是**epoch 秒**（DDL 注释曾误写"毫秒"），入参统一归一到秒
  start_s = _epoch_seconds(params.get('start_ms'))
  end_s = _epoch_seconds_end(params.get('end_ms'))
  unresponded_min = _unresponded_minutes(params)

  resolved = None
  with _Backend() as b:
    # `space` 是给人用的（「A1」），`space_guid` 是给机器用的；显式 guid 更精确，优先
    if space and not space_guid:
      resolved = _resolve_space_guid(b, cluster, space)
      space_guid = str(resolved.get('guid') or '') or None
    total = b.pg.count_alarms(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      level=level, status=status, start_ms=start_s, end_ms=end_s,
      unresponded_min=unresponded_min,
    )
    records = b.pg.find_alarms(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      level=level, status=status, start_ms=start_s, end_ms=end_s,
      unresponded_min=unresponded_min, limit=limit, offset=offset,
    )
  for r in records:
    _attach_work_order(r)                   # 用原始秒算响应时长，须在渲染之前
    for key in ('alarm_time', 'remove_time'):
      if key in r:
        r[key] = _local_time(r.get(key))
    _decode_alarm(r)
  out = _paged(records, total)
  echo = _echo_space(resolved)
  if echo:
    out['space'] = echo
  # 回带**实际生效**的时间窗：模型把「某个时刻」自行扩成区间时，
  # 结果里能一眼对照用户说的时刻与真正查的区间，排障也有证据。
  if start_s or end_s:
    out['window'] = {
      'start': _local_time(start_s) if start_s else None,
      'end': _local_time(end_s) if end_s else None,
    }
  return out


# 告警工单的码值 → 中文（与 `dcim_alarms` 的 status 是**两套**，别混：
# 告警事件是 触发/刷新/恢复，工单是 处理中/已处理）
_ORDER_STATUS_LABELS = {1: '处理中', 2: '已处理'}
_ORDER_RESULT_LABELS = {1: '确认告警', 2: '已恢复正常'}
_ORDER_TIME_FIELDS = ('alarm_time', 'response_time', 'start_time', 'end_time')


def _minutes(start_s, end_s):
  """两个 epoch 秒 → 相差分钟（1 位小数）；任一无效或倒挂返回 None（脏数据不猜）。"""
  try:
    a, b = int(start_s), int(end_s)
  except (TypeError, ValueError):
    return None
  if a <= 0 or b <= 0 or b < a:
    return None
  return round((b - a) / 60.0, 1)


def _slim_order(r: dict) -> dict:
  """工单行 → 模型要的瘦形状。

  **时长必须先用原始秒算、再渲染时间**（渲染后是字符串，减不了）：
  `response_minutes` = 响应时刻 − 告警时刻，`handle_minutes` = 处理结束 − 开始。
  算好在服务端给，模型就不用自己做减法（平台既有原则，见报告侧事故）。

  **空值键不进结果**：工单大部分字段在「处理中」阶段是空的，逐条带 `null` 纯浪费字符
  （工具输出有 12,000 字符上限，与 `_slim_point` 同一取舍）。
  """
  response_min = _minutes(r.get('alarm_time'), r.get('response_time'))
  handle_min = _minutes(r.get('start_time'), r.get('end_time'))
  out: dict[str, Any] = {
    'cluster': r.get('cluster'),
    'order_no': r.get('order_no'),
    'event_log_id': r.get('event_log_id'),
    'alarm_status': _ALARM_STATUS_LABELS.get(str(r.get('alarm_status') or '')),
    'status': _ORDER_STATUS_LABELS.get(r.get('status'), r.get('status')),
    'alarm_level': _ALARM_LEVEL_LABELS.get(r.get('alarm_level'), r.get('alarm_level')),
    'space_name': r.get('space_name'),
    'device_name': r.get('device_name'),
    'point_name': r.get('point_name'),
    'content': r.get('content'),
    'alarm_value': r.get('alarm_value'),
    'alarm_time': r.get('alarm_time'),
    'response_time': r.get('response_time'),
    'response_minutes': response_min,
    'start_time': r.get('start_time'),
    'end_time': r.get('end_time'),
    'handle_minutes': handle_min,
    'star': r.get('star'),
    'responder': r.get('responder'),
    'handler': r.get('handler'),
    'process': r.get('process'),
    'process_result': _ORDER_RESULT_LABELS.get(r.get('process_result'), r.get('process_result')),
    'processing_operator_name': r.get('processing_operator_name'),
    'processed_operator_name': r.get('processed_operator_name'),
  }
  for key in _ORDER_TIME_FIELDS:
    if key in out:
      out[key] = _local_time(out[key])
  return {k: v for k, v in out.items() if v not in (None, '')}


def _attach_work_order(r: dict) -> None:
  """把同库 LEFT JOIN 带出的工单列收成嵌套 `work_order`（无工单则整个键不出现）。

  **必须在渲染 `alarm_time` 之前调用** —— `response_minutes` 要用两边的原始秒相减，
  渲染成字符串后就减不了。
  """
  order_no = r.pop('wo_order_no', None)
  status = r.pop('wo_status', None)
  responder = r.pop('wo_responder', None)
  handler = r.pop('wo_handler', None)
  response_ts = r.pop('wo_response_time', None)
  result = r.pop('wo_process_result', None)
  star = r.pop('wo_star', None)
  if not order_no:
    return
  wo = {
    'order_no': order_no,
    'status': _ORDER_STATUS_LABELS.get(status, status),
    'responder': responder,
    'handler': handler,
    'response_time': _local_time(response_ts),
    'response_minutes': _minutes(r.get('alarm_time'), response_ts),
    'process_result': _ORDER_RESULT_LABELS.get(result, result),
    'star': star,
  }
  r['work_order'] = {k: v for k, v in wo.items() if v not in (None, '')}


def _order_summary(row: dict) -> dict:
  """仓库返回的一行聚合 → 模型要的嵌套形状（只在有值时才出现子块）。"""
  total = int(row.get('total') or 0)
  out: dict[str, Any] = {'total': total}
  by_status = {}
  for key, label in (('processing', '处理中'), ('handled', '已处理')):
    n = int(row.get(key) or 0)
    if n:
      by_status[label] = n
  unknown = total - sum(by_status.values())
  if unknown:
    by_status['未知'] = unknown
  if by_status:
    out['by_status'] = by_status
    if total:
      out['handled_pct'] = f'{int(row.get("handled") or 0) / total:.0%}'
  by_level = {}
  for key, label in (('urgent', '紧急'), ('important', '重要'),
                     ('general', '一般'), ('hint', '提示')):
    n = int(row.get(key) or 0)
    if n:
      by_level[label] = n
  if by_level:
    out['by_level'] = by_level
  samples = int(row.get('resp_samples') or 0)
  if samples:
    resp: dict[str, Any] = {'samples': samples}
    for key, label in (('resp_avg_min', 'avg'), ('resp_p50_min', 'p50'),
                       ('resp_p90_min', 'p90')):
      value = row.get(key)
      if value is not None:
        resp[label] = round(float(value), 1)
    out['response_minutes'] = resp
  return out


def _minutes_arg(params: dict, key: str) -> int | None:
  """正数分钟参数（`response_timeout_min` / `unresponded_min`）。

  缺失 → None；给了但非法 → 报错。**非法值绝不退化成「不过滤」**：那样
  「超过 5 分钟的单」会返回全量，模型看到结果只会以为都超了 —— 比报错更坏。
  """
  raw = str(params.get(key) or '').strip()
  if not raw:
    return None
  try:
    value = int(float(raw))
  except (TypeError, ValueError):
    raise DcimMetaError(f'{key} 需为大于 0 的整数（分钟），收到：{raw}') from None
  if value <= 0:
    raise DcimMetaError(f'{key} 需为大于 0 的整数（分钟），收到：{raw}')
  return value


def _timeout_minutes(params: dict) -> int | None:
  """「已响应但慢」的分钟数（工单主表）。"""
  return _minutes_arg(params, 'response_timeout_min')


def _unresponded_minutes(params: dict) -> int | None:
  """「还没人响应」的分钟数（告警主表，无任何工单）。"""
  return _minutes_arg(params, 'unresponded_min')


def _query_alarm_orders(params: dict) -> dict:
  """**告警工单**（人工处置记录）清单 + 整个过滤集的汇总。

  与 `query_alarms` 的分工：那个回答「告警在不在跑」，这里回答「这条告警处理了吗、
  谁处理的、多久响应、处理结果是什么」。`event_log_id` 是两者的连接键。

  ⚠️ 工单是「人工处置记录」，**不是**告警生命周期 —— 同一 `orderNo` 多次推送
  （处理中 → 已处理）已 upsert 成一行，所以 `total` 是**工单数**，不是推送次数。

  `response_timeout_min` = 只看**响应晚了**的单：以工单为主表，`response_time - alarm_time`
  超过该分钟数。**尚无响应时刻的工单（响应为 0 / 早于告警的脏数据）不参与**
  —— 它们不是「响应超时」，是「还没响应」，口径不同不能混在一起。
  """
  cluster = _scope(params)
  limit, offset = _page(params, default_size=100)
  space = str(params['space']) if params.get('space') else None
  filters = dict(
    space_guid=str(params['space_guid']) if params.get('space_guid') else None,
    device_guid=str(params['device_guid']) if params.get('device_guid') else None,
    keyword=str(params['keyword']) if params.get('keyword') else None,
    level=str(params['level']) if params.get('level') else None,
    # 默认全部：问「处理得怎么样」时只给处理中会漏掉已处理的大头
    status=str(params.get('status') or 'all'),
    operator=str(params['operator']) if params.get('operator') else None,
    event_log_id=str(params['event_log_id']) if params.get('event_log_id') else None,
    start_s=_epoch_seconds(params.get('start_ms')),
    end_s=_epoch_seconds_end(params.get('end_ms')),
    response_timeout_min=_timeout_minutes(params),
  )
  resolved = None
  with _Backend() as b:
    if space and not filters['space_guid']:
      resolved = _resolve_space_guid(b, cluster, space)
      filters['space_guid'] = str(resolved.get('guid') or '') or None
    total = b.pg.count_alarm_orders(cluster, **filters)
    records = b.pg.find_alarm_orders(cluster, limit=limit, offset=offset, **filters)
    # 汇总按**整个过滤集**算（工具层会把 page_size 钳到 20，翻页算统计既慢又易错）
    summary = b.pg.alarm_order_summary(cluster, **filters)
  out = _paged([_slim_order(r) for r in records], total)
  out['summary'] = _order_summary(summary)
  echo = _echo_space(resolved)
  if echo:
    out['space'] = echo
  return out


_HANDLERS = {
  'dcim_meta.query_space_tree': _query_space_tree,
  'dcim_meta.query_devices': _query_devices,
  'dcim_meta.query_points': _query_points,
  'dcim_meta.query_series': _query_series,
  'dcim_meta.query_pue': _query_pue,
  'dcim_meta.query_alarms': _query_alarms,
  'dcim_meta.query_alarm_orders': _query_alarm_orders,
  'dcim_meta.query_over_threshold': _query_over_threshold,
  'dcim_meta.check_consistency': _check_consistency,
}


async def call(
  capability_id: str,
  params: dict | None = None,
  *,
  on_behalf_of: str | None = None,
) -> dict:
  """能力入口。`on_behalf_of` 是平台统一契约的一部分（DCIM 是设备数据、无按人隔离）。"""
  handler = _HANDLERS.get(capability_id)
  if handler is None:
    raise DcimMetaError(f'未知能力：{capability_id}')
  if not settings.DCIM_PG_DSN:
    raise DcimMetaError('DCIM_PG_DSN 未配置，无法查询元数据')
  # 阻塞 IO 丢到线程，别卡事件循环（这条链路在 SSE 对话里）
  return await asyncio.to_thread(handler, dict(params or {}))

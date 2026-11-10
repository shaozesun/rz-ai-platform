"""DCIM 元数据落库：当前态 upsert + 变更历史（同事务），失败进 DLQ。

- 驱动：psycopg3（同步；worker 每个消费线程各自持一条连接）
- 语义：at-least-once。**同一事务**写"当前态表 + 历史表"，成功后调用方再提交 Kafka offset
- 幂等：当前态按**复合主键 (cluster, guid)** upsert；重复消费不会产生重复行
  （历史表按次追加，属预期）
- 园区维度：cluster 来自消息所在的 Kafka 集群（由消费端经 meta 传入）——
  不同园区的同 guid 记录互不覆盖
- 删除：语义待与 DCIM 确认，当前按软删（is_deleted/deleted_at），并在历史表记 op='delete'
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from service.dcim_ingest.models import ParsedRecord, TABLES

logger = logging.getLogger(__name__)


def _quote(cols: list[str]) -> str:
  # 列名均为代码内常量（guid/name/...），此处仅做引号包裹
  return ', '.join(f'"{c}"' for c in cols)


def _float_or_none(value: Any) -> float | None:
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _int_or_none(value: Any) -> int | None:
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


# 短代号（A1 / A12 / M1 / B2）：关键词若整体形如「1~3 字母 + 1~3 数字」，按**词边界**匹配。
# 否则 `ILIKE '%A1%'` 会把 A18/A12 一起带进来（空间全名是「廊坊园区A18数据中心…」，无分隔符）。
_CODE_KW = re.compile(r'[A-Za-z]{1,3}\d{1,3}')


def _kw_match(col: str, kw: str) -> tuple[str, list]:
  """keyword → 单列匹配片段。短代号按词边界（A1 不命中 A18），其余维持子串 ILIKE。

  `~*` 是大小写不敏感正则，与原 `ILIKE` 语义一致；`kw` 已由 `_CODE_KW` 限定为纯字母数字，
  拼进正则无注入风险。`M1-3F`、`201机房`、`温度` 等不满足 `fullmatch`，行为逐字不变。
  """
  if _CODE_KW.fullmatch(kw):
    return f'{col} ~* %s', [f'(^|[^0-9]){kw}([^0-9]|$)']
  return f'{col} ILIKE %s', [f'%{kw}%']


def _kw_any(kw: str, *cols: str) -> tuple[str, list]:
  """多列 OR 的关键词条件（`count` 与列表查询共用，total 与明细天然一致）。"""
  frags: list[str] = []
  args: list = []
  for col in cols:
    sql, vals = _kw_match(col, kw)
    frags.append(sql)
    args += vals
  return ' OR '.join(frags), args


def _kw_hit(text: str, kw: str) -> bool:
  """`_kw_match` 的 Python 侧镜像 —— 空间名解析与 SQL 关键词必须**同一套语义**。

  短代号（`A1`）按数字边界，避免命中 `A10`/`A18`；其余仍是大小写不敏感子串。
  两处若漂移，就是「按名称解析到的节点」和「按关键词搜到的行」对不上。
  """
  if not text or not kw:
    return False
  if _CODE_KW.fullmatch(kw):
    pat = f'(^|[^0-9]){re.escape(kw)}([^0-9]|$)'
    return re.search(pat, text, re.IGNORECASE) is not None
  return kw.lower() in text.lower()


# 空间名多词分隔：空格 / 顿号 / 逗号 / 分号（「A1 101机房」「A1、101机房」都是同一栋楼的房间）
_SPACE_SPLIT = re.compile(r'[\s、,，;；]+')

# 空间树根节点在 `parent_guid` 上的哨兵值（顶层空间的父指针，不是真实节点）
_ROOT_PARENT = '9999_9999_9999_9999'


def _space_roots(rows: list[dict], space: str) -> list[dict]:
  """空间名 → **顶层命中节点**（纯函数，不连库；入参是 `{cluster, guid, name, parent_guid}`）。

  多条规则，都是为「A1」这种口语短代号服务的：

  - **多词 AND**：「A1 101机房」拆成两个词，**都命中**才算候选。单个词按 `_kw_hit` 匹配
    （短代号有数字边界，`A1` 不会串到 `A10`/`A18`）。
  - **祖先折叠**：命中的节点若其祖先也在候选里，丢弃它、只留最上层那个。
    否则「A1」会同时命中 A1 楼和它下面每个房间（几十个），上层无法判断该用哪个 guid。
  - **单代号优先认楼**：房间名常以代号结尾（实测 `廊坊园区A12数据中心配电区域三层电池室A1`），
    只按数字边界仍会跟着命中。所以「整个输入是一个短代号」时，优先取名字含
    `<代号>数据中心` 的那个（楼的标准命名）；没有或有多个才保留全部候选。
  - **不做模糊猜测**：0 个/多个候选都原样交给调用层处理（报错或回候选），
    这里绝不挑一个 —— 猜错就是从「整园区」变成「另一栋楼」，比报错更坏。
  """
  tokens = [t for t in _SPACE_SPLIT.split(str(space or '').strip()) if t]
  if not tokens or not rows:
    return []
  hits = [
    r for r in rows
    if all(_kw_hit(str(r.get('name') or ''), t) for t in tokens)
  ]
  if len(hits) <= 1:
    return hits

  by_guid = {(r.get('cluster'), r.get('guid')): r for r in rows}
  hit_keys = {(r.get('cluster'), r.get('guid')) for r in hits}
  roots: list[dict] = []
  for r in hits:
    cluster, guid = r.get('cluster'), r.get('guid')
    parent = r.get('parent_guid')
    seen = {(cluster, guid)}
    is_child = False
    while parent and parent != _ROOT_PARENT:
      pkey = (cluster, parent)
      if pkey in seen:          # 脏数据成环：当根处理，别死循环
        break
      if pkey in hit_keys:      # 祖先是候选 → 本节点是子节点
        is_child = True
        break
      seen.add(pkey)
      prow = by_guid.get(pkey)
      if prow is None:          # 祖先行不在本次取数范围（跨园区/已删）→ 当根
        break
      parent = prow.get('parent_guid')
    if not is_child:
      roots.append(r)

  if len(roots) > 1 and len(tokens) == 1 and _CODE_KW.fullmatch(tokens[0]):
    # 楼的标准命名是「<园区>园区<代号>数据中心」（以此为结尾）；模组 `…A1数据中心M1模组`、
    # 房间里的 `…电池室A1` 都不是楼
    buildings = [r for r in roots if str(r.get('name') or '').endswith(f'{tokens[0]}数据中心')]
    if buildings:                 # 有楼就只在楼里选（跨园区多个楼 = 真正的歧义）
      return buildings
  return roots


class DcimRepository:
  """一个连接（或从池里借一个）。**不要在多个线程间共享同一个实例**。

  `pool` 给定时从池里借（用完归还），否则自建连接 ——
  差别只在**连接开销**：`psycopg.connect()` 每次要 TCP 握手 + 认证（本机约几毫秒，
  跨机几十毫秒），而「每次查询新建连接」在**查询量上来时就是第一瓶颈**。
  摄入侧是「一条线程一条长连接」，池主要给能力层（一次对话几次查询、每次一个 `_Backend`）用。
  """

  def __init__(self, dsn: str, pool: Any = None):
    self._dsn = dsn
    self._pool = pool
    self._conn: psycopg.Connection | None = None
    self._stack: contextlib.ExitStack | None = None

  # ── 连接 ──
  def connect(self) -> None:
    self._stack = contextlib.ExitStack()
    if self._pool is not None:
      # 借出的连接由 ExitStack 在 close() 时归还（contextlib 管生命周期，避免手写 __enter__/__exit__）
      self._conn = self._stack.enter_context(self._pool.connection())
    else:
      self._conn = psycopg.connect(self._dsn, row_factory=dict_row)
      self._conn.autocommit = False

  def close(self) -> None:
    """自建连接直接关；池里借的**归还**（`ExitStack.close()` 会调 `pool.connection()` 的退出）。"""
    try:
      if self._conn is not None and self._pool is None:
        self._conn.close()
    finally:
      self._conn = None
      if self._stack is not None:
        self._stack.close()
        self._stack = None

  @property
  def conn(self) -> psycopg.Connection:
    if self._conn is None:
      self.connect()
    assert self._conn is not None
    return self._conn

  def commit(self) -> None:
    self.conn.commit()

  def rollback(self) -> None:
    self.conn.rollback()

  # ── 写库 ──
  def apply_batch(
    self,
    kind: str,
    items: list[tuple[ParsedRecord, dict[str, Any]]],
  ) -> None:
    """一个事务内：逐条 upsert 当前态 +（仅告警类）追加历史。items: [(record, meta)]。

    meta 需含 cluster / topic / partition / offset（cluster 落库、其余为历史表溯源）。
    cluster = 园区名，来自消息所在的 Kafka 集群 —— 只有消费端知道，故从 meta 取。
    """
    if not items:
      return
    cur_table, hist_table = TABLES[kind]
    conn = self.conn
    with conn.cursor() as cur:
      for rec, meta in items:
        cluster = meta['cluster']
        if rec.op == 'delete':
          cur.execute(
            f'UPDATE "{cur_table}" SET is_deleted = TRUE, deleted_at = now(),'
            f' updated_at = now(), payload = %s WHERE cluster = %s AND guid = %s',
            (Jsonb(rec.payload), cluster, rec.guid),
          )
        else:
          cols = ['cluster', 'guid', *rec.values.keys(), 'payload']
          placeholders = ', '.join(['%s'] * len(cols))
          updates = ', '.join(
            f'"{c}" = EXCLUDED."{c}"'
            for c in [*rec.values.keys(), 'payload']
          )
          params = [
            cluster,
            rec.guid,
            *[Jsonb(v) if isinstance(v, (dict, list)) else v for v in rec.values.values()],
            Jsonb(rec.payload),
          ]
          cur.execute(
            f'INSERT INTO "{cur_table}" ({_quote(cols)}) VALUES ({placeholders})'
            f' ON CONFLICT (cluster, guid) DO UPDATE SET {updates},'
            f' is_deleted = FALSE, deleted_at = NULL, updated_at = now()',
            params,
          )

        # 配置类（空间/设备/点位）的历史表已移除（TABLES 里为 None），只有告警类还写历史
        if hist_table:
          cur.execute(
            f'INSERT INTO "{hist_table}"'
            f' (cluster, guid, op, payload, valid_from, kafka_topic, partition, "offset")'
            f' VALUES (%s, %s, %s, %s, now(), %s, %s, %s)'
            # 同一条 Kafka 消息重复处理时不重复追加历史（重放/补数安全）
            f' ON CONFLICT (cluster, kafka_topic, partition, "offset") DO NOTHING',
            (
              cluster,
              rec.guid,
              rec.op,
              Jsonb(rec.payload),
              meta.get('topic'),
              meta.get('partition'),
              meta.get('offset'),
            ),
          )
    conn.commit()

  def record_dlq(
    self,
    *,
    cluster: str | None,
    topic: str | None,
    partition: int | None,
    offset: int | None,
    error: str,
    raw: str,
  ) -> None:
    """解析失败的消息进 DLQ，不阻塞后续批次。"""
    conn = self.conn
    with conn.cursor() as cur:
      cur.execute(
        'INSERT INTO dcim_dlq (cluster, topic, partition, "offset", error, raw)'
        ' VALUES (%s, %s, %s, %s, %s, %s)',
        (cluster, topic, partition, offset, error[:2000], raw[:4000]),
      )
    conn.commit()

  # ── 查询（供联调与后续能力层复用） ──
  def count(self, table: str, cluster: str | None = None) -> int:
    with self.conn.cursor() as cur:
      if cluster:
        cur.execute(f'SELECT count(*) AS n FROM "{table}" WHERE cluster = %s', (cluster,))
      else:
        cur.execute(f'SELECT count(*) AS n FROM "{table}"')
      row = cur.fetchone()
      return int(row['n']) if row else 0

  def space_tree(self, cluster: str, guid: str) -> list[dict]:
    """递归 CTE 查空间树（数据中心 → 楼宇 → 房间）。**按园区隔离**。"""
    with self.conn.cursor() as cur:
      cur.execute(
        'WITH RECURSIVE t AS ('
        '  SELECT * FROM dcim_spaces WHERE cluster = %s AND guid = %s'
        '  UNION ALL'
        '  SELECT s.* FROM dcim_spaces s JOIN t'
        '    ON s.parent_guid = t.guid AND s.cluster = t.cluster'
        ') SELECT cluster, guid, name, parent_guid, idc FROM t',
        (cluster, guid),
      )
      return list(cur.fetchall())

  # ── 时序库（TDengine）建模注册表 ──
  # 实时报文里**没有 device_type**，而宽表要按设备类型选超级表 → 写入端必须来 PG 查档案。
  def devices_meta_rows(
    self, guids: list[str], cluster: str | None = None,
  ) -> list[dict]:
    """批量取设备的建模信息，**一行一台设备**：`[{guid, cluster, device_type, space_guid}]`。

    ⚠️ **guid 只在园区内唯一**（PG 主键 `(cluster, guid)`，实测 652 个 guid 跨园区重复）：
    - 写入侧（`WideWriter`）**必须传 cluster**，否则重复 guid 命中哪条档案是随机的；
    - 读侧按查询给的园区传 cluster，跨园区查询才传 None —— 此时同名 guid 会返回**多行**，
      调用方必须按 `(cluster, guid)` 区分，不能压成 dict。
    """
    if not guids:
      return []
    sql = 'SELECT guid, cluster, device_type, space_guid FROM dcim_devices WHERE guid = ANY(%s)'
    params: list = [list(guids)]
    if cluster:
      sql += ' AND cluster = %s'
      params.append(cluster)
    with self.conn.cursor() as cur:
      cur.execute(sql, tuple(params))
      return [dict(r) for r in cur.fetchall()]

  def devices_meta(self, guids: list[str], cluster: str | None = None) -> dict[str, dict]:
    """批量取设备的建模信息：{guid: {device_type, space_guid}}。

    **只在身份唯一时可用**（写入侧永远传 cluster；单园区读侧也唯一）。
    跨园区查询请改用 `devices_meta_rows()` —— 这里有同名 guid 会由 dict 后者覆盖前者。
    """
    return {r['guid']: r for r in self.devices_meta_rows(guids, cluster)}

  def td_columns(self, device_type: str) -> dict[str, str]:
    """已注册的列映射 {td_column: point_code}（列顺序由 TDengine 的 DESCRIBE 决定，不在这里）。"""
    with self.conn.cursor() as cur:
      cur.execute(
        'SELECT td_column, point_code FROM dcim_td_column WHERE device_type = %s',
        (device_type,),
      )
      return {r['td_column']: r['point_code'] for r in cur.fetchall()}

  def td_columns_by_group(self, device_type: str) -> dict[str, dict[str, str]]:
    """按测点分组返回列映射 `{point_group: {point_code: td_column}}`（读侧按组分别查表用）。"""
    with self.conn.cursor() as cur:
      cur.execute(
        'SELECT point_code, td_column, point_group FROM dcim_td_column'
        ' WHERE device_type = %s ORDER BY point_group, point_code',
        (device_type,),
      )
      out: dict[str, dict[str, str]] = {}
      for r in cur.fetchall():
        out.setdefault(r['point_group'], {})[r['point_code']] = r['td_column']
      return out

  def allocate_columns_locked(
    self, device_type: str, codes: list[str], *, commit: bool = True,
  ) -> dict[str, tuple[str, str]]:
    """**原子地**「读现有列映射 → 给新编码分配列名与分组 → 落库」，
    返回全量 `{point_code: (point_group, td_column)}`。

    ⚠️ 为什么必须原子：并发下两条线程给**同一个 device_type** 加列时，若各自先读到旧快照、
      再在内存里分配，会给**不同**的 point_code 分到**同一个**归一化列名 →
      第二个 INSERT 撞 `UNIQUE(device_type, td_column)` → 整批失败；
      而实时数据是 **auto-commit**，失败那批就**丢了**。

    用 `pg_advisory_xact_lock` 把「同一 device_type 的分配」串起来 ——
    **不同 device_type 各一把锁，互不阻塞**。事务结束时锁自动释放。

    **分组（治 TDengine 4,096 列上限）**：每组最多 `GROUP_MAX_COLUMNS` 个测点列。
    存量编码**永不换组**（历史数据按组落在各自的超级表里），新编码按 g0→g1→… 顺序填、
    装满换下一组 —— `00AG`/`00WG` 已超容量线，它们的新测点直接落 g1。

    `commit=False` 时由调用方决定提交时机（见 `wide_writer._ensure_columns`：**先建
    TDengine 表、再提交映射**）。分配结果是**确定性**的（`sorted(codes)` + 已知集合），
    所以回滚后重放会分到同样的列名与分组。
    """
    from service.dcim_ingest.td_schema import (
      GROUP_MAX_COLUMNS, GROUP_WARN_COLUMNS, allocate_columns, next_group, stable_name,
    )

    with self.conn.cursor() as cur:
      # 建议锁的键用 device_type 派生；xact 级 → 事务结束自动释放
      cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (f'dcim_td:{device_type}',))
      cur.execute(
        'SELECT point_code, td_column, point_group FROM dcim_td_column WHERE device_type = %s',
        (device_type,),
      )
      known: dict[str, tuple[str, str]] = {
        r['point_code']: (r['point_group'], r['td_column']) for r in cur.fetchall()
      }
      counts: dict[str, int] = {}
      for group, _col in known.values():
        counts[group] = counts.get(group, 0) + 1

      new_codes = sorted(c for c in codes if c not in known)
      if new_codes:
        taken = {col for _group, col in known.values()}
        group = 'g0'
        i = 0
        while i < len(new_codes):
          room = GROUP_MAX_COLUMNS - counts.get(group, 0)
          if room <= 0:                      # 本组装满 → 换下一组
            group = next_group(group)
            continue
          chunk = new_codes[i:i + room]
          added = allocate_columns(chunk, taken=taken)
          cur.executemany(
            'INSERT INTO dcim_td_column (device_type, point_code, td_column, point_group)'
            ' VALUES (%s, %s, %s, %s) ON CONFLICT (device_type, point_code) DO NOTHING',
            [(device_type, code, col, group) for code, col in added.items()],
          )
          taken.update(added.values())
          for code, col in added.items():
            known[code] = (group, col)
          counts[group] = counts.get(group, 0) + len(added)
          i += len(chunk)

      # 分组登记：每组一行（每组一张超级表），point_count 是该组的列数
      if counts:
        cur.executemany(
          'INSERT INTO dcim_td_stable (device_type, point_group, stable_name)'
          ' VALUES (%s, %s, %s)'
          ' ON CONFLICT (device_type, point_group) DO UPDATE'
          ' SET stable_name = EXCLUDED.stable_name, updated_at = now()',
          [(device_type, g, stable_name(device_type, g)) for g in sorted(counts)],
        )
        cur.executemany(
          'UPDATE dcim_td_stable SET point_count = %s, updated_at = now()'
          ' WHERE device_type = %s AND point_group = %s',
          [(n, device_type, g) for g, n in sorted(counts.items())],
        )
        for g, n in sorted(counts.items()):
          if n >= GROUP_WARN_COLUMNS:      # 4,096 硬上限前先叫一声，别等 ALTER 抛错
            logger.warning(
              'device_type=%s group=%s 已用 %d/%d 列，接近 TDengine 单表上限',
              device_type, g, n, GROUP_MAX_COLUMNS,
            )
    if commit:
      self.conn.commit()
    return known

  def level_rules(self) -> list[dict]:
    """启用中的测点分级规则（按 priority 升序）。

    给「重要测点」判定用 —— 分级决定哪些测点进业务当前态表、建哪几档聚合
    （见 `service/dcim_ingest/td_schema.py::classify_level`）。规则是**数据**，改规则不发版。
    """
    with self.conn.cursor() as cur:
      cur.execute(
        'SELECT priority, match_on, pattern, level FROM dcim_level_rule'
        ' WHERE enabled ORDER BY priority'
      )
      return list(cur.fetchall())

  # ── 面向能力层的查询（供 service/query/dcim_meta 复用） ──
  # 一律**按园区隔离**：13 个园区的 guid 可能重名，不隔离会串数据。
  #
  # 每个实体是「find_x 取一页 + count_x 取总数」一对。**两者必须共用同一套过滤条件**
  # （下面的 `_x_filters`）—— 否则 total 与数据对不上，上层（`analysis_load`）翻页会漏或重复。
  # ORDER BY 一律**带唯一列做 tiebreaker**（guid）—— 排序列有重复值时，
  # 不加 tiebreaker 会让 LIMIT/OFFSET 翻页**漏行/重复行**。

  def _space_filters(
    self, cluster: str | None, *, keyword: str | None = None, idc: str | None = None,
    parent_guid: str | None = None, top_only: bool = False,
  ) -> tuple[str, list]:
    conds, args = self._cluster_clause('cluster', cluster)
    conds.append('NOT is_deleted')
    if keyword:
      frags, kargs = _kw_any(keyword, 'name', 'path')
      conds.append(f'({frags})')
      args += kargs
    if idc:
      conds.append('idc = %s')
      args.append(idc)
    if parent_guid:
      conds.append('parent_guid = %s')
      args.append(parent_guid)
    elif top_only:
      conds.append('(parent_guid IS NULL OR parent_guid = %s)')
      args.append('')
    return ' AND '.join(conds), args

  def find_spaces(
    self, cluster: str, *, keyword: str | None = None, idc: str | None = None,
    parent_guid: str | None = None, top_only: bool = False,
    limit: int = 200, offset: int = 0,
  ) -> list[dict]:
    """列空间（按名称关键词 / idc / 父节点筛）。名称/路径都是模糊匹配。

    `top_only`：只看**顶层空间**（parent_guid 为空）—— 不给关键词时的默认视角。
    """
    where, args = self._space_filters(
      cluster, keyword=keyword, idc=idc, parent_guid=parent_guid, top_only=top_only,
    )
    with self.conn.cursor() as cur:
      cur.execute(
        'SELECT cluster, guid, name, path, idc, space_type, parent_guid'
        f' FROM dcim_spaces WHERE {where}'
        ' ORDER BY path NULLS LAST, name, guid LIMIT %s OFFSET %s',
        [*args, limit, offset],
      )
      return list(cur.fetchall())

  def count_spaces(
    self, cluster: str, *, keyword: str | None = None, idc: str | None = None,
    parent_guid: str | None = None, top_only: bool = False,
  ) -> int:
    where, args = self._space_filters(
      cluster, keyword=keyword, idc=idc, parent_guid=parent_guid, top_only=top_only,
    )
    return self._scalar(f'SELECT count(*) AS n FROM dcim_spaces WHERE {where}', args)

  def find_space_roots(self, cluster: str | list[str] | None, space: str) -> list[dict]:
    """空间名 → 顶层命中节点（`_space_roots` 的取数外壳）。

    **整园区空间一次取回、在内存里算**（全库实测仅 2,206 行）：名称不保证包含父名，
    按 `name ILIKE` 只能命中自己那一层，判断不了「谁是谁的祖先」。
    `cluster` 为空 = 跨所有园区取 —— 这样同一短代号在多个园区都有时才能报歧义。
    """
    conds, args = self._cluster_clause('cluster', cluster)
    conds.append('NOT is_deleted')
    with self.conn.cursor() as cur:
      cur.execute(
        'SELECT cluster, guid, name, parent_guid FROM dcim_spaces'
        f' WHERE {" AND ".join(conds)}',
        args,
      )
      rows = list(cur.fetchall())
    return _space_roots(rows, space)

  # 设备/点位查询的**共用 SQL 构造**：`find_x` 与 `count_x` 必须走同一个函数 ——
  # 否则 total 与数据对不上，上层翻页会漏或重复。

  _DEVICE_COLS = (
    'd.cluster, d.guid, d.name, d.path, d.idc, d.device_type, d.device_name,'
    ' d.space_guid, d.extend_position'
  )
  _POINT_COLS = (
    'p.cluster, p.guid, p.point_id, p.name, p.idc, p.point_type, p.unit, p.status_map,'
    ' p.device_guid, p.hint_alarm, p.general_alarm, p.important_alarm, p.urgent_alarm,'
    # 带上所属设备名/类型：类型是查时序时**选超级表**的依据（宽表按设备类型分表）
    ' d.name AS device_name, d.device_type'
  )
  # 递归子空间 CTE：空间树是 园区→楼宇→房间→…，按单一 guid 精确匹配会**漏掉子空间**里的设备
  _SUBTREE_CTE = (
    'WITH RECURSIVE sub AS ('
    '  SELECT cluster, guid FROM dcim_spaces WHERE cluster = %s AND guid = %s'
    '  UNION ALL'
    '  SELECT s.cluster, s.guid FROM dcim_spaces s'
    '    JOIN sub ON s.parent_guid = sub.guid AND s.cluster = sub.cluster'
    ') '
  )
  # 不带园区时的同款 CTE：空间 guid **全局唯一**（实测形如 `RZYWBXSJ-P006-…`），只按 guid 锚定即可
  _SUBTREE_CTE_NO_CLUSTER = (
    'WITH RECURSIVE sub AS ('
    '  SELECT cluster, guid FROM dcim_spaces WHERE guid = %s'
    '  UNION ALL'
    '  SELECT s.cluster, s.guid FROM dcim_spaces s'
    '    JOIN sub ON s.parent_guid = sub.guid AND s.cluster = sub.cluster'
    ') '
  )

  @staticmethod
  def _cluster_clause(col: str, cluster: str | list[str] | None) -> tuple[list[str], list]:
    """园区过滤条件。**`cluster` 为空则不拼** —— 表示「跨所有园区查」。

    为什么允许跨园区（2026-10-13）：模型**无从知道哪个园区有数据**，只能拿代号清单
    一个个试；而它有"一批并行发几条"的自我限制 → **试到一半就停**，数据在后面的园区
    会被漏掉（实测给了 11 个代号，模型只扫了前 6 个，恰好有数据的排第 3 才蒙对）。
    不传园区时记录**自带 `cluster` 列**，模型一眼看得出数据在哪个园区。

    **列表 = 「一个园区下的多个代号」**（调用层的 `park` 展开，如廊坊 → 廊坊A/B1M1/B1M2），
    走 `= ANY`；空列表等价于不传（跨园区）。
    """
    if isinstance(cluster, (list, tuple)):
      items = [c for c in cluster if c]
      if not items:
        return [], []
      return [f'{col} = ANY(%s)'], [items]
    if not cluster:
      return [], []
    return [f'{col} = %s'], [cluster]

  def _subtree_cte(self, cluster: str | list[str] | None, space_guid: str) -> tuple[str, list]:
    """递归子空间 CTE + 它的参数（无单个园区时只按 guid 锚定）。

    `cluster` 是列表（园区级筛选）时走 NO_CLUSTER 版：空间 guid **全局唯一**（实测形如
    `RZYWBXSJ-P006-…`），锚定后 join 仍按 `cluster` 对齐，外层的 `= ANY` 再收敛园区。
    """
    if isinstance(cluster, str) and cluster:
      return self._SUBTREE_CTE, [cluster, space_guid]
    return self._SUBTREE_CTE_NO_CLUSTER, [space_guid]

  def _devices_sql(
    self, cluster: str | None, *, space_guid: str | None = None, subtree: bool = True,
    device_type: str | None = None, keyword: str | None = None, count: bool = False,
  ) -> tuple[str, list]:
    """设备查询 SQL。`subtree=True` 时 `space_guid` 按**递归子空间**匹配（含包间/机柜）。

    `cluster` 为空 = **跨所有园区**（记录自带 `cluster` 列）。
    """
    args: list = []
    cte = join = ''
    if space_guid and subtree:
      cte, args = self._subtree_cte(cluster, space_guid)
      join = ' JOIN sub ON d.cluster = sub.cluster AND d.space_guid = sub.guid'
    conds, extra = self._cluster_clause('d.cluster', cluster)
    args += extra
    conds.append('NOT d.is_deleted')
    if space_guid and not subtree:
      conds.append('d.space_guid = %s')
      args.append(space_guid)
    if device_type:
      conds.append('d.device_type = %s')
      args.append(device_type)
    if keyword:
      frags, kargs = _kw_any(keyword, 'd.name', 'd.path')
      conds.append(f'({frags})')
      args += kargs
    sel = 'count(*) AS n' if count else self._DEVICE_COLS
    return f'{cte}SELECT {sel} FROM dcim_devices d{join} WHERE {" AND ".join(conds)}', args

  def find_devices(
    self, cluster: str, *, space_guid: str | None = None, subtree: bool = True,
    device_type: str | None = None, keyword: str | None = None,
    limit: int = 200, offset: int = 0,
  ) -> list[dict]:
    """列设备台账（按空间 / 设备类型 / 名称关键词筛）。空间默认含**子空间**。"""
    sql, args = self._devices_sql(
      cluster, space_guid=space_guid, subtree=subtree,
      device_type=device_type, keyword=keyword,
    )
    with self.conn.cursor() as cur:
      cur.execute(f'{sql} ORDER BY d.name, d.guid LIMIT %s OFFSET %s', [*args, limit, offset])
      return list(cur.fetchall())

  def count_devices(
    self, cluster: str, *, space_guid: str | None = None, subtree: bool = True,
    device_type: str | None = None, keyword: str | None = None,
  ) -> int:
    sql, args = self._devices_sql(
      cluster, space_guid=space_guid, subtree=subtree,
      device_type=device_type, keyword=keyword, count=True,
    )
    return self._scalar(sql, args)

  def device_types(self, cluster: str | None, *, space_guid: str | None = None) -> list[str]:
    """该空间下出现过的设备类型。`space_guid` 给定时按**递归子空间**（与 `find_devices` 同口径）。

    跨类型时序查询要用它：**超级表按设备类型分**，不知道类型就不知道该打哪几张表。
    `cluster` 为空 = 跨所有园区。
    """
    cte = join = ''
    if space_guid:
      cte, args = self._subtree_cte(cluster, space_guid)
      join = ' JOIN sub ON d.cluster = sub.cluster AND d.space_guid = sub.guid'
    else:
      args = []
    conds, extra = self._cluster_clause('d.cluster', cluster)
    args += extra
    conds.append('NOT d.is_deleted')
    conds.append('d.device_type IS NOT NULL AND d.device_type <> %s')
    args.append('')
    sql = (f'{cte}SELECT DISTINCT d.device_type FROM dcim_devices d{join}'
           f' WHERE {" AND ".join(conds)}')
    with self.conn.cursor() as cur:
      cur.execute(sql, args)
      return [r['device_type'] for r in cur.fetchall()]

  def device_guids(self, cluster: str, *, device_type: str | None = None) -> list[str]:
    """该范围下**全部**设备 guid（一致性巡检用）。

    ⚠️ 不能拿 `find_devices` 凑：它有 limit，巡检写 `limit=100000` 只是"看起来没截断"，
    真超过就**静默丢** —— 总表有、时序库也有的设备会被误报成"只有定义没有数值"。
    **跨园区时 guid 会重复**，要区分设备请用 `device_keys()`。
    """
    conds, args = self._cluster_clause('cluster', cluster)
    conds.append('NOT is_deleted')
    sql = f'SELECT guid FROM dcim_devices WHERE {" AND ".join(conds)}'
    if device_type:
      sql += ' AND device_type = %s'
      args.append(device_type)
    with self.conn.cursor() as cur:
      cur.execute(sql, args)
      return [r['guid'] for r in cur.fetchall()]

  def device_keys(
    self, cluster: str, *, device_type: str | None = None,
  ) -> list[tuple[str, str]]:
    """该范围下**全部** `(cluster, guid)`（跨园区一致性巡检用）。

    guid 只在园区内唯一 —— 只比 guid 集合时，两个园区各有一台同名设备、
    其中一个在时序库缺数据，集合差集是空，缺失被**掩盖**。
    """
    conds, args = self._cluster_clause('cluster', cluster)
    conds.append('NOT is_deleted')
    sql = f'SELECT cluster, guid FROM dcim_devices WHERE {" AND ".join(conds)}'
    if device_type:
      sql += ' AND device_type = %s'
      args.append(device_type)
    with self.conn.cursor() as cur:
      cur.execute(sql, args)
      return [(r['cluster'], r['guid']) for r in cur.fetchall()]

  def _points_sql(
    self, cluster: str | None, *, space_guid: str | None = None, device_guid: str | None = None,
    keyword: str | None = None, count: bool = False,
  ) -> tuple[str, list]:
    """测点查询 SQL。

    **空间路径是一次 JOIN 出全量**（递归子空间 → 设备 → 点位）——
    这是 PG 同库三张表的能力，不必"先列设备再逐台查点位"。
    非空间路径也 `LEFT JOIN` 设备，让返回形状一致（多带 device_name/device_type）。

    `cluster` 为空 = **跨所有园区**（记录自带 `cluster` 列）。
    """
    args: list = []
    cte = joins = ''
    if space_guid:
      cte, args = self._subtree_cte(cluster, space_guid)
      joins = (' JOIN dcim_devices d ON d.cluster = p.cluster AND d.guid = p.device_guid'
               ' AND NOT d.is_deleted'
               ' JOIN sub ON d.cluster = sub.cluster AND d.space_guid = sub.guid')
    else:
      joins = ' LEFT JOIN dcim_devices d ON d.cluster = p.cluster AND d.guid = p.device_guid'
    conds, extra = self._cluster_clause('p.cluster', cluster)
    args += extra
    conds.append('NOT p.is_deleted')
    if device_guid:
      conds.append('p.device_guid = %s')
      args.append(device_guid)
    if keyword:
      frags, kargs = _kw_any(keyword, 'p.name', 'p.point_id')
      conds.append(f'({frags})')
      args += kargs
    sel = 'count(*) AS n' if count else self._POINT_COLS
    return f'{cte}SELECT {sel} FROM dcim_points p{joins} WHERE {" AND ".join(conds)}', args

  def find_points(
    self, cluster: str, *, space_guid: str | None = None, device_guid: str | None = None,
    keyword: str | None = None, limit: int = 300, offset: int = 0,
  ) -> list[dict]:
    """列测点定义（含单位、状态映射、4 档告警阈值）—— 这些是 TDengine 里没有的。

    传 `space_guid` 时**一次 JOIN 出该空间（含子空间）下的全部测点**。
    """
    sql, args = self._points_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
    )
    with self.conn.cursor() as cur:
      cur.execute(
        f'{sql} ORDER BY d.name, p.device_guid, p.point_id, p.guid LIMIT %s OFFSET %s',
        [*args, limit, offset],
      )
      return list(cur.fetchall())

  def count_points(
    self, cluster: str, *, space_guid: str | None = None, device_guid: str | None = None,
    keyword: str | None = None,
  ) -> int:
    sql, args = self._points_sql(
      cluster, space_guid=space_guid, device_guid=device_guid,
      keyword=keyword, count=True,
    )
    return self._scalar(sql, args)

  # 4 档告警阈值的列名（`_POINT_COLS` 里已带出；超阈值巡检按档位筛"定义了阈值的测点"）
  _THRESHOLD_COLUMNS = (
    ('urgent', 'urgent_alarm'), ('important', 'important_alarm'),
    ('general', 'general_alarm'), ('hint', 'hint_alarm'),
  )

  @staticmethod
  def _alarm_enabled(col: str) -> str:
    """该档「配置了告警」的判据 —— 看 `maxUse`/`minUse` 开关，**不看 `maxAlarm` 是否为 0**。

    语义依据见 `service/query/dcim_meta/client.py::_breach`（生产数据验证）：未配置的档位是
    `maxUse:0, minUse:0, maxAlarm:0, minAlarm:0`；而通断类测点（`HT_Status_Comm`）
    `maxUse:1, maxAlarm:0` 的 **0 是真上限**（0=正常，超过就告警）。
    所以「一律看非零」和「一律看有对象」两种都会判错。

    `->>` 出来是文本，**先过正则再转型** —— 直接 `::numeric` 遇到脏值会让整条查询报错。
    """
    num = r'^-?[0-9]+(\.[0-9]+)?$'
    parts = []
    for key in ('maxUse', 'minUse'):
      raw = f"{col}->>'{key}'"
      parts.append(f"({raw} ~ '{num}' AND ({raw})::numeric <> 0)")
    return '(' + ' OR '.join(parts) + ')'

  def _threshold_filter(self, levels: list[str]) -> str:
    """追加到 `_points_sql` 上的 `AND (...)`：**只留真的配了阈值的测点**。

    不筛的话「超阈值巡检」会把整个空间的测点全拉回来再在应用层丢 ——
    空间一大就是几万行白拉（而且分页会翻在无阈值的点上，命中集看起来忽多忽少）。
    """
    cols = dict(self._THRESHOLD_COLUMNS)
    known = [cols[l] for l in levels if l in cols]
    if not known:
      known = [c for _, c in self._THRESHOLD_COLUMNS]
    return ' AND (' + ' OR '.join(self._alarm_enabled(f'p.{c}') for c in known) + ')'

  def find_points_with_threshold(
    self, cluster: str, *, space_guid: str | None = None, device_guid: str | None = None,
    keyword: str | None = None, levels: list[str] | None = None,
    limit: int = 500, offset: int = 0,
  ) -> list[dict]:
    """定义了告警阈值的测点定义 —— 超阈值巡检的候选集。"""
    sql, args = self._points_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
    )
    sql += self._threshold_filter(levels or [])
    with self.conn.cursor() as cur:
      cur.execute(
        f'{sql} ORDER BY d.name, p.device_guid, p.point_id, p.guid LIMIT %s OFFSET %s',
        [*args, limit, offset],
      )
      return list(cur.fetchall())

  def count_points_with_threshold(
    self, cluster: str, *, space_guid: str | None = None, device_guid: str | None = None,
    keyword: str | None = None, levels: list[str] | None = None,
  ) -> int:
    sql, args = self._points_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword, count=True,
    )
    return self._scalar(sql + self._threshold_filter(levels or []), args)

  # ── 告警（`ems-alarm-data`）────────────────────────────────────────────
  # 三态：1-触发 / 2-刷新 / 3-恢复。**只有 3 是"不跑了"** ——
  # 判「在跑的告警」只能看 `status <> '3'`（刷新态 2 也是还在跑）。
  _ALARM_COLS = (
    'a.cluster, a.guid AS event_log_id, a.space_guid, s.name AS space_name,'
    ' a.status, a.alarm_type,'
    ' a.device_guid, a.device_name, a.point_guid, a.point_name,'
    ' a.alarm_level, a.normal_range, a.content, a.alarm_time, a.alarm_value,'
    # 不返回 `a.updated_at`：那是**我们落库的时间**（Kafka 消费/重推时刻），
    # 不是对方的事件时间，返回只会被模型当成告警时间误读。
    ' a.remove_time, a.remove_value,'
    # 关联的处置工单（若有）：同库 LEFT JOIN LATERAL，客户端收成嵌套 `work_order`
    ' wo.guid AS wo_order_no, wo.status AS wo_status, wo.responder AS wo_responder,'
    ' wo.handler AS wo_handler, wo.response_time AS wo_response_time,'
    ' wo.process_result AS wo_process_result, wo.star AS wo_star'
  )
  # 每告警**最多取最新一单**（LATERAL + LIMIT 1）：同一事件理论上可能有多次建单，
  # 直接 JOIN 会放大行数 → `count_alarms` 与分页都对不上。
  _ALARM_WO_JOIN = (
    ' LEFT JOIN LATERAL ('
    '   SELECT o.guid, o.status, o.responder, o.handler, o.response_time,'
    '          o.process_result, o.star'
    '     FROM dcim_alarm_orders o'
    '    WHERE o.cluster = a.cluster AND o.event_log_id = a.guid AND NOT o.is_deleted'
    '    ORDER BY o.updated_at DESC, o.guid DESC'
    '    LIMIT 1'
    ' ) wo ON TRUE'
  )
  _ALARM_LEVELS = {'提示': 1, '一般': 2, '重要': 3, '紧急': 4, 'hint': 1, 'general': 2, 'important': 3, 'urgent': 4}

  def _alarms_sql(
    self, cluster: str | list[str] | None, *, space_guid: str | None = None,
    device_guid: str | None = None, keyword: str | None = None,
    level: str | None = None, status: str | None = None,
    start_ms: int | None = None, end_ms: int | None = None,
    unresponded_min: int | None = None, count: bool = False,
  ) -> tuple[str, list]:
    """告警查询 SQL。`status` 见 `_alarm_status_filter`。"""
    cte = join = ''
    args: list = []
    if space_guid:
      cte, args = self._subtree_cte(cluster, space_guid)
      join = ' JOIN sub ON a.cluster = sub.cluster AND a.space_guid = sub.guid'
    # 空间全名：`dcim_spaces.name` 本身就是「园区+数据中心+模组+楼层+房间」全称，
    # 报文只带 `space_guid`，名称只能查这张表。`(cluster, guid)` 是主键，1:1 不放大行数；
    # count 与列表必须带同一个 JOIN，否则按地点关键词的计数会与实际不符。
    join += (' LEFT JOIN dcim_spaces s'
             ' ON s.cluster = a.cluster AND s.guid = a.space_guid')
    conds, extra = self._cluster_clause('a.cluster', cluster)
    args += extra
    conds.append('NOT a.is_deleted')
    if device_guid:
      conds.append('a.device_guid = %s')
      args.append(device_guid)
    if status:
      frag, st_args = self._alarm_status_filter(status)
      conds.append(frag)
      args += st_args
    if level:
      lv = self._ALARM_LEVELS.get(str(level).strip().lower()) or self._ALARM_LEVELS.get(str(level).strip())
      if lv:
        conds.append('a.alarm_level >= %s')     # 「该档及以上」
        args.append(lv)
    if keyword:
      frags, kargs = _kw_any(
        keyword, 'a.point_name', 'a.device_name', 'a.content', 's.name',
      )
      conds.append(f'({frags})')
      args += kargs
    if start_ms:
      conds.append('a.alarm_time >= %s')
      args.append(start_ms)
    if end_ms:
      conds.append('a.alarm_time <= %s')
      args.append(end_ms)
    if unresponded_min is not None:
      # 「告警响应超时」= 发生超过 N 分钟 + 仍未解除 + **没有关联该告警的处置工单**。
      # 关联键与 `_ALARM_WO_JOIN` 一致（`o.event_log_id = a.guid`）；人员点响应会自动建单，
      # 所以「查不到工单」就等于「没人响应」，不必再分辨工单里有没有填响应时刻。
      conds.append("a.status <> '3'")
      conds.append('a.alarm_time > 0 AND a.alarm_time <= %s')
      args.append(int(time.time()) - int(unresponded_min) * 60)
      conds.append(
        'NOT EXISTS (SELECT 1 FROM dcim_alarm_orders o'
        ' WHERE o.cluster = a.cluster AND o.event_log_id = a.guid AND NOT o.is_deleted)'
      )
    if count:
      return (
        f'{cte}SELECT count(*) AS n FROM dcim_alarms a{join}'
        f' WHERE {" AND ".join(conds)}'
      ), args
    return (
      f'{cte}SELECT {self._ALARM_COLS} FROM dcim_alarms a{join}{self._ALARM_WO_JOIN}'
      f' WHERE {" AND ".join(conds)}'
    ), args

  def _alarm_status_filter(self, status: str) -> tuple[str, list]:
    """`status` 入参 → SQL 片段。

    接受四种写法：`active`/`在跑的`（= `status <> '3'`）、`recovered`/`已恢复`（= `status = '3'`）、
    `all`/`全部`（不加状态过滤，用于「所有告警」，含已恢复）、
    或具体的 `1`/`2`/`3`（`触发`/`刷新`/`恢复` 同义 —— 结果里 status 已解码成中文，
    模型照着回填很自然）。**默认由调用方给 `active`**。
    """
    s = str(status).strip().lower()
    if s in ('all', '全部', '所有'):
      return 'TRUE', []      # 不能返回空串：调用方会 append 后 join，空串会拼出悬空 AND
    if s in ('active', 'running', '在跑的', '在跑', '未恢复'):
      return "a.status <> '3'", []
    if s in ('recovered', '已恢复', '恢复'):
      return "a.status = '3'", []
    if s in ('触发', '刷新'):
      return 'a.status = %s', [{'触发': '1', '刷新': '2'}[s]]
    if s in ('1', '2', '3'):
      return 'a.status = %s', [s]
    # 认不出就当作"在跑的"，别把整个库倒出来
    return "a.status <> '3'", []

  def find_alarms(
    self, cluster: str | list[str] | None, *, space_guid: str | None = None,
    device_guid: str | None = None, keyword: str | None = None,
    level: str | None = None, status: str | None = 'active',
    start_ms: int | None = None, end_ms: int | None = None,
    unresponded_min: int | None = None,
    limit: int = 200, offset: int = 0,
  ) -> list[dict]:
    """列告警。`status` 默认 `active`（在跑的）。"""
    sql, args = self._alarms_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      level=level, status=status, start_ms=start_ms, end_ms=end_ms,
      unresponded_min=unresponded_min,
    )
    with self.conn.cursor() as cur:
      cur.execute(
        f'{sql} ORDER BY a.alarm_time DESC NULLS LAST, a.guid LIMIT %s OFFSET %s',
        [*args, limit, offset],
      )
      return list(cur.fetchall())

  # 触发态时间 CTE：每个 `(cluster, guid)` 取 history 里第一条 `status='1'` 的 payload
  # `timestamp`（append-only，不可被刷新/恢复推送改写）。与事件统计表的
  # `incident_stats/source.py::_CANDIDATES_SQL` 同一口径；`payload->>'timestamp'` 是文本，
  # 用字符串参数做范围比较（生产已验证），不 CAST，避免脏行把整条查询打挂。
  _TRIGGER_CTE = (
    'WITH trig AS ('
    '  SELECT DISTINCT ON (h.cluster, h.guid) h.cluster, h.guid,'
    "         h.payload->>'timestamp' AS trigger_ts_raw"
    '    FROM dcim_alarms_history h'
    '   WHERE {h_where}'
    '   ORDER BY h.cluster, h.guid, h.ingested_at, h.id'
    ') '
  )

  def find_alarms_by_trigger_time(
    self, clusters: str | list[str], start_s: int, end_s: int, limit: int = 200,
  ) -> list[dict]:
    """按**触发态时间**查候选告警，返回行带 `trigger_ts`（秒级）。

    当前态 `alarm_time`/`alarm_value` 会被刷新(2)/恢复(3)推送覆盖（`apply_batch`
    整行 upsert），晚到的通报按 ±120s 查当前态会漏掉对应告警。优先读历史表第一条
    `status='1'` 的 `payload->>'timestamp'`；没有历史行的老告警回退当前态 `alarm_time`
    （与原查询逐行等价）。`trigger_ts` 供调用方算 `delta` 与展示时间。
    """
    cluster_list = [clusters] if isinstance(clusters, str) else list(clusters or [])
    cluster_list = [c for c in cluster_list if c]
    if not cluster_list or not start_s or not end_s:
      return []
    h_conds, h_args = self._cluster_clause('h.cluster', cluster_list)
    a_conds, a_args = self._cluster_clause('a.cluster', cluster_list)
    h_where = ' AND '.join(h_conds + [
      "h.payload->>'status' = '1'",
      "h.payload->>'timestamp' BETWEEN %s AND %s",
    ])
    a_where = ' AND '.join(a_conds + [
      'NOT a.is_deleted',
      'a.alarm_time BETWEEN %s AND %s',
      'NOT EXISTS (SELECT 1 FROM dcim_alarms_history h'
      ' WHERE h.cluster = a.cluster AND h.guid = a.guid)',
    ])
    space_join = (' LEFT JOIN dcim_spaces s'
                  ' ON s.cluster = a.cluster AND s.guid = a.space_guid')
    sql = (
      self._TRIGGER_CTE.format(h_where=h_where)
      + f'SELECT {self._ALARM_COLS}, t.trigger_ts_raw'
      + '  FROM trig t'
      + '  JOIN dcim_alarms a ON a.cluster = t.cluster AND a.guid = t.guid'
      + f'{space_join}{self._ALARM_WO_JOIN}'
      + ' WHERE NOT a.is_deleted'
      + ' UNION ALL '
      + f'SELECT {self._ALARM_COLS}, CAST(a.alarm_time AS text) AS trigger_ts_raw'
      + f'  FROM dcim_alarms a{space_join}{self._ALARM_WO_JOIN}'
      + f' WHERE {a_where}'
      + ' ORDER BY trigger_ts_raw DESC NULLS LAST, event_log_id'
      + ' LIMIT %s'
    )
    args = [*h_args, str(int(start_s)), str(int(end_s)),
            *a_args, int(start_s), int(end_s), int(limit)]
    with self.conn.cursor() as cur:
      cur.execute(sql, args)
      rows = list(cur.fetchall())
    out = []
    for row in rows:
      item = dict(row)
      item['trigger_ts'] = _int_or_none(item.pop('trigger_ts_raw', None))
      out.append(item)
    return out

  def find_alarms_by_ids(self, pairs: list[tuple[str, str]]) -> list[dict]:
    """按告警身份 `(cluster, event_log_id)` 批量取**当前态**展示字段。

    判定表只存身份，展示（空间名/设备/测点/内容/等级）实时读；行被软删时查不到，
    调用方回退判定表里的 `alarm_snapshot`。
    """
    pairs = [(str(c), str(g)) for c, g in (pairs or []) if c and g]
    if not pairs:
      return []
    wanted = set(pairs)
    space_join = (' LEFT JOIN dcim_spaces s'
                  ' ON s.cluster = a.cluster AND s.guid = a.space_guid')
    sql = (
      f'SELECT {self._ALARM_COLS} FROM dcim_alarms a'
      f'{space_join}{self._ALARM_WO_JOIN}'
      ' WHERE NOT a.is_deleted AND a.cluster = ANY(%s) AND a.guid = ANY(%s)'
    )
    with self.conn.cursor() as cur:
      cur.execute(sql, [sorted({c for c, _ in pairs}), sorted({g for _, g in pairs})])
      rows = list(cur.fetchall())
    return [dict(r) for r in rows
            if (str(r.get('cluster')), str(r.get('event_log_id'))) in wanted]

  def count_alarms(
    self, cluster: str | list[str] | None, *, space_guid: str | None = None,
    device_guid: str | None = None, keyword: str | None = None,
    level: str | None = None, status: str | None = 'active',
    start_ms: int | None = None, end_ms: int | None = None,
    unresponded_min: int | None = None,
  ) -> int:
    sql, args = self._alarms_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      level=level, status=status, start_ms=start_ms, end_ms=end_ms,
      unresponded_min=unresponded_min, count=True,
    )
    return self._scalar(sql, args)

  def find_alarm_trigger_values(
    self, pairs: list[tuple[str, str]],
  ) -> dict[tuple[str, str], float | None]:
    """每条告警**触发时刻**的值：history 里第一条 `status='1'` 的报文。

    当前态 `dcim_alarms.alarm_value` 会被刷新(2)/恢复(3)推送覆盖，报告要的是
    「当时的值」。history **没有时间索引**，所以只按 `(cluster, guid)` 精确点查，
    不在 history 上做时间范围过滤。
    """
    pairs = [(str(c), str(g)) for c, g in (pairs or []) if c and g]
    if not pairs:
      return {}
    sql = (
      "SELECT cluster, guid, payload->>'value' AS value"
      ' FROM dcim_alarms_history'
      " WHERE cluster = ANY(%s) AND guid = ANY(%s) AND payload->>'status' = '1'"
      ' ORDER BY cluster, guid, ingested_at'
    )
    out: dict[tuple[str, str], float | None] = {}
    with self.conn.cursor() as cur:
      cur.execute(sql, [list({c for c, _ in pairs}), list({g for _, g in pairs})])
      for row in cur.fetchall():
        key = (str(row['cluster']), str(row['guid']))
        if key not in out:          # ORDER BY 保证首条即触发时刻那条
          out[key] = _float_or_none(row.get('value'))
    return out

  def find_point_thresholds(
    self, pairs: list[tuple[str, str]],
  ) -> dict[tuple[str, str], dict]:
    """测点的 4 档告警阈值原始对象（JSONB）与单位，按 `(cluster, guid)` 取。"""
    pairs = [(str(c), str(g)) for c, g in (pairs or []) if c and g]
    if not pairs:
      return {}
    sql = (
      'SELECT cluster, guid, unit, hint_alarm, general_alarm,'
      ' important_alarm, urgent_alarm'
      ' FROM dcim_points WHERE cluster = ANY(%s) AND guid = ANY(%s)'
    )
    with self.conn.cursor() as cur:
      cur.execute(sql, [list({c for c, _ in pairs}), list({g for _, g in pairs})])
      return {(str(r['cluster']), str(r['guid'])): dict(r) for r in cur.fetchall()}

  # ── 告警工单（`ems-alarm-order-rz`）────────────────────────────────────
  # `guid` = `orderNo`；同一单多次推送 upsert 成一行（处理中 → 已处理）。
  # 与告警事件**同库**（PG），可直接 LEFT JOIN 取事件的当前状态。
  _ORDER_COLS = (
    'o.cluster, o.guid AS order_no, o.event_log_id, a.status AS alarm_status,'
    ' o.status, o.alarm_level, o.space_guid, o.space_name, o.device_guid, o.device_name,'
    ' o.point_guid, o.point_name, o.content, o.alarm_value, o.alarm_time, o.response_time,'
    ' o.start_time, o.end_time, o.star, o.responder, o.handler,'
    ' o.process, o.process_result, o.processing_operator_name, o.processed_operator_name'
  )
  _ORDER_STATUS = {
    '处理中': 1, 'processing': 1, 'active': 1, '1': 1,
    '已处理': 2, 'handled': 2, 'done': 2, '2': 2,
  }
  # 可算响应时长的条件：两个时刻都有效、且响应不早于告警（负差值的脏数据剔除）
  _RESP_OK = ('o.response_time > 0 AND o.alarm_time > 0'
              ' AND o.response_time >= o.alarm_time')

  def _order_status_filter(self, status: str | None) -> tuple[str, list]:
    """工单状态入参 → SQL 片段。认不出/`all` 一律「不过滤」—— 工单量小，宁可多给也别静默漏。"""
    s = str(status or '').strip().lower()
    if not s or s in ('all', '全部', '所有'):
      return 'TRUE', []
    value = self._ORDER_STATUS.get(s)
    if value is None:
      return 'TRUE', []
    return 'o.status = %s', [value]

  def _orders_sql(
    self, cluster: str | list[str] | None, *, space_guid: str | None = None,
    device_guid: str | None = None, keyword: str | None = None,
    level: str | None = None, status: str | None = 'all',
    operator: str | None = None, event_log_id: str | None = None,
    start_s: int | None = None, end_s: int | None = None,
    response_timeout_min: int | None = None,
    count: bool = False, summary: bool = False,
  ) -> tuple[str, list]:
    """工单查询 SQL。`start_s`/`end_s` 按**告警发生时间**过滤（列是秒级 epoch）。"""
    cte = join = ''
    args: list = []
    if space_guid:
      cte, args = self._subtree_cte(cluster, space_guid)
      join = ' JOIN sub ON o.cluster = sub.cluster AND o.space_guid = sub.guid'
    # 带出对应告警事件的当前状态：「工单已处理、但事件未恢复」的矛盾要看得见
    join += ' LEFT JOIN dcim_alarms a ON a.cluster = o.cluster AND a.guid = o.event_log_id'
    conds, extra = self._cluster_clause('o.cluster', cluster)
    args += extra
    conds.append('NOT o.is_deleted')
    if device_guid:
      conds.append('o.device_guid = %s')
      args.append(device_guid)
    if event_log_id:
      conds.append('o.event_log_id = %s')
      args.append(event_log_id)
    if status:
      frag, st_args = self._order_status_filter(status)
      conds.append(frag)
      args += st_args
    if level:
      lv = self._ALARM_LEVELS.get(str(level).strip().lower()) or self._ALARM_LEVELS.get(str(level).strip())
      if lv:
        conds.append('o.alarm_level >= %s')     # 「该档及以上」
        args.append(lv)
    if keyword:
      frags, kargs = _kw_any(
        keyword, 'o.space_name', 'o.device_name', 'o.point_name', 'o.content',
      )
      conds.append(f'({frags})')
      args += kargs
    if operator:
      conds.append('(o.responder ILIKE %s OR o.handler ILIKE %s'
                   ' OR o.processing_operator_name ILIKE %s'
                   ' OR o.processed_operator_name ILIKE %s)')
      args += [f'%{operator}%'] * 4
    if start_s:
      conds.append('o.alarm_time >= %s')
      args.append(start_s)
    if end_s:
      conds.append('o.alarm_time <= %s')
      args.append(end_s)
    if response_timeout_min is not None:
      # 只算**响应晚了**的：`_RESP_OK` 先剔除脏数据（无响应时刻 / 响应早于告警）
      conds.append(f'{self._RESP_OK} AND (o.response_time - o.alarm_time) > %s')
      args.append(int(response_timeout_min) * 60)
    if count:
      sel = 'count(*) AS n'
    elif summary:
      ok = self._RESP_OK
      sel = (
        'count(*) AS total,'
        ' count(*) FILTER (WHERE o.status = 1) AS processing,'
        ' count(*) FILTER (WHERE o.status = 2) AS handled,'
        ' count(*) FILTER (WHERE o.alarm_level = 4) AS urgent,'
        ' count(*) FILTER (WHERE o.alarm_level = 3) AS important,'
        ' count(*) FILTER (WHERE o.alarm_level = 2) AS general,'
        ' count(*) FILTER (WHERE o.alarm_level = 1) AS hint,'
        f' count(*) FILTER (WHERE {ok}) AS resp_samples,'
        f' avg((o.response_time - o.alarm_time) / 60.0)'
        f' FILTER (WHERE {ok}) AS resp_avg_min,'
        f' percentile_cont(0.5) WITHIN GROUP'
        f' (ORDER BY (o.response_time - o.alarm_time) / 60.0)'
        f' FILTER (WHERE {ok}) AS resp_p50_min,'
        f' percentile_cont(0.9) WITHIN GROUP'
        f' (ORDER BY (o.response_time - o.alarm_time) / 60.0)'
        f' FILTER (WHERE {ok}) AS resp_p90_min'
      )
    else:
      sel = self._ORDER_COLS
    return f'{cte}SELECT {sel} FROM dcim_alarm_orders o{join} WHERE {" AND ".join(conds)}', args

  def find_alarm_orders(
    self, cluster: str | list[str] | None, *, space_guid: str | None = None,
    device_guid: str | None = None, keyword: str | None = None,
    level: str | None = None, status: str | None = 'all',
    operator: str | None = None, event_log_id: str | None = None,
    start_s: int | None = None, end_s: int | None = None,
    response_timeout_min: int | None = None,
    limit: int = 100, offset: int = 0,
  ) -> list[dict]:
    """列告警工单。`status` 默认 `all`（处理中 + 已处理）。"""
    sql, args = self._orders_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      level=level, status=status, operator=operator, event_log_id=event_log_id,
      start_s=start_s, end_s=end_s, response_timeout_min=response_timeout_min,
    )
    with self.conn.cursor() as cur:
      cur.execute(
        f'{sql} ORDER BY o.alarm_time DESC NULLS LAST, o.guid LIMIT %s OFFSET %s',
        [*args, limit, offset],
      )
      return list(cur.fetchall())

  def count_alarm_orders(
    self, cluster: str | list[str] | None, *, space_guid: str | None = None,
    device_guid: str | None = None, keyword: str | None = None,
    level: str | None = None, status: str | None = 'all',
    operator: str | None = None, event_log_id: str | None = None,
    start_s: int | None = None, end_s: int | None = None,
    response_timeout_min: int | None = None,
  ) -> int:
    sql, args = self._orders_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      level=level, status=status, operator=operator, event_log_id=event_log_id,
      start_s=start_s, end_s=end_s, response_timeout_min=response_timeout_min,
      count=True,
    )
    return self._scalar(sql, args)

  def alarm_order_summary(
    self, cluster: str | list[str] | None, *, space_guid: str | None = None,
    device_guid: str | None = None, keyword: str | None = None,
    level: str | None = None, status: str | None = 'all',
    operator: str | None = None, event_log_id: str | None = None,
    start_s: int | None = None, end_s: int | None = None,
    response_timeout_min: int | None = None,
  ) -> dict:
    """**整个过滤集**的汇总（处理中/已处理、各档级别、响应时长分位）—— 与分页无关。"""
    sql, args = self._orders_sql(
      cluster, space_guid=space_guid, device_guid=device_guid, keyword=keyword,
      level=level, status=status, operator=operator, event_log_id=event_log_id,
      start_s=start_s, end_s=end_s, response_timeout_min=response_timeout_min,
      summary=True,
    )
    with self.conn.cursor() as cur:
      cur.execute(sql, args)
      return dict(cur.fetchone() or {})

  def _scalar(self, sql: str, args: list) -> int:
    with self.conn.cursor() as cur:
      cur.execute(sql, args)
      row = cur.fetchone()
      return int(row['n']) if row else 0

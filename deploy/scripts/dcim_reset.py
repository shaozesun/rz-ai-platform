#!/usr/bin/env python3
"""DCIM 测试数据清理：告警全清；空间/设备/点位只保留廊坊A1数据中心。

口径（2026-10-27 确认）：
  - 保留：`廊坊A` 下名为「廊坊园区A1数据中心」（guid=P001_A001）的空间 → **整棵子树 + 到根的祖先链**
    （根节点删了树就断，`query_space_tree` 从顶层递归再也到不了 A1），
    以及落在这些空间/idc 下的设备、点位。
  - 全清：dcim_alarms / dcim_alarms_history / dcim_alarm_orders /
    dcim_alarm_orders_history / dcim_incident_reports / dcim_dlq。
  - 不动：TDengine 时序数据；`dcim_td_column` / `dcim_td_stable`（列映射清了，
    重新分配不保证与旧列一致 → 旧行会被读成别的测点）；`dcim_level_rule`（配置）。

为什么清空是安全的：上游配置 topic 是**每小时全量重推**，配置类数据约 1 小时内自己
回来；告警只有实时流，不会回流；消费端写库成功才提交 offset，清空不会触发重放。

用法（在 backend 容器里跑；**执行前先停 dcim-ingest**）：

    docker cp deploy/scripts/dcim_reset.py rz-backend:/tmp/
    docker exec -w /app rz-backend python /tmp/dcim_reset.py           # dry-run
    docker exec -w /app rz-backend python /tmp/dcim_reset.py --apply   # 真清

退出码：0 正常（dry-run 也返回 0）；1 A1 锚点解析失败（命中 0 或多个）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

# 既支持「仓库内直接跑」（此时脚本在 deploy/scripts/ 下），也支持 docker cp 到 /tmp 后
# 用 `-w /app` 跑（此时要靠工作目录）。按「哪个目录下有 service/」判断仓库根。
for _root in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
  if (_root / 'service').is_dir() and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from config.settings import settings  # noqa: E402

# 整表清空
TRUNCATE_TABLES = (
  'dcim_alarms',
  'dcim_alarms_history',
  'dcim_alarm_orders',
  'dcim_alarm_orders_history',
  'dcim_incident_reports',
  'dcim_dlq',
)

# 只留 A1：(表, 该表对应的 keep 临时表)
SCOPED_TABLES = (
  ('dcim_point_level', 'keep_point'),
  ('dcim_points', 'keep_point'),
  ('dcim_devices', 'keep_device'),
  ('dcim_spaces', 'keep_space'),
)

ALL_TABLES = ('dcim_spaces', 'dcim_devices', 'dcim_points', 'dcim_point_level',
              *TRUNCATE_TABLES)


def _existing(cur, table: str) -> bool:
  """表是否已建（首次部署时个别表可能还没被幂等建表跑过）。"""
  cur.execute('SELECT to_regclass(%s) AS t', (f'public.{table}',))
  return cur.fetchone()['t'] is not None


def _count(cur, table: str) -> int:
  cur.execute(f'SELECT count(*) AS n FROM {table}')
  return int(cur.fetchone()['n'])


def _resolve_target(cur, cluster: str, name: str) -> dict | None:
  """按「园区 + 名称 + 数据中心层」找唯一锚点；命中 0 或多个一律返回 None（不猜）。"""
  cur.execute(
    'SELECT cluster, guid, idc, name, space_type, parent_guid'
    '  FROM dcim_spaces'
    " WHERE cluster = %s AND space_type = '1' AND NOT is_deleted AND name LIKE %s",
    (cluster, f'%{name}%'),
  )
  rows = cur.fetchall()
  if len(rows) == 1:
    return rows[0]

  cur.execute(
    'SELECT cluster, guid, idc, name, space_type, parent_guid'
    '  FROM dcim_spaces WHERE NOT is_deleted AND name LIKE %s'
    ' ORDER BY cluster, name LIMIT 50',
    (f'%{name}%',),
  )
  candidates = cur.fetchall()
  print(f'! 锚点命中 {len(rows)} 个（要求恰好 1 个）：cluster={cluster}、名称含「{name}」')
  print('  候选（cluster / space_type / idc / name）：')
  for row in candidates:
    print(f"    {row['cluster']} / {row['space_type']} / {row['idc']} / {row['name']}")
  print('  请用 --keep-cluster / --keep-name 指定更精确的值后重跑（不会自动挑一个）。')
  return None


def _print_chain(cur, a1: dict) -> None:
  """打印 根 → A1 的祖先链，供执行前人工确认根节点会被保留。"""
  cur.execute(
    'WITH RECURSIVE up AS ('
    '  SELECT cluster, guid, parent_guid, name, space_type, 0 AS lvl'
    '    FROM dcim_spaces WHERE cluster = %s AND guid = %s'
    '  UNION'
    '  SELECT s.cluster, s.guid, s.parent_guid, s.name, s.space_type, u.lvl + 1'
    '    FROM dcim_spaces s JOIN up u'
    '      ON s.cluster = u.cluster AND s.guid = u.parent_guid'
    '   WHERE u.lvl < 20'
    ') SELECT * FROM up ORDER BY lvl DESC',
    (a1['cluster'], a1['guid']),
  )
  rows = cur.fetchall()
  print('\n祖先链（根 → 目标）：')
  for row in rows:
    flag = ' ← 目标' if row['guid'] == a1['guid'] else ''
    print(f"  {'  ' * row['lvl']}{row['name']}"
          f"  [space_type={row['space_type']}] {row['guid']}{flag}")


def _build_keep(cur, a1: dict) -> None:
  """把 A1 的保留集合物化成临时表（空间子树+祖先、设备、点位）。"""
  cur.execute(
    'CREATE TEMP TABLE keep_space AS '
    'WITH RECURSIVE anchor AS ('
    '    SELECT cluster, guid, parent_guid FROM dcim_spaces'
    '     WHERE cluster = %s AND guid = %s),'
    ' desc_ AS ('
    '    SELECT cluster, guid, parent_guid FROM anchor'
    '    UNION SELECT s.cluster, s.guid, s.parent_guid FROM dcim_spaces s'
    '            JOIN desc_ d ON s.parent_guid = d.guid AND s.cluster = d.cluster),'
    ' anc AS ('
    '    SELECT cluster, guid, parent_guid FROM anchor'
    '    UNION SELECT s.cluster, s.guid, s.parent_guid FROM dcim_spaces s'
    '            JOIN anc a ON s.guid = a.parent_guid AND s.cluster = a.cluster)'
    ' SELECT cluster, guid FROM desc_ UNION SELECT cluster, guid FROM anc',
    (a1['cluster'], a1['guid']),
  )
  # idc 兜底：idc 相同但空间链缺失的设备/点位（限定在目标园区内，避免跨园区 idc 撞名）
  cur.execute(
    'CREATE TEMP TABLE keep_device AS'
    ' SELECT cluster, guid FROM dcim_devices'
    '  WHERE (cluster, space_guid) IN (SELECT cluster, guid FROM keep_space)'
    '     OR (cluster = %s AND idc = %s)',
    (a1['cluster'], a1['idc']),
  )
  cur.execute(
    'CREATE TEMP TABLE keep_point AS'
    ' SELECT cluster, guid FROM dcim_points'
    '  WHERE (cluster, device_guid) IN (SELECT cluster, guid FROM keep_device)'
    '     OR (cluster = %s AND idc = %s)',
    (a1['cluster'], a1['idc']),
  )


def _incident_chats() -> list[str]:
  """飞书群 chat_id：`INCIDENT_FEISHU_PARK_CHATS`(JSON) + `FEISHU_GROUP_INGEST_CHATS`。"""
  found: list[str] = []
  raw = (getattr(settings, 'INCIDENT_FEISHU_PARK_CHATS', '') or '').strip()
  if raw:
    try:
      conf = json.loads(raw)
      if isinstance(conf, dict):
        found.extend(str(v).strip() for v in conf.values())
    except json.JSONDecodeError:
      print('! INCIDENT_FEISHU_PARK_CHATS 不是合法 JSON，跳过该来源')
  found.extend(
    c.strip() for c in (getattr(settings, 'FEISHU_GROUP_INGEST_CHATS', '') or '').split(',')
  )

  chats: list[str] = []
  for value in found:
    if not value:
      continue
    if not value.startswith('oc_'):
      print(f'! 跳过非 chat_id 的配置值（需 oc_ 前缀）：{value}')
      continue
    if value not in chats:
      chats.append(value)
  return chats


def _reset_incident_watermarks(apply: bool) -> None:
  """把回补水位置为「现在」——**不删键**：删了 sync_gaps 会追满 72h，重拉群里的测试报文。"""
  chats = _incident_chats()
  if not chats:
    print('\nRedis 水位：未配置飞书群，跳过')
    return
  try:
    from config.redis_conn import redis_manager
  except Exception as e:                       # noqa: BLE001  水位只是善后，不拦主流程
    print(f'\n! Redis 不可用，跳过水位处理：{e}')
    return
  try:
    from service.scheduled_task.incident_stats import sync as incident_sync
    prefix, ttl = incident_sync._WATERMARK_PREFIX, incident_sync._WATERMARK_TTL
  except Exception:                            # noqa: BLE001  导入失败就用同源常量
    prefix, ttl = 'incident:backfill:ts:', 30 * 86400

  now = int(time.time())
  print('\nRedis 回补水印（incident:backfill:ts:*）：')
  for chat in chats:
    key = redis_manager.key(prefix + chat)
    try:
      raw = redis_manager.client.get(key)
      old = time.strftime('%m-%d %H:%M', time.localtime(int(raw))) if raw else '无'
    except Exception as e:                     # noqa: BLE001
      old = f'读取失败（{e}）'
    if not apply:
      print(f'  chat={chat}: 当前 {old} → --apply 后设为现在')
      continue
    try:
      redis_manager.client.set(key, now, ex=ttl)
      print(f'  chat={chat}: {old} → 现在（TTL {ttl // 86400} 天）')
    except Exception as e:                     # noqa: BLE001
      print(f'  ! chat={chat}: 写入失败 {e}')


def main() -> int:
  ap = argparse.ArgumentParser(description='DCIM 测试数据清理（默认 dry-run）')
  ap.add_argument('--apply', action='store_true', help='真正执行清理（默认只打印计划）')
  ap.add_argument('--keep-cluster', default='廊坊A', help='保留目标所在园区（默认 廊坊A）')
  ap.add_argument('--keep-name', default='廊坊园区A1数据中心',
                  help='保留目标空间名（默认 廊坊园区A1数据中心）')
  args = ap.parse_args()

  print(f"DCIM 测试数据清理（{'APPLY' if args.apply else 'DRY-RUN'}）")
  if args.apply:
    print('! 请确认 `dcim-ingest` 已停止 —— 否则配置重推会与清表交错。')

  with psycopg.connect(settings.DCIM_PG_DSN, row_factory=dict_row) as conn:
    with conn.cursor() as cur:
      a1 = _resolve_target(cur, args.keep_cluster, args.keep_name)
      if a1 is None:
        conn.rollback()
        return 1
      print(f"\n锚点：cluster={a1['cluster']}  name={a1['name']}  idc={a1['idc']}")
      print(f"      guid={a1['guid']}")
      _print_chain(cur, a1)

      _build_keep(cur, a1)
      keep_n = {t: _count(cur, t) for t in ('keep_space', 'keep_device', 'keep_point')}
      before = {t: _count(cur, t) for t in ALL_TABLES if _existing(cur, t)}
      delete_n: dict[str, int] = {}
      for table, keep in SCOPED_TABLES:
        if table not in before:
          continue
        cur.execute(f'SELECT count(*) AS n FROM {table}'
                    f' WHERE (cluster, guid) NOT IN (SELECT * FROM {keep})')
        delete_n[table] = int(cur.fetchone()['n'])

      print(f"\nA1 保留规模：空间 {keep_n['keep_space']} / 设备 {keep_n['keep_device']}"
            f" / 点位 {keep_n['keep_point']}")
      print('\n将整表清空：')
      for table in TRUNCATE_TABLES:
        if table in before:
          print(f'  {table:<28}{before[table]:>10} 行')
      print('\n将删除（只留 A1）：')
      for table, n in delete_n.items():
        print(f'  {table:<28}{n:>10} 行（保留 {before[table] - n}）')

      if not args.apply:
        _reset_incident_watermarks(apply=False)
        conn.rollback()
        print('\nDRY-RUN：未改动任何数据。确认无误后加 --apply 执行。')
        return 0

      for table in TRUNCATE_TABLES:
        if table in before:
          cur.execute(f'TRUNCATE TABLE {table}')
      for table, keep in SCOPED_TABLES:
        if table in before:
          cur.execute(f'DELETE FROM {table}'
                      f' WHERE (cluster, guid) NOT IN (SELECT * FROM {keep})')
      conn.commit()
      conn.autocommit = True          # VACUUM 不能跑在事务块里

      after = {t: _count(cur, t) for t in ALL_TABLES if t in before}
      print('\n执行完成（清理前 → 清理后）：')
      for table in ALL_TABLES:
        if table in before:
          print(f'  {table:<28}{before[table]:>10} → {after[table]}')

  # 水位必须独立于 VACUUM：善后步骤失败不能把「防止 72h 重拉」也一起跳过
  _reset_incident_watermarks(apply=True)

  # 删掉绝大多数行后统计信息失真；顺手回收空间给整点配置重推复用。
  # `PARALLEL 0`：容器 /dev/shm 只有 64MB，并行 vacuum 会 `DiskFull`（现网踩过）。
  vacuum = [t for t, _ in SCOPED_TABLES if t in before]
  if vacuum:
    print(f'\nVACUUM (ANALYZE)：{", ".join(vacuum)}')
    try:
      with psycopg.connect(settings.DCIM_PG_DSN, autocommit=True) as vconn:
        with vconn.cursor() as vcur:
          vcur.execute('VACUUM (ANALYZE, PARALLEL 0) ' + ', '.join(vacuum))
    except Exception as e:                     # noqa: BLE001  数据已提交，统计/回收失败不算清理失败
      print(f'! VACUUM 失败（数据已提交，不影响结果）：{e}')
  return 0


if __name__ == '__main__':
  sys.exit(main())

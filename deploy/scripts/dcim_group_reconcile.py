#!/usr/bin/env python3
"""DCIM 拆组对账：PG 列映射 vs TDengine 实际列，把「幽灵映射」挪到有空位的组。

**问题（2026-10-26 生产实测）**：旧代码是「先提交 PG 映射 → 再 ALTER TDengine」。
ALTER 失败时（历史上根盘写满、或撞 4,096 列上限）PG 里仍留下那批映射 ——
这些测点**从来没有真正写进 TDengine**，是「幽灵列」。当某一组的 PG 列数超过
4,096 时，`ensure_stable` 的 `CREATE STABLE IF NOT EXISTS` 会被 TDengine 直接拒绝
（`Too many columns`），该园区消费者永久重试、实时数据停更。

幽灵列没有历史数据，所以**换组是安全的**；有数据的列一律不动。本脚本只做这件事：

  - 只移动「PG 有、TDengine 里没有」的列
  - 目标组按**TDengine 真实列数**算空位（容量线 `GROUP_MAX_COLUMNS`），装不下顺延 g1/g2…
  - 写完只改 PG；TDengine 侧的表/列由写侧下一次 `ensure_stable` 自动补

用法（在 ingest 容器里跑，PG/TD 凭据都在容器环境里）：

    docker cp deploy/scripts/dcim_group_reconcile.py rz-dcim-ingest:/tmp/
    docker exec -w /app rz-dcim-ingest python /tmp/dcim_group_reconcile.py           # dry-run
    docker exec -w /app rz-dcim-ingest python /tmp/dcim_group_reconcile.py --apply    # 真写

退出码：0 正常；1 检测到「TDengine 有、PG 没有」的列（映射缺失，本脚本不修，需人工看）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 既支持「仓库内直接跑」（此时脚本在 deploy/scripts/ 下），也支持 docker cp 到 /tmp 后
# 用 `-w /app` 跑（此时要靠工作目录）。按「哪个目录下有 service/」判断仓库根。
for _root in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
  if (_root / 'service').is_dir() and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from config.settings import settings  # noqa: E402
from service.dcim_ingest.realtime import RealtimeRepository  # noqa: E402
from service.dcim_ingest.td_schema import (  # noqa: E402
  GROUP_MAX_COLUMNS, next_group, stable_name,
)

SYSTEM_COLUMNS = {'ts', 'report_ts', 'quality'}


def _td_state(td: RealtimeRepository):
  """返回 `(实际存在的超级表名集合, {stable: 数据列集合})`。"""
  stables = {row[0] for row in td._exec('SHOW STABLES', db=settings.TAOS_DB)['data']}
  actual: dict[str, set[str]] = {}
  for stable in stables:
    rows = td._exec(f'DESCRIBE {stable}', db=settings.TAOS_DB)['data']
    actual[stable] = {r[0] for r in rows if (r[3] or '') != 'TAG'} - SYSTEM_COLUMNS
  return stables, actual


def _plan_type(device_type: str, groups: dict[str, dict[str, str]], actual: dict) -> tuple:
  """算该设备类型的搬迁计划 → `(moves, extras)`。

  `groups` = `{point_group: {point_code: td_column}}`（PG 现状）
  `moves` = `[(code, col, src_group, dst_group)]`；`extras` = TDengine 有、PG 没有的列。
  """
  live: dict[str, int] = {}
  phantoms: dict[str, dict[str, str]] = {}
  extras: list[tuple[str, str]] = []
  for group, mapping in groups.items():
    present = actual.get(stable_name(device_type, group))
    if present is None:                       # 表还没建 → 整组都还没落地
      live[group], phantoms[group] = 0, dict(mapping)
      continue
    pg_cols = set(mapping.values())
    live[group] = len(pg_cols & present)
    phantoms[group] = {c: col for c, col in mapping.items() if col not in present}
    extras += [(group, col) for col in sorted(present - pg_cols)]

  moves: list[tuple[str, str, str, str]] = []
  for group in sorted(phantoms):
    room = max(0, GROUP_MAX_COLUMNS - live[group])
    codes = sorted(phantoms[group])
    for code in codes[:room]:                 # 本组还有空位 → 留在原地等 ALTER 补齐
      live[group] += 1
    rest, target = codes[room:], group
    while rest:
      target = next_group(target)
      if target not in live:                  # 新组：容量从 0 起算
        live[target] = 0
        phantoms.setdefault(target, {})
      take = min(len(rest), GROUP_MAX_COLUMNS - live[target])
      if take <= 0:
        continue
      for code in rest[:take]:
        moves.append((code, phantoms[group][code] if group in phantoms else '', group, target))
      live[target] += take
      rest = rest[take:]
  return moves, extras


def main() -> int:
  ap = argparse.ArgumentParser(description='DCIM 拆组对账（默认 dry-run）')
  ap.add_argument('--apply', action='store_true', help='真正写 PG（默认只打印）')
  ap.add_argument('--device-type', help='只处理某个 device_type')
  args = ap.parse_args()

  td = RealtimeRepository()
  td.connect()
  _stables, actual = _td_state(td)

  with psycopg.connect(settings.DCIM_PG_DSN, row_factory=dict_row) as conn:
    sql = ('SELECT device_type, point_code, td_column, point_group FROM dcim_td_column'
           + (' WHERE device_type = %s' if args.device_type else '')
           + ' ORDER BY device_type, point_group, point_code')
    params = (args.device_type,) if args.device_type else ()
    grouped: dict[str, dict[str, dict[str, str]]] = {}
    for row in conn.execute(sql, params).fetchall():
      grouped.setdefault(row['device_type'], {}).setdefault(row['point_group'], {})[
        row['point_code']] = row['td_column']

    total_moves = 0
    total_extras = 0
    for device_type in sorted(grouped):
      groups = grouped[device_type]
      moves, extras = _plan_type(device_type, groups, actual)
      total_extras += len(extras)
      if not moves and not extras:
        continue
      print(f'\n{device_type}')
      for src in sorted(groups):
        present = actual.get(stable_name(device_type, src), set())
        pg_cols = set(groups[src].values())
        print(f'  PG {src}: {len(pg_cols)} 列，TDengine 实有 {len(pg_cols & present)} 列'
              f'（幽灵 {len(pg_cols - present)}）')
      for group, col in extras[:10]:
        print(f'  ! TDengine 有、PG 没有（映射缺失）: {group} / {col}')
      for group in sorted({m[3] for m in moves}):
        n = sum(1 for m in moves if m[3] == group)
        print(f'  → 搬到 {group}: {n} 列')
      total_moves += len(moves)

      if args.apply and moves:
        with conn.cursor() as cur:
          for group in sorted({m[3] for m in moves}):
            cols = [m[1] for m in moves if m[3] == group]
            cur.execute(
              'UPDATE dcim_td_column SET point_group = %s'
              ' WHERE device_type = %s AND td_column = ANY(%s)',
              (group, device_type, cols),
            )
          counts: dict[str, int] = {}
          for row in cur.execute(
            'SELECT point_group, count(*) AS n FROM dcim_td_column'
            ' WHERE device_type = %s GROUP BY point_group', (device_type,),
          ).fetchall():
            counts[row['point_group']] = int(row['n'])
          for group in sorted(counts):
            cur.execute(
              'INSERT INTO dcim_td_stable (device_type, point_group, stable_name)'
              ' VALUES (%s, %s, %s) ON CONFLICT (device_type, point_group) DO UPDATE'
              ' SET stable_name = EXCLUDED.stable_name, updated_at = now()',
              (device_type, group, stable_name(device_type, group)),
            )
            cur.execute(
              'UPDATE dcim_td_stable SET point_count = %s, updated_at = now()'
              ' WHERE device_type = %s AND point_group = %s',
              (counts[group], device_type, group),
            )
        conn.commit()
        print(f'  已写库（{len(moves)} 列）')

  mode = '已应用' if args.apply else 'dry-run（加 --apply 才写库）'
  print(f'\n合计待搬迁 {total_moves} 列；TDengine 独有列 {total_extras} 个。{mode}')
  td.close()
  return 1 if total_extras else 0


if __name__ == '__main__':
  sys.exit(main())

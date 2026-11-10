"""命令行：出事件统计表 / 同步飞书通报 / 导出原始报文。

```bash
# 出表（默认上一完整自然周，**所有已配置园区合并成一张**）
python -m service.scheduled_task.incident_stats.cli \
  --start 2026-10-12 --end 2026-10-19 --out /tmp/incident_w41.xlsx
# 只出某个园区
python -m service.scheduled_task.incident_stats.cli --start ... --end ... --park 廊坊
# 清理某群（占位/测试群）的通报数据：默认 dry-run，确认后加 --yes
python -m service.scheduled_task.incident_stats.cli --purge-chat oc_xxx
# 回填飞书通报（bot 进群后跑一次）
python -m service.scheduled_task.incident_stats.cli --sync --start 2026-10-01 --end 2026-10-21
# 导出一手报文，调规则用
python -m service.scheduled_task.incident_stats.cli --dump 廊坊园区动环通报群 \
  --start ... --end ... --out /tmp/raw.json
# 回捞卡片通报的当前正文（「进展同步N /【进展描述】」是原地追加的）
python -m service.scheduled_task.incident_stats.cli --refresh-cards --hours 72
```
"""

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings
from service.scheduled_task.incident_stats import build, feishu, schema, sync, xlsx

_TZ = ZoneInfo('Asia/Shanghai')


def _day(value: str) -> datetime:
  return datetime.strptime(value, '%Y-%m-%d').replace(tzinfo=_TZ)


def _window(args) -> tuple[int, int]:
  """[start 00:00, end 00:00)；缺省取上一完整自然周（周一 ~ 下周一）。"""
  if args.start:
    start = _day(args.start)
  else:
    from service.scheduled_task.report_templates import window_for

    start = datetime.combine(window_for('weekly')[0], datetime.min.time(), tzinfo=_TZ)
  end = _day(args.end) if args.end else start + timedelta(days=7)
  if end <= start:
    raise SystemExit('--end 必须晚于 --start')
  return int(start.timestamp()), int(end.timestamp())


async def _resolve_chat(name: str) -> str:
  if name.startswith('oc_'):
    return name
  chats = await feishu.list_chats()
  exact = [c for c in chats if c['name'] == name]
  fuzzy = [c for c in chats if name and name in c['name']]
  hit = exact or fuzzy
  if not hit:
    raise SystemExit(f'未找到群「{name}」（机器人是否已进群？）')
  return hit[0]['chat_id']


async def _dump(args, start_s: int, end_s: int) -> None:
  chat_id = await _resolve_chat(args.dump)
  rows, recalled = await feishu.fetch_messages(chat_id, start_s, end_s)
  text = json.dumps(rows, ensure_ascii=False, indent=2, default=str)
  note = f'（另有已撤回 {len(recalled)} 条，未导出）' if recalled else ''
  if args.out:
    with open(args.out, 'w', encoding='utf-8') as f:
      f.write(text)
    print(f'已导出 {len(rows)} 条{note} → {args.out}')
  else:
    print(text)
    if recalled:
      print(note, file=sys.stderr)


async def _build(args, start_s: int, end_s: int) -> None:
  """出表：取数**必须带园区群 chat_id**（只按 cluster 前缀会把同园区测试群/合成行混进表）。"""
  schema.ensure_reports_schema(settings.DCIM_PG_DSN)
  chats = await feishu.resolve_park_chats()
  if args.park:
    chat = chats.get(args.park)
    if not chat:
      raise SystemExit(f'园区「{args.park}」未解析到群；可用：{"、".join(chats) or "（无）"}')
    pairs = [(args.park, chat)]
  else:
    pairs = list(chats.items())
    if not pairs:
      raise SystemExit('没有可用的园区群：INCIDENT_FEISHU_PARK_CHATS 未登记或群名未匹配'
                       '（机器人是否已进群？）')
  rows: list[dict] = []
  for park, chat in pairs:
    rows.extend(build.build_rows(start_s, end_s, park, [chat]))
  rows.sort(key=lambda r: (r.get('发生时间') or '', r.get('事件名称') or ''))
  for i, row in enumerate(rows, 1):
    row['序号'] = i
  summary = build.summary_counts(rows)
  out = args.out or f'/tmp/incident_w{xlsx.sheet_title(start_s)[:-1]}.xlsx'
  path = xlsx.write_workbook(out, rows, summary, start_s)
  print(f'覆盖园区：{"、".join(p for p, _ in pairs)}；事件 {len(rows)} 行 → {path}')
  if not rows:
    print('提示：该窗口未同步到飞书通报（bot 进群后用 --sync 回填）',
          file=sys.stderr)


def _purge(args) -> None:
  """按 chat_id 清理通报数据（默认 dry-run，`--yes` 才真删）。

  **顺序**：先把该群从 `INCIDENT_FEISHU_PARK_CHATS` 与 `FEISHU_GROUP_INGEST_CHATS`
  移除并重启后端，再执行删除 —— 它仍在映射/白名单里时，`sync_gaps` 会按 30 分钟回补
  重新拉回来（72h 窗口）。Redis 回补水印一并清掉；历史 xlsx/run 记录不动。
  """
  chats = list(dict.fromkeys(
    part.strip() for raw in (args.purge_chat or []) for part in raw.split(',')
    if part.strip()
  ))
  if not chats:
    raise SystemExit('--purge-chat 需要 chat_id')
  if not settings.DCIM_PG_DSN:
    raise SystemExit('DCIM_PG_DSN 未配置，无法清理通报数据')
  print('删除前请确认该群已从 INCIDENT_FEISHU_PARK_CHATS 与 '
        'FEISHU_GROUP_INGEST_CHATS 移除并已重启后端，否则回补会重新拉回数据。')
  import psycopg

  from config.redis_conn import redis_manager

  with psycopg.connect(settings.DCIM_PG_DSN) as conn:
    with conn.cursor() as cur:
      for chat in chats:
        cur.execute('SELECT count(*), min(create_time), max(create_time) '
                    'FROM dcim_incident_reports WHERE chat_id = %s', (chat,))
        count, lo, hi = cur.fetchone()
        span = ''
        if lo and hi:
          span = (f'（{datetime.fromtimestamp(lo, _TZ):%Y-%m-%d %H:%M} ~ '
                  f'{datetime.fromtimestamp(hi, _TZ):%Y-%m-%d %H:%M}）')
        print(f'{chat}: {count} 条{span}')
      if not args.yes:
        print('dry-run：未删除任何数据；确认清单无误后加 --yes 执行。')
        return
      cur.execute('DELETE FROM dcim_incident_reports WHERE chat_id = ANY(%s)', (chats,))
      deleted = cur.rowcount
    conn.commit()

  for chat in chats:
    try:
      redis_manager.client.delete(redis_manager.key(sync._WATERMARK_PREFIX + chat))
    except Exception as e:
      logging.warning('清理回补水印失败 chat=%s: %s', chat, e)
  print(f'已删除 {deleted} 条通报，并清理回补水印。')


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser(description='数据中心事件统计表')
  parser.add_argument('--start', help='起始日期 YYYY-MM-DD（含）')
  parser.add_argument('--end', help='结束日期 YYYY-MM-DD（不含；缺省 = start + 7 天）')
  parser.add_argument('--park', help='只出某个园区（如 廊坊）')
  parser.add_argument('--out', help='输出路径（xlsx 或 --dump 的 JSON）')
  parser.add_argument('--sync', action='store_true', help='同步飞书通报并补匹配')
  parser.add_argument('--dump', metavar='群名或chat_id', help='导出该群的原始报文')
  parser.add_argument('--refresh-cards', action='store_true',
                      help='回捞卡片通报的当前正文（进展同步原地追加）')
  parser.add_argument('--hours', type=int, default=None,
                      help='--refresh-cards 的回看窗口（小时，默认用配置值）')
  parser.add_argument('--purge-chat', action='append', metavar='chat_id',
                      help='清理该群（可重复或逗号分隔）的通报数据；默认 dry-run')
  parser.add_argument('--yes', action='store_true', help='--purge-chat 真的执行删除')
  args = parser.parse_args(argv)
  logging.basicConfig(level=logging.INFO, format='%(levelname)s %(message)s')

  if args.purge_chat:
    _purge(args)
    return

  if args.refresh_cards:
    try:
      updated = asyncio.run(sync.refresh_cards(args.hours))
    except RuntimeError as e:
      raise SystemExit(str(e))
    print(f'卡片回捞完成：更新 {updated} 条')
    return

  start_s, end_s = _window(args)
  try:
    if args.dump:
      asyncio.run(_dump(args, start_s, end_s))
      return
    if args.sync:
      inserted = asyncio.run(sync.sync_range(start_s, end_s, backfill=True))
      print(f'同步完成：新增 {inserted} 条通报')
      if not args.out:
        return
    asyncio.run(_build(args, start_s, end_s))
  except RuntimeError as e:
    raise SystemExit(str(e))


if __name__ == '__main__':
  main()

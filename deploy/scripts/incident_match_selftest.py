#!/usr/bin/env python3
"""事件统计表：通报 ↔ 告警匹配自检（基于真实告警合成速报）。

测试群没有真实「速报/首报」，而匹配逻辑不能等真园区群接入才验证。本脚本从
`dcim_alarms` 取近期告警，按**触发态**（`dcim_alarms_history` 里 status='1' 的第一条）
合成一条速报文本写入 `dcim_incident_reports`（chat_id='TEST-SYNTH'），再跑
`sync.match_pending()`，逐行比对 `event_log_id` 是否等于预期 guid。

    python deploy/scripts/incident_match_selftest.py --days 7 --limit 20
    python deploy/scripts/incident_match_selftest.py --cleanup        # 只统计待删行数
    python deploy/scripts/incident_match_selftest.py --cleanup --yes  # 真正删除（先跟用户确认）

判据：正确率 ≥80% 且「配错」为 0。低于此值先看 `match_detail` 分布；匹配口径是
「发生时间精确同秒 + 告警值副筛选 + 同秒并列交模型」，不要直接开定时任务。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

try:
  ROOT = Path(__file__).resolve().parents[2]
except (NameError, IndexError):
  # `docker exec -i ... python -`（脚本从 stdin 读入）时 __file__='<stdin>'，没有三级父目录
  ROOT = Path.cwd()
if str(ROOT) not in sys.path:
  sys.path.insert(0, str(ROOT))

from config.settings import settings  # noqa: E402
from service.scheduled_task.incident_stats import rules, source, sync  # noqa: E402

TEST_CHAT_ID = 'TEST-SYNTH'

_CANDIDATES_SQL = (
  'WITH recent AS ('
  '  SELECT cluster, guid, space_guid, alarm_time'
  '    FROM dcim_alarms'
  '   WHERE NOT is_deleted AND space_guid IS NOT NULL'
  '     AND alarm_time >= %s'
  '   ORDER BY alarm_time DESC'
  '   LIMIT %s'
  ')'
  ' SELECT DISTINCT ON (r.guid)'
  '        r.cluster, r.guid AS event_log_id, r.space_guid,'
  '        s.name AS space_name, h.payload'
  '   FROM recent r'
  '   JOIN dcim_spaces s'
  '     ON s.cluster = r.cluster AND s.guid = r.space_guid AND NOT s.is_deleted'
  '   JOIN dcim_alarms_history h'
  '     ON h.cluster = r.cluster AND h.guid = r.guid'
  '  WHERE s.name IS NOT NULL AND s.name <> %s'
  "    AND (h.payload->>'timestamp') IS NOT NULL"
  "    AND (h.payload->>'value') IS NOT NULL"
  '  ORDER BY r.guid, (h.payload->>%s = %s) DESC, h.ingested_at, h.id'
)

_RESULT_SQL = (
  'SELECT message_id, cluster, event_time, event_log_id, match_score, match_detail, text'
  '  FROM dcim_incident_reports'
  " WHERE chat_id = %s AND message_id LIKE %s"
  ' ORDER BY id'
)


def _repo():
  from service.dcim_ingest.repository import DcimRepository

  if not settings.DCIM_PG_DSN:
    raise RuntimeError('DCIM_PG_DSN 未配置')
  return DcimRepository(settings.DCIM_PG_DSN)


def _to_int(value) -> int | None:
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


def _to_float(value) -> float | None:
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def pick_alarms(days: int, limit: int) -> list[dict]:
  """近 N 天、有空间名与触发态时间/值的告警，按触发时间倒序取 limit 条。"""
  since = int(time.time()) - max(1, days) * 86400
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(_CANDIDATES_SQL, (
        since, max(limit * 20, 200), '', 'status', '1',
      ))
      rows = list(cur.fetchall())
  finally:
    repo.close()

  out: list[dict] = []
  for row in rows:
    payload = row.get('payload') or {}
    ts = _to_int(payload.get('timestamp'))
    value = _to_float(payload.get('value'))
    if ts is None or value is None:
      continue
    out.append({
      'cluster': row['cluster'],
      'event_log_id': str(row['event_log_id']),
      'space_name': (row.get('space_name') or '').strip(),
      'space_guid': row.get('space_guid'),
      'trigger_time': ts,
      'trigger_value': value,
      'device_name': (payload.get('device_name') or '').strip(),
      'point_name': (payload.get('point_name') or '').strip(),
      'content': (payload.get('content') or '').strip(),
    })
  out.sort(key=lambda r: r['trigger_time'], reverse=True)
  return out[:limit]


def synth_text(alarm: dict) -> str:
  """合成速报正文：字段与真实通报对齐（标签锚定解析依赖这些标签）。"""
  park, _ = rules.park_of(alarm['cluster'])
  when = datetime.fromtimestamp(alarm['trigger_time'])
  overview = ' - '.join(
    part for part in (
      alarm['space_name'], alarm['device_name'], alarm['point_name'], alarm['content'],
    ) if part
  )
  value = f"{alarm['trigger_value']:g}"
  return (
    f'【{park}数据中心速报】\n'
    f'【发生时间】：{when:%Y年%m月%d日 %H时%M分%S秒}\n'
    f'【事件概述】：{overview}\n'
    f'【告警值】：{value}\n'
  )


def insert_rows(alarms: list[dict], run_id: str) -> int:
  rows = []
  for alarm in alarms:
    park, _ = rules.park_of(alarm['cluster'])
    rows.append({
      'chat_id': TEST_CHAT_ID,
      'message_id': f'synth-{run_id}-{alarm["event_log_id"]}',
      'cluster': park,
      'kind': 'sos',
      'title': f'{park}数据中心速报',
      'create_time': alarm['trigger_time'] + 60,
      'event_time': alarm['trigger_time'],
      'text': synth_text(alarm),
      'raw': {'synthetic': True, 'source_cluster': alarm['cluster']},
      'sender': 'self-test',
      'sender_id': 'self-test',
      'sender_id_type': 'app_id',
      'report_value': alarm['trigger_value'],
    })
  return source.save_reports(rows)


def load_results(run_id: str) -> list[dict]:
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(_RESULT_SQL, (TEST_CHAT_ID, f'synth-{run_id}-%'))
      return list(cur.fetchall())
  finally:
    repo.close()


def report(alarms: list[dict], rows: list[dict], run_id: str) -> bool:
  expected = {a['event_log_id']: a for a in alarms}
  prefix = f'synth-{run_id}-'
  correct = wrong = unmatched = 0
  details: Counter = Counter()
  wrong_rows = []
  for row in rows:
    guid = (row['message_id'] or '')[len(prefix):]
    target = row.get('event_log_id')
    details[row.get('match_detail') or '(未匹配)'] += 1
    if not target:
      unmatched += 1
    elif target == guid:
      correct += 1
    else:
      wrong += 1
      wrong_rows.append((expected.get(guid), row))

  total = len(rows)
  rate = correct / total if total else 0.0
  print(f'样本 {total} 条：正确 {correct}（{rate:.0%}）/ 配错 {wrong} / 未配 {unmatched}')
  print('match_detail 分布：' + '，'.join(
    f'{k}={v}' for k, v in details.most_common()
  ))
  for alarm, row in wrong_rows:
    print(f'  [配错] {row["message_id"]}')
    print(f'    期望 {alarm["event_log_id"] if alarm else "?"} 实得 {row["event_log_id"]}'
          f' detail={row.get("match_detail")} score={row.get("match_score")}')
    print(f'    文本 {(row.get("text") or "")[:120]!r}')

  print('\n前 10 条样例：')
  for row in rows[:10]:
    guid = (row['message_id'] or '')[len(prefix):]
    flag = '正确' if row.get('event_log_id') == guid else (
      '未配' if not row.get('event_log_id') else '配错')
    print(f'  [{flag}] detail={row.get("match_detail") or "-"}'
          f' score={row.get("match_score")}')
    print(f'    {(row.get("text") or "").replace(chr(10), " / ")[:150]}')

  return total > 0 and rate >= 0.8 and wrong == 0


def cleanup(yes: bool) -> int:
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'SELECT count(*) AS n FROM dcim_incident_reports WHERE chat_id = %s',
        (TEST_CHAT_ID,),
      )
      row = cur.fetchone()
      count = int(row['n']) if row else 0
      if not count:
        print('无合成数据')
        return 0
      print(f'待删除 {count} 行（chat_id={TEST_CHAT_ID}）')
      if not yes:
        print('未加 --yes，不执行删除。请先确认这条命令只影响合成数据。')
        return 0
      cur.execute('DELETE FROM dcim_incident_reports WHERE chat_id = %s', (TEST_CHAT_ID,))
      deleted = cur.rowcount or 0
    repo.conn.commit()
  finally:
    repo.close()
  print(f'已删除 {deleted} 行')
  return 0


def main() -> int:
  parser = argparse.ArgumentParser(description='通报↔告警匹配自检（合成数据）')
  parser.add_argument('--days', type=int, default=7, help='取近 N 天告警（默认 7）')
  parser.add_argument('--limit', type=int, default=20, help='合成条数（默认 20）')
  parser.add_argument('--cleanup', action='store_true', help='删除 TEST-SYNTH 合成行')
  parser.add_argument('--yes', action='store_true', help='配合 --cleanup 真正执行删除')
  args = parser.parse_args()

  if args.cleanup:
    return cleanup(args.yes)

  alarms = pick_alarms(args.days, args.limit)
  if not alarms:
    print('近 %d 天没有同时具备空间名与触发态时间/值的告警，无法自检' % args.days)
    return 1
  run_id = str(int(time.time()))
  inserted = insert_rows(alarms, run_id)
  print(f'合成 {len(alarms)} 条速报（新增 {inserted} 条，run_id={run_id}）')

  matched = asyncio.run(sync.match_pending(limit=max(2000, args.limit * 10)))
  print(f'match_pending 本轮匹配 {matched} 条')

  rows = load_results(run_id)
  ok = report(alarms, rows, run_id)
  print('\n结论：' + ('通过' if ok else '未通过（正确率 <80% 或存在配错）'))
  return 0 if ok else 1


if __name__ == '__main__':
  raise SystemExit(main())

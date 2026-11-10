"""单个事件的进展全记录（对话「查某个事件的完整进展」用）。

事件聚合完全复用统计表的同一套口径（`_group_reports` + `_compose`），只是额外把
卡片正文里的 `进展同步N` 追加块解析成完整时间线 —— 不另算一套，避免对话与报表对不上。
"""

import re

from service.scheduled_task.incident_stats import build, source


def _title(row: dict, reports: list[dict]) -> str:
  """事件名：统计表口径 > 卡片顶部原地 `【进展同步】`（真实卡片里就是告警名）> 消息标题。"""
  name = (row.get('事件名称') or '').strip()
  if name:
    return name
  inline = build._report_field_latest(reports, ('进展同步',))
  if inline:
    return inline
  return next((r.get('title') for r in reversed(reports) if r.get('title')), '')


def build_events(start_s: int, end_s: int, cluster_prefix: str | None = None,
                 chat_ids: list[str] | None = None) -> list[dict]:
  """窗口内（按发生时间）的事件 + 完整进展块，按发生时间升序。"""
  reports = source.load_reports(
    int(start_s) - build._WINDOW_PAD_SECONDS, int(end_s) + build._WINDOW_PAD_SECONDS,
    cluster_prefix, chat_ids)
  if not reports:
    return []

  groups = build._group_reports(reports)
  guids = [r['event_log_id'] for r in reports if r.get('event_log_id')]
  alarms = source.load_alarms(guids)
  spaces = source.load_spaces()
  buildings = source.building_index(spaces)
  space_names = source.space_index(spaces)

  events: list[dict] = []
  for group in groups:
    alarm = None
    for report in group['reports']:
      if report.get('event_log_id'):
        alarm = alarms.get(report['event_log_id'])
        break
    occurred, _ = build._occurred_at(alarm, group['reports'])
    if not occurred or not (int(start_s) <= occurred < int(end_s)):
      continue
    row = build._compose(group, alarm, buildings, 0, space_names)
    if not row:
      continue
    row = build._sanitize_row(row)
    items = sorted(group['reports'], key=lambda r: r.get('create_time') or 0)
    text = '\n'.join(r.get('text') or '' for r in items)
    title = _title(row, items)
    # 展示时刻：发生时间是推测值时统计表会留空，退回速报/首报时刻
    occurred_text = row['发生时间'] or row['速报时间'] or row['首报时间']
    events.append({
      'event_log_id': next(
        (r.get('event_log_id') for r in items if r.get('event_log_id')), ''),
      'title': title,
      'place': row['地点'],
      'date': row['日期'].isoformat(),
      'occurred': occurred_text,
      'occurred_ts': occurred,
      'blocks': build.progress_blocks(text, include_legacy=True),
      'final': build.final_section(text),
      'haystack': build._plain_text(f'{title} {row["地点"]} {text}').lower(),
      'reports': len(items),
    })
  events.sort(key=lambda e: e['occurred_ts'])
  return events


def search(events: list[dict], query: str) -> list[dict]:
  """按空白/逗号切词，全部命中（AND，大小写不敏感）才算匹配；空 query 返回全部。"""
  tokens = [t for t in re.split(r'[,，、;；\s]+', (query or '').strip()) if t]
  if not tokens:
    return list(events)
  lowered = [t.lower() for t in tokens]
  return [e for e in events if all(t in e['haystack'] for t in lowered)]

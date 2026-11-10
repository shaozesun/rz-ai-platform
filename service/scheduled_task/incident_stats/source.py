"""取数：通报（窗口内）、告警 + 最新工单（按 event_log_id 批量）、空间全名（楼宇兜底）。

复用 `DcimRepository` 的连接（psycopg 同步），**不改 ingest/repository.py 的现有查询**
（那边给告警工具用的 LATERAL 只带 6 列，本表要 process / start_time / end_time）。
"""

import logging
import time
from typing import Any

from config.settings import settings
from service.scheduled_task.incident_stats import rules

logger = logging.getLogger(__name__)

_SPACE_CACHE: dict[str, Any] = {'at': 0.0, 'rows': []}
_SPACE_TTL_SECONDS = 3600


def _repo():
  from service.dcim_ingest.repository import DcimRepository

  if not settings.DCIM_PG_DSN:
    raise RuntimeError('DCIM_PG_DSN 未配置，无法生成事件统计表')
  return DcimRepository(settings.DCIM_PG_DSN)


def load_reports(start_s: int, end_s: int, cluster_prefix: str | None = None,
                 chat_ids: list[str] | None = None) -> list[dict]:
  """窗口内的速报/首报（按消息时间）。

  `chat_ids` 限定**园区群**：同园区的其它群（测试群/自检合成数据）不得混进统计表；
  `cluster_prefix` 只是园区名前缀，单靠它挡不住别的群。
  """
  sql = (
    'SELECT id, chat_id, message_id, cluster, kind, title, create_time, event_time,'
    '       text, sender, event_log_id'
    '  FROM dcim_incident_reports'
    " WHERE kind IN ('sos', 'first') AND create_time >= %s AND create_time < %s"
  )
  args: list = [start_s, end_s]
  if cluster_prefix:
    sql += ' AND cluster LIKE %s'
    args.append(f'{cluster_prefix}%')
  if chat_ids:
    sql += ' AND chat_id = ANY(%s)'
    args.append(list(chat_ids))
  sql += ' ORDER BY create_time'
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(sql, args)
      return list(cur.fetchall())
  finally:
    repo.close()


# `alarm_time` 是当前态，会被刷新(2)/恢复(3)推送覆盖（`parse_alarm` 每条推送都写它 +
# `apply_batch` 整行 upsert），不能当触发时刻展示或归属统计期。`trigger_time` 取历史表
# 第一条 `status='1'` 推送的 `timestamp`（append-only，不变）；没有历史的老告警回退
# 当前态，与改动前逐行等价。取文本、Python 侧再转 epoch，脏值不会让整条查询报错。
_ALARM_SQL = (
  'SELECT a.cluster, a.guid AS event_log_id, a.space_guid, a.status AS alarm_status,'
  '       a.device_name, a.point_name, a.content, a.alarm_level, a.alarm_time,'
  '       th.trigger_time,'
  '       wo.guid AS order_no, wo.status AS order_status, wo.process,'
  '       wo.process_result, wo.start_time, wo.end_time'
  '  FROM dcim_alarms a'
  '  LEFT JOIN LATERAL ('
  "    SELECT h.payload->>'timestamp' AS trigger_time"
  '      FROM dcim_alarms_history h'
  '     WHERE h.cluster = a.cluster AND h.guid = a.guid'
  "       AND h.payload->>'status' = '1'"
  '     ORDER BY h.ingested_at, h.id'
  '     LIMIT 1'
  '  ) th ON TRUE'
  '  LEFT JOIN LATERAL ('
  '    SELECT o.guid, o.status, o.process, o.process_result, o.start_time, o.end_time'
  '      FROM dcim_alarm_orders o'
  '     WHERE o.cluster = a.cluster AND o.event_log_id = a.guid AND NOT o.is_deleted'
  '     ORDER BY o.updated_at DESC, o.guid DESC'
  '     LIMIT 1'
  '  ) wo ON TRUE'
  ' WHERE NOT a.is_deleted AND a.guid = ANY(%s)'
)


def load_alarms(guids: list[str]) -> dict[str, dict]:
  """按 event_log_id 批量取告警 + 最新工单；返回 guid → row。"""
  ids = [g for g in dict.fromkeys(guids) if g]
  if not ids:
    return {}
  repo = _repo()
  out: dict[str, dict] = {}
  try:
    with repo.conn.cursor() as cur:
      for i in range(0, len(ids), 500):
        cur.execute(_ALARM_SQL, (ids[i:i + 500],))
        for row in cur.fetchall():
          out[row['event_log_id']] = row
  finally:
    repo.close()
  return out


def load_spaces() -> list[dict]:
  """全部未删除空间（带 1h 进程缓存）：园区/楼宇解析用，不含 payload。"""
  now = time.time()
  if _SPACE_CACHE['rows'] and now - _SPACE_CACHE['at'] < _SPACE_TTL_SECONDS:
    return _SPACE_CACHE['rows']
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'SELECT cluster, guid, parent_guid, space_type, name'
        '  FROM dcim_spaces WHERE NOT is_deleted'
      )
      rows = list(cur.fetchall())
  finally:
    repo.close()
  _SPACE_CACHE.update(at=now, rows=rows)
  return rows


def building_index(spaces: list[dict]) -> dict[tuple[str, str], str]:
  """(cluster, guid) → 最近的 space_type='1' 祖先名（楼宇/数据中心）。"""
  by_key = {(r['cluster'], r['guid']): r for r in spaces}
  memo: dict[tuple[str, str], str] = {}

  def resolve(key: tuple[str, str]) -> str:
    if key in memo:
      return memo[key]
    seen: set[tuple[str, str]] = set()
    cur = by_key.get(key)
    name = ''
    while cur is not None and (cur['cluster'], cur['guid']) not in seen:
      seen.add((cur['cluster'], cur['guid']))
      if str(cur.get('space_type')) == '1':
        name = (cur.get('name') or '').strip()
        break
      parent = cur.get('parent_guid')
      cur = by_key.get((cur['cluster'], parent)) if parent else None
    memo[key] = name
    return name

  return {k: resolve(k) for k in by_key}


def space_index(spaces: list[dict]) -> dict[tuple[str, str], str]:
  """(cluster, guid) → 空间全名。

  `dcim_spaces.name` 本身就是「园区+数据中心+模组+楼层+房间」全称
  （如 `廊坊园区A1数据中心M4模组一层101机房`），与 `query_alarms.space_name` 同源，
  事件统计表的「地点」直接用它，不再爬到楼宇层。
  """
  return {(r['cluster'], r['guid']): (r.get('name') or '').strip() for r in spaces}


# 候选必须按**触发态**时间查：当前态 `dcim_alarms.alarm_time` 会被刷新(2)/恢复(3)
# 推送覆盖（`parse_alarm` 每条推送都写它 + `apply_batch` 整行 upsert），只有历史表里
# 第一条 `status='1'` 的 `timestamp` 是不变的。区间退化成等值：秒的十进制串同为
# 10 位，字典序即数值序（调用方传的是同一个秒）。
_CANDIDATES_SQL = (
  'WITH trig AS ('
  '  SELECT DISTINCT ON (h.cluster, h.guid) h.cluster, h.guid'
  '    FROM dcim_alarms_history h'
  '   WHERE h.cluster LIKE %s'
  "     AND h.payload->>'status' = '1'"
  "     AND h.payload->>'timestamp' BETWEEN %s AND %s"
  '   ORDER BY h.cluster, h.guid, h.ingested_at, h.id'
  ') '
  'SELECT a.cluster, a.guid AS event_log_id, a.space_guid, a.alarm_time, a.alarm_value,'
  '       a.device_name, a.point_name, a.content, a.alarm_level'
  '  FROM trig t'
  '  JOIN dcim_alarms a'
  '    ON a.cluster = t.cluster AND a.guid = t.guid AND NOT a.is_deleted'
  ' UNION ALL '
  'SELECT a.cluster, a.guid, a.space_guid, a.alarm_time, a.alarm_value,'
  '       a.device_name, a.point_name, a.content, a.alarm_level'
  '  FROM dcim_alarms a'
  ' WHERE NOT a.is_deleted AND a.cluster LIKE %s AND a.alarm_time BETWEEN %s AND %s'
  '   AND NOT EXISTS ('
  '     SELECT 1 FROM dcim_alarms_history h'
  '      WHERE h.cluster = a.cluster AND h.guid = a.guid)'
)


def alarm_candidates(cluster_prefix: str, start_s: int, end_s: int) -> list[dict]:
  """匹配用候选告警（只要时间 + 值 + 文本字段，量小）。

  通报里只存**园区名**（如「廊坊」），而告警的 cluster 是「廊坊A」这类集群名
  → 按前缀匹配，不能写等号。

  时间按**触发态**（历史表第一条 `status='1'` 的 `timestamp`）匹配；没有历史行的
  老告警回退当前态 `alarm_time`（与改动前逐行等价）。匹配前 `first_push_info()`
  仍会再校验一次触发时间，两道独立。
  """
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(_CANDIDATES_SQL, (
        f'{cluster_prefix}%', str(start_s), str(end_s),
        f'{cluster_prefix}%', start_s, end_s,
      ))
      return list(cur.fetchall())
  finally:
    repo.close()


_FIRST_PUSH_SQL = (
  'SELECT DISTINCT ON (guid) guid, payload'
  '  FROM dcim_alarms_history'
  ' WHERE cluster LIKE %s AND guid = ANY(%s)'
  ' ORDER BY guid, (payload->>\'status\' = \'1\') DESC, ingested_at, id'
)


def first_push_info(cluster_prefix: str, guids: list[str]) -> dict[str, dict]:
  """每个告警的**首次推送**（触发态优先）→ {guid: {timestamp, value, ...}}。

  当前态表的 `alarm_time`/`alarm_value` 会被刷新/恢复推送覆盖（`parse_alarm` 对
  每条推送都写这两列），而通报的时间和值一定对应**触发那一刻**。历史表 append-only，
  取每个 guid 的第一条即可；没有历史（接入前的老告警）返回空，调用方回退当前态。
  """
  ids = [g for g in dict.fromkeys(guids) if g]
  if not ids:
    return {}
  repo = _repo()
  out: dict[str, dict] = {}
  try:
    with repo.conn.cursor() as cur:
      for i in range(0, len(ids), 500):
        cur.execute(_FIRST_PUSH_SQL, (f'{cluster_prefix}%', ids[i:i + 500]))
        for row in cur.fetchall():
          payload = row.get('payload') or {}
          out[row['guid']] = {
            'timestamp': _to_int(payload.get('timestamp')),
            'value': _to_float(payload.get('value')),
            'content': payload.get('content') or '',
            'device_name': payload.get('device_name') or '',
            'point_name': payload.get('point_name') or '',
          }
  finally:
    repo.close()
  return out


def space_names(pairs: list[tuple[str, str]]) -> dict[tuple[str, str], str]:
  """(cluster, space_guid) → 空间名（候选告警的机房比对用）。"""
  by_cluster: dict[str, list[str]] = {}
  for cluster, guid in pairs:
    if guid:
      by_cluster.setdefault(cluster, []).append(guid)
  if not by_cluster:
    return {}
  repo = _repo()
  out: dict[tuple[str, str], str] = {}
  try:
    with repo.conn.cursor() as cur:
      for cluster, guids in by_cluster.items():
        cur.execute(
          'SELECT guid, name FROM dcim_spaces WHERE cluster = %s AND guid = ANY(%s)',
          (cluster, list(dict.fromkeys(guids))),
        )
        for row in cur.fetchall():
          out[(cluster, row['guid'])] = (row['name'] or '').strip()
  finally:
    repo.close()
  return out


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


def latest_report_time(chat_id: str) -> int | None:
  """该群已入库通报的最大消息时间（增量游标）。"""
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'SELECT max(create_time) AS ts FROM dcim_incident_reports WHERE chat_id = %s',
        (chat_id,),
      )
      row = cur.fetchone()
      return int(row['ts']) if row and row['ts'] is not None else None
  finally:
    repo.close()


def save_reports(rows: list[dict]) -> int:
  """批量 upsert 通报（幂等：同 (chat_id, message_id) 不重复写入）。返回新插入行数。

  冲突时**重写报文派生字段**（分类/时间/告警值等）：分类规则修正后重拉同一窗口即可
  把已入库的 `other` 就地改成 `first`。匹配结果（`event_log_id` 等）不动。
  """
  if not rows:
    return 0
  import json

  sql = (
    'INSERT INTO dcim_incident_reports'
    ' (chat_id, message_id, cluster, kind, title, create_time, event_time, text, raw,'
    '  sender, sender_id, sender_id_type, report_value)'
    ' VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)'
    ' ON CONFLICT (chat_id, message_id) DO UPDATE SET'
    '   kind = EXCLUDED.kind, title = EXCLUDED.title, event_time = EXCLUDED.event_time,'
    # sender 空值不覆盖：实时推送只拿得到 open_id（通讯录缺权限/离职时姓名为空），
    # 用它盖掉回补时拉到的真名会让表格里一批人有名一批人空
    '   text = EXCLUDED.text, raw = EXCLUDED.raw,'
    # 右侧必须**带表名**限定：SET 目标列也叫 sender，不限定会报
    # `column reference "sender" is ambiguous`（已在 PG 上实跑验证）
    "   sender = COALESCE(NULLIF(EXCLUDED.sender, ''), dcim_incident_reports.sender),"
    '   sender_id = EXCLUDED.sender_id, sender_id_type = EXCLUDED.sender_id_type,'
    '   report_value = EXCLUDED.report_value'
    ' RETURNING (xmax = 0) AS is_new'
  )
  repo = _repo()
  inserted = 0
  try:
    with repo.conn.cursor() as cur:
      for r in rows:
        cur.execute(sql, (
          r['chat_id'], r['message_id'], r.get('cluster'), r['kind'], r.get('title'),
          r['create_time'], r.get('event_time'), r.get('text'),
          json.dumps(r.get('raw'), ensure_ascii=False) if r.get('raw') is not None else None,
          r.get('sender'), r.get('sender_id'), r.get('sender_id_type'),
          r.get('report_value'),
        ))
        row = cur.fetchone()
        inserted += 1 if row and row['is_new'] else 0
    repo.conn.commit()
  finally:
    repo.close()
  return inserted


def delete_report(chat_id: str, message_id: str) -> int:
  """按幂等键**物理删除**一行（撤回消息当作没发过）。返回删除行数。

  匹配结果随行走：`load_reports` / `pending_reports` 自然看不到它，无需额外重置。
  """
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'DELETE FROM dcim_incident_reports WHERE chat_id = %s AND message_id = %s',
        (chat_id, message_id),
      )
      deleted = cur.rowcount
    repo.conn.commit()
  finally:
    repo.close()
  return max(0, int(deleted or 0))


def update_match(report_id: int, event_log_id: str, score: int, detail: str = '') -> None:
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'UPDATE dcim_incident_reports'
        '   SET event_log_id = %s, match_score = %s, match_detail = %s, matched_at = now()'
        ' WHERE id = %s',
        (event_log_id, score, detail, report_id),
      )
    repo.conn.commit()
  finally:
    repo.close()


def pending_reports(limit: int = 500) -> list[dict]:
  """未匹配的通报（只取速报/首报，按时间升序）。

  `llm_tried_at` 非空 = 已经让模型仲裁过一次（判不出也留空），别再重复烧钱；
  结构信号重试**不受它影响**（迟到告警仍能被配上）。
  """
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'SELECT id, chat_id, cluster, kind, create_time, event_time, text, report_value,'
        '       llm_tried_at'
        '  FROM dcim_incident_reports'
        " WHERE event_log_id IS NULL AND kind IN ('sos', 'first')"
        ' ORDER BY create_time LIMIT %s',
        (limit,),
      )
      return list(cur.fetchall())
  finally:
    repo.close()


_SIBLING_SQL = (
  'SELECT chat_id, event_time, event_log_id'
  '  FROM dcim_incident_reports'
  ' WHERE event_log_id IS NOT NULL AND event_time IS NOT NULL'
  '   AND chat_id = ANY(%s) AND event_time = ANY(%s)'
)


def matched_siblings(pairs: list[tuple[str, int | None]]) -> dict[tuple[str, int], set[str]]:
  """(chat_id, event_time) → 该秒上**已经匹配到**的 guid 集合（兄弟证据用）。

  速报与首报是同一次事件的两条群消息，同群同秒。某条已配上告警后，另一条若并列
  分不出，可继承这个 guid（调用方仍要求该 guid 就在自己的候选组里）。

  返回多个 guid 表示同一秒有两起不同事件，调用方**不继承**（歧义）。
  """
  wanted = {(c, int(t)) for c, t in pairs if c and t is not None}
  if not wanted:
    return {}
  repo = _repo()
  out: dict[tuple[str, int], set[str]] = {}
  try:
    with repo.conn.cursor() as cur:
      cur.execute(_SIBLING_SQL, (
        list({c for c, _ in wanted}), list({t for _, t in wanted}),
      ))
      for row in cur.fetchall():
        key = (row['chat_id'], int(row['event_time']))
        if key in wanted:
          out.setdefault(key, set()).add(str(row['event_log_id']))
  finally:
    repo.close()
  return out


def mark_llm_tried(report_id: int) -> None:
  """标记该通报已让模型仲裁过（判不出也标，避免每轮重复调用）。"""
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'UPDATE dcim_incident_reports SET llm_tried_at = now() WHERE id = %s',
        (report_id,),
      )
    repo.conn.commit()
  finally:
    repo.close()


def interactive_reports(since_s: int) -> list[dict]:
  """时间窗内的**卡片消息**（卡片会原地追加进展，需要定点回捞当前正文）。

  不限条数：窗口内 interactive 消息量很小（现场一周也只有几十条）。
  """
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'SELECT id, chat_id, message_id, create_time, text, event_time'
        '  FROM dcim_incident_reports'
        " WHERE raw->>'msg_type' = 'interactive' AND create_time >= %s"
        ' ORDER BY create_time DESC',
        (since_s,),
      )
      return list(cur.fetchall())
  finally:
    repo.close()


def update_report_text(report_id: int, text: str, raw: str,
                       event_time: int | None = None) -> None:
  """只回写正文相关字段：`text` / `raw`，`event_time` 仅在库里为空时补。

  `kind`/`sender`/匹配结果/`report_value` 一律不动 —— 那些是入库时的派生值，
  回捞只负责把卡片追加后的正文拿回来。
  """
  import json

  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(
        'UPDATE dcim_incident_reports'
        '   SET text = %s, raw = %s, event_time = COALESCE(event_time, %s)'
        ' WHERE id = %s',
        (text, json.dumps({'msg_type': 'interactive', 'body': raw}, ensure_ascii=False),
         event_time, report_id),
      )
    repo.conn.commit()
  finally:
    repo.close()


def park_name(cluster: str | None) -> str:
  """cluster → 园区名（供 build 侧按园区过滤）。"""
  return rules.park_of(cluster)[0]

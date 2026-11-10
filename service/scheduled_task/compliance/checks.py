"""班次窗口 + 四项不合规检查（纯 SQL，确定性）。

口径与三个告警技能逐字对齐（`skills/alarm_unclosed_order` / `alarm_premature_close` /
`alarm_repeat`）；「告警响应超时」是**已响应但慢**（工单响应时间比告警时间晚 N 分钟以上），
对齐 `query_alarm_orders(response_timeout_min=N)`，而**不是** skill `alarm_response_timeout`
（那是「还没有工单 = 没人响应」）。取数自己开 `DcimRepository` 连接、自己写
SQL —— **不改** `service/dcim_ingest/repository.py` 的既有查询（那是对话工具的取数口径，
给它加过滤参数会把技能口径搅乱）。

时间列（`alarm_time` / `response_time` / `start_time` / `end_time`）都是**秒级 epoch**，
展示前一律在这里按 Asia/Shanghai 渲染成字符串 —— 表格里不出现裸时间戳。
"""

import logging
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings
from service.scheduled_task.incident_stats import rules

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')

DAY_START = (8, 30)          # 白班开始（夜班结束）
DAY_END = (17, 30)           # 白班结束（夜班开始）

TITLE = '运营监控中心告警不合规检查'

# 检查项注册表：**唯一来源** —— xlsx 的 sheet、总览行、接口清单、前端卡片都由它生成。
# 加检查项（如以后要加的周维度项）= 这里加一条 + 写一个查询函数，其余自动跟上。
CHECKS: tuple[dict, ...] = (
  {
    'key': 'response_timeout',
    'name': '告警响应超时',
    'cadence': '班次',
    # 阈值来自配置，写进口径说明时同源 —— 改配置后页面/表头不会说谎
    'description': (f'重要及以上告警，关联处置工单的响应时间比告警时间晚 '
                    f'{settings.COMPLIANCE_ALARM_TIMEOUT_MINUTES} 分钟以上'),
    'sheet': '告警响应超时',
    'title': '告警响应超时统计',
  },
  {
    'key': 'unclosed_order',
    'name': '告警未关单',
    'cadence': '班次',
    'description': '本班次已响应、工单仍为处理中（未关单）',
    'sheet': '告警未关单',
    'title': '重要/紧急告警工单未关单统计',
  },
  {
    'key': 'premature_close',
    'name': '告警未解除关单',
    'cadence': '班次',
    'description': '工单已关单，但关联告警事件仍未解除',
    'sheet': '告警未解除关单',
    'title': '告警未解除关单统计',
  },
  {
    'key': 'repeat_alarm',
    'name': '重复告警',
    'cadence': '班次',
    'description': (f'同一自然日、同一测点、同一告警内容出现 '
                    f'{settings.COMPLIANCE_REPEAT_MIN_COUNT} 次及以上（重要及以上）'),
    'sheet': '重复告警',
    'title': '数据中心重复告警统计',
  },
)

# 各检查项 sheet 的列（顺序即展示顺序）。0 行也要出完整表头，所以列**显式声明**、
# 不从数据行推断；`tests/test_compliance.py` 会校验行键与这里一致（防加列漏改）。
COLUMNS: dict[str, tuple[str, ...]] = {
  'response_timeout': (
    '序号', '工单号', '园区', '空间', '设备', '测点', '级别', '告警内容',
    '告警时间', '响应时间', '响应时长(分钟)', '响应人', '处理人', '当前告警状态',
  ),
  'unclosed_order': (
    '序号', '工单号', '园区', '空间', '设备', '测点', '级别', '告警内容', '告警时间',
    '响应时间', '开始处理时间', '响应时长(分钟)', '未关单时长(分钟)',
    '响应人', '处理人', '当前告警状态',
  ),
  'premature_close': (
    '序号', '工单号', '园区', '空间', '设备', '测点', '级别', '告警内容', '告警时间',
    '关单时间', '处理时长(分钟)', '处理人', '处理结果', '当前告警状态', '告警已持续(分钟)',
  ),
  # 前 14 列与值守台账《数据中心处置不合规统计表》的「重复告警」sheet 逐字对齐
  # （运维要能把这张表直接抄进周报）；「问题是否解决/解决办法/填报人」是人工填报列，
  # 平台不推导。末两列是「可以多」的补充信息（参考表只有一个告警时间）。
  'repeat_alarm': (
    '序号', '大楼名称', '告警时间', '等级', '次数', '地址名称', '设备名称',
    '监控项名称', '告警内容', '处理过程', '处理结果', '问题是否解决', '解决办法',
    '填报人', '首次时间', '跨度(分钟)',
  ),
}

_LEVEL_LABELS = {1: '提示', 2: '一般', 3: '重要', 4: '紧急'}
_ALARM_STATUS_LABELS = {'1': '触发', '2': '刷新', '3': '恢复'}
_RESULT_LABELS = {1: '确认告警', 2: '已恢复正常'}


# ==================== 班次窗口 ====================

def _at(day: date, hm: tuple[int, int]) -> datetime:
  return datetime(day.year, day.month, day.day, hm[0], hm[1], tzinfo=_TZ)


def shift_window_for(kind: str, end_day: date) -> dict:
  """`night` = 前一日 17:30 ~ 当日 08:30；`day` = 当日 08:30 ~ 17:30。

  `end_day` 是**班次结束那一天**（期号里的日期）：夜班 10-27 → 10-26 17:30 ~ 10-27
  08:30；白班 10-27 → 10-27 08:30 ~ 17:30。
  """
  if kind == 'night':
    start = _at(end_day - timedelta(days=1), DAY_END)
    end = _at(end_day, DAY_START)
    suffix, name = 'n', '夜班'
  else:
    start = _at(end_day, DAY_START)
    end = _at(end_day, DAY_END)
    suffix, name = 'd', '白班'
  return {
    'start': start, 'end': end,
    'start_s': int(start.timestamp()), 'end_s': int(end.timestamp()),
    'key': f'{end_day.isoformat()}-{suffix}',
    'label': (f'{end_day.isoformat()} {name}'
              f'（{start.strftime("%m-%d %H:%M")} ~ {end.strftime("%m-%d %H:%M")}）'),
    'kind': kind,
  }


def latest_end_day(kind: str, now: datetime | None = None) -> date:
  """最近一个**已结束**的该类班次的结束日（CLI 缺省 `--date` 时用）。"""
  now = now or datetime.now(_TZ)
  today = now.date()
  boundary = _at(today, DAY_START if kind == 'night' else DAY_END)
  return today if now >= boundary else today - timedelta(days=1)


def shift_window(now: datetime | None = None) -> dict:
  """**最近一个已结束**的班次。

  09:00（cron 早班）→ 夜班（昨日 17:30 ~ 今日 08:30）；18:00（cron 晚班）→ 当日白班
  （08:30 ~ 17:30）。其它时刻手动触发同样取已结束的那一班（未结束的班次不查，
  否则统计到一半的班次会把"还没到时间"当成不合规）。
  """
  now = now or datetime.now(_TZ)
  today = now.date()
  if now >= _at(today, DAY_END):
    return shift_window_for('day', today)
  if now >= _at(today, DAY_START):
    return shift_window_for('night', today)
  return shift_window_for('day', today - timedelta(days=1))


# ==================== 格式化助手 ====================

def fmt_ts(value) -> str:
  """秒级 epoch → `YYYY-MM-DD HH:MM:SS`（北京时间）；0/空 → 空串。"""
  try:
    ts = int(value)
  except (TypeError, ValueError):
    return ''
  if ts <= 0:
    return ''
  return datetime.fromtimestamp(ts, _TZ).strftime('%Y-%m-%d %H:%M:%S')


def _minutes(start_s, end_s, digits: int = 1):
  """两个 epoch 秒之间的分钟数；任一无效或终点早于起点 → 空串。"""
  try:
    a, b = int(start_s), int(end_s)
  except (TypeError, ValueError):
    return ''
  if a <= 0 or b <= 0 or b < a:
    return ''
  return round((b - a) / 60, digits)


def _text(value) -> str:
  return '' if value is None else str(value).strip()


def _repo():
  from service.dcim_ingest.repository import DcimRepository

  if not settings.DCIM_PG_DSN:
    raise RuntimeError('DCIM_PG_DSN 未配置，无法执行合规检查')
  return DcimRepository(settings.DCIM_PG_DSN)


def _query(sql: str, args: list) -> list[dict]:
  repo = _repo()
  try:
    with repo.conn.cursor() as cur:
      cur.execute(sql, args)
      return list(cur.fetchall())
  finally:
    repo.close()


# ==================== 检查一：告警响应超时 ====================
# **已响应但慢**：有处置工单，且工单的响应时间比告警时间晚 N 分钟以上（重要及以上）。
# 口径对齐 `query_alarm_orders(response_timeout_min=N)`（repository.py 的 `_RESP_` 条件），
# **不是** skill `alarm_response_timeout`（那是「还没有工单 = 没人响应」，另一回事）。
# 按 `response_time` 归属班次：跨零点的单跟着响应时刻走，不会算错班。
_RESPONSE_TIMEOUT_SQL = (
  'SELECT o.cluster, o.guid AS order_no,'
  '       COALESCE(s.name, o.space_name) AS space_name,'
  '       o.device_name, o.point_name,'
  '       COALESCE(o.alarm_level, a.alarm_level) AS alarm_level, o.content,'
  '       o.alarm_time, o.response_time, o.responder, o.handler,'
  '       a.status AS alarm_status'
  '  FROM dcim_alarm_orders o'
  '  LEFT JOIN dcim_alarms a ON a.cluster = o.cluster AND a.guid = o.event_log_id'
  '  LEFT JOIN dcim_spaces s ON s.cluster = o.cluster AND s.guid = o.space_guid'
  ' WHERE NOT o.is_deleted'
  '   AND o.alarm_level >= 3'
  '   AND o.alarm_time > 0 AND o.response_time > o.alarm_time'
  '   AND (o.response_time - o.alarm_time) > %s'
  '   AND o.response_time >= %s AND o.response_time < %s'
  ' ORDER BY o.cluster, o.response_time'
)


def response_timeout(start_s: int, end_s: int, now_s: int | None = None) -> list[dict]:
  """`now_s` 保留只为 `_RUNNERS` 三个参数统一签名 —— 新口径不依赖「当前时刻」。"""
  limit_s = int(settings.COMPLIANCE_ALARM_TIMEOUT_MINUTES) * 60
  rows = _query(_RESPONSE_TIMEOUT_SQL, [limit_s, start_s, end_s])
  return [
    {
      '序号': i,
      '工单号': _text(r.get('order_no')),
      '园区': rules.park_of(r['cluster'])[0],
      '空间': _text(r.get('space_name')),
      '设备': _text(r.get('device_name')),
      '测点': _text(r.get('point_name')),
      '级别': _LEVEL_LABELS.get(r.get('alarm_level'), ''),
      '告警内容': _text(r.get('content')),
      '告警时间': fmt_ts(r.get('alarm_time')),
      '响应时间': fmt_ts(r.get('response_time')),
      '响应时长(分钟)': _minutes(r.get('alarm_time'), r.get('response_time')),
      '响应人': _text(r.get('responder')),
      '处理人': _text(r.get('handler')),
      '当前告警状态': _ALARM_STATUS_LABELS.get(_text(r.get('alarm_status')), ''),
    }
    for i, r in enumerate(rows, 1)
  ]


# ==================== 检查二：告警未关单 ====================
# 按**响应时间**归属班次（用户指定口径）：本班次响应、工单仍是处理中。
_UNCLOSED_ORDER_SQL = (
  'SELECT o.cluster, o.guid AS order_no,'
  '       COALESCE(s.name, o.space_name) AS space_name,'
  '       o.device_name, o.point_name, o.alarm_level, o.content,'
  '       o.alarm_time, o.response_time, o.start_time, o.responder, o.handler,'
  '       a.status AS alarm_status'
  '  FROM dcim_alarm_orders o'
  '  LEFT JOIN dcim_alarms a ON a.cluster = o.cluster AND a.guid = o.event_log_id'
  '  LEFT JOIN dcim_spaces s ON s.cluster = o.cluster AND s.guid = o.space_guid'
  ' WHERE NOT o.is_deleted AND o.status = 1'
  '   AND o.response_time >= %s AND o.response_time < %s'
  ' ORDER BY o.cluster, o.response_time'
)


def unclosed_order(start_s: int, end_s: int, now_s: int | None = None) -> list[dict]:
  now_s = int(now_s or time.time())
  rows = _query(_UNCLOSED_ORDER_SQL, [start_s, end_s])
  return [
    {
      '序号': i,
      '工单号': _text(r.get('order_no')),
      '园区': rules.park_of(r['cluster'])[0],
      '空间': _text(r.get('space_name')),
      '设备': _text(r.get('device_name')),
      '测点': _text(r.get('point_name')),
      '级别': _LEVEL_LABELS.get(r.get('alarm_level'), ''),
      '告警内容': _text(r.get('content')),
      '告警时间': fmt_ts(r.get('alarm_time')),
      '响应时间': fmt_ts(r.get('response_time')),
      '开始处理时间': fmt_ts(r.get('start_time')),
      '响应时长(分钟)': _minutes(r.get('alarm_time'), r.get('response_time')),
      '未关单时长(分钟)': _minutes(r.get('response_time'), now_s),
      '响应人': _text(r.get('responder')),
      '处理人': _text(r.get('handler')),
      '当前告警状态': _ALARM_STATUS_LABELS.get(_text(r.get('alarm_status')), ''),
    }
    for i, r in enumerate(rows, 1)
  ]


# ==================== 检查三：告警未解除关单（提前关单） ====================
# 按**关单时间**（`end_time`）归属班次；`a.status IN ('1','2')` 即"仍未解除"。
# 用 JOIN（不是 LEFT JOIN）：库内查不到告警行的单**不计入** —— 无从证明「未解除」。
_PREMATURE_CLOSE_SQL = (
  'SELECT o.cluster, o.guid AS order_no,'
  '       COALESCE(s.name, o.space_name) AS space_name,'
  '       o.device_name, o.point_name, o.alarm_level, o.content,'
  '       o.alarm_time, o.start_time, o.end_time, o.handler, o.process_result,'
  '       a.status AS alarm_status'
  '  FROM dcim_alarm_orders o'
  '  JOIN dcim_alarms a ON a.cluster = o.cluster AND a.guid = o.event_log_id'
  '  LEFT JOIN dcim_spaces s ON s.cluster = o.cluster AND s.guid = o.space_guid'
  " WHERE NOT o.is_deleted AND o.status = 2 AND a.status IN ('1', '2')"
  '   AND o.end_time >= %s AND o.end_time < %s'
  ' ORDER BY o.cluster, o.end_time'
)


def premature_close(start_s: int, end_s: int, now_s: int | None = None) -> list[dict]:
  now_s = int(now_s or time.time())
  rows = _query(_PREMATURE_CLOSE_SQL, [start_s, end_s])
  return [
    {
      '序号': i,
      '工单号': _text(r.get('order_no')),
      '园区': rules.park_of(r['cluster'])[0],
      '空间': _text(r.get('space_name')),
      '设备': _text(r.get('device_name')),
      '测点': _text(r.get('point_name')),
      '级别': _LEVEL_LABELS.get(r.get('alarm_level'), ''),
      '告警内容': _text(r.get('content')),
      '告警时间': fmt_ts(r.get('alarm_time')),
      '关单时间': fmt_ts(r.get('end_time')),
      '处理时长(分钟)': _minutes(r.get('start_time'), r.get('end_time')),
      '处理人': _text(r.get('handler')),
      '处理结果': _RESULT_LABELS.get(r.get('process_result'), ''),
      '当前告警状态': _ALARM_STATUS_LABELS.get(_text(r.get('alarm_status')), ''),
      '告警已持续(分钟)': _minutes(r.get('alarm_time'), now_s),
    }
    for i, r in enumerate(rows, 1)
  ]


# ==================== 检查四：重复告警 ====================
# 同一**自然日** × 园区 × 空间 × 测点 × 告警内容 ≥ N 次（重要及以上）。
# 夜班跨零点 → 同一设备可能出两行日期，与技能「同一天」口径一致。
# 工单侧用 `idx_dcim_alarm_orders_event (cluster, event_log_id)` 索引逐条关联，不扫全表；
# 取「末次发生那一条告警」的工单，取不到就用组内最近一条有值的（FILTER 跳过 NULL）。
_REPEAT_SQL = (
  "SELECT (to_timestamp(a.alarm_time) AT TIME ZONE 'Asia/Shanghai')::date AS day,"
  '       a.cluster, a.space_guid,'
  '       max(s.name) AS space_name, max(a.device_name) AS device_name,'
  '       max(a.point_name) AS point_name, max(a.alarm_level) AS max_level,'
  '       a.content, count(DISTINCT a.guid) AS n,'
  '       min(a.alarm_time) AS first_s, max(a.alarm_time) AS last_s,'
  '       (array_agg(a.status ORDER BY a.alarm_time DESC))[1] AS last_alarm_status,'
  '       (array_agg(o.process ORDER BY a.alarm_time DESC)'
  '         FILTER (WHERE o.process IS NOT NULL))[1] AS process,'
  '       (array_agg(o.process_result ORDER BY a.alarm_time DESC)'
  '         FILTER (WHERE o.process_result IS NOT NULL))[1] AS process_result,'
  '       (array_agg(o.status ORDER BY a.alarm_time DESC)'
  '         FILTER (WHERE o.status IS NOT NULL))[1] AS order_status,'
  '       (array_agg(o.guid ORDER BY a.alarm_time DESC)'
  '         FILTER (WHERE o.guid IS NOT NULL))[1] AS order_no'
  '  FROM dcim_alarms a'
  '  LEFT JOIN dcim_spaces s ON s.cluster = a.cluster AND s.guid = a.space_guid'
  '  LEFT JOIN dcim_alarm_orders o'
  '         ON o.cluster = a.cluster AND o.event_log_id = a.guid AND NOT o.is_deleted'
  ' WHERE NOT a.is_deleted AND a.alarm_level >= 3'
  '   AND a.alarm_time >= %s AND a.alarm_time < %s'
  ' GROUP BY day, a.cluster, a.space_guid,'
  "          COALESCE(NULLIF(a.point_guid, ''), a.point_name), a.content"
  ' HAVING count(DISTINCT a.guid) >= %s'
)


def _buildings() -> dict[tuple[str, str], str]:
  """(cluster, space_guid) → 最近的 `space_type='1'` 祖先名（如「廊坊园区A1数据中心」）。

  复用事件统计表那套（带 1h 进程缓存）。楼宇名只是展示列，取不到就留空、不拦主流程。
  """
  from service.scheduled_task.incident_stats import source

  try:
    return source.building_index(source.load_spaces())
  except Exception as e:                     # noqa: BLE001
    logger.warning('楼宇名称解析失败（大楼名称留空）: %s', e)
    return {}


def _building_code(cluster: str, name: str) -> str:
  """`('廊坊A', '廊坊园区A1数据中心')` → `LF-A1`（值守台账的「大楼名称」写法）。"""
  park, code = rules.park_of(cluster)
  rest = (name or '').strip()
  for prefix in (f'{park}园区', park):
    if rest.startswith(prefix):
      rest = rest[len(prefix):]
      break
  if rest.endswith('数据中心'):
    rest = rest[:-len('数据中心')]
  rest = rest.strip()
  if not rest:
    return ''
  return f'{code}-{rest}' if code != park else rest


def _natural_key(value: str) -> list:
  """`LF-A2` 排在 `LF-A10` 前（纯字符串序会颠倒）。"""
  return [int(part) if part.isdigit() else part
          for part in re.split(r'(\d+)', value or '')]


def repeat_alarm(start_s: int, end_s: int, now_s: int | None = None) -> list[dict]:
  min_count = int(settings.COMPLIANCE_REPEAT_MIN_COUNT)
  rows = _query(_REPEAT_SQL, [start_s, end_s, min_count])
  buildings = _buildings()
  out = []
  for r in rows:
    cluster = r.get('cluster')
    building = buildings.get((cluster, r.get('space_guid')), '')
    out.append({
      '序号': 0,
      '大楼名称': _building_code(cluster, building),
      '告警时间': fmt_ts(r.get('last_s')),
      '等级': _LEVEL_LABELS.get(r.get('max_level'), ''),
      '次数': int(r.get('n') or 0),
      '地址名称': _text(r.get('space_name')),
      '设备名称': _text(r.get('device_name')),
      '监控项名称': _text(r.get('point_name')),
      '告警内容': _text(r.get('content')),
      '处理过程': _text(r.get('process')),
      '处理结果': rules.event_progress(
        r.get('order_no'), r.get('process_result'), r.get('order_status'),
        r.get('last_alarm_status'), matched=True,
      ),
      '问题是否解决': '',
      '解决办法': '',
      '填报人': '',
      '首次时间': fmt_ts(r.get('first_s')),
      '跨度(分钟)': _minutes(r.get('first_s'), r.get('last_s')),
      # 排序用，出表前删除
      '_last_s': int(r.get('last_s') or 0),
    })
  out.sort(key=lambda row: (_natural_key(row['大楼名称']), -row['_last_s']))
  for i, row in enumerate(out, 1):
    row['序号'] = i
    row.pop('_last_s', None)
  return out


# ==================== 汇总 / 全量执行 ====================

_RUNNERS = {
  'response_timeout': response_timeout,
  'unclosed_order': unclosed_order,
  'premature_close': premature_close,
  'repeat_alarm': repeat_alarm,
}


def summary(rows_by_key: dict[str, list[dict]]) -> list[dict]:
  """检查项清单 + 命中条数（总览、接口、前端卡片共用，列由 `CHECKS` 生成）。"""
  return [
    {
      'key': check['key'],
      'name': check['name'],
      'cadence': check['cadence'],
      'description': check['description'],
      'sheet': check['sheet'],
      'rows': len(rows_by_key.get(check['key']) or []),
    }
    for check in CHECKS
  ]


def matrix(rows_by_key: dict[str, list[dict]]) -> list[dict]:
  """园区 × 检查项命中数（总览第二张表）。"""
  names = [check['name'] for check in CHECKS]
  parks: dict[str, dict[str, int]] = {}
  for check in CHECKS:
    for row in rows_by_key.get(check['key']) or []:
      park = row.get('园区') or '未知'
      bucket = parks.setdefault(park, {name: 0 for name in names})
      bucket[check['name']] += 1
  out = [{'园区': park, **counts, '总计': sum(counts.values())}
         for park, counts in sorted(parks.items())]
  if out:
    out.append({'园区': '总计', **{name: sum(r[name] for r in out) for name in names},
                '总计': sum(r['总计'] for r in out)})
  return out


def run_all(start_s: int, end_s: int, now_s: int | None = None) -> dict:
  """跑全部检查项 → `{'rows', 'summary', 'matrix'}`。

  一次传入 `now_s`，让「已持续 / 未关单时长」在同一轮里自洽。
  """
  now_s = int(now_s or time.time())
  rows = {key: runner(start_s, end_s, now_s) for key, runner in _RUNNERS.items()}
  return {'rows': rows, 'summary': summary(rows), 'matrix': matrix(rows)}

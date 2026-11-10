"""通报配对 → 事件行 → 22 列。

一行 = 一次已通报事件：同一园区的速报 + 首报合成一行，锚点是匹配到的告警；
匹配不到告警**也出行**（用报文自身的发生时间/事件概述填左侧列），不丢数据。
名称取不到的（如华为 eWatch 推送，字段不是【】标签）名称列留空，末两列放速报/首报原文。
"""

import logging
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config.settings import settings
from service.scheduled_task.incident_stats import rules, source

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_WINDOW_PAD_SECONDS = 86400          # 拉通报时前后各放宽 1 天（跨零点的速报/首报）
_SOS_FIRST_MERGE_SECONDS = 900       # 无告警时，速报就近吸附到首报的时间窗

COLUMNS: tuple[str, ...] = (
  '序号', '日期', '发生时间', '速报时间', '首报时间', '地点', '事件等级', '事件类型',
  '事件名称', '专业', '事件原因', '事件进展', '是否完成', '完成时间', '处理结果',
  '处理时长', '过程有无违规', '应急超15min违规', '实时跟踪人员', '经手人',
  '速报内容', '首报内容',
)


# 卡片 lark_md/HTML 标记：标签被 `**` 包裹、值外面包 `<font color='grey'>`。
# 正文经 `feishu._walk_texts` 从卡片 JSON 的 lark_md 原文抽出，不清理就会原样进报表
# （2026-11-09 实测「处理结果」列显示 `**】 <font color='grey'>现场已处理,告警已解除</font>`）。
_CARD_TAG_RE = re.compile(r'</?(?:font|at|text_tag|emoji)\b[^>]*>', re.IGNORECASE)


def _plain_text(text: str) -> str:
  """去掉卡片标记（`**加粗**` / `<font …>` / `<at …>`），换行与标点原样保留。"""
  return _CARD_TAG_RE.sub('', text or '').replace('**', '')


def _clean_value(text: str) -> str:
  """去标记后再剥行首残留的 `**` / `】`（捕获点落在 `【**处理结果**】` 中间时的形态）。"""
  return _plain_text(text).lstrip('*】 ').strip()


def _report_field(text: str, names: tuple[str, ...]) -> str:
  """从报文正文里取【字段】后面的那一行。

  卡片式通报把冒号写在括号内（`【事件概述：】 : 值`），标签后也可能再跟一个冒号。
  """
  text = _plain_text(text)
  for name in names:
    match = re.search(rf'【{name}\s*[:：]?\s*】\s*[:：]?\s*([^\n]+)', text)
    if match:
      return match.group(1).strip()
  return ''


# 追加块起点：`进展同步N`（数字必需 —— 卡片顶部那句不带数字的「进展同步」是原地字段）。
# 现场两种写法：块 1 常带 `通报时间` / `进展描述` 标签，块 2 起只有「时间 + 描述」。
_PROGRESS_BLOCK_RE = re.compile(r'进展同步\s*(\d+)')
# 块终点：下一块 /「事件结束」段 / 分隔线。**不切裸 `【`** —— 旧格式块体自带
# 【通报时间】/【进展描述】，按 `【` 切会把整块切空。
_PROGRESS_STOP_RE = re.compile(r'进展同步\s*\d+|事件结束|－{2,}|-{4,}')
# 块首标签（裸标签与【…】两种，冒号可有可无）
_PROGRESS_LABEL_RE = re.compile(
  r'^\s*(?:【\s*(?:通报时间|进展描述)\s*[:：]?\s*】|通报时间|进展描述)\s*[:：]?\s*')
# 块首时间戳（2026年11月09日13时52分30秒 / 2026-11-05 13:52:30）
_PROGRESS_TIME_RE = re.compile(
  r'^\s*\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?\s*'
  r'(?:\d{1,2}\s*[时:]\s*\d{1,2}(?:\s*[分:]\s*\d{1,2}\s*秒?)?)?\s*')
# 「事件结束」段里的最终处理结果（裸标签与【处理结果】两种）
_RESULT_RE = re.compile(r'(?:【\s*处理结果\s*[:：]?\s*】|处理结果)\s*[:：]?\s*([^\n]+)')
_RESULT_STOP_RE = re.compile(r'\s*(?:事件原因|填写人|处理人|处理时间|【)')
# 旧格式兜底：只有【进展描述】、没有编号块的卡片
_LEGACY_PROGRESS_RE = re.compile(r'【\s*进展描述\s*[:：]?\s*】\s*[:：]?\s*')
_LEGACY_STOP_RE = re.compile(r'【|进展同步|－{2,}|-{4,}')


def _clean_progress(body: str, strip_chars: str = ' ；;，,。') -> str:
  """剥掉块首的 `通报时间`/`进展描述` 标签与时间戳，压平空白。

  `strip_chars` 只影响首尾清理：统计表的「事件进展」列去掉句末标点，进展全记录
  保留原文标点（调用方传 `' '`）。
  """
  value = body or ''
  while True:
    before = value
    value = _PROGRESS_LABEL_RE.sub('', value, count=1)
    value = _PROGRESS_TIME_RE.sub('', value, count=1)
    if value == before:
      break
  return re.sub(r'\s+', ' ', value).strip(strip_chars)


def _final_result(text: str) -> str:
  """`事件结束` 段里的处理结果 —— 它是这次事件的最终进展（如「现场已处理,告警已解除」）。"""
  text = _plain_text(text)
  latest = ''
  for mark in re.finditer(r'事件结束', text):
    match = _RESULT_RE.search(text[mark.end():])
    if not match:
      continue
    value = _clean_value(match.group(1))
    stop = _RESULT_STOP_RE.search(value)
    value = re.sub(r'\s+', ' ', value[:stop.start()] if stop else value).strip(' ；;，,。')
    if value:
      latest = value
  return latest


_FINAL_FIELDS = ('事件原因', '填写人', '处理人', '处理时间')
_FINAL_STOP_RE = re.compile(r'\s*(?:事件原因|填写人|处理人|处理时间|【)')


def _bare_field(text: str, name: str) -> str:
  """裸标签（没有【】）的字段值：`事件原因 环境湿度波动`。"""
  match = re.search(rf'(?:^|\n)\s*{name}\s*[:：]?\s*([^\n]+)', _plain_text(text))
  if not match:
    return ''
  value = match.group(1)
  stop = _FINAL_STOP_RE.search(value)
  return re.sub(r'\s+', ' ', value[:stop.start()] if stop else value).strip(' ；;，,。')


def final_section(text: str) -> dict:
  """`事件结束` 段的最终结论（处理结果 / 事件原因 / 填写人 / 处理人 / 处理时间）。

  只取最后一个「事件结束」之后的内容 —— 卡片在同一消息里原地追加，最后一段才是终态。
  一个字段都取不到返回 `{}`（调用方据此判断事件是否已结束）。
  """
  text = _plain_text(text)
  marks = list(re.finditer(r'事件结束', text))
  if not marks:
    return {}
  tail = text[marks[-1].end():]
  out: dict[str, str] = {}
  result = _final_result(text)
  if result:
    out['处理结果'] = result
  for name in _FINAL_FIELDS:
    value = _report_field(tail, (name,)) or _bare_field(tail, name)
    if value:
      out[name] = value
  return out


def _block_time(body: str) -> str:
  """块首时间戳（先剥 `通报时间`/`进展描述` 标签，再匹配）。"""
  value = body or ''
  while True:
    before = value
    value = _PROGRESS_LABEL_RE.sub('', value, count=1)
    if value == before:
      break
  match = _PROGRESS_TIME_RE.match(value)
  return match.group(0).strip() if match else ''


def progress_blocks(text: str, include_legacy: bool = False,
                    keep_punct: bool = True) -> list[dict]:
  """按编号列出卡片里的全部追加进展（统计表与对话「进展全记录」共用）。

  返回 `[{'no': 1, 'time': '2026年11月09日13时52分30秒', 'desc': '…'}, …]`，按编号升序。
  **同号取后出现者** —— 同一组里补发的多条报文常各自从 1 起编号。
  一个编号块都没有时，`include_legacy=True` 才把旧格式 `【进展描述】` 按出现顺序列出
  （`no=0`）；默认只认编号块（与统计表口径一致）。
  `keep_punct=False` 去掉句末标点（统计表的「事件进展」列沿用旧行为）。
  """
  text = _plain_text(text)
  strip_chars = ' ' if keep_punct else ' ；;，,。'
  by_no: dict[int, dict] = {}
  for match in _PROGRESS_BLOCK_RE.finditer(text):
    rest = text[match.end():]
    stop = _PROGRESS_STOP_RE.search(rest)
    body = rest[:stop.start()] if stop else rest
    value = _clean_progress(body, strip_chars=strip_chars)
    if value:
      number = int(match.group(1))
      by_no[number] = {'no': number, 'time': _block_time(body), 'desc': value}
  if by_no:
    return [by_no[number] for number in sorted(by_no)]
  if not include_legacy:
    return []
  items: list[dict] = []
  for match in _LEGACY_PROGRESS_RE.finditer(text):
    rest = text[match.end():]
    stop = _LEGACY_STOP_RE.search(rest)
    body = rest[:stop.start()] if stop else rest
    value = re.sub(r'\s+', ' ', body).strip(strip_chars)
    if value:
      items.append({'no': 0, 'time': _block_time(body), 'desc': value})
  return items


def _numbered_progress(text: str) -> str:
  """取编号最大的 `进展同步N` 块（同号取后出现者：同组多条报文各自从 1 起编号）。"""
  blocks = progress_blocks(text, keep_punct=False)
  return blocks[-1]['desc'] if blocks else ''


def _legacy_progress(text: str) -> str:
  """旧写法：只有 `【进展描述】` 标签、没有编号块。取最后一条非空。"""
  text = _plain_text(text)
  latest = ''
  for match in _LEGACY_PROGRESS_RE.finditer(text):
    rest = text[match.end():]
    stop = _LEGACY_STOP_RE.search(rest)
    value = re.sub(r'\s+', ' ', rest[:stop.start()] if stop else rest).strip(' ；;，,。')
    if value:
      latest = value
  return latest


def _latest_progress(text: str) -> str:
  """取卡片里最新的追加进展（卡片在同一消息里原地追加，末尾即最新）。

  优先级：`事件结束` 段的处理结果（最终结论）→ 编号最大的 `进展同步N` →
  旧格式 `【进展描述】`。原地字段 `【进展同步】`（不带数字）**不算**，它是首版的
  处置状态，后面紧跟影响范围等字段。
  """
  text = _plain_text(text)
  return _final_result(text) or _numbered_progress(text) or _legacy_progress(text)


def _group_reports(reports: list[dict]) -> list[dict]:
  """按 event_log_id 分组；未匹配的通报用「首报为锚 + 速报就近吸附」分组。"""
  groups: list[dict] = []
  matched: dict[str, dict] = {}
  unmatched: list[dict] = []
  for report in reports:
    event_log_id = report.get('event_log_id')
    if event_log_id:
      group = matched.setdefault(event_log_id, {'reports': []})
      group['reports'].append(report)
    else:
      unmatched.append(report)

  # 先立「首报」锚点再吸附速报 —— 速报时间总是早于首报，按纯时间升序会让速报
  # 先于锚点被处理，900s 吸附永远命中不到（首报排前、组内再按时间升序）
  unmatched.sort(key=lambda r: (r.get('kind') != 'first', r.get('create_time') or 0))
  for report in unmatched:
    if report.get('kind') == 'first':
      groups.append({'reports': [report]})
      continue
    best = None
    for group in groups:
      first = next((r for r in group['reports'] if r.get('kind') == 'first'), None)
      if not first or first.get('cluster') != report.get('cluster'):
        continue
      delta = abs((first.get('create_time') or 0) - (report.get('create_time') or 0))
      if delta <= _SOS_FIRST_MERGE_SECONDS and (best is None or delta < best[0]):
        best = (delta, group)
    if best:
      best[1]['reports'].append(report)
    else:
      groups.append({'reports': [report]})
  groups.extend(matched.values())
  return groups


def _pick(reports: list[dict], kind: str) -> dict | None:
  """同类通报取最早一条（真正的通报时刻）。"""
  items = [r for r in reports if r.get('kind') == kind]
  return min(items, key=lambda r: r.get('create_time') or 0) if items else None


def _pick_latest(reports: list[dict], kind: str, field: str | None = None) -> dict | None:
  """同类通报取**最新**一条（后发的通报覆盖先前的）。

  传 `field` 时只在该字段非空的行里取（用于 `event_time`：后发的残缺报文不该
  把先前解析出的发生时间冲掉）。
  """
  items = [r for r in reports if r.get('kind') == kind]
  if field:
    items = [r for r in items if r.get(field)]
  return max(items, key=lambda r: r.get('create_time') or 0) if items else None


def _report_field_latest(reports: list[dict], names: tuple[str, ...]) -> str:
  """按时间**从新到旧**取第一个非空的【字段】值。

  同一告警补发多条通报时，后发的那条覆盖先前的；只写进展、没重复【事件概述】的
  补充通报取不到值，继续往前找，不会把名称/地点冲空。
  """
  for report in sorted(reports, key=lambda r: r.get('create_time') or 0, reverse=True):
    value = _report_field(report.get('text') or '', names)
    if value:
      return value
  return ''


def _occurred_at(alarm: dict | None, reports: list[dict]) -> tuple[int | None, bool]:
  """`(发生时刻, 是否只是「消息时间」兜底)`。

  报文自身的发生时间取**最新一条非空** `event_time`（后发的补发/更正覆盖先前的）。
  兜底值只用于**把行归属到正确的统计期**（否则报文没写发生时间的行会整行消失）；
  展示层不把它当发生时间 —— `_compose` 会把这个时刻从「发生时间」列留空，
  不写推测值（用户口径：识别不出来的不要乱填写）。
  """
  if alarm:
    # 触发态时间优先：当前态 `alarm_time` 会被刷新/恢复推送覆盖（见 source._ALARM_SQL）
    ts = rules.to_epoch(alarm.get('trigger_time')) or rules.to_epoch(alarm.get('alarm_time'))
    if ts:
      return ts, False
  known = [r for r in reports if r.get('event_time')]
  if known:
    latest = max(known, key=lambda r: r.get('create_time') or 0)
    return int(latest['event_time']), False
  stamps = [r.get('create_time') for r in reports if r.get('create_time')]
  return (min(stamps) if stamps else None), True


def _compose(group: dict, alarm: dict | None, buildings: dict, index: int,
             spaces: dict | None = None) -> dict | None:
  reports = group['reports']
  # 时间列/违规判定取**最早**（真正的通报时刻）；正文与字段取**最新**（后发覆盖）。
  sos = _pick(reports, 'sos')
  first = _pick(reports, 'first')
  latest_sos = _pick_latest(reports, 'sos')
  latest_first = _pick_latest(reports, 'first')
  occurred, estimated = _occurred_at(alarm, reports)
  if not occurred:
    return None

  # 进展要扫**全组**报文的正文：补发的通报可能只带【进展描述】，不重复首版字段
  progress_text = '\n'.join(
    r.get('text') or '' for r in sorted(reports, key=lambda r: r.get('create_time') or 0))

  if alarm:
    cluster = alarm.get('cluster')
    key = (cluster, alarm.get('space_guid'))
    # 地点 = 告警空间全名（与 query_alarms.space_name 同源）；取不到才降级到楼宇/园区
    place = ((spaces or {}).get(key) or '').strip() \
        or rules.place(cluster, buildings.get(key, ''))
  else:
    place = _report_field_latest(reports, ('地点', '影响范围', '事件位置')) or rules.park_of(
      (latest_first or latest_sos or {}).get('cluster'))[0]

  # 事件名称 = 地点 + 设备 + 测点 + 告警名称（对齐值守台账的写法）；
  # 无告警时退回报文自身的【事件概述】
  alarm_name = (alarm.get('content') or '').strip() if alarm else ''
  if alarm:
    parts = [place, alarm.get('device_name'), alarm.get('point_name'), alarm_name]
    name = ' - '.join(p.strip() for p in parts if p and p.strip())
  else:
    name = _report_field_latest(reports, ('事件概述', '事件名称', '事件类型'))

  order_no = alarm.get('order_no') if alarm else None
  # 值守在卡片里手写的进展优先（扫全组报文：事件结束的处理结果 > 编号最大的进展块）；
  # 一条进展都取不到才回退工单/告警状态
  card_progress = _latest_progress(progress_text)
  progress = rules.event_progress(
    order_no,
    alarm.get('process_result') if alarm else None,
    alarm.get('order_status') if alarm else None,
    alarm.get('alarm_status') if alarm else None,
    matched=bool(alarm),
  )
  start_s = alarm.get('start_time') if alarm else None
  end_s = alarm.get('end_time') if alarm else None
  sos_s = sos.get('create_time') if sos else None
  first_s = first.get('create_time') if first else None
  event_name = name.strip()

  return {
    '序号': index,
    # 日期始终填（按速报/首报当天归档，Excel 按日期筛选才对得上）；
    # 发生时间是推测值时留空 —— 两列都写同一个时刻正是这次要修的 bug
    '日期': datetime.fromtimestamp(occurred, _TZ).date(),
    '发生时间': '' if estimated else rules.fmt_event_time(occurred),
    '速报时间': rules.fmt_event_time(sos_s),
    '首报时间': rules.fmt_event_time(first_s),
    '地点': place,
    '事件等级': '',
    # 匹配到告警时事件类型就是告警名称；无告警用报文【事件类型】/ 规则分类兜底
    '事件类型': alarm_name or _report_field_latest(reports, ('事件类型',))
                or rules.event_type(event_name),
    '事件名称': event_name,
    '专业': '',
    '事件原因': (alarm.get('process') or '').strip() if alarm else '',
    '事件进展': card_progress or progress,
    '是否完成': '',
    '完成时间': rules.fmt_event_time(end_s),
    '处理结果': progress,
    '处理时长': rules.duration_text(start_s, end_s),
    # 发生时间不明时不判违规（rules.violation 对 None 返回空串，不猜）
    '过程有无违规': rules.violation(
      None if estimated else occurred, sos_s, first_s,
      int(settings.INCIDENT_SOS_LIMIT_SECONDS),
      int(settings.INCIDENT_FIRST_LIMIT_SECONDS),
    ),
    '应急超15min违规': '',
    '实时跟踪人员': '',
    '经手人': '',
    # 名称取不到时（无告警 + 报文里也没有【事件概述】）名称列留空，改放原文：
    # 如华为 eWatch 推送，字段不是【】标签，只有原始报文能说明发生了什么
    '速报内容': '' if event_name else _plain_text((latest_sos or {}).get('text') or '').strip(),
    '首报内容': '' if event_name else _plain_text((latest_first or {}).get('text') or '').strip(),
  }


def _sanitize_row(row: dict) -> dict:
  """出口兜底：所有字符串单元格再去一次卡片标记（幂等）。

  取值路径分散在告警/报文/工单三处，新增列时容易漏清理；标记一旦进表格，
  Markdown 渲染会把 `**` 当加粗、把 `<font>` 当 HTML，整行显示就乱了。
  """
  return {key: _plain_text(value) if isinstance(value, str) else value
          for key, value in row.items()}


def build_rows(start_s: int, end_s: int, cluster_prefix: str | None = None,
               chat_ids: list[str] | None = None) -> list[dict]:
  """窗口内（按发生时间）的事件统计行，按发生时间升序。

  `chat_ids` 是该园区配置的群：传了才只统计这些群里的通报（出表一律传）。
  """
  reports = source.load_reports(
    int(start_s) - _WINDOW_PAD_SECONDS, int(end_s) + _WINDOW_PAD_SECONDS, cluster_prefix,
    chat_ids)
  if not reports:
    return []

  groups = _group_reports(reports)
  guids = [r['event_log_id'] for r in reports if r.get('event_log_id')]
  alarms = source.load_alarms(guids)
  spaces = source.load_spaces()
  buildings = source.building_index(spaces)
  space_names = source.space_index(spaces)

  rows: list[dict] = []
  for group in groups:
    alarm = None
    for report in group['reports']:
      if report.get('event_log_id'):
        alarm = alarms.get(report['event_log_id'])
        break
    occurred, _ = _occurred_at(alarm, group['reports'])
    if not occurred or not (int(start_s) <= occurred < int(end_s)):
      continue
    row = _compose(group, alarm, buildings, len(rows) + 1, space_names)
    if row:
      rows.append(_sanitize_row(row))

  rows.sort(key=lambda r: (r['发生时间'], r['事件名称']))
  for i, row in enumerate(rows, 1):
    row['序号'] = i
  missing = sum(1 for row in rows if not row.get('发生时间'))
  if missing:
    logger.warning('[Incident] 本期 %d 行缺发生时间（报文未提供，按速报/首报时间归档）', missing)

  unmatched = sum(1 for g in groups if not any(r.get('event_log_id') for r in g['reports']))
  if unmatched:
    logger.info('[Incident] 本期 %d 条事件无关联告警（仅用报文信息）', unmatched)
  logger.info('[Incident] 事件统计：通报 %d 条 → 事件 %d 行', len(reports), len(rows))
  return rows


def outside_events(start_s: int, end_s: int, cluster_prefix: str | None = None,
                   chat_ids: list[str] | None = None, limit: int = 5) -> dict:
  """窗口内 0 行时，找出「通报在本窗口、事件发生时间却在窗口外」的补录事件。

  典型场景（2026-10-26 实测）：昨夜 22:50 的告警，今早 09:56 才发速报/通报。表按
  **发生时间**归属统计期，这行属于前一晚班，因此查「今天白班」是空表 —— 但不是没收到
  消息。统一用这里的探测结果向用户说明「消息收到了、事件落在哪一班、该查哪天」。

  只取 `create_time` 落在窗口内的通报（不加宽），正是「本窗口收到的补录」这一情形。
  """
  reports = source.load_reports(start_s, end_s, cluster_prefix, chat_ids)
  if not reports:
    return {'reports': 0, 'events': 0, 'items': [], 'truncated': False}

  groups = _group_reports(reports)
  guids = [r['event_log_id'] for r in reports if r.get('event_log_id')]
  alarms = source.load_alarms(guids)

  items: list[dict] = []
  for group in groups:
    alarm = None
    for report in group['reports']:
      if report.get('event_log_id'):
        alarm = alarms.get(report['event_log_id'])
        break
    occurred, _ = _occurred_at(alarm, group['reports'])
    if not occurred or int(start_s) <= occurred < int(end_s):
      continue
    items.append({
      'occurred': int(occurred),
      'cluster': (alarm or {}).get('cluster') or group['reports'][0].get('cluster'),
      'name': (alarm or {}).get('content')
              or _report_field_latest(group['reports'], ('事件概述', '事件名称', '事件类型')),
    })
  items.sort(key=lambda x: x['occurred'])
  return {'reports': len(reports), 'events': len(items),
          'items': items[:limit], 'truncated': len(items) > limit}


def summary_counts(rows: list[dict]) -> list[dict]:
  """事件汇总（园区 × I0-I4）。事件等级暂留空 → 计数为 0，结构先立住。"""
  levels = ('I0', 'I1', 'I2', 'I3', 'I4')
  parks: dict[str, dict[str, int]] = {}
  for row in rows:
    park = rules.park_of(_park_from_place(row.get('地点', '')))[0] or '未知'
    bucket = parks.setdefault(park, {level: 0 for level in levels})
    level = (row.get('事件等级') or '').strip()
    if level in bucket:
      bucket[level] += 1
  out = [{'园区': park, **counts, '总计': sum(counts.values())} for park, counts in
         sorted(parks.items())]
  if out:
    out.append({'园区': '总计', **{level: sum(r[level] for r in out) for level in levels},
                '总计': sum(r['总计'] for r in out)})
  return out


def _park_from_place(place: str) -> str:
  """地点文案 → 园区名（`廊坊园区A12数据中心` → `廊坊`）。"""
  for park in ('廊坊', '平湖', '佛山', '惠州', '重庆'):
    if place.startswith(park):
      return park
  return place[:2] if place else ''


def today() -> date:
  return datetime.now(_TZ).date()

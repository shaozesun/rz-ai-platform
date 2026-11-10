"""事件单 ↔ DCIM 告警「判定表」对话工具（`event_alarm_link`）。

**先查表、缺才补判**：判定表 Mongo `event_alarm_link_decisions` 一行 = 一次发生，
键 = `(工单号, 发生时间, 口径)`（口径是园区 slug 或 `nationwide`）。报告与对话共用
同一张表 —— 报告判过的发生，对话里直接出结论；对话补判的，报告下一轮也复用。

缺失的发生（报告上线前的单子、还没跑过的期次）才补算：按 `ticket_no` 或日期区间
调综合管理平台取事件列表，逐条取详情补事件名称/描述，再走 `alarm_link.link_occurrence`
（时间窗 ±120s → 机房码收敛 → 点位签名 → 模型仲裁），判定后批量写回判定表。

**交付由服务端渲染**（同 `incident_tools` 的口径）：固定 16 列、列序与文案都由平台
决定，模型只复制 `table_markdown` 与 `file_tag`，不得自选列或另造表。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.agent.user_context import get_current_user_id
from service.scheduled_task.report import alarm_decision, alarm_link

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_INLINE_MAX_ROWS = 20          # ≤20 条给文字表 + 交互表格；>20 条只给交互表格
_PAGE_SIZE = 500               # 与 AGENT_ANALYSIS_PAGE_SIZE 同量级
_MAX_PAGES = 5000              # 仅防服务端 total 异常导致的死循环

COLUMNS = ('日期', '发生时间', '工单编号', '事件名称', '部门', '关联结论', '匹配方式',
           '告警时间', '等级', '设备', '测点', '告警内容', '告警值', '四档阈值',
           '越限档', '说明')

_STATUS_TEXT = {
  'matched': '已关联',
  'not_covered': '未覆盖（非廊坊或早于 2026-10-29）',
  'no_alarm': '±2 分钟内无告警',
  'ambiguous': '多候选待判定',
  'unknown': '暂无法判定',
  'no_time': '缺发生时间',
  'fetch_error': '取详情失败',
  'undecided': '未判定',
}

_MATCH_TEXT = {'point': '点位签名', 'time': '时间窗唯一候选', 'llm': '模型仲裁'}


def _err(message: str) -> str:
  return json.dumps({'error': message}, ensure_ascii=False)


def _get_thread_id(config: RunnableConfig | None) -> str:
  if not config:
    return 'default'
  return (config.get('configurable') or {}).get('thread_id') or 'default'


def _resolve_park(park) -> str:
  """`廊坊园区`/`廊坊` → `廊坊`；空或「全国」→ `nationwide`；未知返回空串。"""
  from service.scheduled_task import report_templates

  raw = str(park or '').strip()
  if not raw or raw in ('nationwide', '全国', '全国各园区'):
    return 'nationwide'
  for item in report_templates.PARKS:
    if raw in (item['slug'], item['name']):
      return item['slug']
  return ''


def _parse_bound(value: str, label: str) -> tuple[datetime, bool]:
  text = str(value or '').strip()
  for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
    try:
      return datetime.strptime(text, fmt).replace(tzinfo=_TZ), '%H' in fmt
    except ValueError:
      continue
  raise ValueError(f'{label} 需为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（收到 {value!r}）')


def _parse_range(start: str, end: str) -> tuple[datetime, datetime]:
  """日期区间 → [开始, 结束)；只给开始 = 当天（带时分则 1 小时）。"""
  if not start.strip():
    raise ValueError('传 end 时需同时传 start')
  start_dt, start_has_time = _parse_bound(start, 'start')
  if end.strip():
    end_dt, end_has_time = _parse_bound(end, 'end')
    if end_has_time != start_has_time:
      raise ValueError('start 与 end 的精度要一致（都带时分或都不带）')
    if not end_has_time:
      end_dt += timedelta(days=1)      # 自然日含首尾
  else:
    end_dt = start_dt + (timedelta(hours=1) if start_has_time else timedelta(days=1))
  if end_dt <= start_dt:
    raise ValueError('end 必须晚于 start')
  return start_dt, end_dt


def _scope_label(scope: str) -> str:
  if scope == 'nationwide':
    return '全国各园区'
  from service.scheduled_task import report_templates

  for item in report_templates.PARKS:
    if item['slug'] == scope:
      return item['name']
  return scope


def _scope_params(scope: str) -> dict:
  """园区 → `company_id`（与园区报告同一口径）；全国 → `all_companies`。"""
  if scope == 'nationwide':
    return {'all_companies': 'true'}
  from service.scheduled_task import report_templates

  for item in report_templates.PARKS:
    if item['slug'] == scope:
      return {'company_id': item['kingdee']}
  raise ValueError(f'园区 {scope} 不在平台园区清单里')


async def _load_decisions(store, ticket: str, window, scope: str) -> dict:
  """读判定表：按工单号或发生时间区间 + 口径。失败返回空表（按未命中处理）。"""
  try:
    if ticket:
      docs = await store.query(ticket_no=ticket, scope=scope)
    elif window is not None:
      docs = await store.query(
        start_ts=int(window[0].timestamp()),
        end_ts=int(window[1].timestamp()) - 1, scope=scope)
    else:
      return {}
  except Exception as e:                  # noqa: BLE001 —— 读表失败只降级
    logger.warning('[EventAlarm] 判定表查询失败：%s', str(e)[:200])
    return {}
  return {doc['_id']: doc for doc in docs if doc.get('_id')}


async def _list_tickets(scope: str, window, ticket: str, user_id: str) -> list[dict]:
  """事件工单列表（全量翻页；园区口径带 company_id，全国 all_companies）。"""
  from service.query.mgmt import client

  params = _scope_params(scope)
  if ticket:
    params['ticket_no'] = ticket
  else:
    params['start_time'] = window[0].strftime('%Y-%m-%d %H:%M:%S')
    params['end_time'] = (window[1] - timedelta(seconds=1)).strftime(
      '%Y-%m-%d %H:%M:%S')

  records: list[dict] = []
  page, size = 1, _PAGE_SIZE
  while page <= _MAX_PAGES:
    resp = await client.call('mgmt.query_event_ticket',
                             {**params, 'page': page, 'page_size': size},
                             on_behalf_of=user_id or None)
    rows = (resp or {}).get('records') or (resp or {}).get('simpleTicketVOList') or []
    rows = [r for r in rows if isinstance(r, dict)]
    records.extend(rows)
    if not rows:
      break
    total = (resp or {}).get('total')
    try:
      total_n = int(total)
    except (TypeError, ValueError):
      total_n = -1
    # 有 total 就按它判断（服务端可能把 page_size 压到 20，不能按「不足一页」提前停）
    if total_n >= 0:
      if len(records) >= total_n:
        break
    elif len(rows) < size:
      break
    page += 1
  if page > _MAX_PAGES:
    logger.warning('[EventAlarm] 事件列表翻页超过 %d 页，提前停止', _MAX_PAGES)
  return records


async def _fetch_detail(ticket_id, user_id: str) -> dict:
  """事件详情 → 裁剪字段（事件名称/描述/级别/专业等）。"""
  from service.query.mgmt import client
  from tools.analysis_tools import _pick_detail_fields

  resp = await client.call('mgmt.query_event_ticket_detail',
                           {'ticket_id': ticket_id},
                           on_behalf_of=user_id or None)
  return _pick_detail_fields('mgmt.query_event_ticket_detail', resp or {})


class _SharedRepo:
  """`link_occurrence` 每次都会 close 它拿到的 repo —— 这里让多次调用共用一个连接。"""

  def __init__(self, repo):
    self._repo = repo

  def __getattr__(self, name):
    return getattr(self._repo, name)

  def close(self):                        # 真正的连接由 `_RepoHolder.close()` 关
    pass


class _RepoHolder:
  """DCIM 连接的惰性持有者：全部命中判定表时不碰 PostgreSQL。"""

  def __init__(self):
    self.repo = None

  def get(self):
    if self.repo is None:
      from service.dcim_ingest.repository import DcimRepository

      self.repo = DcimRepository(settings.DCIM_PG_DSN)
    return _SharedRepo(self.repo)

  def close(self) -> None:
    if self.repo is not None:
      try:
        self.repo.close()
      except Exception:                   # noqa: BLE001 —— 关闭失败不影响结果
        logger.debug('[EventAlarm] 关闭 DCIM 连接失败', exc_info=True)


def _no_time_view(reason: str = '该次发生缺少发生时间，未查询 DCIM 告警') -> dict:
  return {'status': 'no_time', 'match': '', 'reason': reason, 'alarm': None,
          'alarm_ts': None, 'alarm_snapshot': None, 'point_sig': None}


def _undecided_view(reason: str) -> dict:
  return {'status': 'undecided', 'match': '', 'reason': reason, 'alarm': None,
          'alarm_ts': None, 'alarm_snapshot': None, 'point_sig': None}


def _hit_time(ts) -> str:
  try:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(ts)))
  except (TypeError, ValueError, OSError):
    return ''


def _day_of(ts) -> str:
  try:
    return time.strftime('%Y-%m-%d', time.localtime(int(ts)))
  except (TypeError, ValueError, OSError):
    return '—'


def _cell(value) -> str:
  text = '' if value is None else str(value)
  text = re.sub(r'\s+', ' ', text).strip()
  return text or '—'


def _fallback_alarm_view(key: tuple, info: dict) -> dict:
  """实时补齐失败时按判定表快照展示（值/阈值本来就只实时取）。"""
  snap = (info or {}).get('snapshot') or {}
  ts = (info or {}).get('alarm_ts') or snap.get('ts')
  return {'time': alarm_link._fmt_time(ts) if ts else '',
          'level': snap.get('level') or '', 'device': snap.get('device') or '',
          'point': snap.get('point') or '', 'content': snap.get('content') or '',
          'value': None, 'unit': '', 'thresholds': {}, 'breached': []}


def _threshold_text(thresholds: dict) -> str:
  parts = [f'{label} {alarm_link.format_trigger(bounds)}'
           for label, bounds in (thresholds or {}).items()]
  return '；'.join(parts) if parts else '—'


def _row(item: dict, alarm: dict | None) -> dict:
  view = item.get('view') or {}
  alarm = alarm or {}
  ts = item.get('ts')
  return {
    '日期': _day_of(ts) if ts else '—',
    '发生时间': _hit_time(ts) if ts else '—',
    '工单编号': _cell(item.get('ticket_no')),
    '事件名称': _cell(item.get('name')),
    '部门': _cell(item.get('dc')),
    '关联结论': _STATUS_TEXT.get(view.get('status') or '', view.get('status') or '—'),
    '匹配方式': _MATCH_TEXT.get(view.get('match') or '', '—'),
    '告警时间': _cell(alarm.get('time')),
    '等级': _cell(alarm.get('level')),
    '设备': _cell(alarm.get('device')),
    '测点': _cell(alarm.get('point')),
    '告警内容': _cell(alarm.get('content')),
    '告警值': _cell(alarm_link.format_value(alarm.get('value'),
                                          alarm.get('unit') or '')),
    '四档阈值': _threshold_text(alarm.get('thresholds') or {}),
    '越限档': _cell('、'.join(alarm.get('breached') or [])),
    '说明': _cell(view.get('reason')),
  }


def _markdown_table(columns: tuple, items: list[dict]) -> str:
  """服务端渲染文字表：列与列序固定，模型只复制。"""

  def cell(value) -> str:
    text = _cell(value)
    return text.replace('|', r'\|') if text != '—' else text

  head = '| ' + ' | '.join(columns) + ' |'
  sep = '| ' + ' | '.join('---' for _ in columns) + ' |'
  body = ['| ' + ' | '.join(cell(r.get(c)) for c in columns) + ' |' for r in items]
  return '\n'.join([head, sep] + body)


def _merge_docs(items: dict, docs: dict, scope: str) -> None:
  """判定表已有记录 → 输出行（refresh 时由调用方传空 docs）。"""
  for did, doc in docs.items():
    view = alarm_decision.decision_view(doc)
    items[did] = {
      'ticket_no': str(doc.get('ticket_no') or ''),
      'ts': int(doc.get('occurrence_ts') or 0) or None,
      'name': view.get('name') or '',
      'dc': view.get('dc') or '',
      'view': view,
      'reused': True,
      'ticket_id': None,
      'rec': {},
    }


async def _compute(items: list[dict], *, scope: str, refresh: bool,
                   user_id: str, store) -> tuple[list[dict], int]:
  """逐条补判（串行：mgmt 同账号同接口 1 在途），返回 (写回判定表的文档, LLM 次数)。"""
  company_parks = None
  if scope == 'nationwide':
    company_parks = await alarm_link._company_parks()

  holder = _RepoHolder()
  computed: list[dict] = []
  llm_calls = 0
  try:
    for i, item in enumerate(items, 1):
      ticket_no = item['ticket_no']
      ts = item['ts']
      if ts is None:
        item['view'] = _no_time_view()
        continue

      fields: dict = {}
      error = ''
      if item.get('ticket_id'):
        try:
          fields = await _fetch_detail(item['ticket_id'], user_id)
        except Exception as e:            # noqa: BLE001 —— 单条失败只标该行
          error = f'取事件详情失败：{type(e).__name__}: {str(e)[:120]}'
      if error:
        item['view'] = {'status': 'fetch_error', 'match': '', 'reason': error,
                        'alarm': None, 'alarm_ts': None, 'alarm_snapshot': None,
                        'point_sig': None}
        logger.warning('[EventAlarm] %s %s', ticket_no, error)
        continue

      name = fields.get('事件名称') or item.get('name') or ''
      item['name'] = name
      item['dc'] = item.get('dc') or fields.get('部门') or ''
      details = {'rows': [{'ticketNo': ticket_no, 'fields': fields}]}
      meta = {'ticketNo': ticket_no,
              'occurred_at': alarm_link._fmt_time(ts), 'dc': item['dc']}
      try:
        result = await alarm_link.link_occurrence(
          ticket_no=ticket_no, occurrence_ts=ts, name=name,
          rec=item.get('rec') or {}, meta=meta, details=details,
          scope=scope, company_parks=company_parks,
          repo_factory=holder.get, store=None,
          force_refresh=refresh, source='chat')
      except Exception as e:              # noqa: BLE001 —— 单条失败不整单失败
        item['view'] = {'status': 'unknown', 'match': '',
                        'reason': f'判定异常：{type(e).__name__}: {str(e)[:120]}',
                        'alarm': None, 'alarm_ts': None,
                        'alarm_snapshot': None, 'point_sig': None}
        logger.warning('[EventAlarm] %s 判定失败：%s', ticket_no, e)
        continue

      item['view'] = result
      llm_calls += int(result.get('llm_calls') or 0)
      if result.get('decision'):
        computed.append(result['decision'])
      if i % 20 == 0:
        logger.info('[EventAlarm] 补判进度 %d/%d', i, len(items))
  finally:
    holder.close()

  if computed:
    try:
      await store.save(computed)
    except Exception as e:                # noqa: BLE001 —— 写失败不影响本次结果
      logger.warning('[EventAlarm] 判定写回失败：%s', str(e)[:200])
  return computed, llm_calls


def _empty_note(ticket: str, scope_label: str, backfill_error: str,
                docs: dict, records: list[dict]) -> str:
  if backfill_error:
    return backfill_error
  if ticket:
    return (f'没有找到工单 {ticket}：判定表与综合管理平台事件列表都没有该记录'
            f'（口径：{scope_label}）。')
  if not docs and not records:
    return f'该区间没有事件工单，判定表也没有记录（口径：{scope_label}）。'
  return f'本次没有可输出的事件发生（口径：{scope_label}）。'


@tool
async def event_alarm_link(
  ticket_no: str = '',
  start: str = '',
  end: str = '',
  park: str = '',
  refresh: bool = False,
  link_missing: bool = True,
  config: RunnableConfig = None,
) -> str:
  """查询事件工单关联的 DCIM 告警（先查判定表，缺失才补判）。

  **一行 = 一次发生**。判定表（Mongo `event_alarm_link_decisions`）里已有的结论直接复用，
  不查库、不调模型；没有的（报告上线前的单子、没跑过的期次）才按时间窗 → 机房码 →
  点位签名 → 模型仲裁补判，并把结论写回判定表供报告与后续对话复用。

  查询范围二选一（至少给一个）：
  - `ticket_no`：按工单编号精确查（可另配 `park` 指定口径）；
  - `start`/`end`：按**发生时间**的日期区间查（`YYYY-MM-DD` 含首尾；只给 `start` = 当天）。
  日期区间会逐条补判区间内**全部**事件单，大区间较慢（mgmt 串行取详情）。

  交付：返回 `mode` 决定怎么输出，**不要自己重排/删列/另造表**：
  - `inline`（≤20 条）：原样输出 `table_markdown` + `file_tag`（交互表格）；
  - `records`（>20 条）：原样输出 `file_tag`，再补一两句结论；
  - `rows=0`：原样转述 `note`，不产文件。

  Args:
    ticket_no: 事件工单编号（如 `SJ20261030001`）。与 `start` 至少给一个；同时给时以它为准。
    start: 起始日期 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM`（按事件发生时间筛）。
    end: 结束日期，精度与 `start` 一致；留空 = start 当天（带时分则 1 小时）。
    park: 园区口径，如 `廊坊`（园区名或短名都行）。留空/`全国` = 全国口径
      （法人主体经公司档案换算园区）。判定结论按口径分开存，互不串用。
    refresh: 默认 false：命中判定表直接出结论；true = 忽略已存判定、全部重算并覆盖写回。
    link_missing: 默认 true：缺失的发生补判；false 只标注「未判定」，不补算、不查 DCIM。
  """
  started = time.time()
  ticket = str(ticket_no or '').strip()
  if not ticket and not str(start or '').strip():
    return _err('请至少给 ticket_no 或日期区间（start/end）')

  window = None
  if str(start or '').strip() or str(end or '').strip():
    try:
      window = _parse_range(str(start or ''), str(end or ''))
    except ValueError as e:
      return _err(str(e))

  if not settings.DCIM_ENABLED:
    return _err('DCIM 数据接入未启用（DCIM_ENABLED=false），无法判定告警关联')

  scope = _resolve_park(park)
  if not scope:
    from service.scheduled_task import report_templates

    names = '、'.join(p['slug'] for p in report_templates.PARKS)
    return _err(f'未知园区 {park!r}；可选：{names} 或全国')
  scope_label = _scope_label(scope)
  user_id = get_current_user_id()

  store = alarm_decision.AlarmDecisionStore()
  docs = {} if refresh else await _load_decisions(store, ticket, window, scope)

  # 需要拉事件列表：按区间查（要枚举区间内全部单子）、refresh、或按单号查但判定表没有
  need_list = bool(window) or refresh or not docs
  records: list[dict] = []
  backfill_error = ''
  if need_list:
    if not settings.MGMT_ENABLED:
      if not docs:
        return _err('综合管理平台未启用（MGMT_ENABLED=false），无法查询事件工单；'
                    '判定表也没有该范围的记录')
      backfill_error = ('综合管理平台未启用（MGMT_ENABLED=false），'
                        '未判定的发生本次未补算')
    else:
      try:
        records = await _list_tickets(scope, window, ticket, user_id)
      except Exception as e:            # noqa: BLE001 —— 已有判定照常输出
        if not docs:
          logger.exception('[EventAlarm] 事件列表查询失败')
          return _err(f'查询事件工单失败：{type(e).__name__}: {str(e)[:200]}')
        backfill_error = (f'查询事件工单失败：{type(e).__name__}: '
                          f'{str(e)[:120]}；仅返回判定表已有记录')

  items: dict = {}
  _merge_docs(items, docs, scope)

  pending: list[dict] = []
  seen: set = set()
  for rec in records:
    no = str(rec.get('ticketNo') or '').strip()
    ts = alarm_link._to_seconds(rec.get('occurrenceTime'))
    did = alarm_decision.decision_id(no, ts, scope)
    key = did or ('no_time', no, len(seen))
    if key in seen:
      continue
    seen.add(key)
    if did and did in items:            # 判定表已有（refresh=false）
      continue
    item = {
      'ticket_no': no,
      'ts': ts,
      'ticket_id': rec.get('ticketId'),
      'rec': rec,
      'name': str(rec.get('projectName') or ''),
      'dc': str(rec.get('dc') or ''),
      'view': None, 'reused': False,
    }
    items[key] = item
    pending.append(item)

  llm_calls = 0
  computed: list[dict] = []
  if pending:
    if not link_missing:
      for item in pending:
        item['view'] = _undecided_view('未判定（本次未补算）')
    elif backfill_error or not settings.MGMT_ENABLED:
      for item in pending:
        item['view'] = _undecided_view(backfill_error or '综合管理平台未启用，未补算')
    else:
      computed, llm_calls = await _compute(
        pending, scope=scope, refresh=refresh, user_id=user_id, store=store)
      computed = computed or []

  ordered = sorted(items.values(),
                   key=lambda x: (x.get('ts') is None, x.get('ts') or 0,
                                  x.get('ticket_no') or ''))

  identities: dict = {}
  for item in ordered:
    view = item.get('view') or {}
    alarm = view.get('alarm')
    if not alarm:
      continue
    key = (str(alarm.get('cluster') or ''), str(alarm.get('event_log_id') or ''))
    if not key[0] or not key[1]:
      continue
    info = identities.setdefault(key, {})
    if not info.get('alarm_ts') and view.get('alarm_ts'):
      info['alarm_ts'] = view['alarm_ts']
    if not info.get('snapshot') and view.get('alarm_snapshot'):
      info['snapshot'] = view['alarm_snapshot']

  alarm_views: dict = {}
  if identities:
    holder = _RepoHolder()
    try:
      for view in alarm_link.enrich_identities(identities, repo_factory=holder.get):
        alarm_views[(view['cluster'], view['event_log_id'])] = view
    except Exception as e:              # noqa: BLE001 —— 展示字段失败用快照兜底
      logger.warning('[EventAlarm] 告警展示字段补齐失败：%s', str(e)[:200])
      alarm_views = {key: _fallback_alarm_view(key, info)
                     for key, info in identities.items()}
    finally:
      holder.close()

  rows = []
  for item in ordered:
    view = item.get('view') or {}
    alarm = view.get('alarm')
    key = (str((alarm or {}).get('cluster') or ''),
           str((alarm or {}).get('event_log_id') or ''))
    shown = alarm_views.get(key) if key[0] and key[1] else None
    if shown is None and key[0] and key[1]:
      shown = _fallback_alarm_view(key, identities.get(key) or {})
    rows.append(_row(item, shown))

  status_counts: dict = {}
  for item in ordered:
    status = (item.get('view') or {}).get('status') or 'undecided'
    status_counts[status] = status_counts.get(status, 0) + 1

  result: dict = {
    'scope': scope_label,
    'ticket_no': ticket,
    'window': (f'{window[0]:%Y-%m-%d %H:%M} ~ {window[1]:%Y-%m-%d %H:%M}'
               if window else ''),
    'rows': len(rows),
    'status_counts': status_counts,
    'reused': sum(1 for item in ordered if item.get('reused')),
    'computed': len(computed),
    'llm_calls': llm_calls,
    'elapsed_s': round(time.time() - started, 1),
    'note': alarm_link.NOTE,
  }
  if backfill_error:
    result['backfill_error'] = backfill_error

  thread_id = _get_thread_id(config)
  if rows:
    from core.sandbox.datasets import save_dataset

    info = await asyncio.to_thread(
      save_dataset, thread_id, 'event_alarm_link',
      {'ticket_no': ticket, 'start': start, 'end': end, 'park': scope},
      rows, 'event-alarm-link')
    records_url = (f'/api/v1/chat/agent/artifacts/{quote(thread_id, safe="")}/'
                   f'{quote(info["dataset_id"], safe="")}/records')
    file_tag = f'<file url="{records_url}" name="事件单告警关联"/>'
    if len(rows) > _INLINE_MAX_ROWS:
      result.update(mode='records', records_url=records_url, file_tag=file_tag)
    else:
      result.update(mode='inline',
                    table_markdown=_markdown_table(COLUMNS, rows),
                    records_url=records_url, file_tag=file_tag)
  else:
    result['mode'] = 'empty'
    result['note'] = _empty_note(ticket, scope_label, backfill_error, docs, records)
  return json.dumps(result, ensure_ascii=False)

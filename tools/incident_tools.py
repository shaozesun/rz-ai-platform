"""数据中心事件统计表工具 —— 飞书园区群通报 → 告警/工单关联。

统计口径全部在 `service/scheduled_task/incident_stats/`（与定时预设同一套），
对话侧只传窗口与交付产物、**不复算**：沙箱连不了 PG，也让模型没有改口径的余地。
交付形态：≤20 条服务端渲染文字表 + 交互表格卡片，>20 条只给交互表格，
只有明确要 Excel 才落 xlsx（表格一律由服务端生成，模型只复制，不自选列）。
"""

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.sandbox.datasets import resolve_artifact_url, thread_dir

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_WORKSPACE_ID = 'workspace'
_PARK_SUFFIX = re.compile(r'(园区|数据中心|基地)$')
_PARK_SPLIT = re.compile(r'[,，、\s]+')
_INLINE_MAX_ROWS = 20          # ≤20 条文字表 + 交互表格；>20 条只给交互表格
_HINT_MAX_ITEMS = 5            # 0 行时最多列几条「事件在窗口外」的补录通报
# 核心列**即使整列全空也保留**：「今天没有首报」本身就是结论，整列删掉后模型只能答出
# 「缺首报」的文字，速报/首报时刻全被吃掉（用户 2026-10-30 反馈「总是缺字段」）。
_CORE_COLUMNS = ('序号', '日期', '发生时间', '速报时间', '首报时间', '地点', '事件类型',
                 '事件名称', '事件进展', '过程有无违规')


def _err(message: str) -> str:
  return json.dumps({'error': message}, ensure_ascii=False)


def _get_thread_id(config: RunnableConfig | None) -> str:
  """从 RunnableConfig 取会话 thread_id（产物落在本会话目录，按会话归属鉴权）。"""
  if not config:
    return 'default'
  return (config.get('configurable') or {}).get('thread_id') or 'default'


def _configured_parks() -> list[str]:
  """配置里的园区 key（`INCIDENT_FEISHU_PARK_CHATS` 的键）。"""
  raw = (settings.INCIDENT_FEISHU_PARK_CHATS or '').strip()
  if not raw:
    return []
  try:
    conf = json.loads(raw)
  except json.JSONDecodeError:
    logger.warning('[Incident] INCIDENT_FEISHU_PARK_CHATS 不是合法 JSON')
    return []
  return [str(k) for k in conf] if isinstance(conf, dict) else []


def _split_parks(value: str) -> list[str]:
  return [p for p in _PARK_SPLIT.split(value or '') if p]


def _visible_rows(rows: list[dict]) -> list[dict]:
  """本窗口有效的列（丢全空列，核心列除外）+ date 转字符串。

  22 列里事件等级/专业/是否完成/应急超15min违规/实时跟踪人员/经手人恒空（业务等级表
  未接入），直接给模型或交互表格只会干扰阅读；但 `_CORE_COLUMNS` 里那几列即使全空也留着
  —— 否则「速报时间/首报时间」整列消失，模型只能写「缺首报」而给不出时刻。
  `date` 必须转字符串，否则 json.dumps 抛错。

  inline 交付的文本表由 `_markdown_table` 用这里的行**服务端渲染**：模型只复制，
  不再自己挑列（2026-11-06 实测它会把速报/首报等列删掉）。
  """
  if not rows:
    return []
  from service.scheduled_task.incident_stats.build import COLUMNS

  keys = [c for c in COLUMNS
          if c in _CORE_COLUMNS or any(str(r.get(c) or '').strip() for r in rows)]
  return [
    {k: (v.isoformat() if isinstance(v, date) else v)
     for k, v in ((c, r.get(c)) for c in keys)}
    for r in rows
  ]


def _markdown_table(columns: list[str], items: list[dict]) -> str:
  """把 `_visible_rows` 的结果渲染成 Markdown 表（inline 交付用）。

  表格在服务端生成、列与列序由 `build.COLUMNS` 决定，模型只负责原样输出 —— 让它自己
  拼表时实测会擅自删列（2026-11-06：速报时间/首报时间/事件名称/事件进展整列消失）。
  """
  if not columns or not items:
    return ''

  def cell(value) -> str:
    text = '' if value is None else str(value)
    text = re.sub(r'\s+', ' ', text).strip()
    return text.replace('|', r'\|') if text else '—'

  head = '| ' + ' | '.join(columns) + ' |'
  sep = '| ' + ' | '.join('---' for _ in columns) + ' |'
  body = ['| ' + ' | '.join(cell(r.get(c)) for c in columns) + ' |' for r in items]
  return '\n'.join([head, sep] + body)


def _resolve_parks(value: str) -> tuple[list[str], list[str]]:
  """请求里的园区名 → 配置 key；返回 (命中, 未命中)。空请求 = 全部已配置园区。"""
  configured = _configured_parks()
  requested = _split_parks(value)
  if not requested:
    return configured, []

  hit: list[str] = []
  missing: list[str] = []
  for name in requested:
    slug = _PARK_SUFFIX.sub('', name)
    found = next((c for c in configured if c in (name, slug)), '')
    if not found and slug:
      # 「廊坊园区事件统计」这类口误按前缀吸附（只有一个候选时才算命中）
      cands = [c for c in configured if name.startswith(c) or c.startswith(slug)]
      found = cands[0] if len(cands) == 1 else ''
    if not found:
      missing.append(name)
    elif found not in hit:
      hit.append(found)
  return hit, missing


# 班次切分：白班 08:30–17:30，晚班 17:30–次日 08:30（值守按班次记录告警工单）
_DAY_START = (8, 30)
_DAY_END = (17, 30)
_NIGHT_HOURS = 15   # 晚班 17:30–次日 08:30


def _current_shift(now: datetime | None = None) -> tuple[datetime, datetime, str]:
  """当前班次的 [开始, 结束) 窗口。00:00–08:30 算前一晚班（还没换班）。"""
  now = (now or datetime.now(_TZ)).astimezone(_TZ)
  day_start = now.replace(hour=_DAY_START[0], minute=_DAY_START[1],
                          second=0, microsecond=0)
  day_end = now.replace(hour=_DAY_END[0], minute=_DAY_END[1],
                        second=0, microsecond=0)
  if now >= day_end:
    return day_end, day_end + timedelta(hours=_NIGHT_HOURS), '晚班'
  if now >= day_start:
    return day_start, day_end, '白班'
  return day_start - timedelta(hours=_NIGHT_HOURS), day_start, '晚班'


def _shift_bounds(day: date, kind: str) -> tuple[datetime, datetime, str]:
  """某一天的班次窗口；`kind='day'|'night'`。"""
  if kind == 'night':
    start = datetime(day.year, day.month, day.day, _DAY_END[0], _DAY_END[1], tzinfo=_TZ)
    return start, start + timedelta(hours=_NIGHT_HOURS), '晚班'
  start = datetime(day.year, day.month, day.day, _DAY_START[0], _DAY_START[1], tzinfo=_TZ)
  end = datetime(day.year, day.month, day.day, _DAY_END[0], _DAY_END[1], tzinfo=_TZ)
  return start, end, '白班'


def _recent_shift_day(kind: str, now: datetime) -> date:
  """最近一次**已开始**的该班次所在日期（永不返回还没到点的未来班次）。

  取当天班次起点与 now 比：还没到就退回前一天。凌晨 03:00 问「晚班」= 昨晚 17:30 起
  那班（跨零点、正在进行），不是今晚还没到的班。
  """
  hour, minute = _DAY_START if kind == 'day' else _DAY_END
  boundary = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
  return (now if now >= boundary else now - timedelta(days=1)).date()


def _parse_day(value: str) -> date:
  try:
    return datetime.strptime(value.strip(), '%Y-%m-%d').date()
  except ValueError:
    raise ValueError(f'date 需为 YYYY-MM-DD（收到 {value!r}）')


def _resolve_shift(shift: str, date_str: str,
                   now: datetime | None = None) -> tuple[datetime, datetime, str]:
  """班次语义 → `(开始, 结束, 班次名)`；`now` 可注入，便于离线断言。

  规则（与 docstring / skill 同一口径）：
  - 空 / `current` / `today` → 当前班次；
  - `last` → 最近一个**已结束**的班次（交接后问「上一班」）；
  - `yesterday` → 昨天白班+晚班（昨天 08:30 ~ 今天 08:30）；
  - `day` / `night` [+ `date`] → 指定日（缺省=最近一次已开始）的对应班次；
  - 只传 `date` → 该日白班+晚班（该日 08:30 ~ 次日 08:30）。
  """
  now = (now or datetime.now(_TZ)).astimezone(_TZ)
  name = (shift or '').strip().lower()
  if date_str and not name:
    # 只给日期 = 该日白班+晚班（时段仍按班次切，不是自然日 00:00–24:00）
    day = _parse_day(date_str)
    return _shift_bounds(day, 'day')[0], _shift_bounds(day, 'night')[1], '白班+晚班'
  if name in ('', 'current', 'today'):
    if date_str:
      raise ValueError('shift=current 时不要传 date（指定某一天请用 shift=day/night）')
    return _current_shift(now)
  if name == 'last':
    if date_str:
      raise ValueError('shift=last 不接受 date')
    current_start = _current_shift(now)[0]
    return _current_shift(current_start - timedelta(seconds=1))
  if name == 'yesterday':
    if date_str:
      raise ValueError('shift=yesterday 不接受 date')
    day = now.date() - timedelta(days=1)
    start = datetime(day.year, day.month, day.day, _DAY_START[0], _DAY_START[1], tzinfo=_TZ)
    return start, start + timedelta(days=1), '白班+晚班'
  if name in ('day', 'night'):
    day = _parse_day(date_str) if date_str else _recent_shift_day(name, now)
    return _shift_bounds(day, name)
  raise ValueError(f'shift 只能取 current/last/yesterday/day/night（收到 {shift!r}）')


def _label_shift(label: str) -> str:
  """窗口文案里的班次名（白班/晚班/白班+晚班）；自然日区间返回空串。"""
  head = label.split(' ', 1)[0]
  return head if head in ('白班', '晚班', '白班+晚班') else ''


def _window_note(window_label: str, start_s: int) -> str:
  """窗口口径说明：**班次不是自然日**，凌晨 00:00–08:30 属前一晚班。

  模型会把「白班 08:30 起」说成「今天只有 N 条」，用户拿凌晨的告警来对就被当成丢数据
  （2026-11-10 实测：00:35 的事件属前一晚班，不在当天白班窗口）。这段由服务端确定性生成，
  模型照抄即可，不要自己推算时刻。
  """
  shift = _label_shift(window_label)
  start_dt = datetime.fromtimestamp(start_s, _TZ)
  if not shift:
    return f'本次为自然日窗口（{window_label}，含首尾），按事件发生时间归属。'
  prev_day = (start_dt - timedelta(days=1)).date()
  tail = (f'若问的是 {start_dt:%m-%d} 凌晨 00:00–08:30 的事件，它属于前一晚班'
          f'（shift=night, date={prev_day:%Y-%m-%d}），不在本窗口内。')
  if shift == '晚班':
    return (f'本次为班次口径 {window_label}（晚班 17:30 ~ 次日 08:30，跨零点，'
            f'次日的 00:00–08:30 属于本班）。' + tail)
  if shift == '白班':
    return f'本次为班次口径 {window_label}（白班 08:30–17:30）。' + tail
  return (f'本次为班次口径 {window_label}（该日 {start_dt:%H:%M} 起、非自然日 00:00）。'
          + tail)


def _parse_bound(value: str, label: str) -> tuple[datetime, bool]:
  """`YYYY-MM-DD`（含整天）或 `YYYY-MM-DD HH:MM`（精确到分）；返回 (时间, 是否带时分)。"""
  text = value.strip()
  for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d'):
    try:
      return datetime.strptime(text, fmt).replace(tzinfo=_TZ), '%H' in fmt
    except ValueError:
      continue
  raise ValueError(f'{label} 需为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM（收到 {value!r}）')


def _window(start: str, end: str, shift: str = '',
            date_str: str = '') -> tuple[int, int, str]:
  """统计窗口 → (start_s, end_s, 文案)。

  班次语义（`shift`/`date`）优先，规则见 `_resolve_shift`；都不传 = **当前班次**。
  `start`/`end` 只用于用户明确给的日期区间：裸日期 = 自然日（含首尾），带时分 = 精确到分。
  """
  if (shift or date_str) and (start or end):
    raise ValueError('shift/date 与 start/end 不能同时传：班次用 shift/date，明确日期区间用 '
                     'start/end')
  if not start:
    if end:
      raise ValueError('传 end 时需同时传 start')
    start_dt, end_dt, shift_name = _resolve_shift(shift, date_str)
    label = f'{shift_name} {start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M}'
  else:
    start_dt, start_has_time = _parse_bound(start, 'start')
    if end:
      end_dt, end_has_time = _parse_bound(end, 'end')
      if end_has_time and not start_has_time:
        raise ValueError('start 与 end 的精度要一致（都带时分或都不带）')
      if not end_has_time:
        end_dt += timedelta(days=1)      # 日期含首尾 → 次日 00:00
    elif start_has_time:
      raise ValueError('start 带时分时需同时传 end')
    else:
      end_dt = start_dt + timedelta(days=7)
    label = f'{start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M}'
  if end_dt <= start_dt:
    raise ValueError('end 必须晚于 start')
  return int(start_dt.timestamp()), int(end_dt.timestamp()), label


def _event_window(date_str: str, start: str, end: str) -> tuple[int, int, str]:
  """事件的**自然日**窗口（不套班次）：date=该日整天；start/end=日期区间；都不传=今天。

  查单个事件的进展时用户说的是「今天 / 11 月 9 日」这种自然日，不是值守班次。
  """
  if date_str and (start or end):
    raise ValueError('date 与 start/end 不能同时传：指定某天用 date，日期区间用 start/end')
  if date_str:
    start_dt = datetime.combine(_parse_day(date_str), datetime.min.time()).replace(tzinfo=_TZ)
    end_dt = start_dt + timedelta(days=1)
  elif start:
    start_dt, start_has_time = _parse_bound(start, 'start')
    if end:
      end_dt, end_has_time = _parse_bound(end, 'end')
      if end_has_time and not start_has_time:
        raise ValueError('start 与 end 的精度要一致（都带时分或都不带）')
      if not end_has_time:
        end_dt += timedelta(days=1)
    elif start_has_time:
      raise ValueError('start 带时分时需同时传 end')
    else:
      end_dt = start_dt + timedelta(days=7)
  else:
    start_dt = datetime.now(_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    end_dt = start_dt + timedelta(days=1)
  if end_dt <= start_dt:
    raise ValueError('end 必须晚于 start')
  label = f'{start_dt:%Y-%m-%d %H:%M} ~ {end_dt:%Y-%m-%d %H:%M}'
  return int(start_dt.timestamp()), int(end_dt.timestamp()), label


def _shift_at(ts: int) -> tuple[datetime, datetime, str]:
  """epoch 秒 → 所属班次的 [开始, 结束) 与班次名（班次规则只在 `_current_shift` 一处）。"""
  return _current_shift(datetime.fromtimestamp(ts, _TZ))


def _outside_hints(start_s: int, end_s: int, park_list: list[str],
                   chats: dict[str, str]) -> dict:
  """逐园区探测「通报在窗口内、事件在窗口外」的补录事件（仅 0 行时调用）。"""
  from service.scheduled_task.incident_stats import build

  out: dict = {'reports': 0, 'events': 0, 'items': [], 'truncated': False}
  for park in park_list:
    try:
      hint = build.outside_events(start_s, end_s, park, [chats[park]])
    except Exception as e:
      # 探测只用于解释空表，失败不改变「本窗口没有事件」这个结论
      logger.warning('[Incident] 窗口外事件探测失败 park=%s: %s', park, e)
      continue
    out['reports'] += hint['reports']
    out['events'] += hint['events']
    out['items'].extend(hint['items'])
    out['truncated'] = out['truncated'] or hint['truncated']
  out['items'].sort(key=lambda x: x['occurred'])
  if len(out['items']) > _HINT_MAX_ITEMS:
    out['items'] = out['items'][:_HINT_MAX_ITEMS]
    out['truncated'] = True
  return out


def _empty_note(window_label: str, hint: dict, skipped: list[str] | None = None) -> str:
  """0 行时的说明：必须讲清「收到了哪些通报、事件落在哪班、该查哪天」。

  2026-10-26 实测：昨夜 22:50 的告警今早 09:56 补发通报，查当班得 0 行，旧文案只说
  「该窗口没有已通报事件（或机器人尚未进群）」→ 用户以为消息丢了。
  `skipped` 是未解析到群、本次没纳入统计的园区（默认查全部时才会出现）。
  """
  base = f'本窗口（{window_label}）按事件发生时间统计，没有落在本窗口的事件。'
  if not hint['items']:
    text = base + '窗口内也没有收到通报（机器人未进群时，进群前的历史消息拉不到）。'
  else:
    parts = []
    for item in hint['items']:
      ts = datetime.fromtimestamp(item['occurred'], _TZ)
      shift_start, shift_end, shift = _shift_at(item['occurred'])
      parts.append(f'{ts:%Y-%m-%d %H:%M}（{shift} {shift_start:%m-%d %H:%M}~'
                   f'{shift_end:%m-%d %H:%M}）：{item.get("name") or ""}')
    latest = max(hint['items'], key=lambda i: i['occurred'])
    latest_ts = datetime.fromtimestamp(latest['occurred'], _TZ)
    latest_shift = 'day' if _shift_at(latest['occurred'])[2] == '白班' else 'night'
    more = (f'（窗口内共 {hint["events"]} 起这类补录事件，此处列出前 {len(hint["items"])} 起）'
            if hint['truncated'] else '')
    text = (base + f'窗口内已收到 {hint["reports"]} 条通报，但事件发生时间在：'
            + '；'.join(parts) + more
            + f'，属补录的更早事件；需要查看请传 shift={latest_shift} '
              f'date={latest_ts:%Y-%m-%d}。')
  if skipped:
    text += f'未配置群的园区（暂不纳入）：{"、".join(skipped)}。'
  return text


@tool
async def incident_report(
  start: str = '',
  end: str = '',
  shift: str = '',
  date: str = '',
  parks: str = '',
  sync: bool = True,
  export_excel: bool = False,
  config: RunnableConfig = None,
) -> str:
  """查询数据中心事件（飞书园区群通报 → 告警/工单关联）。

  一行 = 一次已通报事件（速报 + 通报/首报配对），关联 DCIM 告警与告警工单补全原因/进展/时长；
  匹配不到告警的通报**也出行**。统计由平台确定性计算，**不要**自己用 analysis_load 复算。
  **统计口径是班次，不要自己推算日期或换算 08:30/17:30**：用户说「今天/昨天/白班/晚班/上一班」
  时只传 `shift`（必要时配 `date`），不传时间时直接留空（平台按当前班次算）。
  用户只给日期/星期（如「本周五」「11 月 6 日」）时**只传 `date`、不要传 `shift`**，
  返回该日白班+晚班（08:30 ~ 次日 08:30）。

  **回答开头必须复述返回的 `window` 与 `window_note`**：班次不是自然日，凌晨 00:00–08:30
  的事件属**前一晚班**。**不得**把班次窗口说成「今天只有 N 条事件」；用户若拿窗口外的时刻
  （如 00:35）来对，按 `window_note` 说明它属于哪一班、该用哪个 `shift`/`date` 再查。

  返回里的 `mode` 决定怎么交付：
  - `inline`（≤20 条）：**原样输出 `table_markdown`**（服务端已按固定列渲染：日期/发生时间/
    速报时间/首报时间/地点/事件类型/事件名称/事件进展/过程有无违规及全部有效列，空值写「—」），
    再**原样附上 `file_tag`**（可搜索/排序的交互表格）；只在表格前后各加一两句结论，
    **不要自己重排、合并、删除列，也不要另造表**；
  - `records`（>20 条）：原样输出 `file_tag` 打开交互表格，再补一两句结论；
  - `export`（用户明确要 Excel）：原样输出 `file_tag`。
  说「事件统计表 / 统计一下 / 表格」都**不是**要文件，只有明确说 Excel/导出/下载才传
  `export_excel=true`。

  Args:
    shift: 班次，`current`（默认，当前班次）/ `last`（最近一个已结束的班次）/ `yesterday`
      （昨天白班+晚班）/ `day`（白班 08:30–17:30）/ `night`（晚班 17:30–次日 08:30）。
      用户说「今天/昨天/白班/晚班/上一班」时**只传 shift**，不要自己换算时刻。
    date: `YYYY-MM-DD`。**单独传（shift 留空）= 该日白班+晚班**（该日 08:30 ~ 次日 08:30）；
      配 `shift=day|night` 指定哪一天（缺省=最近一次已开始的该班次）。
    start: 起始日期 `YYYY-MM-DD`（自然日，含当天）或 `YYYY-MM-DD HH:MM`。**只在用户明确给出
      日期区间时传**（如「10 月 20 日到 22 日」），不能与 shift/date 同时传。
    end: 结束日期，格式精度与 start 一致。留空 = start 起 7 个自然日。
    parks: 园区名，逗号分隔（如「廊坊」或「廊坊,平湖」）。留空 = 所有已配置园区群。
    sync: 是否先从飞书群补拉该窗口的通报（默认 true，需要机器人已在群里）。
    export_excel: 是否产出 22 列 xlsx（默认 false）。仅用户明确要 Excel/文件/导出时置 true。
  """
  try:
    start_s, end_s, window_label = _window(start, end, shift, date)
  except ValueError as e:
    return _err(str(e))

  park_list, missing = _resolve_parks(parks)
  if missing:
    available = '、'.join(_configured_parks()) or '（未配置）'
    return _err(f'园区未配置园区群：{"、".join(missing)}；当前可用：{available}')
  if not park_list:
    return _err('未配置园区群（INCIDENT_FEISHU_PARK_CHATS 为空），无法确定统计哪个园区；'
                '请先让机器人进群并在部署配置里登记。')

  from service.scheduled_task.incident_stats import (
    build, feishu as feishu_mod, schema, sync as sync_mod, xlsx,
  )

  # 取数必须限定到园区群：同园区还有测试群/自检合成数据，只按 cluster 过滤会混进来
  try:
    chats = await feishu_mod.resolve_park_chats()
  except Exception as e:
    return _err(f'无法解析园区群：{type(e).__name__}: {str(e)[:200]}')
  unresolved = [p for p in park_list if not chats.get(p)]
  skipped: list[str] = []
  if unresolved:
    # 点名要某个园区却解析不到群 → 整单报错（不静默丢）；默认「全部园区」时只跳过未配置的，
    # 否则一个园区没进群就让整张表查不出来
    if _split_parks(parks):
      return _err(f'园区群未解析到 chat_id：{"、".join(unresolved)}（机器人是否已进群？）')
    skipped = unresolved
    park_list = [p for p in park_list if chats.get(p)]
    if not park_list:
      return _err(f'没有可用的园区群：{"、".join(skipped)}'
                  '（群名未匹配或机器人未进群），无法出表。')
    logger.warning('[Incident] 跳过未配置群的园区: %s', skipped)

  sync_error = ''
  if sync:
    try:
      # 班次窗口的终点可能还在未来：拉群只拉到「现在」，避免给接口传未来时间
      sync_end = max(start_s + 1, min(end_s, int(time.time())))
      await sync_mod.sync_range(start_s, sync_end, backfill=True)
    except Exception as e:
      # 拉群失败不整单失败：按库里已有的通报出表，把原因带回给模型说明
      sync_error = f'{type(e).__name__}: {str(e)[:200]}'
      logger.warning('[Incident] 对话出表同步失败（按库内数据继续）: %s', e)

  def _collect() -> list[dict]:
    schema.ensure_reports_schema(settings.DCIM_PG_DSN)
    rows: list[dict] = []
    for park in park_list:
      rows.extend(build.build_rows(start_s, end_s, park, [chats[park]]))
    rows.sort(key=lambda r: (r['发生时间'], r['事件名称']))
    for i, row in enumerate(rows, 1):
      row['序号'] = i
    return rows

  try:
    rows = await asyncio.to_thread(_collect)
  except Exception as e:
    logger.exception('[Incident] 对话出表取数失败')
    return _err(f'生成失败：{type(e).__name__}: {str(e)[:200]}')

  thread_id = _get_thread_id(config)
  visible = _visible_rows(rows)
  result: dict = {
    'window': window_label,
    'shift': _label_shift(window_label),
    'window_basis': '按事件发生时间（匹配到告警时为告警触发时刻）归属',
    # 班次口径 ≠ 自然日：凌晨 00:00–08:30 属前一晚班。模型必须连同 window 一起复述。
    'window_note': _window_note(window_label, start_s),
    'parks': park_list,
    'rows': len(rows),
  }

  if export_excel:
    summary = build.summary_counts(rows)
    start_dt = datetime.fromtimestamp(start_s, _TZ)
    end_dt = datetime.fromtimestamp(end_s, _TZ)
    filename = f'incident_{start_dt:%Y%m%d%H%M}_{end_dt:%Y%m%d%H%M}.xlsx'
    out_path = thread_dir(thread_id) / _WORKSPACE_ID / filename
    try:
      await asyncio.to_thread(xlsx.write_workbook, out_path, rows, summary, start_s)
    except Exception as e:
      logger.exception('[Incident] 对话出表写 xlsx 失败')
      return _err(f'生成失败（写文件）：{type(e).__name__}: {str(e)[:200]}')

    url = resolve_artifact_url(thread_id, filename)
    if not url:
      return _err('生成失败：产物落盘后在会话目录里找不到（文件名或目录异常）')
    result.update(mode='export', url=url, file_name=filename,
                  file_tag=f'<file url="{url}" name="{filename}"/>')
  elif not rows:
    # 0 行：不出文件，由下面的 note 说明为什么空
    result.update(mode='inline', table_markdown='')
  else:
    # 无论条数都落数据集给交互表格：列与内容由服务端控制（RecordsTable 直接读 csv），
    # 不经过模型 —— 让它自己拼表时实测会擅自删列（速报/首报整列消失，2026-11-06）。
    from core.sandbox.datasets import save_dataset

    info = await asyncio.to_thread(
      save_dataset, thread_id, 'incident_stats',
      {'start': start, 'end': end, 'shift': shift, 'date': date,
       'parks': park_list}, visible, 'incident-stats',
    )
    records_url = (f'/api/v1/chat/agent/artifacts/{quote(thread_id, safe="")}/'
                   f'{quote(info["dataset_id"], safe="")}/records')
    file_tag = f'<file url="{records_url}" name="事件明细"/>'
    if len(rows) > _INLINE_MAX_ROWS:
      result.update(mode='records', records_url=records_url, file_tag=file_tag)
    else:
      # ≤20 条：文本表也由服务端渲染，模型只复制（不自选列）
      result.update(mode='inline',
                    table_markdown=_markdown_table(list(visible[0]), visible),
                    records_url=records_url, file_tag=file_tag)

  if sync:
    result['synced'] = True
  if sync_error:
    result['sync_error'] = sync_error
  if skipped:
    result['skipped_parks'] = skipped
  if not rows:
    hint = await asyncio.to_thread(_outside_hints, start_s, end_s, park_list, chats)
    result['note'] = _empty_note(window_label, hint, skipped)
    if hint['items']:
      result['outside_window'] = hint['items']
  return json.dumps(result, ensure_ascii=False)


@tool
async def incident_progress(
  query: str = '',
  date: str = '',
  start: str = '',
  end: str = '',
  parks: str = '',
  config: RunnableConfig = None,
) -> str:
  """查询**单个事件**的完整进展记录（卡片里追加的「进展同步1..N」时间线 + 事件结束结论）。

  用户说「查 XX 事件的进展全记录 / 完整进展 / 处置时间线」时用它，**不要**用
  `incident_report` 出整张统计表代替。事件按 `query`（事件名称/地点/设备/测点关键词，
  空格或逗号分隔 = 全部命中）定位；命中多起时返回候选表，**必须让用户确认是哪一起**，
  不要自己挑一个。窗口是**自然日**（今天 / 某天 / 日期区间），不是值守班次。

  返回里的 `mode` 决定怎么交付：
  - `inline`：唯一命中，原样输出 `table_markdown`（服务端已按「序号/通报时间/进展描述」
    渲染好）与 `file_tag`；`final` 是「事件结束」段的处理结果/原因/填写人，作为结论；
  - `records`：唯一命中但进展超过 20 条，原样输出 `file_tag` 打开交互表格；
  - `candidates`：0 个或多个候选，原样输出候选 `table_markdown` 并问用户选哪一个；
  - `empty`：该窗口内没有事件，按 `note` 说明换个日期再查。

  Args:
    query: 事件关键词，如「A2 UPS」「湿度过低」。留空 = 窗口内全部事件（用于让用户选）。
    date: `YYYY-MM-DD`，查该自然日（00:00–24:00）。不传 = 今天。
    start: 起始日期 `YYYY-MM-DD`（含当天）或 `YYYY-MM-DD HH:MM`，与 end 配对；与 date 互斥。
    end: 结束日期，精度与 start 一致；留空 = start 起 7 天。
    parks: 园区名，逗号分隔（如「廊坊」）；留空 = 所有已配置园区群。
  """
  try:
    start_s, end_s, window_label = _event_window(date, start, end)
  except ValueError as e:
    return _err(str(e))

  park_list, missing = _resolve_parks(parks)
  if missing:
    available = '、'.join(_configured_parks()) or '（未配置）'
    return _err(f'园区未配置园区群：{"、".join(missing)}；当前可用：{available}')
  if not park_list:
    return _err('未配置园区群（INCIDENT_FEISHU_PARK_CHATS 为空），无法确定查哪个园区；'
                '请先让机器人进群并在部署配置里登记。')

  from service.scheduled_task.incident_stats import detail, feishu as feishu_mod

  try:
    chats = await feishu_mod.resolve_park_chats()
  except Exception as e:
    return _err(f'无法解析园区群：{type(e).__name__}: {str(e)[:200]}')
  unresolved = [p for p in park_list if not chats.get(p)]
  skipped: list[str] = []
  if unresolved:
    if _split_parks(parks):
      return _err(f'园区群未解析到 chat_id：{"、".join(unresolved)}（机器人是否已进群？）')
    skipped = unresolved
    park_list = [p for p in park_list if chats.get(p)]
    if not park_list:
      return _err(f'没有可用的园区群：{"、".join(skipped)}'
                  '（群名未匹配或机器人未进群），无法查询。')
    logger.warning('[Incident] 跳过未配置群的园区: %s', skipped)

  # 进展是「每条新消息」由 WS 实时入库（1~2s），库里就是完整正文，查询不调飞书
  def _collect() -> list[dict]:
    events: list[dict] = []
    for park in park_list:
      for event in detail.build_events(start_s, end_s, park, [chats[park]]):
        event['park'] = park
        events.append(event)
    events.sort(key=lambda e: e['occurred_ts'])
    return events

  try:
    events = await asyncio.to_thread(_collect)
  except Exception as e:
    logger.exception('[Incident] 进展查询取数失败')
    return _err(f'查询失败：{type(e).__name__}: {str(e)[:200]}')

  result: dict = {
    'window': window_label,
    'parks': park_list,
    'events': len(events),
    'query': query,
  }
  if skipped:
    result['skipped_parks'] = skipped

  if not events:
    result.update(mode='empty', note=(
      f'{window_label} 内没有事件（按事件发生时间统计）。'
      '可换一个日期（date）或区间（start/end）再查。'))
    return json.dumps(result, ensure_ascii=False)

  matches = detail.search(events, query)
  if len(matches) != 1:
    # 0 个命中时把窗口内全部事件列出来，让用户从里面指；多个命中直接列候选，不猜
    pool = matches or events
    columns = ['时间', '事件名称', '地点', '园区']
    candidates = [
      {'时间': e['occurred'], '事件名称': e['title'] or '（报文未命名）',
       '地点': e['place'], '园区': e['park']}
      for e in pool
    ]
    if matches:
      note = (f'匹配到 {len(matches)} 起事件，请用户确认是哪一起'
              '（可补充具体时刻或更精确的事件名称）。')
    else:
      note = (f'没有匹配到「{query}」的事件；{window_label} 内共有 '
              f'{len(events)} 起，请用户从候选表里指定。')
    result.update(mode='candidates', candidates=candidates,
                  table_markdown=_markdown_table(columns, candidates), note=note)
    return json.dumps(result, ensure_ascii=False)

  event = matches[0]
  blocks = event.get('blocks') or []
  final = event.get('final') or {}
  progress = [{'序号': i, '通报时间': b.get('time') or '', '进展描述': b.get('desc') or ''}
              for i, b in enumerate(blocks, 1)]
  result.update(mode='inline' if len(progress) <= _INLINE_MAX_ROWS else 'records',
                title=event['title'] or '（报文未命名）', place=event['place'],
                date=event['date'], occurred=event['occurred'], park=event['park'],
                progress=progress, final=final)
  if not final:
    result['final_note'] = '该事件还没有「事件结束」段（尚未结束或未填写处理结果）。'

  if not progress:
    result['note'] = '这起事件没有追加的进展块（可能只发了首报、还没同步进展）。'
    return json.dumps(result, ensure_ascii=False)

  thread_id = _get_thread_id(config)
  columns = ['序号', '通报时间', '进展描述']
  from core.sandbox.datasets import save_dataset

  info = await asyncio.to_thread(
    save_dataset, thread_id, 'incident_progress',
    {'date': date, 'start': start, 'end': end, 'query': query, 'parks': park_list},
    progress, 'incident-progress',
  )
  records_url = (f'/api/v1/chat/agent/artifacts/{quote(thread_id, safe="")}/'
                 f'{quote(info["dataset_id"], safe="")}/records')
  file_tag = f'<file url="{records_url}" name="事件进展"/>'
  result.update(records_url=records_url, file_tag=file_tag)
  if result['mode'] == 'inline':
    result['table_markdown'] = _markdown_table(columns, progress)
  return json.dumps(result, ensure_ascii=False)

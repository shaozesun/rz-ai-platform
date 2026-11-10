"""飞书园区群：拉通报消息（含富文本卡片正文）+ 归类速报/首报。

与 `service/feishu/history.py` 的分工：那边给 Agent 返回「[时间] 发送者: 文本」的
拼串，且只认 text/post；本模块要**结构化**字段（message_id / create_time / 原始报文）
并支持 `interactive` 卡片（首报很可能是卡片）。

client 自建、不依赖 `LARK_ENABLED`/`start()`：`get_api_client()` 在未启动时是 None，
而事件同步是独立后台任务，不该被机器人回复开关牵连。
"""

import asyncio
import json
import logging
import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import settings

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_MAX_MESSAGES = 5000          # 单次拉取安全阀（回填一个月也够）
_PAGE_SIZE = 50               # 飞书上限

_TITLE_RE = re.compile(r'【([^】]{0,40})】')

# 字段标签必须**锚定**取，不能全文搜时间：一条通报里可能同时有【恢复时间】和
# 【发生时间】，全文搜会抓到恢复时间且不报错。
_TIME_LABELS = ('发生时间', '事件发生时间', '告警时间', '发生日期')
_ROOM_LABELS = ('地点', '位置', '机房')
_OVERVIEW_LABELS = ('事件概述', '事件名称', '告警内容')
_VALUE_LABELS = ('告警值', '告警数值', '当前值')

# 华为 eWatch 告警推送（速报的另一种格式，如 `【华为】<eWatch><告警产生>`），字段是
# 「对象/机房/时间/监控项/告警值/描述/级别/来源」。
_EWATCH_RE = re.compile(r'<\s*eWatch\s*>|<告警(?:产生|恢复)>', re.IGNORECASE)
# 只有「告警产生」算速报，`<告警恢复>` 不算（用户口径，别自己加）。
_EWATCH_TRIGGER_RE = re.compile(r'<告警产生>', re.IGNORECASE)
# eWatch 抓「时间：」的值。**不能行首锚定** —— 现场报文有两种形态：多行（`时间：` 独占
# 一行）与单行（`对象:… 机房:… 时间：… 监控项：…` 全挤在一行，2026-10-29 实测），
# 行首锚定对后者永远匹配不到。也不能把「时间」塞进 `_TIME_LABELS` —— 那是无锚点搜索，
# 会命中同一条里的「恢复时间：」。捕获必须**截到下一个字段标签**：单行报文后面紧跟
# `监控项/告警值/描述`，而描述里常带「预计 2026年10月30日 完成」这类日期，
# 整段吞进来会被 `_DT_YMD` 先命中那个日期。
_EWATCH_NEXT_LABEL = r'(?:对象|机房|时间|监控项|告警值|描述|级别|来源)'
_EWATCH_TIME_RE = re.compile(
  r'(?<!恢复)(?<!发生)(?<!告警)(?<!产生)时间\s*[:：]\s*'
  r'(.+?)(?=\s*' + _EWATCH_NEXT_LABEL + r'\s*[:：]|\n|$)'
)

# 时段词决定是否补 12 小时（`下午 1:45` → 13:45）。只能写非捕获组，
# 否则会打乱下方各表达式的位置分组。
_PERIOD = r'(?:上午|下午|晚上|凌晨|早上|傍晚)?'
_DT_YMD = re.compile(
  r'(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日'
  r'\s*[,，]?\s*' + _PERIOD + r'\s*'
  r'(?:(\d{1,2})\s*[时:]\s*(\d{1,2})(?:\s*[分:]\s*(\d{1,2}))?)?'
)
_DT_DASH = re.compile(
  r'(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})'
  r'(?:[T\s]+\s*' + _PERIOD + r'\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?'
)
# 无年份写法：年份用消息 create_time 推断
_DT_MD = re.compile(
  r'(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日'
  r'\s*[,，]?\s*' + _PERIOD + r'\s*'
  r'(?:(\d{1,2})\s*[时:]\s*(\d{1,2})(?:\s*[分:]\s*(\d{1,2}))?)?'
)
# 前后必须是非字母数字：卡片事件概述里 `A1M1-3F-B配电室` 会从中间抽出垃圾码 `M1-3`，
# 一旦当机房信号参与匹配就会误命中 `M1-301` 之类的空间名。
_ROOM_RE = re.compile(r'(?<![A-Za-z0-9])[A-Za-z]{1,3}\d{1,3}-\d{1,4}(?![A-Za-z0-9])')


def _client():
  if not settings.LARK_APP_ID or not settings.LARK_APP_SECRET:
    raise RuntimeError('LARK_APP_ID / LARK_APP_SECRET 未配置，无法拉取飞书通报')
  import lark_oapi as lark

  return (
    lark.Client.builder()
    .app_id(settings.LARK_APP_ID)
    .app_secret(settings.LARK_APP_SECRET)
    .build()
  )


def _norm(text: str) -> str:
  """NFKC（全角→半角）+ 括号归一：`【发生时间】` → `[发生时间]`。

  现场报文常把冒号写进括号（`【事件概述：】`）：不先归一的话 `_labeled` 的
  括号分支匹配不上，会退化到无括号分支、返回 `] : 值` 这种带脏前缀的结果。
  """
  normalized = unicodedata.normalize('NFKC', text or '')
  normalized = normalized.replace('【', '[').replace('】', ']')
  return re.sub(r'\[\s*([^\[\]\n]{1,20}?)\s*[:：]\s*\]', r'[\1]', normalized)


def _labeled(text: str, aliases: tuple[str, ...], limit: int = 200) -> str:
  """按标签取值：`【发生时间】:xxx` 或 `发生时间：xxx`。

  取「标签 → 下一个 `[` 或行尾」，最多 `limit` 字。标签自身可带空格
  （`【 发生时间 】`），冒号可有可无。多个标签同时出现时取**最先出现**的那个。
  """
  normalized = _norm(text)
  ends: list[int] = []
  for alias in aliases:
    for pattern in (rf'\[\s*{alias}\s*\]\s*:?\s*', rf'{alias}\s*:\s*'):
      ends.extend(m.end() for m in re.finditer(pattern, normalized))
  if not ends:
    return ''
  rest = normalized[min(ends):].lstrip(' \t\r\n')
  value = rest[:limit]
  stops = [i for i in (value.find('['), value.find('\n')) if i >= 0]
  if stops:
    value = value[:min(stops)]
  return value.strip()


def _ref_dt(ref_ts: int | None) -> datetime:
  if ref_ts is None:
    return datetime.now(tz=_TZ)
  return datetime.fromtimestamp(int(ref_ts), tz=_TZ)


def _walk_texts(node, out: list[str]) -> None:
  """递归抽卡片 JSON 里的可读文本（text/content/title/fields）。"""
  if isinstance(node, dict):
    for key in ('text', 'content', 'title'):
      value = node.get(key)
      if isinstance(value, str) and value.strip():
        out.append(value.strip())
    for value in node.values():
      if isinstance(value, (dict, list)):
        _walk_texts(value, out)
  elif isinstance(node, list):
    for item in node:
      _walk_texts(item, out)


def extract_text(message) -> str:
  """消息体 → 纯文本：text/post 复用 history 的实现，interactive 走卡片递归抽取。"""
  body = getattr(message, 'body', None)
  content_str = getattr(body, 'content', '') if body else ''
  if not content_str:
    return ''
  msg_type = getattr(message, 'msg_type', '') or ''
  if msg_type != 'interactive':
    from service.feishu.history import _extract_text

    text = _extract_text(message)
    if text:
      return text
  try:
    content = json.loads(content_str)
  except Exception:
    return content_str
  parts: list[str] = []
  _walk_texts(content, parts)
  # 卡片里的同一段文本会被 text/content 各收一次 → 保序去重
  return '\n'.join(dict.fromkeys(parts))


def text_from_content(msg_type: str, content_str: str) -> str:
  """WS 事件里的 `message.content`（JSON 字符串）→ 纯文本。

  与 `extract_text` 的差别只在输入形态：拉取接口给 `message.body.content`，
  推送事件直接给 `message.content`。判定逻辑必须与拉取侧一致，否则推/拉两路
  入库的 `kind`/`title` 会不一样。
  """
  if not content_str:
    return ''
  try:
    content = json.loads(content_str)
  except Exception:
    return content_str
  if msg_type == 'interactive':
    parts: list[str] = []
    _walk_texts(content, parts)
    # 卡片里的同一段文本会被 text/content 各收一次 → 保序去重
    return '\n'.join(dict.fromkeys(parts))
  from service.feishu.channel import _extract_text

  return _extract_text(content)


def build_report_row(*, chat_id: str, message_id: str, create_s: int, sender: str = '',
                     sender_id: str = '', sender_id_type: str = '', msg_type: str = '',
                     text: str, raw_body: str = '') -> dict:
  """「一条消息 → 一行 dcim_incident_reports」，拉取与实时推送**共用**。

  共用是为了两路产出的行完全同形：字段名/派生规则（kind/event_time/title/
  report_value/room_code）只要有一处分叉，同一窗口按两种方式进库就会分裂。
  """
  kind = classify(text)
  event_time = extract_event_time(text, create_s)
  if kind != 'other' and event_time is None:
    logger.warning('[Incident] 通报未解析到发生时间 chat=%s msg=%s', chat_id, message_id)
  return {
    'chat_id': chat_id,
    'message_id': message_id,
    'create_time': create_s,
    'sender': sender or '',
    'sender_id': sender_id or '',
    'sender_id_type': sender_id_type or '',
    'kind': kind,
    'title': _title(text),
    'text': text,
    'event_time': event_time,
    'report_value': extract_report_value(text),
    'room_code': extract_room_code(text),
    'raw': {'msg_type': msg_type, 'body': raw_body},
  }


def is_ewatch(text: str) -> bool:
  """是否华为 eWatch 告警推送（只用于**解析**：行首「时间：」、不抽机房码）。

  不用于分类 —— 只有 `<告警产生>` 算速报，`<告警恢复>` 不算（见 `classify`）。
  """
  return bool(_EWATCH_RE.search(text or ''))


def classify(text: str) -> str:
  """正文 → sos(速报) | first(首报/通报) | other。

  纯关键字字面匹配：含「速报」→ 速报；含「通报/首报」→ 事件表里的首报；华为 eWatch
  **告警产生**（`<告警产生>`）是速报的另一种格式，同样算速报（`<告警恢复>` 不算）。
  速报优先于通报 —— 速报正文常写「已通报二线」，先判通报会把速报算成首报。
  """
  text = text or ''
  if '速报' in text:
    return 'sos'
  if '通报' in text or '首报' in text:
    return 'first'
  if _EWATCH_TRIGGER_RE.search(text):
    return 'sos'
  return 'other'


def parse_event_time(text: str, ref_ts: int | None = None) -> int | None:
  """从【发生时间】标签取值并解析成秒级 epoch；解析不到返回 None。

  - 标签锚定，不全文搜（同一条通报里可能先出现【恢复时间】）
  - eWatch 推送没有【发生时间】标签，退回「时间：」字段（单行/多行都认，见 `_EWATCH_TIME_RE`）
  - 兼容 `年月日 时:分:秒` / 横杠 / 斜杠 / ISO `T` 几种写法（`GMT+08:00` 后缀忽略，
    它本来就是北京时间）
  - `下午/晚上` 且 hour < 12 → +12 小时
  - 无年份 → 用消息时间（`ref_ts`）的年份；比消息晚 180 天以上 → 年份 −1
  - **只有日期没有时分 → None**（不用 00:00 硬凑，避免匹配偏到凌晨）
  """
  value = _labeled(text, _TIME_LABELS)
  if not value and is_ewatch(text):
    hit = _EWATCH_TIME_RE.search(_norm(text or ''))
    value = hit.group(1).strip() if hit else ''
  return value_to_epoch(value, ref_ts)


def value_to_epoch(value: str, ref_ts: int | None = None) -> int | None:
  """「日期串 → 秒级 epoch」。确定性解析与模型兜底**共用**这一关。

  模型给的时间串也必须过这里：解析不出来就丢弃，绝不写进库里一个凭空的时刻
  （用户口径「识别不出来的不要乱填写」）。
  """
  if not value:
    return None

  inferred = False
  match = _DT_YMD.search(value)
  if match:
    year, month, day, hour, minute, second = match.groups()
  else:
    match = _DT_DASH.search(value)
    if match:
      year, month, day, hour, minute, second = match.groups()
    else:
      match = _DT_MD.search(value)
      if not match:
        return None
      month, day, hour, minute, second = match.groups()
      year, inferred = None, True

  if hour is None or minute is None:
    return None
  hour, minute, second = int(hour), int(minute), int(second or 0)
  if re.search(r'下午|晚上|傍晚', value) and hour < 12:
    hour += 12
  if year is None:
    year = _ref_dt(ref_ts).year
  try:
    dt = datetime(int(year), int(month), int(day), hour, minute, second, tzinfo=_TZ)
    if inferred and (dt - _ref_dt(ref_ts)).days > 180:
      dt = dt.replace(year=dt.year - 1)
  except ValueError:
    logger.warning('[Incident] 发生时间非法：%s', value[:80])
    return None
  return int(dt.timestamp())


def extract_event_time(text: str, ref_ts: int | None = None) -> int | None:
  """`parse_event_time` 的旧名（调用方与自检沿用）。"""
  return parse_event_time(text, ref_ts)


def extract_report_value(text: str) -> float | None:
  """从【告警值】取数值（忽略单位与「（已编辑）」等尾巴）。"""
  value = _labeled(text, _VALUE_LABELS)
  if not value:
    return None
  match = re.search(r'[-+]?\d+(?:\.\d+)?', value)
  if not match:
    return None
  try:
    return float(match.group())
  except ValueError:
    return None


def extract_room_code(text: str) -> str:
  """抽机房编码（如 `M1-204`）：优先「地点」，其次「事件概述」，最后全文。

  eWatch 推送**不参与机房匹配**（用户口径：只按时间 + 告警值配）：它的
  `A6-M2-4F-401` / `rz6241-4b-01-03` 会被 `_ROOM_RE` 抽成垃圾码（如 `A6-6`）。
  """
  if is_ewatch(text):
    return ''
  for aliases in (_ROOM_LABELS, _OVERVIEW_LABELS):
    match = _ROOM_RE.search(_labeled(text, aliases))
    if match:
      return match.group().upper()
  match = _ROOM_RE.search(_norm(text))
  return match.group().upper() if match else ''


def _title(text: str) -> str:
  match = _TITLE_RE.search(text or '')
  return match.group(1).strip() if match else (text or '')[:40]


def _to_seconds(ms) -> int | None:
  try:
    value = int(ms)
  except (TypeError, ValueError):
    return None
  return value // 1000 if value >= 1_000_000_000_000 else value


async def list_chats() -> list[dict]:
  """机器人加入的群聊（只取 group 类型），用于按群名解析 chat_id。"""
  from lark_oapi.api.im.v1 import ListChatRequest

  client = _client()
  request = ListChatRequest.builder().page_size(100).build()
  response = await asyncio.to_thread(client.im.v1.chat.list, request)
  if response is None or not response.success():
    logger.warning('[Incident] 列群失败 code=%s msg=%s',
                   getattr(response, 'code', '?'), getattr(response, 'msg', ''))
    return []
  items = getattr(getattr(response, 'data', None), 'items', None) or []
  return [
    {'chat_id': getattr(c, 'chat_id', '') or '', 'name': getattr(c, 'name', '') or ''}
    for c in items
    if getattr(c, 'chat_mode', None) == 'group'
  ]


def _park_config() -> dict[str, str]:
  """INCIDENT_FEISHU_PARK_CHATS → {园区: chat_id 或群名}，只解析配置、不查飞书。"""
  raw = (settings.INCIDENT_FEISHU_PARK_CHATS or '').strip()
  if not raw:
    return {}
  try:
    conf = json.loads(raw)
  except json.JSONDecodeError:
    logger.warning('[Incident] INCIDENT_FEISHU_PARK_CHATS 不是合法 JSON，跳过飞书同步')
    return {}
  return {str(k): str(v) for k, v in conf.items()} if isinstance(conf, dict) else {}


def configured_parks() -> list[str]:
  """配置里登记的园区键，用于在结果里列出「群未解析到」的园区。"""
  return list(_park_config())


async def resolve_park_chats() -> dict[str, str]:
  """配置 → {园区: chat_id}。值可以是 `oc_xxx`，也可以是群名（精确优先、再子串）。"""
  conf = _park_config()
  if not conf:
    return {}

  need_name = {p: v for p, v in conf.items() if not v.startswith('oc_')}
  chats = await list_chats() if need_name else []
  out: dict[str, str] = {}
  for park, value in conf.items():
    value = str(value).strip()
    if value.startswith('oc_'):
      out[park] = value
      continue
    exact = [c for c in chats if c['name'] == value]
    fuzzy = [c for c in chats if value and value in c['name']]
    hit = (exact or fuzzy)
    if hit:
      out[park] = hit[0]['chat_id']
    else:
      logger.warning('[Incident] 未找到园区群 park=%s 群名=%s（机器人是否已进群？）', park, value)
  return out


_RECALLED_PLACEHOLDER = 'This message was recalled'


def is_recalled(message) -> bool:
  """消息是否已被撤回。

  撤回后 `message.list` 仍会返回这条消息，但 `deleted=true`、`body.content` 固定是占位符。
  **必须先于 `extract_text` 判断**：占位符非空，否则会被当成一条正常消息 upsert，
  把库里原文覆盖成占位符（且每轮回补再覆盖一次）。
  """
  if getattr(message, 'deleted', False):
    return True
  body = getattr(message, 'body', None)
  content = (getattr(body, 'content', '') or '').strip() if body else ''
  return content == _RECALLED_PLACEHOLDER


def page_rows(chat_id: str, items) -> tuple[list[dict], list[str]]:
  """一页消息 → (可入库行, 已撤回的 message_id)；纯函数，便于离线断言。"""
  rows: list[dict] = []
  recalled: list[str] = []
  for m in items:
    message_id = getattr(m, 'message_id', '') or ''
    if is_recalled(m):
      if message_id:
        recalled.append(message_id)
      continue
    create_s = _to_seconds(getattr(m, 'create_time', None))
    if create_s is None:
      continue
    text = extract_text(m)
    if not text:
      continue
    sender = getattr(m, 'sender', None)
    rows.append(build_report_row(
      chat_id=chat_id,
      message_id=message_id,
      create_s=create_s,
      sender=getattr(sender, 'sender_name', '') or '',
      sender_id=getattr(sender, 'id', '') or '',
      sender_id_type=getattr(sender, 'id_type', '') or '',
      msg_type=getattr(m, 'msg_type', ''),
      text=text,
      raw_body=_raw_body(m),
    ))
  return rows, recalled


async def fetch_messages(chat_id: str, start_s: int, end_s: int) -> tuple[list[dict], list[str]]:
  """拉 [start_s, end_s) 的消息（Desc 翻页）。

  返回 `(rows, recalled_ids)`：rows 按时间升序；recalled_ids 是**已撤回**的 message_id，
  交给调用方删行（撤回当作没发过，绝不能当新消息入库）。
  """
  from lark_oapi.api.im.v1 import ListMessageRequest

  client = _client()
  rows: list[dict] = []
  recalled: list[str] = []
  page_token = ''
  while len(rows) < _MAX_MESSAGES:
    builder = (
      ListMessageRequest.builder()
      .container_id_type('chat')
      .container_id(chat_id)
      .sort_type('ByCreateTimeDesc')
      .page_size(_PAGE_SIZE)
      .start_time(str(int(start_s)))
      .end_time(str(int(end_s)))
      .with_sender_name(True)
    )
    if page_token:
      builder.page_token(page_token)
    response = await asyncio.to_thread(client.im.v1.message.list, builder.build())
    if response is None or not response.success():
      logger.warning('[Incident] 拉群消息失败 chat=%s code=%s msg=%s', chat_id,
                     getattr(response, 'code', '?'), getattr(response, 'msg', ''))
      break
    data = getattr(response, 'data', None)
    items = getattr(data, 'items', None) or []
    page, page_recalled = page_rows(chat_id, items)
    rows.extend(page)
    recalled.extend(page_recalled)
    has_more = getattr(data, 'has_more', False)
    next_token = getattr(data, 'page_token', '') or ''
    if not has_more or not next_token:
      break
    page_token = next_token
  rows.sort(key=lambda r: r['create_time'])
  return rows, recalled


def _raw_body(message) -> str:
  body = getattr(message, 'body', None)
  return getattr(body, 'content', '') if body else ''


async def get_message(message_id: str) -> tuple[str, str] | None:
  """按 message_id 取**当前**正文 → `(text, raw_body)`；失败/空返回 None。

  卡片通报会在同一条消息里原地追加「进展同步N /【进展描述】」，而飞书没有消息
  更新事件、增量游标也不会回看旧消息 —— 只有定点 `message.get` 能拿到追加后的正文。
  `card_msg_content_type='user_card_content'` 取面向用户的卡片内容（接口约保留 72h）。
  """
  from lark_oapi.api.im.v1 import GetMessageRequest

  client = _client()
  request = (
    GetMessageRequest.builder()
    .message_id(message_id)
    .card_msg_content_type('user_card_content')
    .build()
  )
  try:
    response = await asyncio.to_thread(client.im.v1.message.get, request)
  except Exception as e:
    logger.warning('[Incident] 取消息失败 msg=%s: %s', message_id, str(e)[:200])
    return None
  if response is None or not response.success():
    logger.warning('[Incident] 取消息失败 msg=%s code=%s msg=%s', message_id,
                   getattr(response, 'code', '?'), getattr(response, 'msg', ''))
    return None
  items = getattr(getattr(response, 'data', None), 'items', None) or []
  if not items:
    return None
  item = items[0]
  text = extract_text(item)
  if not text:
    return None
  return text, _raw_body(item)

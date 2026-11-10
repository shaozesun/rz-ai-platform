"""飞书群消息历史拉取（im.v1.message.list）。

需飞书应用授予「获取群消息」权限（im:message:group_msg 只读），否则接口 403。
返回按时间升序的「[时间] 发送者: 文本」列表，供 Agent 总结。
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Optional

from service.feishu.client import get_api_client

logger = logging.getLogger(__name__)

_DATETIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d')


def _to_epoch_seconds(value: str, *, end_of_day: bool = False) -> str:
  """把模型给的时间参数归一成飞书要的**秒级**时间戳字符串。

  为什么必须在这里换算（2026-09-28 实测）：
  - 传日期串 `'2026-09-10'` → 飞书直接 `code=230001 invalid request parameter`；
  - 传**毫秒**时间戳 → **不报错但静默查空**（被当成远处未来的秒），比报错更难发现。

  所以对外只收人类可读的日期/时间（模型不该去算 epoch，算错还是静默查空），
  换算统一在这里做。空串/解析不了 → 返回空串（不过滤），宁可不过滤也不要整个查询失败。

  end_of_day: 只给到「天」的 end_time 按当天 23:59:59 处理——否则窗口退化成
  `[00:00, 00:00)` 直接查空（工具承诺的语义是"查某天/某段**完整**消息"）。
  """
  raw = (value or '').strip()
  if not raw:
    return ''
  if raw.isdigit():
    n = int(raw)
    return str(n // 1000 if n >= 1_000_000_000_000 else n)  # ≥1e12 视为毫秒
  is_date_only = False
  for fmt in _DATETIME_FORMATS:
    try:
      dt = datetime.strptime(raw, fmt)
    except ValueError:
      continue
    is_date_only = fmt == '%Y-%m-%d'
    break
  else:
    try:  # ISO 8601（含 T / 时区偏移）
      dt = datetime.fromisoformat(raw)
    except ValueError:
      logger.warning('[Feishu] 无法解析时间参数 %r，忽略该过滤', value)
      return ''
  if end_of_day and is_date_only:
    dt = dt.replace(hour=23, minute=59, second=59)
  return str(int(dt.timestamp()))


async def fetch_recent_messages(
  chat_id: str,
  limit: int = 50,
  start_time: str = '',
  end_time: str = '',
) -> list[str]:
  """拉取群/单聊消息，自动翻页 + 可选时间范围过滤。

  飞书单页 page_size 上限 50，这里循环 has_more + page_token 翻页，直到拉满
  limit 条（硬上限 500）或没有更多。

  start_time/end_time **对外收人类可读的日期**（如 '2026-09-08'、'2026-09-08 14:00'、
  ISO 8601），内部由 `_to_epoch_seconds` 换算成飞书要的**秒级时间戳**——接口本身不认
  日期串（230001），也不认毫秒（静默查空）。空串不过滤。

  **取的是窗口内"最近"的 limit 条**（接口用 ByCreateTimeDesc 拉、返回前 reverse 成升序）：
  以前用 ByCreateTimeAsc，拿到的是该群**最早**的消息——群一活跃，"总结群消息"总结的就是
  史前内容（2026-09-28 实测）。返回仍按时间升序，便于直接拼给模型。
  """
  client = get_api_client()
  if not client:
    return []
  from lark_oapi.api.im.v1 import ListMessageRequest

  start_ts = _to_epoch_seconds(start_time)
  end_ts = _to_epoch_seconds(end_time, end_of_day=True)

  records: list[tuple] = []
  open_ids: set[str] = set()
  page_token: str = ''
  remaining = max(1, min(limit, 500))
  while remaining > 0:
    page_size = min(50, remaining)
    builder = (
      ListMessageRequest.builder()
      .container_id_type('chat')
      .container_id(chat_id)
      .sort_type('ByCreateTimeDesc')
      .page_size(page_size)
      .with_sender_name(True)
    )
    if start_ts:
      builder.start_time(start_ts)
    if end_ts:
      builder.end_time(end_ts)
    if page_token:
      builder.page_token(page_token)

    response = await asyncio.to_thread(client.im.v1.message.list, builder.build())
    if response is None or not response.success():
      logger.warning(
        '[Feishu] 拉群消息失败 code=%s msg=%s', getattr(response, 'code', '?'), getattr(response, 'msg', ''),
      )
      break

    data = getattr(response, 'data', None)
    items = getattr(data, 'items', None) or []
    for m in items:
      text = _extract_text(m)
      if not text:
        continue
      sender = getattr(m, 'sender', None)
      oid = getattr(sender, 'id', '') or ''
      if oid:
        open_ids.add(oid)
      records.append((m, text))

    remaining -= page_size
    has_more = getattr(data, 'has_more', False)
    next_token = getattr(data, 'page_token', '') or ''
    if not has_more or not next_token:
      break
    page_token = next_token

  from service.feishu import contact
  name_map = await contact.resolve_names(open_ids)
  # Desc 拉回来的顺序是"新→旧"，返回契约是升序（直接拼给模型看）→ 反转
  return [
    f'[{_fmt_time(getattr(m, "create_time", ""))}] {_sender_label(m, name_map)}: {text}'
    for m, text in reversed(records)
  ]


async def _list_group_chats() -> list[dict]:
  """列机器人加入的所有群聊，返回 [{chat_id, name, description}]；失败返回空列表。

  chat.list 已验证稳定（chat.search 按名搜不可靠，已废弃）。MVP page_size=100 不翻页。
  """
  client = get_api_client()
  if not client:
    return []
  from lark_oapi.api.im.v1 import ListChatRequest

  request = ListChatRequest.builder().page_size(100).build()
  response = await asyncio.to_thread(client.im.v1.chat.list, request)
  if response is None or not response.success():
    logger.warning(
      '[Feishu] 列群失败 code=%s msg=%s', getattr(response, 'code', '?'), getattr(response, 'msg', ''),
    )
    return []

  result: list[dict] = []
  for c in (getattr(getattr(response, 'data', None), 'items', None) or []):
    if getattr(c, 'chat_mode', None) == 'group':
      result.append({
        'chat_id': getattr(c, 'chat_id', None),
        'name': getattr(c, 'name', None) or '',
        'description': getattr(c, 'description', None) or '',
      })
  return result


async def search_chats_by_name(name: str) -> list[dict]:
  """按群名称查找机器人所在群（本地匹配），返回候选 [{chat_id, name, description}]。

  先 chat.list 列全量群，再本地做名称匹配：精确相等优先，否则子串。
  多候选（同名/相似群）由调用方决定：唯一直接读，多个让用户选。
  """
  key = (name or '').strip()
  chats = await _list_group_chats()
  if not chats:
    return []

  logger.info('[Feishu] 按名搜群 query=%r 全量 %d 个群', key, len(chats))
  exact = [c for c in chats if c['name'] == key]
  if exact:
    return exact
  return [c for c in chats if key and (key in c['name'] or c['name'] in key)]


async def find_p2p_chat_id(open_id: str) -> Optional[str]:
  """找「指定用户 open_id ↔ 机器人」的单聊会话 chat_id（找不到返回 None）。"""
  client = get_api_client()
  if not client:
    return None
  from lark_oapi.api.im.v1 import ListChatRequest

  request = (
    ListChatRequest.builder()
    .user_id_type('open_id')
    .types('p2p')
    .page_size(100)
    .build()
  )
  response = await asyncio.to_thread(client.im.v1.chat.list, request)
  if response is None or not response.success():
    logger.warning(
      '[Feishu] 列单聊会话失败 code=%s msg=%s', getattr(response, 'code', '?'), getattr(response, 'msg', ''),
    )
    return None

  items = getattr(getattr(response, 'data', None), 'items', None) or []
  for c in items:
    if getattr(c, 'p2p_target_id', None) == open_id:
      return getattr(c, 'chat_id', None)
  return None


async def list_joined_chats() -> list[dict]:
  """列出机器人加入的所有群聊，返回 [{chat_id, name}]；失败返回空列表。"""
  chats = await _list_group_chats()
  return [{'chat_id': c['chat_id'], 'name': c['name']} for c in chats]


async def list_group_members(chat_id: str) -> list[dict]:
  """列出群成员，返回 [{open_id, name, display_name}]；失败返回空列表。"""
  client = get_api_client()
  if not client:
    return []
  from lark_oapi.api.im.v1 import GetChatMembersRequest

  members: list[dict] = []
  page_token = ''
  while True:
    builder = (
      GetChatMembersRequest.builder()
      .chat_id(chat_id)
      .member_id_type('open_id')
      .page_size(100)
    )
    if page_token:
      builder.page_token(page_token)
    response = await asyncio.to_thread(client.im.v1.chat_members.get, builder.build())
    if response is None or not response.success():
      logger.warning(
        '[Feishu] 列群成员失败 code=%s msg=%s',
        getattr(response, 'code', '?'), getattr(response, 'msg', ''),
      )
      break
    data = getattr(response, 'data', None)
    items = getattr(data, 'items', None) or []
    for m in items:
      members.append({
        'open_id': getattr(m, 'member_id', '') or '',
        'display_name': getattr(m, 'name', '') or '',
      })
    has_more = getattr(data, 'has_more', False)
    page_token = getattr(data, 'page_token', '') or ''
    if not has_more or not page_token:
      break

  from service.feishu import contact
  name_map = await contact.resolve_names([m['open_id'] for m in members if m['open_id']])
  for m in members:
    m['name'] = name_map.get(m['open_id'], '') or m['display_name']
  return members


def _extract_text(message) -> str:
  body = getattr(message, 'body', None)
  content_str = getattr(body, 'content', '') if body else ''
  if not content_str:
    return ''
  try:
    content = json.loads(content_str)
  except Exception:
    return content_str
  if isinstance(content, dict):
    if 'text' in content:
      return content['text']
    if isinstance(content.get('content'), list):
      parts: list[str] = []
      for para in content['content']:
        if isinstance(para, list):
          for el in para:
            if isinstance(el, dict) and el.get('tag') in ('text', 'at'):
              t = el.get('text', '')
              if t:
                parts.append(t)
      return ' '.join(parts)
  return ''


def _sender_label(message, name_map: dict[str, str] | None = None) -> str:
  sender = getattr(message, 'sender', None)
  open_id = getattr(sender, 'id', '') or ''
  real_name = (name_map or {}).get(open_id, '') if open_id else ''
  sender_name = getattr(sender, 'sender_name', '') or ''
  return real_name or sender_name or open_id


def _fmt_time(ms: str) -> str:
  try:
    return datetime.fromtimestamp(int(ms) / 1000).strftime('%m-%d %H:%M')
  except (ValueError, TypeError):
    return ''

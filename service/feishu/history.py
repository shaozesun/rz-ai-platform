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


async def fetch_recent_messages(
  chat_id: str,
  limit: int = 50,
  start_time: str = '',
  end_time: str = '',
) -> list[str]:
  """拉取群/单聊消息，自动翻页 + 可选时间范围过滤。

  飞书单页 page_size 上限 50，这里循环 has_more + page_token 翻页，直到拉满
  limit 条（硬上限 500）或没有更多。start_time/end_time 传日期/ISO 字符串
  （如 '2026-09-08'）时透传给飞书接口过滤，空串不过滤。返回按时间升序。
  """
  client = get_api_client()
  if not client:
    return []
  from lark_oapi.api.im.v1 import ListMessageRequest

  lines: list[str] = []
  page_token: str = ''
  remaining = max(1, min(limit, 500))
  while remaining > 0:
    page_size = min(50, remaining)
    builder = (
      ListMessageRequest.builder()
      .container_id_type('chat')
      .container_id(chat_id)
      .sort_type('ByCreateTimeAsc')
      .page_size(page_size)
    )
    if start_time:
      builder.start_time(start_time)
    if end_time:
      builder.end_time(end_time)
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
      sender = _sender_label(m)
      lines.append(f'[{_fmt_time(getattr(m, "create_time", ""))}] {sender}: {text}')

    remaining -= page_size
    has_more = getattr(data, 'has_more', False)
    next_token = getattr(data, 'page_token', '') or ''
    if not has_more or not next_token:
      break
    page_token = next_token
  return lines


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


def _sender_label(message) -> str:
  sender = getattr(message, 'sender', None)
  name = getattr(sender, 'sender_name', '') or ''
  return name or getattr(sender, 'id', '') or ''


def _fmt_time(ms: str) -> str:
  try:
    return datetime.fromtimestamp(int(ms) / 1000).strftime('%m-%d %H:%M')
  except (ValueError, TypeError):
    return ''

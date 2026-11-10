"""个人微信入站报文解析：把 iLink update 映射成统一入站结构并路由。

只处理文本消息（`message_type == 1`）：发送者 = `from_user_id`（或 `ilink_user_id`，
形如 `xxx@im.wechat`），正文取 `item_list[].type == 1` 的 `text_item.text`，
`context_token` 取报文顶层的同名字段。`/connect <code>` 走绑定，其余走 Agent。
非文本消息（图片/文件/语音）本阶段不处理。
"""

import logging

from config.settings import settings
from service.channels.connect_code import normalize_code

logger = logging.getLogger(__name__)

_CONNECT_PREFIXES = ('/connect ', '/connect', 'connect ')


def _extract_text(update: dict) -> str:
  """取文本消息正文（拼接多个 text item）；非文本返回空串。"""
  parts = []
  for item in update.get('item_list') or []:
    if not isinstance(item, dict) or item.get('type') != 1:
      continue
    text = (item.get('text_item') or {}).get('text')
    if isinstance(text, str) and text.strip():
      parts.append(text.strip())
  return '\n'.join(parts)


def _is_connect_command(text: str) -> bool:
  t = (text or '').strip()
  return any(t.startswith(p) for p in _CONNECT_PREFIXES)


def _is_allowed(sender: str) -> bool:
  """可选白名单：空 = 不限制；否则裸 user_id 或完整 `xxx@im.wechat` 命中即可。"""
  allowed = [u.strip() for u in (settings.WECHAT_ALLOWED_USERS or '').split(',') if u.strip()]
  if not allowed:
    return True
  return sender in allowed or sender.split('@', 1)[0] in allowed


async def handle_update(update: dict) -> None:
  """处理单条 iLink update（由 client 的长轮询循环逐条调用）。"""
  if not isinstance(update, dict) or update.get('message_type') != 1:
    return

  sender = str(update.get('from_user_id') or update.get('ilink_user_id') or '').strip()
  if not sender:
    return

  text = _extract_text(update)
  context_token = str(update.get('context_token') or '').strip()

  from service.wechat import runner

  # 绑定码先于白名单判定：浏览器发起的一次性码要把尚未在白名单里的新身份 bootstrap 进来。
  if _is_connect_command(text):
    await runner.handle_bind(sender, normalize_code(text), context_token)
    return

  if not _is_allowed(sender):
    logger.info('[WeChat] 非白名单用户，忽略 sender=%s', sender)
    return

  if not text:
    return
  await runner.handle_message(text, sender, context_token)

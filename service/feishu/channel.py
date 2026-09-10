"""飞书事件分发：消息解析 + 卡片动作，统一提交到主 loop 处理。

事件处理器运行在 WS 线程里，这里只做轻量解析，然后通过 submit_async 把协程
提交回主 loop（uvicorn loop），DB 查询 / Agent 执行都在主 loop 完成。
"""

import json
import logging
import re

from config.settings import settings
from service.feishu.client import submit_async

logger = logging.getLogger(__name__)

_AT_MENTION_RE = re.compile(r'^(?:@\S+\s*)+')


def build_event_handler(lark):
  return (
    lark.EventDispatcherHandler.builder(
      settings.LARK_ENCRYPT_KEY or '',
      settings.LARK_VERIFICATION_TOKEN or '',
    )
    .register_p2_im_message_receive_v1(_on_message)
    .build()
  )


def _on_message(event) -> None:
  try:
    message = event.event.message
    chat_id = message.chat_id
    msg_id = message.message_id
    chat_type = getattr(message, 'chat_type', None)
    sender_id = event.event.sender.sender_id.open_id
    content = json.loads(message.content)
    text = _extract_text(content).strip()
    if not text:
      return
    if chat_type != 'p2p' and not _is_bot_mentioned(message):
      return

    from service.feishu import runner

    if _is_connect_command(text):
      submit_async(runner.handle_bind(open_id=sender_id, code=text, message_id=msg_id, chat_id=chat_id))
      return

    if _is_summary_command(text):
      submit_async(runner.handle_summary(
        open_id=sender_id, chat_id=chat_id, chat_type=chat_type, message_id=msg_id,
      ))
      return

    submit_async(runner.handle_message(
      text=_strip_mentions(text), open_id=sender_id, chat_id=chat_id,
      chat_type=chat_type, message_id=msg_id,
    ))
  except Exception:
    logger.exception('[Feishu] 处理消息事件失败')


def _is_bot_mentioned(message) -> bool:
  for m in (message.mentions or []):
    if getattr(m, 'mentioned_type', None) == 'bot':
      return True
  return False


def _is_connect_command(text: str) -> bool:
  t = _strip_mentions(text)
  return t.startswith('/connect') or t.startswith('connect ')


def _is_summary_command(text: str) -> bool:
  t = _strip_mentions(text)
  return t.startswith('/summary') or t.startswith('总结')


def _strip_mentions(text: str) -> str:
  t = _AT_MENTION_RE.sub('', text.strip()).strip()
  if settings.LARK_BOT_NAME and t.startswith(settings.LARK_BOT_NAME):
    t = t[len(settings.LARK_BOT_NAME):].strip()
  return t


def _extract_text(content) -> str:
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
      return '\n'.join(parts)
  return ''

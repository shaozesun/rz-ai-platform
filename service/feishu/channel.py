"""飞书事件分发：消息解析 + 卡片动作，统一提交到主 loop 处理。

事件处理器运行在 WS 线程里，这里只做轻量解析，然后通过 submit_async 把协程
提交回主 loop（uvicorn loop），DB 查询 / Agent 执行都在主 loop 完成。
"""

import json
import logging
import re

from config.settings import settings
from service.feishu.client import note_event, submit_async

logger = logging.getLogger(__name__)

_AT_MENTION_RE = re.compile(r'^(?:@\S+\s*)+')


def build_event_handler(lark):
  return (
    lark.EventDispatcherHandler.builder(
      settings.LARK_ENCRYPT_KEY or '',
      settings.LARK_VERIFICATION_TOKEN or '',
    )
    .register_p2_im_message_receive_v1(_on_message)
    .register_p2_im_message_recalled_v1(_on_recall)
    .build()
  )


def _on_message(event) -> None:
  try:
    note_event()          # /health 的「最后事件时间」：只证明 WS 还活着
    message = event.event.message
    chat_id = message.chat_id
    msg_id = message.message_id

    # 群消息实时入库旁路（与对话链**完全独立**）：命中白名单才把事件交给事件统计的
    # 实时通道，之后照旧走「@ 机器人才响应」的原逻辑 —— 两条路并存、互不影响。
    # 独立 try：入库侧任何异常（配置/DB/通讯录）都**不能**把下面的对话链路带崩。
    try:
      from service.scheduled_task.incident_stats import realtime

      if realtime.should_ingest(chat_id):
        realtime.on_message_event(event)
    except Exception:
      logger.exception('[Feishu] 实时入库旁路失败（不影响对话）')

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

    # 「总结当前群」的快捷路由**只在群里生效**：私聊里没有"当前群"可总结，
    # 而用户很可能是在说「总结一下 XX 群」——那是 agent 该干的活（工具描述里的
    # 用法二：chat_name 填群名）。以前这里用 "以「总结」开头" 一律截走，私聊里
    # 就被 handle_summary 直接回「仅支持群聊使用」，把工具支持的正常路径堵死了。
    # 显式的 /summary 仍走 handle_summary（私聊里由它给出正确说法）。
    if _is_summary_command(text) and (chat_type != 'p2p' or _is_explicit_summary_command(text)):
      submit_async(runner.handle_summary(
        open_id=sender_id, chat_id=chat_id, chat_type=chat_type, message_id=msg_id,
        text=_strip_mentions(text),
      ))
      return

    submit_async(runner.handle_message(
      text=_strip_mentions(text), open_id=sender_id, chat_id=chat_id,
      chat_type=chat_type, message_id=msg_id,
    ))
  except Exception:
    logger.exception('[Feishu] 处理消息事件失败')


def _on_recall(event) -> None:
  """消息撤回：交给事件统计把库里那一行删掉（当没发过）。

  与对话链完全无关；独立 try —— 入库侧任何异常都不能往上冒。
  """
  try:
    note_event()
  except Exception:
    logger.exception('[Feishu] 记录撤回事件失败')

  try:
    from service.scheduled_task.incident_stats import realtime

    body = getattr(event, 'event', None)
    chat_id = getattr(body, 'chat_id', '') or ''
    if realtime.should_ingest(chat_id):
      realtime.on_recall_event(chat_id, getattr(body, 'message_id', '') or '')
  except Exception:
    logger.exception('[Feishu] 撤回事件处理失败（不影响对话）')


def _is_bot_mentioned(message) -> bool:
  for m in (message.mentions or []):
    if getattr(m, 'mentioned_type', None) == 'bot':
      return True
  return False


def _is_connect_command(text: str) -> bool:
  t = _strip_mentions(text)
  return t.startswith('/connect') or t.startswith('connect ')


def _is_summary_command(text: str) -> bool:
  """「总结当前群」的触发词（自然语言或显式命令）。"""
  t = _strip_mentions(text)
  return t.startswith('/summary') or t.startswith('总结')


def _is_explicit_summary_command(text: str) -> bool:
  """显式 `/summary` 命令——私聊里也认它（由 handle_summary 给出正确说法）。"""
  return _strip_mentions(text).startswith('/summary')


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

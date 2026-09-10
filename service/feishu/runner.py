"""飞书消息 → Agent 路由：身份解析、权限加载、流式卡片回复、绑定、群总结。

复用 service/scheduled_task/runner.py 已验证的范式：把 AgentService().stream() 的
SSE 事件流喂给非 Web 场景，text 事件累计成最终回复。身份解析按 open_id → User，
未绑定/非 ACTIVE 用户一律回绑定卡片，不触发 Agent。
"""

import json
import logging
import time

from config.settings import settings
from core.agent.feishu_context import set_current_feishu
from core.rbac import load_role_permissions
from models.user import UserStatus
from service.auth.user_service import user_service
from service.feishu import artifacts, binding, reply

logger = logging.getLogger(__name__)


def _parse_sse(chunk: str) -> dict | None:
  line = chunk.strip()
  if not line.startswith('data:'):
    return None
  try:
    return json.loads(line[5:].strip())
  except Exception:
    return None


async def handle_message(text: str, open_id: str, chat_id: str, chat_type: str, message_id: str) -> None:
  """普通消息：绑定用户 → Agent 问答；未绑定 → 绑定卡片。"""
  user = await binding.resolve_user_by_open_id(open_id)
  if not user or user.get('status') != UserStatus.ACTIVE.value:
    await reply.reply_bind_card(message_id)
    return

  thread_id = (
    f'rz-agent-feishu-p2p-{open_id}' if chat_type == 'p2p'
    else f'rz-agent-feishu-group-{chat_id}'
  )
  set_current_feishu(chat_id, open_id, chat_type)
  await _run_agent(user, text, message_id, thread_id)


async def handle_bind(open_id: str, code: str, message_id: str, chat_id: str = None) -> None:
  """一次性码绑定（/connect 命令或卡片输入框提交）。"""
  user_id = binding.consume_connect_code(code)
  if not user_id:
    await _reply_or_create(message_id, chat_id, '绑定码无效或已过期，请重新生成。')
    return
  target = await user_service.get_by_id(user_id)
  if not target:
    await _reply_or_create(message_id, chat_id, '绑定失败：系统用户不存在。')
    return
  await binding.bind_open_id(user_id, open_id)
  name = target.get('name') or target.get('phone', '')
  await _reply_or_create(message_id, chat_id, f'绑定成功！已关联系统用户：{name}')


async def handle_summary(open_id: str, chat_id: str, chat_type: str, message_id: str) -> None:
  """群消息总结：拉最近 N 条群消息，交给 Agent 独立线程总结。"""
  user = await binding.resolve_user_by_open_id(open_id)
  if not user or user.get('status') != UserStatus.ACTIVE.value:
    await reply.reply_bind_card(message_id)
    return
  if chat_type == 'p2p':
    await reply.reply_card(message_id, '群消息总结仅支持群聊使用。')
    return

  from service.feishu import history
  lines = await history.fetch_recent_messages(chat_id, settings.LARK_HISTORY_LIMIT)
  if not lines:
    await reply.reply_card(message_id, '未拉到群消息。')
    return

  prompt = '请总结以下飞书群最近消息（按时间顺序，提炼要点、待办与结论）：\n\n' + '\n'.join(lines)
  thread_id = f'rz-agent-feishu-group-{chat_id}-summary'
  await _run_agent(user, prompt, message_id, thread_id)


async def _run_agent(user: dict, text: str, message_id: str, thread_id: str) -> None:
  """执行一轮 Agent 并流式更新卡片。"""
  perms = await load_role_permissions(user.get('roles', []))
  perms |= set(user.get('permissions', []))
  session_id = thread_id.removeprefix('rz-agent-')

  from service.agent.agent_service import AgentService

  card_id = await reply.reply_card(message_id, '正在思考…')
  buf = ''
  error = ''
  last_flush = time.time()
  try:
    svc = AgentService()
    async for chunk in svc.stream(
      message=text,
      thread_id=thread_id,
      user_id=user['user_id'],
      session_id=session_id,
      permissions=perms,
    ):
      payload = _parse_sse(chunk)
      if payload is None:
        continue
      event = payload.get('event')
      data = payload.get('data') or {}
      if event == 'text':
        buf += data.get('content', '')
        now = time.time()
        if card_id and (now - last_flush >= 1.5 or len(buf) >= 2000):
          await reply.update_card(card_id, buf + '\n\n（生成中…）')
          last_flush = now
      elif event == 'error':
        error = data.get('message', '')
  except Exception as e:
    logger.exception('[Feishu] Agent 执行失败 open_id=%s', user.get('lark_open_id'))
    error = str(e)

  if error:
    final = buf + f'\n\n（生成中断：{error}）' if buf else f'回复生成中断：{error}'
  else:
    final = buf or '（无回复内容）'
  final = artifacts.rewrite_for_feishu(final, thread_id)
  if card_id:
    await reply.update_card(card_id, final)
  else:
    await reply.reply_card(message_id, final)


async def _reply_or_create(message_id: str, chat_id: str, text: str) -> None:
  if message_id:
    await reply.reply_card(message_id, text)
  elif chat_id:
    await reply.create_card(chat_id, text)

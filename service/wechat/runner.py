"""微信消息 → Agent 路由：身份解析、权限加载、终稿回复、绑定。

镜像 `service/feishu/runner.py`，差异在协议能力：iLink **不能编辑已发消息**、也无打字态，
故不做流式卡片刷新，累计到终稿后一次性发送（见 reply.py）。
复用 `service/channels/agent_stream.py` 消费 `AgentService().stream()` 的 SSE 事件流。
"""

import logging

from core.rbac import load_role_permissions
from models.user import UserStatus
from service.auth.user_service import user_service
from service.channels import agent_stream
from service.wechat import artifacts, binding, reply

logger = logging.getLogger(__name__)

_BIND_HINT = (
  '你还没有绑定系统账号。\n'
  '请到平台的【认证中心】生成验证码，然后把验证码直接发给我即可绑定。'
)


async def handle_message(text: str, wechat_user_id: str, context_token: str) -> None:
  """普通消息：绑定用户 → Agent 问答；未绑定 → 回绑定提示。"""
  user = await binding.resolve_user_by_wechat_id(wechat_user_id)
  if not user or user.get('status') != UserStatus.ACTIVE.value:
    user_id = binding.consume_connect_code(text)
    if user_id:
      await _do_bind(user_id, wechat_user_id, context_token)
      return
    await reply.send_text(wechat_user_id, _BIND_HINT, context_token)
    return

  thread_id = f'rz-agent-wechat-{wechat_user_id}'
  await _run_agent(user, text, wechat_user_id, context_token, thread_id)


async def handle_bind(wechat_user_id: str, code: str, context_token: str) -> None:
  """一次性码绑定（/connect 命令）。"""
  user_id = binding.consume_connect_code(code)
  if not user_id:
    await reply.send_text(wechat_user_id, '绑定码无效或已过期，请重新生成。', context_token)
    return
  await _do_bind(user_id, wechat_user_id, context_token)


async def _do_bind(user_id: str, wechat_user_id: str, context_token: str) -> None:
  target = await user_service.get_by_id(user_id)
  if not target:
    await reply.send_text(wechat_user_id, '绑定失败：系统用户不存在。', context_token)
    return
  await binding.bind_wechat_id(user_id, wechat_user_id)
  name = target.get('name') or target.get('phone', '')
  await reply.send_text(wechat_user_id, f'绑定成功！已关联系统用户：{name}', context_token)


async def _run_agent(
  user: dict, text: str, wechat_user_id: str, context_token: str, thread_id: str,
) -> None:
  """执行一轮 Agent，累计文本后一次性发送终稿（iLink 无流式）。"""
  perms = await load_role_permissions(user.get('roles', []))
  perms |= set(user.get('permissions', []))
  session_id = thread_id.removeprefix('rz-agent-')

  from service.agent.agent_service import AgentService

  buf = ''
  error = ''
  try:
    svc = AgentService()
    async for event, data in agent_stream.iter_agent_events(
      svc,
      message=text,
      thread_id=thread_id,
      user_id=user['user_id'],
      session_id=session_id,
      permissions=perms,
    ):
      if event == 'text':
        buf += data.get('content', '')
      elif event == 'error':
        error = data.get('message', '')
  except Exception as e:
    logger.exception('[WeChat] Agent 执行失败 wechat=%s', wechat_user_id)
    error = str(e)

  if error:
    final = buf + f'\n\n（生成中断：{error}）' if buf else f'回复生成中断：{error}'
  else:
    final = buf or '（无回复内容）'
  final = artifacts.rewrite_for_wechat(final, thread_id)
  await reply.send_text(wechat_user_id, final, context_token)

"""飞书消息 → Agent 路由：身份解析、权限加载、流式卡片回复、绑定、群总结。

复用 service/scheduled_task/runner.py 已验证的范式：把 AgentService().stream() 的
SSE 事件流喂给非 Web 场景，text 事件累计成最终回复。身份解析按 open_id → User，
未绑定/非 ACTIVE 用户一律回绑定卡片，不触发 Agent。
"""

import logging
import re
import time
from datetime import datetime, timedelta

from config.settings import settings
from core.agent.feishu_context import set_current_feishu
from core.rbac import load_role_permissions
from models.user import UserStatus
from service.auth.user_service import user_service
from service.channels import agent_stream
from service.feishu import artifacts, binding, reply

logger = logging.getLogger(__name__)


async def handle_message(text: str, open_id: str, chat_id: str, chat_type: str, message_id: str) -> None:
  """普通消息：绑定用户 → Agent 问答；未绑定 → 发验证码绑定或回绑定卡片。"""
  user = await binding.resolve_user_by_open_id(open_id)
  if not user or user.get('status') != UserStatus.ACTIVE.value:
    user_id = binding.consume_connect_code(text)
    if user_id:
      await _do_bind(user_id, open_id, message_id, chat_id)
      return
    await reply.reply_bind_card(message_id)
    return

  thread_id = (
    f'rz-agent-feishu-p2p-{open_id}' if chat_type == 'p2p'
    else f'rz-agent-feishu-group-{chat_id}'
  )
  set_current_feishu(chat_id, open_id, chat_type)
  await _run_agent(user, text, message_id, thread_id)


async def handle_bind(open_id: str, code: str, message_id: str, chat_id: str = None) -> None:
  """一次性码绑定（/connect 命令或直接发验证码）。"""
  user_id = binding.consume_connect_code(code)
  if not user_id:
    await _reply_or_create(message_id, chat_id, '绑定码无效或已过期，请重新生成。')
    return
  await _do_bind(user_id, open_id, message_id, chat_id)


async def _do_bind(user_id: str, open_id: str, message_id: str, chat_id: str = None) -> None:
  """已拿到 user_id 后执行绑定并回复结果。"""
  target = await user_service.get_by_id(user_id)
  if not target:
    await _reply_or_create(message_id, chat_id, '绑定失败：系统用户不存在。')
    return
  await binding.bind_open_id(user_id, open_id)
  name = target.get('name') or target.get('phone', '')
  await _reply_or_create(message_id, chat_id, f'绑定成功！已关联系统用户：{name}')


_WEEKDAY_CN = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


def _resolve_summary_window(text: str) -> tuple[str, str, str]:
  """从用户那句话里解析总结的**时间窗口**，返回 (start, end, 人话标签)。

  默认**本周**（周一 00:00 ~ 现在）——"总结群消息"不指定时间时，用户要的是最近的讨论，
  而不是"最近 N 条"（条数硬截断会让活跃群只覆盖几小时、冷清群覆盖几个月，量纲不一致）。

  支持的相对时间（都没命中 → 本周）：今天 / 昨天 / 本周 / 上周 / 本月 / 最近N天 / N天内。
  返回的是 `history._to_epoch_seconds` 认的日期串。
  """
  now = datetime.now()
  fmt = '%Y-%m-%d %H:%M:%S'
  t = (text or '').strip()

  def day(d, end: bool = False) -> str:
    return d.strftime('%Y-%m-%d') + (' 23:59:59' if end else ' 00:00:00')

  if '上周' in t:
    this_monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    last_monday = this_monday - timedelta(days=7)
    return (last_monday.strftime(fmt), day(this_monday - timedelta(days=1), end=True),
            f'上周（{last_monday:%m-%d} ~ {(this_monday - timedelta(days=1)):%m-%d}）')
  if '本周' in t or '这周' in t:
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    return (monday.strftime(fmt), now.strftime(fmt), f'本周（{monday:%m-%d} 起）')
  if '昨天' in t:
    y = now - timedelta(days=1)
    return (day(y), day(y, end=True), f'昨天（{y:%m-%d}）')
  if '今天' in t or '今日' in t:
    return (day(now), now.strftime(fmt), f'今天（{now:%m-%d}）')
  if '本月' in t or '这个月' in t:
    first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return (first.strftime(fmt), now.strftime(fmt), f'本月（{first:%m-%d} 起）')
  m = re.search(r'最近\s*(\d+)\s*天|(\d+)\s*天内', t)
  if m:
    n = int(m.group(1) or m.group(2))
    n = max(1, min(n, 90))
    since = (now - timedelta(days=n - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (since.strftime(fmt), now.strftime(fmt), f'最近 {n} 天（{since:%m-%d} 起）')

  monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
  return (monday.strftime(fmt), now.strftime(fmt), f'本周（{monday:%m-%d} 起）')


async def handle_summary(
  open_id: str, chat_id: str, chat_type: str, message_id: str, text: str = '',
) -> None:
  """群消息总结：按**时间窗口**拉消息（默认本周）交给 Agent 总结，并在回复里说明范围。"""
  user = await binding.resolve_user_by_open_id(open_id)
  if not user or user.get('status') != UserStatus.ACTIVE.value:
    await reply.reply_bind_card(message_id)
    return
  if chat_type == 'p2p':
    # 私聊里没有"当前群"可总结——但用户想总结的是**某个具体的群**，那条路是
    # 直接说群名（agent 会用 fetch_group_messages(chat_name=…) 搜群再总结）。
    await reply.reply_card(
      message_id,
      '「/summary」是总结**当前群**的，只能在群里用。\n'
      '如果你想总结某个群，直接跟我说群名就行，比如：**总结一下「群组测试2」**',
    )
    return

  from service.feishu import history
  start, end, label = _resolve_summary_window(text)
  # 按窗口取全量（不按条数硬截断），只用 LARK_SUMMARY_MAX_MESSAGES 兜极端情况
  cap = settings.LARK_SUMMARY_MAX_MESSAGES
  lines = await history.fetch_recent_messages(chat_id, cap, start, end)
  if not lines:
    await reply.reply_card(message_id, f'{label}没有群消息。')
    return

  truncated = len(lines) >= cap
  prompt = (
    f'请总结以下飞书群消息。\n'
    f'时间范围：{label}（共 {len(lines)} 条，按时间顺序）。\n'
    '**回复的第一行必须注明这次统计的时间范围**，然后再给要点、待办与责任人、结论。'
  )
  if truncated:
    prompt += (
      f'\n注意：该范围内消息超过 {cap} 条，**本次只分析了最近的 {cap} 条**，'
      '请在回复里如实说明，并提示用户可以缩小时间范围（如「总结昨天的」）。'
    )
  prompt += '\n\n' + '\n'.join(lines)

  thread_id = f'rz-agent-feishu-group-{chat_id}-summary'
  # 与 handle_message 对齐：设好飞书上下文，这一轮里 agent 若调 fetch_group_messages
  # （不带 chat_name）才能读到**当前群**，而不是退化成私聊路径
  set_current_feishu(chat_id, open_id, chat_type)
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

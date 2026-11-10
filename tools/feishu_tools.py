"""飞书消息工具 — 读取群消息 / 私聊记录，供 Agent 自然语言查询。

支持两类场景：
1. 飞书群聊内：@机器人 要求「总结群消息」→ 读当前群（feishu_context 提供 chat_id）。
2. 平台 Web / 飞书私聊：绑定飞书的用户说「总结 XX 群」「看下我和机器人的私聊」→
   按群名称搜群 / 按 open_id 定位单聊会话，再拉消息。

权限：非飞书群聊场景（Web / 私聊）要求用户已绑定飞书（User.lark_open_id 非空）。
飞书 API 为同步阻塞 HTTP，用 asyncio.to_thread 包装（见 service/feishu/history.py）。
"""

import json
import logging

from langchain.tools import tool

from core.agent.feishu_context import get_current_feishu
from core.agent.untrusted_content import sanitizing
from core.agent.user_context import get_current_user_id
from service.auth.user_service import user_service

logger = logging.getLogger(__name__)


async def _resolve_bound_open_id() -> str | None:
  """解析当前用户绑定的飞书 open_id；未登录 / 未绑定返回 None。"""
  user_id = get_current_user_id()
  if not user_id:
    return None
  user = await user_service.get_by_id(user_id)
  if not user:
    return None
  return user.get('lark_open_id') or None


@tool
@sanitizing
async def fetch_group_messages(
  chat_name: str = '',
  chat_id: str = '',
  limit: int = 50,
  start_time: str = '',
  end_time: str = '',
) -> str:
  """读取飞书群聊消息（自动翻页 + 可选时间范围）。

  用法一：在飞书群聊里 @机器人 要求「总结群消息」时，chat_name/chat_id 留空，读当前群。
  用法二：在平台 Web 或飞书私聊里说「总结 XX 群」时，chat_name 填群名称，按名称搜群后读消息。
  用法三：按名搜到多个候选群、用户选定后，用该群的 chat_id 直接读（不再按名搜）。
  结果按时间升序，格式「[时间] 发送者: 文本」。非群聊场景需要用户已绑定飞书。

  时间范围：用户要「查今天的」「查昨天的」「查某段时间」时，先用 get_current_time 拿当前
  时间算出具体**日期**，再传 start_time / end_time，并把 limit 设大（如 500）以拉全，
  工具会自动翻页。**只传日期字符串，不要自己算时间戳**（内部会换算）。

  Args:
    chat_name: 群名称（仅 Web / 私聊场景需要；群聊内读当前群时留空）。
    chat_id: 群 chat_id（搜到多个候选、用户选定后精确读取用，优先于 chat_name）。
    limit: 拉取条数，默认 50，最多 500（超过单页 50 时自动翻页）。
    start_time: 起始时间，传**日期**即可（`2026-09-08` 或 `2026-09-08 14:00`），空串不过滤。
    end_time: 结束时间（格式同 start_time），空串表示到最新。**只给到"天"时自动含当天 23:59:59**，
      所以查"某一天"就把 start/end 都填那天日期即可。

  多候选交互约定：按 chat_name 搜到多个群时，本工具返回候选列表（need_select=true，
  含每个群的 chat_id 与名称）。此时不要把 chat_id 展示给用户，只把群名编号列表转述给
  用户，等用户选定后再用对应 chat_id 调用本工具精确读取。
  """
  ctx = get_current_feishu()
  if chat_id:
    target_chat_id = chat_id
  elif ctx and ctx.get('chat_type') == 'group' and not chat_name.strip():
    target_chat_id = ctx['chat_id']
  else:
    open_id = await _resolve_bound_open_id()
    if not open_id:
      return json.dumps(
        {'error': '请先在「认证中心」绑定飞书账号', 'suggestion': '绑定后才能查询飞书群消息'},
        ensure_ascii=False,
      )
    from service.feishu import history
    candidates = await history.search_chats_by_name(chat_name.strip())
    if not candidates:
      return json.dumps(
        {'error': f'未找到群「{chat_name}」',
         'suggestion': '机器人可能未加入该群，或群名不匹配，请确认群名'},
        ensure_ascii=False,
      )
    if len(candidates) > 1:
      numbered = [
        f'{i + 1}. {c["name"]}' + (f'（{c["description"]}）' if c.get('description') else '')
        for i, c in enumerate(candidates)
      ]
      return json.dumps(
        {
          'need_select': True,
          'candidates': [{'id': c['chat_id'], 'name': c['name']} for c in candidates],
          'message': f'找到 {len(candidates)} 个匹配的群，请让用户确认要读哪个：\n' + '\n'.join(numbered),
        },
        ensure_ascii=False,
      )
    target_chat_id = candidates[0]['chat_id']

  from service.feishu import history
  lines = await history.fetch_recent_messages(target_chat_id, min(limit, 500), start_time, end_time)
  if not lines:
    return json.dumps(
      {'message': '未拉到群消息（群内可能暂无文本消息，或机器人缺少读取群消息权限）'},
      ensure_ascii=False,
    )
  return '\n'.join(lines)


@tool
@sanitizing
async def fetch_p2p_messages(limit: int = 50, start_time: str = '', end_time: str = '') -> str:
  """读取当前用户与机器人的飞书私聊记录。

  当用户在平台 Web 或飞书里说「看下我和机器人的私聊」「查下之前私聊说了什么」时调用。
  结果按时间升序，格式「[时间] 发送者: 文本」。需要用户已绑定飞书，且之前私聊过机器人。
  用户要「查某天/某段时间」时，用 get_current_time 算**日期**传 start_time/end_time 并把
  limit 设大（如 500）拉全，工具自动翻页。**只传日期字符串，不要自己算时间戳**（内部会换算）。

  Args:
    limit: 拉取条数，默认 50，最多 500（超过单页 50 时自动翻页）。
    start_time: 起始时间，传**日期**即可（`2026-09-08` 或 `2026-09-08 14:00`），空串不过滤。
    end_time: 结束时间（格式同 start_time），空串表示到最新。只给到"天"时自动含当天 23:59:59。
  """
  open_id = await _resolve_bound_open_id()
  if not open_id:
    return json.dumps(
      {'error': '请先在「认证中心」绑定飞书账号', 'suggestion': '绑定后才能查询私聊记录'},
      ensure_ascii=False,
    )

  from service.feishu import history
  chat_id = await history.find_p2p_chat_id(open_id)
  if not chat_id:
    return json.dumps(
      {'message': '未找到您与机器人的私聊会话', 'suggestion': '请先在飞书私聊机器人一次'},
      ensure_ascii=False,
    )

  lines = await history.fetch_recent_messages(chat_id, min(limit, 500), start_time, end_time)
  if not lines:
    return json.dumps({'message': '未拉到私聊消息'}, ensure_ascii=False)
  return '\n'.join(lines)


@tool
@sanitizing
async def list_joined_chats() -> str:
  """列出机器人加入的所有飞书群聊名称。

  当用户问「你在哪些群」「列出群名称」「这个群叫什么」时调用。结果每行一个群，
  格式「群名称（当前群） [chat_id=...]」。用户选定某个群后，用 fetch_group_messages(chat_id=...)
  精确读该群消息。当前飞书群聊上下文中的群会带「（当前群）」标注。

  Args:
    无参数。
  """
  from service.feishu import history
  chats = await history.list_joined_chats()
  ctx = get_current_feishu()
  current_id = ctx.get('chat_id') if ctx and ctx.get('chat_type') == 'group' else None
  if not chats:
    return json.dumps(
      {
        'error': '未列出任何群',
        'suggestion': '机器人可能未加入任何群，或缺少「获取群信息」权限（im:chat:readonly）',
      },
      ensure_ascii=False,
    )
  lines = []
  for c in chats:
    marker = '（当前群）' if c['chat_id'] == current_id else ''
    lines.append(f'{c["name"]}{marker} [chat_id={c["chat_id"]}]')
  return '\n'.join(lines)


@tool
@sanitizing
async def list_group_members(chat_id: str = '') -> str:
  """列出飞书群成员（含真实姓名）。

  用户问「群里都有谁」「群成员有哪些」「这个群有哪些人」时调用。飞书群聊上下文里
  chat_id 留空读当前群；Web / 私聊场景需先 list_joined_chats 列群，用户选定后用其
  chat_id 调用。结果每行一个成员姓名。需要机器人有「获取群信息」权限（im:chat:readonly）。
  """
  ctx = get_current_feishu()
  if chat_id:
    target = chat_id
  elif ctx and ctx.get('chat_type') == 'group':
    target = ctx['chat_id']
  else:
    return json.dumps(
      {'error': '请先用 list_joined_chats 列出群，用户选定后传对应 chat_id 查询'},
      ensure_ascii=False,
    )
  from service.feishu import history
  members = await history.list_group_members(target)
  if not members:
    return json.dumps(
      {'message': '未拉到群成员（机器人可能缺少「获取群信息」权限 im:chat:readonly）'},
      ensure_ascii=False,
    )
  return '\n'.join(m['name'] for m in members)

"""飞书身份绑定：一次性码 + open_id 落库。

绑定码生命周期：Web 端点生成 → Redis 存（TTL）→ 飞书侧 `/connect <code>` 或
卡片输入框提交 → 消费（GETDEL 单次有效）→ 把 lark_open_id 写回系统用户。
"""

import logging
import secrets
from typing import Optional

from config.settings import settings
from config.redis_conn import redis_manager
from service.auth.user_service import user_service

logger = logging.getLogger(__name__)

_CODE_KEY = 'lark:connect:'


def create_connect_code(user_id: str) -> str:
  """生成一次性绑定码并写入 Redis，返回码。"""
  code = secrets.token_urlsafe(16)
  redis_manager.client.set(
    redis_manager.key(f'{_CODE_KEY}{code}'),
    user_id,
    ex=settings.LARK_CONNECT_CODE_TTL,
  )
  return code


def consume_connect_code(code: str) -> Optional[str]:
  """消费一次性码，返回绑定的 user_id；无效/已用返回 None。"""
  code = _normalize_code(code)
  if not code:
    return None
  user_id = redis_manager.client.getdel(redis_manager.key(f'{_CODE_KEY}{code}'))
  return user_id or None


def _normalize_code(code: str) -> str:
  """剥离用户可能多带的 `/connect ` 前缀与空白。"""
  code = (code or '').strip()
  for prefix in ('/connect ', '/connect', 'connect '):
    if code.startswith(prefix):
      code = code[len(prefix):].strip()
      break
  return code


async def bind_open_id(user_id: str, open_id: str, name: str = '') -> bool:
  """把 open_id 绑定到 user_id。若 open_id 已被他人占用，先解绑旧映射。"""
  # 防重：同一 open_id 只允许绑一个系统用户
  existing = await user_service.users.find_one({'lark_open_id': open_id})
  if existing and existing.get('user_id') != user_id:
    await user_service.users.update_one(
      {'user_id': existing['user_id']},
      {'$set': {'lark_open_id': '', 'lark_name': ''}},
    )
    logger.info('飞书 open_id 已从旧用户解绑 old=%s open_id=%s', existing['user_id'], open_id)

  updates = {'lark_open_id': open_id}
  if name:
    updates['lark_name'] = name
  await user_service.update_user(user_id, updates)
  return True


async def resolve_user_by_open_id(open_id: str) -> Optional[dict]:
  """按飞书 open_id 查系统用户。"""
  return await user_service.users.find_one({'lark_open_id': open_id})


async def unbind(user_id: str) -> None:
  """解绑当前用户的飞书账号。"""
  await user_service.update_user(user_id, {'lark_open_id': '', 'lark_name': ''})

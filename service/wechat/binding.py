"""个人微信身份绑定：一次性码 + ilink_user_id 落库。

绑定码生命周期与飞书一致（由 `service/channels/connect_code.py` 通用实现），
只是把外部身份换成 `wechat_user_id`（形如 `xxx@im.wechat`）写回系统用户。
"""

import logging

from config.settings import settings
from service.auth.user_service import user_service
from service.channels import connect_code

logger = logging.getLogger(__name__)

_CODE_PREFIX = 'wechat:connect:'


def create_connect_code(user_id: str) -> str:
  """生成一次性绑定码并写入 Redis，返回码。"""
  return connect_code.create_connect_code(
    user_id, prefix=_CODE_PREFIX, ttl=settings.WECHAT_CONNECT_CODE_TTL,
  )


def consume_connect_code(code: str) -> str | None:
  """消费一次性码，返回绑定的 user_id；无效/已用返回 None。"""
  return connect_code.consume_connect_code(code, prefix=_CODE_PREFIX)


async def bind_wechat_id(user_id: str, wechat_user_id: str, name: str = '') -> bool:
  """把 wechat_user_id 绑定到 user_id。若已被他人占用，先解绑旧映射。"""
  existing = await user_service.users.find_one({'wechat_user_id': wechat_user_id})
  if existing and existing.get('user_id') != user_id:
    await user_service.users.update_one(
      {'user_id': existing['user_id']},
      {'$set': {'wechat_user_id': '', 'wechat_name': ''}},
    )
    logger.info('微信 user_id 已从旧用户解绑 old=%s wechat=%s', existing['user_id'], wechat_user_id)

  updates = {'wechat_user_id': wechat_user_id}
  if name:
    updates['wechat_name'] = name
  await user_service.update_user(user_id, updates)
  return True


async def resolve_user_by_wechat_id(wechat_user_id: str) -> dict | None:
  """按 wechat_user_id 查系统用户。"""
  return await user_service.users.find_one({'wechat_user_id': wechat_user_id})


async def unbind(user_id: str) -> None:
  """解绑当前用户的微信账号。"""
  await user_service.update_user(user_id, {'wechat_user_id': '', 'wechat_name': ''})

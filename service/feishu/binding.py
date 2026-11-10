"""飞书身份绑定：一次性码 + open_id 落库。

绑定码生命周期：Web 端点生成 → Redis 存（TTL）→ 飞书侧 `/connect <code>` 或
卡片输入框提交 → 消费（GETDEL 单次有效）→ 把 lark_open_id 写回系统用户。
码本身由 `service/channels/connect_code.py` 通用实现（各通道用不同 prefix）。
"""

import logging

from config.settings import settings
from service.auth.user_service import user_service
from service.channels import connect_code

logger = logging.getLogger(__name__)

_CODE_PREFIX = 'lark:connect:'


def create_connect_code(user_id: str) -> str:
  """生成一次性绑定码并写入 Redis，返回码。"""
  return connect_code.create_connect_code(
    user_id, prefix=_CODE_PREFIX, ttl=settings.LARK_CONNECT_CODE_TTL,
  )


def consume_connect_code(code: str) -> str | None:
  """消费一次性码，返回绑定的 user_id；无效/已用返回 None。"""
  return connect_code.consume_connect_code(code, prefix=_CODE_PREFIX)


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


async def resolve_user_by_open_id(open_id: str) -> dict | None:
  """按飞书 open_id 查系统用户。"""
  return await user_service.users.find_one({'lark_open_id': open_id})


async def unbind(user_id: str) -> None:
  """解绑当前用户的飞书账号。"""
  await user_service.update_user(user_id, {'lark_open_id': '', 'lark_name': ''})

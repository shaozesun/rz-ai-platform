"""通道通用：一次性绑定码。

生命周期：Web 端点生成 → Redis 存（TTL）→ IM 侧用户把码发给机器人 → 消费
（GETDEL 单次有效）→ 把外部身份写回系统用户。各通道用不同 `prefix` 隔离命名空间，
互不干扰。
"""

import secrets
from typing import Optional

from config.redis_conn import redis_manager

_PREFIXES = ('/connect ', '/connect', 'connect ')


def create_connect_code(user_id: str, *, prefix: str, ttl: int) -> str:
  """生成一次性绑定码并写入 Redis，返回码。"""
  code = secrets.token_urlsafe(16)
  redis_manager.client.set(redis_manager.key(f'{prefix}{code}'), user_id, ex=ttl)
  return code


def consume_connect_code(code: str, *, prefix: str) -> Optional[str]:
  """消费一次性码，返回绑定的 user_id；无效/已用返回 None。"""
  code = normalize_code(code)
  if not code:
    return None
  user_id = redis_manager.client.getdel(redis_manager.key(f'{prefix}{code}'))
  return user_id or None


def normalize_code(code: str) -> str:
  """剥离用户可能多带的 `/connect ` 前缀与空白。"""
  code = (code or '').strip()
  for prefix in _PREFIXES:
    if code.startswith(prefix):
      code = code[len(prefix):].strip()
      break
  return code

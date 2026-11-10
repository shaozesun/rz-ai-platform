"""通道通用：产物下载签名 token。

IM 里的产物链接在浏览器侧没有登录态，故为真实存在的产物签一个短期 token 存在 Redis，
公开下载端点凭 token 放行（token 即授权，TTL 内可多次下载）。各通道用不同 `prefix`
隔离命名空间。
"""

import json
import secrets
from typing import Optional

from config.redis_conn import redis_manager


def create_download_token(
  thread_id: str, dataset_id: str, filename: str, *, prefix: str, ttl: int,
) -> str:
  """为产物生成签名 token 并写入 Redis，返回 token。"""
  token = secrets.token_urlsafe(24)
  payload = json.dumps(
    {'thread_id': thread_id, 'dataset_id': dataset_id, 'filename': filename},
  )
  redis_manager.client.set(redis_manager.key(f'{prefix}{token}'), payload, ex=ttl)
  return token


def resolve_download_token(token: str, *, prefix: str) -> Optional[dict]:
  """解析下载 token，返回 {thread_id, dataset_id, filename}；无效返回 None。"""
  raw = redis_manager.client.get(redis_manager.key(f'{prefix}{token}'))
  if not raw:
    return None
  try:
    return json.loads(raw)
  except json.JSONDecodeError:
    return None

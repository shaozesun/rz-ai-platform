import uuid
import secrets
from datetime import datetime, timedelta, timezone
import jwt
from config.settings import settings
from config.redis_conn import redis_manager

SECRET_KEY = settings.JWT_PRIVATE_KEY or secrets.token_hex(32)
ALGORITHM = 'HS256'


def create_access_token(user_id: str, phone: str, roles: list[str]) -> str:
  now = datetime.now(timezone.utc)
  payload = {
    'user_id': user_id,
    'phone': phone,
    'roles': roles,
    'iat': now,
    'exp': now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
    'jti': uuid.uuid4().hex,
  }
  return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def create_refresh_token(user_id: str) -> str:
  token = secrets.token_hex(32)
  key = redis_manager.key(f'refresh:{token}')
  redis_manager.client.setex(
    key, settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400, user_id
  )
  return token


def verify_access_token(token: str) -> dict:
  """验证 access_token, 返回 payload. 无效则抛出异常."""
  return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def verify_refresh_token(token: str) -> str | None:
  """验证 refresh_token, 返回 user_id 或 None."""
  key = redis_manager.key(f'refresh:{token}')
  return redis_manager.client.get(key)


def revoke_refresh_token(token: str):
  key = redis_manager.key(f'refresh:{token}')
  redis_manager.client.delete(key)


def revoke_all_user_tokens(user_id: str):
  """通过 SCAN 撤销某用户所有 refresh token"""
  pattern = redis_manager.key('refresh:*')
  cursor = 0
  while True:
    cursor, keys = redis_manager.client.scan(cursor, match=pattern, count=100)
    for key in keys:
      if redis_manager.client.get(key) == user_id:
        redis_manager.client.delete(key)
    if cursor == 0:
      break


def blacklist_access_token(jti: str, expire_seconds: int):
  """将 access token 加入黑名单 (用户主动登出时调用)"""
  key = redis_manager.key(f'bl:{jti}')
  redis_manager.client.setex(key, expire_seconds, '1')


def is_token_blacklisted(jti: str) -> bool:
  key = redis_manager.key(f'bl:{jti}')
  return redis_manager.client.exists(key) > 0

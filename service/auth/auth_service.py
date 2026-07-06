"""认证服务: 登录/Token 管理"""

import hashlib
import os

import logging
from core.auth_engine import (
  create_access_token, create_refresh_token,
  verify_refresh_token, revoke_refresh_token,
  revoke_all_user_tokens, blacklist_access_token,
  verify_access_token,
)
from service.auth.user_service import user_service

logger = logging.getLogger(__name__)

HASH_ITERATIONS = 100000


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
  """返回 (hash_hex, salt_hex)"""
  if salt is None:
    salt = os.urandom(32)
  dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, HASH_ITERATIONS)
  return dk.hex(), salt.hex()


def _verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
  dk, _ = _hash_password(password, bytes.fromhex(salt_hex))
  return dk == hash_hex


class AuthService:

  async def login(self, phone: str, password: str, ip: str = '') -> dict:
    """手机号 + 密码登录"""
    user = await user_service.get_by_phone(phone)
    if not user:
      raise ValueError('该手机号未注册，请先注册')

    if not _verify_password(password, user.get('password_salt', ''), user.get('password_hash', '')):
      raise ValueError('密码错误')

    if user.get('status') != 'ACTIVE':
      raise ValueError('账号已被禁用, 请联系管理员')

    await user_service.update_login_info(user['user_id'], ip)

    access_token = create_access_token(
      user['user_id'], user['phone'], user.get('roles', [])
    )
    refresh_token = create_refresh_token(user['user_id'])

    user_info = await user_service.get_user_public(user)

    return {
      'access_token': access_token,
      'refresh_token': refresh_token,
      'user': user_info,
    }

  async def register(self, phone: str, password: str, ip: str = '') -> dict:
    """手机号 + 密码注册"""
    existing = await user_service.get_by_phone(phone)
    if existing:
      raise ValueError('该手机号已注册，请直接登录')

    if len(password) < 6:
      raise ValueError('密码不能少于6位')

    user = await user_service.create_user(phone, password)

    await user_service.update_login_info(user['user_id'], ip)

    access_token = create_access_token(
      user['user_id'], user['phone'], user.get('roles', [])
    )
    refresh_token = create_refresh_token(user['user_id'])

    user_info = await user_service.get_user_public(user)

    return {
      'access_token': access_token,
      'refresh_token': refresh_token,
      'user': user_info,
    }

  async def refresh_token(self, refresh_token: str) -> dict:
    """刷新 access_token"""
    user_id = verify_refresh_token(refresh_token)
    if not user_id:
      raise ValueError('refresh_token 无效或已过期')

    user = await user_service.get_by_id(user_id)
    if not user or user.get('status') != 'ACTIVE':
      raise ValueError('用户不存在或已禁用')

    # 撤销旧 refresh_token
    revoke_refresh_token(refresh_token)

    # 签发新 token
    new_access = create_access_token(
      user['user_id'], user['phone'], user.get('roles', [])
    )
    new_refresh = create_refresh_token(user['user_id'])

    return {
      'access_token': new_access,
      'refresh_token': new_refresh,
    }

  async def logout(self, access_token: str, refresh_token: str):
    """退出登录"""
    try:
      payload = verify_access_token(access_token)
      # 计算剩余有效期, 加入黑名单
      import time
      from datetime import datetime, timezone
      exp = payload.get('exp', 0)
      now = datetime.now(timezone.utc).timestamp()
      remaining = max(0, int(exp - now))
      blacklist_access_token(payload['jti'], remaining)
    except Exception:
      pass
    if refresh_token:
      revoke_refresh_token(refresh_token)

  async def get_current_user_info(self, user_id: str) -> dict:
    """获取当前用户信息 (token 刷新后角色可能已变更, 重新查库)"""
    user = await user_service.get_by_id(user_id)
    if not user:
      raise ValueError('用户不存在')
    return await user_service.get_user_public(user)


auth_service = AuthService()

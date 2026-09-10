"""认证服务: 登录/Token 管理"""

import asyncio
import hashlib
import os
import re

import logging
from core.auth_engine import (
  create_access_token, create_refresh_token,
  verify_refresh_token, revoke_refresh_token,
  revoke_all_user_tokens, blacklist_access_token,
  verify_access_token,
)
from models.user import UserStatus
from service.auth.user_service import user_service

logger = logging.getLogger(__name__)

HASH_ITERATIONS = 100000

PASSWORD_RE = re.compile(
  r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=\[\]{};'
  r"':" r'"\\|,.<>\/?~`]).{8,}$'
)
PASSWORD_MSG = '密码至少8位，必须包含大写字母、小写字母、数字和特殊符号'


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

    status = user.get('status')
    if status == 'PENDING':
      raise ValueError('账号正在审批中，通过后方可登录')
    if status != 'ACTIVE':
      raise ValueError('账号已被禁用，请联系管理员')

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
    """手机号 + 密码注册申请（需管理员审批后才能登录）"""
    existing = await user_service.get_by_phone(phone)
    if existing:
      status = existing.get('status')
      if status == 'PENDING':
        raise ValueError('该手机号正在审批中，请耐心等待')
      if status == 'DISABLED':
        raise ValueError('账号已被禁用，请联系管理员')
      raise ValueError('该手机号已注册，请直接登录')

    if not PASSWORD_RE.match(password):
      raise ValueError(PASSWORD_MSG)

    user = await user_service.create_user(phone, password)
    await user_service.create_application(
      user['user_id'], phone, '',
      requested_roles=['user'],
      reason='新用户注册申请',
    )

    # 注册时一次性从 mgmt 同步档案（手机号 + 默认密码 666666），后台异步、失败静默。
    # 只在注册时拉取一次；后续 rz 登录不再调用。
    asyncio.create_task(user_service.enrich_user_from_mgmt(user['user_id'], phone))

    return {
      'pending': True,
      'message': '注册申请已提交，请等待管理员审批',
    }

  async def mgmt_login(self, username: str, password: str, ip: str = '') -> dict:
    """综合管理平台账号登录（SSO）：验证 mgmt 账号 → find-or-create rz 用户 →
    自动绑定认证中心授权 → 完善档案（姓名/部门/专业/职位）→ 发 rz JWT。"""
    from config.settings import settings
    from core.mgmt_credentials import encrypt_password
    from service.query.mgmt import http as mgmt_http

    # 验证 mgmt 账号密码并取档案（失败抛 MgmtError，调用方转 400 引导）。
    # 综合管理平台账号均为手机号，直接按手机号匹配 rz 用户（同人同号合并）。
    ctx = await mgmt_http._login_with_credentials(username, password)
    phone = username if re.match(r'^1[3-9]\d{9}$', username) else username

    user = await user_service.get_by_phone(phone)

    try:
      password_enc = encrypt_password(password)
    except Exception:
      password_enc = ''

    if user:
      user_id = user['user_id']
      if user.get('status') == UserStatus.DISABLED.value:
        raise ValueError('账号已被禁用，请联系管理员')
      updates = user_service.apply_mgmt_profile(ctx)
      updates.update({'mgmt_username': username, 'mgmt_password_enc': password_enc})
      # SSO 登录证明身份：审批中的账号直接激活（DISABLED 除外）
      if user.get('status') in (None, UserStatus.PENDING.value):
        updates['status'] = UserStatus.ACTIVE.value
      await user_service.update_user(user_id, updates)
    else:
      user_id = await user_service.create_user_from_mgmt(
        phone, ctx, settings.MGMT_SSO_DEFAULT_ROLE, password,
      )
      if password_enc:
        await user_service.update_user(user_id, {
          'mgmt_username': username, 'mgmt_password_enc': password_enc,
        })

    # 缓存 mgmt token/档案上下文（Agent 平台查询按本人身份，免二次登录）
    await mgmt_http.cache_user_login(user_id, ctx)

    await user_service.update_login_info(user_id, ip)
    fresh = await user_service.get_by_id(user_id)
    access_token = create_access_token(user_id, phone, fresh.get('roles', []))
    refresh_token = create_refresh_token(user_id)
    user_info = await user_service.get_user_public(fresh)
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

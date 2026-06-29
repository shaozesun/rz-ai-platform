"""用户 & 角色服务"""

import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Optional
from config.settings import settings
from config.mongodb_conn import mongodb_manager
import logging

from models.user import (
  User, UserPublic, Role, RoleApplication,
  UserType, UserStatus, ApplicationStatus,
  BUILTIN_PERMISSIONS, BUILTIN_ROLES,
)
from core.rbac import load_role_permissions, invalidate_role_cache

HASH_ITERATIONS = 100000


def _hash_password(password: str) -> tuple[str, str]:
  """返回 (hash_hex, salt_hex)"""
  salt = os.urandom(32)
  dk = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, HASH_ITERATIONS)
  return dk.hex(), salt.hex()

logger = logging.getLogger(__name__)


class UserService:

  @property
  def users(self):
    return mongodb_manager.db.users

  @property
  def roles(self):
    return mongodb_manager.db.roles

  @property
  def applications(self):
    return mongodb_manager.db.applications

  @property
  def audit_logs(self):
    return mongodb_manager.db.audit_logs

  # ==================== 初始化 ====================

  async def init_builtin_roles(self):
    """初始化预置角色 (幂等)"""
    for role_data in BUILTIN_ROLES:
      existing = await self.roles.find_one({'role_id': role_data['role_id']})
      if existing:
        # 更新权限 (可能版本升级新增了权限)
        await self.roles.update_one(
          {'role_id': role_data['role_id']},
          {'$set': {
            'permissions': role_data['permissions'],
            'description': role_data['description'],
          }}
        )
      else:
        await self.roles.insert_one({**role_data, 'created_at': datetime.utcnow()})
    invalidate_role_cache()
    logger.info('预置角色初始化完成')

  # ==================== 用户 ====================

  async def get_by_phone(self, phone: str) -> Optional[dict]:
    return await self.users.find_one({'phone': phone})

  async def get_by_id(self, user_id: str) -> Optional[dict]:
    return await self.users.find_one({'user_id': user_id})

  async def create_user(self, phone: str, password: str = '') -> dict:
    """新用户注册, 默认 user 角色"""
    now = datetime.utcnow()
    pw_hash, pw_salt = _hash_password(password) if password else ('', '')
    user = {
      'user_id': uuid.uuid4().hex,
      'phone': phone,
      'name': '',
      'email': None,
      'company': None,
      'user_type': UserType.UNVERIFIED.value,
      'status': UserStatus.ACTIVE.value,
      'roles': ['user'],
      'permissions': [],
      'password_hash': pw_hash,
      'password_salt': pw_salt,
      'created_at': now,
      'last_login_at': now,
      'last_login_ip': '',
    }
    await self.users.insert_one(user)
    logger.info(f'新用户注册: {phone}')
    return user

  async def update_login_info(self, user_id: str, ip: str):
    await self.users.update_one(
      {'user_id': user_id},
      {'$set': {'last_login_at': datetime.utcnow(), 'last_login_ip': ip}}
    )

  async def get_user_public(self, user: dict) -> dict:
    """构建用户公开信息, 附带权限列表"""
    role_ids = user.get('roles', [])
    permissions = await load_role_permissions(role_ids)
    # 合并用户独立权限（非角色授予的单独权限）
    permissions.update(user.get('permissions', []))
    # 权限全开模式 — 所有认证用户看到全部权限（角色保持不变）
    if settings.PERMISSION_OPEN_MODE:
      permissions = {p.perm_key for p in BUILTIN_PERMISSIONS}
    return {
      'user_id': user['user_id'],
      'phone': user['phone'],
      'name': user.get('name', ''),
      'email': user.get('email'),
      'company': user.get('company'),
      'user_type': user.get('user_type', UserType.UNVERIFIED.value),
      'status': user.get('status', UserStatus.ACTIVE.value),
      'roles': role_ids,
      'permissions': sorted(permissions),
      'avatar': user.get('avatar', ''),
      'created_at': user.get('created_at', datetime.utcnow()),
    }

  async def update_user(self, user_id: str, updates: dict):
    await self.users.update_one(
      {'user_id': user_id},
      {'$set': updates}
    )

  async def reset_password(self, user_id: str, new_password: str) -> str:
    """重置用户密码，返回新密码"""
    pw_hash, pw_salt = _hash_password(new_password)
    await self.users.update_one(
      {'user_id': user_id},
      {'$set': {'password_hash': pw_hash, 'password_salt': pw_salt}}
    )
    logger.info(f'密码已重置: user_id={user_id}')

  async def list_users(self, page: int = 1, page_size: int = 20,
                       keyword: str = '', status: str = '') -> tuple[list, int]:
    query = {}
    if keyword:
      query['$or'] = [
        {'phone': {'$regex': keyword}},
        {'name': {'$regex': keyword}},
      ]
    if status:
      query['status'] = status

    total = await self.users.count_documents(query)
    items = await self.users.find(query) \
      .sort('created_at', -1) \
      .skip((page - 1) * page_size) \
      .limit(page_size) \
      .to_list(None)
    return items, total

  # ==================== 角色 ====================

  async def get_all_roles(self) -> list:
    roles = await self.roles.find().to_list(None)
    for r in roles:
      r['_id'] = str(r['_id'])
    return roles

  async def get_role(self, role_id: str) -> Optional[dict]:
    return await self.roles.find_one({'role_id': role_id})

  async def create_role(self, role_id: str, name: str,
                        permissions: list[str], description: str = '') -> dict:
    role = {
      'role_id': role_id,
      'name': name,
      'description': description,
      'permissions': permissions,
      'is_builtin': False,
      'created_at': datetime.utcnow(),
    }
    await self.roles.insert_one(role)
    invalidate_role_cache(role_id)
    return role

  async def update_role(self, role_id: str, updates: dict):
    updates['updated_at'] = datetime.utcnow()
    await self.roles.update_one({'role_id': role_id}, {'$set': updates})
    invalidate_role_cache(role_id)

  # ==================== 权限申请 ====================

  async def create_application(self, user_id: str, phone: str, name: str,
                               requested_roles: list[str], reason: str,
                               requested_permissions: list[str] = None) -> dict:
    if requested_permissions is None:
      requested_permissions = []

    # 检查是否已有该角色（轻量检查，实际防重靠唯一索引）
    user = await self.get_by_id(user_id)
    existing_roles = set(user.get('roles', []))
    already_has = [r for r in requested_roles if r in existing_roles]
    if already_has:
      raise ValueError(f'已拥有角色: {", ".join(already_has)}')

    # 检查是否已有该权限
    existing_perms = set(user.get('permissions', []))
    already_has_perm = [p for p in requested_permissions if p in existing_perms]
    if already_has_perm:
      raise ValueError(f'已拥有权限: {", ".join(already_has_perm)}')

    app = {
      'application_id': uuid.uuid4().hex,
      'user_id': user_id,
      'phone': phone,
      'name': name,
      'requested_roles': requested_roles,
      'requested_permissions': requested_permissions,
      'reason': reason,
      'status': ApplicationStatus.PENDING.value,
      'reviewer_id': None,
      'review_reason': None,
      'reviewed_at': None,
      'created_at': datetime.utcnow(),
    }
    try:
      await self.applications.insert_one(app)
    except Exception as e:
      # 唯一索引 (user_id, requested_roles, status) 防重复提交
      if 'duplicate key' in str(e).lower() or 'E11000' in str(e):
        raise ValueError(f'角色 {requested_roles} 已有待处理的申请，请勿重复提交')
      raise
    logger.info(f'权限申请: {phone} 角色 {requested_roles}, 权限 {requested_permissions}')
    app['_id'] = str(app['_id'])
    return app

  async def list_applications(self, status: str = '', page: int = 1,
                              page_size: int = 20,
                              search: str = '') -> tuple[list, int]:
    query = {}
    if status:
      query['status'] = status
    if search:
      query['$or'] = [
        {'name': {'$regex': search, '$options': 'i'}},
        {'phone': {'$regex': search, '$options': 'i'}},
      ]
    total = await self.applications.count_documents(query)
    items = await self.applications.find(query) \
      .sort('created_at', -1) \
      .skip((page - 1) * page_size) \
      .limit(page_size) \
      .to_list(None)
    for item in items:
      item['_id'] = str(item['_id'])
    return items, total

  async def get_user_applications(self, user_id: str) -> list:
    items = await self.applications.find(
      {'user_id': user_id}
    ).sort('created_at', -1).to_list(None)
    for item in items:
      item['_id'] = str(item['_id'])
    return items

  async def approve_application(self, application_id: str, reviewer_id: str) -> dict:
    # 原子操作：仅当 status=PENDING 时更新为 APPROVED，防止重复审批
    app = await self.applications.find_one_and_update(
      {'application_id': application_id, 'status': ApplicationStatus.PENDING.value},
      {'$set': {
        'status': ApplicationStatus.APPROVED.value,
        'reviewer_id': reviewer_id,
        'reviewed_at': datetime.utcnow(),
      }}
    )
    if not app:
      raise ValueError('申请不存在或已处理')

    user_updates = {}
    if app.get('requested_roles'):
      user_updates['roles'] = {'$each': app['requested_roles']}
    if app.get('requested_permissions'):
      user_updates['permissions'] = {'$each': app['requested_permissions']}

    if user_updates:
      await self.users.update_one(
        {'user_id': app['user_id']},
        {'$addToSet': user_updates},
      )

    logger.info(f'审批通过: {app["phone"]} → 角色 {app.get("requested_roles", [])}, 权限 {app.get("requested_permissions", [])}')
    app['_id'] = str(app['_id'])
    return app

  async def reject_application(self, application_id: str, reviewer_id: str,
                               reason: str) -> dict:
    app = await self.applications.find_one(
      {'application_id': application_id, 'status': ApplicationStatus.PENDING.value}
    )
    if not app:
      raise ValueError('申请不存在或已处理')

    await self.applications.update_one(
      {'application_id': application_id},
      {'$set': {
        'status': ApplicationStatus.REJECTED.value,
        'reviewer_id': reviewer_id,
        'review_reason': reason,
        'reviewed_at': datetime.utcnow(),
      }}
    )
    logger.info(f'审批拒绝: {app["phone"]} → {app["requested_roles"]}, 原因: {reason}')
    app['_id'] = str(app['_id'])
    return app

  # ==================== 审计 ====================

  async def write_audit(self, user_id: str, phone: str, action: str,
                        resource: str, detail: str = '', ip: str = '',
                        operator_name: str = ''):
    await self.audit_logs.insert_one({
      'log_id': uuid.uuid4().hex,
      'user_id': user_id,
      'phone': phone,
      'operator_name': operator_name,
      'action': action,
      'resource': resource,
      'detail': detail,
      'ip': ip,
      'created_at': datetime.now(timezone.utc),
    })

  async def list_audit_logs(self, page: int = 1, page_size: int = 20,
                            action: str = '', user_id: str = '') -> tuple[list, int]:
    query = {}
    if action:
      query['action'] = action
    if user_id:
      query['user_id'] = user_id
    total = await self.audit_logs.count_documents(query)
    items = await self.audit_logs.find(query) \
      .sort('created_at', -1) \
      .skip((page - 1) * page_size) \
      .limit(page_size) \
      .to_list(None)
    for item in items:
      item['_id'] = str(item['_id'])
    return items, total


user_service = UserService()

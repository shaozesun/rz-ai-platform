"""RBAC 角色权限引擎"""

from functools import wraps
from typing import Callable, Optional
from fastapi import HTTPException, Request
from config.mongodb_conn import mongodb_manager
from config.settings import settings

# 缓存: role_id → [perm_key, ...]
_role_permissions_cache: dict[str, set[str]] = {}


async def load_role_permissions(role_ids: list[str]) -> set[str]:
  """从 MongoDB 加载角色对应的所有权限"""
  perms: set[str] = set()
  uncached_ids = [rid for rid in role_ids if rid not in _role_permissions_cache]

  if uncached_ids:
    roles = await mongodb_manager.db.roles.find(
      {'role_id': {'$in': uncached_ids}}
    ).to_list(None)
    for role in roles:
      pset = set(role.get('permissions', []))
      _role_permissions_cache[role['role_id']] = pset

  for rid in role_ids:
    if rid in _role_permissions_cache:
      perms.update(_role_permissions_cache[rid])

  return perms


def invalidate_role_cache(role_id: Optional[str] = None):
  """清除权限缓存 (角色变更后调用)"""
  if role_id:
    _role_permissions_cache.pop(role_id, None)
  else:
    _role_permissions_cache.clear()


def match_permission(user_perms: set[str], required: str) -> bool:
  """通配符权限匹配. 用户持有 ai:* 可匹配 ai:chat、ai:knowledge 等"""
  if required in user_perms:
    return True
  for p in user_perms:
    if p.endswith(':*') and required.startswith(p[:-2]):
      return True
  return False


def _get_request(*args, **kwargs) -> Optional[Request]:
  for arg in args:
    if isinstance(arg, Request):
      return arg
  for val in kwargs.values():
    if isinstance(val, Request):
      return val
  return None


def require_permission(permission: str):
  """权限校验装饰器 (支持通配符)"""
  def decorator(func: Callable):
    @wraps(func)
    async def wrapper(*args, **kwargs):
      if settings.PERMISSION_OPEN_MODE:
        return await func(*args, **kwargs)

      request = _get_request(*args, **kwargs)
      if request is None:
        raise HTTPException(500, 'Request object not found')

      user = getattr(request.state, 'current_user', None)
      if not user:
        raise HTTPException(401, '未登录')

      user_perms: set = getattr(request.state, 'user_permissions', set())
      if not match_permission(user_perms, permission):
        raise HTTPException(403, f'没有权限: {permission}')

      return await func(*args, **kwargs)
    return wrapper
  return decorator


def require_any_permission(*permissions: str):
  """需要拥有任意一个权限 (支持通配符)"""
  def decorator(func: Callable):
    @wraps(func)
    async def wrapper(*args, **kwargs):
      if settings.PERMISSION_OPEN_MODE:
        return await func(*args, **kwargs)

      request = _get_request(*args, **kwargs)
      if request is None:
        raise HTTPException(500, 'Request object not found')

      user_perms = getattr(request.state, 'user_permissions', set())
      if not any(match_permission(user_perms, p) for p in permissions):
        raise HTTPException(403, f'没有所需权限, 需要: {permissions}')

      return await func(*args, **kwargs)
    return wrapper
  return decorator

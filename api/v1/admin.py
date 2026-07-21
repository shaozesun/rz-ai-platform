"""Root 管理平台 API"""

from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel, Field
from core.rbac import require_permission
from service.auth import user_service

router = APIRouter(prefix='/admin')


async def _audit(request: Request, action: str, resource: str, detail: str, ip: str = ''):
  """写审计日志，自动提取操作人信息"""
  await user_service.write_audit(
    request.state.user_id,
    request.state.current_user.get('phone', ''),
    action, resource, detail, ip,
    operator_name=request.state.current_user.get('name', ''),
  )


# ==================== 用户管理 ====================

@router.get('/users')
@require_permission('system:admin')
async def list_users(
  request: Request,
  page: int = Query(1, ge=1),
  page_size: int = Query(20, ge=1, le=100),
  keyword: str = '',
  status: str = '',
):
  items, total = await user_service.list_users(page, page_size, keyword, status)
  # 脱敏: 只返回公开字段
  public_items = []
  for u in items:
    public_items.append(await user_service.get_user_public(u))
  return {'ok': True, 'data': {'items': public_items, 'total': total, 'page': page}}


@router.get('/users/{user_id}')
@require_permission('system:admin')
async def get_user(user_id: str, request: Request):
  user = await user_service.get_by_id(user_id)
  if not user:
    raise HTTPException(404, '用户不存在')
  return {'ok': True, 'data': await user_service.get_user_public(user)}


@router.put('/users/{user_id}/status')
@require_permission('system:admin')
async def update_user_status(
  user_id: str, request: Request,
  status: str = Query(..., pattern='^(ACTIVE|DISABLED)$'),
):
  await user_service.update_user(user_id, {'status': status})
  await _audit(request, 'admin.update_status', 'user', f'用户 {user_id} 状态 → {status}')
  user = await user_service.get_by_id(user_id)
  return {'ok': True, 'data': await user_service.get_user_public(user)}


@router.put('/users/{user_id}/roles')
@require_permission('system:admin')
async def update_user_roles(user_id: str, request: Request):
  """直接修改用户角色"""
  body = await request.json()
  roles = body.get('roles', [])
  await user_service.update_user(user_id, {'roles': roles, 'permissions': []})
  await _audit(request, 'admin.update_roles', 'user', f'用户 {user_id} 角色 → {roles}')
  user = await user_service.get_by_id(user_id)
  return {'ok': True, 'data': await user_service.get_user_public(user)}


@router.put('/users/{user_id}/permissions')
@require_permission('system:admin')
async def update_user_permissions(user_id: str, request: Request):
  """直接修改用户权限"""
  body = await request.json()
  permissions = body.get('permissions', [])
  await user_service.update_user(user_id, {'permissions': permissions})
  await _audit(request, 'admin.update_permissions', 'user', f'用户 {user_id} 权限 → {permissions}')
  user = await user_service.get_by_id(user_id)
  return {'ok': True, 'data': await user_service.get_user_public(user)}


@router.post('/users/{user_id}/reset-password')
@require_permission('system:admin')
async def reset_user_password(user_id: str, request: Request):
  """管理员重置用户密码，返回新密码"""
  import secrets
  new_pw = secrets.token_hex(8)
  await user_service.reset_password(user_id, new_pw)
  await _audit(request, 'admin.reset_password', 'user', f'重置用户 {user_id} 密码')
  user = await user_service.get_by_id(user_id)
  return {'ok': True, 'data': {'new_password': new_pw, 'user': await user_service.get_user_public(user)}}


# ==================== 角色管理 ====================

@router.get('/roles')
@require_permission('system:admin')
async def list_roles(request: Request):
  roles = await user_service.get_all_roles()
  return {'ok': True, 'data': roles}


class CreateRoleRequest(BaseModel):
  role_id: str = Field(..., pattern=r'^[a-z_][a-z0-9_]*$')
  name: str
  permissions: list[str]
  description: str = ''


@router.post('/roles')
@require_permission('system:admin')
async def create_role(body: CreateRoleRequest, request: Request):
  existing = await user_service.get_role(body.role_id)
  if existing:
    raise HTTPException(400, 'role_id 已存在')
  role = await user_service.create_role(
    body.role_id, body.name, body.permissions, body.description,
  )
  await _audit(request, 'admin.create_role', 'role', f'角色: {body.role_id}')
  return {'ok': True, 'data': role}


class UpdateRoleRequest(BaseModel):
  name: str = ''
  permissions: list[str] = []
  description: str = ''


@router.put('/roles/{role_id}')
@require_permission('system:admin')
async def update_role(role_id: str, body: UpdateRoleRequest, request: Request):
  role = await user_service.get_role(role_id)
  if not role:
    raise HTTPException(404, '角色不存在')
  if role.get('is_builtin'):
    raise HTTPException(400, '内置角色不可修改')
  updates = {}
  if body.name:
    updates['name'] = body.name
  if body.permissions:
    updates['permissions'] = body.permissions
  if body.description:
    updates['description'] = body.description
  if updates:
    await user_service.update_role(role_id, updates)
  await _audit(request, 'admin.update_role', 'role', f'角色: {role_id}')
  return {'ok': True, 'data': await user_service.get_role(role_id)}


PERM_LABELS = {
  'ai:chat': 'AI 对话',
  'ai:knowledge': '知识库管理',
  'ai:risk': '隐患识别',
  'ai:fire_safety': '消防配置',
  'ai:video': '视频生成',
}

ROLE_LABELS = {
  'admin': '管理员',
  'power_user': '全功能用户',
  'user': '普通用户',
}


def _app_detail(app: dict) -> str:
  """生成审批审计详情（可读格式）"""
  name = app.get('name', '') or app.get('phone', '')
  phone = app.get('phone', '')
  parts = [f'申请人: {name} ({phone})']
  roles = app.get('requested_roles') or []
  perms = app.get('requested_permissions') or []
  if roles:
    parts.append('角色: ' + '、'.join(ROLE_LABELS.get(r, r) for r in roles))
  if perms:
    parts.append('权限: ' + '、'.join(PERM_LABELS.get(p, p) for p in perms))
  return ' | '.join(parts)


# ==================== 审批管理 ====================

@router.get('/applications')
@require_permission('system:admin')
async def list_applications(
  request: Request,
  status: str = '',
  search: str = '',
  page: int = Query(1, ge=1),
  page_size: int = Query(20, ge=1, le=100),
):
  items, total = await user_service.list_applications(status, page, page_size, search)
  return {'ok': True, 'data': {'items': items, 'total': total, 'page': page}}


@router.post('/applications/{application_id}/approve')
@require_permission('system:admin')
async def approve_application(application_id: str, request: Request):
  try:
    app = await user_service.approve_application(
      application_id,
      request.state.current_user['user_id'],
    )
    await _audit(request, 'admin.approve', 'application', f'通过申请 | {_app_detail(app)}')
    return {'ok': True, 'data': app}
  except ValueError as e:
    raise HTTPException(400, str(e))


class BatchApproveRequest(BaseModel):
  ids: list[str] = Field(..., min_length=1)


@router.post('/applications/batch-approve')
@require_permission('system:admin')
async def batch_approve(body: BatchApproveRequest, request: Request):
  ok = 0
  fail = 0
  for app_id in body.ids:
    try:
      app = await user_service.approve_application(
        app_id,
        request.state.current_user['user_id'],
      )
      await _audit(request, 'admin.approve', 'application', f'通过申请 | {_app_detail(app)}')
      ok += 1
    except Exception:
      fail += 1
  return {'ok': True, 'data': {'ok': ok, 'fail': fail}}


class RejectRequest(BaseModel):
  reason: str = ''


@router.post('/applications/{application_id}/reject')
@require_permission('system:admin')
async def reject_application(
  application_id: str,
  body: RejectRequest,
  request: Request,
):
  try:
    app = await user_service.reject_application(
      application_id,
      request.state.current_user['user_id'],
      body.reason,
    )
    await _audit(request, 'admin.reject', 'application', f'驳回申请 | {_app_detail(app)} | 原因: {body.reason or "无"}')
    return {'ok': True, 'data': app}
  except ValueError as e:
    raise HTTPException(400, str(e))


class BatchRejectRequest(BaseModel):
  ids: list[str] = Field(..., min_length=1)
  reason: str = ''


@router.post('/applications/batch-reject')
@require_permission('system:admin')
async def batch_reject(body: BatchRejectRequest, request: Request):
  ok = 0
  fail = 0
  for app_id in body.ids:
    try:
      app = await user_service.reject_application(
        app_id,
        request.state.current_user['user_id'],
        body.reason,
      )
      await _audit(request, 'admin.reject', 'application', f'驳回申请 | {_app_detail(app)} | 原因: {body.reason or "无"}')
      ok += 1
    except Exception:
      fail += 1
  return {'ok': True, 'data': {'ok': ok, 'fail': fail}}


# ==================== 审计日志 ====================

@router.get('/audit-logs')
@require_permission('system:admin')
async def list_audit_logs(
  request: Request,
  page: int = Query(1, ge=1),
  page_size: int = Query(20, ge=1, le=100),
  action: str = '',
  user_id: str = '',
):
  items, total = await user_service.list_audit_logs(page, page_size, action, user_id)
  return {'ok': True, 'data': {'items': items, 'total': total, 'page': page}}

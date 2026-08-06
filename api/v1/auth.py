"""认证相关 API: 登录/验证码/刷新/登出/用户信息/权限申请/重置密码"""

from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel, Field
from service.auth import auth_service, user_service
from config.settings import settings

router = APIRouter(prefix='/auth')


# ==================== 请求/响应模型 ====================

class LoginRequest(BaseModel):
  phone: str = Field(..., pattern=r'^1[3-9]\d{9}$')
  password: str = Field(..., min_length=1)


class RegisterRequest(BaseModel):
  phone: str = Field(..., pattern=r'^1[3-9]\d{9}$')
  password: str = Field(..., min_length=6)


class RefreshRequest(BaseModel):
  refresh_token: str


class LogoutRequest(BaseModel):
  access_token: str = ''
  refresh_token: str = ''


class UpdateProfileRequest(BaseModel):
  name: str = ''
  phone: str = ''
  email: str | None = None
  company: str | None = None
  avatar: str = ''


class ApplyRoleRequest(BaseModel):
  roles: list[str] = []
  permissions: list[str] = []
  reason: str = ''


# ==================== API ====================

@router.post('/login')
async def login(body: LoginRequest, request: Request):
  ip = request.client.host if request.client else ''
  try:
    result = await auth_service.login(body.phone, body.password, ip)
    return {
      'ok': True,
      'data': result,
    }
  except ValueError as e:
    raise HTTPException(400, str(e))


@router.post('/register')
async def register(body: RegisterRequest, request: Request):
  ip = request.client.host if request.client else ''
  try:
    result = await auth_service.register(body.phone, body.password, ip)
    return {
      'ok': True,
      'data': result,
    }
  except ValueError as e:
    raise HTTPException(400, str(e))


@router.post('/refresh')
async def refresh(body: RefreshRequest):
  try:
    result = await auth_service.refresh_token(body.refresh_token)
    return {'ok': True, 'data': result}
  except ValueError as e:
    raise HTTPException(401, str(e))


@router.post('/logout')
async def logout(body: LogoutRequest):
  await auth_service.logout(body.access_token, body.refresh_token)
  return {'ok': True, 'message': '已退出登录'}


@router.get('/me')
async def get_me(request: Request):
  """获取当前用户信息 (需登录)"""
  user_id = request.state.user_id
  user_info = await auth_service.get_current_user_info(user_id)
  user_info['agent_enabled'] = settings.AGENT_ENABLED
  return {'ok': True, 'data': user_info}


@router.put('/profile')
async def update_profile(body: UpdateProfileRequest, request: Request):
  """更新个人资料"""
  updates = {}
  if body.name:
    updates['name'] = body.name
  if body.phone:
    updates['phone'] = body.phone
  if body.email is not None:
    updates['email'] = body.email
  if body.company is not None:
    updates['company'] = body.company
  if body.avatar:
    updates['avatar'] = body.avatar
  if updates:
    await user_service.update_user(request.state.user_id, updates)
  user_info = await auth_service.get_current_user_info(request.state.user_id)
  return {'ok': True, 'data': user_info}


@router.post('/apply')
async def apply_role(body: ApplyRoleRequest, request: Request):
  """申请更多权限"""
  user = request.state.current_user
  try:
    app = await user_service.create_application(
      user['user_id'], user['phone'],
      user.get('name', ''), body.roles, body.reason, body.permissions,
    )
    detail_parts = []
    if body.roles: detail_parts.append(f'申请角色: {body.roles}')
    if body.permissions: detail_parts.append(f'申请权限: {body.permissions}')
    await user_service.write_audit(
      user['user_id'], user['phone'],
      'user.apply', 'application',
      '; '.join(detail_parts), '',
      operator_name=user.get('name', ''),
    )
    return {'ok': True, 'data': app}
  except ValueError as e:
    raise HTTPException(400, str(e))


@router.get('/applications')
async def my_applications(request: Request):
  """查看我的权限申请记录"""
  apps = await user_service.get_user_applications(request.state.user_id)
  return {'ok': True, 'data': apps}


# ==================== 密码重置 (公开接口) ====================

class ResetPasswordRequest(BaseModel):
  phone: str = Field(..., pattern=r'^1[3-9]\d{9}$')
  new_password: str = Field(..., min_length=6)


@router.post('/reset-password')
async def reset_password(body: ResetPasswordRequest):
  """重置密码 — 直接输入手机号和新密码即可"""
  user = await user_service.get_by_phone(body.phone)
  if not user:
    raise HTTPException(400, '该手机号未注册')

  await user_service.reset_password(user['user_id'], body.new_password)
  return {'ok': True, 'message': '密码已重置，请重新登录'}

"""认证相关 API: 登录/验证码/刷新/登出/用户信息/权限申请/重置密码"""

from io import BytesIO
import base64
import httpx
import random
import uuid
from fastapi import APIRouter, Request, HTTPException
import re
from pydantic import BaseModel, Field, field_validator
from PIL import Image, ImageDraw, ImageFont
from service.auth import auth_service, user_service
from service.auth.user_service import mask_mgmt_username
from service.query.mgmt import http as mgmt_http
from core.mgmt_credentials import encrypt_password
from config.settings import settings
from config.redis_conn import redis_manager

router = APIRouter(prefix='/auth')


def _inject_agent_enabled(user_info: dict) -> dict:
  """agent 是否对当前用户可用：全局开关开启且该用户有 ai:agent 权限。

  前端按此字段分流（走 Agent 还是 RAG），普通用户无 ai:agent 时保持 RAG。
  顺带带出 web_search_enabled：联网能力未启用时前端不渲染「联网搜索」开关，
  避免出现点了没反应的死按钮。
  """
  has_perm = 'ai:agent' in (user_info.get('permissions') or [])
  user_info['agent_enabled'] = settings.AGENT_ENABLED and has_perm
  user_info['web_search_enabled'] = (
    settings.AGENT_ENABLED and settings.WEB_ENABLED and has_perm
  )
  # 对话窗口「讲解视频」入口：需要 ai:video 权限（生成动作由工具内再校验一次）
  user_info['video_agent_enabled'] = (
    settings.AGENT_ENABLED and settings.VIDEO_AGENT_ENABLED
    and 'ai:video' in (user_info.get('permissions') or [])
  )
  return user_info


# ==================== Pillow 图形验证码 ====================

def _generate_captcha_image() -> tuple[str, str]:
  """生成「三位数 ±1」算式图片（好算），返回 (answer, base64_png)"""
  n = random.randint(100, 999)
  op = random.choice(['+', '-'])
  answer = str(n + 1 if op == '+' else n - 1)
  text = f'{n} {op} 1 = ?'

  width, height = 160, 50
  img = Image.new('RGB', (width, height), (255, 255, 255))
  draw = ImageDraw.Draw(img)

  for _ in range(3):
    x1, y1 = random.randint(0, width), random.randint(0, height)
    x2, y2 = random.randint(0, width), random.randint(0, height)
    draw.line([(x1, y1), (x2, y2)], fill=(random.randint(150, 220),) * 3, width=1)

  for _ in range(40):
    draw.point(
      (random.randint(0, width - 1), random.randint(0, height - 1)),
      fill=(random.randint(100, 200),) * 3,
    )

  try:
    font = ImageFont.truetype('/Library/Fonts/Arial.ttf', 24)
  except (OSError, IOError):
    try:
      font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', 24)
    except (OSError, IOError):
      font = ImageFont.load_default()

  bbox = draw.textbbox((0, 0), text, font=font)
  tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
  draw.text(((width - tw) // 2, (height - th) // 2), text, fill=(0, 0, 0), font=font)

  buf = BytesIO()
  img.save(buf, format='PNG')
  return answer, base64.b64encode(buf.getvalue()).decode('ascii')


def _check_captcha(captcha_id: str, captcha_code: str):
  """校验图形验证码，失败抛 HTTPException"""
  if not captcha_id or not captcha_code:
    raise HTTPException(400, '请输入验证码')
  key = f'{settings.REDIS_PREFIX}captcha:{captcha_id}'
  stored = redis_manager.client.get(key)
  if stored is None:
    raise HTTPException(400, '验证码已过期，请刷新重试')
  redis_manager.client.delete(key)
  if str(stored) != str(captcha_code):
    raise HTTPException(400, '验证码错误')


@router.get('/captcha')
async def get_captcha():
  """获取图形验证码"""
  answer, img_base64 = _generate_captcha_image()
  captcha_id = uuid.uuid4().hex
  key = f'{settings.REDIS_PREFIX}captcha:{captcha_id}'
  redis_manager.client.set(key, answer, ex=120)
  return {'ok': True, 'data': {'captcha_id': captcha_id, 'image_base64': img_base64}}


# ==================== Turnstile 验证 ====================

async def _verify_turnstile(token: str):
  """验证 Cloudflare Turnstile token，失败抛 HTTPException"""
  if not token:
    raise HTTPException(400, '请完成安全验证')
  async with httpx.AsyncClient() as client:
    resp = await client.post(
      'https://challenges.cloudflare.com/turnstile/v0/siteverify',
      data={'secret': settings.TURNSTILE_SECRET_KEY, 'response': token},
      timeout=10,
    )
    result = resp.json()
    if not result.get('success'):
      raise HTTPException(400, '安全验证失败，请重试')


def _validate_captcha(body):
  """返回 None（Pillow 同步校验完成/开发模式跳过）或 Turnstile 异步协程"""
  if settings.SMS_DEV_MODE:
    cap_id = getattr(body, 'captcha_id', '')
    cap_code = getattr(body, 'captcha_code', '')
    tt = getattr(body, 'turnstile_token', '')
    if not cap_id and not cap_code and not tt:
      return None
    if settings.CAPTCHA_PROVIDER == 'pillow' and not cap_id:
      return None
    if settings.CAPTCHA_PROVIDER == 'turnstile' and not tt:
      return None
  if settings.CAPTCHA_PROVIDER == 'turnstile':
    return _verify_turnstile(body.turnstile_token)
  _check_captcha(body.captcha_id, body.captcha_code)
  return None


# ==================== 请求/响应模型 ====================

class LoginRequest(BaseModel):
  phone: str = Field(..., pattern=r'^1[3-9]\d{9}$')
  password: str = Field(..., min_length=1)
  captcha_id: str = ''
  captcha_code: str = ''
  turnstile_token: str = ''


PASSWORD_RE = re.compile(
  r'^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=\[\]{};'
  r"':" r'"\\|,.<>\/?~`]).{8,}$'
)
PASSWORD_MSG = '密码至少8位，必须包含大写字母、小写字母、数字和特殊符号'


class RegisterRequest(BaseModel):
  phone: str = Field(..., pattern=r'^1[3-9]\d{9}$')
  password: str = Field(..., min_length=8, description=PASSWORD_MSG)
  captcha_id: str = ''
  captcha_code: str = ''
  turnstile_token: str = ''

  @field_validator('password')
  @classmethod
  def validate_password(cls, v: str) -> str:
    if not PASSWORD_RE.match(v):
      raise ValueError(PASSWORD_MSG)
    return v


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
  verify = _validate_captcha(body)
  if verify: await verify
  ip = request.client.host if request.client else ''
  try:
    result = await auth_service.login(body.phone, body.password, ip)
    result['user'] = _inject_agent_enabled(result['user'])
    return {
      'ok': True,
      'data': result,
    }
  except ValueError as e:
    raise HTTPException(400, str(e))


class MgmtLoginRequest(BaseModel):
  username: str = Field(..., min_length=1)
  password: str = Field(..., min_length=1)
  captcha_id: str = ''
  captcha_code: str = ''
  turnstile_token: str = ''


@router.post('/mgmt-login')
async def mgmt_login(body: MgmtLoginRequest, request: Request):
  """综合管理平台账号登录（SSO）：mgmt 账号+密码 → 建/合并 rz 用户 → 发 JWT。"""
  if not settings.MGMT_SSO_ENABLED:
    raise HTTPException(403, '综合管理平台登录未开启')
  verify = _validate_captcha(body)
  if verify: await verify
  ip = request.client.host if request.client else ''
  try:
    result = await auth_service.mgmt_login(body.username.strip(), body.password, ip)
    result['user'] = _inject_agent_enabled(result['user'])
    return {'ok': True, 'data': result}
  except (mgmt_http.MgmtError, ValueError) as e:
    raise HTTPException(400, str(e))


@router.post('/register')
async def register(body: RegisterRequest, request: Request):
  verify = _validate_captcha(body)
  if verify: await verify
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
  user_info = _inject_agent_enabled(user_info)
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
  new_password: str = Field(..., min_length=8, description=PASSWORD_MSG)
  captcha_id: str = ''
  captcha_code: str = ''
  turnstile_token: str = ''

  @field_validator('new_password')
  @classmethod
  def validate_password(cls, v: str) -> str:
    if not PASSWORD_RE.match(v):
      raise ValueError(PASSWORD_MSG)
    return v


@router.post('/reset-password')
async def reset_password(body: ResetPasswordRequest):
  """重置密码 — 直接输入手机号和新密码即可"""
  verify = _validate_captcha(body)
  if verify: await verify
  user = await user_service.get_by_phone(body.phone)
  if not user:
    raise HTTPException(400, '该手机号未注册')

  await user_service.reset_password(user['user_id'], body.new_password)
  return {'ok': True, 'message': '密码已重置，请重新登录'}


# ==================== 认证中心（综合管理平台账号绑定） ====================

class BindMgmtRequest(BaseModel):
  username: str = Field(..., min_length=1)
  password: str = Field(..., min_length=1)


@router.get('/mgmt/status')
async def mgmt_status(request: Request):
  """查看当前用户综合管理平台账号绑定状态"""
  user = await user_service.get_by_id(request.state.user_id)
  if not user:
    raise HTTPException(404, '用户不存在')
  username = user.get('mgmt_username', '')
  return {
    'ok': True,
    'data': {'bound': bool(username), 'username_masked': mask_mgmt_username(username)},
  }


@router.post('/mgmt/bind')
async def mgmt_bind(body: BindMgmtRequest, request: Request):
  """绑定综合管理平台账号（先真实登录校验，成功后加密落库）"""
  user_id = request.state.user_id
  username = body.username.strip()
  password = body.password
  if not username or not password:
    raise HTTPException(400, '账号和密码不能为空')

  # 真实校验：调 mgmt 登录接口，失败说明账号/密码错（此时不会缓存 token）
  try:
    await mgmt_http.login_user(user_id, username, password)
  except mgmt_http.MgmtError:
    raise HTTPException(400, '综合管理平台账号或密码错误，请核对后重试')

  try:
    password_enc = encrypt_password(password)
  except Exception:
    raise HTTPException(500, '加密服务未就绪，请联系管理员')

  # 绑定同时回写档案（部门/专业/姓名/公司），与 SSO 登录一致；先取档案再 clear
  updates = user_service.apply_mgmt_profile(await mgmt_http.get_user_profile(user_id))
  updates.update({'mgmt_username': username, 'mgmt_password_enc': password_enc})
  await user_service.update_user(user_id, updates)
  mgmt_http.clear_user_token(user_id)  # 重绑时清旧 token 缓存，强制用新账号

  user = await user_service.get_by_id(user_id)
  await user_service.write_audit(
    user_id, user.get('phone', ''), 'user.mgmt_bind', 'mgmt_account',
    f'绑定综合管理平台账号 {username}', '',
    operator_name=user.get('name', ''),
  )
  return {'ok': True, 'message': '绑定成功'}


@router.post('/mgmt/unbind')
async def mgmt_unbind(request: Request):
  """解绑综合管理平台账号，恢复默认平台账号（手机号 + 默认密码）"""
  user_id = request.state.user_id
  await user_service.update_user(user_id, {'mgmt_username': '', 'mgmt_password_enc': ''})
  mgmt_http.clear_user_token(user_id)
  return {'ok': True, 'message': '已解绑'}

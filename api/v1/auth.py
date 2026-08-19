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
from config.settings import settings
from config.redis_conn import redis_manager

router = APIRouter(prefix='/auth')


def _inject_agent_enabled(user_info: dict) -> dict:
  """agent 是否对当前用户可用：全局开关开启且该用户有 ai:agent 权限。

  前端按此字段分流（走 Agent 还是 RAG），普通用户无 ai:agent 时保持 RAG。
  """
  has_perm = 'ai:agent' in (user_info.get('permissions') or [])
  user_info['agent_enabled'] = settings.AGENT_ENABLED and has_perm
  return user_info


# ==================== Pillow 图形验证码 ====================

def _generate_captcha_image() -> tuple[str, str]:
  """生成数学算式图片，返回 (answer, base64_png)"""
  a = random.randint(1, 30)
  b = random.randint(1, 30)
  op = random.choice(['+', '-'])
  if op == '-':
    a, b = max(a, b), min(a, b)
  answer = str(a + b if op == '+' else a - b)
  text = f'{a} {op} {b} = ?'

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

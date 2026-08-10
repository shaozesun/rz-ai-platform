"""综合管理平台 HTTP 层 —— 登录换 token、token/deptId 缓存、统一请求封装。

身份策略（已与用户确认，见 docs/ 或方案记录）：综合管理平台无服务级 token，只有
`POST /user/login/v1`（账号密码）。用一个最高权限服务账号登录，`deptId` 取登录
响应里该账号自带的部门作为默认上下文——不建 rz User 到 mgmt deptId 的映射表。
折衷：mgmt 侧数据权限对所有 rz 用户一视同仁，仅适合只读查询。

写法参考 service/video/pipeline.py 的 `_call_plugin`（本项目现有最简 httpx 调用
范式：AsyncClient + timeout + header 认证），复杂之处在于这里需要 token 续期
（用 Redis 缓存，参考 core/sms.py 用 redis_manager 的方式）。
"""

import logging

import httpx

from config.redis_conn import redis_manager
from config.settings import settings

logger = logging.getLogger(__name__)

_TOKEN_KEY = 'mgmt:token'
_DEPTID_KEY = 'mgmt:deptid'


class MgmtError(Exception):
  """综合管理平台调用异常。"""


async def _login() -> tuple[str, str]:
  """调用登录接口换取 token + 默认 deptId，写入 Redis 缓存。

  Returns:
    (token, dept_id)

  Raises:
    MgmtError: 登录失败或响应里缺 token。
  """
  if not settings.MGMT_USERNAME or not settings.MGMT_PASSWORD:
    raise MgmtError('未配置 MGMT_USERNAME/MGMT_PASSWORD，无法登录综合管理平台')

  url = f'{settings.MGMT_BASE_URL}/user/login/v1'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.post(url, json={
      'user': settings.MGMT_USERNAME,
      'password': settings.MGMT_PASSWORD,
    })
    resp.raise_for_status()
    body = resp.json()

  if body.get('code') != 0:
    raise MgmtError(f'综合管理平台登录失败: {body.get("msg", "未知错误")}')

  data = body.get('data') or {}
  token = data.get('token')
  if not token:
    raise MgmtError('综合管理平台登录响应缺少 token')

  # deptId 字段名以真实响应为准，登录成功后打日志确认一次（文档未明确 user 对象
  # 结构，联调时核对下方取值是否命中，若字段名不同需调整）。
  user_info = data.get('user') or {}
  dept_id = user_info.get('deptId', '')
  if not dept_id:
    logger.warning(
      'mgmt 登录成功但未取到 deptId，user 字段结构: %s（需核对真实字段名）',
      list(user_info.keys()),
    )

  ttl = settings.MGMT_TOKEN_TTL
  redis_manager.client.setex(redis_manager.key(_TOKEN_KEY), ttl, token)
  redis_manager.client.setex(redis_manager.key(_DEPTID_KEY), ttl, dept_id)
  logger.info('mgmt 登录成功，token 已缓存 ttl=%ds dept_id=%s', ttl, dept_id or '-')
  return token, dept_id


async def _get_token_and_dept() -> tuple[str, str]:
  """取当前有效的 (token, dept_id)，缓存 miss 时触发登录。"""
  if settings.MGMT_API_TOKEN:
    # 若后端将来提供服务级 token，直接用，跳过登录（deptId 仍需登录才能拿到，
    # 此分支下 dept_id 为空，调用方需显式传 dept_id 参数）。
    return settings.MGMT_API_TOKEN, ''

  token = redis_manager.client.get(redis_manager.key(_TOKEN_KEY))
  dept_id = redis_manager.client.get(redis_manager.key(_DEPTID_KEY))
  if token:
    return token, dept_id or ''
  return await _login()


def _is_auth_error(body: dict) -> bool:
  """粗略判断响应是否属于鉴权失效（token 过期/无效），用于触发单次重登重试。

  文档未明确鉴权失败的 code 值，先按「非 0 且 msg 含鉴权相关关键字」判断；
  联调时如发现真实鉴权失败 code 固定，应改成精确匹配 code。
  """
  msg = str(body.get('msg', ''))
  return any(kw in msg for kw in ('token', 'Token', '登录', '未授权', '认证'))


async def request(
  method: str,
  path: str,
  *,
  params: dict | None = None,
  json_body: dict | None = None,
  dept_id: str | None = None,
  _retried: bool = False,
) -> dict:
  """发起一次综合管理平台 HTTP 请求，返回拆开信封后的 `data`。

  Args:
    method: 'GET' / 'POST'。
    path: 接口路径（不含 base url，占位符已在 client.py 里填充完毕）。
    params: query 参数。
    json_body: body 参数（POST 用）。
    dept_id: 显式指定 Deptid，未传则用登录账号的默认部门。
    _retried: 内部标记，鉴权失败重登后重试一次，避免死循环。

  Returns:
    响应体 `data` 字段。

  Raises:
    MgmtError: 非鉴权类业务错误，或重试后仍失败。
  """
  token, default_dept = await _get_token_and_dept()
  headers = {
    'token': token,
    'Deptid': dept_id or default_dept,
  }

  url = f'{settings.MGMT_BASE_URL}{path}'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.request(
      method, url, params=params, json=json_body, headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()

  code = body.get('code')
  if code == 0:
    return body.get('data') or {}

  if not _retried and _is_auth_error(body):
    logger.info('mgmt 请求鉴权失效，清缓存重登一次 path=%s', path)
    redis_manager.client.delete(redis_manager.key(_TOKEN_KEY))
    redis_manager.client.delete(redis_manager.key(_DEPTID_KEY))
    return await request(
      method, path, params=params, json_body=json_body,
      dept_id=dept_id, _retried=True,
    )

  raise MgmtError(f'综合管理平台调用失败 path={path}: {body.get("msg", "未知错误")}')

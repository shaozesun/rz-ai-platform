"""综合管理平台 HTTP 层 —— 登录换 token、token/deptId 缓存、统一请求封装。

身份策略（已与用户确认，见 docs/ 或方案记录）：综合管理平台无服务级 token，只有
`POST /user/login/v1`（账号密码）。用一个最高权限服务账号登录，`deptId` 取登录
响应里该账号自带的部门作为默认上下文——不建 rz User 到 mgmt deptId 的映射表。
折衷：mgmt 侧数据权限对所有 rz 用户一视同仁，仅适合只读查询。

pen 流程服务（MGMT_PEN_BASE_URL，mgmt 能力延伸非新平台）：认证头是
`Authorization: Bearer` 且认 token 归属人。用服务账号 token 调
`/user/dandang/user/token/{kingdeeUid}` 换发指定人员的 token（path 参数区分人），
按人缓存后查询（get_person_token / pen_request）。

写法参考 service/video/pipeline.py 的 `_call_plugin`（本项目现有最简 httpx 调用
范式：AsyncClient + timeout + header 认证），复杂之处在于这里需要 token 续期
（用 Redis 缓存，参考 core/sms.py 用 redis_manager 的方式）。
"""

import json
import logging
from urllib.parse import quote

import httpx

from config.redis_conn import redis_manager
from config.settings import settings

logger = logging.getLogger(__name__)

_TOKEN_KEY = 'mgmt:token'
_DEPTID_KEY = 'mgmt:deptid'
_OWN_DEPTID_KEY = 'mgmt:owndeptid'
_COMPANYID_KEY = 'mgmt:companyid'
_KINGDEEUID_KEY = 'mgmt:kingdeeuid'
_KINGDEE_COMPANY_KEY = 'mgmt:kingdeecompany'
_PEN_TOKEN_PREFIX = 'pen:token:'
_PEN_COMPANY_MAP_KEY = 'pen:companymap'


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

  # 实测确认：真实平台成功码是 code=1（msg="请求成功"），不是 code=0。
  if body.get('code') != 1:
    raise MgmtError(f'综合管理平台登录失败: {body.get("msg", "未知错误")}')

  data = body.get('data') or {}
  token = data.get('token')
  if not token:
    raise MgmtError('综合管理平台登录响应缺少 token')

  # deptId/ownDeptId/companyId/kingdeeUid/kingdeeCompanyId 字段名已对真实响应核对过
  # （孙韶泽账号）。ownDeptId 是 records/v2 等 json 接口需要的父部门上下文，companyId
  # 是 mgmt 内部公司 id；kingdeeUid 是换发人员 token 用的金蝶用户 id；kingdeeCompanyId
  # 是 pen 变更工单接口 companyId 认的格式（sA8...，与内部 companyId 27Wq... 不同）。
  user_info = data.get('user') or {}
  dept_id = user_info.get('deptId', '')
  own_dept_id = user_info.get('ownDeptId', '')
  company_id = user_info.get('companyId', '')
  kingdee_uid = user_info.get('kingdeeUid', '')
  kingdee_company_id = user_info.get('kingdeeCompanyId', '')
  if not dept_id or not own_dept_id:
    logger.warning(
      'mgmt 登录成功但未取到 deptId/ownDeptId，user 字段结构: %s（需核对真实字段名）',
      list(user_info.keys()),
    )

  ttl = settings.MGMT_TOKEN_TTL
  redis_manager.client.setex(redis_manager.key(_TOKEN_KEY), ttl, token)
  redis_manager.client.setex(redis_manager.key(_DEPTID_KEY), ttl, dept_id)
  redis_manager.client.setex(redis_manager.key(_OWN_DEPTID_KEY), ttl, own_dept_id)
  redis_manager.client.setex(redis_manager.key(_COMPANYID_KEY), ttl, company_id)
  if kingdee_uid:
    redis_manager.client.setex(redis_manager.key(_KINGDEEUID_KEY), ttl, kingdee_uid)
  if kingdee_company_id:
    redis_manager.client.setex(redis_manager.key(_KINGDEE_COMPANY_KEY), ttl, kingdee_company_id)
  logger.info(
    'mgmt 登录成功，token 已缓存 ttl=%ds dept_id=%s own_dept_id=%s kingdee_uid=%s',
    ttl, dept_id or '-', own_dept_id or '-', kingdee_uid or '-',
  )
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


async def get_default_context() -> dict:
  """取服务账号默认上下文（own_dept_id/company_id），缓存 miss 时触发登录。

  供 client.py 兜底 records/v2 这类需要部门/公司上下文的 json 接口——
  实测只传 uid 会报「部门不存在/公司不存在」，deptId 必须用父部门 ownDeptId。
  """
  if not redis_manager.client.get(redis_manager.key(_TOKEN_KEY)):
    await _login()
  return {
    'own_dept_id': redis_manager.client.get(redis_manager.key(_OWN_DEPTID_KEY)) or '',
    'company_id': redis_manager.client.get(redis_manager.key(_COMPANYID_KEY)) or '',
  }


async def get_service_kingdee_uid() -> str:
  """取服务账号本人的 kingdeeUid（pen 流程服务换发本人 token 用），缓存 miss 触发登录。"""
  if not redis_manager.client.get(redis_manager.key(_TOKEN_KEY)):
    await _login()
  return redis_manager.client.get(redis_manager.key(_KINGDEEUID_KEY)) or ''


async def get_service_pen_company_id() -> str:
  """取服务账号默认公司在 pen 平台的 kingdeeCompanyId（变更工单 companyId 认的格式）。

  与 mgmt 内部 companyId（27Wq...）不同——pen 的 companyId 是 kingdee 格式
  （sA8...，实测内部格式返回 0 条）。登录响应自带该值；缓存 miss 触发登录
  （本 key 是后加的，旧会话可能只有 token 没有它，故单独检查本 key）。
  """
  if not redis_manager.client.get(redis_manager.key(_KINGDEE_COMPANY_KEY)):
    await _login()
  return redis_manager.client.get(redis_manager.key(_KINGDEE_COMPANY_KEY)) or ''


async def get_person_token(kingdee_uid: str) -> str:
  """用服务账号授权，按 kingdeeUid 换发指定人员的 token（按人缓存）。

  换发接口 GET /user/dandang/user/token/{kingdeeUserId}：header 带服务账号 token
  只是「签发授权」，path 里的 kingdeeUserId 才是人员区分点——不同 id 换出不同
  人的 token（已实测对照：本人 id vs 别人 id 换出的 token 不同，pen 查询各归各人）。
  """
  if not kingdee_uid:
    raise MgmtError('get_person_token 缺 kingdee_uid')
  cache_key = redis_manager.key(f'{_PEN_TOKEN_PREFIX}{kingdee_uid}')
  cached = redis_manager.client.get(cache_key)
  if cached:
    return cached

  token, _dept = await _get_token_and_dept()
  url = f'{settings.MGMT_BASE_URL}/user/dandang/user/token/{quote(str(kingdee_uid), safe="")}'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.get(url, headers={'token': token})
    resp.raise_for_status()
    body = resp.json()

  if body.get('code') != 1:
    raise MgmtError(f'换取人员 token 失败 uid={kingdee_uid}: {body.get("msg", "未知错误")}')
  person_token = body.get('data')
  if not person_token or not isinstance(person_token, str):
    raise MgmtError('换取人员 token 响应 data 应为字符串 token，实际: '
                    f'{type(person_token).__name__}（需核对真实返回结构）')

  redis_manager.client.setex(cache_key, settings.MGMT_PEN_TOKEN_TTL, person_token)
  logger.info('pen 人员 token 已换发并缓存 uid=%s ttl=%ds', kingdee_uid, settings.MGMT_PEN_TOKEN_TTL)
  return person_token


def _clear_person_token(kingdee_uid: str) -> None:
  if kingdee_uid:
    redis_manager.client.delete(redis_manager.key(f'{_PEN_TOKEN_PREFIX}{kingdee_uid}'))


async def pen_request(
  path: str,
  params: dict | None = None,
  json_body: dict | None = None,
  kingdee_uid: str | None = None,
  _retried: bool = False,
) -> dict:
  """发起一次 pen 流程服务请求，`Authorization: Bearer` 携带**指定人员**的 token。

  与 mgmt 请求（`token` + `Deptid` 头）不同——pen 认证头是 Bearer，且认的是
  「token 归属人」（即 kingdee_uid 那个人）的数据（待办按人，变更工单按公司，
  token 只做鉴权）。kingdee_uid 为空时用服务账号本人 token。

  Args:
    path: pen 接口路径。
    params: GET query 参数。
    json_body: POST body（传了就走 POST，否则 GET）。待办查询用 GET，变更工单
      列表用 POST body。
    kingdee_uid: 指定人员；空用服务账号本人。
    _retried: 内部标记，鉴权失败清缓存重试一次，避免死循环。

  Returns:
    响应体 `data` 字段（pen 信封如 {records,total,current,size,pages} 或
    {simpleTicketVOList,total,...}）。

  Raises:
    MgmtError: 非鉴权类业务错误，或重试后仍失败；未配置 MGMT_PEN_BASE_URL。
  """
  if not settings.MGMT_PEN_BASE_URL:
    raise MgmtError('未配置 MGMT_PEN_BASE_URL，无法调用 pen 流程服务')
  if not kingdee_uid:
    kingdee_uid = await get_service_kingdee_uid()
  if not kingdee_uid:
    raise MgmtError('pen 请求缺 kingdee_uid 且服务账号本人 kingdeeUid 未取到')

  bearer = await get_person_token(kingdee_uid)
  method = 'POST' if json_body is not None else 'GET'
  url = f'{settings.MGMT_PEN_BASE_URL}{path}'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.request(
      method, url, params=params, json=json_body,
      headers={'Authorization': f'Bearer {bearer}'},
    )
    resp.raise_for_status()
    body = resp.json()

  code = body.get('code')
  if code == 1:
    return body.get('data') or {}

  if not _retried and _is_auth_error(body):
    logger.info('pen 请求鉴权失效，清该人 token 缓存重试一次 uid=%s path=%s', kingdee_uid, path)
    _clear_person_token(kingdee_uid)
    return await pen_request(path, params, json_body, kingdee_uid, _retried=True)

  raise MgmtError(f'pen 流程服务调用失败 path={path}: {body.get("msg", "未知错误")}')


async def get_pen_company_kingdee_id(internal_company_id: str) -> str:
  """把 mgmt 内部 companyId 映射为 pen 平台的 kingdeeCompanyId（跨公司查变更工单用）。

  pen 的变更工单接口 companyId 只认 kingdee 格式（sA8...，实测内部格式 27Wq...
  返回 0 条），而 query_company_list 返回的是内部 id——映射表来自
  `GET /pen/pending/list/company`（Bearer 服务账号 token），data 为
  [{companyId 内部, companyName, kingdee}, ...]，缓存 Redis `pen:companymap`
  （JSON）。查不到返回空串（调用方抛错提示公司不可见）。
  """
  if not internal_company_id:
    return ''
  key = redis_manager.key(_PEN_COMPANY_MAP_KEY)
  raw = redis_manager.client.get(key)
  if not raw:
    data = await pen_request(
      '/pen/pending/list/company', kingdee_uid=await get_service_kingdee_uid(),
    )
    mapping = {
      comp.get('companyId'): comp.get('kingdee')
      for comp in data or []
      if comp.get('companyId')
    }
    redis_manager.client.setex(key, settings.MGMT_PEN_TOKEN_TTL, json.dumps(mapping))
    raw = redis_manager.client.get(key)
  try:
    return json.loads(raw or '{}').get(internal_company_id, '') or ''
  except (TypeError, ValueError):
    return ''


def _is_auth_error(body: dict) -> bool:
  """判断响应是否属于鉴权失效（token 过期/无效），用于触发单次重登重试。

  实测：巡检接口未带有效 token 时返回 code=401、msg="用户未登录或登录失效"，
  故这里按 code=401 精确判断（保险起见再扫一遍 msg 里的鉴权关键字）。
  """
  if body.get('code') == 401:
    return True
  msg = str(body.get('msg', ''))
  return any(kw in msg for kw in ('token', 'Token', '登录', '未授权', '认证'))


async def request(
  method: str,
  path: str,
  *,
  params: dict | None = None,
  json_body: dict | None = None,
  dept_id: str | None = None,
  base_url: str | None = None,
  _retried: bool = False,
) -> dict:
  """发起一次综合管理平台 HTTP 请求，返回拆开信封后的 `data`。

  Args:
    method: 'GET' / 'POST'。
    path: 接口路径（不含 base url，占位符已在 client.py 里填充完毕）。
    params: query 参数。
    json_body: body 参数（POST 用）。
    dept_id: 显式指定 Deptid，未传则用登录账号的默认部门。
    base_url: 覆盖 base URL（如巡检服务在根路径 /inspection）；None 用
      settings.MGMT_BASE_URL。
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

  url = f'{base_url or settings.MGMT_BASE_URL}{path}'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.request(
      method, url, params=params, json=json_body, headers=headers,
    )
    resp.raise_for_status()
    body = resp.json()

  code = body.get('code')
  if code == 1:  # 实测确认：真实平台成功码是 1（msg="请求成功"），非 0
    return body.get('data') or {}

  if not _retried and _is_auth_error(body):
    logger.info('mgmt 请求鉴权失效，清缓存重登一次 path=%s', path)
    redis_manager.client.delete(redis_manager.key(_TOKEN_KEY))
    redis_manager.client.delete(redis_manager.key(_DEPTID_KEY))
    return await request(
      method, path, params=params, json_body=json_body,
      dept_id=dept_id, base_url=base_url, _retried=True,
    )

  raise MgmtError(f'综合管理平台调用失败 path={path}: {body.get("msg", "未知错误")}')

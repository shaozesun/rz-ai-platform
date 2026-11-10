"""综合管理平台 HTTP 层 —— 登录换 token、token/deptId 缓存、统一请求封装。

身份策略（认证中心）：综合管理平台无服务级 token，只有 `POST /user/login/v1`
（账号密码）。每个 rz 用户绑定自己的 mgmt 账号密码（Fernet 加密落库，见
core/mgmt_credentials.py）后，Agent 查询 mgmt/pen 接口用**该用户自己的 token**
身份（`mgmt:token:{uid}` 按 user_id 缓存），mgmt 侧数据权限按人生效；未绑定用户
默认用「rz 手机号 + MGMT_DEFAULT_PASSWORD」登录。用户通道（auth_user_id）优先，
服务账号仅在非用户通道（user_id 为空）兜底。

工单服务（MGMT_TICKET_BASE_URL = `https://ticket-api.ddbes.com`，mgmt 能力延伸
非新平台）：**2026-10-26 对方整体重做后**，工单接口全走 `ticket_request()`，认证
与 mgmt 同一套 `token` + `Deptid` 头，路径去掉老的 `/ticket`、`/pen` 前缀
（`/wiporder/*`、`/pending/list/v1`、`/company/user/{uid}`），入参与信封不变。
老的 pen 流程服务（MGMT_PEN_BASE_URL，`Authorization: Bearer` + 用 mgmt token 换发
人员 token `/user/dandang/user/token/{kingdeeUid}`）的实现保留在
`get_person_token / pen_request`，但**已不在主链路上**。

写法参考 service/video/pipeline.py 的 `_call_plugin`（本项目现有最简 httpx 调用
范式：AsyncClient + timeout + header 认证），复杂之处在于这里需要 token 续期
（用 Redis 缓存，参考 core/sms.py 用 redis_manager 的方式）。
"""

import json
import logging
import re
from urllib.parse import quote

import httpx

from config.redis_conn import redis_manager
from config.settings import settings
from core.mgmt_credentials import decrypt_password
from service.auth.user_service import user_service

logger = logging.getLogger(__name__)

_TOKEN_KEY = 'mgmt:token'
_DEPTID_KEY = 'mgmt:deptid'
_OWN_DEPTID_KEY = 'mgmt:owndeptid'
_COMPANYID_KEY = 'mgmt:companyid'
_KINGDEEUID_KEY = 'mgmt:kingdeeuid'
_KINGDEE_COMPANY_KEY = 'mgmt:kingdeecompany'
_PEN_TOKEN_PREFIX = 'pen:token:'
# pen 的 kingdeeCompanyId 格式（sA8 + 12 位）；园区预设传的就是这个格式
_KINGDEE_RE = re.compile(r'^sA8[A-Za-z0-9]{8,}$')
_USERINFO_PREFIX = 'mgmt:userinfo:'
_REF_PREFIX = 'mgmt:ref:'   # 静态参考数据缓存前缀（公司/部门/pen 公司部门）


def cache_get_json(key: str):
  """读取 Redis JSON 缓存；无或解析失败返回 None。"""
  raw = redis_manager.client.get(key)
  if not raw:
    return None
  try:
    return json.loads(raw)
  except (TypeError, ValueError):
    return None


def cache_set_json(key: str, value, ttl: int) -> None:
  """写入 Redis JSON 缓存。"""
  redis_manager.client.setex(key, ttl, json.dumps(value, ensure_ascii=False))


def ref_cache_key(capability_id: str, scope: str, params: dict | None = None) -> str:
  """静态参考数据缓存 key（能力 + 用户/服务账号 + 参数）。"""
  suffix = json.dumps(params or {}, sort_keys=True, ensure_ascii=False)
  return redis_manager.key(f'{_REF_PREFIX}{capability_id}:{scope}:{suffix}')

# 用户 mgmt 认证失败时的引导文案（默认账号密码无效/已绑定密码被改时返回给用户）
_BIND_PROMPT = (
  '综合管理平台账号认证失败（默认账号或已绑定账号密码无效，可能已修改密码）。'
  '请前往【认证中心】绑定您的 mgmt 账号密码授权'
)


class MgmtError(Exception):
  """综合管理平台调用异常。"""


def _user_ctx_keys(user_id: str) -> tuple[str, str, str, str, str, str]:
  """按用户 id 生成专属的 6 个 Redis key（token/dept/own_dept/company/kingdee...）。"""
  suffix = f':{user_id}'
  return (
    f'{_TOKEN_KEY}{suffix}', f'{_DEPTID_KEY}{suffix}', f'{_OWN_DEPTID_KEY}{suffix}',
    f'{_COMPANYID_KEY}{suffix}', f'{_KINGDEEUID_KEY}{suffix}', f'{_KINGDEE_COMPANY_KEY}{suffix}',
  )


def _cache_context(keys: tuple[str, str, str, str, str, str], ctx: dict) -> None:
  """把登录返回的账号上下文写入指定的一组 Redis key。"""
  token_key, dept_key, own_key, company_key, kingdee_key, kingdee_company_key = keys
  ttl = settings.MGMT_TOKEN_TTL
  redis_manager.client.setex(redis_manager.key(token_key), ttl, ctx['token'])
  redis_manager.client.setex(redis_manager.key(dept_key), ttl, ctx['dept_id'])
  redis_manager.client.setex(redis_manager.key(own_key), ttl, ctx['own_dept_id'])
  redis_manager.client.setex(redis_manager.key(company_key), ttl, ctx['company_id'])
  if ctx['kingdee_uid']:
    redis_manager.client.setex(redis_manager.key(kingdee_key), ttl, ctx['kingdee_uid'])
  if ctx['kingdee_company_id']:
    redis_manager.client.setex(redis_manager.key(kingdee_company_key), ttl, ctx['kingdee_company_id'])


# 登录失败原因上游是英文（实测 "wrong password"），直接透出会在登录页显示成中英混排。
# 已知文案给中文对照；认不出的英文按「账号或密码错误」兜底（原始文案进日志便于排查），
# 已是中文的原样返回。
_LOGIN_FAIL_CN = {
  'wrong password': '账号或密码错误',
  'invalid password': '账号或密码错误',
  'incorrect password': '账号或密码错误',
  'user not exist': '账号不存在',
  'user not exists': '账号不存在',
  'user not found': '账号不存在',
  'account not exist': '账号不存在',
  'account locked': '账号已被锁定',
  'account disabled': '账号已被禁用',
}


def _login_fail_text(msg: str) -> str:
  """把登录接口返回的 msg 转成中文口径。"""
  text = (msg or '').strip()
  if not text:
    return '未知错误'
  mapped = _LOGIN_FAIL_CN.get(text.lower())
  if mapped:
    return mapped
  if any('\u4e00' <= ch <= '\u9fff' for ch in text):
    return text
  logger.info('综合管理平台登录失败（未登记的上游文案）: %s', text)
  return '账号或密码错误'


async def _login_with_credentials(username: str, password: str) -> dict:
  """调用登录接口，返回解析后的账号上下文（token/dept/own_dept/company/kingdee...）。"""
  if not username or not password:
    raise MgmtError('综合管理平台账号或密码为空，无法登录')

  url = f'{settings.MGMT_BASE_URL}/user/login/v1'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.post(url, json={'user': username, 'password': password})
    resp.raise_for_status()
    body = resp.json()

  # 实测确认：真实平台成功码是 code=1（msg="请求成功"），不是 code=0。
  if body.get('code') != 1:
    raise MgmtError(f'综合管理平台登录失败：{_login_fail_text(body.get("msg", ""))}')

  data = body.get('data') or {}
  token = data.get('token')
  if not token:
    raise MgmtError('综合管理平台登录响应缺少 token')

  # deptId/ownDeptId/companyId/kingdeeUid/kingdeeCompanyId 字段名已对真实响应核对过
  # （孙韶泽账号）。ownDeptId 是 records/v2 等 json 接口需要的父部门上下文，companyId
  # 是 mgmt 内部公司 id；kingdeeUid 是换发人员 token 用的金蝶用户 id；kingdeeCompanyId
  # 是 pen 变更工单接口 companyId 认的格式（sA8...，与内部 companyId 27Wq... 不同）。
  user_info = data.get('user') or {}
  ctx = {
    'token': token,
    'dept_id': user_info.get('deptId', ''),
    'own_dept_id': user_info.get('ownDeptId', ''),
    'company_id': user_info.get('companyId', ''),
    'kingdee_uid': user_info.get('kingdeeUid', ''),
    'kingdee_company_id': user_info.get('kingdeeCompanyId', ''),
    # 用户档案（接口文档登记字段，供 query_my_info 解析「我」的身份）
    'uid': user_info.get('uid', ''),
    'name': user_info.get('name', ''),
    'work_number': user_info.get('workNumber', ''),
    'occupation': user_info.get('occupation', ''),
    'dept_name': user_info.get('deptName', ''),
    'own_dept_name': user_info.get('ownDeptName', ''),
    'company_name': user_info.get('companyName', ''),
  }
  if not ctx['dept_id'] or not ctx['own_dept_id']:
    logger.warning(
      'mgmt 登录成功但未取到 deptId/ownDeptId，user 字段结构: %s（需核对真实字段名）',
      list(user_info.keys()),
    )
  return ctx


async def _login() -> tuple[str, str]:
  """服务账号登录换取 token + 默认 deptId，写入 Redis 缓存。

  Returns:
    (token, dept_id)

  Raises:
    MgmtError: 登录失败或响应里缺 token。
  """
  if not settings.MGMT_USERNAME or not settings.MGMT_PASSWORD:
    raise MgmtError('未配置 MGMT_USERNAME/MGMT_PASSWORD，无法登录综合管理平台')

  ctx = await _login_with_credentials(settings.MGMT_USERNAME, settings.MGMT_PASSWORD)
  _cache_context((_TOKEN_KEY, _DEPTID_KEY, _OWN_DEPTID_KEY, _COMPANYID_KEY,
                  _KINGDEEUID_KEY, _KINGDEE_COMPANY_KEY), ctx)
  logger.info(
    'mgmt 登录成功，token 已缓存 ttl=%ds dept_id=%s own_dept_id=%s kingdee_uid=%s',
    settings.MGMT_TOKEN_TTL, ctx['dept_id'] or '-', ctx['own_dept_id'] or '-',
    ctx['kingdee_uid'] or '-',
  )
  return ctx['token'], ctx['dept_id']


async def login_user(user_id: str, username: str, password: str) -> tuple[str, str]:
  """指定用户登录 mgmt，token/上下文按 user_id 缓存（认证中心绑定校验也用）。

  Returns:
    (token, dept_id)

  Raises:
    MgmtError: 登录失败或响应里缺 token。
  """
  ctx = await _login_with_credentials(username, password)
  _cache_context(_user_ctx_keys(user_id), ctx)
  # 用户档案（去 token）JSON 落缓存，供 query_my_info 解析「我」的身份
  profile = {k: v for k, v in ctx.items() if k != 'token'}
  redis_manager.client.setex(
    redis_manager.key(f'{_USERINFO_PREFIX}{user_id}'),
    settings.MGMT_TOKEN_TTL, json.dumps(profile, ensure_ascii=False),
  )
  logger.info('mgmt 用户登录成功 user_id=%s account=%s dept_id=%s',
              user_id, username, ctx['dept_id'] or '-')
  return ctx['token'], ctx['dept_id']


async def cache_user_login(user_id: str, ctx: dict) -> None:
  """把一次已完成的登录上下文写入用户 Redis 缓存（免二次登录）。

  SSO 登录已通过 _login_with_credentials 拿到 ctx，直接缓存即可，避免再调一次
  mgmt 登录接口。
  """
  _cache_context(_user_ctx_keys(user_id), ctx)
  profile = {k: v for k, v in ctx.items() if k != 'token'}
  redis_manager.client.setex(
    redis_manager.key(f'{_USERINFO_PREFIX}{user_id}'),
    settings.MGMT_TOKEN_TTL, json.dumps(profile, ensure_ascii=False),
  )


async def fetch_mgmt_profile(phone: str) -> dict:
  """用默认密码拉取指定手机号在综合管理平台的档案（部门/专业/职位等）。

  仅用于**注册时一次性档案同步**（Path B）：大部分账号密码还是默认 666666，
  登录成功即视为该账号可用，返回去敏档案；默认密码登录失败（用户已改密码）或
  平台无此账号/网络异常时**静默返回空 dict，不抛错**，不影响注册流程。
  """
  try:
    ctx = await _login_with_credentials(phone, settings.MGMT_DEFAULT_PASSWORD)
  except Exception:
    return {}
  return {
    'name': ctx.get('name', ''),
    'company_name': ctx.get('company_name', ''),
    'dept_name': ctx.get('own_dept_name') or ctx.get('dept_name', ''),
    'mgmt_uid': ctx.get('uid', ''),
  }


async def get_user_profile(user_id: str) -> dict:
  """取指定用户登录缓存的个人档案（name/dept/company 等，不含 token）。

  档案缓存 miss（如本次新增 key 前的旧会话只有 token 无档案）时清缓存强制重登
  补齐；登录失败会抛 MgmtError（引导去认证中心绑定）。
  """
  key = redis_manager.key(f'{_USERINFO_PREFIX}{user_id}')
  raw = redis_manager.client.get(key)
  if not raw:
    # token 命中也会跳过登录，故这里清掉用户缓存强制重登，登录时一并写档案
    clear_user_token(user_id)
    await get_user_token_and_dept(user_id)
    raw = redis_manager.client.get(key)
  if not raw:
    return {}
  try:
    return json.loads(raw)
  except (TypeError, ValueError):
    return {}


async def get_user_token_and_dept(user_id: str) -> tuple[str, str]:
  """取指定用户的有效 (token, dept_id)；缓存 miss 时用绑定账号或默认账号登录。

  绑定用户用解密后的 mgmt 账号密码；未绑定用户用「手机号 + MGMT_DEFAULT_PASSWORD」
  兜底（默认平台账号）。登录/解密失败抛 MgmtError（引导用户去认证中心绑定）。
  """
  token_key, dept_key = _user_ctx_keys(user_id)[:2]
  token = redis_manager.client.get(redis_manager.key(token_key))
  dept_id = redis_manager.client.get(redis_manager.key(dept_key))
  if token:
    return token, dept_id or ''

  user = await user_service.get_by_id(user_id)
  if not user:
    raise MgmtError(_BIND_PROMPT)
  username = user.get('mgmt_username', '')
  password_enc = user.get('mgmt_password_enc', '')
  if username and password_enc:
    try:
      password = decrypt_password(password_enc)
    except Exception:
      logger.warning('解密用户 mgmt 密码失败 user_id=%s', user_id)
      raise MgmtError(_BIND_PROMPT) from None
  else:
    username = user.get('phone', '')
    password = settings.MGMT_DEFAULT_PASSWORD

  try:
    return await login_user(user_id, username, password)
  except MgmtError as e:
    logger.warning('用户 mgmt 登录失败 user_id=%s account=%s: %s', user_id, username, e)
    raise MgmtError(_BIND_PROMPT) from e


async def get_user_context(user_id: str) -> dict:
  """取指定用户的 {own_dept_id, company_id}（供 spec.defaults 兜底），缓存 miss 触发登录。"""
  if not user_id:
    return {'own_dept_id': '', 'company_id': ''}
  token_key, _dept_key, own_key, company_key = _user_ctx_keys(user_id)[:4]
  if not redis_manager.client.get(redis_manager.key(token_key)):
    await get_user_token_and_dept(user_id)
  return {
    'own_dept_id': redis_manager.client.get(redis_manager.key(own_key)) or '',
    'company_id': redis_manager.client.get(redis_manager.key(company_key)) or '',
  }


def clear_user_token(user_id: str) -> None:
  """清除指定用户的 mgmt token/上下文缓存（解绑/重绑时调用）。"""
  if not user_id:
    return
  for key in _user_ctx_keys(user_id):
    redis_manager.client.delete(redis_manager.key(key))
  redis_manager.client.delete(redis_manager.key(f'{_USERINFO_PREFIX}{user_id}'))
  # 清静态参考缓存（公司/部门/pen 公司部门，key 含 user_id）
  redis_manager.client.delete(redis_manager.key(f'{_REF_PREFIX}pen_company_dept:{user_id}'))
  for key in redis_manager.client.scan_iter(
    match=redis_manager.key(f'{_REF_PREFIX}*:{user_id}:*'),
  ):
    redis_manager.client.delete(key)


async def get_user_kingdee_uid(user_id: str) -> str:
  """取指定用户本人的 kingdeeUid（pen 换发本人 token 用），缓存 miss 触发登录。"""
  if not user_id:
    return ''
  token_key, _dept_key, _own_key, _company_key, kingdee_key, _c_key = _user_ctx_keys(user_id)
  if not redis_manager.client.get(redis_manager.key(token_key)):
    await get_user_token_and_dept(user_id)
  return redis_manager.client.get(redis_manager.key(kingdee_key)) or ''


async def get_user_pen_company_id(user_id: str) -> str:
  """取指定用户默认公司在 pen 平台的 kingdeeCompanyId（变更工单 companyId 认的格式）。"""
  if not user_id:
    return ''
  token_key, _dept_key, _own_key, _company_key, _kingdee_key, c_key = _user_ctx_keys(user_id)
  if not redis_manager.client.get(redis_manager.key(token_key)):
    await get_user_token_and_dept(user_id)
  return redis_manager.client.get(redis_manager.key(c_key)) or ''


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


async def get_person_token(kingdee_uid: str, *, auth_user_id: str | None = None) -> str:
  """按 kingdeeUid 换发指定人员的 token（按人缓存）。

  换发接口 GET /user/dandang/user/token/{kingdeeUserId}：header 带一个有效 mgmt
  token 只是「签发授权」，path 里的 kingdeeUserId 才是人员区分点——不同 id 换出
  不同人的 token（已实测对照：本人 id vs 别人 id 换出的 token 不同，pen 查询各归
  各人）。`auth_user_id` 有值时用该用户自己的 mgmt token 授权（认证中心，不再走
  服务账号），否则用服务账号 token 授权（非用户通道兜底）。
  """
  if not kingdee_uid:
    raise MgmtError('get_person_token 缺 kingdee_uid')
  # 缓存 key 带授权人（auth_user_id），避免跨用户串用他人换发的 token（越权）
  suffix = f':{auth_user_id}' if auth_user_id else ':service'
  cache_key = redis_manager.key(f'{_PEN_TOKEN_PREFIX}{kingdee_uid}{suffix}')
  cached = redis_manager.client.get(cache_key)
  if cached:
    return cached

  if auth_user_id:
    token, _dept = await get_user_token_and_dept(auth_user_id)
  else:
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


def _clear_person_token(kingdee_uid: str, auth_user_id: str | None = None) -> None:
  if kingdee_uid:
    suffix = f':{auth_user_id}' if auth_user_id else ':service'
    redis_manager.client.delete(redis_manager.key(f'{_PEN_TOKEN_PREFIX}{kingdee_uid}{suffix}'))


async def pen_request(
  path: str,
  params: dict | None = None,
  json_body: dict | None = None,
  kingdee_uid: str | None = None,
  *,
  auth_user_id: str | None = None,
  _retried: bool = False,
) -> dict:
  """发起一次 pen 流程服务请求，`Authorization: Bearer` 携带**指定人员**的 token。

  与 mgmt 请求（`token` + `Deptid` 头）不同——pen 认证头是 Bearer，且认的是
  「token 归属人」（即 kingdee_uid 那个人）的数据（待办按人，变更工单按公司，
  token 只做鉴权）。kingdee_uid 为空时：auth_user_id 有值用该用户本人，否则服务
  账号本人。

  Args:
    path: pen 接口路径。
    params: GET query 参数。
    json_body: POST body（传了就走 POST，否则 GET）。待办查询用 GET，变更工单
      列表用 POST body。
    kingdee_uid: 指定人员；空按 auth_user_id / 服务账号本人解析。
    auth_user_id: 认证中心用户 id；换发人员 token 用该用户自己的 mgmt token 授权。
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
    kingdee_uid = (
      await get_user_kingdee_uid(auth_user_id) if auth_user_id
      else await get_service_kingdee_uid()
    )
  if not kingdee_uid:
    raise MgmtError('pen 请求缺 kingdee_uid 且该用户/服务账号本人 kingdeeUid 未取到')

  bearer = await get_person_token(kingdee_uid, auth_user_id=auth_user_id)
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
    _clear_person_token(kingdee_uid, auth_user_id=auth_user_id)
    return await pen_request(
      path, params, json_body, kingdee_uid,
      auth_user_id=auth_user_id, _retried=True,
    )

  raise MgmtError(f'pen 流程服务调用失败 path={path}: {body.get("msg", "未知错误")}')


async def company_registry(auth_user_id: str | None = None) -> list[dict]:
  """公司档案列表 `[{companyId 内部, companyName, kingdee, gardenName}]`。

  唯一来源＝文档内的 `mgmt.query_company_list`（GET /authority/userrole/v1/current/company，
  一次给全账号可见公司），经 `client.call()` 复用 `_STATIC_REF_CACHE`
  （TTL `settings.MGMT_REF_CACHE_TTL`）。

  **不要回头调 pen 的 `/pen/pending/list/company`**：那是未登记的逆向接口，2026-10-25
  pen 全局故障（所有鉴权后请求返回 code:-1）时它一挂，全国口径取数与园区公司的
  kingdee 映射同时失效 —— 24 份预设报告在 129~529ms 内全部失败。公司清单是低频参考
  数据，与 pen 健康与否无关；mgmt 与 pen 是两个不同后端，查清单不必绕道 pen。
  """
  from service.query.mgmt.client import call   # 延迟导入：client 依赖本模块

  data = await call('mgmt.query_company_list', {}, on_behalf_of=auth_user_id)
  rows = data if isinstance(data, list) else (data or {}).get('records') or []
  out: list[dict] = []
  for comp in rows:
    if not isinstance(comp, dict):
      continue
    out.append({
      'companyId': str(comp.get('companyId') or ''),
      'companyName': str(comp.get('companyName') or ''),
      'kingdee': str(comp.get('kingdeeCompanyId') or comp.get('kingdee') or ''),
      'gardenName': str(comp.get('gardenName') or ''),
    })
  return out


async def get_pen_company_kingdee_id(
  internal_company_id: str, *, auth_user_id: str | None = None,
) -> str:
  """把 mgmt 内部 companyId（或公司名）映射为 pen 平台的 kingdeeCompanyId。

  pen 的工单接口 companyId 只认 kingdee 格式（sA8...，实测内部格式 27Wq... 返回 0 条）。
  映射来自 `company_registry()`（mgmt.query_company_list）。

  **入参已是 kingdee 则原样返回、不查映射**：园区预设传的正是 kingdee，此前仍要先拉一次
  公司清单才算「原样返回」，清单接口一挂 40 份园区报告的取数就跟着全灭。查不到返回空串
  （调用方抛错提示公司不可见）。
  """
  if not internal_company_id:
    return ''
  company = str(internal_company_id).strip()
  if _KINGDEE_RE.match(company):
    return company
  for comp in await company_registry(auth_user_id):
    kid = comp['kingdee']
    if kid and company in (comp['companyId'], comp['companyName'], kid):
      return kid
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
  user_id: str | None = None,
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
    user_id: 指定按哪个用户身份查询（认证中心）；空则用服务账号 token。
    _retried: 内部标记，鉴权失败重登后重试一次，避免死循环。

  Returns:
    响应体 `data` 字段。

  Raises:
    MgmtError: 非鉴权类业务错误，或重试后仍失败。
  """
  if user_id:
    token, default_dept = await get_user_token_and_dept(user_id)
  else:
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
    if user_id:
      clear_user_token(user_id)
    else:
      redis_manager.client.delete(redis_manager.key(_TOKEN_KEY))
      redis_manager.client.delete(redis_manager.key(_DEPTID_KEY))
    return await request(
      method, path, params=params, json_body=json_body,
      dept_id=dept_id, base_url=base_url, user_id=user_id, _retried=True,
    )

  # 实测：GET /user/info/v2/{name} 查无此人时平台返回 code=0、msg=「服务器错误」，
  # 而 Web 人员页 GET /user/dept/v4/page 无人时是 code=1、users=[]。Agent 用 v2，
  # 此处归一为空列表，避免 tools 层抛 MgmtError、模型误报「平台故障」。
  if (
    method == 'GET'
    and path.startswith('/user/info/v2/')
    and code != 1
    and str(body.get('msg', '')).strip() == '服务器错误'
  ):
    logger.info('mgmt 按姓名查人无匹配，归一为空列表 path=%s', path)
    return []

  raise MgmtError(f'综合管理平台调用失败 path={path}: {body.get("msg", "未知错误")}')


async def ticket_request(
  path: str,
  params: dict | None = None,
  json_body: dict | None = None,
  *,
  auth_user_id: str | None = None,
) -> dict:
  """调用工单服务（ticket-api），返回拆包后的 `data`。

  2026-10-26 对方把工单接口整体从 `api.ddbes.com` 迁到 `ticket-api.ddbes.com`：
  - 路径去掉 `/ticket` 前缀：`/ticket/wiporder/*` → `/wiporder/*`；
    `/pen/pending/list/v1` → `/pending/list/v1`；`/ticket/company/user/{uid}` → `/company/user/{uid}`
  - 认证改为 mgmt 同一套 `token` + `Deptid` 头（不再认 pen 的 Bearer 人员 token）
  - 入参与返回信封（`{code:1, data:{simpleTicketVOList,total}}`）不变

  直接复用 `request()`：它已带 401 清缓存重登一次，且 `_is_auth_error` 能识别
  新服务的「未登录或登录已失效」。
  """
  if not settings.MGMT_TICKET_BASE_URL:
    raise MgmtError('未配置 MGMT_TICKET_BASE_URL，无法调用工单服务')
  return await request(
    'POST' if json_body is not None else 'GET', path,
    params=params, json_body=json_body,
    base_url=settings.MGMT_TICKET_BASE_URL, user_id=auth_user_id,
  )

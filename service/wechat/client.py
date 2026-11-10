"""个人微信 iLink HTTP client 单例 + 长轮询 + 多 worker 选主。

iLink 是 HTTP/JSON 长轮询协议（`POST /ilink/bot/getupdates`，服务端 hold ≈35s），
无 SDK、无需公网回调。**`get_updates_buf` 游标是单消费者**：多 worker 各自长轮询会
重复消费同一批消息，故用 Redis 选主锁（`rz:wechat:leader`），只有 leader 起轮询，
其余周期重试以便 leader 崩溃后接管。游标持久化到 Redis（`rz:wechat:cursor`）。

协议要点（对齐 DeerFlow 实测）：响应信封键是 `ret`（0/None=成功），消息列表键是
`msgs`，游标键是 `get_updates_buf`。鉴权头 `Authorization: Bearer {bot_token}` +
`AuthorizationType: ilink_bot_token` + `iLink-App-ClientVersion`（打包整数）+
每次随机 `X-WECHAT-UIN`（base64）。`errcode == -14` 表示 token 过期，需重新扫码，
此时清凭据并停止轮询。
"""

import asyncio
import base64
import logging
import secrets
from typing import Optional

import httpx

from config.settings import settings
from config.redis_conn import redis_manager
from service.channels.leader import LeaderLock
from service.wechat import state

logger = logging.getLogger(__name__)

_CURSOR_KEY = 'wechat:cursor'

_client: Optional[httpx.AsyncClient] = None
_leader_lock: Optional[LeaderLock] = None
_leader_task: Optional[asyncio.Task] = None
_poll_task: Optional[asyncio.Task] = None
_running = False

_bot_token = ''
_ilink_bot_id = ''
_base_url = ''


# ==================== 请求头 / base_info ====================

def client_version_header() -> str:
  """`iLink-App-ClientVersion`：把 `major.minor.patch` 打包成整数。"""
  parts = [p.strip() for p in str(settings.WECHAT_CHANNEL_VERSION).split('.')]

  def _part(i: int) -> int:
    if i >= len(parts):
      return 0
    try:
      return max(0, min(int(parts[i] or 0), 0xFF))
    except ValueError:
      return 0

  return str((_part(0) << 16) | (_part(1) << 8) | _part(2))


def wechat_uin() -> str:
  """`X-WECHAT-UIN`：每次随机的 32 位整数做 base64。"""
  return base64.b64encode(str(secrets.randbits(32)).encode('utf-8')).decode('utf-8')


def _common_headers() -> dict:
  return {
    'Content-Type': 'application/json',
    'iLink-App-ClientVersion': client_version_header(),
    'X-WECHAT-UIN': wechat_uin(),
  }


def public_headers() -> dict:
  """公开接口（取二维码/查状态）用的头，无鉴权。"""
  return _common_headers()


def auth_headers() -> dict:
  """业务接口（getupdates/sendmessage）用的头，带 bot_token。"""
  return {
    **_common_headers(),
    'Authorization': f'Bearer {_bot_token}',
    'AuthorizationType': 'ilink_bot_token',
  }


def base_info() -> dict:
  return {'channel_version': settings.WECHAT_CHANNEL_VERSION}


# ==================== 凭据 ====================

def init_credentials() -> bool:
  """从 state 文件（优先）或环境变量加载凭据；返回是否拿到 bot_token。"""
  global _bot_token, _ilink_bot_id, _base_url
  auth = state.load_auth()
  _bot_token = (auth.get('bot_token') or settings.WECHAT_BOT_TOKEN or '').strip()
  _ilink_bot_id = (auth.get('ilink_bot_id') or settings.WECHAT_ILINK_BOT_ID or '').strip()
  _base_url = (auth.get('base_url') or settings.WECHAT_BASE_URL or '').rstrip('/')
  return bool(_bot_token)


def current_bot_id() -> str:
  return _ilink_bot_id


def is_running() -> bool:
  return _running


# ==================== 发送 ====================

async def post(path: str, payload: dict, *, auth: bool = True,
               timeout: Optional[float] = None) -> dict:
  """向 iLink 发一次 POST，返回 JSON body；异常向上抛由调用方处理。"""
  if _client is None:
    raise RuntimeError('微信客户端未初始化')
  resp = await _client.post(
    f'{_base_url}{path}',
    json=payload,
    headers=auth_headers() if auth else public_headers(),
    timeout=timeout or (settings.WECHAT_POLL_TIMEOUT + 10),
  )
  resp.raise_for_status()
  data = resp.json()
  return data if isinstance(data, dict) else {}


# ==================== 游标 ====================

def _load_cursor() -> str:
  try:
    return redis_manager.client.get(redis_manager.key(_CURSOR_KEY)) or ''
  except Exception as e:
    logger.warning('[WeChat] 读取游标失败: %s', e)
    return ''


def _save_cursor(buf: str) -> None:
  try:
    redis_manager.client.set(redis_manager.key(_CURSOR_KEY), buf or '')
  except Exception as e:
    logger.warning('[WeChat] 保存游标失败: %s', e)


# ==================== 生命周期 ====================

async def start() -> None:
  global _client, _running, _leader_task, _leader_lock
  if _running:
    return
  if not init_credentials():
    logger.warning('[WeChat] 未接入：缺少 bot_token，请在「认证中心」页扫码接入')
    return

  _client = httpx.AsyncClient()
  _running = True
  _leader_lock = LeaderLock('wechat:leader', settings.WECHAT_LEADER_TTL)
  _leader_task = asyncio.create_task(_leader_loop())
  logger.info('[WeChat] 微信机器人选主循环已启动（bot_id=%s）', _ilink_bot_id or '?')


async def restart() -> None:
  """扫码接入成功后热启动：先停旧轮询再起新的（不重启进程）。"""
  await shutdown()
  await start()


async def _leader_loop() -> None:
  """选主：抢到锁的 worker 起长轮询，其余周期重试。"""
  interval = max(settings.WECHAT_LEADER_TTL / 2, 1)
  while _running:
    try:
      if _leader_lock.is_leader():
        _leader_lock.renew()
        _ensure_poll()
      elif _leader_lock.try_acquire():
        logger.info('[WeChat] 本 worker 成为微信 leader，启动长轮询')
        _ensure_poll()
    except asyncio.CancelledError:
      raise
    except Exception as e:
      logger.warning('[WeChat] leader 循环异常: %s', e)
    await asyncio.sleep(interval)


def _ensure_poll() -> None:
  global _poll_task
  if _poll_task is not None and not _poll_task.done():
    return
  _poll_task = asyncio.create_task(_poll_loop())


async def _poll_loop() -> None:
  """长轮询主循环：批次全部处理完才推进游标，单条异常不拖垮整批。"""
  from service.wechat import channel

  cursor = _load_cursor()
  while _running and _leader_lock is not None and _leader_lock.is_leader():
    try:
      body = await post('/ilink/bot/getupdates', {
        'get_updates_buf': cursor,
        'base_info': base_info(),
      }, timeout=settings.WECHAT_POLL_TIMEOUT + 10)
    except asyncio.CancelledError:
      raise
    except Exception as e:
      logger.warning('[WeChat] getupdates 失败，%ss 后重试: %s', settings.WECHAT_RETRY_DELAY, e)
      await asyncio.sleep(settings.WECHAT_RETRY_DELAY)
      continue

    ret = body.get('ret', 0)
    if ret not in (0, None):
      errcode = body.get('errcode')
      if errcode == -14:
        _on_token_expired()
        return
      logger.warning('[WeChat] getupdates ret=%s errcode=%s errmsg=%s',
                     ret, errcode, body.get('errmsg', ''))
      await asyncio.sleep(settings.WECHAT_RETRY_DELAY)
      continue

    for msg in body.get('msgs') or []:
      try:
        await channel.handle_update(msg)
      except asyncio.CancelledError:
        raise
      except Exception:
        logger.exception('[WeChat] 处理单条消息失败，跳过')

    next_buf = body.get('get_updates_buf')
    if isinstance(next_buf, str) and next_buf != cursor:
      cursor = next_buf
      _save_cursor(cursor)


def _on_token_expired() -> None:
  """token 过期：清凭据、停轮询（leader 循环随 _running=False 退出）。"""
  global _running, _bot_token
  logger.error('[WeChat] bot_token 已过期（errcode -14），停止轮询，请在「认证中心」页重新扫码')
  _bot_token = ''
  _running = False
  state.clear_auth()


async def shutdown() -> None:
  global _running, _leader_task, _poll_task, _leader_lock, _client
  _running = False
  for task in (_poll_task, _leader_task):
    if task is not None:
      task.cancel()
      try:
        await task
      except asyncio.CancelledError:
        pass
  _poll_task = None
  _leader_task = None
  if _leader_lock is not None:
    _leader_lock.release()
    _leader_lock = None
  if _client is not None:
    await _client.aclose()
    _client = None
  logger.info('[WeChat] 微信机器人已停止')

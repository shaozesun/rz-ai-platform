"""飞书 WebSocket 长连接（无公网 IP）+ API client 单例 + 多 worker 选主。

lark-oapi 在导入时捕获模块级 event loop，uvicorn 用 uvloop 时该 loop 已在运行，
导致 ws.Client.start() 里 loop.run_until_complete() 抛 RuntimeError。处理方式与
DeerFlow 一致：在独立线程里新建普通 asyncio loop 并替换 SDK 模块级 loop 引用。

所有异步业务（DB 查询、Agent 流式）统一提交回主 loop（uvicorn loop）执行，
与 MongoDB/AgentService 的既有生命周期一致。

多 worker：`WORKERS>1` 时每个 worker 的 lifespan 都会调 start()，若各自起 WS
连接会触发飞书连接数限制、消息重复投递。这里用 Redis 锁（`set nx ex`）选主，
只有抢到锁的 worker 建 WS 线程，其余 worker 周期重试以便 leader 崩溃后接管。
抢锁/续期用「get 对比 + set nx / expire」两步，续期窗口极小，机器人场景可接受；
若坚持单进程最简，也可 `WORKERS=1` 部署（此时选主退化为恒为 leader）。
"""

import asyncio
import logging
import ssl
import threading
import uuid
from typing import Optional

import certifi

from config.redis_conn import redis_manager
from config.settings import settings

logger = logging.getLogger(__name__)

_main_loop: Optional[asyncio.AbstractEventLoop] = None
_thread: Optional[threading.Thread] = None
_api_client = None
_running = False
_leader_task: Optional[asyncio.Task] = None
_worker_id = uuid.uuid4().hex


def get_api_client():
  return _api_client


def submit_async(coro):
  """把协程提交到主 loop 执行（WS 线程内调用），返回 concurrent.futures.Future。"""
  if _main_loop is None:
    logger.warning('[Feishu] 主 loop 未就绪，丢弃事件处理')
    return None
  return asyncio.run_coroutine_threadsafe(coro, _main_loop)


def _lock_key() -> str:
  return redis_manager.key('lark:leader')


def _try_acquire() -> bool:
  return bool(
    redis_manager.client.set(
      _lock_key(), _worker_id, nx=True, ex=settings.LARK_LEADER_TTL,
    )
  )


def _is_leader() -> bool:
  return redis_manager.client.get(_lock_key()) == _worker_id


def _renew_lease() -> None:
  redis_manager.client.expire(_lock_key(), settings.LARK_LEADER_TTL)


def _ensure_ws(channel) -> None:
  """确保 WS 线程在跑（成为 leader 时启动，线程异常退出后顺带自愈重连）。"""
  global _thread
  if _thread is not None and _thread.is_alive():
    return
  _thread = threading.Thread(
    target=_run_ws, args=(channel.build_event_handler,), daemon=True, name='feishu-ws',
  )
  _thread.start()


async def start() -> None:
  global _main_loop, _api_client, _running, _leader_task
  if _running:
    return
  if not settings.LARK_APP_ID or not settings.LARK_APP_SECRET:
    logger.warning('[Feishu] 缺少 LARK_APP_ID/LARK_APP_SECRET，跳过启动')
    return
  try:
    import lark_oapi as lark
  except ImportError:
    logger.error('[Feishu] 未安装 lark-oapi，请 pip install lark-oapi')
    return

  _api_client = (
    lark.Client.builder()
    .app_id(settings.LARK_APP_ID)
    .app_secret(settings.LARK_APP_SECRET)
    .build()
  )
  _main_loop = asyncio.get_running_loop()
  _running = True

  _leader_task = asyncio.create_task(_leader_loop())
  logger.info('[Feishu] 飞书机器人选主循环已启动')


async def _leader_loop() -> None:
  from service.feishu import channel

  interval = settings.LARK_LEADER_TTL / 2
  while True:
    try:
      is_leader = await asyncio.to_thread(_is_leader)
      if is_leader:
        await asyncio.to_thread(_renew_lease)
        _ensure_ws(channel)
      else:
        acquired = await asyncio.to_thread(_try_acquire)
        if acquired:
          logger.info('[Feishu] 本 worker 成为飞书 leader，建立 WS 连接')
          _ensure_ws(channel)
    except asyncio.CancelledError:
      raise
    except Exception as e:
      logger.warning('[Feishu] leader 循环异常: %s', e)
    await asyncio.sleep(interval)


def _run_ws(event_handler_factory) -> None:
  loop = asyncio.new_event_loop()
  asyncio.set_event_loop(loop)
  try:
    import lark_oapi as lark
    import lark_oapi.ws.client as _ws_client_mod

    # 替换 SDK 模块级 loop，规避主线程 uvloop 冲突
    _ws_client_mod.loop = loop

    # python.org 版 Python 在 macOS 上自带 cert.pem 缺失，websockets 默认 SSL 上下文
    # 无信任锚，连 wss://open.feishu.cn 报 self-signed certificate in certificate chain。
    # 改用 certifi 的 CA bundle（与 requests 同源），让 websockets.connect 能校验飞书证书。
    _ssl_context = ssl.create_default_context(cafile=certifi.where())
    _orig_ws_connect_kwargs = _ws_client_mod._ws_connect_kwargs

    def _ws_connect_kwargs():
      kwargs = _orig_ws_connect_kwargs()
      kwargs['ssl'] = _ssl_context
      return kwargs

    _ws_client_mod._ws_connect_kwargs = _ws_connect_kwargs

    event_handler = event_handler_factory(lark)
    ws_client = lark.ws.Client(
      app_id=settings.LARK_APP_ID,
      app_secret=settings.LARK_APP_SECRET,
      event_handler=event_handler,
      log_level=lark.LogLevel.INFO,
    )
    ws_client.start()
  except Exception:
    logger.exception('[Feishu] WebSocket 异常退出')
  finally:
    loop.close()


async def shutdown() -> None:
  global _running, _leader_task, _thread, _main_loop, _api_client
  _running = False
  if _leader_task is not None:
    _leader_task.cancel()
    try:
      await _leader_task
    except asyncio.CancelledError:
      pass
    _leader_task = None
  try:
    if await asyncio.to_thread(_is_leader):
      await asyncio.to_thread(lambda: redis_manager.client.delete(_lock_key()))
  except Exception:
    pass
  if _thread:
    _thread.join(timeout=5)
    _thread = None
  _main_loop = None
  _api_client = None
  logger.info('[Feishu] 飞书机器人已停止')

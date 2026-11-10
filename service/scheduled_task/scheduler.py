"""定时任务调度器：asyncio 后台轮询 + Redis 分布式锁防多 worker 重复触发

多进程（WORKERS>1）/多节点下每个 worker 都在轮询同一批到期任务，靠 Redis 锁
（set nx）保证每个任务只有抢到锁的 worker 真正推进并执行。锁内重新读任务
（fresh）关闭「读旧快照后重复推进」的竞态。
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone

from config.redis_conn import redis_manager
from config.settings import settings
from service.scheduled_task.repository import get_due_tasks, get_task, set_next_run
from service.scheduled_task.runner import run_task

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_stop = False


class _RunGate:
  """定时任务并发闸：优先**跨进程**（Redis 计数，生产 4 worker 才限得住），Redis 不可用降级进程内。

  为什么需要：调度器是"到点就 `asyncio.create_task(run_task)`"，**原本没有任何上限** ——
  多份重任务撞在一起（36 份报告 + 用户自建任务）会同时打上游，而上游对长流式生成本就不稳
  （实测并发越高越容易 `StreamChunkTimeoutError`）。上限只为保护，不为提速。
  """

  def __init__(self, limit: int, dist=None, redis_client=None, key: str = ''):
    self._limit = max(1, limit)
    self._dist = dist
    self._redis = redis_client
    self._key = key
    self._local = asyncio.Semaphore(self._limit)

  async def acquire(self, task_id: str) -> str:
    """返回占用方式（'redis' / 'local' / 'failopen'），release 时按它归还。"""
    if self._dist is None:
      await self._local.acquire()
      return 'local'
    # DistributedSemaphore 只有非阻塞 try_acquire → 轮询等待（最多约 10 分钟）
    for _ in range(300):
      try:
        if await self._dist.try_acquire():
          await self._renew_lease()
          return 'redis'
      except Exception as e:            # Redis 抖动：直接放行，别把定时任务全堵死
        logger.warning('定时任务并发闸不可用，放行: %s', e)
        return 'failopen'
      await asyncio.sleep(2)
    logger.warning('等并发槽位超时，放行 task_id=%s', task_id)
    return 'failopen'

  async def release(self, mode: str) -> None:
    if mode == 'redis':
      try:
        await self._dist.release()
      except Exception as e:
        logger.warning('释放并发槽位失败（计数可能偏高，靠 TTL 自愈）: %s', e)
    elif mode == 'local':
      self._local.release()

  async def _renew_lease(self) -> None:
    """给计数键续 TTL：DistributedSemaphore 无过期，worker 被 kill 会永久漏槽位。"""
    if self._redis is None or not self._key:
      return
    try:
      await self._redis.expire(self._key, settings.SCHEDULED_TASK_SLOT_LEASE_TTL)
    except Exception:
      pass


_gate: _RunGate | None = None


def _get_gate() -> _RunGate:
  """惰性构建并发闸（首次用到时连一次 Redis；失败降级进程内）。"""
  global _gate
  if _gate is not None:
    return _gate
  limit = settings.SCHEDULED_TASK_MAX_CONCURRENCY
  try:
    import redis.asyncio as aioredis
    client = aioredis.Redis(
      host=settings.REDIS_HOST, port=settings.REDIS_PORT,
      password=settings.REDIS_PASSWORD or None, db=settings.REDIS_DB,
      decode_responses=True, socket_timeout=5,
    )
    key = redis_manager.key('scheduled:run:slots')
    from core.scheduler.semaphore import DistributedSemaphore
    _gate = _RunGate(limit, dist=DistributedSemaphore(client, key, limit), redis_client=client, key=key)
    logger.info('定时任务并发闸就绪：跨进程 Redis 限 %d', limit)
  except Exception as e:
    logger.warning('定时任务并发闸降级为进程内限 %d: %s', limit, e)
    _gate = _RunGate(limit)
  return _gate


async def _run_with_gate(task) -> None:
  """占槽 → 跑任务 → 归还（并发上限之外的行为与直接 run_task 完全一致）。"""
  mode = await _get_gate().acquire(task.task_id)
  try:
    await run_task(task)
  finally:
    await _get_gate().release(mode)


async def _poll_once() -> None:
  now = datetime.now(timezone.utc)
  due = await get_due_tasks(now=now)
  for task in due:
    lock_key = redis_manager.key(f'scheduled:{task.task_id}:lock')
    # 同步 redis 客户端，放线程池避免阻塞事件循环
    acquired = await asyncio.to_thread(
      redis_manager.client.set,
      lock_key, uuid.uuid4().hex,
      nx=True, ex=settings.SCHEDULED_TASK_LOCK_TTL,
    )
    if not acquired:
      continue
    try:
      # 锁内重新读最新任务：等锁期间可能已被别的 worker 推进，读到已过期则跳过
      fresh = await get_task(task.task_id)
      if fresh is None or not fresh.enabled:
        continue
      if fresh.next_run_at and fresh.next_run_at > datetime.now(timezone.utc):
        continue
      await set_next_run(fresh)
      asyncio.create_task(_run_with_gate(fresh))   # 经并发闸（限 SCHEDULED_TASK_MAX_CONCURRENCY）
    finally:
      await asyncio.to_thread(redis_manager.client.delete, lock_key)


async def _loop() -> None:
  while not _stop:
    try:
      await _poll_once()
    except Exception as e:
      logger.warning('定时任务轮询异常: %s', e)
    await asyncio.sleep(settings.SCHEDULED_TASK_POLL_INTERVAL)


async def start() -> None:
  """启动后台轮询（幂等；main.py lifespan 调用）"""
  global _task, _stop
  if _task is not None:
    return
  _stop = False
  _task = asyncio.create_task(_loop())
  logger.info('定时任务调度器已启动，轮询间隔 %ss', settings.SCHEDULED_TASK_POLL_INTERVAL)


async def shutdown() -> None:
  """停止后台轮询（main.py lifespan 关闭时调用）"""
  global _task, _stop
  _stop = True
  if _task is not None:
    _task.cancel()
    try:
      await _task
    except asyncio.CancelledError:
      pass
    _task = None

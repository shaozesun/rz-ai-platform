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
      asyncio.create_task(run_task(fresh))
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

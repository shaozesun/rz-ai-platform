import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator, Awaitable, Optional
import redis.asyncio as aioredis
from fastapi import Request

from core.scheduler.enums import TaskType, RoleLevel
from core.scheduler.config import scheduler_settings
from core.scheduler.priority import PriorityCalculator
from core.scheduler.semaphore import DistributedSemaphore, LocalSemaphore
from core.scheduler.queue import PriorityTaskQueue

logger = logging.getLogger(__name__)


class SlotNotAvailable(Exception):
  """流式请求无法获取槽位时抛出，API 层应返回 503。"""

  def __init__(self, available_in_seconds: int = 5):
    self.available_in_seconds = available_in_seconds
    super().__init__(f'暂无可用槽位，请 {available_in_seconds}s 后重试')


class TaskScheduler:
  """LLM 调用优先级调度器（单例）。

  协调分布式信号量和优先级队列，实现：
  - 跨 Worker 并发控制
  - 任务类型 + 角色优先级调度
  - 流式请求不排队、直接抢槽位
  """

  _instance: Optional['TaskScheduler'] = None

  def __new__(cls):
    if cls._instance is None:
      cls._instance = super().__new__(cls)
      cls._instance._initialized = False
    return cls._instance

  def __init__(self):
    if self._initialized:
      return
    self._initialized = True
    self._redis: Optional[aioredis.Redis] = None
    self._semaphore: Optional[DistributedSemaphore] = None
    self._local_semaphore: Optional[LocalSemaphore] = None
    self._queue: Optional[PriorityTaskQueue] = None
    self._use_distributed = False

  def initialize(self, redis_client: Optional[aioredis.Redis] = None):
    """初始化调度器，传入 Redis 客户端。

    Redis 不可用或 scheduler 禁用时降级为本地信号量。
    如未传入客户端则自动从 settings 创建 async Redis 连接。
    """
    if not scheduler_settings.enabled:
      logger.info('Scheduler 已禁用，使用本地信号量降级')
      self._local_semaphore = LocalSemaphore(
        scheduler_settings.max_concurrent_llm_calls
      )
      self._use_distributed = False
      return

    if redis_client is None:
      try:
        from config.settings import settings
        redis_client = aioredis.Redis(
          host=settings.REDIS_HOST,
          port=settings.REDIS_PORT,
          password=settings.REDIS_PASSWORD or None,
          db=settings.REDIS_DB,
          decode_responses=True,
          socket_timeout=5,
          retry_on_timeout=True,
        )
      except Exception as e:
        logger.warning('创建 Redis 异步客户端失败: %s，降级为本地模式', e)
        self._local_semaphore = LocalSemaphore(
          scheduler_settings.max_concurrent_llm_calls
        )
        self._use_distributed = False
        return

    try:
      self._redis = redis_client
      self._semaphore = DistributedSemaphore(
        redis_client,
        scheduler_settings.semaphore_key,
        scheduler_settings.max_concurrent_llm_calls,
      )
      self._queue = PriorityTaskQueue(
        redis_client,
        scheduler_settings.queue_key,
        scheduler_settings.notify_channel,
      )
      self._local_semaphore = LocalSemaphore(
        scheduler_settings.max_concurrent_llm_calls
      )
      self._use_distributed = True
      logger.info(
        'Scheduler 初始化完成 (分布式模式), max_concurrent=%s',
        scheduler_settings.max_concurrent_llm_calls,
      )
    except Exception as e:
      logger.warning('Redis 连接失败，Scheduler 降级为本地模式: %s', e)
      self._local_semaphore = LocalSemaphore(
        scheduler_settings.max_concurrent_llm_calls
      )
      self._use_distributed = False

  def _get_semaphore(self):
    """获取当前可用的信号量。"""
    if self._use_distributed and self._semaphore:
      return self._semaphore
    return self._local_semaphore

  async def schedule(
    self,
    task_type: TaskType,
    coro: Awaitable[Any],
    request: Optional[Request] = None,
    role: Optional[RoleLevel] = None,
  ) -> Any:
    """调度非流式任务。无槽位时进入优先级队列等待。

    Args:
        task_type: 业务场景类型，决定基础优先级
        coro: 实际 LLM 调用协程
        request: FastAPI Request（用于自动解析角色），与 role 二选一
        role: 显式指定角色，优先级高于 request

    Returns:
        LLM 调用结果
    """
    resolved_role = role or PriorityCalculator.resolve_role(request)
    priority = PriorityCalculator.calculate(task_type, resolved_role)

    sem = self._get_semaphore()

    # 先尝试直接获取槽位
    try:
      acquired = await sem.try_acquire()
    except Exception:
      acquired = False

    if acquired:
      try:
        return await coro
      finally:
        await self._release_slot(sem)
        await self._notify_waiters()

    # 无槽位，进入队列等待
    if not self._use_distributed or not self._queue:
      # 本地模式：阻塞等待信号量
      logger.debug('本地模式阻塞等待槽位: task_type=%s, priority=%s', task_type, priority)
      try:
        await asyncio.wait_for(
          sem.blocking_acquire(),
          timeout=scheduler_settings.slot_acquire_timeout,
        )
      except asyncio.TimeoutError:
        raise SlotNotAvailable(5)
      try:
        return await coro
      finally:
        sem.release()
    else:
      # 分布式模式：Redis 队列等待
      task_id = str(uuid.uuid4())
      queue_timeout = scheduler_settings.queue_timeout

      logger.debug(
        '任务入队: task_id=%s, task_type=%s, priority=%s',
        task_id, task_type, priority,
      )
      await self._queue.enqueue(task_id, priority)

      try:
        dequeued = await self._queue.wait_and_dequeue(timeout=queue_timeout)
        if dequeued is None:
          raise asyncio.TimeoutError(f'排队超时 ({queue_timeout}s)')
      except asyncio.TimeoutError:
        logger.warning('任务排队超时: task_id=%s', task_id)
        raise

      # 轮到自己，抢槽位执行
      deadline = time.time() + scheduler_settings.slot_acquire_timeout
      while time.time() < deadline:
        try:
          acquired = await sem.try_acquire()
        except Exception:
          acquired = False
        if acquired:
          try:
            return await coro
          finally:
            await self._release_slot(sem)
            await self._notify_waiters()
        await asyncio.sleep(0.1)

      raise SlotNotAvailable(
        scheduler_settings.slot_acquire_timeout
      )

  async def schedule_stream(
    self,
    task_type: TaskType,
    stream_coro: AsyncIterator[Any],
    request: Optional[Request] = None,
    role: Optional[RoleLevel] = None,
  ) -> AsyncIterator[Any]:
    """调度流式任务。仅抢信号量，不进入队列。

    流式请求（SSE 对话）不能让用户等待排队，因此：
    1. 尝试立即获取槽位
    2. 如果无槽位，等待 stream_slot_timeout
    3. 超时抛 SlotNotAvailable，API 层返回 503

    Args:
        task_type: 业务场景类型
        stream_coro: 流式生成器
        request: FastAPI Request
        role: 显式指定角色

    Yields:
        LLM 流式输出 chunk
    """
    resolved_role = role or PriorityCalculator.resolve_role(request)
    priority = PriorityCalculator.calculate(task_type, resolved_role)

    sem = self._get_semaphore()

    acquired = False
    try:
      acquired = await sem.try_acquire()
    except Exception:
      acquired = False

    if not acquired:
      # 短暂等待
      deadline = time.time() + scheduler_settings.stream_slot_timeout
      while time.time() < deadline:
        await asyncio.sleep(0.2)
        try:
          acquired = await sem.try_acquire()
        except Exception:
          acquired = False
        if acquired:
          break

    if not acquired:
      logger.warning(
        '流式请求获取槽位超时: task_type=%s', task_type
      )
      raise SlotNotAvailable(scheduler_settings.stream_slot_timeout)

    try:
      async for chunk in stream_coro:
        yield chunk
    finally:
      await self._release_slot(sem)
      await self._notify_waiters()

  async def _release_slot(self, sem):
    """释放信号量槽位。"""
    try:
      await sem.release()
    except Exception as e:
      logger.debug('释放槽位异常: %s', e)

  async def _notify_waiters(self):
    """通知 Redis 等待者。"""
    if self._use_distributed and self._queue:
      await self._queue.notify()

  async def close(self):
    """关闭调度器，清理资源。"""
    if self._use_distributed and self._semaphore:
      await self._semaphore.reset()
    logger.info('Scheduler 已关闭')


scheduler = TaskScheduler();

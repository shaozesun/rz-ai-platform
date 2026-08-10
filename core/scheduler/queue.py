import asyncio
import logging
import random
import time
import uuid
from typing import Optional
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class PriorityTaskQueue:
  """Redis Sorted Set 优先级任务队列。

  使用 ZADD/ZPOPMIN 实现优先级排序。
  Pub/Sub 通知机制避免轮询惊群。
  """

  def __init__(
    self,
    redis_client: aioredis.Redis,
    queue_key: str,
    notify_channel: str,
  ):
    self._redis = redis_client
    self._key = queue_key
    self._channel = notify_channel

  def _make_score(self, priority: int) -> float:
    """将优先级和时间戳打包为 Redis score（越小越优先）。

    score = priority + timestamp_micros / 1e16
    同优先级时时间戳小的（先入队的）优先。
    """
    ts = time.time()
    return priority + ts / 1e16

  async def enqueue(self, task_id: str, priority: int) -> None:
    """将任务加入优先级队列。"""
    score = self._make_score(priority)
    try:
      await self._redis.zadd(self._key, {task_id: score})
      logger.debug('任务入队: %s, priority=%s, score=%.2f', task_id, priority, score)
    except Exception as e:
      logger.error('任务入队失败: %s', e)
      raise

  async def dequeue(self) -> Optional[str]:
    """非阻塞出队，返回最高优先级任务 task_id，队列空返回 None。"""
    try:
      results = await self._redis.zpopmin(self._key, 1)
      if results and len(results) > 0:
        task_id, score = results[0]
        logger.debug('任务出队: %s, score=%s', task_id, score)
        return task_id
      return None
    except Exception as e:
      logger.error('任务出队失败: %s', e)
      return None

  async def wait_and_dequeue(self, timeout: float = 30.0) -> Optional[str]:
    """阻塞等待直到有任务可出队或超时。

    优先级：Pub/Sub 通知 + 轮询兜底。
    """
    deadline = time.time() + timeout

    # 先立刻尝试出队
    task_id = await self.dequeue()
    if task_id:
      return task_id

    # 订阅 Pub/Sub 通知
    try:
      pubsub = self._redis.pubsub()
      await pubsub.subscribe(self._channel)
    except Exception as e:
      logger.warning('Pub/Sub 订阅失败: %s，降级为轮询', e)
      pubsub = None

    try:
      while time.time() < deadline:
        remaining = deadline - time.time()

        if pubsub:
          try:
            message = await pubsub.get_message(
              ignore_subscribe_messages=True,
              timeout=min(remaining, 5.0),
            )
            if message:
              task_id = await self.dequeue()
              if task_id:
                return task_id
          except Exception:
            pass

        # 轮询兜底（500ms-1500ms 随机间隔避免惊群）
        await asyncio.sleep(random.uniform(0.5, 1.5))
        task_id = await self.dequeue()
        if task_id:
          return task_id

      return None
    finally:
      if pubsub:
        try:
          await pubsub.unsubscribe(self._channel)
          await pubsub.close()
        except Exception:
          pass

  async def notify(self):
    """通知等待的 Worker 有新槽位可用。"""
    try:
      await self._redis.publish(self._channel, 'slot_freed')
    except Exception:
      pass

  async def size(self) -> int:
    """查询队列中等待任务数。"""
    try:
      return await self._redis.zcard(self._key)
    except Exception:
      return 0

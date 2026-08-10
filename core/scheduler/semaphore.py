import asyncio
import logging
from typing import Optional
import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

# Lua 脚本：原子获取信号量槽位
_ACQUIRE_SCRIPT = """
local current = redis.call('GET', KEYS[1]) or 0
if tonumber(current) < tonumber(ARGV[1]) then
    redis.call('INCR', KEYS[1])
    return 1
end
return 0
"""

# Lua 脚本：原子释放信号量槽位
_RELEASE_SCRIPT = """
local current = redis.call('GET', KEYS[1]) or 0
if tonumber(current) > 0 then
    redis.call('DECR', KEYS[1])
    return 1
end
return 0
"""


class LocalSemaphore:
  """进程内信号量，用于 Redis 不可用时的降级方案。"""

  def __init__(self, max_concurrency: int):
    self._semaphore = asyncio.Semaphore(max_concurrency)
    self._max = max_concurrency

  async def acquire(self) -> bool:
    return self._semaphore.locked() is False or True

  async def try_acquire(self) -> bool:
    """非阻塞尝试获取，成功返回 True。"""
    return self._semaphore._value > 0

  async def blocking_acquire(self):
    """阻塞获取。"""
    await self._semaphore.acquire()
    return True

  async def release(self):
    self._semaphore.release()

  @property
  async def available(self) -> int:
    return self._semaphore._value


class DistributedSemaphore:
  """Redis 分布式信号量，跨 Worker 限制并发 LLM 调用数。"""

  def __init__(
    self,
    redis_client: aioredis.Redis,
    key: str,
    max_concurrency: int,
  ):
    self._redis = redis_client
    self._key = key
    self._max = max_concurrency
    self._acquire_script: Optional[str] = None
    self._release_script: Optional[str] = None

  async def _ensure_scripts(self):
    """懒加载 Lua 脚本到 Redis。"""
    if self._acquire_script is None:
      self._acquire_script = self._redis.register_script(_ACQUIRE_SCRIPT)
      self._release_script = self._redis.register_script(_RELEASE_SCRIPT)

  async def try_acquire(self) -> bool:
    """非阻塞尝试获取槽位，成功返回 True。"""
    await self._ensure_scripts()
    try:
      result = await self._acquire_script(
        keys=[self._key],
        args=[self._max],
      )
      return result == 1
    except Exception as e:
      logger.warning('分布式信号量获取失败: %s，请检查 Redis 连接', e)
      raise

  async def release(self):
    """释放一个槽位。"""
    await self._ensure_scripts()
    try:
      await self._release_script(keys=[self._key], args=[])
    except Exception as e:
      logger.warning('分布式信号量释放失败: %s', e)

  @property
  async def available(self) -> int:
    """查询当前空闲槽位数。"""
    try:
      current = await self._redis.get(self._key)
      current = int(current) if current else 0
      return max(0, self._max - current)
    except Exception:
      return 0

  async def reset(self):
    """重置信号量计数器（用于异常恢复）。"""
    try:
      await self._redis.delete(self._key)
    except Exception:
      pass

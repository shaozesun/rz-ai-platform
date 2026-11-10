"""通道通用：Redis 选主锁。

多 worker 部署时，每个 worker 的 lifespan 都会启动通道；但外部平台对同一凭据
往往只允许一条连接（飞书连接数限制 / 微信游标单消费 / 企微一机器人一连接），
各自建连会触发限制或消息重复投递。故用 Redis 锁（`set nx ex`）选主，只有抢到锁的
worker 建立连接，其余周期重试以便 leader 崩溃后接管。

抢锁/续期用「get 对比 + set nx / expire」两步，续期窗口极小，机器人场景可接受。
"""

import logging
import uuid

from config.redis_conn import redis_manager

logger = logging.getLogger(__name__)


class LeaderLock:
  """按 key 的选主锁；`worker_id` 缺省用进程级随机 id。"""

  def __init__(self, key: str, ttl: int, worker_id: str | None = None):
    self._key = redis_manager.key(key)
    self._ttl = ttl
    self._worker_id = worker_id or uuid.uuid4().hex

  @property
  def worker_id(self) -> str:
    return self._worker_id

  def try_acquire(self) -> bool:
    return bool(
      redis_manager.client.set(self._key, self._worker_id, nx=True, ex=self._ttl),
    )

  def is_leader(self) -> bool:
    return redis_manager.client.get(self._key) == self._worker_id

  def renew(self) -> None:
    redis_manager.client.expire(self._key, self._ttl)

  def release(self) -> None:
    try:
      if self.is_leader():
        redis_manager.client.delete(self._key)
    except Exception as e:
      logger.warning('选主锁释放失败 key=%s: %s', self._key, e)

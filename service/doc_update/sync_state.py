"""文档更新检查的同步租约（Redis `doc_update:sync`）。

锁值是 JSON `{token, mode, user_id, started_at, done, total, current}`：
- TTL 120s，后台心跳每 30s 续期 → 进程被 kill 后 **≤2 分钟**锁自动释放，
  页面自动恢复「立即检查」（旧实现 TTL 1800s 且 `finally` 不执行时会一直显示「检查中」）；
- 续期/删除用 Lua 比对 token，防被 kill 的旧任务在恢复后覆盖新租约；
- 旧格式（纯 user_id 字符串）在 `read()` 里容错成 `{mode: 'legacy'}`。
"""

import asyncio
import json
import logging
import time
import uuid

from config.redis_conn import redis_manager

logger = logging.getLogger(__name__)

LOCK_KEY = 'doc_update:sync'
LEASE_TTL = 120
HEARTBEAT_SECONDS = 30
# 进度更新节流：进度回调每项都会调一次，没必要每次都往返 Redis
UPDATE_THROTTLE = 1.0

_RENEW_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local ok, cur = pcall(cjson.decode, v)
if not ok or type(cur) ~= 'table' or cur['token'] ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""

_RELEASE_LUA = """
local v = redis.call('GET', KEYS[1])
if not v then return 0 end
local ok, cur = pcall(cjson.decode, v)
if not ok or type(cur) ~= 'table' or cur['token'] ~= ARGV[1] then return 0 end
redis.call('DEL', KEYS[1])
return 1
"""


def _redis_key() -> str:
  return redis_manager.key(LOCK_KEY)


class Lease:
  """一次同步的执行权：带心跳、进度与 token 保护的租约。"""

  def __init__(self, token: str, mode: str, user_id: str):
    self.token = token
    self.mode = mode
    self.user_id = user_id
    self.started_at = time.time()
    self.done = 0
    self.total = 0
    self.current = ''
    self._last_update = 0.0
    self._task: asyncio.Task | None = None

  def _payload(self) -> str:
    return json.dumps({
      'token': self.token,
      'mode': self.mode,
      'user_id': self.user_id,
      'started_at': round(self.started_at, 3),
      'done': self.done,
      'total': self.total,
      'current': self.current,
    }, ensure_ascii=False)

  def update(self, done: int, total: int, current: str = '') -> None:
    """进度 + 续期（节流 1s）。可作为 `perform_sync(progress=…)` 直接传入。"""
    self.done = int(done or 0)
    self.total = int(total or 0)
    self.current = current or ''
    now = time.monotonic()
    if now - self._last_update < UPDATE_THROTTLE:
      return
    self._last_update = now
    self._renew()

  def _renew(self) -> None:
    try:
      redis_manager.client.eval(_RENEW_LUA, 1, _redis_key(),
                                self.token, self._payload(), LEASE_TTL)
    except Exception as e:  # noqa: BLE001  Redis 抖动只丢一次续期，下次心跳补
      logger.debug('文档更新检查租约续期失败: %s', e)

  def _start_heartbeat(self) -> None:
    try:
      loop = asyncio.get_running_loop()
    except RuntimeError:
      return
    self._task = loop.create_task(self._beat())

  async def _beat(self) -> None:
    while True:
      await asyncio.sleep(HEARTBEAT_SECONDS)
      self._renew()

  def release(self) -> None:
    """取消心跳并按 token 删除锁（被 kill 的旧任务不会删掉新租约）。"""
    if self._task is not None:
      self._task.cancel()
      self._task = None
    try:
      redis_manager.client.eval(_RELEASE_LUA, 1, _redis_key(), self.token)
    except Exception as e:  # noqa: BLE001
      logger.debug('文档更新检查租约释放失败: %s', e)


def acquire(mode: str = 'manual', user_id: str = '') -> 'Lease | None':
  """抢锁；已有租约返回 None。Redis 不可用时 fail-open（与旧行为一致）。"""
  lease = Lease(uuid.uuid4().hex, mode, user_id)
  try:
    ok = bool(redis_manager.client.set(_redis_key(), lease._payload(),
                                       nx=True, ex=LEASE_TTL))
  except Exception as e:  # noqa: BLE001
    logger.warning('文档更新检查抢锁失败（Redis 不可用，按未上锁继续）: %s', e)
    return lease
  if not ok:
    return None
  lease._start_heartbeat()
  return lease


def read() -> dict | None:
  """当前租约（含进度）；无锁返回 None，旧格式值容错成 `{mode: 'legacy'}`。"""
  try:
    raw = redis_manager.client.get(_redis_key())
  except Exception:
    return None
  if not raw:
    return None
  try:
    data = json.loads(raw)
  except (ValueError, TypeError):
    data = None
  if not isinstance(data, dict):
    return {
      'mode': 'legacy', 'user_id': str(raw), 'started_at': None,
      'done': 0, 'total': 0, 'current': '',
    }
  return {
    'mode': data.get('mode') or 'manual',
    'user_id': data.get('user_id') or '',
    'started_at': data.get('started_at'),
    'done': int(data.get('done') or 0),
    'total': int(data.get('total') or 0),
    'current': data.get('current') or '',
  }

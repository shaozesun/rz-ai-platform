import threading
import redis
from config.settings import settings


class RedisClientManager:
  """Redis 客户端管理器 (线程安全单例)"""

  _instance = None
  _lock = threading.Lock()
  _initialized = False

  def __new__(cls):
    if cls._instance is None:
      with cls._lock:
        if cls._instance is None:
          cls._instance = super().__new__(cls)
    return cls._instance

  def __init__(self):
    if self._initialized:
      return
    with self._lock:
      if self._initialized:
        return
      self._pool = None
      self._client = None
      RedisClientManager._initialized = True

  def connect(self):
    if self._client is not None:
      return
    self._pool = redis.ConnectionPool(
      host=settings.REDIS_HOST,
      port=settings.REDIS_PORT,
      password=settings.REDIS_PASSWORD or None,
      db=settings.REDIS_DB,
      decode_responses=True,
      max_connections=10,
      socket_timeout=5,
      retry_on_timeout=True,
    )
    self._client = redis.Redis(connection_pool=self._pool)

  @property
  def client(self) -> redis.Redis:
    if self._client is None:
      self.connect()
    return self._client

  def key(self, name: str) -> str:
    return f'{settings.REDIS_PREFIX}{name}'

  def close(self):
    if self._client:
      self._client.close()
      self._client = None
    if self._pool:
      self._pool.disconnect()
      self._pool = None


redis_manager = RedisClientManager()

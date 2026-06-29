import threading
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient, ASCENDING, IndexModel
from config.settings import settings


def _build_uri() -> str:
  if settings.MONGO_USER and settings.MONGO_PASSWORD:
    return (
      f'mongodb://{settings.MONGO_USER}:{settings.MONGO_PASSWORD}'
      f'@{settings.MONGO_HOST}:{settings.MONGO_PORT}/?authSource=admin'
    )
  return f'mongodb://{settings.MONGO_HOST}:{settings.MONGO_PORT}'


class MongoDBManager:
  """MongoDB 连接管理器 (单例, 同时提供 async motor 和 sync pymongo 客户端)"""

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
      self._client = None       # motor async
      self._sync_client = None  # pymongo sync
      self._db = None
      self._sync_db = None
      MongoDBManager._initialized = True

  def connect(self):
    if self._client is not None:
      return
    opts = {
      'serverSelectionTimeoutMS': 5000,
      'maxPoolSize': settings.MONGO_MAX_POOL_SIZE,
      'minPoolSize': settings.MONGO_MIN_POOL_SIZE,
    }
    uri = _build_uri()
    self._client = AsyncIOMotorClient(uri, **opts)
    self._db = self._client[settings.MONGO_DB_NAME]
    # 同步客户端 (仅 chat_history.py 因 LangChain sync 接口约束使用)
    self._sync_client = MongoClient(uri, **opts)
    self._sync_db = self._sync_client[settings.MONGO_DB_NAME]

  @property
  def db(self):
    """异步 motor 数据库 (用于 async/await 上下文)"""
    if self._db is None:
      self.connect()
    return self._db

  @property
  def client(self):
    if self._client is None:
      self.connect()
    return self._client

  @property
  def sync_db(self):
    """同步 pymongo 数据库 (用于 __init__、同步迭代 cursor 等场景)"""
    if self._sync_db is None:
      self.connect()
    return self._sync_db

  def get_collection(self, name: str):
    """默认返回同步集合 (兼容旧 RAG 模块)"""
    return self.sync_db[name]

  async def init_indexes(self):
    """初始化所有集合的索引"""
    db = self.db

    # users
    await db.users.create_index('phone', unique=True, sparse=True)
    await db.users.create_index('user_id', unique=True)

    # roles
    await db.roles.create_index('role_id', unique=True)

    # applications
    await db.applications.create_index('application_id', unique=True)
    await db.applications.create_index('user_id')
    await db.applications.create_index('status')
    await db.applications.create_index(
      [('user_id', ASCENDING), ('requested_roles', ASCENDING), ('status', ASCENDING)],
      unique=True, sparse=True,
    )

    # audit_logs
    await db.audit_logs.create_index('log_id', unique=True)
    await db.audit_logs.create_index('user_id')
    await db.audit_logs.create_index([('created_at', ASCENDING)])

    # sessions
    await db.sessions.create_index('session_id', unique=True)
    await db.sessions.create_index('user_id')
    await db.sessions.create_index([('user_id', ASCENDING), ('created_at', DESCENDING)])

    # messages
    await db.messages.create_index('message_id', unique=True)
    await db.messages.create_index('session_id')
    await db.messages.create_index([('session_id', ASCENDING), ('created_at', ASCENDING)])

    # video_tasks
    await db.video_tasks.create_index('task_id', unique=True)
    await db.video_tasks.create_index('user_id')

    # feedbacks
    await db.feedbacks.create_index('feedback_id', unique=True)
    await db.feedbacks.create_index('user_id')
    await db.feedbacks.create_index([('created_at', ASCENDING)])

    # risk_checks
    await db.risk_checks.create_index('check_id', unique=True)
    await db.risk_checks.create_index('user_id')
    await db.risk_checks.create_index([('checked_at', ASCENDING)])

    # llm_usage
    await db.llm_usage.create_index([('created_at', ASCENDING)])

    # knowledge_groups
    await db.knowledge_groups.create_index(
      [('user_id', ASCENDING), ('group_id', ASCENDING)], unique=True
    )

  def close(self):
    if self._client:
      self._client.close()
      self._client = None
      self._db = None
    if self._sync_client:
      self._sync_client.close()
      self._sync_client = None
      self._sync_db = None


mongodb_manager = MongoDBManager()

import threading
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient, ASCENDING, DESCENDING, IndexModel
from config.settings import settings


def _build_uri() -> str:
  auth = ''
  if settings.MONGO_USER and settings.MONGO_PASSWORD:
    auth = f'{settings.MONGO_USER}:{settings.MONGO_PASSWORD}@'
  hosts = settings.MONGO_HOST  # 可能是 "host1,host2" 多节点
  uri = f'mongodb://{auth}{hosts}:{settings.MONGO_PORT}/?authSource=admin'
  if settings.MONGO_REPLICA_SET:
    uri += f'&replicaSet={settings.MONGO_REPLICA_SET}'
  return uri


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
    """初始化所有集合的索引（每个集合独立容错，单表失败不影响其他表）"""
    db = self.db
    errors: list[str] = []

    async def _safe_index(collection_name: str, fn):
      try:
        await fn()
      except Exception as e:
        errors.append(f'{collection_name}: {e}')

    # users
    await _safe_index('users', lambda: db.users.create_index('phone', unique=True, sparse=True))
    await _safe_index('users', lambda: db.users.create_index('user_id', unique=True))

    # roles
    await _safe_index('roles', lambda: db.roles.create_index('role_id', unique=True))

    # permissions
    await _safe_index('permissions', lambda: db.permissions.create_index('perm_key', unique=True))

    # applications
    await _safe_index('applications', lambda: db.applications.create_index('application_id', unique=True))
    await _safe_index('applications', lambda: db.applications.create_index('user_id'))
    await _safe_index('applications', lambda: db.applications.create_index('status'))

    # audit_logs
    await _safe_index('audit_logs', lambda: db.audit_logs.create_index('log_id', unique=True))
    await _safe_index('audit_logs', lambda: db.audit_logs.create_index('user_id'))
    await _safe_index('audit_logs', lambda: db.audit_logs.create_index([('created_at', ASCENDING)]))

    # sessions
    await _safe_index('sessions', lambda: db.sessions.create_index('session_id', unique=True))
    await _safe_index('sessions', lambda: db.sessions.create_index('user_id'))
    await _safe_index('sessions', lambda: db.sessions.create_index([('user_id', ASCENDING), ('created_at', DESCENDING)]))

    # messages
    await _safe_index('messages', lambda: db.messages.create_index('message_id', unique=True))
    await _safe_index('messages', lambda: db.messages.create_index('session_id'))
    await _safe_index('messages', lambda: db.messages.create_index([('session_id', ASCENDING), ('created_at', ASCENDING)]))

    # video_tasks
    await _safe_index('video_tasks', lambda: db.video_tasks.create_index('task_id', unique=True))
    await _safe_index('video_tasks', lambda: db.video_tasks.create_index('user_id'))

    # feedbacks
    await _safe_index('feedbacks', lambda: db.feedbacks.create_index('feedback_id', unique=True))
    await _safe_index('feedbacks', lambda: db.feedbacks.create_index('user_id'))
    await _safe_index('feedbacks', lambda: db.feedbacks.create_index([('created_at', ASCENDING)]))

    # risk_checks
    await _safe_index('risk_checks', lambda: db.risk_checks.create_index('check_id', unique=True))
    await _safe_index('risk_checks', lambda: db.risk_checks.create_index('user_id'))
    await _safe_index('risk_checks', lambda: db.risk_checks.create_index([('checked_at', ASCENDING)]))

    # fire_safety_history
    await _safe_index('fire_safety_history', lambda: db.fire_safety_history.create_index('record_id', unique=True))
    await _safe_index('fire_safety_history', lambda: db.fire_safety_history.create_index('user_id'))
    await _safe_index('fire_safety_history', lambda: db.fire_safety_history.create_index([('created_at', ASCENDING)]))

    # llm_usage
    await _safe_index('llm_usage', lambda: db.llm_usage.create_index([('caller', ASCENDING), ('created_at', ASCENDING)]))

    # kb_files 文件索引
    await _safe_index('kb_files', lambda: db.kb_files.create_index(
      [('group_id', ASCENDING), ('source', ASCENDING)]
    ))

    # knowledge_groups
    await _safe_index('knowledge_groups', lambda: db.knowledge_groups.create_index(
      [('user_id', ASCENDING), ('group_id', ASCENDING)], unique=True
    ))

    if errors:
      import logging
      logging.getLogger(__name__).warning('部分索引创建跳过: %s', '; '.join(errors))

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

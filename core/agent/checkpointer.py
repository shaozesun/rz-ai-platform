"""Agent Checkpointer 工厂 — 参照 DeerFlow runtime/checkpointer/provider.py 三后端切换模式"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, TYPE_CHECKING

if TYPE_CHECKING:
  from config.settings import Settings
  from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)

# 异步清理函数：关闭连接池 / 释放资源（memory 后端无资源需释放，为 None）
Closer = Callable[[], Awaitable[None]] | None


async def create_checkpointer(settings: 'Settings') -> tuple['BaseCheckpointSaver', Closer]:
  """创建 Checkpointer 实例。

  参照 DeerFlow checkpointer/provider.py 的三后端模式（memory/sqlite/postgres），
  rz 简化为 memory/postgres 两后端。默认 memory 向后兼容。

  返回 (checkpointer, closer)：closer 为异步清理函数（memory 后端为 None），
  调用方在进程退出前 await 它关闭连接池。

  注意：AsyncPostgresSaver 直接用 AsyncConnectionPool 构造（连接池可跨并发复用），
  不经过 from_conn_string 的 async 上下文管理器——那样连接生命周期被 with 块
  绑定，无法常驻整个服务生命周期。
  """
  from langgraph.checkpoint.memory import MemorySaver

  backend = settings.AGENT_CHECKPOINT_BACKEND
  if backend == 'postgres':
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    url = _resolve_postgres_url(settings)
    # min_size=1：AsyncPostgresSaver 内部用 asyncio.Lock 串行化 checkpoint 读写，
    # 连接池里常驻 1 条连接足够；open=False 显式控制开启时机。
    # kwargs 对齐 from_conn_string：autocommit=True（否则 setup 的
    # CREATE INDEX CONCURRENTLY 会在事务里报 ActiveSqlTransaction）、
    # row_factory=dict_row（saver 按列名取字段，如 row["v"]）。
    pool = AsyncConnectionPool(
      conninfo=url,
      min_size=1,
      open=False,
      kwargs={'autocommit': True, 'row_factory': dict_row, 'prepare_threshold': 0},
    )
    await pool.open()
    saver = AsyncPostgresSaver(conn=pool)
    await saver.setup()
    logger.info('Agent Checkpointer: 使用 PostgreSQL 后端')

    async def _close() -> None:
      await pool.close()
      logger.info('Agent Checkpointer: PostgreSQL 连接池已关闭')

    return saver, _close

  if backend == 'memory':
    logger.info('Agent Checkpointer: 使用 MemorySaver（进程内，重启丢失）')
    return MemorySaver(), None

  logger.warning('未知的 AGENT_CHECKPOINT_BACKEND=%s，降级为 MemorySaver', backend)
  return MemorySaver(), None


def _resolve_postgres_url(settings: 'Settings') -> str:
  """解析 PostgreSQL 连接 URL。

  优先使用 AGENT_CHECKPOINT_POSTGRES_URL 完整 DSN；
  否则从独立的 POSTGRES_* 字段拼装。
  """
  url = settings.AGENT_CHECKPOINT_POSTGRES_URL
  if url:
    return url
  # Fallback：从独立字段拼装（兼容那些喜欢拆分配置的部署环境）
  host = getattr(settings, 'POSTGRES_HOST', 'localhost')
  port = getattr(settings, 'POSTGRES_PORT', 5432)
  user = getattr(settings, 'POSTGRES_USER', '')
  password = getattr(settings, 'POSTGRES_PASSWORD', '')
  db = getattr(settings, 'POSTGRES_DB', '')
  if user and password and db:
    return f'postgresql://{user}:{password}@{host}:{port}/{db}'
  raise ValueError(
    'AGENT_CHECKPOINT_BACKEND=postgres 但未配置 PostgreSQL 连接：'
    '请设置 AGENT_CHECKPOINT_POSTGRES_URL 或 POSTGRES_HOST/PORT/USER/PASSWORD/DB'
  )

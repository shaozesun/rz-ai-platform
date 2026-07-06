import os
import sys

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 日志初始化必须最先完成
import logging

from config.logger_config import setup_logging
setup_logging(level='INFO')

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from config.settings import settings
from config.mongodb_conn import mongodb_manager
from config.redis_conn import redis_manager
from config.trace_id import TraceIdMiddleware
from middleware.auth_middleware import AuthMiddleware
from service.auth.user_service import user_service

logger = logging.getLogger(__name__)


def _reset_all_for_child():
    """fork 后子进程重置所有连接和单例"""
    from config.mongodb_conn import mongodb_manager
    mongodb_manager.close()
    from config.redis_conn import redis_manager
    redis_manager.close()
    from service.rag.pipeline.rag_service import rag_service
    rag_service.reset_for_child()
    from repository.vector_store.milvus_store import reset_for_child as _vector_reset
    _vector_reset()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_all_for_child)


@asynccontextmanager
async def lifespan(app: FastAPI):
  logger.info(f'启动 {settings.PROJECT_NAME} v{settings.VERSION}')

  try:
    mongodb_manager.connect()
    redis_manager.connect()
    await mongodb_manager.init_indexes()
    logger.info('数据库连接成功')
  except Exception as e:
    logger.critical('数据库初始化失败: %s', e)

  try:
    await user_service.init_builtin_permissions()
  except Exception as e:
    logger.critical('权限初始化失败: %s', e)

  try:
    await user_service.init_builtin_roles()
    logger.info('预置数据已初始化')
  except Exception as e:
    logger.critical('角色初始化失败: %s', e)

  logger.info('初始化完成, 开始接收请求')
  yield
  mongodb_manager.close()
  redis_manager.close()
  logger.info('服务关闭')


app = FastAPI(
  title=settings.PROJECT_NAME,
  version=settings.VERSION,
  lifespan=lifespan,
)

app.add_middleware(
  CORSMiddleware,
  allow_origins=['*'],
  allow_credentials=True,
  allow_methods=['*'],
  allow_headers=['*'],
)

app.add_middleware(AuthMiddleware)
app.add_middleware(TraceIdMiddleware)

from api.v1 import api_v1_router
app.include_router(api_v1_router)


if __name__ == '__main__':
  uvicorn.run(
    'main:app',
    host=settings.HOST,
    port=settings.PORT,
    workers=settings.WORKERS,
    log_level='info',
    reload=False,
  )

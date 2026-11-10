import os
import sys

# 确保项目根目录在 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import asyncio

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
    from core.scheduler.scheduler import TaskScheduler
    TaskScheduler._instance = None


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_all_for_child)


@asynccontextmanager
async def lifespan(app: FastAPI):
  logger.info(f'启动 {settings.PROJECT_NAME} v{settings.VERSION}')

  # 提示词自检：一个没转义的花括号就能让 Agent 建不起来、**整个对话不可用**（实测事故：
  # SYSTEM_PROMPT 里写了 {"error": …} → KeyError '"error"'）。放在启动阶段，
  # 让它炸在部署/重启时，而不是等用户发消息才 500。
  try:
    from core.agent.prompts import verify_prompts
    verify_prompts()
    logger.info('提示词自检通过')
  except Exception as e:
    logger.critical('提示词自检失败（Agent 对话会不可用）: %s', e)

  try:
    mongodb_manager.connect()
    redis_manager.connect()
    await mongodb_manager.init_indexes()
    logger.info('数据库连接成功')
  except Exception as e:
    logger.critical('数据库初始化失败: %s', e)

  try:
    from core.scheduler.scheduler import scheduler
    from core.scheduler.rag_llm_patch import patch_rag_service
    scheduler.initialize()
    patch_rag_service(scheduler)
    logger.info('Scheduler 初始化完成')
  except Exception as e:
    logger.warning('Scheduler 初始化失败: %s，LLM 调用将不受限', e)

  try:
    await user_service.init_builtin_permissions()
  except Exception as e:
    logger.critical('权限初始化失败: %s', e)

  try:
    await user_service.init_builtin_roles()
    logger.info('预置数据已初始化')
  except Exception as e:
    logger.critical('角色初始化失败: %s', e)

  if settings.SCHEDULED_TASKS_ENABLED:
    # 预设分析报告：启动时按 key 幂等建/同步「系统任务」（全平台只一条，共享产出）。
    # 幂等且有唯一 key，多 worker 各自执行安全；失败只记日志，不拦启动。
    try:
      from service.scheduled_task.report_templates import ensure_preset_tasks
      keys = await ensure_preset_tasks()
      logger.info('预设分析报告就绪: %s', keys)
    except Exception as e:
      logger.warning('预设分析报告初始化失败（不影响启动）: %s', e)

    # 事件统计表预设（`incident__`，两族各自维护；默认关，INCIDENT_STATS_ENABLED=true 才建）
    try:
      from service.scheduled_task.incident_stats.presets import ensure_incident_presets
      ikeys = await ensure_incident_presets()
      if ikeys:
        logger.info('事件统计表预设就绪: %s', ikeys)
    except Exception as e:
      logger.warning('事件统计表预设初始化失败（不影响启动）: %s', e)

    # 告警不合规检查预设（`compliance__`，默认关：COMPLIANCE_ENABLED=false 时直接返回空）
    try:
      from service.scheduled_task.compliance.presets import ensure_compliance_presets
      ckeys = await ensure_compliance_presets()
      if ckeys:
        logger.info('告警不合规检查预设就绪: %s', ckeys)
    except Exception as e:
      logger.warning('告警不合规检查预设初始化失败（不影响启动）: %s', e)

    # 文档更新检查预设（`doc_update__`，默认关：DOC_UPDATE_CHECK_ENABLED=false 时返回空）
    try:
      from service.doc_update.presets import ensure_doc_update_presets
      dkeys = await ensure_doc_update_presets()
      if dkeys:
        logger.info('文档更新检查预设就绪: %s', dkeys)
    except Exception as e:
      logger.warning('文档更新检查预设初始化失败（不影响启动）: %s', e)

    try:
      from service.scheduled_task.scheduler import start as scheduled_start
      await scheduled_start()
    except Exception as e:
      logger.warning('定时任务调度器启动失败: %s', e)

  # 文档更新检查：只有显式开启 DOC_UPDATE_AUTO_BASELINE 且从没有过部门口径快照时才跑一次首期基线。
  # 默认不跑（否则每次 reload / 新月份都静默跑一遍全量，页面一直显示「检查中」）。
  if settings.DOC_UPDATE_CHECK_ENABLED and settings.DOC_UPDATE_AUTO_BASELINE:
    try:
      from service.doc_update import workflow as doc_update_workflow
      asyncio.create_task(doc_update_workflow.ensure_initial_sync())
      logger.info('文档更新检查：首期基线自动执行已排入后台')
    except Exception as e:
      logger.warning('文档更新检查首期自动执行调度失败（不影响启动）: %s', e)

  if settings.LARK_ENABLED:
    try:
      from service.feishu.client import start as lark_start
      await lark_start()
    except Exception as e:
      logger.warning('飞书机器人启动失败: %s', e)

  if settings.WECHAT_ENABLED:
    try:
      from service.wechat.client import start as wechat_start
      await wechat_start()
    except Exception as e:
      logger.warning('微信机器人启动失败: %s', e)

  # 飞书群消息实时入库（WS 推送 → 事件通报库）：与上面的轮询、与对话链都独立，
  # 只管「群里发的通报立即落库」，靠 FEISHU_GROUP_INGEST_ENABLED 单独开关
  if settings.FEISHU_GROUP_INGEST_ENABLED:
    try:
      from service.scheduled_task.incident_stats.realtime import start as ingest_start
      await ingest_start()
    except Exception as e:
      logger.warning('飞书群实时入库启动失败: %s', e)

  # 事件统计表的飞书通报同步（自建 client，不依赖 LARK_ENABLED；多 worker Redis 选主）
  if settings.INCIDENT_STATS_ENABLED:
    try:
      from service.scheduled_task.incident_stats.sync import start_background
      await start_background()
    except Exception as e:
      logger.warning('事件通报同步启动失败: %s', e)

  # 预热：把 agent/平台工具/LangGraph 图的构建提前到启动时后台做，
  # 避免每个 worker 的**首个用户请求**承担 2–3s 冷启动（多 worker 时各进程各预热一次）。
  async def _warmup_agent():
    try:
      from service.agent.agent_service import AgentService
      await AgentService()._ensure_agent()
      logger.info('预热完成：agent 与平台工具已就绪')
    except Exception as e:
      logger.warning('预热失败（不影响启动，首个请求仍会即时构建）: %s', e)

    # 预热常用静态参考数据（公司/组织列表）：写入 Redis，TTL 见 MGMT_REF_CACHE_TTL（默认 24h）
    try:
      from service.query.mgmt.client import call as mgmt_call
      for cap in ('mgmt.query_company_list', 'mgmt.query_dept_list'):
        try:
          await mgmt_call(cap, {})
          logger.info('预热缓存完成：%s', cap)
        except Exception as e:
          logger.warning('预热 %s 失败（首个请求会即时查询，不影响使用）: %s', cap, e)
    except Exception as e:
      logger.warning('静态参考数据预热失败（不影响启动）: %s', e)

  asyncio.create_task(_warmup_agent())

  logger.info('初始化完成, 开始接收请求')
  yield
  if settings.LARK_ENABLED:
    try:
      from service.feishu.client import shutdown as lark_shutdown
      await lark_shutdown()
    except Exception:
      pass
  if settings.WECHAT_ENABLED:
    try:
      from service.wechat.client import shutdown as wechat_shutdown
      await wechat_shutdown()
    except Exception:
      pass
  if settings.SCHEDULED_TASKS_ENABLED:
    try:
      from service.scheduled_task.scheduler import shutdown as scheduled_shutdown
      await scheduled_shutdown()
    except Exception:
      pass
  if settings.INCIDENT_STATS_ENABLED:
    try:
      from service.scheduled_task.incident_stats.sync import stop_background
      await stop_background()
    except Exception:
      pass
  mongodb_manager.close()
  redis_manager.close()
  # Agent Checkpointer 清理（PostgreSQL 模式需关闭连接池）
  if settings.AGENT_ENABLED:
    from service.agent.agent_service import AgentService
    await AgentService().teardown()
  logger.info('服务关闭')


app = FastAPI(
  title=settings.PROJECT_NAME,
  version=settings.VERSION,
  lifespan=lifespan,
)

origins = [o.strip() for o in settings.ALLOWED_ORIGINS.split(',') if o.strip()]
if not origins or origins == ['*']:
  origins = ['*']
  allow_creds = False
else:
  allow_creds = True

app.add_middleware(
  CORSMiddleware,
  allow_origins=origins,
  allow_credentials=allow_creds,
  allow_methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
  allow_headers=['Authorization', 'Content-Type', 'X-Trace-Id'],
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

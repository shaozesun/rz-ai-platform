from fastapi import APIRouter
from config.settings import settings
from config.mongodb_conn import mongodb_manager
from config.redis_conn import redis_manager

router = APIRouter()


@router.get('/health')
async def health():
  checks = {}
  ok = True

  # MongoDB
  try:
    await mongodb_manager.client.server_info()
    checks['mongodb'] = 'ok'
  except Exception as e:
    checks['mongodb'] = str(e)
    ok = False

  # Redis
  try:
    redis_manager.client.ping()
    checks['redis'] = 'ok'
  except Exception as e:
    checks['redis'] = str(e)
    ok = False

  # 飞书通道：**只做展示，不并入 ok** —— 4 个 worker 里只有 leader 持有 WS，
  # 并入会让 Docker HEALTHCHECK 把正常的 follower 判死、容器反复重启。
  # 真正的判据是 leader_present=false 持续超过 2 分钟（外部监控）。
  if settings.LARK_ENABLED:
    try:
      from service.feishu.client import health_snapshot
      checks['feishu'] = health_snapshot()
    except Exception as e:
      checks['feishu'] = str(e)

  return {
    'ok': ok,
    'name': settings.PROJECT_NAME,
    'version': settings.VERSION,
    'checks': checks,
  }


@router.get('/')
async def root():
  return {
    'name': settings.PROJECT_NAME,
    'version': settings.VERSION,
  }

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

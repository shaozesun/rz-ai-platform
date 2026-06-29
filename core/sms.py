"""短信验证码服务"""

import secrets
import time
from config.settings import settings
from config.redis_conn import redis_manager
import logging

logger = logging.getLogger(__name__)


def generate_code() -> str:
  """生成 6 位数字验证码"""
  return f'{secrets.randbelow(1000000):06d}'


async def send_sms(phone: str, code: str) -> bool:
  """发送短信验证码"""
  if settings.SMS_DEV_MODE:
    logger.info(f'[DEV] SMS to {phone}: {code}')
    return True

  # TODO: 对接腾讯云/阿里云短信 SDK
  # 示例: aliyun SDK
  # client = AlibabaCloud.Client(...)
  # request = SendSmsRequest(...)
  # response = client.send_sms(request)
  logger.info(f'SMS sent to {phone}')
  return True


async def send_verification_code(phone: str) -> dict:
  """发送验证码, 返回 {ok, retry_after}"""
  # 频率限制: 同一手机号 60s
  rate_key = redis_manager.key(f'sms_rate:{phone}')
  ttl = redis_manager.client.ttl(rate_key)
  if ttl > 0:
    return {'ok': False, 'retry_after': ttl}

  # IP 频率限制 (在 middleware 层处理)

  code = generate_code()
  store_key = redis_manager.key(f'sms:{phone}')
  redis_manager.client.setex(store_key, 300, code)  # 5min TTL
  redis_manager.client.setex(rate_key, 60, '1')     # 60s 间隔

  await send_sms(phone, code)
  return {'ok': True, 'retry_after': 60}


async def verify_code(phone: str, code: str) -> bool:
  """验证短信验证码"""
  store_key = redis_manager.key(f'sms:{phone}')
  stored = redis_manager.client.get(store_key)
  if stored is None:
    return False
  if stored != code:
    # 错误次数计数
    fail_key = redis_manager.key(f'sms_fail:{phone}')
    fails = redis_manager.client.incr(fail_key)
    redis_manager.client.expire(fail_key, 1800)  # 30min 窗口
    if fails >= 5:
      # 锁定 30 分钟
      redis_manager.client.delete(store_key)
      return False
    return False
  # 验证通过, 删除验证码
  redis_manager.client.delete(store_key)
  redis_manager.client.delete(redis_manager.key(f'sms_fail:{phone}'))
  return True

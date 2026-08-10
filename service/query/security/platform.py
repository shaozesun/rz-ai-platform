"""安防平台装配 —— 把「能力数据 + 后端 caller」组装成 Platform，供统一层注册。"""

from service.query.base.platform import Platform
from service.query.security import client
from service.query.security.capabilities import CAPABILITIES


def security_platform() -> Platform:
  """构建安防平台描述。caller 直接用 client.call（签名已对齐契约）。"""
  return Platform(
    name='security',
    capabilities=tuple(CAPABILITIES),
    caller=client.call,
  )

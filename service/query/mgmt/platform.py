"""综合管理平台装配 —— 把「能力数据 + 后端 caller」组装成 Platform，供统一层注册。"""

from service.query.base.platform import Platform
from service.query.mgmt import client
from service.query.mgmt.capabilities import CAPABILITIES


def mgmt_platform() -> Platform:
  """构建综合管理平台描述。caller 直接用 client.call（签名已对齐契约）。"""
  return Platform(
    name='mgmt',
    capabilities=tuple(CAPABILITIES),
    caller=client.call,
  )

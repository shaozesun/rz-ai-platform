"""DCIM 平台装配 —— 把「能力数据 + 后端 caller」组装成 Platform，供统一层注册。

平台目录只干这一件"装配"的活：capabilities.py 提供数据，client.call 提供后端
调用，此处把两者打包成 base.Platform。启动时由注册入口调 register_platform(dcim_platform())。
"""

from service.query.base.platform import Platform
from service.query.dcim import client
from service.query.dcim.capabilities import CAPABILITIES


def dcim_platform() -> Platform:
  """构建 DCIM 平台描述。caller 直接用 client.call（签名已对齐契约）。"""
  return Platform(
    name='dcim',
    capabilities=tuple(CAPABILITIES),
    caller=client.call,
  )

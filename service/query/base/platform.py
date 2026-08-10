"""平台注册中心 —— 统一层聚合各平台能力的唯一入口。

一个平台 = 「能力数据 + 后端 caller」两样东西（见 Platform）。启动时按
<PLATFORM>_ENABLED 开关调 register_platform() 注册；base 的 retriever/
tools_factory/intent_bindings 全部只依赖这里的聚合视图，不感知具体平台。

设计要点：
- id 全局唯一（带平台前缀），聚合时天然不冲突；据此建反向表 id -> caller。
- caller 签名统一 async (capability_id, params, *, on_behalf_of) -> dict，
  与现有 dcim/client.call 完全一致，未来接 httpx / MCP 只换 caller 实现。
- 重复注册同名平台按幂等处理（覆盖），避免热重载场景下的重复累积。
"""

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from service.query.base.capability import Capability

logger = logging.getLogger(__name__)

# caller 契约：与 service/query/dcim/client.call 同签名
Caller = Callable[..., Awaitable[dict]]


@dataclass(frozen=True)
class Platform:
  """一个外部平台的接入描述。

  Attributes:
    name: 平台标识，如 'dcim' / 'mgmt'，与 capability id 前缀一致。
    capabilities: 该平台的全部能力元数据（纯数据）。
    caller: 后端调用实现，async call(capability_id, params, *, on_behalf_of) -> dict。
  """

  name: str
  capabilities: tuple[Capability, ...]
  caller: Caller


# 已注册平台：name -> Platform
_PLATFORMS: dict[str, Platform] = {}
# 反向表：capability_id -> caller，供 get_caller 快速定位（注册时构建）
_CALLER_INDEX: dict[str, Caller] = {}


def register_platform(platform: Platform) -> None:
  """注册一个平台（幂等：同名覆盖）。

  注册后其能力即进入全局聚合视图，供检索/工具生成使用。

  P0-2 修复：retriever 的向量索引是惰性构建且只建一次的缓存（见
  service/query/base/retriever.py ensure_index），若在索引建成后又注册新平台，
  新能力永远不会进索引、tool_search 搜不到。这里注册后主动清空索引缓存，
  下次检索时会用最新的 all_capabilities() 重建，避免多平台先后注册时的漂移。
  """
  _PLATFORMS[platform.name] = platform
  # 重建反向表（简单可靠，平台数量极少）
  _CALLER_INDEX.clear()
  for p in _PLATFORMS.values():
    for cap in p.capabilities:
      _CALLER_INDEX[cap.id] = p.caller
  logger.info(
    'platform 注册 name=%s caps=%d 总能力=%d',
    platform.name, len(platform.capabilities), len(_CALLER_INDEX),
  )

  from service.query.base.retriever import reset_index
  reset_index()


def all_capabilities() -> list[Capability]:
  """跨平台聚合全部能力，供 retriever 建统一索引 / 工具工厂生成工具。"""
  caps: list[Capability] = []
  for p in _PLATFORMS.values():
    caps.extend(p.capabilities)
  return caps


def get_capability(capability_id: str) -> Capability | None:
  """按 id 取能力，未找到返回 None。"""
  for p in _PLATFORMS.values():
    for cap in p.capabilities:
      if cap.id == capability_id:
        return cap
  return None


def get_caller(capability_id: str) -> Caller | None:
  """按能力 id 定位其平台 caller，未注册返回 None。"""
  return _CALLER_INDEX.get(capability_id)


def registered_platforms() -> list[str]:
  """返回已注册平台名列表，供诊断/日志。"""
  return list(_PLATFORMS.keys())


def reset() -> None:
  """清空注册（仅供测试隔离用）。"""
  _PLATFORMS.clear()
  _CALLER_INDEX.clear()

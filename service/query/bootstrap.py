"""平台注册引导 —— 按 <PLATFORM>_ENABLED 开关注册启用的平台到统一层。

统一入口，供 agent_service 在构建 Agent 前调用。新增平台只需在此加一个
开关分支 + 一行 register_platform，base 检索/工具生成自动纳入其能力。

幂等：register_platform 同名覆盖；重复调用安全（热重载/多次初始化场景）。
"""

import logging

from config.settings import settings
from service.query.base.platform import register_platform, registered_platforms

logger = logging.getLogger(__name__)


def register_enabled_platforms() -> list[str]:
  """按配置开关注册启用平台，返回已注册平台名列表。"""
  if settings.DCIM_ENABLED:
    from service.query.dcim.platform import dcim_platform
    register_platform(dcim_platform())

  if settings.MGMT_ENABLED:
    from service.query.mgmt.platform import mgmt_platform
    register_platform(mgmt_platform())

  if settings.SECURITY_ENABLED:
    from service.query.security.platform import security_platform
    register_platform(security_platform())

  # skills 平台是纯本地文档能力（skills/<name>/SKILL.md 自动发现，无外部 API/连通性依赖），
  # 无需 _ENABLED 开关，直接注册。接入新 skill 零代码：丢一个目录即可。
  from service.query.skills.platform import register_skills_platform
  register_platform(register_skills_platform())

  names = registered_platforms()
  logger.info('平台注册完成 platforms=%s', names)
  return names

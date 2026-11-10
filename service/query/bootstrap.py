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
  if settings.MGMT_ENABLED:
    from service.query.mgmt.platform import mgmt_platform
    register_platform(mgmt_platform())

  if settings.SECURITY_ENABLED:
    from service.query.security.platform import security_platform
    register_platform(security_platform())

  if settings.DCIM_ENABLED:
    # DCIM 数据是**本地库**能力：空间/设备/测点定义在 PG、测点数值在 TDengine
    # （Kafka 接入见 service/dcim_ingest/）。跨库查询的两段式封装在 dcim_meta/client.py。
    from service.query.dcim_meta.platform import dcim_meta_platform
    register_platform(dcim_meta_platform())

  if settings.WEB_ENABLED:
    # 联网平台：注册后仍**不会**出现在其它模式的工具清单/检索目录里 —— 它在
    # MODE_GATED_PLATFORMS 中，构建期就被 manifest/intent_bindings 排除，
    # 只在联网调研模式由 orchestrator 预 promote 才可见（见 base/platform.py）
    from service.query.web.platform import web_platform
    register_platform(web_platform())

  if settings.BROWSER_ENABLED or settings.BROWSER_EXPLORE_ENABLED:
    # 页面截图：配方能力由 recipes.py 里 enabled=True 的配方生成（未校准不注册）；
    # 交互浏览（BROWSER_EXPLORE_ENABLED）另加 6 个逐步操作工具，会话在旁路容器里。
    from service.query.browser.platform import browser_platform
    register_platform(browser_platform())

  # skills 平台是纯本地文档能力（skills/<name>/SKILL.md 自动发现，无外部 API/连通性依赖），
  # 无需 _ENABLED 开关，直接注册。接入新 skill 零代码：丢一个目录即可。
  from service.query.skills.platform import register_skills_platform
  register_platform(register_skills_platform())

  names = registered_platforms()
  logger.info('平台注册完成 platforms=%s', names)
  return names

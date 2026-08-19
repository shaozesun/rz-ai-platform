"""skills 平台装配 —— 自动发现 skills/ 目录生成 Platform。

纯本地文档能力（无外部 API/连通性依赖），与旧 chart 平台同属本地能力，
无需 _ENABLED 开关，bootstrap 直接注册。
"""

from service.query.base.platform import Platform
from service.query.skills import client
from service.query.skills.discover import discover_skills


def register_skills_platform() -> Platform:
  """扫描 skills/ 目录并装配为 skill 平台。"""
  caps = tuple(discover_skills())
  return Platform(name='skill', capabilities=caps, caller=client.build_caller())

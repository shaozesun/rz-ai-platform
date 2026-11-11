"""skills 平台装配 —— 自动发现 skills/ 目录生成 Platform。

纯本地文档能力（无外部 API/连通性依赖），与旧 chart 平台同属本地能力，
无需 _ENABLED 开关，bootstrap 直接注册。
"""

from service.query.base.platform import Platform, register_platform
from service.query.skills import client
from service.query.skills.discover import discover_skills_cached, refresh_skills_cache


def register_skills_platform() -> Platform:
  """按进程内技能快照装配 skill 平台（不扫盘）。"""
  caps = tuple(discover_skills_cached())
  return Platform(name='skill', capabilities=caps, caller=client.build_caller())


def reload_skills() -> None:
  """写盘后刷新技能快照 + 全局技能注册 + 检索索引，并 bump 版本号（下条消息触发 agent 重建）。

  对话内 `skill_manage` 与技能中心 API（启停/删除）共用的唯一热加载入口。
  """
  from service.query.skills.storage import bump_skills_version

  refresh_skills_cache()
  register_platform(register_skills_platform())
  bump_skills_version()

"""web 平台注册 —— 联网检索/抓取（仅联网调研模式可用，见 capabilities.py）。"""

from __future__ import annotations

from service.query.base.platform import Platform
from service.query.web import client
from service.query.web.capabilities import CAPABILITIES


def web_platform() -> Platform:
  return Platform(name='web', capabilities=tuple(CAPABILITIES), caller=client.call)

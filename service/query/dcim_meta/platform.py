"""dcim_meta 平台装配。

纯**本地库**能力（PostgreSQL 元数据 + TDengine 时序），无外部 HTTP 依赖 ——
结构参照同为本地能力的 `service/query/skills/`（因此**不需要 endpoints.py**）。
"""

from service.query.base.platform import Platform
from service.query.dcim_meta import client
from service.query.dcim_meta.capabilities import CAPABILITIES


def dcim_meta_platform() -> Platform:
  return Platform(
    name='dcim_meta',
    capabilities=tuple(CAPABILITIES),
    caller=client.call,
  )

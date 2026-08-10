"""安防平台能力数据（占位，暂无接口文档）。

无接口文档前保持空元组：不注册任何能力，仅验证「三平台并存」时统一层
（platform/retriever/tools_factory）对空能力平台的兼容性。接口文档到位后，
参照 service/query/mgmt/capabilities.py 的写法逐条登记 Capability。
"""

from service.query.base.capability import Capability

CAPABILITIES: tuple[Capability, ...] = ()

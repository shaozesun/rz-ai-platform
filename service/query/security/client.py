"""安防平台统一调用客户端（占位，暂无接口文档）。

capabilities.py 目前为空元组，正常情况下不会有能力路由到这里；call() 直接抛
异常仅作兜底防御。接口文档到位后参照 service/query/mgmt/client.py 的写法实现
真实 HTTP 调用。
"""

import logging

logger = logging.getLogger(__name__)


class SecurityError(Exception):
  """安防平台调用异常。"""


async def call(
  capability_id: str,
  params: dict | None = None,
  *,
  on_behalf_of: str | None = None,
) -> dict:
  """执行一次安防平台能力调用（占位实现）。

  Args:
    capability_id: 能力 id。
    params: 调用参数。
    on_behalf_of: 代表哪个用户发起，阶段二透传鉴权。

  Raises:
    SecurityError: 安防平台尚未接入真实接口。
  """
  logger.warning(
    'security.call 被调用但平台未接入真实接口 id=%s on_behalf_of=%s',
    capability_id, on_behalf_of or '-',
  )
  raise SecurityError(f'安防平台尚未接入真实接口，无法调用能力 {capability_id}')

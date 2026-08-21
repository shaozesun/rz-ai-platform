"""可观测性追踪回调装配 — Langfuse（自建部署，数据不出内网）。

只接 Langfuse，不接 LangSmith（LangSmith 仅云服务，会让 DCIM/mgmt 内部数据出网）。
默认关闭（LANGFUSE_ENABLED=false），未装 langfuse 包或未启用时不影响 Agent 主链路。

脱敏优先走 Langfuse 自建部署的服务端侧脱敏（server-side ingestion masking），
不在本文件里做客户端侧脱敏——集中在 Langfuse 服务配置，改规则不需要重新发布本项目代码。
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)


def build_langfuse_handler():
  """构造 Langfuse CallbackHandler；未启用或未装包时返回 None。

  Returns:
    CallbackHandler 实例，或 None（LANGFUSE_ENABLED=false / langfuse 包未安装 /
    初始化失败时），调用方需自行判断 None 并跳过装配。
  """
  if not settings.LANGFUSE_ENABLED:
    return None

  try:
    from langfuse.callback import CallbackHandler
  except ImportError:
    logger.warning('LANGFUSE_ENABLED=true 但未安装 langfuse 包，追踪不生效（pip install langfuse）')
    return None

  try:
    return CallbackHandler(
      public_key=settings.LANGFUSE_PUBLIC_KEY,
      secret_key=settings.LANGFUSE_SECRET_KEY,
      host=settings.LANGFUSE_HOST,
    )
  except Exception as e:
    logger.warning('Langfuse CallbackHandler 初始化失败，追踪不生效: %s', e)
    return None


def build_tracing_callbacks() -> list:
  """汇总已启用的追踪回调，供 agent_service.py 装配时调用一次并缓存。

  Returns:
    callback 实例列表；无任何追踪启用时返回空列表。
  """
  handler = build_langfuse_handler()
  return [handler] if handler else []

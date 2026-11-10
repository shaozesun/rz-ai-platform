"""可观测性追踪回调装配 — Langfuse（自建部署，数据不出内网）。

只接 Langfuse，不接 LangSmith（LangSmith 仅云服务，会让 DCIM/mgmt 内部数据出网）。
默认关闭（LANGFUSE_ENABLED=false），未装 langfuse 包或未启用时不影响 Agent 主链路。

脱敏优先走 Langfuse 自建部署的服务端侧脱敏（server-side ingestion masking），
不在本文件里做客户端侧脱敏——集中在 Langfuse 服务配置，改规则不需要重新发布本项目代码。
"""

import logging
import uuid

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
    # langfuse 4.x 把 LangChain 回调移到了 langfuse.langchain（旧路径 langfuse.callback 已移除）
    from langfuse.langchain import CallbackHandler
  except ImportError:
    logger.warning('LANGFUSE_ENABLED=true 但未安装 langfuse 包，追踪不生效（pip install langfuse）')
    return None

  class _InlineCallbackHandler(CallbackHandler):
    # 在事件循环的当前上下文里同步执行回调。默认（False）会被 LangChain 丢到线程池，
    # span 的 context.attach / detach 落在不同上下文 → detach 失败被静默吞掉，残留 span
    # 会成为后续 trace 根节点的「幽灵父节点」，Langfuse 找不到 trace 根（列表三列全空）。
    run_inline = True

  try:
    # 显式 trace_context：langfuse 源码中 self._trace_context 优先于环境 OTel 上下文，
    # 根 span 不再从 ambient span 取父节点 → 无论哪里有残留 span，本请求的 LangGraph
    # 根观察 parentObservationId 都为空，即它自己就是 trace 根。
    # （LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST 仍从环境变量读取）
    return _InlineCallbackHandler(trace_context={'trace_id': uuid.uuid4().hex})
  except Exception as e:
    logger.warning('Langfuse CallbackHandler 初始化失败，追踪不生效: %s', e)
    return None


def build_tracing_callbacks() -> list:
  """汇总已启用的追踪回调，由 agent_service.py 每请求调用一次（handler 不跨请求共享）。

  Returns:
    callback 实例列表；无任何追踪启用时返回空列表。
  """
  handler = build_langfuse_handler()
  return [handler] if handler else []

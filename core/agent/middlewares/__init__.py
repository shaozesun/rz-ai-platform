"""Agent 中间件 — 装配函数"""

import logging
from langgraph.graph import StateGraph

from core.agent.state import RzAgentState
from core.agent.middlewares.tool_error import wrap_tool_with_error_handling

logger = logging.getLogger(__name__)


def apply_tool_middlewares(tools: list) -> list:
  """对工具列表应用所有中间件包装。

  顺序：
  1. ToolError — 异常捕获（最内层，直接包工具函数）
  2. RBAC 由 guardrail.check_tool_permission 在运行时按 contextvar 检查
     （AgentService.stream 在每次请求开始时调 set_current_permissions 设置，
     streaming.py 在 on_tool_start 事件里检查），与本函数（构建期）无关，
     不在此处设置。

  返回包装后的工具列表。
  """
  wrapped = []
  for t in tools:
    t = wrap_tool_with_error_handling(t)
    wrapped.append(t)

  return wrapped

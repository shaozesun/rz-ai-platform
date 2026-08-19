"""RZ Agent 工厂 — 使用 langchain create_agent 构建（支持 AgentMiddleware）"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING

from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver

from core.agent.prompts import SYSTEM_PROMPT
from core.agent.middlewares import apply_tool_middlewares
from core.agent.state import RzAgentState

if TYPE_CHECKING:
  from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)


def build_rz_agent(
  model,
  tools,
  *,
  middleware: list | None = None,
  extra_prompt: str = '',
  raw_tools: list | None = None,
  checkpointer: 'BaseCheckpointSaver | None' = None,
):
  """构建 RZ Agent 图。

  Args:
    model: LangChain BaseChatModel 实例
    tools: 同步 @tool 工具列表，走异常包装中间件
    middleware: AgentMiddleware 列表（如 deferred tool filter）
    extra_prompt: 追加到系统提示词末尾的内容（如 <available-tools> 名单）
    raw_tools: 不进「同步」异常包装的工具。analysis 工具在 agent_service 装配时已
      单独套 async 错误包装（await 原 coroutine，异常转错误串喂回 LLM）；返回 Command
      的 tool_search / 自身已有异常捕获的 deferred 工具走原样通道——同步 wrapper 会
      丢 coroutine、破坏 Command 语义，不能包。
    checkpointer: LangGraph Checkpointer 实例。默认 None→MemorySaver。
      外部注入（如 AsyncPostgresSaver）以支持持久化/多进程。

  Returns:
    编译后的 LangGraph 图

  注（P1-2）：不接收 permissions 参数——RBAC 不是「构建时固化」的东西，而是
  streaming.py 在每次 on_tool_start 时按 guardrail.check_tool_permission() 读取
  当前请求的 contextvar 权限做运行时检查（AgentService.stream 里
  set_current_permissions 设置）。之前这里收一个 permissions 形参传给
  apply_tool_middlewares，但 AgentService._ensure_agent 只在 model 变化时才重建
  Agent，导致这个参数形同虚设、还会误导人以为权限会随构建刷新。
  """
  # 应用工具中间件：异常处理（RBAC 检查在运行时按 contextvar 进行，见上方说明）
  wrapped_tools = apply_tool_middlewares(tools)
  all_tools = wrapped_tools + (raw_tools or [])

  # 构建系统提示词（注入当前时间）
  current_time = datetime.now().strftime('%Y年%m月%d日 %H:%M')
  system_msg = SYSTEM_PROMPT.format(current_time=current_time, user_name='运维工程师')
  if extra_prompt:
    system_msg = system_msg + extra_prompt

  if checkpointer is None:
    checkpointer = MemorySaver()

  agent = create_agent(
    model=model,
    tools=all_tools,
    system_prompt=system_msg,
    middleware=middleware or [],
    checkpointer=checkpointer,
    state_schema=RzAgentState,
  )

  logger.info(
    'Agent 构建完成 tools=%d(raw=%d) middleware=%d checkpointer=%s',
    len(all_tools),
    len(raw_tools or []),
    len(middleware or []),
    type(checkpointer).__name__,
  )
  return agent

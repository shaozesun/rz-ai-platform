"""当前请求的用户身份上下文 — 基于 contextvars 的运行时用户隔离。

与 guardrail.py 的 permissions contextvar 对齐：AgentService.stream() 入口调用
set_current_user_id(user_id)，skill 工具 / tool_search / 平台 caller 在运行期读取，
实现用户自建技能的「按用户隔离」访问控制。
"""

import contextvars

# 当前请求的用户 id（空 = 未登录/未走 Agent 通道，不识别用户身份）
_current_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
  'agent_user_id', default=''
)


def set_current_user_id(user_id: str) -> None:
  """设置当前请求的用户 id（在 Agent 流式入口调用，随请求上下文传播）。"""
  _current_user_id.set(user_id or '')


def get_current_user_id() -> str:
  """获取当前请求的用户 id；未设置返回空串。"""
  return _current_user_id.get()

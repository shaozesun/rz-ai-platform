"""RBAC 工具白名单 — 基于 contextvars 的运行时权限过滤"""

import contextvars
from typing import Callable

# 当前请求的权限集合（由 AgentService.stream 设置）
_current_permissions: contextvars.ContextVar[set[str]] = contextvars.ContextVar(
  'agent_permissions', default=set()
)

def set_current_permissions(permissions: set[str]) -> None:
  """设置当前请求的权限（在 stream 入口调用）"""
  _current_permissions.set(permissions)


def get_current_permissions() -> set[str]:
  """获取当前请求的权限"""
  return _current_permissions.get()


def check_tool_permission(tool_name: str) -> bool:
  """检查是否有权使用指定工具。

  规则：拥有 ai:agent / ai:agent:* / ai:agent:<tool_name> 任一即可。
  """
  perms = get_current_permissions()
  return 'ai:agent' in perms or 'ai:agent:*' in perms or f'ai:agent:{tool_name}' in perms

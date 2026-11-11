"""工具中心 API —— 只读列出智能体当前可调用的全部工具。

**只读**：不提供启停。工具的可用性由部署开关（各 `<PLATFORM>_ENABLED`）与
RBAC 角色权限决定，且 Agent 图按配置指纹缓存共享（见 agent_service._ensure_agent），
按用户加减工具会破坏图复用 —— 页面只负责让用户看见「现在会做什么」。
权限与技能中心一致：`ai:agent`。
"""

from fastapi import APIRouter, Request

from core.rbac import require_permission
from service.query.tool_catalog import build_groups

router = APIRouter(prefix='/tools', tags=['tools'])


@router.get('')
@require_permission('ai:agent')
async def list_tools(request: Request):
  """按平台 / 内置分组返回工具清单（技能只在技能中心展示，这里不列）。"""
  return {'ok': True, 'data': {'groups': build_groups()}}

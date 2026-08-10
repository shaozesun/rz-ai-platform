from typing import Optional
from fastapi import Request
from core.scheduler.enums import TaskType, RoleLevel

# 任务类型基础权重：数值越小越优先
_TASK_BASE = {
  TaskType.CHAT: -300,
  TaskType.RISK_DETECTION: -200,
  TaskType.FIRE_SAFETY: -200,
  TaskType.INGESTION: -50,
  TaskType.VIDEO: -50,
}

# 角色加权：数值越小越优先
_ROLE_BONUS = {
  RoleLevel.ADMIN: -100,
  RoleLevel.USER: 0,
  RoleLevel.ANONYMOUS: 50,
}


class PriorityCalculator:
  """根据任务类型和用户角色计算调度优先级。"""

  @classmethod
  def calculate(cls, task_type: TaskType, role: RoleLevel) -> int:
    """返回有效优先级（越小越优先），用于 Redis Sorted Set score。"""
    base = _TASK_BASE.get(task_type, 0)
    bonus = _ROLE_BONUS.get(role, 0)
    return base + bonus

  @classmethod
  def resolve_role(cls, request: Optional[Request]) -> RoleLevel:
    """从 FastAPI Request 解析用户角色等级。"""
    if request is None:
      return RoleLevel.ANONYMOUS
    try:
      user = getattr(request.state, 'current_user', None)
    except Exception:
      return RoleLevel.ANONYMOUS
    if user is None:
      return RoleLevel.ANONYMOUS
    roles = user.get('roles', [])
    if not roles:
      return RoleLevel.ANONYMOUS
    for r in roles:
      name = r.get('name', '') if isinstance(r, dict) else r
      if name in ('root', 'admin'):
        return RoleLevel.ADMIN
    return RoleLevel.USER

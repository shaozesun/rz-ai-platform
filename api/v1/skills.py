"""技能中心 API —— 列出平台内置技能与当前用户自建技能，管理启停与删除。

职责边界：**修改技能内容仍走对话里的 `skill_manage`**，这里只做展示与管理动作。
- 官方技能（owner=''）只读：所有人可见，不可停用/删除（共享 Agent 图不按用户重建）；
- 自建技能（owner=<uid>）只对本人可见，可停用/启用、可删除。

启停状态存 Mongo `skill_states`（运行期判断，跨 worker 立即一致）；删除后调
`reload_skills()` 热加载，与对话内 `skill_manage delete` 同一链路。
权限统一 `ai:agent`（与报告/合规页一致）。
"""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.rbac import require_permission
from service.query.skills import catalog, storage
from service.query.skills.states import (
  get_disabled_skill_names,
  is_skill_enabled,
  remove_skill_state,
  set_skill_enabled,
)

router = APIRouter(prefix='/skills', tags=['skills'])


class SkillEnabledBody(BaseModel):
  enabled: bool = True


def _current_user(request: Request) -> str:
  return getattr(request.state, 'user_id', '') or ''


def _owned_name(name: str, user_id: str) -> str:
  """校验技能名并确认属于当前用户；不存在/越权统一 404（不暴露归属）。"""
  try:
    normalized = storage.validate_skill_name(name)
    storage.ensure_user_skill_editable(user_id, normalized)
  except ValueError:
    raise HTTPException(404, '技能不存在或无权限')
  return normalized


@router.get('')
@require_permission('ai:agent')
async def list_skills(request: Request):
  """当前用户可见的全部技能：公开技能 + 本人自建技能。"""
  user_id = _current_user(request)
  disabled = await get_disabled_skill_names(user_id)
  return {'ok': True, 'data': {'skills': catalog.list_catalog(user_id, disabled)}}


@router.get('/{name}')
@require_permission('ai:agent')
async def get_skill(name: str, request: Request):
  """技能详情（SKILL.md 全文 + references 索引）；他人技能 404。"""
  user_id = _current_user(request)
  detail = catalog.skill_detail(name, user_id)
  if detail is None:
    raise HTTPException(404, '技能不存在或无权限')
  if detail['owner']:
    detail['enabled'] = await is_skill_enabled(user_id, name)
  else:
    detail['enabled'] = True
  return {'ok': True, 'data': detail}


@router.patch('/{name}/enabled')
@require_permission('ai:agent')
async def set_enabled(name: str, body: SkillEnabledBody, request: Request):
  """启停本人自建技能（官方技能不可停用，一律 404）。"""
  user_id = _current_user(request)
  normalized = _owned_name(name, user_id)
  await set_skill_enabled(user_id, normalized, body.enabled)
  return {'ok': True, 'data': {'name': normalized, 'enabled': bool(body.enabled)}}


@router.delete('/{name}')
@require_permission('ai:agent')
async def delete_skill(name: str, request: Request):
  """删除本人自建技能，并清掉启用状态、热加载技能注册。"""
  user_id = _current_user(request)
  normalized = _owned_name(name, user_id)
  storage.delete_skill(user_id, normalized)
  await remove_skill_state(user_id, normalized)
  from service.query.skills.platform import reload_skills
  reload_skills()
  return {'ok': True, 'message': f'技能「{normalized}」已删除'}

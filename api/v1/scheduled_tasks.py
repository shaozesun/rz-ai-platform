"""定时任务管理 API（定时报告/巡检）

覆盖：任务定义 CRUD、手动触发、运行历史、任务级产物下载。
权限：scheduled_task:manage（管理员角色预置）。响应统一 {ok, data} 包装（对齐项目约定）。
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from core.rbac import require_permission
from models.rag.message_models import MessageListResponse
from models.scheduled_task import (
  ScheduledTaskCreate,
  ScheduledTaskUpdate,
)
from service.rag.conversation.message_service import message_service
from service.scheduled_task import repository as tasks_repo
from service.scheduled_task.runner import run_task

router = APIRouter(prefix='/scheduled-tasks', tags=['scheduled-tasks'])


@router.post('')
@require_permission('scheduled_task:manage')
async def create_task(body: ScheduledTaskCreate, request: Request):
  user_id = getattr(request.state, 'user_id', '') or 'system'
  task = await tasks_repo.create_task(body, created_by=user_id)
  return {'ok': True, 'data': task.model_dump(mode='json')}


@router.get('')
@require_permission('scheduled_task:manage')
async def list_tasks(request: Request, offset: int = 0, limit: int = Query(100, le=500)):
  tasks = await tasks_repo.list_tasks(limit=limit, offset=offset)
  return {'ok': True, 'data': [t.model_dump(mode='json') for t in tasks]}


@router.get('/notifications')
async def list_notifications(request: Request, limit: int = Query(20, le=50)):
  """当前用户的任务完成通知（铃铛轮询用，仅靠 middleware 登录校验，普通用户为空）。"""
  user_id = getattr(request.state, 'user_id', '')
  if not user_id:
    return {'ok': True, 'data': []}
  items = await tasks_repo.list_notifications(user_id, limit=limit)
  return {'ok': True, 'data': [i.model_dump(mode='json') for i in items]}


@router.get('/{task_id}')
@require_permission('scheduled_task:manage')
async def get_task(task_id: str, request: Request):
  task = await tasks_repo.get_task(task_id)
  if not task:
    raise HTTPException(404, '任务不存在')
  return {'ok': True, 'data': task.model_dump(mode='json')}


@router.patch('/{task_id}')
@require_permission('scheduled_task:manage')
async def update_task(task_id: str, body: ScheduledTaskUpdate, request: Request):
  task = await tasks_repo.update_task(task_id, body)
  if not task:
    raise HTTPException(404, '任务不存在')
  return {'ok': True, 'data': task.model_dump(mode='json')}


@router.delete('/{task_id}')
@require_permission('scheduled_task:manage')
async def delete_task(task_id: str, request: Request):
  if not await tasks_repo.delete_task(task_id):
    raise HTTPException(404, '任务不存在')
  return {'ok': True}


@router.post('/{task_id}/trigger')
@require_permission('scheduled_task:manage')
async def trigger_task(task_id: str, request: Request):
  """手动立即触发一次（不改调度计划）"""
  task = await tasks_repo.get_task(task_id)
  if not task:
    raise HTTPException(404, '任务不存在')
  asyncio.create_task(run_task(task))
  return {'ok': True}


@router.get('/{task_id}/runs')
@require_permission('scheduled_task:manage')
async def list_runs(
  task_id: str, request: Request,
  offset: int = 0, limit: int = Query(50, le=200),
):
  runs = await tasks_repo.list_runs(task_id, limit=limit, offset=offset)
  return {'ok': True, 'data': [r.model_dump(mode='json') for r in runs]}


@router.get('/{task_id}/runs/{run_id}/messages')
@require_permission('scheduled_task:manage')
async def get_run_messages(task_id: str, run_id: str, request: Request):
  """运行完整对话：按 run 的 thread_id 拉取会话消息（供前端「查看对话」展示）。"""
  run = await tasks_repo.get_run(run_id)
  if not run or run.task_id != task_id:
    raise HTTPException(404, '运行记录不存在')
  if not run.thread_id:
    return MessageListResponse(messages=[], total=0)
  session_id = run.thread_id.removeprefix('rz-agent-')
  messages = await message_service.get_messages(session_id)
  return MessageListResponse(messages=messages, total=len(messages))


@router.get('/{task_id}/runs/{run_id}/artifacts/{filename}')
@require_permission('scheduled_task:manage')
async def download_artifact(task_id: str, run_id: str, filename: str, request: Request):
  """任务级产物下载（绕过现有 /chat/agent/artifacts 的 session 归属校验）"""
  run = await tasks_repo.get_run(run_id)
  if not run or run.task_id != task_id:
    raise HTTPException(404, '运行记录不存在')
  path = tasks_repo.resolve_artifact_path(run, filename)
  if path is None:
    raise HTTPException(404, '产物不存在')
  return FileResponse(path, filename=path.name)

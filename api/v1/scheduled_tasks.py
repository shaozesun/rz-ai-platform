"""定时任务管理 API（定时报告/巡检）

覆盖：任务定义 CRUD、手动触发、运行历史、任务级产物下载。
权限：scheduled_task:manage（管理员角色预置）。
"""

import asyncio

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from core.rbac import require_permission
from models.scheduled_task import (
  ScheduledTask,
  ScheduledTaskCreate,
  ScheduledTaskRun,
  ScheduledTaskUpdate,
)
from service.scheduled_task import repository as tasks_repo
from service.scheduled_task.runner import run_task

router = APIRouter(prefix='/scheduled-tasks', tags=['scheduled-tasks'])


@router.post('', response_model=ScheduledTask)
@require_permission('scheduled_task:manage')
async def create_task(body: ScheduledTaskCreate, request: Request):
  user_id = getattr(request.state, 'user_id', '') or 'system'
  return await tasks_repo.create_task(body, created_by=user_id)


@router.get('', response_model=list[ScheduledTask])
@require_permission('scheduled_task:manage')
async def list_tasks(request: Request, offset: int = 0, limit: int = Query(100, le=500)):
  return await tasks_repo.list_tasks(limit=limit, offset=offset)


@router.get('/{task_id}', response_model=ScheduledTask)
@require_permission('scheduled_task:manage')
async def get_task(task_id: str, request: Request):
  task = await tasks_repo.get_task(task_id)
  if not task:
    raise HTTPException(404, '任务不存在')
  return task


@router.patch('/{task_id}', response_model=ScheduledTask)
@require_permission('scheduled_task:manage')
async def update_task(task_id: str, body: ScheduledTaskUpdate, request: Request):
  task = await tasks_repo.update_task(task_id, body)
  if not task:
    raise HTTPException(404, '任务不存在')
  return task


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


@router.get('/{task_id}/runs', response_model=list[ScheduledTaskRun])
@require_permission('scheduled_task:manage')
async def list_runs(
  task_id: str, request: Request,
  offset: int = 0, limit: int = Query(50, le=200),
):
  return await tasks_repo.list_runs(task_id, limit=limit, offset=offset)


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

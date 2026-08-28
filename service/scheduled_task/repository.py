"""定时任务定义与运行记录持久化（MongoDB）"""

import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.mongodb_conn import mongodb_manager
from config.settings import settings
from models.scheduled_task import (
  ScheduleType,
  ScheduledTask,
  ScheduledTaskCreate,
  ScheduledTaskRun,
  ScheduledTaskUpdate,
)

logger = logging.getLogger(__name__)

TASKS_COLLECTION = 'scheduled_tasks'
RUNS_COLLECTION = 'scheduled_task_runs'


def compute_next_run(
  schedule_type: ScheduleType,
  last_run_at: Optional[datetime],
  once_at: Optional[datetime],
  interval_seconds: Optional[int],
  cron_expr: Optional[str],
) -> Optional[datetime]:
  """计算任务下次运行时间。

  once：直接返回指定时间（跑完由 set_next_run 禁用任务）；
  interval：last_run_at + interval（无 last 则从现在起算）；
  cron：croniter 从 last_run_at（或现在）起算下一次。
  """
  base = last_run_at or datetime.utcnow()
  if schedule_type == ScheduleType.ONCE:
    return once_at
  if schedule_type == ScheduleType.INTERVAL:
    return base + timedelta(seconds=interval_seconds or 3600)
  if schedule_type == ScheduleType.CRON:
    if not cron_expr:
      return None
    from croniter import croniter
    return croniter(cron_expr, base).get_next(datetime)
  return None


# ==================== 任务定义 CRUD ====================

async def create_task(data: ScheduledTaskCreate, created_by: str) -> ScheduledTask:
  task = ScheduledTask(
    task_id=uuid.uuid4().hex,
    **data.model_dump(),
    created_by=created_by,
    next_run_at=compute_next_run(
      data.schedule_type, None, data.once_at, data.interval_seconds, data.cron_expr,
    ),
  )
  await mongodb_manager.db[TASKS_COLLECTION].insert_one(task.model_dump())
  return task


async def list_tasks(limit: int = 100, offset: int = 0) -> list[ScheduledTask]:
  cursor = (
    mongodb_manager.db[TASKS_COLLECTION]
    .find().sort('created_at', -1).skip(offset).limit(limit)
  )
  docs = await cursor.to_list(limit)
  return [ScheduledTask(**doc) for doc in docs]


async def get_task(task_id: str) -> Optional[ScheduledTask]:
  doc = await mongodb_manager.db[TASKS_COLLECTION].find_one({'task_id': task_id})
  return ScheduledTask(**doc) if doc else None


async def update_task(task_id: str, data: ScheduledTaskUpdate) -> Optional[ScheduledTask]:
  updates = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
  if not updates:
    return await get_task(task_id)
  updates['updated_at'] = datetime.utcnow()
  await mongodb_manager.db[TASKS_COLLECTION].update_one(
    {'task_id': task_id}, {'$set': updates},
  )
  return await get_task(task_id)


async def delete_task(task_id: str) -> bool:
  result = await mongodb_manager.db[TASKS_COLLECTION].delete_one({'task_id': task_id})
  return result.deleted_count > 0


async def get_due_tasks(now: Optional[datetime] = None, limit: int = 50) -> list[ScheduledTask]:
  """查询已到期的启用任务（供调度器轮询）"""
  now = now or datetime.utcnow()
  cursor = (
    mongodb_manager.db[TASKS_COLLECTION]
    .find({'enabled': True, 'next_run_at': {'$lte': now}})
    .limit(limit)
  )
  docs = await cursor.to_list(limit)
  return [ScheduledTask(**doc) for doc in docs]


async def set_next_run(task: ScheduledTask) -> None:
  """推进任务下次运行时间；once 任务跑完即禁用（下次不再触发）"""
  now = datetime.utcnow()
  if task.schedule_type == ScheduleType.ONCE:
    await mongodb_manager.db[TASKS_COLLECTION].update_one(
      {'task_id': task.task_id},
      {'$set': {'enabled': False, 'next_run_at': None, 'updated_at': now}},
    )
    return
  nxt = compute_next_run(task.schedule_type, task.next_run_at, task.once_at,
                         task.interval_seconds, task.cron_expr)
  await mongodb_manager.db[TASKS_COLLECTION].update_one(
    {'task_id': task.task_id},
    {'$set': {'next_run_at': nxt, 'updated_at': now}},
  )


# ==================== 运行记录 CRUD ====================

async def create_run(task_id: str) -> ScheduledTaskRun:
  run = ScheduledTaskRun(run_id=uuid.uuid4().hex, task_id=task_id)
  await mongodb_manager.db[RUNS_COLLECTION].insert_one(run.model_dump())
  return run


async def finish_run(
  run_id: str,
  *,
  status,
  final_reply: str = '',
  artifact_urls: list[str] | None = None,
  error: str = '',
  duration_ms: int = 0,
) -> None:
  await mongodb_manager.db[RUNS_COLLECTION].update_one(
    {'run_id': run_id},
    {'$set': {
      'status': status,
      'final_reply': final_reply,
      'artifact_urls': artifact_urls or [],
      'error': error,
      'duration_ms': duration_ms,
    }},
  )


async def list_runs(task_id: str, limit: int = 50, offset: int = 0) -> list[ScheduledTaskRun]:
  cursor = (
    mongodb_manager.db[RUNS_COLLECTION]
    .find({'task_id': task_id}).sort('triggered_at', -1).skip(offset).limit(limit)
  )
  docs = await cursor.to_list(limit)
  return [ScheduledTaskRun(**doc) for doc in docs]


async def get_run(run_id: str) -> Optional[ScheduledTaskRun]:
  doc = await mongodb_manager.db[RUNS_COLLECTION].find_one({'run_id': run_id})
  return ScheduledTaskRun(**doc) if doc else None


def resolve_artifact_path(run: ScheduledTaskRun, filename: str) -> Optional[Path]:
  """把运行记录里的产物 URL 反查为沙箱产物文件路径（供任务级下载端点）。

  产物 URL 形如 /api/v1/chat/agent/artifacts/{thread_id}/{dataset_id}/{filename}，
  实际文件在 {AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/{filename}。
  filename 需精确匹配，拒绝路径穿越。
  """
  if not filename or '/' in filename or filename in ('.', '..'):
    return None
  base = Path(settings.AGENT_ANALYSIS_DATA_DIR)
  for url in run.artifact_urls:
    marker = '/artifacts/'
    idx = url.find(marker)
    if idx < 0:
      continue
    segs = url[idx + len(marker):].split('/')
    if len(segs) < 2 or segs[-1] != filename:
      continue
    path = base.joinpath(*segs)
    if path.is_file():
      return path
  return None

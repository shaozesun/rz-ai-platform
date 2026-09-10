"""定时任务定义与运行记录持久化（MongoDB）"""

import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from config.mongodb_conn import mongodb_manager
from config.settings import settings
from models.scheduled_task import (
  ScheduleType,
  ScheduledTask,
  ScheduledTaskCreate,
  ScheduledTaskNotification,
  ScheduledTaskRun,
  ScheduledTaskUpdate,
)

logger = logging.getLogger(__name__)

TASKS_COLLECTION = 'scheduled_tasks'
RUNS_COLLECTION = 'scheduled_task_runs'
NOTIFICATIONS_COLLECTION = 'scheduled_task_notifications'


def _get_task_tz():
  """cron 按此本地时区计算（默认 Asia/Shanghai）；缺 tzdata 时回退固定 UTC+8。"""
  from zoneinfo import ZoneInfo
  try:
    return ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE)
  except Exception:
    return timezone(timedelta(hours=8))


def compute_next_run(
  schedule_type: ScheduleType,
  last_run_at: Optional[datetime],
  once_at: Optional[datetime],
  interval_seconds: Optional[int],
  cron_expr: Optional[str],
) -> Optional[datetime]:
  """计算任务下次运行时间（统一 UTC-aware）。

  once：直接返回指定时间（跑完由 set_next_run 禁用任务）；
  interval：last_run_at + interval（无 last 则从现在起算）；
  cron：**在本地时区**算下一次（用户填的时间是本地时间），结果转回 UTC 存储——
    避免「每天8点」被按 UTC 8点（=本地16点）触发。
  """
  base = last_run_at or datetime.now(timezone.utc)
  if schedule_type == ScheduleType.ONCE:
    return once_at
  if schedule_type == ScheduleType.INTERVAL:
    return base + timedelta(seconds=interval_seconds or 3600)
  if schedule_type == ScheduleType.CRON:
    if not cron_expr:
      return None
    from croniter import croniter
    local_next = croniter(cron_expr, base.astimezone(_get_task_tz())).get_next(datetime)
    return local_next.astimezone(timezone.utc)
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


async def list_tasks_by_creator(user_id: str, limit: int = 100) -> list[ScheduledTask]:
  """按创建人列出任务（对话工具 list 按用户隔离）"""
  cursor = (
    mongodb_manager.db[TASKS_COLLECTION]
    .find({'created_by': user_id}).sort('created_at', -1).limit(limit)
  )
  docs = await cursor.to_list(limit)
  return [ScheduledTask(**doc) for doc in docs]


async def get_task_by_name_and_creator(name: str, user_id: str) -> Optional[ScheduledTask]:
  """按名称 + 创建人定位任务（对话工具 delete/pause/resume 用 + create 查重）"""
  doc = await mongodb_manager.db[TASKS_COLLECTION].find_one({
    'name': name, 'created_by': user_id,
  })
  return ScheduledTask(**doc) if doc else None


async def update_task(task_id: str, data: ScheduledTaskUpdate) -> Optional[ScheduledTask]:
  current = await get_task(task_id)
  if not current:
    return None
  updates = {k: v for k, v in data.model_dump(exclude_unset=True).items() if v is not None}
  if not updates:
    return current

  # 调度字段有变 → 用合并后的状态重算 next_run_at（否则编辑时间后下次运行仍是旧值）
  sched_fields = {'schedule_type', 'once_at', 'interval_seconds', 'cron_expr'}
  if sched_fields & set(updates.keys()):
    merged = {**current.model_dump(), **updates}
    st = merged.get('schedule_type')
    if st == ScheduleType.ONCE:
      # once：下次运行 = 新 once_at；未过期则重新启用（编辑时间后恢复触发）
      updates['next_run_at'] = merged.get('once_at')
      if merged.get('once_at') is not None:
        updates['enabled'] = True
    else:
      updates['next_run_at'] = compute_next_run(
        st, current.next_run_at, merged.get('once_at'),
        merged.get('interval_seconds'), merged.get('cron_expr'),
      )

  updates['updated_at'] = datetime.now(timezone.utc)
  await mongodb_manager.db[TASKS_COLLECTION].update_one(
    {'task_id': task_id}, {'$set': updates},
  )
  return await get_task(task_id)


async def delete_task(task_id: str) -> bool:
  result = await mongodb_manager.db[TASKS_COLLECTION].delete_one({'task_id': task_id})
  return result.deleted_count > 0


async def get_due_tasks(now: Optional[datetime] = None, limit: int = 50) -> list[ScheduledTask]:
  """查询已到期的启用任务（供调度器轮询，UTC-aware）"""
  now = now or datetime.now(timezone.utc)
  cursor = (
    mongodb_manager.db[TASKS_COLLECTION]
    .find({'enabled': True, 'next_run_at': {'$lte': now}})
    .limit(limit)
  )
  docs = await cursor.to_list(limit)
  return [ScheduledTask(**doc) for doc in docs]


async def set_next_run(task: ScheduledTask) -> None:
  """推进任务下次运行时间；once 任务跑完即禁用（下次不再触发）"""
  now = datetime.now(timezone.utc)
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

async def create_run(task_id: str, run_id: str = '', thread_id: str = '') -> ScheduledTaskRun:
  rid = run_id or uuid.uuid4().hex
  run = ScheduledTaskRun(run_id=rid, task_id=task_id, thread_id=thread_id)
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


# ==================== 完成通知 ====================

async def create_notification(
  *,
  user_id: str,
  task_id: str,
  task_name: str,
  run_id: str,
  status: str,
  title: str,
  body: str = '',
) -> None:
  """写一条任务完成通知（铃铛轮询 + 首页动态共用）。"""
  notif = ScheduledTaskNotification(
    user_id=user_id,
    task_id=task_id,
    task_name=task_name,
    run_id=run_id,
    status=status,
    title=title,
    body=body,
  )
  await mongodb_manager.db[NOTIFICATIONS_COLLECTION].insert_one(notif.model_dump())


async def list_notifications(user_id: str, limit: int = 20) -> list[ScheduledTaskNotification]:
  """按创建人拉取最近通知，倒序。"""
  cursor = (
    mongodb_manager.db[NOTIFICATIONS_COLLECTION]
    .find({'user_id': user_id}).sort('created_at', -1).limit(limit)
  )
  docs = await cursor.to_list(limit)
  return [ScheduledTaskNotification(**doc) for doc in docs]


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

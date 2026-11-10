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

# 系统预设报告任务的创建人标记：不是真实用户，也不参与用户隔离（报告是共享产出）
SYSTEM_USER_ID = 'system'


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


async def ensure_system_task(
  preset_key: str,
  *,
  name: str,
  description: str,
  cron_expr: str,
  prompt: str,
  task_mode: str = 'full_analysis',
  thread_strategy: str = 'new',
) -> ScheduledTask:
  """幂等维护一条**系统预设任务**（分析报告用；`created_by='system'`，全平台只一条）。

  - 不存在 → 建（cron 任务，下次运行按本地时区算）
  - 已存在 → 同步 name/description/cron/prompt/task_mode（模板升级自动生效）；
    **不动** enabled 与 next_run_at（重启不该打乱已排好的计划），只有 cron 变了才重算下次运行
  """
  coll = mongodb_manager.db[TASKS_COLLECTION]
  existing = await coll.find_one({'template_id': preset_key, 'created_by': SYSTEM_USER_ID})
  now = datetime.now(timezone.utc)
  if not existing:
    task = ScheduledTask(
      task_id=uuid.uuid4().hex,
      name=name,
      description=description,
      schedule_type=ScheduleType.CRON,
      cron_expr=cron_expr,
      prompt=prompt,
      task_mode=task_mode,
      thread_strategy=thread_strategy,
      enabled=True,
      template_id=preset_key,
      created_by=SYSTEM_USER_ID,
      next_run_at=compute_next_run(ScheduleType.CRON, None, None, None, cron_expr),
    )
    await coll.insert_one(task.model_dump())
    logger.info('预设报告任务已创建: %s（%s）', name, preset_key)
    return task

  updates: dict = {
    'name': name, 'description': description, 'prompt': prompt,
    'task_mode': task_mode, 'thread_strategy': thread_strategy, 'updated_at': now,
  }
  cron_changed = str(existing.get('cron_expr') or '') != cron_expr
  if cron_changed:
    updates['cron_expr'] = cron_expr
    updates['next_run_at'] = compute_next_run(
      ScheduleType.CRON, existing.get('last_run_at'), None, None, cron_expr,
    )
  await coll.update_one({'task_id': existing['task_id']}, {'$set': updates})
  return ScheduledTask(**{**existing, **updates})


async def disable_stale_system_tasks(valid_keys: list[str], *,
                                     prefix: str = '',
                                     exclude_prefixes: tuple[str, ...] = ()) -> list[str]:
  """把**已下线预设**对应的系统任务置为停用（**不删除**，留痕便于回溯）。

  为什么需要：预设是代码里的定义，改版后旧 key（如上一版的 `event_ticket_weekly`）在库里
  仍是 enabled 的定时任务，会继续按旧模板跑、产出重复/过期报告（2026-10-13 实测遗留 2 条）。
  返回被停用的 template_id 列表。

  `prefix` 限定只处理某一族预设（如 `incident__`），`exclude_prefixes` 反向排除别的族
  —— 两族预设各有自己的 ensure，不能互相把对方当「已下线」停掉。
  """
  coll = mongodb_manager.db[TASKS_COLLECTION]
  stale: list[str] = []
  async for d in coll.find({'created_by': SYSTEM_USER_ID, 'enabled': True}):
    key = d.get('template_id') or ''
    if not key or key in valid_keys:
      continue
    if prefix and not key.startswith(prefix):
      continue
    if any(key.startswith(p) for p in exclude_prefixes):
      continue
    stale.append(key)
  if stale:
    await coll.update_many(
      {'created_by': SYSTEM_USER_ID, 'template_id': {'$in': stale}},
      {'$set': {'enabled': False, 'updated_at': datetime.now(timezone.utc)}},
    )
  return stale


async def enable_system_tasks(cron_by_key: dict[str, str]) -> list[str]:
  """把**在清单内却被停用**的系统预设任务恢复启用（每个 key 只恢复最早的一条）。

  为什么需要：`ensure_system_task()` 刻意不碰 enabled（重启不该打乱人工设置），
  而 `disable_stale_system_tasks()` 会批量停用「不在清单里」的预设 —— 2026-10-25 生产事故：
  园区发现接口失败 → 清单退化成 9 条全国口径 → 40 条园区预设被停用，重启也不自愈，
  报告页「空间」下拉（由预设清单推导）里园区全部消失。

  `next_run_at` **必须重算**：被停用期间它不推进，直接恢复等于一次性放出一堆过期任务，
  一启动就并发触发几十份报告。重复副本保持停用（恢复只取每个 key 最早的一条，与
  `disable_duplicate_system_tasks()` 同一口径）。返回被恢复的 key。
  """
  if not cron_by_key:
    return []
  coll = mongodb_manager.db[TASKS_COLLECTION]
  now = datetime.now(timezone.utc)
  revived: list[str] = []
  seen: set[str] = set()
  cursor = coll.find(
    {'created_by': SYSTEM_USER_ID, 'enabled': False,
     'template_id': {'$in': list(cron_by_key)}},
    {'task_id': 1, 'template_id': 1},
  ).sort('_id', 1)
  async for d in cursor:
    key = d.get('template_id') or ''
    if key in seen:
      continue
    seen.add(key)
    await coll.update_one(
      {'_id': d['_id']},
      {'$set': {
        'enabled': True,
        'next_run_at': compute_next_run(ScheduleType.CRON, None, None, None, cron_by_key[key]),
        'updated_at': now,
      }},
    )
    revived.append(key)
  return revived


async def disable_duplicate_system_tasks(valid_keys: list[str]) -> list[str]:
  """停用同一预设 key 的**重复副本**（每个 key 按 `_id` 保留最早一条）。

  为什么需要：4 个 uvicorn worker 启动时会并发执行 `ensure_system_task()`，而它是
  「find_one 查不到 → insert_one」；都查到空就各插一条 → 同一预设被建 2~3 份
  （2026-10-19 实测 49 个预设变成 87 条启用任务）。重复副本到点同时运行：既会
  重复产出报告，又会并发打同一个 pen 接口触发「请勿重复请求」。

  与 `disable_stale_system_tasks` 一样**只停用不删除**（留痕可人工恢复）。
  返回被停用的 task_id 列表。多个 worker 并发执行也安全：保留规则是确定性的。
  """
  if not valid_keys:
    return []
  coll = mongodb_manager.db[TASKS_COLLECTION]
  seen: set[str] = set()
  duplicates: list[str] = []
  cursor = coll.find(
    {'created_by': SYSTEM_USER_ID, 'enabled': True,
     'template_id': {'$in': list(valid_keys)}},
    {'task_id': 1, 'template_id': 1},
  ).sort('_id', 1)
  async for d in cursor:
    key = d.get('template_id') or ''
    if key in seen:
      task_id = d.get('task_id')
      if task_id:
        duplicates.append(task_id)
    else:
      seen.add(key)
  if duplicates:
    await coll.update_many(
      {'task_id': {'$in': duplicates}},
      {'$set': {'enabled': False, 'updated_at': datetime.now(timezone.utc)}},
    )
  return duplicates


def _runs_query(
  task_ids: list[str], preset_keys: list[str] | None, since,
  period_from: str | None = None, period_to_excl: str | None = None,
) -> dict:
  """产出记录的筛选条件 —— `list_runs_for_tasks` 与 `count_runs_by_status` **共用**。

  抽出来是防口径漂移：列表页的「共 N 份」与「成功/失败」必须来自**同一套筛选**
  （曾出现过前端拿"已加载那一页"去凑成功/失败数、与总数对不上的问题）。

  period_from / period_to_excl：按 `period_key` 的字典序区间筛（不合规检查的期号形如
  `YYYY-MM-DD-n/-d`，日期序即字符串序；右端传「结束日 + 1 天」做开区间，才能含当天两班）。
  """
  query: dict = {'task_id': {'$in': list(task_ids)}}
  if preset_keys is not None:
    query['preset_key'] = {'$in': list(preset_keys)}
  if since is not None:
    query['triggered_at'] = {'$gte': since}
  if period_from or period_to_excl:
    cond: dict = {}
    if period_from:
      cond['$gte'] = period_from
    if period_to_excl:
      cond['$lt'] = period_to_excl
    query['period_key'] = cond
  return query


async def list_runs_for_tasks(
  task_ids: list[str], limit: int = 20, offset: int = 0, *,
  preset_keys: list[str] | None = None, since=None,
  period_from: str | None = None, period_to_excl: str | None = None,
) -> tuple[list[ScheduledTaskRun], int]:
  """跨任务的运行记录 + 总数（报告页的「期刊目录」用：支持筛选与分页）。

  ``total`` 是**产出份数**（不是"期数"—— 一期之内可能有多份产出）。

  preset_keys：只取这些预设的产出（按工单类型/空间/周期筛出来的 key）
  since：只要该时间之后的产出（按 triggered_at，UTC-aware）
  period_from / period_to_excl：按 `period_key` 日期区间筛（见 `_runs_query`）
  """
  if not task_ids:
    return [], 0
  query = _runs_query(task_ids, preset_keys, since, period_from, period_to_excl)
  coll = mongodb_manager.db[RUNS_COLLECTION]
  total = await coll.count_documents(query)
  cursor = coll.find(query).sort('triggered_at', -1).skip(offset).limit(limit)
  docs = await cursor.to_list(limit)
  return [ScheduledTaskRun(**doc) for doc in docs], total


async def count_runs_by_status(
  task_ids: list[str], *, preset_keys: list[str] | None = None, since=None,
  period_from: str | None = None, period_to_excl: str | None = None,
) -> dict[str, int]:
  """按状态统计产出份数（**全量口径，不受分页影响**）。

  报告页要在筛选条上显示「✓x ✗y」；原先前端只能数"已加载那一页"，与总数对不上
  （数据超过一页就会自相矛盾）。这里用同一套筛选条件做一次聚合，返回 {状态: 份数}。
  """
  if not task_ids:
    return {}
  query = _runs_query(task_ids, preset_keys, since, period_from, period_to_excl)
  cursor = mongodb_manager.db[RUNS_COLLECTION].aggregate([
    {'$match': query},
    {'$group': {'_id': '$status', 'n': {'$sum': 1}}},
  ])
  out: dict[str, int] = {}
  async for row in cursor:
    status = row.get('_id')
    out[str(getattr(status, 'value', status))] = int(row.get('n', 0))
  return out


async def list_tasks(
  created_by: str, limit: int = 100, offset: int = 0,
) -> list[ScheduledTask]:
  """按创建人列出任务（**用户隔离**：定时任务只属于创建人，用户之间无关联）。

  created_by 必填——以前这里不带过滤，等于把所有人的任务都列出来了（2026-10-10 修）。
  全量视图只有调度器需要，走 `get_due_tasks()`（内部路径，不受此限）。
  """
  cursor = (
    mongodb_manager.db[TASKS_COLLECTION]
    .find({'created_by': created_by})
    .sort('created_at', -1).skip(offset).limit(limit)
  )
  docs = await cursor.to_list(limit)
  return [ScheduledTask(**doc) for doc in docs]


async def get_task(task_id: str) -> Optional[ScheduledTask]:
  doc = await mongodb_manager.db[TASKS_COLLECTION].find_one({'task_id': task_id})
  return ScheduledTask(**doc) if doc else None


async def list_tasks_by_creator(user_id: str, limit: int = 100) -> list[ScheduledTask]:
  """按创建人列出任务（对话工具 list 按用户隔离）——与 `list_tasks` 同一份实现。"""
  return await list_tasks(user_id, limit=limit)


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

async def create_run(
  task_id: str, run_id: str = '', thread_id: str = '', *,
  preset_key: str = '', period_key: str = '', period_label: str = '',
) -> ScheduledTaskRun:
  rid = run_id or uuid.uuid4().hex
  run = ScheduledTaskRun(
    run_id=rid, task_id=task_id, thread_id=thread_id,
    preset_key=preset_key, period_key=period_key, period_label=period_label,
  )
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
  thread_id: str | None = None,
  check_counts: dict[str, int] | None = None,
) -> None:
  updates: dict = {
    'status': status,
    'final_reply': final_reply,
    'artifact_urls': artifact_urls or [],
    'error': error,
    'duration_ms': duration_ms,
  }
  if check_counts is not None:
    # 只有产生命中数的流水线（不合规检查）才写，其他任务不凭空多一个空字段
    updates['check_counts'] = check_counts
  if thread_id:
    # 重试场景：把**成功那次**的 thread 落库（create_run 写的是第一次的），供「查看对话」
    updates['thread_id'] = thread_id
  await mongodb_manager.db[RUNS_COLLECTION].update_one(
    {'run_id': run_id}, {'$set': updates},
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

async def alert_user_ids() -> list[str]:
  """预设报告失败通知的接收人（运维）。

  配置 `SCHEDULED_TASK_ALERT_USER_IDS` 优先；为空回退内置管理员角色
  （`roles` 含 admin/root）的 ACTIVE 用户。预设的 created_by='system'，
  原样发给它等于没有任何登录用户看得到（2026-10-26 24 份报告集体失败、
  页面上零提示）。查库失败返回空，由调用方退回创建人。
  """
  configured = [u.strip() for u in (settings.SCHEDULED_TASK_ALERT_USER_IDS or '').split(',')]
  configured = [u for u in configured if u]
  if configured:
    return configured
  try:
    cursor = mongodb_manager.db.users.find(
      {'status': 'ACTIVE', 'roles': {'$in': ['admin', 'root']}}, {'user_id': 1})
    docs = await cursor.to_list(50)
  except Exception:
    logger.warning('查询管理员账号失败，预设失败通知退回任务创建人', exc_info=True)
    return []
  return [str(d['user_id']) for d in docs if d.get('user_id')]


def alert_batch_key(kind: str = 'preset-fail') -> str:
  """失败通知的合并批次键：同一本地自然日、同一类失败聚成一条。"""
  return f'{kind}:{datetime.now(_get_task_tz()).strftime("%Y-%m-%d")}'


async def create_notification(
  *,
  user_id: str,
  task_id: str,
  task_name: str,
  run_id: str,
  status: str,
  title: str,
  body: str = '',
  batch_key: str = '',
) -> None:
  """写一条任务完成通知（铃铛轮询 + 首页动态共用）。

  `batch_key` 非空时按「接收人 + 批次」聚合成一条（`$inc` 计数、保留首个错误与任务名）：
  上游一次故障会让几十份预设报告同时失败，逐份一条会把铃铛刷满。
  """
  coll = mongodb_manager.db[NOTIFICATIONS_COLLECTION]
  if not batch_key:
    notif = ScheduledTaskNotification(
      user_id=user_id,
      task_id=task_id,
      task_name=task_name,
      run_id=run_id,
      status=status,
      title=title,
      body=body,
    )
    await coll.insert_one(notif.model_dump())
    return

  await coll.update_one(
    {'user_id': user_id, 'batch_key': batch_key},
    {
      '$setOnInsert': {
        'notification_id': uuid.uuid4().hex,
        'task_id': task_id,
        'task_name': task_name,
        'run_id': run_id,
        'status': status,
        'body': body,
        'created_at': datetime.now(timezone.utc),
      },
      '$inc': {'count': 1},
    },
    upsert=True,
  )
  doc = await coll.find_one({'user_id': user_id, 'batch_key': batch_key}, {'count': 1})
  n = int((doc or {}).get('count') or 1)
  await coll.update_one(
    {'user_id': user_id, 'batch_key': batch_key},
    {'$set': {'title': f'{title}（本批 {n} 份）'}},
  )


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

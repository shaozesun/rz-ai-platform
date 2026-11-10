"""Mongo 存储：一任务一条结果 + 一批勾选一条 run（只做索引，不存行）。

- `infrared_quality_tasks`：`_id = task_id`，任务元信息 + `checked_at/checked_by/
  result_status/error/elapsed/counts/rows[]`。一个任务最多几百行（约 100~200KB），
  远低于 16MB，重检整条覆盖。
- `infrared_quality_runs`：`run_id`，记录本次勾了哪些任务与会话级汇总。

**已检测状态只认 `infrared_quality_tasks`**：任务列表接口与它合并后给出
`checked_at`，页面据此默认只勾未检测的任务。
"""

from datetime import datetime, timezone

from config.mongodb_conn import mongodb_manager

TASKS = 'infrared_quality_tasks'
RUNS = 'infrared_quality_runs'


def _tasks():
  return mongodb_manager.db[TASKS]


def _runs():
  return mongodb_manager.db[RUNS]


def _aware(value):
  """Mongo 读回来是 naive UTC：补时区，否则前端按本地时间显示会早 8 小时。"""
  if isinstance(value, datetime):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
  if isinstance(value, dict):
    return {k: _aware(v) for k, v in value.items()}
  if isinstance(value, list):
    return [_aware(v) for v in value]
  return value


async def save_task(task_id: str, doc: dict) -> None:
  body = dict(doc)
  body.pop('_id', None)
  await _tasks().replace_one({'_id': task_id}, body, upsert=True)


async def get_task(task_id: str) -> dict | None:
  doc = await _tasks().find_one({'_id': task_id})
  return _aware(doc) if doc else None


_COUNT_KEYS = ('合格', '待复核', '不合格', '检测失败')


def _counts_expr() -> dict:
  """服务端按「有图片」的行重算四档计数。

  历史文档里存着「说明=无图片」的行（未巡检的台账设备），它们不是检测结论，
  统计时剔除；新文档本来就没有这类行，同一份聚合照常成立。只回计数不回 rows。
  """
  rows = {'$ifNull': ['$rows', []]}
  return {
    key: {'$size': {'$filter': {
      'input': rows,
      'as': 'r',
      'cond': {'$and': [
        {'$eq': ['$$r.判定', key]},
        {'$ne': ['$$r.说明', '无图片']},
      ]},
    }}}
    for key in _COUNT_KEYS
  }


def _photo_count_expr() -> dict:
  """历史文档没存 `photo_count`：去掉「无图片」的行数就是当时有图片的设备数。"""
  return {'$ifNull': ['$photo_count', {'$size': {'$filter': {
    'input': {'$ifNull': ['$rows', []]},
    'as': 'r',
    'cond': {'$ne': ['$$r.说明', '无图片']},
  }}}]}


async def list_checked(dept_id: str) -> list[dict]:
  """该部门已检测任务的状态（不含 rows；计数按有图片的行重算）。"""
  pipeline = [
    {'$match': {'dept_id': dept_id}},
    {'$project': {
      'checked_at': 1, 'result_status': 1, 'error': 1,
      'photo_count': _photo_count_expr(),
      'counts': _counts_expr(),
    }},
  ]
  out: list[dict] = []
  async for doc in _tasks().aggregate(pipeline):
    out.append({
      'task_id': str(doc.get('_id') or ''),
      'checked_at': _aware(doc.get('checked_at')),
      'result_status': doc.get('result_status') or '',
      'counts': doc.get('counts') or {},
      'photo_count': doc.get('photo_count'),
      'error': doc.get('error') or '',
    })
  return out


def sum_counts(items: list[dict]) -> dict[str, int]:
  """多任务计数求和（run 文档里存的是旧口径，汇总时按重算后的任务计数来）。"""
  total = {key: 0 for key in _COUNT_KEYS}
  for counts in items:
    for key in total:
      total[key] += int((counts or {}).get(key) or 0)
  return total


async def create_run(run: dict) -> None:
  body = dict(run)
  body.setdefault('created_at', datetime.now(timezone.utc))
  await _runs().insert_one(body)


async def finish_run(run_id: str, *, status: str, counts: dict, error: str = '',
                     elapsed: float = 0, task_ids: list[str] | None = None) -> None:
  update = {
    'status': status,
    'counts': counts,
    'error': error,
    'elapsed': elapsed,
    'finished_at': datetime.now(timezone.utc),
  }
  if task_ids is not None:
    update['task_ids'] = task_ids
  await _runs().update_one({'run_id': run_id}, {'$set': update})


async def get_run(run_id: str) -> dict | None:
  doc = await _runs().find_one({'run_id': run_id})
  return _aware(doc) if doc else None


async def latest_run(dept_id: str) -> dict | None:
  doc = await _runs().find_one(
    {'dept_id': dept_id, 'status': {'$in': ['success', 'partial']}},
    sort=[('created_at', -1)],
  )
  return _aware(doc) if doc else None


def run_public(doc: dict | None) -> dict | None:
  if not doc:
    return None
  created = doc.get('created_at')
  return {
    'run_id': doc.get('run_id') or '',
    'status': doc.get('status') or '',
    'created_at': created.isoformat() if isinstance(created, datetime) else created,
    'counts': doc.get('counts') or {},
    'error': doc.get('error') or '',
    'elapsed': doc.get('elapsed') or 0,
    'task_ids': doc.get('task_ids') or [],
  }

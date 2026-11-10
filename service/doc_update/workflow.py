"""文档更新检查工作流：取快照 → 落库 → 比对 → 写运行记录 + 通知。

形态照 `service/scheduled_task/compliance/workflow.py`：
`create_run` → `perform_sync` → `finish_run(check_counts=…)` → 失败通知。
**不抛异常**：成败由运行记录与通知体现。本功能无产物文件（只落快照）。
"""

import logging
import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from config.settings import settings
from models.scheduled_task import RunStatus
from service.doc_update import fetch, store
from service.doc_update.verdict import evaluate, summary_text
from service.scheduled_task import repository as repo

logger = logging.getLogger(__name__)


def _tz() -> ZoneInfo:
  return ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE or 'Asia/Shanghai')


def period_key() -> str:
  return datetime.now(_tz()).strftime('%Y-%m')


def period_label() -> str:
  return datetime.now(_tz()).strftime('%Y年%m月')


def _dept_scope() -> bool:
  return (settings.DOC_UPDATE_SCOPE or 'dept').lower() != 'tree'


async def _take_snapshot(previous: dict | None, progress, only=None,
                         dept_names: list[str] | None = None,
                         companies: list[str] | None = None) -> dict:
  if _dept_scope():
    return await fetch.take_dept_snapshot(
      previous=previous, progress=progress, only=only, dept_names=dept_names,
      companies=companies,
    )
  return await fetch.take_snapshot(previous=previous, progress=progress, only=only)


async def perform_sync(created_by: str = '', progress=None, persist: bool = True,
                       dept_names: list[str] | None = None,
                       companies: list[str] | None = None) -> dict:
  """取一期快照 → 落库 → 与**上一期**比对。返回 `{snapshot, verdict}`。

  `persist=False` 只算不写（CLI 不加 `--yes` 用）。
  参照只有上一期（`store.previous`）：没有上期就是基线期，**不拿本月早先的检查结果当参照**。
  """
  key = period_key()
  # last_changed_at 结转优先用本期已有快照（同月重跑不丢上次变更时间），否则用上期
  current = await store.same_period(key)
  carry = current or await store.previous(key)
  snapshot = await _take_snapshot(carry, progress, dept_names=dept_names,
                                  companies=companies)
  if persist:
    await store.save(snapshot, created_by=created_by)
  previous = await store.previous(key)
  verdict = evaluate(snapshot, previous)
  return {'snapshot': snapshot, 'verdict': verdict}


async def ensure_initial_sync() -> None:
  """显式开启自动基线后跑一次（**从没有过部门口径快照**才跑，不按月份重跑）。

  与手动同步共用 `doc_update:sync` 锁，多 worker 只有一个真正执行。
  失败只记日志，页面靠「立即检查」兜底。
  """
  if not _dept_scope():
    return
  latest = await store.latest()
  if latest and latest.get('depts'):
    return

  from service.doc_update import sync_state

  lease = sync_state.acquire('auto', 'system')
  if lease is None:
    return
  try:
    await perform_sync(created_by='system', progress=lease.update)
    logger.info('文档更新检查首期基线已自动生成')
  except Exception as e:  # noqa: BLE001  自动跑失败不拦启动
    logger.warning('文档更新检查首期自动执行失败: %s', e)
  finally:
    lease.release()


async def build_overview(period_key: str | None = None) -> dict:
  """页面/工具读：指定期（默认最新）快照 + 与**上一期**的比对结果（不下载）。

  没有上期就是基线期（`evaluate(…, None)` 自带 `baseline`），**不拿本月早先的检查结果当参照**。
  `dept_columns`（固定部门列）、`catalog`（25 条月度项）与 `periods`（历史期）**始终返回**。
  指定期不存在时 `latest=None` + `missing_period`，由页面提示「该期没有快照」。
  历史期的 `now` 取该快照的采集时间，「上次变更 N 天前」是站在那一期看的。
  """
  from service.doc_update import depts
  from service.doc_update.catalog import MONTHLY_ITEMS

  columns = await depts.dept_columns()
  catalog = [{'seq': i.seq, 'number': i.number_label, 'name': i.name}
             for i in MONTHLY_ITEMS]
  periods = await store.list_periods()

  if period_key:
    snapshot = await store.same_period(period_key)
    if not snapshot:
      return {'latest': None, 'verdict': None, 'missing_period': period_key,
              'dept_columns': columns, 'catalog': catalog, 'periods': periods}
    previous = await store.previous(period_key)
    verdict = evaluate(snapshot, previous, now=snapshot.get('taken_at'))
    return {'latest': snapshot, 'verdict': verdict,
            'dept_columns': columns, 'catalog': catalog, 'periods': periods}

  latest = await store.latest()
  if not latest:
    return {'latest': None, 'verdict': evaluate(None, None),
            'dept_columns': columns, 'catalog': catalog, 'periods': periods}
  previous = await store.previous(latest.get('period_key') or '')
  return {'latest': latest, 'verdict': evaluate(latest, previous),
          'dept_columns': columns, 'catalog': catalog, 'periods': periods}


def _entry_key(entry: dict) -> tuple[str, str, str, str]:
  """收窄用的键：部门口径带上公司 + dept_id（同名部门在不同公司是两份）。"""
  dept = str(entry.get('dept_id') or entry.get('dept_name') or '')
  return (str(entry.get('company_id') or ''), dept, entry.get('number'),
          entry.get('name'))


def _dept_key(entry: dict) -> tuple[str, str]:
  return (str(entry.get('company_id') or ''),
          str(entry.get('dept_id') or entry.get('dept_name') or ''))


def subset_verdict(verdict: dict, seqs: set[int]) -> dict:
  """只保留指定项的比对结果，计数/待复核/变更同步收窄（工具按名字查时用）。"""
  items = [i for i in verdict.get('items') or [] if i.get('seq') in seqs]
  keys = {_entry_key(i) for i in items}
  counts = {k: 0 for k in ('updated', 'stale', 'missing', 'failed', 'baseline')}
  for item in items:
    counts[item['status']] = counts.get(item['status'], 0) + 1
  out = dict(verdict)
  out.update({
    'items': items,
    'counts': counts,
    'total': len(items),
    'warnings': [w for w in verdict.get('warnings') or []
                 if _entry_key(w) in keys],
  })
  if verdict.get('depts'):
    depts = []
    for dept in verdict.get('depts') or []:
      dept_key = _dept_key(dept)
      dept_items = [i for i in items if _dept_key(i) == dept_key]
      if not dept_items:
        continue
      dept_counts = {k: 0 for k in counts}
      for item in dept_items:
        dept_counts[item['status']] = dept_counts.get(item['status'], 0) + 1
      depts.append({**dept, 'counts': dept_counts, 'total': len(dept_items)})
    out['depts'] = depts
    out['dept_count'] = len(depts)
  changes = dict(verdict.get('changes') or {})
  changes['changed'] = [c for c in changes.get('changed') or []
                        if _entry_key(c) in keys]
  out['changes'] = changes
  return out


async def refresh_items(items, created_by: str = '', progress=None,
                        persist: bool = True,
                        dept_names: list[str] | None = None,
                        companies: list[str] | None = None) -> dict:
  """只刷新指定的清单项（按文件名查单项），其余项保持上次结果。

  参照始终是**上一期**：有上期时，本期已有快照就合并落库、没有就拼到上期上做临时比对
  （**不落库**，不制造半份基线，否则其余 24 项会被算成「未找到」）；
  没有上期就是基线期，本期有快照就合并落库、否则只算不写。
  `persist=False` 时无论有没有本期快照都不写库（CLI 不加 `--yes` 用）。
  `dept_names` 限定部门口径下只刷新哪几个部门。
  """
  key = period_key()
  current = await store.same_period(key)
  carry = current or await store.previous(key)
  patch = await _take_snapshot(carry, progress, only=items, dept_names=dept_names,
                               companies=companies)
  previous = await store.previous(key)
  seqs = {i.seq for i in items}

  if previous:
    if current:
      merged = fetch.merge_snapshot(current, patch)
      if persist:
        await store.save(merged, created_by=created_by)
      verdict, stored = evaluate(merged, previous), persist
    else:
      verdict = evaluate(fetch.merge_snapshot(previous, patch), previous)
      stored = False
  elif current:
    # 基线期：本期已有快照就合并落库，结论仍是「基线」
    merged = fetch.merge_snapshot(current, patch)
    if persist:
      await store.save(merged, created_by=created_by)
    verdict, stored = evaluate(merged, None), persist
  else:
    verdict, stored = evaluate(patch, None), False

  return {
    'snapshot': patch,
    'verdict': subset_verdict(verdict, seqs),
    'stored': stored,
    'items': [i.number_label for i in items],
  }


def _summary(result: dict) -> str:
  verdict = result['verdict']
  snapshot = result['snapshot']
  lines = [
    f'《平台每月所需更新文件目录》{verdict.get("period_label") or period_label()}'
    f' 文档更新检查。',
    f'- {summary_text(verdict)}',
    f'- 本次比对 {snapshot.get("file_count", 0)} 个文件，'
    f'耗时 {snapshot.get("elapsed", 0)} 秒。',
  ]
  if verdict.get('baseline'):
    if verdict.get('scope') == 'dept':
      lines.append('- 首轮：按期提交型已直接判定；原地更新型无上期快照，记为基线。')
    else:
      lines.append('- 首期只建基线，下期起比对是否更新。')
  counts = verdict.get('counts') or {}
  if counts.get('missing'):
    lines.append(f'- 未找到不是「未更新」：{counts["missing"]} 项未查询到相关文件。')
  if counts.get('failed'):
    lines.append(f'- 下载失败也不是「未更新」：{counts["failed"]} 项取不到字节，需人工复核。')
  return '\n'.join(lines)


async def _notify_outcome(task, run, status, final_text: str, error: str) -> None:
  recipients = [task.created_by]
  batch_key = ''
  if status == RunStatus.FAILED and task.created_by == repo.SYSTEM_USER_ID:
    recipients = await repo.alert_user_ids() or [task.created_by]
    batch_key = repo.alert_batch_key()

  if batch_key:
    title = '文档更新检查执行失败'
    body = f'「{task.name}」{(final_text or error).strip()[:120]}'
  else:
    title = (f"定时任务「{task.name}」执行"
             f"{'成功' if status == RunStatus.SUCCESS else '失败'}")
    body = (final_text or error).strip()[:80]

  for user_id in recipients:
    await repo.create_notification(
      user_id=user_id, task_id=task.task_id, task_name=task.name, run_id=run.run_id,
      status=status.value, title=title, body=body, batch_key=batch_key,
    )


async def run_doc_update_task(task) -> None:
  """跑一期快照比对。**不抛异常**（全部落库）。"""
  run_id = uuid.uuid4().hex
  thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
  run = await repo.create_run(
    task.task_id, run_id=run_id, thread_id=thread_id, preset_key=task.template_id,
    period_key=period_key(), period_label=period_label(),
  )
  start_ts = time.time()
  error, final_text = '', ''
  counts: dict[str, int] | None = None

  try:
    result = await perform_sync(created_by=task.created_by)
    counts = result['verdict']['counts']
    final_text = _summary(result)
  except Exception as e:
    logger.exception('文档更新检查工作流失败 task_id=%s', task.task_id)
    error = f'{type(e).__name__}: {str(e)[:400]}'

  duration_ms = int((time.time() - start_ts) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await repo.finish_run(run.run_id, status=status, final_reply=final_text,
                        artifact_urls=[], error=error, duration_ms=duration_ms,
                        thread_id=thread_id, check_counts=counts)
  await _notify_outcome(task, run, status, final_text, error)
  logger.info('文档更新检查完成 task_id=%s status=%s duration_ms=%s',
              task.task_id, status.value, duration_ms)

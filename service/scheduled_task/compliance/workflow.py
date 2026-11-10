"""不合规检查定时任务：班次窗口 → 四项检查 → xlsx → 落库。

落库与产物路径对齐事件统计表 / 报告工作流：`finish_run` 必写
status/artifact_urls/error/duration_ms；产物落
`{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/`，**文件名纯 ASCII**
（`resolve_artifact_path` 不解码 URL，中文名会静默 404）。

全 0 行**不是失败**：照出空表（表头完整），摘要写「本班次未发现不合规项」——
页面上能看出这一班确实查过了。
"""

import logging
import time
import uuid

from core.sandbox.datasets import load_dataset_dir, save_dataset
from models.scheduled_task import RunStatus
from service.scheduled_task import repository as repo
from service.scheduled_task.compliance import checks, xlsx

logger = logging.getLogger(__name__)

_ARTIFACT_PREFIX = '/api/v1/chat/agent/artifacts/'


def file_name(window: dict) -> str:
  """`compliance_20261027_night.xlsx`（纯 ASCII，URL 段与磁盘逐字相同）。"""
  return f"compliance_{window['end'].strftime('%Y%m%d')}_{window['kind']}.xlsx"


def workbook_title(window: dict) -> str:
  return f"{checks.TITLE} · {window['label']}"


def summary_text(window: dict, result: dict, name: str, thread_id: str,
                 dataset_id: str) -> str:
  """`final_reply`：首行抬头 + 一条命中汇总（列表页 `teaser` 读它）+ `<file>` 标签。"""
  total = sum(item['rows'] for item in result['summary'])
  counts = ' · '.join(f"{item['name']} {item['rows']}" for item in result['summary'])
  url = f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{name}'
  lines = [
    f'《{checks.TITLE}》{window["label"]}。',
    f'- 命中 {total} 条：{counts}',
  ]
  if not total:
    lines.append('- 本班次未发现不合规项。')
  lines.append(f'完整表格见 xlsx 产物：<file url="{url}" name="{name}"/>')
  return '\n'.join(lines)


async def _notify_outcome(task, run, status, final_text: str, error: str) -> None:
  """写完成通知；预设失败要发给运维（`created_by='system'` 谁都看不到）。

  同一天的预设失败按批次聚合成一条，与报告预设同一口径。
  """
  recipients = [task.created_by]
  batch_key = ''
  if status == RunStatus.FAILED and task.created_by == repo.SYSTEM_USER_ID:
    recipients = await repo.alert_user_ids() or [task.created_by]
    batch_key = repo.alert_batch_key()

  if batch_key:
    title = '不合规检查执行失败'
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


async def run_compliance_task(task) -> None:
  """跑一期不合规检查。**不抛异常**（全部落库），成败由运行记录与通知体现。"""
  run_id = uuid.uuid4().hex
  thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
  window = checks.shift_window()
  run = await repo.create_run(task.task_id, run_id=run_id, thread_id=thread_id,
                              preset_key=task.template_id, period_key=window['key'],
                              period_label=window['label'])
  start_ts = time.time()
  error, final_text, artifacts = '', '', []
  counts: dict[str, int] | None = None

  try:
    result = checks.run_all(window['start_s'], window['end_s'])
    params = {
      'period': window['key'],
      'start': window['start'].isoformat(),
      'end': window['end'].isoformat(),
    }
    info = save_dataset(thread_id, 'compliance_alarm', params, result['summary'],
                        dataset_name='compliance')
    dataset_id = info['dataset_id']
    ds_dir = load_dataset_dir(thread_id, dataset_id)
    name = file_name(window)
    xlsx.write_workbook(ds_dir / name, result, title=workbook_title(window),
                        period=window['label'])

    url = f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{name}'
    artifacts = [url]
    final_text = summary_text(window, result, name, thread_id, dataset_id)
    counts = {item['key']: item['rows'] for item in result['summary']}
  except Exception as e:
    logger.exception('不合规检查工作流失败 task_id=%s', task.task_id)
    error = f'{type(e).__name__}: {str(e)[:400]}'

  duration_ms = int((time.time() - start_ts) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await repo.finish_run(run.run_id, status=status, final_reply=final_text,
                        artifact_urls=artifacts, error=error, duration_ms=duration_ms,
                        thread_id=thread_id, check_counts=counts)
  await _notify_outcome(task, run, status, final_text, error)
  logger.info('不合规检查工作流完成 task_id=%s period=%s status=%s duration_ms=%s',
              task.task_id, window['key'], status.value, duration_ms)

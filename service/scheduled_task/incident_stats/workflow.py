"""事件统计表定时任务：补匹配 → 配对取数 → xlsx → 落库。

落库与产物路径对齐报告工作流：`finish_run` 必写 status/artifact_urls/error/duration_ms，
产物落 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/`，**文件名纯 ASCII**
（`resolve_artifact_path` 不解码 URL，中文名会静默 404）。本期 0 条通报不是失败：
照出表头完整的 xlsx，`final_reply` 写明「本期无通报事件」。
"""

import asyncio
import logging
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config.settings import settings
from core.sandbox.datasets import load_dataset_dir, save_dataset
from models.scheduled_task import RunStatus
from service.scheduled_task import repository as repo
from service.scheduled_task.incident_stats import build, feishu, presets, rules, sync, xlsx

logger = logging.getLogger(__name__)

_TZ = ZoneInfo('Asia/Shanghai')
_ARTIFACT_PREFIX = '/api/v1/chat/agent/artifacts/'


def _bounds(start_d: date, end_d: date) -> tuple[int, int]:
  """自然日窗口 [start 00:00, end 次日 00:00) → 秒级 epoch。"""
  start = datetime.combine(start_d, datetime.min.time(), tzinfo=_TZ)
  end = datetime.combine(end_d + timedelta(days=1), datetime.min.time(), tzinfo=_TZ)
  return int(start.timestamp()), int(end.timestamp())


def _period_label(start_d: date) -> tuple[str, str]:
  iso_year, iso_week, _ = start_d.isocalendar()
  return f'{iso_year:04d}-W{iso_week:02d}', f'{iso_year} 第 {iso_week} 周'


def _file_name(period_key: str, park: str) -> str:
  if park in (presets.ALL_PARKS, ''):
    code = 'all'
  else:
    code = rules.park_of(park)[1]
    code = code.lower() if code.isascii() else 'all'
  return f'incident_{period_key.lower().replace("-", "")}_{code}.xlsx'


class _NoParkGroups(Exception):
  """一个园区群都没解析到：配置/进群问题，必须判失败而不是出健康空表。"""


def _merge_rows(parts: list[list[dict]]) -> list[dict]:
  """多园区行合并成一张表：按发生时间升序、序号全局重排 1..N。"""
  rows = [row for part in parts for row in part]
  rows.sort(key=lambda r: (r.get('发生时间') or '', r.get('事件名称') or ''))
  for i, row in enumerate(rows, 1):
    row['序号'] = i
  return rows


def _collect_rows(chats: dict[str, str], start_s: int, end_s: int,
                  fetch=None) -> tuple[list[dict], list[str]]:
  """逐园区取数再合并；单园区异常记日志、不中断其余园区。

  返回 `(rows, failed_parks)`。`fetch` 默认 `build.build_rows`，自检可注入假数据。
  """
  fetch = fetch or build.build_rows
  parts: list[list[dict]] = []
  failed: list[str] = []
  for park, chat_id in chats.items():
    try:
      parts.append(fetch(start_s, end_s, park, [chat_id]))
    except Exception as e:
      logger.warning('事件统计表取数失败 park=%s: %s', park, e)
      failed.append(park)
  return _merge_rows(parts), failed


def _shift_window() -> tuple[int, int, str, str, str]:
  """最近一个**已结束**的班次：`(start_s, end_s, period_key, period_label, window_desc)`。

  直接复用 `compliance.checks.shift_window()`：两个功能同属值守视角，期号必须对得上
  （`2026-10-27-n` / `-d`），否则页面上两张卡说的不是同一班次。
  """
  from service.scheduled_task.compliance import checks

  w = checks.shift_window()
  start = w['start'].strftime('%Y-%m-%d %H:%M')
  end = w['end'].strftime('%Y-%m-%d %H:%M')
  return w['start_s'], w['end_s'], w['key'], w['label'], f'{start} ~ {end}'


async def run_incident_task(task) -> None:
  """跑一期事件统计表。**不抛异常**（全部落库），成败由运行记录与通知体现。"""
  run_id = uuid.uuid4().hex
  thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
  parsed = presets.parse_key(getattr(task, 'template_id', '') or '')
  park, period = parsed or ('', presets.PERIOD)

  if period == presets.SHIFT_PERIOD:
    start_s, end_s, period_key, period_label, window_desc = _shift_window()
    start_d = datetime.fromtimestamp(start_s, _TZ).date()
    end_d = datetime.fromtimestamp(end_s, _TZ).date()
  else:
    from service.scheduled_task.report_templates import window_for

    start_d, end_d = window_for(period)
    start_s, end_s = _bounds(start_d, end_d)
    period_key, period_label = _period_label(start_d)
    window_desc = f'{start_d} ~ {end_d}'
  run = await repo.create_run(task.task_id, run_id=run_id, thread_id=thread_id,
                              preset_key=task.template_id, period_key=period_key,
                              period_label=period_label)
  start_ts = time.time()
  error, final_text, artifacts = '', '', []

  try:
    # 卡片「进展同步」是原地追加的，出表前补捞一次；多进程同时触发时单飞，
    # 只让一个真跑、其余等它完成（不会因为「另一个正在刷新」拿到旧正文）
    try:
      await sync.refresh_cards_coalesced(
        hours=settings.INCIDENT_CARD_REFRESH_BUILD_HOURS, max_age_s=300, wait_s=180,
      )
    except Exception as e:
      logger.warning('事件统计表出表前回捞卡片进展失败（按现有正文出表）: %s', e)
    # Kafka 可能晚于群消息落库：出表前补一次匹配，只补 event_log_id 为空的行
    await sync.match_pending()
    # 只统计**配置的群**：同园区还有测试群/自检合成数据，按 cluster 过滤挡不住。
    # 不再退回「按园区前缀全量扫描」：那会把测试群与 TEST-SYNTH 合成行混进表。
    try:
      chats = await feishu.resolve_park_chats()
    except Exception as e:
      chats = {}
      logger.warning('事件统计表解析园区群失败: %s', e)
    missing = [p for p in feishu.configured_parks() if p not in chats]
    failed: list[str] = []
    if park in (presets.ALL_PARKS, ''):
      if not chats:
        raise _NoParkGroups(
          '没有可用的园区群：'
          + ('、'.join(missing) or '（未登记 INCIDENT_FEISHU_PARK_CHATS）')
          + '（群名未匹配或机器人未进群）')
      rows, failed = await asyncio.to_thread(_collect_rows, chats, start_s, end_s)
      if failed:
        logger.warning('事件统计表部分园区取数失败: %s', failed)
    else:
      chat_id = chats.get(park)
      if not chat_id:
        raise _NoParkGroups(f'园区群未解析到 chat_id：{park}（机器人是否已进群？）')
      rows = await asyncio.to_thread(build.build_rows, start_s, end_s, park, [chat_id])
    summary = build.summary_counts(rows)
    all_scope = park in (presets.ALL_PARKS, '')
    covered = [p for p in chats if p not in failed] if all_scope else [park]

    params = {'park': park, 'parks': covered,
              'start': start_d.isoformat(), 'end': end_d.isoformat()}
    info = save_dataset(thread_id, 'incident_stats', params, rows,
                        dataset_name='incident-stats')
    dataset_id = info['dataset_id']
    ds_dir = load_dataset_dir(thread_id, dataset_id)
    file_name = _file_name(period_key, park)
    xlsx.write_workbook(ds_dir / file_name, rows, summary, start_s)

    url = f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{file_name}'
    artifacts = [url]
    scope_line = f'本表覆盖园区：{"、".join(covered)}。' if covered else ''
    if all_scope:
      if missing:
        scope_line += f'未配置群的园区（暂不纳入）：{"、".join(missing)}。'
      if failed:
        scope_line += f'取数失败的园区（不在本表内）：{"、".join(failed)}。'
    if rows:
      final_text = (f'《数据中心事件统计表》已生成（{window_desc}，{period_label}）。\n'
                    f'共 {len(rows)} 条已通报事件。\n'
                    f'{scope_line}\n'
                    f'完整表格见 xlsx 产物：<file url="{url}" name="{file_name}"/>')
    else:
      final_text = (f'本期无通报事件，已生成空表（{window_desc}）。\n'
                    f'{scope_line}\n'
                    f'<file url="{url}" name="{file_name}"/>')
      logger.info('本期无通报事件 task_id=%s window=%s', task.task_id, window_desc)
  except _NoParkGroups as e:
    logger.warning('事件统计表无可用园区群 task_id=%s: %s', task.task_id, e)
    error = str(e)
  except Exception as e:
    logger.exception('事件统计表工作流失败 task_id=%s', task.task_id)
    error = f'{type(e).__name__}: {str(e)[:400]}'

  duration_ms = int((time.time() - start_ts) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await repo.finish_run(run.run_id, status=status, final_reply=final_text,
                        artifact_urls=artifacts, error=error, duration_ms=duration_ms,
                        thread_id=thread_id)
  await repo.create_notification(
    user_id=task.created_by, task_id=task.task_id, task_name=task.name, run_id=run.run_id,
    status=status.value,
    title=f"定时任务「{task.name}」执行{'成功' if status == RunStatus.SUCCESS else '失败'}",
    body=(final_text or error).strip()[:80] or error[:80],
  )
  logger.info('事件统计表工作流完成 task_id=%s status=%s duration_ms=%s',
              task.task_id, status.value, duration_ms)

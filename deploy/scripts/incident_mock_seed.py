#!/usr/bin/env python3
"""本地演示数据：给「数据中心事件统计表」灌最近两期班次产出（**只用于本地看页面效果**）。

链路与真实工作流一致：建 `incident__{园区}__shift` 预设任务（建完立刻停用）→
每园区一期写一份 xlsx 到 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/` →
插运行记录（`preset_key` / `period_key` / `period_label` / `artifact_urls` /
`final_reply` 都按真实格式写），所以顶部大卡的班次、条数、园区 chip 与下载都走正常链路。

    python deploy/scripts/incident_mock_seed.py          # 灌数据
    python deploy/scripts/incident_mock_seed.py --clean  # 删掉本脚本造的数据

`--clean` **只删带 `mock: 'incident'` 标记**的任务/运行记录及其产物目录，不碰真实产出。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import sys
import uuid
from datetime import timedelta
from pathlib import Path

for _root in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
  if (_root / 'service').is_dir() and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from config.mongodb_conn import mongodb_manager  # noqa: E402
from config.settings import settings  # noqa: E402
from core.sandbox.datasets import load_dataset_dir, save_dataset  # noqa: E402
from models.scheduled_task import RunStatus, ScheduledTaskRun  # noqa: E402
from service.scheduled_task import repository as repo  # noqa: E402
from service.scheduled_task.compliance import checks  # noqa: E402
from service.scheduled_task.incident_stats import build, presets, xlsx  # noqa: E402
from service.scheduled_task.incident_stats.workflow import _file_name  # noqa: E402
from service.scheduled_task.repository import (  # noqa: E402
  RUNS_COLLECTION, TASKS_COLLECTION,
)

PARKS = ['廊坊', '平湖', '惠州', '佛山', '重庆']
# 最新一期每个园区的事件条数；None = 该园区生成失败（无产物、卡片走虚线）
LATEST = {'廊坊': 8, '平湖': 5, '惠州': 3, '佛山': 0, '重庆': None}
PREV = {'廊坊': 4, '平湖': 6, '惠州': 0, '佛山': 2, '重庆': 3}

DEVICES = ['PDU-01', 'UPS-02', '精密空调-07', '配电柜-12', '蓄电池组-A']
POINTS = ['输入电压', '输出电流', '有功功率', '回风温度', '电池电压']
CONTENTS = ['输入电压过高', '输出电流超限', '电池温度过高', 'UPS 旁路运行', '配电柜过载']
OPERATORS = ['张伟', '李强', '王敏', '赵磊', '陈静']


def _fmt(t) -> str:
  return t.strftime('%Y年%m月%d日%H时%M分%S秒')


def _rows_for(park: str, n: int, window: dict, rng: random.Random) -> list[dict]:
  start, end = window['start'], window['end']
  span = max(60, int((end - start).total_seconds()))
  rows: list[dict] = []
  for i in range(1, n + 1):
    at = start + timedelta(seconds=rng.randrange(span))
    content = rng.choice(CONTENTS)
    rows.append({
      '序号': i,
      '日期': at.strftime('%Y-%m-%d'),
      '发生时间': _fmt(at),
      '速报时间': _fmt(at + timedelta(seconds=rng.randrange(30, 110))),
      '首报时间': _fmt(at + timedelta(seconds=rng.randrange(120, 290))),
      '地点': f'{park}园区A1数据中心{rng.choice(["3F-301", "2F-208", "B1-017"])}',
      '事件类型': content,
      '事件名称': (f'{park}园区A1数据中心 · {rng.choice(DEVICES)} · '
                   f'{rng.choice(POINTS)} · {content}'),
      '事件原因': rng.choice(['设备老化', '负载突增', '接线松动', '环境温度偏高']),
      '事件进展': rng.choice(['已处理', '处理中', '已升级']),
      '处理结果': rng.choice(['已恢复正常', '确认告警']),
      '处理时长': f'{rng.randint(5, 120)}分钟',
      '过程有无违规': '无',
      '经手人': rng.choice(OPERATORS),
    })
  return rows


def _reply(period_label: str, window_desc: str, n: int, url: str, name: str) -> str:
  """与 `incident_stats.workflow` 的 `final_text` 同格式（接口从这段文本解析条数与窗口）。"""
  if n:
    return (f'《数据中心事件统计表》已生成（{window_desc}，{period_label}）。\n'
            f'共 {n} 条已通报事件。\n'
            f'完整表格见 xlsx 产物：<file url="{url}" name="{name}"/>')
  return (f'本期无通报事件，已生成空表（{window_desc}）。\n'
          f'<file url="{url}" name="{name}"/>')


def _windows(count: int) -> list[dict]:
  """从「最近一个已结束的班次」往前取 count 个（夜/白交替）。"""
  latest = checks.shift_window()
  kind, day = latest['kind'], latest['end'].date()
  out: list[dict] = []
  for _ in range(count):
    out.append(checks.shift_window_for(kind, day))
    if kind == 'night':
      kind, day = 'day', day - timedelta(days=1)
    else:
      kind = 'night'
  return out


async def _clean() -> int:
  db = mongodb_manager.db
  runs = [r async for r in db[RUNS_COLLECTION].find({'mock': 'incident'})]
  for run in runs:
    shutil.rmtree(Path(settings.AGENT_ANALYSIS_DATA_DIR) / run.get('thread_id', ''),
                  ignore_errors=True)
  deleted = await db[RUNS_COLLECTION].delete_many({'mock': 'incident'})
  tasks = await db[TASKS_COLLECTION].delete_many({'mock': 'incident'})
  print(f'已删除演示运行记录 {deleted.deleted_count} 条、演示任务 {tasks.deleted_count} 条'
        f'及其产物目录')
  return 0


async def _ensure_task(park: str) -> dict:
  key = presets.preset_key(park, presets.SHIFT_PERIOD)
  task = await repo.ensure_system_task(
    key,
    name=presets.preset_name(park, f'{park}园区', presets.SHIFT_PERIOD),
    description='数据中心事件统计表：飞书园区群通报 → 告警/工单关联，产出 xlsx',
    cron_expr=presets.preset_cron(key),
    prompt=presets.render_prompt(key),
    task_mode='normal',
    thread_strategy='new',
  )
  # ensure_system_task 建任务时固定 enabled=True。演示数据必须显式停掉：
  # 调度器不认 INCIDENT_STATS_ENABLED，只看到点就跑，真跑起来会去连 DCIM_PG_DSN 与飞书。
  await mongodb_manager.db[TASKS_COLLECTION].update_one(
    {'task_id': task.task_id},
    {'$set': {'enabled': False, 'next_run_at': None, 'mock': 'incident'}},
  )
  return task


async def _seed() -> int:
  db = mongodb_manager.db
  existing = await db[RUNS_COLLECTION].count_documents({'mock': 'incident'})
  if existing:
    print(f'! 本地已有 {existing} 条演示数据；先跑 --clean 再灌，避免页面出现重复期号')
    return 1

  tasks = {park: await _ensure_task(park) for park in PARKS}
  windows = _windows(2)
  plans = [LATEST, PREV]
  created = 0
  for window, plan in zip(windows, plans):
    window_desc = (f'{window["start"].strftime("%Y-%m-%d %H:%M")} ~ '
                   f'{window["end"].strftime("%Y-%m-%d %H:%M")}')
    # 触发时刻与 cron 对齐：夜班 09:00 出、白班 18:00 出
    triggered = window['end'] + timedelta(minutes=30)
    for park in PARKS:
      task = tasks[park]
      n = plan[park]
      run_id = uuid.uuid4().hex
      thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
      rng = random.Random(f'{window["key"]}-{park}')

      if n is None:
        doc = ScheduledTaskRun(
          run_id=run_id, task_id=task.task_id, thread_id=thread_id,
          status=RunStatus.FAILED, triggered_at=triggered,
          preset_key=task.template_id, period_key=window['key'],
          period_label=window['label'],
          error='FeishuAPIError: 拉取群消息超时（演示数据）', duration_ms=31_000,
        ).model_dump()
        doc['mock'] = 'incident'
        await db[RUNS_COLLECTION].insert_one(doc)
        created += 1
        print(f'  {window["key"]}  {park}  失败（无产物）')
        continue

      rows = _rows_for(park, n, window, rng)
      params = {'park': park, 'start': window['start'].strftime('%Y-%m-%d'),
                'end': window['end'].strftime('%Y-%m-%d')}
      info = save_dataset(thread_id, 'incident_stats', params, rows,
                          dataset_name='incident-stats')
      dataset_id = info['dataset_id']
      ds_dir = load_dataset_dir(thread_id, dataset_id)
      name = _file_name(window['key'], park)
      xlsx.write_workbook(ds_dir / name, rows, build.summary_counts(rows),
                          window['start_s'])
      url = f'/api/v1/chat/agent/artifacts/{thread_id}/{dataset_id}/{name}'

      doc = ScheduledTaskRun(
        run_id=run_id, task_id=task.task_id, thread_id=thread_id,
        status=RunStatus.SUCCESS, triggered_at=triggered,
        preset_key=task.template_id, period_key=window['key'],
        period_label=window['label'], artifact_urls=[url],
        final_reply=_reply(window['label'], window_desc, n, url, name),
        duration_ms=rng.randrange(9_000, 28_000),
      ).model_dump()
      doc['mock'] = 'incident'
      await db[RUNS_COLLECTION].insert_one(doc)
      created += 1
      print(f'  {window["key"]}  {park}  {n} 条')

  print(f'\n完成：{created} 条演示运行记录已写入；打开 http://localhost:5173/compliance 查看')
  print('清理：python deploy/scripts/incident_mock_seed.py --clean')
  return 0


async def main() -> int:
  ap = argparse.ArgumentParser(description='数据中心事件统计表本地演示数据')
  ap.add_argument('--clean', action='store_true', help='删除本脚本造的演示数据')
  args = ap.parse_args()
  try:
    return await (_clean() if args.clean else _seed())
  finally:
    mongodb_manager.close()


if __name__ == '__main__':
  sys.exit(asyncio.run(main()))

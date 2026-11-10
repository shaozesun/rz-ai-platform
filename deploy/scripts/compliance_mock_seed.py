#!/usr/bin/env python3
"""本地演示数据：给「不合规检查」页灌 12 期假产出（**只用于本地看页面效果**）。

做的和真实工作流一致：建系统预设任务 → 每个班次写一份 xlsx 到
`{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/` → 插运行记录
（`check_counts` / `artifact_urls` / `final_reply` 都按真实格式写）。
所以页面上的卡片、看板、明细表格、Excel 下载走的都是正常链路。

    python deploy/scripts/compliance_mock_seed.py          # 灌数据
    python deploy/scripts/compliance_mock_seed.py --clean  # 删掉本脚本造的数据

`--clean` **只删带 `mock: true` 标记的运行记录**及其产物目录，不碰真实产出。
"""

from __future__ import annotations

import argparse
import asyncio
import random
import shutil
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

for _root in (Path.cwd(), Path(__file__).resolve().parent.parent.parent):
  if (_root / 'service').is_dir() and str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from config.mongodb_conn import mongodb_manager  # noqa: E402
from config.settings import settings  # noqa: E402
from models.scheduled_task import RunStatus, ScheduledTaskRun  # noqa: E402
from service.scheduled_task import repository as repo  # noqa: E402
from service.scheduled_task.compliance import checks, presets, xlsx  # noqa: E402
from service.scheduled_task.repository import (  # noqa: E402
  RUNS_COLLECTION, TASKS_COLLECTION,
)

# 最近 12 个班次（新 → 旧）的四项命中数；None = 这一期生成失败（无产物）
PLAN: list[tuple[int, int, int, int] | None] = [
  (3, 5, 2, 4),
  (1, 2, 0, 1),
  (0, 0, 0, 0),
  (4, 6, 1, 3),
  (2, 3, 2, 0),
  None,
  (5, 4, 3, 2),
  (1, 1, 0, 1),
  (0, 2, 1, 0),
  (6, 7, 4, 5),
  (2, 0, 1, 2),
  (3, 3, 2, 3),
]

PARKS = ['廊坊', '平湖', '惠州', '佛山', '重庆']
DEVICES = ['PDU-01', 'UPS-02', '精密空调-07', '配电柜-12', '列头柜-03', '蓄电池组-A']
POINTS = ['输入电压', '输出电流', '有功功率', '回风温度', '电池电压', '冷却水温度', '负载率']
CONTENTS = ['输入电压过高', '输出电流超限', '电池温度过高', 'UPS 旁路运行',
            '配电柜过载', '空调回风温度过高', '蓄电池组电压异常']
OPERATORS = ['张伟', '李强', '王敏', '赵磊', '陈静']


def _pick(rng: random.Random) -> dict:
  park = rng.choice(PARKS)
  return {
    '园区': park,
    '空间': f'{park}园区A1数据中心{rng.choice(["3F-301", "2F-208", "1F-105", "B1-017"])}',
    '设备': rng.choice(DEVICES),
    '测点': rng.choice(POINTS),
    '级别': rng.choice(['重要', '重要', '紧急', '一般']),
    '告警内容': rng.choice(CONTENTS),
    '告警值': f'{rng.uniform(200, 260):.1f}{rng.choice(["V", "A", "℃", "%"])}',
  }


def _at(rng: random.Random, start: datetime, end: datetime) -> str:
  span = max(60, int((end - start).total_seconds()))
  return (start + timedelta(seconds=rng.randrange(span))).strftime('%Y-%m-%d %H:%M:%S')


PARK_CODES = {'廊坊': 'LF', '平湖': 'PH', '惠州': 'HZ', '佛山': 'FS', '重庆': 'CQ'}


def _rows_for(key: str, n: int, window: dict, rng: random.Random) -> list[dict]:
  rows: list[dict] = []
  start, end = window['start'], window['end']
  for i in range(1, n + 1):
    base = _pick(rng)
    alarm_at = _at(rng, start, end)
    order_no = f'GD2026{rng.randrange(100000, 999999)}'
    people = {'响应人': rng.choice(OPERATORS), '处理人': rng.choice(OPERATORS)}
    if key == 'response_timeout':
      lag = rng.uniform(6, 120)
      responded = (datetime.strptime(alarm_at, '%Y-%m-%d %H:%M:%S')
                   + timedelta(minutes=lag)).strftime('%Y-%m-%d %H:%M:%S')
      rows.append({
        '序号': i, '工单号': order_no, **base,
        '告警时间': alarm_at, '响应时间': responded,
        '响应时长(分钟)': round(lag, 1), **people,
        '当前告警状态': rng.choice(['触发', '刷新', '恢复']),
      })
    elif key == 'unclosed_order':
      responded = _at(rng, start, end)
      rows.append({
        '序号': i, '工单号': order_no, **base,
        '告警时间': alarm_at, '响应时间': responded,
        '开始处理时间': responded,
        '响应时长(分钟)': round(rng.uniform(1, 40), 1),
        '未关单时长(分钟)': round(rng.uniform(30, 600), 1),
        **people, '当前告警状态': '触发',
      })
    elif key == 'premature_close':
      rows.append({
        '序号': i, '工单号': order_no, **base,
        '告警时间': alarm_at, '关单时间': _at(rng, start, end),
        '处理时长(分钟)': round(rng.uniform(3, 90), 1),
        '处理人': people['处理人'],
        '处理结果': rng.choice(['确认告警', '已恢复正常']),
        '当前告警状态': rng.choice(['触发', '刷新']),
        '告警已持续(分钟)': round(rng.uniform(20, 300), 1),
      })
    else:  # repeat_alarm（列对齐值守台账）
      park = base['园区']
      rows.append({
        '序号': i,
        '大楼名称': f'{PARK_CODES[park]}-A1',
        '告警时间': alarm_at,
        '等级': base['级别'],
        '次数': rng.randint(3, 9),
        '地址名称': base['空间'],
        '设备名称': base['设备'],
        '监控项名称': base['测点'],
        '告警内容': base['告警内容'],
        '处理过程': rng.choice(['现场已调整空调参数，报警已解除', '市电波动，设备运行正常']),
        '处理结果': rng.choice(['确认告警', '已恢复正常']),
        '问题是否解决': '', '解决办法': '', '填报人': '',
        '首次时间': alarm_at,
        '跨度(分钟)': round(rng.uniform(5, 240), 1),
      })
  return rows


def _slots(count: int) -> list[tuple[str, date]]:
  """从「最近一个已结束的班次」往前补齐 count 个（day/night 交替）。"""
  latest = checks.shift_window()
  kind, day = latest['kind'], latest['end'].date()
  out: list[tuple[str, date]] = []
  for _ in range(count):
    out.append((kind, day))
    if kind == 'night':
      kind, day = 'day', day - timedelta(days=1)
    else:
      kind = 'night'
  return out


def _final_reply(window: dict, result: dict, url: str, name: str) -> str:
  total = sum(item['rows'] for item in result['summary'])
  counts = ' · '.join(f"{item['name']} {item['rows']}" for item in result['summary'])
  lines = [f'《{checks.TITLE}》{window["label"]}。', f'- 命中 {total} 条：{counts}']
  if not total:
    lines.append('- 本班次未发现不合规项。')
  lines.append(f'完整表格见 xlsx 产物：<file url="{url}" name="{name}"/>')
  return '\n'.join(lines)


async def _clean() -> int:
  coll = mongodb_manager.db[RUNS_COLLECTION]
  runs = [r async for r in coll.find({'mock': True})]
  for run in runs:
    shutil.rmtree(Path(settings.AGENT_ANALYSIS_DATA_DIR) / run.get('thread_id', ''),
                  ignore_errors=True)
  deleted = await coll.delete_many({'mock': True})
  print(f'已删除演示运行记录 {deleted.deleted_count} 条及其产物目录')
  return 0


async def _seed() -> int:
  coll = mongodb_manager.db[RUNS_COLLECTION]
  existing = await coll.count_documents({'mock': True})
  if existing:
    print(f'! 本地已有 {existing} 条演示数据；先跑 --clean 再灌，避免页面出现重复期号')
    return 1

  task = await repo.ensure_system_task(
    presets.KEY,
    name=presets.NAME,
    description='运营监控中心告警不合规检查：按班次核对四项告警处置口径，产出 xlsx',
    cron_expr=presets.CRON,
    prompt=presets.render_prompt(),
    task_mode='normal',
    thread_strategy='new',
  )
  # ensure_system_task 建任务时固定 enabled=True。演示数据必须显式停掉：
  # 调度器不认 COMPLIANCE_ENABLED，只看到点就跑，真跑起来会去连 DCIM_PG_DSN。
  # 停用不影响页面（列表接口默认含已停用的任务，历史产出照常可看）。
  await mongodb_manager.db[TASKS_COLLECTION].update_one(
    {'task_id': task.task_id},
    {'$set': {'enabled': False, 'next_run_at': None}},
  )
  print(f'预设任务：{task.task_id}（已停用，cron 不会跑）')

  base = Path(settings.AGENT_ANALYSIS_DATA_DIR)
  slots = _slots(len(PLAN))
  created = 0
  for idx, ((kind, day), plan) in enumerate(zip(slots, PLAN)):
    window = checks.shift_window_for(kind, day)
    run_id = uuid.uuid4().hex
    thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
    dataset_id = f'compliance_{window["key"]}'
    triggered = window['end'] + timedelta(minutes=30)
    rng = random.Random(20260 + idx)

    if plan is None:
      doc = ScheduledTaskRun(
        run_id=run_id, task_id=task.task_id, thread_id=thread_id,
        status=RunStatus.FAILED, triggered_at=triggered,
        preset_key=presets.KEY, period_key=window['key'], period_label=window['label'],
        error='RuntimeError: DCIM_PG_DSN 连接超时（演示数据）', duration_ms=42_000,
      ).model_dump()
      doc['mock'] = True
      await coll.insert_one(doc)
      created += 1
      print(f'  {window["key"]}  {window["label"]}  失败（无产物）')
      continue

    rows = {key: _rows_for(key, n, window, rng)
            for key, n in zip(('response_timeout', 'unclosed_order',
                               'premature_close', 'repeat_alarm'), plan)}
    result = {'rows': rows, 'summary': checks.summary(rows), 'matrix': checks.matrix(rows)}
    name = f"compliance_{window['end'].strftime('%Y%m%d')}_{window['kind']}.xlsx"
    ds_dir = base / thread_id / dataset_id
    ds_dir.mkdir(parents=True, exist_ok=True)
    xlsx.write_workbook(ds_dir / name, result,
                        title=f'{checks.TITLE} · {window["label"]}',
                        period=window['label'])
    url = f'/api/v1/chat/agent/artifacts/{thread_id}/{dataset_id}/{name}'

    doc = ScheduledTaskRun(
      run_id=run_id, task_id=task.task_id, thread_id=thread_id,
      status=RunStatus.SUCCESS, triggered_at=triggered,
      preset_key=presets.KEY, period_key=window['key'], period_label=window['label'],
      check_counts={item['key']: item['rows'] for item in result['summary']},
      artifact_urls=[url],
      final_reply=_final_reply(window, result, url, name),
      duration_ms=rng.randrange(8_000, 24_000),
    ).model_dump()
    doc['mock'] = True
    await coll.insert_one(doc)
    created += 1
    total = sum(plan)
    print(f'  {window["key"]}  {window["label"]}  命中 {total} 条 {plan}')

  print(f'\n完成：{created} 期演示数据已写入；打开 http://localhost:5173/compliance 查看')
  print('清理：python deploy/scripts/compliance_mock_seed.py --clean')
  return 0


async def main() -> int:
  ap = argparse.ArgumentParser(description='不合规检查页本地演示数据')
  ap.add_argument('--clean', action='store_true', help='删除本脚本造的演示数据')
  args = ap.parse_args()
  try:
    return await (_clean() if args.clean else _seed())
  finally:
    mongodb_manager.close()


if __name__ == '__main__':
  sys.exit(asyncio.run(main()))

#!/usr/bin/env python3
"""补跑预设分析报告（上游恢复后把漏掉的这一期补上）。

为什么**不依赖失败记录**：2026-10-26 那批失败记录已按用户确认清理，靠「昨天 failed 的
预设」来补跑就补不回来了。这里改成自证口径 ——「这一期还没有 success 产出」即为待补。

选谁：
  - `--key` 指定（可重复，如 `event__廊坊__weekly`）—— **按人说了算，不过下面两道闸**
  - 缺省：全部 enabled 预设中**当前期还没有 success 运行记录**、且满足两道闸的：
    1. 本期的**计划触发时刻 ≤ now**（还没到该跑时间的不补）
    2. 本期的计划触发时刻 **≥ 预设创建时间**（预设上线前就已过期的历史期不算欠账）

判据为什么是「本期的计划触发时刻」而不是 `task.next_run_at`：调度器在 **claim 那一刻**就
`set_next_run()` 把 next_run_at 推进到下一期（失败也照样推进）→ 用它判「是否到期」会把刚
失败、正该补跑的期次全部漏掉。也不能只判「本期是否已过去」：2026-10-26 实测，`period_of`
把月报算成 2026-09（该在 10-01 跑），而预设 10-13 才建 → 被当成欠账跑了 25 份旧格式月报。
第 2 道闸正是拦这种情况。

`--allow-pre-preset` 只关掉第 2 道（`due > now` 恒保留，跑不出未来期次），用于**上线后
主动补一份历史期**（例如新版月报部署后重跑 2026-09）。

`--period` 直接点名**要补哪一期**（月报 `YYYY-MM`、周报 `YYYY-Www`）：期次是人指定的，
与 `--key` 同语义 —— 不受上面两道闸约束，只按「该期还没有 success」筛（给了 `--key`
就只剩「是否已有 success」的提示，仍会跑）。窗口/期号由锚点推出来，dry-run 会打印。

用法（默认 dry-run，只打印不动手）：
    python deploy/scripts/replay_presets.py
    python deploy/scripts/replay_presets.py --key event__廊坊__weekly
    python deploy/scripts/replay_presets.py --allow-pre-preset     # 补上线前的历史期
    python deploy/scripts/replay_presets.py --key event__nationwide__monthly --period 2026-07
    python deploy/scripts/replay_presets.py --yes

串行执行（pen 同账号 × 同接口只允许 1 个在途），复用
`service.scheduled_task.report.workflow.run_report_workflow()`，落库/产物/通知
与正常调度完全同形。

退出码：0 全部成功（或 dry-run）；1 有补跑失败。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from croniter import croniter

# 直接 `python deploy/scripts/x.py` 时 sys.path[0] 是脚本目录 → 显式把仓库根加进来
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.mongodb_conn import mongodb_manager  # noqa: E402
from models.scheduled_task import ScheduledTask  # noqa: E402
from service.scheduled_task import repository as repo  # noqa: E402
from service.scheduled_task.report.spec import get_report_spec  # noqa: E402
from service.scheduled_task.report.workflow import run_report_workflow  # noqa: E402
from service.scheduled_task.report_templates import (  # noqa: E402
  parse_key, period_of, prev_window, window_for,
)

_MONTH_ARG_RE = re.compile(r'^(\d{4})-(0[1-9]|1[0-2])$')
_WEEK_ARG_RE = re.compile(r'^(\d{4})-w(\d{2})$', re.IGNORECASE)


def _parse_period(text: str) -> tuple[str, datetime] | None:
  """`YYYY-MM` / `YYYY-Www` → `(周期类型, 锚点)`；非法格式返回 None。

  锚点＝该期**结束后的次日 0 点**（月报＝次月 1 日、周报＝下一周一），也就是正常工作流在
  该期被调度的时刻 —— 拿它当 `now` 传进 `window_for`/`period_of`，算出来的正是目标期次。
  """
  if not text:
    return None
  tz = repo._get_task_tz()
  value = text.strip()
  month = _MONTH_ARG_RE.match(value)
  if month:
    year, mon = int(month.group(1)), int(month.group(2))
    anchor = datetime(year + 1, 1, 1) if mon == 12 else datetime(year, mon + 1, 1)
    return 'monthly', anchor.replace(tzinfo=tz)
  week = _WEEK_ARG_RE.match(value)
  if week:
    try:
      monday = date.fromisocalendar(int(week.group(1)), int(week.group(2)), 1)
    except ValueError:
      return None
    return 'weekly', datetime.combine(monday + timedelta(days=7), time.min, tzinfo=tz)
  return None


async def _preset_task(key: str) -> ScheduledTask | None:
  doc = await mongodb_manager.db[repo.TASKS_COLLECTION].find_one(
    {'template_id': key, 'created_by': repo.SYSTEM_USER_ID})
  return ScheduledTask(**doc) if doc else None


async def _enabled_presets() -> list[ScheduledTask]:
  docs = await mongodb_manager.db[repo.TASKS_COLLECTION].find(
    {'created_by': repo.SYSTEM_USER_ID, 'enabled': True}
  ).sort('created_at', 1).to_list(500)
  return [ScheduledTask(**d) for d in docs]


async def _success_count(task: ScheduledTask, now=None) -> int:
  """该期已有多少条成功产出（「本期无工单」也算成功——说明检查过了）。"""
  period_key, _ = period_of(task.template_id, now)
  if not period_key:
    return 0
  return await mongodb_manager.db[repo.RUNS_COLLECTION].count_documents(
    {'task_id': task.task_id, 'period_key': period_key, 'status': 'success'})


async def _has_success(task: ScheduledTask, now=None) -> bool:
  return await _success_count(task, now) > 0


def _period_due_at(task: ScheduledTask, now=None) -> datetime | None:
  """本期的「计划触发时刻」（UTC）：**本期窗口结束后** cron 的第一次触发。

  注意基点是窗口 **end + 1 天**，不是 start —— 报告跑的是「上一个完整周期」，周报在
  下周一凌晨、月报在次月 1 日凌晨才出上一期。用 start 会算早一整期（如 2026-09 月报
  会被算成 09-01，实际 10-01 才跑）。cron 按任务本地时区解释（与
  `repository.compute_next_run` 同一套），结果转 UTC 比较。
  """
  parsed = parse_key(task.template_id or '')
  if not parsed or not task.cron_expr:
    return None
  _start, end = window_for(parsed[2], now)
  base = datetime(end.year, end.month, end.day, tzinfo=repo._get_task_tz()) + timedelta(days=1)
  return croniter(task.cron_expr, base).get_next(datetime).astimezone(timezone.utc)


def _fmt(value: datetime | None) -> str:
  """UTC → 本地时区的可读时间（打印用）。"""
  if value is None:
    return '未知'
  return value.astimezone(repo._get_task_tz()).strftime('%Y-%m-%d %H:%M')


async def _collect(keys: list[str], allow_pre_preset: bool = False, now=None,
                   period_type: str | None = None) -> list[ScheduledTask]:
  """选出本次要跑的预设。

  `now`/`period_type` 只在 `--period` 指定期次时给：锚点即目标期次的触发时刻，
  期次由人点名 → 与 `--key` 同语义，不受 due 两道闸约束，只按「该期还没有 success」筛。
  """
  targets: list[ScheduledTask] = []
  if keys:
    for key in keys:
      task = await _preset_task(key)
      if task is None:
        print(f'[跳过] 库里没有预设 {key}', file=sys.stderr)
        continue
      parsed = parse_key(key)
      if period_type and parsed and parsed[2] != period_type:
        print(f'[跳过] {key}：周期类型是 {parsed[2]}，与 --period 不符', file=sys.stderr)
        continue
      if not task.enabled:
        print(f'[注意] {key} 当前处于停用状态，仍按指定补跑')
      existing = await _success_count(task, now) if now is not None else 0
      if existing:
        print(f'[注意] {key}：该期已有 {existing} 条成功产出，本次会新增一条运行记录'
              f'（重复条目用 delete_preset_runs.py 清理）')
      targets.append(task)
    return targets

  if period_type and now is not None:
    for task in await _enabled_presets():
      if get_report_spec(task.template_id) is None:
        continue
      parsed = parse_key(task.template_id or '')
      if not parsed or parsed[2] != period_type:
        continue
      if await _has_success(task, now):
        continue
      targets.append(task)
    return targets

  now_utc = datetime.now(timezone.utc)
  for task in await _enabled_presets():
    if get_report_spec(task.template_id) is None:
      continue
    if await _has_success(task):
      continue
    due = _period_due_at(task)
    if due is None:
      print(f'[跳过] {task.template_id}：算不出本期应跑时间（缺 cron 或 key 非法）')
      continue
    if due > now_utc:
      print(f'[跳过] {task.template_id}：本期还没到应跑时间（应跑 {_fmt(due)}）')
      continue
    if not allow_pre_preset and task.created_at and due < task.created_at:
      print(f'[跳过] {task.template_id}：预设建立于 {_fmt(task.created_at)}，'
            f'晚于本期应跑时间 {_fmt(due)}（上线前的历史期，不算欠账；'
            f'确需补跑加 --allow-pre-preset）')
      continue
    targets.append(task)
  return targets


async def main() -> int:
  ap = argparse.ArgumentParser(description='补跑预设分析报告（默认 dry-run）')
  ap.add_argument('--key', action='append', default=[], help='预设 key，可重复')
  ap.add_argument('--allow-pre-preset', action='store_true',
                  help='允许补「预设建立前就已到期」的历史期次（如上线后重跑 2026-09 月报）')
  ap.add_argument('--period', default='',
                  help='指定补哪一期：月报 YYYY-MM（如 2026-07）、周报 YYYY-Www（如 2026-W41）。'
                       '期次由人点名 → 不受 --allow-pre-preset 两道闸约束')
  ap.add_argument('--yes', action='store_true', help='真的执行（默认只打印）')
  args = ap.parse_args()

  period = _parse_period(args.period)
  if args.period and period is None:
    print(f'[错误] --period 格式非法：{args.period}（月报 YYYY-MM，周报 YYYY-Www）',
          file=sys.stderr)
    return 2
  period_type, anchor = period if period else (None, None)

  collected = await _collect(args.key, args.allow_pre_preset, now=anchor,
                             period_type=period_type)
  targets = [t for t in collected if get_report_spec(t.template_id)]
  skipped = len(collected) - len(targets)
  if skipped:
    print(f'[跳过] {skipped} 个预设未登记确定性工作流规格（供应商服务工单仍走 agent 路径）')
  if not targets:
    print('没有需要补跑的预设')
    return 0

  for i, task in enumerate(targets, 1):
    period_key, label = period_of(task.template_id, anchor)
    parsed = parse_key(task.template_id) if anchor is not None else None
    if parsed:
      cur = window_for(parsed[2], anchor)
      prev = prev_window(parsed[2], anchor)
      print(f'{i}/{len(targets)} {task.template_id}（{label}｜期号 {period_key}）\n'
            f'    本期 {cur[0]} ~ {cur[1]}｜上期 {prev[0]} ~ {prev[1]}')
    else:
      print(f'{i}/{len(targets)} {task.template_id}（{label}）')
  if not args.yes:
    print('\n以上为 dry-run；确认无误后加 --yes 真正执行')
    return 0

  failed = 0
  for i, task in enumerate(targets, 1):
    print(f'\n[{i}/{len(targets)}] 开始 {task.template_id}', flush=True)
    try:
      await run_report_workflow(task, get_report_spec(task.template_id), now=anchor)
    except Exception as e:   # run_report_workflow 自身不抛，这里只兜底
      failed += 1
      print(f'[异常] {task.template_id}: {type(e).__name__}: {e}', file=sys.stderr)
      continue
    if await _has_success(task, anchor):
      print(f'[成功] {task.template_id}')
    else:
      failed += 1
      print(f'[失败] {task.template_id}（运行记录状态见 scheduled_task_runs）', file=sys.stderr)
  print(f'\n完成：{len(targets) - failed} 成功 / {failed} 失败')
  return 1 if failed else 0


if __name__ == '__main__':
  try:
    sys.exit(asyncio.run(main()))
  finally:
    mongodb_manager.close()

#!/usr/bin/env python3
"""按期号删除预设报告运行记录与产物目录（破坏性，默认 dry-run）。

为什么单独一个脚本：`replay_presets.py` 会补齐「本期无 success」的预设，一旦补错了期次
（2026-10-26 实测：补跑把**上线前就已到期**的 2026-09 月报当欠账跑了出来，共 25 份、
且用的是当时的旧版报告代码），需要能把那一期**整期回滚**再重跑。逐条点页面删不现实，
直接改库又容易把产物目录留成孤儿。

安全边界（全部 fail-closed）：
  - 只删 `scheduled_task_runs` 里 `period_key` 命中的记录。用户自建任务的运行记录不写
    period_key（`create_run` 对非预设传空），天然不在范围内。
  - 仍逐条回查其任务必须是 `created_by='system'` 的预设；有一条不符就**整体中止**。
  - 产物目录按运行记录的 `thread_id` 定位，`resolve()` 后必须仍在 AGENT_ANALYSIS_DATA_DIR
    之下（防路径穿越）；不存在只记日志、不算失败。
  - **不删预设任务本身**（enabled / next_run_at 原样保留），不删通知。

用法（默认 dry-run，只打印不动手）：
    python deploy/scripts/delete_preset_runs.py --period-key 2026-09
    python deploy/scripts/delete_preset_runs.py --period-key 2026-09 --status success
    python deploy/scripts/delete_preset_runs.py --period-key 2026-W42 --ticket event breakdown
    python deploy/scripts/delete_preset_runs.py --period-key 2026-09 --yes

`--ticket` 按预设 key 的**工单类型前缀**收窄（`event__…`/`breakdown__…`）；不给则删该期
全部预设 —— 逐类重出时务必带上，否则会把其它类型的同期产物一起删掉。

退出码：0 成功（或 dry-run）；1 命中非系统预设 / 有删除异常。
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shutil
import sys
from pathlib import Path

# 直接 `python deploy/scripts/x.py` 时 sys.path[0] 是脚本目录 → 显式把仓库根加进来
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.mongodb_conn import mongodb_manager  # noqa: E402
from config.settings import settings  # noqa: E402
from service.scheduled_task import repository as repo  # noqa: E402


def _fmt_time(value) -> str:
  return value.strftime('%Y-%m-%d %H:%M') if value else '-'


def _artifact_dir(thread_id: str, root: Path) -> Path | None:
  """运行线程的产物目录；越界或线程 id 为空返回 None。"""
  if not thread_id:
    return None
  target = (root / thread_id).resolve()
  if target == root or root not in target.parents:
    return None
  return target


async def _load_runs(keys: list[str], status: str, tickets: list[str]) -> list[dict]:
  query: dict = {'period_key': {'$in': keys}}
  if status:
    query['status'] = status
  if tickets:
    query['preset_key'] = {'$regex': '^(?:' + '|'.join(re.escape(t) for t in tickets) + ')__'}
  cursor = mongodb_manager.db[repo.RUNS_COLLECTION].find(query).sort('created_at', 1)
  return await cursor.to_list(1000)


async def _verify_scope(runs: list[dict]) -> list[str]:
  """逐条回查归属，返回错误说明（空列表＝全部是系统预设）。"""
  task_ids = sorted({r.get('task_id', '') for r in runs})
  docs = await mongodb_manager.db[repo.TASKS_COLLECTION].find(
    {'task_id': {'$in': task_ids}}).to_list(len(task_ids))
  owners = {d['task_id']: d.get('created_by', '') for d in docs}
  errors: list[str] = []
  for r in runs:
    task_id = r.get('task_id', '')
    if owners.get(task_id) != repo.SYSTEM_USER_ID:
      errors.append(
        f'run_id={r.get("run_id")} task_id={task_id} '
        f'created_by={owners.get(task_id, "<任务不存在>")}')
  return errors


async def main() -> int:
  ap = argparse.ArgumentParser(description='按期号删除预设运行记录与产物（默认 dry-run）')
  ap.add_argument('--period-key', action='append', default=[],
                  help='期号，如 2026-09 / 2026-W43，可重复')
  ap.add_argument('--status', default='', help='只删该状态（如 success）；缺省删全部状态')
  ap.add_argument('--ticket', nargs='+', default=[], metavar='TYPE',
                  help='只删这些工单类型的预设（按 key 前缀，如 event breakdown）；缺省全部类型')
  ap.add_argument('--yes', action='store_true', help='真的删除（默认只打印）')
  args = ap.parse_args()

  keys = [k.strip() for k in args.period_key if k.strip()]
  if not keys:
    print('必须用 --period-key 指定期号（如 --period-key 2026-09）', file=sys.stderr)
    return 1

  runs = await _load_runs(keys, args.status, args.ticket)
  if not runs:
    print(f'没有命中的运行记录（period_key={keys} status={args.status or "全部"} '
          f'ticket={args.ticket or "全部"}）')
    return 0

  errors = await _verify_scope(runs)
  if errors:
    print(f'[中止] 命中 {len(errors)} 条非系统预设的运行记录，未做任何修改：', file=sys.stderr)
    for e in errors:
      print(f'  {e}', file=sys.stderr)
    return 1

  root = Path(settings.AGENT_ANALYSIS_DATA_DIR).resolve()
  dirs: list[Path] = []
  print(f'命中 {len(runs)} 条运行记录（数据根 {root}）：')
  for r in runs:
    d = _artifact_dir(r.get('thread_id', ''), root)
    if d is not None:
      dirs.append(d)
    print(f'  {_fmt_time(r.get("created_at"))}  {r.get("period_key")}  '
          f'{r.get("preset_key") or r.get("task_id")}  {r.get("status")}  '
          f'run_id={r.get("run_id")}')
  existing = sorted({d for d in dirs if d.is_dir()})
  print(f'\n产物目录 {len(existing)} 个：')
  for d in existing:
    print(f'  {d}')

  if not args.yes:
    print('\n以上为 dry-run，未做任何修改；确认无误后加 --yes 真正删除')
    return 0

  run_ids = [r['run_id'] for r in runs]
  result = await mongodb_manager.db[repo.RUNS_COLLECTION].delete_many(
    {'run_id': {'$in': run_ids}})
  print(f'\n已删除运行记录 {result.deleted_count} 条')
  removed = 0
  for d in existing:
    try:
      shutil.rmtree(d)
      removed += 1
    except OSError as e:
      print(f'[失败] 删除目录 {d}: {e}', file=sys.stderr)
  print(f'已删除产物目录 {removed} 个')
  return 0 if removed == len(existing) else 1


if __name__ == '__main__':
  try:
    sys.exit(asyncio.run(main()))
  finally:
    mongodb_manager.close()

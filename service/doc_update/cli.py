"""本机核对口径：`python -m service.doc_update.cli [--sync] [--name X] [--dept A1] [--yes]`。

- 默认：只打印库里**最新一期**快照的「公司 × 部门 × 25 项」状态（不连 mgmt、不下载）
- `--period YYYY-MM`：看某一历史期（只读）
- `--sync`：实际取一期（按期提交型不下载，原地更新型串行下载），结果只打印
- `--name`：只看一个文档（传文件名，如「能源月报」），只取匹配到的那几项
- `--dept`：只看/只跑指定部门（逗号分隔，如 A1,A2；同名部门跨公司会一起命中）
- `--company`：只看/只跑指定公司（companyId 或配置里的标签，如 廊坊 / 平湖）
- `--depts`：只看固定部门列（配置 × 注册表缓存，不联网）
- `--refresh-depts`：按接口重新解析 dept_id/root_fid（加 `--yes` 才写注册表）
- `--yes`：与 `--sync` / `--refresh-depts` 联用时写库（不改库内其它数据）

只读优先：不加 `--yes` 绝不写库。
"""

import argparse
import asyncio
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config.settings import settings
from service.doc_update import workflow
from service.doc_update.catalog import MONTHLY_ITEMS, resolve_items, summary
from service.doc_update.verdict import STATUS_LABELS

_MODE_TEXT = {'cross': '与上一期比', 'baseline': '基线'}


def _fmt_dt(value) -> str:
  if not isinstance(value, datetime):
    return '-'
  if value.tzinfo is None:
    # Mongo 读回来是 naive UTC（库里存的是 aware UTC），不能当本地时间直接格式化
    value = value.replace(tzinfo=timezone.utc)
  value = value.astimezone(ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE or 'Asia/Shanghai'))
  return value.strftime('%Y-%m-%d %H:%M')


def _print_items(items) -> None:
  for item in items or []:
    days = item.get('changed_days_ago')
    age = f'{days}天前' if days is not None else '-'
    extra = item.get('error') or ''
    print(f"{str(item.get('number')):<20} {item.get('name', ''):<24} "
          f"{item.get('status_label'):<6} 文件 {item.get('file_count')} "
          f"上次变更 {age} {extra}".rstrip())


def _print_verdict(verdict: dict) -> None:
  mode = verdict.get('mode') or ('baseline' if verdict.get('baseline') else 'cross')
  head = f">>> {verdict.get('period_label') or '-'} · {_MODE_TEXT.get(mode, '比对')}"
  parts = [f"本次快照 {_fmt_dt(verdict.get('taken_at'))}"]
  print(f"{head}（{'，'.join(parts)}）")
  labels = verdict.get('labels') or STATUS_LABELS
  counts = verdict.get('counts') or {}
  print('>>> ' + ' · '.join(f'{labels[k]} {counts.get(k, 0)}' for k in labels))
  print()
  if verdict.get('depts'):
    for dept in verdict.get('depts') or []:
      label = _dept_label(dept)
      error = dept.get('error') or ''
      print(f"--- {label}" + (f"  [{error}]" if error else ''))
      _print_items([i for i in verdict.get('items') or []
                    if _dept_key(i) == _dept_key(dept)])
      print()
  else:
    _print_items(verdict.get('items'))
  for w in verdict.get('warnings') or []:
    dept = f"[{_dept_label(w)}] " if w.get('dept_name') else ''
    print(f"待复核 {dept}{w.get('number')} {w.get('file_name')}: {w.get('warn')}")


def _dept_key(row: dict) -> tuple[str, str]:
  return (str(row.get('company_id') or ''),
          str(row.get('dept_id') or row.get('dept_name') or ''))


def _dept_label(row: dict) -> str:
  name = row.get('dept_name') or row.get('dept_id') or ''
  company = row.get('company_name') or ''
  return f'{company} · {name}' if company else name


def _filter_depts(verdict: dict, names: list[str],
                  companies: list[str] | None = None) -> dict:
  """读库视图按公司/部门收窄（`--dept` / `--company`）。"""
  wanted = {n.upper() for n in names}
  wanted_c = {c.strip().lower() for c in (companies or []) if c.strip()}
  depts = []
  for d in verdict.get('depts') or []:
    if wanted and str(d.get('dept_name') or '').upper() not in wanted:
      continue
    if wanted_c:
      cid = str(d.get('company_id') or '').strip().lower()
      cname = str(d.get('company_name') or '').strip().lower()
      if cid not in wanted_c and cname not in wanted_c:
        continue
    depts.append(d)
  keys = {_dept_key(d) for d in depts}
  items = [i for i in verdict.get('items') or [] if _dept_key(i) in keys]
  counts = {k: 0 for k in ('updated', 'stale', 'missing', 'failed', 'baseline')}
  for item in items:
    counts[item['status']] = counts.get(item['status'], 0) + 1
  out = dict(verdict)
  out.update({'items': items, 'depts': depts, 'counts': counts,
              'total': len(items), 'dept_count': len(depts)})
  return out


def _resolve(keyword: str):
  """把 `--name` 解析到唯一清单项；命中 0 项 / 多项时打印原因并返回 None。"""
  items = resolve_items(keyword)
  if not items:
    print(f'>>> 没找到匹配「{keyword}」的清单项（本功能只覆盖月度项）')
    print('>>> 可用：' + ' / '.join(f'{i.number_label} {i.name}' for i in MONTHLY_ITEMS))
    return None
  if len(items) > 1:
    print(f'>>> 「{keyword}」匹配到 {len(items)} 项，请指明：')
    for i in items:
      print(f"    {i.number_label:<20} {i.name}")
    return None
  print(f'>>> 命中：{items[0].number_label} {items[0].name}')
  return items


def _dept_names(raw: str) -> list[str]:
  return [n.strip() for n in (raw or '').split(',') if n.strip()]


def _print_columns(rows: list[dict]) -> None:
  for row in rows or []:
    flag = '' if row.get('dept_id') else '  （未解析 dept_id，先 --refresh-depts --yes）'
    print(f"{_dept_label(row):<12} {row.get('dept_id') or '-':<16} {flag}".rstrip())


async def _run(args) -> int:
  from service.doc_update import depts

  if args.depts:
    _print_columns(await depts.dept_columns())
    return 0

  if args.refresh_depts:
    if not args.yes:
      print('>>> 刷新部门注册表会写 Mongo（doc_update_depts），加 --yes 执行；当前：')
      _print_columns(await depts.dept_columns())
      return 0
    rows = await depts.ensure_registry(force=True)
    print('>>> 已刷新部门注册表：')
    _print_columns(rows)
    return 0

  items = None
  keyword = (args.name or '').strip()
  dept_names = _dept_names(args.dept) or None
  companies = _dept_names(args.company) or None
  if keyword:
    items = _resolve(keyword)
    if items is None:
      return 1

  if not args.sync:
    overview = await workflow.build_overview(args.period or None)
    if overview.get('missing_period'):
      periods = overview.get('periods') or []
      print(f">>> 该期没有快照：{overview['missing_period']}")
      if periods:
        print('>>> 可用期号：' + ' / '.join(p.get('period_key') or '' for p in periods))
      return 0
    if not overview['latest']:
      print('>>> 库里还没有快照。首次请跑：python -m service.doc_update.cli --sync --yes')
      return 0
    verdict = overview['verdict']
    if dept_names or companies:
      verdict = _filter_depts(verdict, dept_names or [], companies)
    if items:
      verdict = workflow.subset_verdict(verdict, {i.seq for i in items})
    _print_verdict(verdict)
    return 0

  if items:
    result = await workflow.refresh_items(
      items, created_by='cli' if args.yes else '', persist=args.yes,
      dept_names=dept_names, companies=companies,
    )
    _print_verdict(result['verdict'])
    print('\n>>> 已落库' if result['stored'] else '\n>>> 未写库（加 --yes 才保存）')
    return 0

  print(f">>> 清单：{summary()['total']} 条 / 月度在范围 {summary()['in_scope']} 条")
  result = await workflow.perform_sync(
    created_by='cli' if args.yes else '', persist=args.yes, dept_names=dept_names,
    companies=companies,
  )
  _print_verdict(result['verdict'])
  if args.yes:
    print('\n>>> 已落库')
  else:
    print('\n>>> 未写库（加 --yes 才保存）')
  return 0


def main() -> int:
  parser = argparse.ArgumentParser(description='文档更新检查（部门 × 25 项月度快照）')
  parser.add_argument('--sync', action='store_true', help='实际取一期快照（会下载）')
  parser.add_argument('--name', default='', help='只看一个文档（传文件名，如 能源月报）')
  parser.add_argument('--dept', default='', help='只看/只跑指定部门，逗号分隔（如 A1,A2）')
  parser.add_argument('--company', default='',
                      help='只看/只跑指定公司（companyId 或标签，如 廊坊 / 平湖）')
  parser.add_argument('--depts', action='store_true', help='只看固定部门列（不联网、不看快照）')
  parser.add_argument('--period', default='', help='看历史期快照（YYYY-MM，默认最新一期）')
  parser.add_argument('--refresh-depts', action='store_true',
                      help='刷新部门注册表（解析 dept_id/root_fid，需 --yes 才写库）')
  parser.add_argument('--yes', action='store_true', help='与 --sync / --refresh-depts 联用：落库')
  return asyncio.run(_run(parser.parse_args()))


if __name__ == '__main__':
  sys.exit(main())

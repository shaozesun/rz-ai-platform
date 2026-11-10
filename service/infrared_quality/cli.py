"""本机核对：默认 dry-run（只打印任务/图片数与前几行，不调 Gradio、不写库）。

  python -m service.infrared_quality.cli --dept A1 --task 2CkNdoO79J3
  python -m service.infrared_quality.cli --dept A1 --unchecked --yes
  python -m service.infrared_quality.cli --predict /tmp/a.jpg [/tmp/b.jpg]

`--dept` 是部门短名（A1、A2…），跨公司同名时用 `--company` 限定。
`--predict` 只做预检 + 推理并打印标签/分数，不读 mgmt、不写库（与模型负责人对齐用）。
"""

import argparse
import asyncio
import json
import sys

from service.doc_update import depts as doc_depts
from service.infrared_quality import fetch, predict, store, workflow


def _pick_dept(columns: list[dict], name: str, company: str) -> dict:
  hits = [c for c in columns if c.get('dept_name') == name and c.get('dept_id')]
  if company:
    hits = [c for c in hits
            if company in (c.get('company_id') or '', c.get('company_name') or '')]
  if not hits:
    raise SystemExit(f'找不到部门 {name!r}（先用 --dept 指定 A1、A2…，多公司时加 --company）')
  if len(hits) > 1:
    labels = '、'.join(f'{h.get("company_name")}·{h.get("dept_name")}' for h in hits)
    raise SystemExit(f'部门 {name!r} 在多家公司都存在：{labels}；请用 --company 限定')
  return hits[0]


async def _predict(paths: list[str]) -> int:
  """只调推理服务，打印每张图的标签与分数（不读 mgmt、不写库）。"""
  client = predict.client()
  try:
    await client.preflight()
  except predict.InfraredUnavailable as exc:
    print(f'检测服务不可达：{exc}')
    return 1
  result = await client.predict_batch(paths)
  for path in paths:
    hit = result.get(path) or {}
    print(f'{path}  {hit.get("label") or "(无标签)"}  '
          f'{hit.get("score")}  {hit.get("error") or ""}')
  return 0


async def _run(args) -> int:
  if args.predict:
    return await _predict(args.predict)
  if not args.dept:
    raise SystemExit('缺少 --dept（或改用 --predict 复测单张图片）')
  columns = await doc_depts.dept_columns()
  dept = _pick_dept(columns, args.dept, args.company)
  tasks = await fetch.list_tasks(dept['dept_id'], refresh=args.refresh_tasks)
  checked = {c['task_id']: c for c in await store.list_checked(dept['dept_id'])}

  if args.task:
    wanted = set(args.task)
    tasks = [t for t in tasks if str(t.get('inspectionTaskId')) in wanted]
  elif args.unchecked:
    tasks = [t for t in tasks if str(t.get('inspectionTaskId')) not in checked]

  if not tasks:
    print('没有可检测的任务')
    return 0

  print(f'部门：{dept.get("company_name")} · {dept.get("dept_name")}（{dept["dept_id"]}）')
  for t in tasks:
    tid = str(t.get('inspectionTaskId'))
    mark = '已检测' if tid in checked else '未检测'
    print(f'  {tid}  巡检人={t.get("inspector")}  设备={t.get("deviceCount")}  '
          f'状态={t.get("status")}  {mark}')

  if args.dry_run:
    first = tasks[0]
    detail = await fetch.task_detail(str(first.get('inspectionTaskId')))
    devices = fetch.flatten_devices(detail)
    photos = workflow.photo_devices(devices)
    print(f'\n[ dry-run ] 首个任务 {first.get("inspectionTaskId")}：'
          f'{len(devices)} 台设备，{len(photos)} 张图片（只检测有图片的设备）')
    for d in photos[:5]:
      print('  ', json.dumps({k: d[k] for k in
                               ('name', 'num', 'type_name', 'installation_site',
                                'picture')}, ensure_ascii=False)[:200])
    print('\n未写库、未调用推理服务。加 --yes 执行。')
    return 0

  result = await workflow.perform_check(
    dept, tasks, created_by=args.user,
    progress=lambda **kw: print('  进度', kw.get('phase'), kw.get('done'),
                                '/', kw.get('total'), kw.get('current') or ''),
    refresh=args.refresh,
  )
  print(json.dumps(result, ensure_ascii=False, indent=2))
  return 0 if result.get('status') in ('success', 'partial') else 1


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description='红外巡检图片质量检测（本机核对）')
  parser.add_argument('--dept', default='', help='部门短名，如 A1（--predict 时不需要）')
  parser.add_argument('--predict', action='append', default=[], metavar='FILE',
                      help='只推理这些图片并打印标签/分数（不读 mgmt、不写库）')
  parser.add_argument('--company', default='', help='公司 id 或名称（跨公司同名部门时必填）')
  parser.add_argument('--task', action='append', default=[], help='指定任务 id（可重复）')
  parser.add_argument('--unchecked', action='store_true', help='只检测未检测过的任务')
  parser.add_argument('--refresh', action='store_true', help='忽略推理缓存重新跑')
  parser.add_argument('--refresh-tasks', action='store_true', help='绕过任务列表缓存')
  parser.add_argument('--yes', dest='dry_run', action='store_false', default=True,
                      help='真正执行并写库（默认 dry-run）')
  parser.add_argument('--user', default='cli', help='写入 checked_by')
  args = parser.parse_args(argv)
  return asyncio.run(_run(args))


if __name__ == '__main__':
  sys.exit(main())

"""事件工单详情只读探测（不写库、不落盘）—— 现场确认字段路径与「影响时长」单位。

为什么需要：报告要新增「影响时长」维度的深度分析，而 `influenceDuration` /
`eventReasonAnalyse` / `eventHandleProcesses` / `eventProcessesResult` **只在详情接口**
（列表 `data.csv` 没有）。字段路径、单位（毫秒 / 分钟 / 平台算好的可读串）都不能靠猜：
猜错会把 3 小时写成 3 毫秒，或把「3小时20分」当数字解析成 0。

两种用法（都只读）：

  # A. 按 19 位 id 直连（自登服务账号，不依赖数据集与 Redis）
  python deploy/scripts/event_detail_probe.py --event-id 2115278263667527680 --raw

  # B. 按最近一份事件报告数据集里的编号（复用 fetch_row_details，含 ticketNo→id 映射）
  python deploy/scripts/event_detail_probe.py --n 3

输出：
  1) `_DETAIL_FIELDS` 里登记的每个路径**是否命中**（路径失效是硬前提，得先修再分析）
  2) 「影响时长」「影响范围」的 `repr` 原值与类型（定 impact.py 的分档与单位换算）
  3) `ticketContent` 各顶层字段的非空率（看还有哪些字段可挖）
  4) `--raw` 时打印完整响应
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import settings  # noqa: E402

_EVENT_CAP = 'mgmt.query_event_ticket_detail'


async def _login() -> tuple[str, str]:
  """服务账号登录（自己发请求，不碰 Redis 缓存）。返回 (token, deptId)。"""
  import httpx

  if not settings.MGMT_USERNAME or not settings.MGMT_PASSWORD:
    raise SystemExit('未配置 MGMT_USERNAME/MGMT_PASSWORD，无法登录')
  url = f'{settings.MGMT_BASE_URL}/user/login/v1'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.post(url, json={'user': settings.MGMT_USERNAME,
                                        'password': settings.MGMT_PASSWORD})
    resp.raise_for_status()
    body = resp.json()
  if body.get('code') != 1:
    raise SystemExit(f'登录失败: {body.get("msg", "未知错误")}')
  data = body.get('data') or {}
  user = data.get('user') or {}
  token, dept = data.get('token', ''), user.get('deptId', '')
  if not token:
    raise SystemExit('登录响应缺少 token')
  print(f'>>> 登录成功 deptId={dept or "-"} 账号={settings.MGMT_USERNAME}')
  return token, dept


async def _fetch_by_id(event_id: str, token: str, dept: str) -> dict:
  import httpx

  base = settings.MGMT_TICKET_BASE_URL
  url = f'{base}/wiporder/event/{event_id}'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.get(url, headers={'token': token, 'Deptid': dept})
    resp.raise_for_status()
    body = resp.json()
  print(f'>>> GET {url} → code={body.get("code")} msg={body.get("msg")}')
  return body


def _find_dataset_dir() -> Path | None:
  """最近修改的一份事件报告数据集目录（含 data.csv）。"""
  root = Path(settings.AGENT_ANALYSIS_DATA_DIR)
  if not root.is_dir():
    return None
  cands = [p for p in root.glob('*/event-report-*') if (p / 'data.csv').is_file()]
  return max(cands, key=lambda p: p.stat().st_mtime) if cands else None


def _recent_ticket_nos(ds_dir: Path, n: int) -> list[str]:
  import csv

  with (ds_dir / 'data.csv').open(encoding='utf-8', newline='') as f:
    nos = [str(r.get('ticketNo') or '').strip() for r in csv.DictReader(f)]
  return list(dict.fromkeys([x for x in nos if x]))[:n]


async def _fetch_by_dataset(ds_dir: Path, nos: list[str]) -> list[dict]:
  from service.query.bootstrap import register_enabled_platforms
  from tools.analysis_tools import fetch_row_details

  register_enabled_platforms()
  # 目录形如 {DATA_DIR}/{thread_id}/{dataset_id}
  thread_id, dataset_id = ds_dir.parent.name, ds_dir.name
  print(f'>>> 数据集 thread={thread_id} dataset={dataset_id} 取 {len(nos)} 条详情')
  payload = await fetch_row_details(thread_id, dataset_id, nos,
                                   user_id=settings.SCHEDULED_TASK_SERVICE_USER_ID or None)
  return payload.get('rows') or []


def _report_fields(resp: dict) -> None:
  """逐条打印登记路径是否命中 + 影响时长/影响范围原值 + ticketContent 非空率。"""
  from tools.analysis_tools import _DETAIL_FIELDS, _dig

  print('\n--- _DETAIL_FIELDS 命中情况 ---')
  for entry in _DETAIL_FIELDS.get(_EVENT_CAP, ()):
    label, path = entry[0], entry[1]
    join_key = entry[2] if len(entry) > 2 else ''
    value = _dig(resp, path)
    hit = value not in (None, '', [], {})
    print(f'  {"OK " if hit else "MISS"} {label:<6} {path}'
          f'{" ." + join_key if join_key else ""}  → {_preview(value)}')

  content = resp.get('ticketContent') or {}
  print('\n--- 影响时长 / 影响范围 原值 ---')
  for key in ('influenceDuration', 'influenceScope', 'eventCompletionTime',
              'occurrenceTime', 'level', 'eventStage'):
    raw = content.get(key)
    print(f'  {key:<22} type={type(raw).__name__:<6} repr={raw!r}')

  print('\n--- ticketContent 顶层字段非空率 ---')
  total = len(content) or 1
  filled = []
  for key, value in content.items():
    if value not in (None, '', [], {}):
      filled.append((key, str(value)[:40]))
  print(f'  非空 {len(filled)} / {len(content)}')
  for key, sample in sorted(filled):
    print(f'    {key:<28} {sample}')


def _preview(value) -> str:
  if isinstance(value, list):
    return f'[{len(value)} 项]'
  text = repr(value)
  return text if len(text) <= 70 else text[:70] + '…'


async def main() -> int:
  parser = argparse.ArgumentParser(description='事件工单详情只读探测')
  parser.add_argument('--dataset-dir', default='', help='事件报告数据集目录（默认取最近一份）')
  parser.add_argument('--n', type=int, default=1, help='数据集模式下取前 N 个编号（默认 1）')
  parser.add_argument('--event-id', default='', help='直连单个 19 位工单 id（不依赖数据集）')
  parser.add_argument('--raw', action='store_true', help='打印完整响应 JSON')
  args = parser.parse_args()

  if args.event_id:
    token, dept = await _login()
    body = await _fetch_by_id(args.event_id, token, dept)
    resp = body.get('data') or {}
    if args.raw:
      print(json.dumps(body, ensure_ascii=False, indent=2)[:20000])
    _report_fields(resp)
    return 0

  ds_dir = Path(args.dataset_dir) if args.dataset_dir else _find_dataset_dir()
  if not ds_dir or not ds_dir.is_dir():
    print('未找到事件报告数据集，请用 --dataset-dir 指定或改用 --event-id')
    return 2
  print(f'>>> 数据集目录 {ds_dir}')
  nos = _recent_ticket_nos(ds_dir, max(1, args.n))
  rows = await _fetch_by_dataset(ds_dir, nos)
  for row in rows:
    print(f'\n=== {row.get("ticketNo")} ===')
    if row.get('error'):
      print(f'  取数失败：{row["error"]}')
      continue
    fields = row.get('fields') or {}
    print('  裁剪字段：')
    for key, value in fields.items():
      print(f'    {key:<8} {_preview(value)}')
  return 0


if __name__ == '__main__':
  raise SystemExit(asyncio.run(main()))

#!/usr/bin/env python3
"""Langfuse 空 trace 回填：把图执行根观察（LangGraph span）上的 name/input/output
补写回 trace 本体。

**问题（2026-11-02 排查）**：2026-10-08 起大量 trace 的 name/input/output 为空，
但 trace 内部的观察数据完好。原因是图执行根 span 挂在一个「只开始不结束」的父 span
下（OTEL 只在 end() 时导出，那个父 span 永远进不了库），Langfuse 找不到 trace 根，
就只留了个空壳 trace。代码侧已修（见 docs/problems/agent/langfuse-empty-traces.md），
但历史记录不会自己变好 —— 本脚本按 Langfuse 官方 ingestion 接口补写。

判据（只处理同时满足的 trace）：
  - trace.name 为空；
  - 能找到一个 `is_langchain_root=true` 的根观察（没有就退回 name == 'LangGraph'），
    且它至少带 input 或 output。

补写内容：name / input / output / userId（观察 metadata 的 user_id）/ sessionId
（同 session_id）/ environment。**不新增、不删除观察**，只更新 trace 本体的字段。

用法（凭据默认取环境变量 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST）：

    python deploy/scripts/langfuse_backfill_traces.py                  # dry-run，只统计
    python deploy/scripts/langfuse_backfill_traces.py --trace-id XXX   # 单条试跑（dry-run）
    python deploy/scripts/langfuse_backfill_traces.py --yes            # 真写
    python deploy/scripts/langfuse_backfill_traces.py --yes --limit 20 # 只补最近 20 条

退出码：0 正常；1 有写失败的批次（已成功的部分不回滚，重跑幂等）。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT_HOST = 'http://10.100.17.151:3001'
BATCH_SIZE = 10   # 单批条数：输出最长见过 138K 字符，10 条一批 ≈1MB，稳妥
PAGE_SIZE = 100   # 列表接口上限就是 100，超过会被 400


def _request(url: str, auth: str, *, payload: dict | None = None) -> tuple[int, dict]:
  """发一次请求；非 2xx 也把 body 读回来，方便看 Langfuse 的报错文案。"""
  data = json.dumps(payload).encode() if payload is not None else None
  headers = {'Authorization': auth}
  if data is not None:
    headers['Content-Type'] = 'application/json'
  req = urllib.request.Request(
    url, data=data, headers=headers, method='POST' if data is not None else 'GET',
  )
  try:
    with urllib.request.urlopen(req, timeout=120) as resp:
      return resp.status, json.loads(resp.read().decode() or '{}')
  except urllib.error.HTTPError as e:
    body = e.read().decode(errors='replace')
    try:
      return e.code, json.loads(body)
    except ValueError:
      return e.code, {'raw': body[:500]}


def list_traces(base: str, auth: str) -> list[dict]:
  """翻页拉全量 trace 概要。"""
  out: list[dict] = []
  page = 1
  while True:
    status, body = _request(f'{base}/api/public/traces?limit={PAGE_SIZE}&page={page}', auth)
    if status != 200:
      raise SystemExit(f'列 trace 失败 HTTP {status}: {json.dumps(body, ensure_ascii=False)[:300]}')
    out.extend(body.get('data') or [])
    pages = (body.get('meta') or {}).get('totalPages') or 1
    if page >= pages:
      return out
    page += 1


def pick_root(observations: list[dict]) -> dict | None:
  """挑出图执行的根观察：优先 is_langchain_root=true，退回名字 LangGraph，
  多个候选取 output 最长的那个（总结性最强）。"""
  roots = [o for o in observations if (o.get('metadata') or {}).get('is_langchain_root')]
  if not roots:
    roots = [o for o in observations if o.get('name') == 'LangGraph']
  roots = [o for o in roots if o.get('input') is not None or o.get('output') is not None]
  if not roots:
    return None
  return max(roots, key=lambda o: len(str(o.get('output') or '')))


def build_event(trace: dict, root: dict) -> dict:
  """组装一条 trace-create 事件（Langfuse 官方方式更新 trace 本体）。"""
  md = root.get('metadata') or {}
  return {
    'id': str(uuid.uuid4()),
    'type': 'trace-create',
    'timestamp': trace.get('timestamp') or None,
    'body': {
      'id': trace['id'],
      'timestamp': trace.get('timestamp') or None,
      'name': root.get('name') or 'LangGraph',
      'input': root.get('input'),
      'output': root.get('output'),
      'userId': md.get('user_id') or md.get('langfuse_user_id'),
      'sessionId': md.get('session_id') or md.get('langfuse_session_id'),
      'environment': trace.get('environment') or 'default',
      'tags': trace.get('tags') or [],
      'release': trace.get('release') or None,
      'version': trace.get('version') or None,
      'metadata': {'backfilled_from_root': root.get('id')},
    },
  }


def main() -> int:
  parser = argparse.ArgumentParser(description='回填 Langfuse 空 trace 的 name/input/output')
  parser.add_argument('--host', default=os.getenv('LANGFUSE_HOST') or DEFAULT_HOST)
  parser.add_argument('--public-key', default=os.getenv('LANGFUSE_PUBLIC_KEY', ''))
  parser.add_argument('--secret-key', default=os.getenv('LANGFUSE_SECRET_KEY', ''))
  parser.add_argument('--trace-id', default='', help='只处理这一条（先用它小样验证）')
  parser.add_argument('--limit', type=int, default=0, help='最多处理多少条（0 = 全部）')
  parser.add_argument('--yes', action='store_true', help='真写；缺省 dry-run 只统计')
  parser.add_argument('--sleep', type=float, default=0.2, help='每批之间的间隔秒数')
  args = parser.parse_args()

  if not args.public_key or not args.secret_key:
    raise SystemExit('缺 LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY（环境变量或 --public-key/--secret-key）')
  base = args.host.rstrip('/')
  if base.startswith('http') is False:
    base = f'http://{base}'
  auth = 'Basic ' + base64.b64encode(f'{args.public_key}:{args.secret_key}'.encode()).decode()

  traces = list_traces(base, auth)
  empty = [t for t in traces if not (t.get('name') or '').strip()]
  if args.trace_id:
    empty = [t for t in empty if t['id'] == args.trace_id]
    if not empty:
      raise SystemExit(f'trace {args.trace_id} 不存在或 name 非空，无需回填')
  if args.limit:
    empty = empty[:args.limit]

  print(f'共 {len(traces)} 条 trace，其中 name 为空 {len(empty)} 条（本次处理范围）')

  events: list[dict] = []
  skipped = 0
  for i, t in enumerate(empty, 1):
    status, detail = _request(f'{base}/api/public/traces/{t["id"]}', auth)
    if status != 200:
      print(f'  [{i}/{len(empty)}] {t["id"]} 取详情失败 HTTP {status}，跳过')
      skipped += 1
      continue
    root = pick_root(detail.get('observations') or [])
    if not root:
      skipped += 1
      continue
    events.append(build_event(t, root))

  print(f'可回填 {len(events)} 条，跳过 {skipped} 条（无可用根观察）')
  if not events:
    return 0
  sample = events[0]['body']
  print('样例：')
  print(f'  trace   {sample["id"]}')
  print(f'  name    {sample["name"]}')
  print(f'  user    {sample["userId"]}')
  print(f'  session {sample["sessionId"]}')
  print(f'  input   {str(sample["input"])[:120]}')
  print(f'  output  {str(sample["output"])[:120]}…')
  if not args.yes:
    print('（dry-run，未写入；确认无误后加 --yes）')
    return 0

  failed = 0
  for start in range(0, len(events), BATCH_SIZE):
    batch = events[start:start + BATCH_SIZE]
    status, body = _request(f'{base}/api/public/ingestion', auth, payload={'batch': batch})
    errs = body.get('errors') or []
    ok = len(body.get('successes') or [])
    print(f'  批次 {start // BATCH_SIZE + 1}: HTTP {status} 成功 {ok} 失败 {len(errs)}')
    if status not in (200, 207) or errs:
      failed += 1
      print(f'    错误样例: {json.dumps(errs[:1], ensure_ascii=False)[:300]}')
    time.sleep(args.sleep)

  print(f'完成：{len(events) - failed * BATCH_SIZE} 条左右已提交，失败批次 {failed}')
  return 1 if failed else 0


if __name__ == '__main__':
  sys.exit(main())

"""文档更新检查探针（只读，不写库、不落盘）—— 确认综合管理平台的文件能否下载并算 sha256。

为什么需要：月度文档更新检查曾因时间字段不可信（`updateTime`/`uploadTime` 方向不一致、
大批文件同一天 `uploadTime`）而暂缓，拟改用内容哈希（定期快照 `fid → sha256`，与上期比对）。
前提是文件能下载 —— 而接口文档里 `POST /file/v1/download/{fid}`、
`POST /file/v1/minio/download/{fid}` 的返回体都只写了占位 `{code,data:{}}`，
`GET /file/v1/minio/url/{fileId}` 的 data 结构也没登记。本脚本只验证这件事。

用法（全部只读，下载的字节只算哈希、不写文件）：
  python deploy/scripts/doc_update_probe.py                       # 整树 + 全部文件 meta，下载前 3 个试两条通道
  python deploy/scripts/doc_update_probe.py --limit 8 --numbers 1.2.2,1.2.5
  python deploy/scripts/doc_update_probe.py --no-download          # 只看 meta，不下载
  python deploy/scripts/doc_update_probe.py --out /tmp/docs.json   # 额外把 meta 快照写盘

输出：文件名 / 大小 / version / updateTime / uploadTime / 下载通道 / sha256 / 耗时，末尾汇总。
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import settings  # noqa: E402

TREE_P3ID = '24CiSBO7cxt'  # 「数据中心运维管理体系」整树
DIR_PAGE_SIZE = 200
URL_KEYS = ('url', 'presignedUrl', 'downloadUrl', 'path', 'minioUrl', 'data')


def _headers(token: str, dept: str) -> dict:
  return {'token': token, 'Deptid': dept or ''}


async def _login() -> tuple[str, str]:
  """服务账号登录。只调纯函数、不写 Redis 缓存（`_login()` 才写）。"""
  from service.query.mgmt.http import MgmtError, _login_with_credentials

  if not settings.MGMT_USERNAME or not settings.MGMT_PASSWORD:
    raise SystemExit('未配置 MGMT_USERNAME/MGMT_PASSWORD，无法登录')
  try:
    ctx = await _login_with_credentials(settings.MGMT_USERNAME, settings.MGMT_PASSWORD)
  except MgmtError as exc:
    raise SystemExit(f'登录失败: {exc}')
  dept = ctx.get('dept_id') or ''
  print(f'>>> 登录成功 deptId={dept or "-"} 账号={settings.MGMT_USERNAME}')
  return ctx['token'], dept


async def _get_json(path: str, token: str, dept: str) -> dict:
  import httpx

  url = f'{settings.MGMT_BASE_URL}{path}'
  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
    resp = await client.get(url, headers=_headers(token, dept))
    resp.raise_for_status()
    return resp.json()


def collect_files(root: dict) -> tuple[list[dict], dict]:
  """递归收集文件节点（type=0；分类节点是 type=1），返回 (文件列表, 树的统计)。"""
  files: list[dict] = []
  stats = {'nodes': 0, 'max_depth': 0, 'keys': set()}

  def walk(node: dict, parent_id: str, trail: list[str], depth: int) -> None:
    stats['nodes'] += 1
    stats['max_depth'] = max(stats['max_depth'], depth)
    stats['keys'].update(node.keys())
    topic = str(node.get('topic') or '').strip()
    node_id = str(node.get('id') or '')
    children = node.get('children') or []
    if str(node.get('type')) == '0' and not children:
      files.append({'fid': node_id, 'name': topic, 'path': ' / '.join(trail),
                    'parent_id': parent_id})
      return
    for child in children:
      walk(child, node_id, trail + [topic] if topic else trail, depth + 1)

  walk(root, '', [], 0)
  return files, stats


async def fetch_dir_meta(parent_id: str, token: str, dept: str) -> dict[str, dict]:
  """取某目录下的文件清单（version/size/updateTime/uploadTime），按 fid 索引。"""
  if not parent_id:
    return {}
  body = await _get_json(f'/file/v1/{quote(parent_id)}/1/{DIR_PAGE_SIZE}', token, dept)
  if body.get('code') != 1:
    raise RuntimeError(body.get('msg') or '目录列表返回异常')
  data = body.get('data') or {}
  return {str(v.get('fid')): v for v in (data.get('value') or [])}


def extract_url(data, depth: int = 0) -> str:
  """从下载接口的 data 里挑出地址。

  实测 `minio/url` 把地址藏在 `data.document.url`（不是顶层 `url`），故递归找。
  """
  if isinstance(data, str):
    return data if data.startswith('http') else ''
  if isinstance(data, dict) and depth < 3:
    for key in URL_KEYS:
      value = data.get(key)
      if isinstance(value, str) and value.startswith('http'):
        return value
    for value in data.values():
      if isinstance(value, (dict, list)):
        found = extract_url(value, depth + 1)
        if found:
          return found
  if isinstance(data, list) and depth < 3:
    for value in data:
      found = extract_url(value, depth + 1)
      if found:
        return found
  return ''


async def download_bytes(url: str, token: str, dept: str) -> tuple[bytes, str]:
  """按地址取字节。域内地址带 token/Deptid 头；预签名 OSS 地址带了也无害。"""
  import httpx

  async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT, follow_redirects=True) as client:
    resp = await client.get(url, headers=_headers(token, dept))
    resp.raise_for_status()
    return resp.content, resp.headers.get('content-type', '')


async def probe_minio_url(fid: str, token: str, dept: str) -> dict:
  """通道 a：GET /file/v1/minio/url/{fileId} → 取地址 → 下载算 sha256。"""
  started = time.monotonic()
  result = {'channel': 'minio/url', 'ok': False, 'detail': '', 'sha256': '',
            'bytes': 0, 'elapsed': 0.0}
  try:
    body = await _get_json(f'/file/v1/minio/url/{quote(fid)}', token, dept)
    data = body.get('data')
    url = extract_url(data)
    result['detail'] = f'code={body.get("code")} data={type(data).__name__} url={"有" if url else "无"}'
    if not url:
      result['detail'] += f' 前 120 字: {str(data)[:120]}'
      return result
    content, ctype = await download_bytes(url, token, dept)
    result.update(ok=True, bytes=len(content), sha256=hashlib.sha256(content).hexdigest(),
                  detail=f'{result["detail"]} ctype={ctype or "-"}')
  except Exception as exc:  # noqa: BLE001  探针：失败也要继续下一个
    result['detail'] = f'{type(exc).__name__}: {exc}'
  result['elapsed'] = round(time.monotonic() - started, 2)
  return result


async def probe_post_download(fid: str, token: str, dept: str) -> dict:
  """通道 b：POST /file/v1/download/{fid} —— 返回可能是字节流，也可能是带地址的 JSON。"""
  import httpx

  started = time.monotonic()
  result = {'channel': 'download', 'ok': False, 'detail': '', 'sha256': '',
            'bytes': 0, 'elapsed': 0.0}
  try:
    url = f'{settings.MGMT_BASE_URL}/file/v1/download/{quote(fid)}'
    async with httpx.AsyncClient(timeout=settings.MGMT_TIMEOUT) as client:
      resp = await client.post(url, headers=_headers(token, dept))
      resp.raise_for_status()
      ctype = resp.headers.get('content-type', '')
      if 'json' in ctype.lower():
        body = resp.json()
        data = body.get('data')
        link = extract_url(data)
        result['detail'] = f'json code={body.get("code")} url={"有" if link else "无"} 前 120 字: {str(data)[:120]}'
        if link:
          content, _ = await download_bytes(link, token, dept)
          result.update(ok=True, bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest())
      else:
        content = resp.content
        result.update(ok=bool(content), bytes=len(content), detail=f'二进制 ctype={ctype or "-"}',
                      sha256=hashlib.sha256(content).hexdigest() if content else '')
  except Exception as exc:  # noqa: BLE001
    result['detail'] = f'{type(exc).__name__}: {exc}'
  result['elapsed'] = round(time.monotonic() - started, 2)
  return result


def _human_size(size) -> str:
  try:
    value = float(size or 0)
  except (TypeError, ValueError):
    return '—'
  if value <= 0:
    return '—'
  for unit in ('B', 'KB', 'MB', 'GB'):
    if value < 1024 or unit == 'GB':
      return f'{value:.0f}{unit}' if unit == 'B' else f'{value:.1f}{unit}'
    value /= 1024
  return '—'


def _matches(name: str, numbers: list[str]) -> bool:
  if not numbers:
    return True
  head = name.split(' ')[0]
  return any(head.startswith(n) for n in numbers)


async def main() -> int:
  parser = argparse.ArgumentParser(description='文档更新检查探针（只读）')
  parser.add_argument('--limit', type=int, default=3, help='下载探测的文件个数（默认 3）')
  parser.add_argument('--numbers', default='', help='只处理这些编号前缀，逗号分隔（如 1.2.2,1.2.5）')
  parser.add_argument('--no-download', action='store_true', help='只看 meta，不下载')
  parser.add_argument('--out', default='', help='把 meta 快照写成 JSON（默认不落盘）')
  args = parser.parse_args()

  token, dept = await _login()

  body = await _get_json(f'/file/v1/getAll/{quote(TREE_P3ID)}', token, dept)
  if body.get('code') != 1:
    raise SystemExit(f'整树接口异常: {body.get("msg")}')
  files, stats = collect_files(body.get('data') or {})
  print(f'>>> 整树：{stats["nodes"]} 节点 / 最大深度 {stats["max_depth"]} / 文件节点 {len(files)} 个')
  print(f'>>> 节点字段：{sorted(stats["keys"])}')

  numbers = [n.strip() for n in args.numbers.split(',') if n.strip()]
  picked = [f for f in files if _matches(f['name'], numbers)]
  meta_cache: dict[str, dict[str, dict]] = {}
  rows: list[dict] = []
  for item in picked:
    parent = item['parent_id']
    if parent not in meta_cache:
      try:
        meta_cache[parent] = await fetch_dir_meta(parent, token, dept)
      except Exception as exc:  # noqa: BLE001
        meta_cache[parent] = {}
        print(f'!! 目录 {parent} 列表失败: {type(exc).__name__}: {exc}')
    meta = meta_cache[parent].get(item['fid']) or {}
    rows.append({
      'fid': item['fid'],
      'name': meta.get('name') or item['name'],
      'path': item['path'],
      'size': meta.get('size'),
      'version': meta.get('version'),
      'updateTime': meta.get('updateTime'),
      'uploadTime': meta.get('uploadTime'),
      'sha256': '',
      'channel': '',
    })

  print(f'\n=== 文件清单（{len(rows)} 个）===')
  for row in rows:
    print(f'{row["name"]} | 大小={_human_size(row["size"])} | version={row["version"] or "-"}'
          f' | updateTime={row["updateTime"] or "-"} | uploadTime={row["uploadTime"] or "-"}')

  if not args.no_download and rows:
    print(f'\n=== 下载探测（前 {min(args.limit, len(rows))} 个，两条通道都试）===')
    elapsed_all: list[float] = []
    total_bytes = 0
    usable: dict[str, int] = {}
    for row in rows[:args.limit]:
      print(f'--- {row["name"]} (fid={row["fid"]})')
      for probe in (probe_minio_url, probe_post_download):
        res = await probe(row['fid'], token, dept)
        mark = 'OK' if res['ok'] else 'FAIL'
        print(f'  [{mark}] {res["channel"]:<10} {res["elapsed"]}s {_human_size(res["bytes"])}'
              f' sha256={res["sha256"][:16] or "-"} | {res["detail"]}')
        if res['ok']:
          usable[res['channel']] = usable.get(res['channel'], 0) + 1
          elapsed_all.append(res['elapsed'])
          total_bytes += res['bytes']
          if not row['sha256']:
            row['sha256'], row['channel'] = res['sha256'], res['channel']
    print('\n=== 汇总 ===')
    print(f'可用通道: {usable or "无"}')
    if elapsed_all:
      print(f'单文件平均耗时 {sum(elapsed_all) / len(elapsed_all):.2f}s / 总下载 {_human_size(total_bytes)}')

  if args.out:
    Path(args.out).write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n>>> 快照已写入 {args.out}')
  return 0


if __name__ == '__main__':
  raise SystemExit(asyncio.run(main()))

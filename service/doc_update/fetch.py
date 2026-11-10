"""取数 + 算哈希：整树 → 匹配 25 条月度项 → 逐文件流式算 sha256（**不落盘**）。

取数走综合管理平台的既有 HTTP 封装（`service/query/mgmt/http.request`，自带 401 重登）：
- 整树 `GET /file/v1/getAll/{tree_id}`（节点只有 id/topic/type/children，没有 size）
- 目录列表 `GET /file/v1/{pid}/1/200`（才有 size/version/updateTime）
- 下载地址 `GET /file/v1/minio/url/{fid}` → `data.document.url`（S3 预签名，**嵌套两层**）
- 字节：直接 GET 预签名地址，`httpx.stream` 边下边喂 hashlib，算完即弃

**必须串行**：mgmt 同账号 × 同接口同时只允许 1 个在途。单文件失败只记 `error`，
不中断整轮（否则一个坏文件让整期永远拿不到结果）。
"""

import hashlib
import inspect
import logging
import time
from datetime import datetime, timezone

import httpx

from config.settings import settings
from service.doc_update import depts, period, share
from service.doc_update.catalog import MONTHLY_ITEMS, DocItem
from service.doc_update.verdict import MISSING_TEXT

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024
_HEAD_BYTES = 1024
_URL_KEYS = ('url', 'presignedUrl', 'downloadUrl', 'path', 'minioUrl', 'data')
_ZIP_MAGIC = b'PK\x03\x04'
_MAGIC = {
  'xlsx': _ZIP_MAGIC, 'docx': _ZIP_MAGIC, 'pptx': _ZIP_MAGIC, 'zip': _ZIP_MAGIC,
  'pdf': b'%PDF-',
}


def _norm(text: str) -> str:
  """去首尾空白 + 全角空格（文档名里常见 `1.2.2 负载…`）。"""
  return (text or '').replace('\u3000', ' ').strip()


async def load_tree(tree_id: str = '') -> dict:
  from service.query.mgmt import http

  tree_id = tree_id or settings.DOC_UPDATE_TREE_ID
  data = await http.request('GET', f'/file/v1/getAll/{tree_id}')
  if not isinstance(data, dict):
    raise RuntimeError(f'整树接口返回异常：{type(data).__name__}')
  return data


def walk_files(root: dict) -> list[dict]:
  """递归收集文件节点（`type == 0` 且没有 children）。分类节点是 `type == 1`。"""
  out: list[dict] = []

  def walk(node: dict, parent_id: str, trail: list[str]) -> None:
    node_id = str(node.get('id') or '')
    topic = _norm(str(node.get('topic') or ''))
    children = node.get('children') or []
    if str(node.get('type')) == '0' and not children:
      out.append({
        'fid': node_id,
        'topic': topic,
        'parent_id': parent_id,
        'path': ' / '.join(trail),
      })
      return
    for child in children:
      walk(child, node_id, trail + [topic] if topic else trail)

  walk(root, '', [])
  return out


def _starts_with_number(topic: str, number: str) -> bool:
  """编号前缀匹配：后面不能再接数字或点（防 `1.6.6.1` 命中 `1.6.6.10`）。"""
  if not topic.startswith(number):
    return False
  rest = topic[len(number):]
  return not (rest[:1].isdigit() or rest[:1] == '.')


def _numbered(name: str, numbers) -> bool:
  return any(_starts_with_number(name, n) for n in numbers)


def _compact(text) -> str:
  """去所有空白（含全角）+ 转小写：真实目录名里编号后常带空格。"""
  return ''.join(str(text or '').replace('\u3000', ' ').split()).lower()


def _alias_hit(name: str, folders) -> bool:
  """实际名称匹配：目录/文件名里含登记过的真实写法（清单名只是模糊叫法）。"""
  text = _compact(name)
  return bool(text) and any(
    alias and alias in text for alias in (_compact(a) for a in folders or ())
  )


def match_item(item: DocItem, files: list[dict]) -> list[dict]:
  """挑出该清单项对应的文件（可能多个）。

  **实际名称优先**：真实共享树里的目录名常和 PDF 清单名对不上（清单只给「大概叫法」），
  故路径任一段或文件名含 `item.folders` 里登记过的写法即命中，不再过关键词闸。
  兜底仍按编号前缀（文件名或目录名），关键词（`4.1.5` 四条同名表）只对编号命中消歧。

  返回 `files` 的**副本**（同一文件可能命中多项，不能互相污染），
  附带 `match_dir` = 最深命中目录名（仅文件名命中记 `''`）。
  """
  hits = []
  folders = item.folders or ()
  for f in files:
    topic = f['topic']
    segments = f.get('segments') or []
    dir_alias = [s for s in segments if _alias_hit(s, folders)]
    if dir_alias or _alias_hit(topic, folders):
      hits.append(dict(f, match_dir=dir_alias[-1] if dir_alias else ''))
      continue
    dir_number = [s for s in segments if _numbered(s, item.numbers)]
    if not _numbered(topic, item.numbers) and not dir_number:
      continue
    if item.keywords:
      names = [topic, *dir_number]
      if not any(k in name for name in names for k in item.keywords):
        continue
    hits.append(dict(f, match_dir=dir_number[-1] if dir_number else ''))
  return hits


async def fetch_dir_meta(parent_id: str) -> dict[str, dict]:
  """目录下的文件清单（size/version/updateTime/uploadTime），按 fid 索引。"""
  from service.query.mgmt import http

  if not parent_id:
    return {}
  data = await http.request('GET', f'/file/v1/{parent_id}/1/200')
  values = (data or {}).get('value') if isinstance(data, dict) else None
  return {str(v.get('fid')): v for v in (values or []) if v.get('fid')}


def extract_url(data, depth: int = 0) -> str:
  """从下载接口的 data 里挑出地址（实测藏在 `data.document.url`，故递归找）。"""
  if isinstance(data, str):
    return data if data.startswith('http') else ''
  if isinstance(data, dict) and depth < 3:
    for key in _URL_KEYS:
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


def _expected_magic(file_type: str, file_name: str) -> bytes:
  kind = (file_type or '').lower().lstrip('.')
  if not kind:
    kind = file_name.rsplit('.', 1)[-1].lower() if '.' in file_name else ''
  return _MAGIC.get(kind, b'')


async def stream_sha256(url: str) -> tuple[str, int, bytes]:
  """流式下载算 sha256，返回 (hexdigest, 字节数, 头部若干字节)。**不写文件**。"""
  headers = {}
  if settings.MGMT_BASE_URL and url.startswith(settings.MGMT_BASE_URL):
    # 域内地址带鉴权头；S3 预签名地址不带（避免把 token 发给对象存储）
    from service.query.mgmt import http

    token, dept = await http._get_token_and_dept()
    headers = {'token': token, 'Deptid': dept or ''}

  digest = hashlib.sha256()
  total = 0
  head = b''
  async with httpx.AsyncClient(
    timeout=settings.DOC_UPDATE_TIMEOUT, follow_redirects=True,
  ) as client:
    async with client.stream('GET', url, headers=headers) as resp:
      resp.raise_for_status()
      async for chunk in resp.aiter_bytes(_CHUNK):
        if not chunk:
          continue
        if len(head) < _HEAD_BYTES:
          head += chunk[:_HEAD_BYTES - len(head)]
        digest.update(chunk)
        total += len(chunk)
  return digest.hexdigest(), total, head


async def download_sha256(fid: str, file_type: str = '', file_name: str = '') -> dict:
  """取一个文件的哈希。返回 `{sha256, download_size, error, warn}`，失败不抛异常。"""
  from service.query.mgmt import http

  out = {'sha256': '', 'download_size': 0, 'error': '', 'warn': ''}
  try:
    data = await http.request('GET', f'/file/v1/minio/url/{fid}')
    url = extract_url(data)
    if not url:
      out['error'] = f'下载地址为空（data 前 120 字：{str(data)[:120]}）'
      return out
    sha, size, head = await stream_sha256(url)
    magic = _expected_magic(file_type, file_name)
    if magic and not head.startswith(magic):
      out['error'] = f'内容类型不符（期望 {file_type} 文件头，实际前 8 字节 {head[:8]!r}）'
      return out
    out.update(sha256=sha, download_size=size)
  except Exception as exc:  # noqa: BLE001  单文件失败不能拖垮整期
    out['error'] = f'{type(exc).__name__}: {str(exc)[:200]}'
  return out


def _prev_index(previous: dict | None) -> dict[str, tuple[str, datetime | str]]:
  index: dict[str, tuple[str, datetime | str]] = {}
  for item in (previous or {}).get('items') or []:
    for f in item.get('files') or []:
      if f.get('fid'):
        index[str(f['fid'])] = (f.get('sha256') or '', f.get('last_changed_at') or '')
  return index


def _prev_flat_index(previous: dict | None) -> dict[str, tuple[str, str]]:
  """上期快照里所有文件的 (sha256, last_changed_at)，按 fid 索引（部门分支用）。"""
  index: dict[str, tuple[str, str]] = {}
  for dept in (previous or {}).get('depts') or []:
    for item in dept.get('items') or []:
      for f in item.get('files') or []:
        fid = str(f.get('fid') or '')
        if fid:
          index[fid] = (f.get('sha256') or '', f.get('last_changed_at') or '')
  return index


def _size_warn(meta: dict, download_size: int) -> str:
  """档案 size 与实际字节差 >10% 记「待复核」——只提示，不当失败。"""
  try:
    size = float(meta.get('size') or 0)
  except (TypeError, ValueError):
    return ''
  if size <= 0 or download_size <= 0:
    return ''
  diff = abs(size - download_size) / size
  if diff <= 0.10:
    return ''
  return f'实际字节与档案大小不符（档案 {size:.0f} / 实际 {download_size}，差 {diff:.0%}）'


async def take_snapshot(
  *,
  tree_id: str = '',
  previous: dict | None = None,
  progress=None,
  only: list[DocItem] | None = None,
) -> dict:
  """跑一期快照：清单项 → 匹配文件 → 逐文件算 hash。

  `only` 为空时取全部 25 条月度项（整期）；传子集则只算这几项（单项刷新用）。
  `progress(done, total, name)` 用于推送进度（对话工具防止 360s 静默超时）。
  `last_changed_at` 从上期结转：hash 没变就沿用上期时间，变了（或新增）记本次时间。
  """
  started = time.monotonic()
  taken_at = datetime.now(timezone.utc)
  scope = list(only or MONTHLY_ITEMS)
  tree = await load_tree(tree_id)
  files = walk_files(tree)
  prev = _prev_index(previous)
  meta_cache: dict[str, dict[str, dict]] = {}
  items: list[dict] = []
  budget = max(1, settings.DOC_UPDATE_MAX_FILES)

  for idx, item in enumerate(scope, 1):
    matched = match_item(item, files)
    records: list[dict] = []
    for f in matched:
      if budget <= 0:
        records.append({
          'fid': f['fid'], 'file_name': f['topic'], 'path': f['path'], 'size': None,
          'download_size': 0, 'version': '', 'update_time': '', 'upload_time': '',
          'sha256': '', 'warn': '',
          'error': f'超出 DOC_UPDATE_MAX_FILES={settings.DOC_UPDATE_MAX_FILES} 安全阀，未取',
          'last_changed_at': '',
        })
        continue
      budget -= 1
      parent = f['parent_id']
      if parent not in meta_cache:
        try:
          meta_cache[parent] = await fetch_dir_meta(parent)
        except Exception as exc:  # noqa: BLE001
          logger.warning('[DocUpdate] 目录列表失败 parent=%s: %s', parent, exc)
          meta_cache[parent] = {}
      meta = meta_cache[parent].get(f['fid']) or {}
      file_name = _norm(str(meta.get('name') or f['topic']))
      res = await download_sha256(
        f['fid'], file_type=str(meta.get('fileType') or ''), file_name=file_name,
      )
      prev_sha, prev_changed = prev.get(f['fid'], ('', ''))
      changed = bool(res['sha256']) and res['sha256'] != prev_sha
      records.append({
        'fid': f['fid'],
        'file_name': file_name,
        'path': f['path'],
        'size': meta.get('size'),
        'download_size': res['download_size'],
        'version': meta.get('version') or '',
        'update_time': str(meta.get('updateTime') or ''),
        'upload_time': str(meta.get('uploadTime') or ''),
        'sha256': res['sha256'],
        'error': res['error'],
        'warn': res['warn'] or _size_warn(meta, res['download_size']),
        'last_changed_at': taken_at if changed or not prev_changed else prev_changed,
      })

    items.append({
      'seq': item.seq,
      'number': item.number_label,
      'name': item.name,
      'cadence': item.cadence,
      'note': item.note,
      'files': records,
      'error': '' if records else MISSING_TEXT,
    })
    if progress:
      # 进度回调同步/异步都支持（对话工具要 await 才能推 SSE 事件）
      out = progress(idx, len(scope), item.name)
      if inspect.isawaitable(out):
        await out

  elapsed = round(time.monotonic() - started, 2)
  logger.info('[DocUpdate] 快照完成 items=%d files=%d elapsed=%ss',
              len(items), sum(len(i['files']) for i in items), elapsed)
  return {
    'taken_at': taken_at,
    'period_key': taken_at.astimezone(_tz()).strftime('%Y-%m'),
    'period_label': taken_at.astimezone(_tz()).strftime('%Y年%m月'),
    'tree_id': tree_id or settings.DOC_UPDATE_TREE_ID,
    'items': items,
    'file_count': sum(len(i['files']) for i in items),
    'elapsed': elapsed,
  }


def _period_record(f: dict) -> dict:
  """按期提交型的文件记录：只带位置信息，不下载不哈希。"""
  return {
    'fid': f['fid'],
    'file_name': f['topic'],
    'path': f['path'],
    'match_dir': f.get('match_dir') or '',
    'period': period.period_of_file(f),
    'size': f.get('size'),
    'download_size': 0,
    'sha256': '',
    'error': '',
    'warn': '',
    'last_changed_at': '',
  }


def _flat_record(f: dict, res: dict, prev: dict[str, tuple[str, str]],
                 taken_at: datetime) -> dict:
  """原地更新型的文件记录：哈希与上期比（新增或变化才更新 last_changed_at）。"""
  prev_sha, prev_changed = prev.get(f['fid'], ('', ''))
  changed = bool(res['sha256']) and res['sha256'] != prev_sha
  return {
    'fid': f['fid'],
    'file_name': f['topic'],
    'path': f['path'],
    'match_dir': f.get('match_dir') or '',
    'period': '',
    'size': f.get('size'),
    'download_size': res['download_size'],
    'sha256': res['sha256'],
    'error': res['error'],
    'warn': res['warn'] or _size_warn({'size': f.get('size')}, res['download_size']),
    'last_changed_at': taken_at if changed or not prev_changed else prev_changed,
  }


async def take_dept_snapshot(
  *,
  previous: dict | None = None,
  progress=None,
  only: list[DocItem] | None = None,
  dept_names: list[str] | None = None,
  companies: list[str] | None = None,
) -> dict:
  """跑一期「部门 × 清单项」快照。

  判据按目录形态分两种（`period.detect_layout` 自动判定）：
  - `period` 按期提交型：只看本期应到月份里有没有文件，**不下载、不哈希**；
  - `flat` 原地更新型：下载算 sha256，与上一期快照比。

  部门清单取固定配置 + 注册表缓存（`depts.resolve_departments`，缓存齐了不联网），
  传 `dept_names` / `companies` 时只跑指定部门/公司。单个部门/文件失败只记 `error`，
  不中断整轮。
  """
  started = time.monotonic()
  taken_at = datetime.now(timezone.utc)
  scope = list(only or MONTHLY_ITEMS)
  numbers = share.known_numbers()
  prev = _prev_flat_index(previous)
  budget = max(1, settings.DOC_UPDATE_MAX_FILES)
  expected = period.expected_period(taken_at, settings.DOC_UPDATE_PERIOD_GRACE_MONTHS)

  all_depts = await depts.resolve_departments(dept_names, companies)

  total_steps = max(1, len(all_depts) * len(scope))
  done = 0
  out_depts: list[dict] = []

  for dept in all_depts:
    entry = {
      'company_id': dept.get('company_id') or '',
      'company_name': dept.get('company_name') or '',
      'dept_id': dept['dept_id'],
      'dept_name': dept['dept_name'],
      'root_fid': '',
      'error': '',
      'items': [],
    }
    dept_label = (f'{entry["company_name"]} · ' if entry['company_name'] else '') \
      + dept['dept_name']
    files: list[dict] = []
    root = dept.get('root_fid') or ''
    if not root and dept.get('dept_id'):
      try:
        root = await depts.root_file_id(dept['dept_id'])
      except Exception as exc:  # noqa: BLE001  单个部门失败不拖垮整轮
        root = ''
        entry['error'] = f'部门详情获取失败：{type(exc).__name__}: {str(exc)[:200]}'
    elif dept.get('error'):
      entry['error'] = dept['error']
    if root:
      entry['root_fid'] = root
      if progress:
        # 大部门遍历目录可能耗时数分钟，进度别停在上个项目名上
        out = progress(done, total_steps, f'{dept_label} · 正在遍历目录')
        if inspect.isawaitable(out):
          await out
      try:
        files = await share.walk(root, numbers)
      except Exception as exc:  # noqa: BLE001
        entry['error'] = f'共享目录遍历失败：{type(exc).__name__}: {str(exc)[:200]}'
    elif not entry['error']:
      entry['error'] = '部门共享文档根为空（rootFileId 未配置）'

    dept_error = entry['error']
    for item in scope:
      done += 1
      matched = match_item(item, files) if files else []
      layout = period.detect_layout(matched)
      latest = period.pick_latest_period(matched)
      records: list[dict] = []
      if not dept_error:
        if layout == period.LAYOUT_PERIOD:
          records = [_period_record(f) for f in matched]
        else:
          for f in matched:
            if budget <= 0:
              records.append({
                'fid': f['fid'], 'file_name': f['topic'], 'path': f['path'],
                'match_dir': f.get('match_dir') or '',
                'period': '', 'size': f.get('size'), 'download_size': 0,
                'sha256': '', 'warn': '',
                'error': f'超出 DOC_UPDATE_MAX_FILES={settings.DOC_UPDATE_MAX_FILES} 安全阀，未取',
                'last_changed_at': '',
              })
              continue
            budget -= 1
            res = await share.download_sha256(f['fid'])
            records.append(_flat_record(f, res, prev, taken_at))
      # 实际命中的目录名（可能多处，如同一份月报存了三个地方），供页面展示核对
      actual_dirs = sorted({r['match_dir'] for r in records if r.get('match_dir')})
      entry['items'].append({
        'seq': item.seq,
        'number': item.number_label,
        'name': item.name,
        'cadence': item.cadence,
        'note': item.note,
        'layout': layout,
        'expected_period': expected,
        'latest_period': latest,
        'actual_dirs': actual_dirs,
        'files': records,
        'error': dept_error or ('' if matched else MISSING_TEXT),
      })
      if progress:
        out = progress(done, total_steps, f'{dept_label} · {item.name}')
        if inspect.isawaitable(out):
          await out

    out_depts.append(entry)

  elapsed = round(time.monotonic() - started, 2)
  logger.info('[DocUpdate] 部门快照完成 depts=%d files=%d elapsed=%ss',
              len(out_depts),
              sum(len(i['files']) for d in out_depts for i in d['items']), elapsed)
  return {
    'taken_at': taken_at,
    'period_key': taken_at.astimezone(_tz()).strftime('%Y-%m'),
    'period_label': taken_at.astimezone(_tz()).strftime('%Y年%m月'),
    'scope': 'dept',
    'depts': out_depts,
    'items': [],
    'file_count': sum(len(i['files']) for d in out_depts for i in d['items']),
    'elapsed': elapsed,
  }


def _dept_key(dept: dict) -> tuple[str, str]:
  """部门键：公司 + dept_id（dept_id 空时退回名称，防跨公司串项）。"""
  return (str(dept.get('company_id') or ''),
          str(dept.get('dept_id') or dept.get('dept_name') or ''))


def _merge_dept_snapshot(base: dict, patch: dict) -> dict:
  """按 `(company_id, dept_id)` + `seq` 把单项刷新结果并回整期快照。"""
  by_key = {_dept_key(d): d for d in base.get('depts') or []}
  merged: list[dict] = []
  for dept in patch.get('depts') or []:
    old = by_key.get(_dept_key(dept))
    if not old:
      merged.append(dept)
      continue
    repl = {i.get('seq'): i for i in dept.get('items') or []}
    items = [repl.pop(i.get('seq'), i) for i in old.get('items') or []]
    items.extend(repl.values())  # 本期新增项兜底，不漏项
    entry = dict(old)
    entry.update(dept)
    entry['items'] = items
    entry['error'] = dept.get('error') or old.get('error') or ''
    merged.append(entry)
  seen = {_dept_key(d) for d in merged}
  merged.extend(d for d in base.get('depts') or [] if _dept_key(d) not in seen)

  out = {k: v for k, v in base.items() if k != 'depts'}
  out.update({k: v for k, v in patch.items() if k != 'depts'})
  out['depts'] = merged
  out['file_count'] = sum(len(i.get('files') or [])
                          for d in merged for i in d.get('items') or [])
  return out


def merge_snapshot(base: dict | None, patch: dict) -> dict:
  """把单项刷新的结果并回整期快照：按 `seq` 替换命中项，其余项原样保留。

  `base` 为空时直接返回 patch（临时比对用：拼到上期快照上算 verdict，不落库）。
  部门口径（patch 带 `depts`）按 `((company_id, dept_id), seq)` 合并。
  """
  if not base:
    return patch
  if 'depts' in patch:
    return _merge_dept_snapshot(base, patch)
  replacements = {i.get('seq'): i for i in patch.get('items') or []}
  merged = [replacements.pop(i.get('seq'), i) for i in base.get('items') or []]
  merged.extend(replacements.values())  # 本期新增项兜底，不漏项
  out = dict(base)
  out.update({
    'items': merged,
    'file_count': sum(len(i.get('files') or []) for i in merged),
    'taken_at': patch.get('taken_at') or base.get('taken_at'),
    'elapsed': patch.get('elapsed', 0),
  })
  return out


def _tz():
  from zoneinfo import ZoneInfo

  return ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE or 'Asia/Shanghai')

"""共享文档目录遍历（部门 rootFileId 之下）。

接口：`GET /file/share/v2/{parentId}/2/{page}/{pageSize}` → `data.shareFileVOList[]`，
字段 `{fid, name, type, parentId, createTime, size}`。

**两条实测硬约束**：
- `type == '1'` 是文件夹，**其余一律当文件** —— 共享文件节点的 `type` 常常是空串 `''`，
  不是普通整树接口里的 `'0'`；
- `shareFileVOList` 可能为 `null` → 当空目录。

编号剪枝（fail-open）：文件夹名前导数字串与**全部目标编号**都明显不兼容时才跳过
（如目标是 `1.4.5`、目录是 `2.x`）；`1.4.5` 与 `1.4.5.1` 视为兼容，无数字名字一律下钻。
宁可多走几页，不可漏匹配。
"""

import asyncio
import logging
import re

import httpx

from config.settings import settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
_MAX_PAGES = 50        # 单目录页数安全阀（5000 条）
_MAX_DIRS = 2000       # 单部门访问目录数安全阀
_MAX_DEPTH = 10

_LEADING_NUMBER = re.compile(r'^(\d+(?:\.\d+)*)')


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _compatible(folder_number: str, numbers: tuple[str, ...]) -> bool:
  """目录编号是否可能与任一目标编号相关（前缀关系任一方向）。"""
  for n in numbers:
    if folder_number == n:
      return True
    if n.startswith(f'{folder_number}.'):
      return True
    if folder_number.startswith(f'{n}.'):
      return True
  return False


async def _list_page(parent_id: str, page: int):
  """取一页目录；传输层超时/断连重试一次（大树单页 > MGMT_TIMEOUT=30s 时实测会超时）。"""
  from service.query.mgmt import http

  path = f'/file/share/v2/{parent_id}/2/{page}/{PAGE_SIZE}'
  for attempt in range(2):
    try:
      return await http.request('GET', path)
    except httpx.TransportError:
      if attempt:
        raise
      await asyncio.sleep(1)


async def list_dir(parent_id: str) -> list[dict]:
  """列一个目录（自动翻页）。返回 `[{fid, name, is_dir, create_time, size}]`。"""
  out: list[dict] = []
  page = 1
  while page <= _MAX_PAGES:
    data = await _list_page(parent_id, page)
    rows = (data or {}).get('shareFileVOList') if isinstance(data, dict) else None
    rows = rows or []
    for row in rows:
      fid = _norm(row.get('fid'))
      if not fid:
        continue
      out.append({
        'fid': fid,
        'name': _norm(row.get('name')),
        'is_dir': str(row.get('type')) == '1',
        'create_time': row.get('createTime'),
        'size': row.get('size'),
      })
    total = (data or {}).get('size') if isinstance(data, dict) else None
    if len(rows) < PAGE_SIZE:
      break
    if isinstance(total, int) and page * PAGE_SIZE >= total:
      break
    page += 1
  return out


async def walk(root_id: str, numbers: tuple[str, ...]) -> list[dict]:
  """递归收集 root 下所有文件。

  返回 `[{fid, topic, parent_id, path, segments, create_time, size}]`；
  `segments` 是从 root 往下的目录名列表（期号解析用），`path` 是展示用的 `' / '` 串。
  """
  out: list[dict] = []
  visited = 0

  async def walk_dir(dir_id: str, segments: list[str], depth: int) -> None:
    nonlocal visited
    if depth > _MAX_DEPTH or visited >= _MAX_DIRS:
      return
    visited += 1
    try:
      rows = await list_dir(dir_id)
    except Exception as exc:  # noqa: BLE001  单个目录失败不拖垮整部门
      logger.warning('[DocUpdate] 共享目录列举失败 parent=%s: %s', dir_id, exc)
      return
    for row in rows:
      if row['is_dir']:
        m = _LEADING_NUMBER.match(row['name'])
        if m and numbers and not _compatible(m.group(1), numbers):
          continue
        await walk_dir(row['fid'], segments + [row['name']], depth + 1)
        continue
      out.append({
        'fid': row['fid'],
        'topic': row['name'],
        'parent_id': dir_id,
        'path': ' / '.join(segments),
        'segments': list(segments),
        'create_time': row['create_time'],
        'size': row['size'],
      })

  await walk_dir(root_id, [], 1)
  return out


async def download_sha256(fid: str) -> dict:
  """共享文件的下载地址 + 内容哈希（不落盘）。失败返回 `{sha256:'', error:...}`。"""
  from service.doc_update.fetch import extract_url, stream_sha256
  from service.query.mgmt import http

  out = {'sha256': '', 'download_size': 0, 'url': '', 'error': ''}
  try:
    data = await http.request('GET', f'/file/share/v1/minio/url/{fid}')
    url = extract_url(data)
    if not url:
      detail = str(data)[:160]
      hint = ''
      if '403' in detail or 'forbidden' in detail.lower() or '权限' in detail:
        hint = '（服务账号无共享文件下载权限，需管理员开通）'
      out['error'] = f'共享文件下载地址为空{hint}：{detail}'
      return out
    sha, size, _head = await stream_sha256(url)
    out.update(sha256=sha, download_size=size, url=url)
  except Exception as exc:  # noqa: BLE001
    text = f'{type(exc).__name__}: {str(exc)[:200]}'
    if '403' in text or 'Forbidden' in text:
      text += '（服务账号无共享文件下载权限，需管理员开通）'
    out['error'] = text
  return out


def known_numbers() -> tuple[str, ...]:
  """剪枝用的全部目标编号：清单编号 ∪ 实际目录名里的前导编号。

  实际名称登记（`DocItem.folders`）指向的目录可能挂在别的编号下
  （如同一份月报也在 `1.2.3` / `8.1.2.3`），只收清单编号会被剪枝挡在门外。
  """
  from service.doc_update.catalog import MONTHLY_ITEMS

  out: list[str] = []
  for item in MONTHLY_ITEMS:
    out.extend(item.numbers)
    for folder in item.folders:
      m = _LEADING_NUMBER.match(_norm(folder))
      if m:
        out.append(m.group(1))
  return tuple(dict.fromkeys(out))


def timeout() -> int:
  return settings.DOC_UPDATE_TIMEOUT

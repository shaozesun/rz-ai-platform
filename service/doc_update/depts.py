"""部门清单与注册表：各公司 A1/A2/… 部门、每个部门的共享文档根。

接口（综合管理平台，复用 `service/query/mgmt/http.request`，自带 401 重登）：
- `GET /dept/dept/list?companyId=…` → 部门清单
- `GET /dept/dept/details?deptId=…` → `userList[0].rootFileId`（该部门共享文件夹的根）

**列清单固定**：页面/检查范围取 `DOC_UPDATE_DEPTS`（`标签:companyId:部门,…;…`，
旧的不带 `:` 的值按单公司套 `DOC_UPDATE_COMPANY_ID`），不再每次按接口全量动态扩列。
接口只用来解析 `dept_id` / `root_fid`，结果落 Mongo `doc_update_depts` 缓存；
缓存齐了就不再联网，页面也不再依赖「有没有跑过快照」。
单个部门取根失败只记 `error`，不中断其它部门，也不删列。
"""

import logging
import re
from datetime import datetime, timezone

from config.mongodb_conn import mongodb_manager
from config.settings import settings

logger = logging.getLogger(__name__)

COLLECTION = 'doc_update_depts'
REGISTRY_ID = 'registry'


def _coll():
  return mongodb_manager.db[COLLECTION]


def _norm(text) -> str:
  return str(text or '').replace('\u3000', ' ').strip()


def _rows(data) -> list[dict]:
  """把接口返回的 `data` 归一成行列表（兼容 裸数组 / value / records / list）。"""
  if isinstance(data, list):
    return [r for r in data if isinstance(r, dict)]
  if isinstance(data, dict):
    for key in ('value', 'records', 'list', 'data', 'rows'):
      value = data.get(key)
      if isinstance(value, list):
        return [r for r in value if isinstance(r, dict)]
  return []


def _name_of(row: dict) -> str:
  """部门名取短名优先（A1 这类），没有再用全名。"""
  return _norm(row.get('deptShortName') or row.get('shortName')
               or row.get('deptName') or row.get('name'))


def _id_of(row: dict) -> str:
  return _norm(row.get('deptId') or row.get('id'))


def _sort_no(name: str) -> int:
  m = re.search(r'\d+', name or '')
  return int(m.group()) if m else 10**6


def parse_names(raw: str) -> list[str]:
  """把逗号清单解析成部门名：去空、去重、按编号排序（纯函数）。"""
  text = (raw or '').replace('，', ',')
  names = [n.strip() for n in text.split(',') if n.strip()]
  uniq = list(dict.fromkeys(names))
  return sorted(uniq, key=lambda n: (_sort_no(n), n))


def parse_spec(raw: str, default_company_id: str = '') -> list[dict]:
  """把配置解析成 `[{company_id, company_name, names}]`（纯函数）。

  - 分组格式：`标签:companyId:部门,部门;…`；两段 `companyId:部门` 时标签用 id。
  - **不含 `:` 的旧格式**＝单公司，套 `default_company_id`、标签留空。
  """
  text = (raw or '').replace('，', ',').replace('；', ';').strip()
  if not text:
    return []
  if ':' not in text and '：' not in text:
    names = parse_names(text)
    if not names:
      return []
    return [{'company_id': default_company_id, 'company_name': '', 'names': names}]

  text = text.replace('：', ':')
  groups: list[dict] = []
  for part in text.split(';'):
    bits = [b.strip() for b in part.split(':')]
    if len(bits) >= 3:
      label, company_id, dept_raw = bits[0], bits[1], ':'.join(bits[2:])
    elif len(bits) == 2:
      label, company_id, dept_raw = '', bits[0], bits[1]
    else:
      continue
    names = parse_names(dept_raw)
    if not company_id or not names:
      continue
    groups.append({
      'company_id': company_id,
      'company_name': label or company_id,
      'names': names,
    })
  return groups


def configured_groups() -> list[dict]:
  """固定「公司 × 部门」清单（顺序与配置一致）。"""
  return parse_spec(settings.DOC_UPDATE_DEPTS or '',
                    settings.DOC_UPDATE_COMPANY_ID or '')


def configured_names() -> list[str]:
  """全部部门名（跨公司去重，按编号排序）。"""
  names: list[str] = []
  for group in configured_groups():
    names.extend(group['names'])
  uniq = list(dict.fromkeys(names))
  return sorted(uniq, key=lambda n: (_sort_no(n), n))


def merge_columns(groups, cached) -> list[dict]:
  """配置清单 × 缓存 → 页面列（纯函数）。

  `groups` 为 `parse_spec` 结果（也兼容旧的部门名列表）。配置里有、缓存里没有的
  部门**照样出列**（`dept_id` 为空、格子显示 `—`）；缓存多出来的不出列（配置是唯一来源）。
  缓存行缺 `company_id` 视为旧单公司数据（`DOC_UPDATE_COMPANY_ID`）。
  """
  if not groups:
    return []
  if isinstance(groups, (list, tuple)) and groups and isinstance(groups[0], str):
    groups = parse_spec(','.join(groups), settings.DOC_UPDATE_COMPANY_ID or '')
  if isinstance(cached, dict):
    rows = list(cached.values())
  else:
    rows = list(cached or [])

  default_cid = settings.DOC_UPDATE_COMPANY_ID or ''
  by_key: dict[tuple[str, str], dict] = {}
  for row in rows:
    name = str(row.get('dept_name') or '')
    if not name:
      continue
    cid = str(row.get('company_id') or default_cid)
    by_key[(cid, name)] = row

  out: list[dict] = []
  for group in groups:
    cid = str(group.get('company_id') or '')
    label = str(group.get('company_name') or '')
    for name in group.get('names') or []:
      row = by_key.get((cid, name)) or {}
      out.append({
        'company_id': cid,
        'company_name': label,
        'dept_id': str(row.get('dept_id') or ''),
        'dept_name': name,
        'error': str(row.get('error') or ''),
      })
  return out


def _row_key(row: dict) -> tuple[str, str]:
  cid = str(row.get('company_id') or settings.DOC_UPDATE_COMPANY_ID or '')
  return (cid, str(row.get('dept_name') or ''))


async def _load_registry() -> dict[tuple[str, str], dict]:
  doc = await _coll().find_one({'_id': REGISTRY_ID})
  rows = (doc or {}).get('depts') or []
  out: dict[tuple[str, str], dict] = {}
  for row in rows:
    if not row.get('dept_name'):
      continue
    key = _row_key(row)
    out[key] = {**dict(row), 'company_id': key[0]}
  return out


async def _save_registry(rows: list[dict]) -> None:
  now = datetime.now(timezone.utc)
  await _coll().update_one(
    {'_id': REGISTRY_ID},
    {'$set': {'depts': rows, 'updated_at': now}},
    upsert=True,
  )


async def list_departments(company_id: str = '') -> list[dict]:
  """该公司的接口部门清单 `[{dept_id, dept_name}]`，按编号数值排序。"""
  from service.query.mgmt import http

  pattern = re.compile(settings.DOC_UPDATE_DEPT_PATTERN or r'^A\d+$')
  data = await http.request(
    'GET', '/dept/dept/list',
    params={'companyId': company_id or settings.DOC_UPDATE_COMPANY_ID},
  )
  out: dict[str, dict] = {}
  for row in _rows(data):
    dept_id = _id_of(row)
    name = _name_of(row)
    if not dept_id or not pattern.match(name):
      continue
    out[dept_id] = {'dept_id': dept_id, 'dept_name': name}
  return sorted(out.values(), key=lambda d: (_sort_no(d['dept_name']), d['dept_id']))


async def root_file_id(dept_id: str) -> str:
  """该部门共享文档的根文件夹 id；取不到返回空串。"""
  from service.query.mgmt import http

  data = await http.request('GET', '/dept/dept/details', params={'deptId': dept_id})
  detail = data
  if isinstance(detail, list):
    detail = detail[0] if detail else {}
  users = (detail or {}).get('userList') if isinstance(detail, dict) else None
  for user in users or []:
    fid = _norm((user or {}).get('rootFileId'))
    if fid:
      return fid
  return ''


async def ensure_registry(force: bool = False) -> list[dict]:
  """保证配置里的「公司 × 部门」都有 `dept_id` / `root_fid`，结果落 Mongo 缓存。

  已有完整缓存就不再调用接口（`force=True` 才全量重取，部门增删后用）。
  每个公司只调一次部门清单；接口失败只记日志、保留旧缓存，仍按配置输出全部列。
  """
  groups = configured_groups()
  cached = await _load_registry()

  need_by_company: dict[str, list[str]] = {}
  for group in groups:
    cid = str(group.get('company_id') or '')
    for name in group['names']:
      row = cached.get((cid, name)) or {}
      if force or not row.get('dept_id') or not row.get('root_fid'):
        need_by_company.setdefault(cid, []).append(name)

  discovered: dict[str, dict[str, str]] = {}
  discovery_ok: dict[str, bool] = {}
  for cid in need_by_company:
    try:
      discovered[cid] = {d['dept_name']: d['dept_id']
                         for d in await list_departments(cid)}
      discovery_ok[cid] = True
    except Exception as exc:  # noqa: BLE001  接口失败不删列，保留缓存
      discovery_ok[cid] = False
      logger.warning('部门清单获取失败（沿用缓存） company=%s: %s: %s',
                     cid, type(exc).__name__, exc)

  out: list[dict] = []
  changed = bool(need_by_company)
  for group in groups:
    cid = str(group.get('company_id') or '')
    label = str(group.get('company_name') or '')
    for name in group['names']:
      row = dict(cached.get((cid, name)) or {})
      row.update({'company_id': cid, 'company_name': label, 'dept_name': name})
      old_id = str(row.get('dept_id') or '')
      dept_id = (discovered.get(cid) or {}).get(name) or old_id
      if dept_id and (force or not row.get('root_fid') or dept_id != old_id):
        try:
          root = await root_file_id(dept_id)
          if root:
            row['root_fid'] = root
          row.pop('error', None)
        except Exception as exc:  # noqa: BLE001  单个部门失败不中断
          row['error'] = f'部门详情获取失败：{type(exc).__name__}: {str(exc)[:200]}'
          changed = True
      if dept_id:
        row['dept_id'] = dept_id
      elif discovery_ok.get(cid) and cid in need_by_company:
        row.setdefault('error', '未在部门清单中找到该部门')
      out.append(row)

  if changed:
    await _save_registry(out)
  return out


async def dept_columns() -> list[dict]:
  """页面列：**不联网、不看快照**，只读「配置 × 缓存」。"""
  return merge_columns(configured_groups(), (await _load_registry()).values())


def _in_companies(row: dict, companies: set[str]) -> bool:
  if not companies:
    return True
  cid = str(row.get('company_id') or '').strip().lower()
  cname = str(row.get('company_name') or '').strip().lower()
  return cid in companies or cname in companies


async def resolve_departments(names: list[str] | None = None,
                              companies: list[str] | None = None) -> list[dict]:
  """取数用的部门清单（含公司字段与 `root_fid`）。

  `names` 限定部门名，`companies` 限定公司（id 或配置里的标签）；都不传＝全部。
  """
  registry = await ensure_registry()
  wanted = {str(n).upper() for n in (names or [])}
  wanted_c = {str(c).strip().lower() for c in (companies or []) if str(c).strip()}
  out = []
  for row in registry:
    if wanted and str(row.get('dept_name') or '').upper() not in wanted:
      continue
    if not _in_companies(row, wanted_c):
      continue
    out.append({
      'company_id': str(row.get('company_id') or ''),
      'company_name': str(row.get('company_name') or ''),
      'dept_id': str(row.get('dept_id') or ''),
      'dept_name': str(row.get('dept_name') or ''),
      'root_fid': str(row.get('root_fid') or ''),
      'error': str(row.get('error') or ''),
    })
  return out

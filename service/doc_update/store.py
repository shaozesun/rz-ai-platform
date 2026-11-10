"""Mongo `doc_update_snapshots`：一期快照一条文档（同 `period_key` 覆盖，不重复堆叠）。

一期约 25 项 × 若干文件 ≈ 6KB，不做淘汰 —— 历史期按 `period_key` 直接读。
**参照只有 `previous(period_key)`（严格早于该期的最近一期）**：每月 10 日跑一次，
所以同月内的多次检查都能看到上月那份；同月重跑只覆盖**本期**这份，上月那份永不被写，
「已更新/未更新」整月不会翻。没有上期就是基线期，不拿本月早先的检查结果当参照。
"""

import uuid
from datetime import datetime, timezone

from config.mongodb_conn import mongodb_manager
from config.settings import settings

COLLECTION = 'doc_update_snapshots'


def _coll():
  return mongodb_manager.db[COLLECTION]


def _aware(value):
  """Mongo 读回来是 naive UTC：补上时区。

  不补的话，接口序列化成不带偏移的 `2026-10-30T09:20:10`，前端 `new Date()` 会按本地
  时区解析 —— 页面上「快照 / 参照」时间比北京时间早 8 小时。
  """
  if isinstance(value, datetime):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
  if isinstance(value, dict):
    return {k: _aware(v) for k, v in value.items()}
  if isinstance(value, list):
    return [_aware(v) for v in value]
  return value


def _claim_legacy_companies(doc: dict) -> dict:
  """旧版单公司快照的部门条目没有 `company_id` → 认领给 `DOC_UPDATE_COMPANY_ID`。

  这些快照只跑过默认公司（润泽/廊坊）。不认领的话，新增的多公司列与旧条目键对不上，
  页面筛不出旧快照、单项刷新 merge 时同一部门会被当成两个。
  """
  depts = doc.get('depts') or []
  default_id = settings.DOC_UPDATE_COMPANY_ID or ''
  if not depts or not default_id:
    return doc
  label = ''
  for dept in depts:
    if dept.get('company_id'):
      continue
    if not label:
      from service.doc_update.depts import configured_groups
      label = next((g['company_name'] for g in configured_groups()
                    if g['company_id'] == default_id), '') or ''
    dept['company_id'] = default_id
    dept['company_name'] = dept.get('company_name') or label
  return doc


def _clean(doc: dict | None) -> dict | None:
  if not doc:
    return None
  out = _aware(dict(doc))
  out.pop('_id', None)
  return _claim_legacy_companies(out)


async def latest() -> dict | None:
  docs = await _coll().find({}).sort('taken_at', -1).limit(1).to_list(1)
  return _clean(docs[0]) if docs else None


async def same_period(period_key: str) -> dict | None:
  doc = await _coll().find_one({'period_key': period_key})
  return _clean(doc)


async def previous(period_key: str) -> dict | None:
  """**严格早于**给定期的最近一条（即上一期）。

  按 `period_key` 而不是 `taken_at` 排序：补跑旧期时 `taken_at` 会被刷新成最新，
  拿它当参照会把后写入的旧期当成上期。`YYYY-MM` 字符串比较与时间顺序一致。
  """
  docs = (await _coll().find({'period_key': {'$lt': period_key}})
          .sort('period_key', -1).limit(1).to_list(1))
  return _clean(docs[0]) if docs else None


async def list_periods() -> list[dict]:
  """所有期号（新到旧），页面「历史期」下拉用。

  一月一条、每条几 KB，不做条数上限。`_aware` 补时区，否则前端显示早 8 小时。
  """
  pipeline = [
    {'$project': {
      '_id': 0,
      'period_key': 1,
      'period_label': 1,
      'taken_at': 1,
      'scope': {'$ifNull': ['$scope', 'tree']},
      'dept_count': {'$size': {'$ifNull': ['$depts', []]}},
    }},
    {'$sort': {'period_key': -1}},
  ]
  docs = await _coll().aggregate(pipeline).to_list(None)
  return [_aware(doc) for doc in docs]


async def save(snapshot: dict, created_by: str = '') -> str:
  """落一期快照；同 `period_key` 已存在则整条覆盖（保留原 snapshot_id）。

  覆盖只动**本期**这份：上月（不同 `period_key`）那份永不被写，所以同月重跑的结论不会翻。
  """
  now = datetime.now(timezone.utc)
  doc = dict(snapshot)
  doc.pop('snapshot_id', None)
  doc['created_by'] = created_by
  existing = await _coll().find_one({'period_key': doc.get('period_key')})
  if existing:
    doc['snapshot_id'] = existing.get('snapshot_id') or uuid.uuid4().hex
    doc['updated_at'] = now
    await _coll().update_one({'_id': existing['_id']}, {'$set': doc})
  else:
    doc['snapshot_id'] = uuid.uuid4().hex
    doc['created_at'] = now
    await _coll().insert_one(doc)
  return doc['snapshot_id']

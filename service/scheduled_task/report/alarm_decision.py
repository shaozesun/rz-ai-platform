"""事件单 ↔ DCIM 告警「判定表」（Mongo `event_alarm_link_decisions`）。

一行 = **一次发生**（不是类目）：键 = `(工单号, 发生时间, 口径)`，口径是园区 slug
（`廊坊`/`平湖`/…）或 `nationwide`。加口径维度是必须的 —— 旧缓存
（`event_alarm_links`）按「代表单 + 类目名」缓存，W45 全国报告判出的「非廊坊」
会被廊坊园区报告原样命中，导致一条告警都关联不上。

存的是**判定结论**（状态 + 告警身份 `(cluster, event_log_id)`），展示用的告警值/阈值
仍实时从 PG 取；只有告警行被软删时才回退 `alarm_snapshot`。

状态与复用规则：

| status | 含义 | 复用 |
|--------|------|------|
| `matched` | 已确定告警身份 | 永久 |
| `not_covered` | 明确不在覆盖范围（非廊坊 / 早于 2026-10-29） | 永久（指纹变了才重判） |
| `no_alarm` | ±120s 内无候选 | 到 `recheck_after` 前**每次重查**（等数据落库），之后永久 |
| `ambiguous` | 多候选且模型判不出 | 永久（显式 refresh 才重判） |

`unknown`（档案不可用 / 仲裁调用异常）与 `no_time`（缺发生时间）**不落表**，下次重试。
读取时校验 `rule_version` 与 `coverage_fingerprint`，不一致视为未命中。

读写失败一律 fail-open（按未命中 / 忽略写入），只记 warning，绝不让报告或对话失败。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from pymongo import UpdateOne

from config.mongodb_conn import mongodb_manager

logger = logging.getLogger(__name__)

COLLECTION = 'event_alarm_link_decisions'
# 匹配规则变更（时间窗、候选判定、仲裁口径）时 +1，旧判定自动失效。
RULE_VERSION = 1
# 覆盖范围指纹：园区代号集合 + 数据起始日。范围变了旧判定（尤其是 not_covered）失效。
COVERAGE_FINGERPRINT = '廊坊A|廊坊B1M1|廊坊B1M2@2026-10-29'

# 只有这四种状态可落表、可复用。
STATUSES = ('matched', 'not_covered', 'no_alarm', 'ambiguous')
# no_alarm 的重查窗口：至少等 30 分钟，且不早于事件发生 +24 小时（等 DCIM 数据落库）。
NO_ALARM_RECHECK_SECONDS = 24 * 3600
NO_ALARM_MIN_RECHECK_SECONDS = 30 * 60


def normalize_scope(scope) -> str:
  """园区口径归一：`廊坊园区`/`廊坊` → `廊坊`；空或「全国」→ `nationwide`。"""
  from service.scheduled_task import report_templates

  raw = str(scope or '').strip()
  if not raw or raw in ('nationwide', '全国'):
    return 'nationwide'
  for park in report_templates.PARKS:
    if raw in (park['slug'], park['name']):
      return park['slug']
  return raw


def decision_id(ticket_no, occurrence_ts, scope) -> str | None:
  """`(工单号, 发生时间, 口径)` → 稳定键；缺工单号或时间则返回 None（不落表）。"""
  ticket = str(ticket_no or '').strip()
  try:
    ts = int(occurrence_ts)
  except (TypeError, ValueError):
    return None
  if not ticket or ts <= 0:
    return None
  key = f'{ticket}\x1f{ts}\x1f{normalize_scope(scope)}'
  return hashlib.sha1(key.encode('utf-8')).hexdigest()


def _as_utc(value) -> datetime | None:
  if not isinstance(value, datetime):
    return None
  if value.tzinfo is None:
    return value.replace(tzinfo=timezone.utc)
  return value


def is_reusable(doc: dict | None, now: datetime | None = None) -> bool:
  """判定是否还能直接用：版本/指纹一致，且 no_alarm 已过重查时刻。"""
  if not doc:
    return False
  try:
    version = int(doc.get('rule_version') or 0)
  except (TypeError, ValueError):
    version = 0
  if version != RULE_VERSION:
    return False
  if doc.get('coverage_fingerprint') != COVERAGE_FINGERPRINT:
    return False
  status = doc.get('status')
  if status not in STATUSES:
    return False
  if status == 'no_alarm':
    recheck_after = _as_utc(doc.get('recheck_after'))
    if recheck_after is not None:
      now = _as_utc(now) or datetime.now(timezone.utc)
      if now < recheck_after:
        return False
  return True


def build_decision(
  *,
  ticket_no,
  occurrence_ts,
  scope,
  status,
  name='',
  dc='',
  match='',
  alarm=None,
  point_sig=None,
  alarm_ts=None,
  alarm_snapshot=None,
  reason='',
  source='report',
  now=None,
) -> dict | None:
  """组装一条判定文档；`status` 不在可落表集合时返回 None（调用方跳过写表）。"""
  _id = decision_id(ticket_no, occurrence_ts, scope)
  if not _id or status not in STATUSES:
    return None
  now = _as_utc(now) or datetime.now(timezone.utc)
  recheck_after = None
  if status == 'no_alarm':
    recheck_after = datetime.fromtimestamp(
      max(now.timestamp() + NO_ALARM_MIN_RECHECK_SECONDS,
          float(occurrence_ts) + NO_ALARM_RECHECK_SECONDS),
      tz=timezone.utc)
  return {
    '_id': _id,
    'ticket_no': str(ticket_no).strip(),
    'occurrence_ts': int(occurrence_ts),
    'scope': normalize_scope(scope),
    'name': str(name or ''),
    'dc': str(dc or ''),
    'rule_version': RULE_VERSION,
    'coverage_fingerprint': COVERAGE_FINGERPRINT,
    'status': status,
    'match': str(match or ''),
    'alarm': alarm or None,
    'point_sig': list(point_sig) if point_sig else None,
    'alarm_ts': int(alarm_ts) if alarm_ts else None,
    'alarm_snapshot': alarm_snapshot or None,
    'reason': str(reason or ''),
    'source': str(source or 'report'),
    'decided_at': now,
    'recheck_after': recheck_after,
  }


def decision_view(doc: dict) -> dict:
  """判定文档 → 调用方使用的精简结构（报告与对话共用）。"""
  return {
    'status': doc.get('status') or '',
    'name': doc.get('name') or '',
    'dc': doc.get('dc') or '',
    'match': doc.get('match') or '',
    'reason': doc.get('reason') or '',
    'alarm': doc.get('alarm') or None,
    'point_sig': tuple(doc['point_sig']) if doc.get('point_sig') else None,
    'alarm_ts': doc.get('alarm_ts'),
    'alarm_snapshot': doc.get('alarm_snapshot') or None,
    'decided_at': doc.get('decided_at'),
    'recheck_after': doc.get('recheck_after'),
  }


class AlarmDecisionStore:
  """Mongo 实现。测试可注入同接口（`load` / `query` / `save`）的内存桩。"""

  async def load(self, ids: list[str]) -> dict[str, dict]:
    """批量取判定：`{id: doc}`；失败返回空表（按未命中处理）。

    只读取版本/指纹一致的记录 —— 不一致的视为未命中，由调用方重判后覆盖写回。
    """
    keys = [k for k in ids if k]
    if not keys:
      return {}
    try:
      cursor = mongodb_manager.db[COLLECTION].find({
        '_id': {'$in': keys},
        'rule_version': RULE_VERSION,
        'coverage_fingerprint': COVERAGE_FINGERPRINT,
      })
      return {doc['_id']: doc async for doc in cursor}
    except Exception as e:                # noqa: BLE001 —— 判定表不可用只降级
      logger.warning('[AlarmDecision] 判定表读取失败：%s', str(e)[:200])
      return {}

  async def query(
    self,
    *,
    ticket_no='',
    start_ts=None,
    end_ts=None,
    scope=None,
    limit=None,
  ) -> list[dict]:
    """对话侧查询：按工单号或发生时间区间取判定（升序）。失败返回空列表。"""
    condition: dict = {
      'rule_version': RULE_VERSION,
      'coverage_fingerprint': COVERAGE_FINGERPRINT,
    }
    ticket = str(ticket_no or '').strip()
    if ticket:
      condition['ticket_no'] = ticket
    if scope:
      condition['scope'] = normalize_scope(scope)
    span: dict = {}
    if start_ts is not None:
      span['$gte'] = int(start_ts)
    if end_ts is not None:
      span['$lte'] = int(end_ts)
    if span:
      condition['occurrence_ts'] = span
    try:
      cursor = mongodb_manager.db[COLLECTION].find(condition).sort('occurrence_ts', 1)
      if limit:
        cursor = cursor.limit(int(limit))
      return [doc async for doc in cursor]
    except Exception as e:                # noqa: BLE001
      logger.warning('[AlarmDecision] 判定表查询失败：%s', str(e)[:200])
      return []

  async def save(self, items: list[dict]) -> None:
    """按 `_id` 覆盖写（同键重跑以最新结论为准）；失败只记日志。"""
    ops = []
    for item in items or []:
      if not item or not item.get('_id'):
        continue
      doc = dict(item)
      ops.append(UpdateOne({'_id': doc.pop('_id')}, {'$set': doc}, upsert=True))
    if not ops:
      return
    try:
      await mongodb_manager.db[COLLECTION].bulk_write(ops, ordered=False)
    except Exception as e:                # noqa: BLE001 —— 写失败不影响报告
      logger.warning('[AlarmDecision] 判定表写入失败：%s', str(e)[:200])

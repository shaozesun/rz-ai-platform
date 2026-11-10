"""重复类目 → DCIM 告警匹配结果的持久化缓存（Mongo `event_alarm_links`）。

为什么要缓存：同一张代表单会出现在多份报告里（全国口径是各公司的合并，
园区报告又与全国共享同一批工单），而匹配结论只取决于「代表单 + 类目名」——
重复查库、重复调模型仲裁没有意义。

- 键是 `(代表单号, 类目名)`，**不按类目名单独缓存**：全国与园区的同名类目可能
  对应不同代表单，合并会张冠李戴；
- 存的是**结论快照**（告警值与四档阈值按当时取到的值），不是重新取当前态；
- `CACHE_VERSION` 变更后旧记录自动失效（读取按版本过滤）；调用方传
  `force_refresh=True` 时绕过读取、重算后覆盖写回；
- 读写失败一律 fail-open（按未命中 / 忽略写入），只记 warning，绝不让报告失败。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

from pymongo import UpdateOne

from config.mongodb_conn import mongodb_manager

logger = logging.getLogger(__name__)

COLLECTION = 'event_alarm_links'
# v2（2026-11-02）：阈值查询键修正 + 逐次发生关联 + 不再截断。v1 快照是
# 「阈值全空 + 只有一条告警」，必须失效重算。
CACHE_VERSION = 2


def cache_key(ticket_no, name) -> str | None:
  """`(代表单号, 类目名)` → 稳定键；任一项为空则不缓存。"""
  ticket = str(ticket_no or '').strip()
  category = str(name or '').strip()
  if not ticket or not category:
    return None
  return hashlib.sha1(f'{ticket}\x1f{category}'.encode('utf-8')).hexdigest()


class EventAlarmCache:
  """Mongo 实现。测试可注入同接口（`load` / `save`）的内存桩。"""

  def __init__(self, version: int | None = None):
    self.version = int(version or CACHE_VERSION)

  async def load(self, keys: list[str]) -> dict[str, dict]:
    """批量取匹配结论：`{key: cluster}`；失败返回空表（按未命中处理）。"""
    keys = [k for k in keys if k]
    if not keys:
      return {}
    try:
      cursor = mongodb_manager.db[COLLECTION].find(
        {'_id': {'$in': keys}, 'version': self.version})
      docs = {doc['_id']: doc async for doc in cursor}
    except Exception as e:                # noqa: BLE001 —— 缓存不可用只降级
      logger.warning('[AlarmLink] 匹配缓存读取失败：%s', str(e)[:200])
      return {}
    return {k: d['cluster'] for k, d in docs.items()
            if isinstance(d.get('cluster'), dict)}

  async def save(self, items: list[dict]) -> None:
    """覆盖写回（同键重跑以最新结论为准）；失败只记日志。"""
    now = datetime.now(timezone.utc)
    ops = [UpdateOne(
      {'_id': it['key']},
      {'$set': {'version': self.version,
                'ticket_no': it.get('ticket_no') or '',
                'name': it.get('name') or '',
                'cluster': it.get('cluster') or {},
                'cached_at': now}},
      upsert=True,
    ) for it in items if it.get('key')]
    if not ops:
      return
    try:
      await mongodb_manager.db[COLLECTION].bulk_write(ops, ordered=False)
    except Exception as e:                # noqa: BLE001 —— 写失败不影响报告
      logger.warning('[AlarmLink] 匹配缓存写入失败：%s', str(e)[:200])

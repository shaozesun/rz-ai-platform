"""推理结果缓存（Mongo `infrared_quality_predictions`）。

`_id = '{task_id}:{device_id}'`，存 `{label, score, at}`。同一任务重检时默认复用
（图没变就没必要再算一遍），勾「重新推理」才跳过读取。读失败按未命中、写失败只
记日志——缓存只是省时，不能成为失败点。
"""

import logging
from datetime import datetime, timezone

from pymongo import UpdateOne

from config.mongodb_conn import mongodb_manager

logger = logging.getLogger(__name__)

COLLECTION = 'infrared_quality_predictions'
# 一次最多查多少个 _id（Mongo $in 不宜过大）
_CHUNK = 200


def _coll():
  return mongodb_manager.db[COLLECTION]


def _key(task_id: str, device_id: str) -> str:
  return f'{task_id}:{device_id}'


async def get_many(task_id: str, device_ids: list[str]) -> dict[str, dict]:
  """返回 `{device_id: {label, score, at}}`；取不到就是空 dict。"""
  ids = [_key(task_id, d) for d in device_ids]
  out: dict[str, dict] = {}
  for i in range(0, len(ids), _CHUNK):
    batch = ids[i:i + _CHUNK]
    try:
      cursor = _coll().find({'_id': {'$in': batch}})
      async for doc in cursor:
        device_id = str(doc.get('_id') or '').split(':', 1)[-1]
        out[device_id] = {
          'label': doc.get('label') or '',
          'score': doc.get('score'),
          'at': doc.get('at'),
        }
    except Exception as e:  # noqa: BLE001
      logger.warning('红外推理缓存读取失败: %s', e)
      return out
  return out


def _bulk_ops(task_id: str, rows: list[dict], now: datetime) -> list[UpdateOne]:
  """`bulk_write` 只认 `UpdateOne` 对象（传 mongosh 那种 dict 会报 not a valid request）。"""
  return [UpdateOne(
    {'_id': _key(task_id, str(r.get('device_id') or ''))},
    {'$set': {
      'label': r.get('label') or '',
      'score': r.get('score'),
      'at': now,
    }},
    upsert=True,
  ) for r in rows if r.get('device_id')]


async def put_many(task_id: str, rows: list[dict]) -> None:
  """批量写缓存：`rows` 每项 `{device_id, label, score}`；失败只记日志。"""
  if not rows:
    return
  ops = _bulk_ops(task_id, rows, datetime.now(timezone.utc))
  if not ops:
    return
  try:
    await _coll().bulk_write(ops, ordered=False)
  except Exception as e:  # noqa: BLE001
    logger.warning('红外推理缓存写入失败: %s', e)


async def drop_task(task_id: str) -> None:
  """重新推理时清掉该任务的旧缓存（避免个别图失败后永远读到旧值）。"""
  try:
    await _coll().delete_many({'_id': {'$regex': f'^{task_id}:'}})
  except Exception as e:  # noqa: BLE001
    logger.warning('红外推理缓存清理失败: %s', e)

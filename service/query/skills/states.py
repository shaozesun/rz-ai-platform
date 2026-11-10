"""用户级技能启用状态（Mongo `skill_states`）。

一行 = 一个用户对一个自建技能的开关：`{user_id, skill_name, enabled, updated_at}`。
官方技能不落表（页面上只读），因此这里只处理本人技能的启用/停用。

**fail-open**：开关是用户偏好，Mongo 抖动时按「启用」处理（记 warning），
不能因为读不到偏好就让技能整体不可用。
"""

import logging
from datetime import UTC, datetime

from config.mongodb_conn import mongodb_manager

logger = logging.getLogger(__name__)

COLLECTION = 'skill_states'


def _collection():
  return mongodb_manager.db[COLLECTION]


async def get_disabled_skill_names(user_id: str) -> set[str]:
  """当前用户已停用的技能名集合；读失败返回空集（fail-open）。"""
  if not user_id:
    return set()
  try:
    cursor = _collection().find(
      {'user_id': user_id, 'enabled': False}, {'skill_name': 1, '_id': 0},
    )
    return {doc['skill_name'] async for doc in cursor if doc.get('skill_name')}
  except Exception as e:
    logger.warning('读取技能开关失败（按全部启用处理） user=%s: %s', user_id, e)
    return set()


async def is_skill_enabled(user_id: str, name: str) -> bool:
  """当前用户是否启用了该技能；无记录默认启用，读失败 fail-open。"""
  if not user_id or not name:
    return True
  try:
    doc = await _collection().find_one(
      {'user_id': user_id, 'skill_name': name}, {'enabled': 1, '_id': 0},
    )
    return True if doc is None else bool(doc.get('enabled', True))
  except Exception as e:
    logger.warning('读取技能开关失败（按启用处理） user=%s skill=%s: %s', user_id, name, e)
    return True


async def set_skill_enabled(user_id: str, name: str, enabled: bool) -> None:
  """写入（upsert）当前用户对某技能的启用状态。"""
  await _collection().update_one(
    {'user_id': user_id, 'skill_name': name},
    {'$set': {
      'enabled': bool(enabled),
      'updated_at': datetime.now(UTC),
    }},
    upsert=True,
  )


async def remove_skill_state(user_id: str, name: str) -> None:
  """删除技能时清掉开关记录，避免同名重建继承旧状态（best-effort）。"""
  try:
    await _collection().delete_one({'user_id': user_id, 'skill_name': name})
  except Exception as e:
    logger.warning('清理技能开关失败 user=%s skill=%s: %s', user_id, name, e)

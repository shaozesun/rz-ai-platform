"""风险检测历史记录持久化"""
from __future__ import annotations

import base64
import io
import logging
import uuid
from datetime import datetime
from typing import Optional

from PIL import Image

from config.mongodb_conn import mongodb_manager
from models.risk.schemas import CheckResult, FireSafetyRequest, FireSafetyResult

logger = logging.getLogger(__name__)

COLLECTION = 'risk_checks'
FIRE_SAFETY_COLLECTION = 'fire_safety_history'
THUMB_MAX_WIDTH = 200
THUMB_QUALITY = 30


def _make_thumbnail(image_bytes: bytes) -> str:
  """生成缩略图 base64（宽 ≤200px，JPEG q30），失败返回空字符串"""
  try:
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    if w > THUMB_MAX_WIDTH:
      ratio = THUMB_MAX_WIDTH / w
      img = img.resize((THUMB_MAX_WIDTH, int(h * ratio)), Image.LANCZOS)
    img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, format='JPEG', quality=THUMB_QUALITY)
    return base64.b64encode(buf.getvalue()).decode('utf-8')
  except Exception:
    logger.warning('缩略图生成失败', exc_info=True)
    return ''


async def save_check_result(
  user_id: str,
  result: CheckResult,
  image_bytes: bytes,
) -> None:
  """保存检测结果到 MongoDB"""
  doc = {
    'check_id': result.check_id,
    'user_id': user_id,
    'image_name': result.image_name,
    'thumbnail': _make_thumbnail(image_bytes),
    'result': result.model_dump(),
    'checked_at': datetime.utcnow(),
  }
  try:
    await mongodb_manager.db[COLLECTION].insert_one(doc)
    logger.info('检测结果已保存: check_id=%s user_id=%s', result.check_id, user_id)
  except Exception:
    logger.error('保存检测结果失败: check_id=%s', result.check_id, exc_info=True)


async def get_user_history(
  user_id: str,
  limit: int = 10,
  offset: int = 0,
) -> list[dict]:
  """获取用户检测历史列表（仅含摘要，不含完整结果）"""
  cursor = (
    mongodb_manager.db[COLLECTION]
    .find({'user_id': user_id})
    .sort('checked_at', -1)
    .skip(offset)
    .limit(limit)
  )
  records = []
  async for doc in cursor:
    r = doc.get('result', {})
    records.append({
      'check_id': doc['check_id'],
      'image_name': doc['image_name'],
      'thumbnail': doc.get('thumbnail', ''),
      'summary': r.get('summary', ''),
      'description': r.get('description', ''),
      'hazard_count': len(r.get('hazards', [])),
      'has_risk': r.get('has_risk'),
      'risk_level': r.get('risk_level', 'unknown'),
      'checked_at': doc['checked_at'].isoformat() if doc.get('checked_at') else '',
    })
  return records


async def get_check_detail(user_id: str, check_id: str) -> Optional[dict]:
  """获取单条检测详情"""
  doc = await mongodb_manager.db[COLLECTION].find_one({
    'check_id': check_id,
    'user_id': user_id,
  })
  if not doc:
    return None
  return {
    'check_id': doc['check_id'],
    'image_name': doc['image_name'],
    'thumbnail': doc.get('thumbnail', ''),
    'checked_at': doc['checked_at'].isoformat() if doc.get('checked_at') else '',
    'result': doc.get('result', {}),
  }


async def delete_check_record(user_id: str, check_id: str) -> bool:
  """删除检测记录"""
  result = await mongodb_manager.db[COLLECTION].delete_one({
    'check_id': check_id,
    'user_id': user_id,
  })
  return result.deleted_count > 0


# ==================== 消防配置推荐历史 ====================

async def save_fire_safety_result(
  user_id: str,
  request: FireSafetyRequest,
  result: FireSafetyResult,
) -> str:
  """保存消防配置推荐结果到 MongoDB，返回 record_id"""
  record_id = str(uuid.uuid4())
  doc = {
    'record_id': record_id,
    'user_id': user_id,
    'building_type': result.building_type or request.building_type,
    'risk_level': result.risk_level,
    'summary': result.summary,
    'request': request.model_dump(),
    'result': result.model_dump(),
    'created_at': datetime.utcnow(),
  }
  try:
    await mongodb_manager.db[FIRE_SAFETY_COLLECTION].insert_one(doc)
    logger.info('消防推荐结果已保存: record_id=%s user_id=%s', record_id, user_id)
  except Exception:
    logger.error('保存消防推荐结果失败: record_id=%s', record_id, exc_info=True)
  return record_id


async def get_fire_safety_history(
  user_id: str,
  limit: int = 10,
  offset: int = 0,
) -> list[dict]:
  """获取用户消防推荐历史列表（仅含摘要）"""
  cursor = (
    mongodb_manager.db[FIRE_SAFETY_COLLECTION]
    .find({'user_id': user_id})
    .sort('created_at', -1)
    .skip(offset)
    .limit(limit)
  )
  records = []
  async for doc in cursor:
    req = doc.get('request', {})
    records.append({
      'record_id': doc['record_id'],
      'building_type': doc.get('building_type', ''),
      'risk_level': doc.get('risk_level', 'unknown'),
      'summary': doc.get('summary', ''),
      'building_height': req.get('building_height', 0),
      'building_area': req.get('building_area', 0),
      'created_at': doc['created_at'].isoformat() if doc.get('created_at') else '',
    })
  return records


async def get_fire_safety_detail(user_id: str, record_id: str) -> Optional[dict]:
  """获取单条消防推荐详情"""
  doc = await mongodb_manager.db[FIRE_SAFETY_COLLECTION].find_one({
    'record_id': record_id,
    'user_id': user_id,
  })
  if not doc:
    return None
  return {
    'record_id': doc['record_id'],
    'building_type': doc.get('building_type', ''),
    'risk_level': doc.get('risk_level', 'unknown'),
    'created_at': doc['created_at'].isoformat() if doc.get('created_at') else '',
    'request': doc.get('request', {}),
    'result': doc.get('result', {}),
  }


async def delete_fire_safety_record(user_id: str, record_id: str) -> bool:
  """删除消防推荐记录"""
  result = await mongodb_manager.db[FIRE_SAFETY_COLLECTION].delete_one({
    'record_id': record_id,
    'user_id': user_id,
  })
  return result.deleted_count > 0

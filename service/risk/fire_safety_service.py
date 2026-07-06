"""
消防配置推荐服务
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from core.model_gateway import model_gateway
from models.risk.schemas import (
  FireSafetyRequest,
  FireSafetyResult,
  FireFacilityItem,
  BuildingMaterialItem,
  SafetyPreparationItem,
  FireSafetyItem,
)
from prompts.risk.fire_safety import FIRE_SAFETY_SYSTEM_PROMPT, FIRE_SAFETY_USER_TEMPLATE

logger = logging.getLogger(__name__)

# 文本模型响应缓存
_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 3600
_CACHE_MAX_SIZE = 128



def _clean_cache() -> None:
  now = time.time()
  expired = [k for k, (_, ts) in _cache.items() if now - ts > _CACHE_TTL]
  for k in expired:
    del _cache[k]
  if len(_cache) > _CACHE_MAX_SIZE:
    sorted_keys = sorted(_cache.items(), key=lambda x: x[1][1])
    for k, _ in sorted_keys[:len(sorted_keys) // 2]:
      del _cache[k]


def _parse_fire_safety_json(raw: str) -> dict:
  raw = (raw or '').strip()
  # 清洗思考标签
  raw = re.sub(r'^.*? response', '', raw, flags=re.IGNORECASE | re.DOTALL)
  raw = re.sub(r'<think>[\s\S]*?</think>', '', raw, flags=re.IGNORECASE)
  raw = re.sub(r'<thinking>[\s\S]*?</thinking>', '', raw, flags=re.IGNORECASE)
  raw = raw.strip()
  match = re.search(r'\{[\s\S]*\}', raw)
  if not match:
    return {
      'fire_facilities': [], 'building_materials': [],
      'safety_preparations': [], 'applicable_standards': [],
      'summary': raw, 'risk_level': 'unknown',
    }
  try:
    return json.loads(match.group(0))
  except json.JSONDecodeError:
    return {
      'fire_facilities': [], 'building_materials': [],
      'safety_preparations': [], 'applicable_standards': [],
      'summary': raw, 'risk_level': 'unknown',
    }


def _is_simple_mode(request: FireSafetyRequest) -> bool:
  """判断是否使用简化模式（兼容旧接口）"""
  return bool(request.room_type) and not bool(request.building_type)


async def recommend(request: FireSafetyRequest) -> FireSafetyResult:
  """根据建筑参数推荐消防配置方案。

  支持两种模式：
  - 完整模式：使用 building_type 等全套建筑参数
  - 简化模式（兼容旧接口）：使用 room_type 等简单参数
  """
  # 简化模式兼容
  if _is_simple_mode(request):
    return await _recommend_simple(request)

  # 完整模式
  user_text = FIRE_SAFETY_USER_TEMPLATE.format(
    building_type=request.building_type,
    building_height=request.building_height,
    floor_count_above=request.floor_count_above,
    floor_count_below=request.floor_count_below,
    building_area=request.building_area,
    fire_resistance_rating=request.fire_resistance_rating,
    structural_form=request.structural_form or '未指定',
    fire_hazard_category=request.fire_hazard_category or '无',
    occupancy_count=request.occupancy_count if request.occupancy_count > 0 else '未知',
    construction_status=request.construction_status,
    has_sprinkler='已安装' if request.has_sprinkler_system else '未安装',
    has_alarm='已安装' if request.has_alarm_system else '未安装',
    has_hydrant='已安装' if request.has_hydrant_system else '未安装',
    additional_notes=f'\n用户补充说明：{request.additional_notes}' if request.additional_notes else '',
  )

  # 缓存检查
  cache_key = hashlib.sha256(
    (FIRE_SAFETY_SYSTEM_PROMPT + '\x00' + user_text).encode()
  ).hexdigest()[:32]
  now = time.time()
  if cache_key in _cache:
    cached_result, cached_ts = _cache[cache_key]
    if now - cached_ts < _CACHE_TTL:
      logger.info('消防推荐命中缓存 key=%s', cache_key[:8])
      result_dict = cached_result
      return _build_result(request, result_dict)

  try:
    raw = await model_gateway.chat(
      messages=[
        {'role': 'system', 'content': FIRE_SAFETY_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_text},
      ],
      temperature=0.1,
      max_tokens=4096,
      json_mode=True,
      timeout=120,
      caller='fire_safety',
    )
  except Exception as e:
    logger.error('[FireSafety] LLM 调用失败 (%s): %s', type(e).__name__, e)
    return FireSafetyResult(
      ok=False,
      building_type=request.building_type,
      building_height=request.building_height,
      building_area=request.building_area,
      error=f'推荐生成失败: {type(e).__name__}: {e}',
    )

  parsed = _parse_fire_safety_json(raw)

  # 缓存结果
  _cache[cache_key] = (parsed, now)
  if len(_cache) > _CACHE_MAX_SIZE * 1.5:
    _clean_cache()

  return _build_result(request, parsed)


def _build_result(request: FireSafetyRequest, parsed: dict) -> FireSafetyResult:
  """从解析结果构建 FireSafetyResult"""
  facilities = [FireFacilityItem(**f) for f in parsed.get('fire_facilities', [])]
  materials = [BuildingMaterialItem(**m) for m in parsed.get('building_materials', [])]
  preparations = [SafetyPreparationItem(**p) for p in parsed.get('safety_preparations', [])]

  logger.info(
    '[FireSafety] type=%s height=%.1fm area=%.0fm² level=%s facilities=%d materials=%d preparations=%d',
    request.building_type, request.building_height, request.building_area,
    parsed.get('risk_level', 'unknown'), len(facilities), len(materials), len(preparations),
  )
  return FireSafetyResult(
    building_type=request.building_type,
    building_height=request.building_height,
    building_area=request.building_area,
    structural_form=request.structural_form or '',
    risk_level=parsed.get('risk_level', 'unknown'),
    summary=parsed.get('summary', ''),
    fire_facilities=facilities,
    building_materials=materials,
    safety_preparations=preparations,
    applicable_standards=parsed.get('applicable_standards', []),
  )


async def _recommend_simple(request: FireSafetyRequest) -> FireSafetyResult:
  """简化模式（兼容旧接口）：仅用 room_type 做数据中心场景推荐"""
  user_text = f"""请为以下房间推荐消防配置方案：

- 房间类型：{request.room_type}
- 面积：{request.area} 平方米
- 层高：{request.height or '标准层高'} 米
- 主要设备：{request.equipment or '未指定'}
- 特殊要求：{request.special_requirements or '无'}"""

  try:
    raw = await model_gateway.chat(
      messages=[
        {'role': 'system', 'content': FIRE_SAFETY_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_text},
      ],
      temperature=0.1,
      max_tokens=2048,
      json_mode=True,
      timeout=120,
      caller='fire_safety',
    )
  except Exception as e:
    logger.error('[FireSafety] LLM 调用失败 (%s): %s', type(e).__name__, e)
    return FireSafetyResult(
      ok=False,
      room_type=request.room_type,
      notes=f'推荐生成失败: {type(e).__name__}: {e}',
    )

  parsed = _parse_fire_safety_json(raw)

  items = [
    FireSafetyItem(
      name=r.get('name', ''),
      quantity=r.get('quantity', ''),
      specification=r.get('specification', ''),
      location=r.get('location', ''),
      reason=r.get('reason', ''),
    )
    for r in parsed.get('recommendations', [])
  ]

  return FireSafetyResult(
    room_type=request.room_type,
    recommendations=items,
    notes=parsed.get('notes', ''),
    regulation_refs=parsed.get('regulation_refs', ''),
    fire_facilities=[
      FireFacilityItem(
        category='消防设施', name=r.get('name', ''),
        specification=r.get('specification', ''),
        quantity_or_coverage=r.get('quantity', ''),
        installation_location=r.get('location', ''),
        regulation_ref=r.get('reason', ''),
        priority='mandatory',
      )
      for r in parsed.get('recommendations', [])
    ],
  )

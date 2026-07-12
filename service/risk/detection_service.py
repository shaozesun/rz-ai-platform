"""
隐患检测服务：两阶段流水线
  Phase 1: VLM 识别机柜类型 / 通用安全检查
  Phase 2: CV 指示灯检测（优先），失败则降级到 VLM 视觉对比
"""
from __future__ import annotations

import base64
import io
import json
import logging
import re
import time
import asyncio
import uuid
from pathlib import Path
from typing import Optional

from PIL import Image

from core.model_gateway import model_gateway
from models.risk.schemas import CheckResult, ViolationItem, HazardItem, BatchCheckResult
from prompts.risk.detection import (
  DETECTION_SYSTEM_PROMPT,
  USER_PROMPT,
  CABINET_COMPARE_PROMPT,
)
from service.risk.cv_detection import cabinet_cv_detect, get_available_cabinet_types

logger = logging.getLogger(__name__)

# 参考图目录 — Phase 2 降级时发送给 VLM 做视觉对比
_REF_IMAGES_DIR = Path(__file__).parent.parent.parent / 'images'
_CABINET_REF_IMAGES: dict[str, str] = {}

# 图片压缩阈值
MAX_IMAGE_SIZE = 300 * 1024  # 300KB


def _load_reference_images() -> None:
  """加载四种机柜正常状态参考图，用于 Phase 2 降级对比"""
  ref_files = {
    '喷淋稳压泵控制柜': '喷淋稳压泵控制柜.jpeg',
    '排烟风机控制柜': '排烟风机.jpeg',
    '正压送风风机控制柜': '正压送风风机.jpeg',
    '消火栓稳压泵控制柜': '消火栓稳压泵控制柜.jpeg',
  }
  for name, filename in ref_files.items():
    path = _REF_IMAGES_DIR / filename
    if path.exists():
      data = path.read_bytes()
      compressed = compress_image(data)
      _CABINET_REF_IMAGES[name] = base64.b64encode(compressed).decode('utf-8')
      logger.info('加载机柜参考图: %s (%.0fKB → %.0fKB)', name, len(data) / 1024, len(compressed) / 1024)
    else:
      logger.warning('机柜参考图缺失: %s', path)


def compress_image(data: bytes) -> bytes:
  """压缩图片：先缩尺寸（最大边 ≤ 2048），再逐步降 JPEG 质量直到 ≤ 300KB"""
  if len(data) <= MAX_IMAGE_SIZE:
    return data
  try:
    img = Image.open(io.BytesIO(data))
    max_side = 2048
    if max(img.size) > max_side:
      ratio = max_side / max(img.size)
      img = img.resize(
        (int(img.size[0] * ratio), int(img.size[1] * ratio)),
        Image.LANCZOS,
      )

    out = io.BytesIO()
    for quality in range(80, 9, -15):
      out.seek(0)
      out.truncate()
      img = img.convert('RGB')
      img.save(out, format='JPEG', quality=quality)
      if out.tell() <= MAX_IMAGE_SIZE or quality <= 20:
        break
    compressed = out.getvalue()
    if len(compressed) < len(data):
      return compressed
  except Exception:
    logger.warning('图片压缩失败，使用原图', exc_info=True)
  return data


def _parse_response(raw: str) -> dict:
  """从 LLM 输出中提取 JSON 结果，做多层兜底"""
  text = raw.strip()

  # 仅当文本不以 { 开头时，尝试清理前缀（避免误伤 JSON 内容）
  if text and text[0] != '{':
    text = re.sub(r'^.*? response', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<thinking>[\s\S]*?</thinking>', '', text, flags=re.IGNORECASE)
    text = re.sub(r'<reasoning>[\s\S]*?</reasoning>', '', text, flags=re.IGNORECASE)
    text = text.strip()

  # 优先从 ```json ... ``` 代码块提取
  json_block_m = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
  if json_block_m:
    try:
      result = json.loads(json_block_m.group(1).strip())
      if 'violations' not in result:
        result['violations'] = []
      return result
    except json.JSONDecodeError:
      pass

  try:
    result = json.loads(text)
    if 'violations' not in result:
      result['violations'] = []
    return result
  except json.JSONDecodeError:
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
      try:
        result = json.loads(m.group(0))
        if 'violations' not in result:
          result['violations'] = []
        return result
      except json.JSONDecodeError:
        logger.warning('模型输出解析失败 (regex_fallback) | raw_preview=%s', raw[:2000])
    else:
      logger.warning('模型输出解析失败 (no_json_found) | raw_preview=%s', raw[:2000])
    return {
      'cabinet_type': None,
      'has_risk': None,
      'risk_level': 'unknown',
      'risk_categories': [],
      'description': raw.strip(),
      'suggestion': '',
      'violations': [],
      'confidence': 0.0,
    }

SEVERITY_MAP = {'high': '高', 'medium': '中', 'low': '低', 'critical': '高', 'none': '低'}


def _build_result(
  check_id: str,
  image_name: str,
  parsed: dict,
  error: str = '',
) -> CheckResult:
  """从解析结果构建 CheckResult"""
  risk_level_raw = parsed.get('risk_level', 'unknown')
  severity = SEVERITY_MAP.get(risk_level_raw, '低')
  risk_level = SEVERITY_MAP.get(risk_level_raw, risk_level_raw)

  violations_raw = parsed.get('violations', [])
  if not isinstance(violations_raw, list):
    violations_raw = []

  violations = [
    ViolationItem(
      category=v.get('category', ''),
      description=v.get('description', ''),
      regulation=v.get('regulation', ''),
      suggestion=v.get('suggestion', ''),
    )
    for v in violations_raw
  ]

  # Map violations to frontend-compatible HazardItem list
  hazards = []
  for v in violations_raw:
    if not isinstance(v, dict):
      continue
    desc = v.get('description', '')
    # Attempt to extract location from description (typically before "存在" or first clause)
    loc = ''
    for sep in ['存在', '，', '、', '。']:
      idx = desc.find(sep)
      if idx > 0:
        loc = desc[:idx].strip()
        break
    if not loc:
      loc = desc[:20].strip()
    hazards.append(HazardItem(
      category=v.get('category', ''),
      severity=severity,
      location=loc,
      description=desc,
      recommendation=v.get('suggestion', ''),
      reference=v.get('regulation', ''),
    ))

  # Generate human-readable summary
  if error:
    summary = f'检测失败：{error}'
  elif not hazards:
    summary = '未发现安全隐患'
  else:
    high_count = sum(1 for h in hazards if h.severity == '高')
    mid_count = sum(1 for h in hazards if h.severity == '中')
    parts = [f'发现 {len(hazards)} 项隐患']
    if high_count > 0:
      parts.append(f'{high_count} 项高风险')
    if mid_count > 0:
      parts.append(f'{mid_count} 项中风险')
    summary = '，'.join(parts)

  return CheckResult(
    check_id=check_id,
    image_name=image_name,
    cabinet_type=parsed.get('cabinet_type'),
    has_risk=parsed.get('has_risk'),
    risk_level=risk_level,
    risk_categories=parsed.get('risk_categories', []),
    description=parsed.get('description', ''),
    suggestion=parsed.get('suggestion', ''),
    violations=violations,
    hazards=hazards,
    summary=summary,
    confidence=parsed.get('confidence', 0.0),
    error=error,
  )


async def check_image(
  image_bytes: bytes,
  image_name: str = '',
  media_type: str = 'image/jpeg',
  user_description: str = '',
) -> CheckResult:
  """两阶段图片安全检测。

  Phase 1: VLM 识别机柜类型或通用安全检查
  Phase 2: 若为机柜 → CV 指示灯检测（优先），失败降级到 VLM 视觉对比
  """
  check_id = str(uuid.uuid4())
  t0 = time.perf_counter()

  # 压缩 + base64
  compressed = compress_image(image_bytes)
  b64 = base64.b64encode(compressed).decode('utf-8')

  # 组装用户 prompt
  user_text = USER_PROMPT
  if user_description:
    user_text = f'{USER_PROMPT}\n\n用户特别要求检查：{user_description}'

  # ==================== Phase 1: VLM 识别 ====================
  logger.info('[Phase1] 开始调用 model image_size=%.0fKB', len(b64) * 3 / 4 / 1024)
  try:
    raw = await model_gateway.vision(
      system_prompt=DETECTION_SYSTEM_PROMPT,
      user_text=user_text,
      images=[b64],
      temperature=0.1,
      max_tokens=4096,
    )
  except Exception as e:
    logger.error('[Phase1] 模型调用失败: %s', e)
    return CheckResult(
      ok=False,
      check_id=check_id,
      image_name=image_name,
      summary=f'检测失败：{str(e)}',
      error=f'检测失败: {str(e)}',
    )

  phase1_elapsed = time.perf_counter() - t0
  parsed = _parse_response(raw)
  cabinet_type = parsed.get('cabinet_type')
  logger.info('[Phase1] 完成 elapsed=%.1fs cabinet_type=%s has_risk=%s',
              phase1_elapsed, cabinet_type, parsed.get('has_risk'))

  # 非机柜图片，直接返回 Phase 1 结果
  if not cabinet_type or cabinet_type not in _CABINET_REF_IMAGES:
    return _build_result(check_id, image_name, parsed)

  # ==================== Phase 2: 机柜专项检测 ====================
  # 优先走 CV
  available = get_available_cabinet_types()
  if cabinet_type in available:
    logger.info('[Phase2] 走 CV 检测 cabinet=%s', cabinet_type)
    cv_result = await asyncio.to_thread(cabinet_cv_detect, image_bytes, cabinet_type)
    if cv_result is not None:
      total_elapsed = time.perf_counter() - t0
      logger.info(
        '[Cabinet] 两阶段完成(CV) type=%s has_risk=%s phase1=%.1fs total=%.1fs',
        cabinet_type, cv_result.get('has_risk'), phase1_elapsed, total_elapsed,
      )
      cv_result['check_id'] = check_id
      cv_result['image_name'] = image_name
      return _build_result(check_id, image_name, cv_result)
    logger.warning('[Phase2] CV 检测失败，降级到视觉模型')

  # Phase 2 降级: VLM 视觉对比（用户图 + 参考图）
  ref_b64 = _CABINET_REF_IMAGES.get(cabinet_type)
  if not ref_b64:
    logger.warning('[Phase2] 无参考图，返回 Phase 1 结果')
    return _build_result(check_id, image_name, parsed)

  logger.info('[Phase2] 降级到视觉模型对比 cabinet=%s', cabinet_type)
  try:
    raw2 = await model_gateway.vision(
      system_prompt=CABINET_COMPARE_PROMPT,
      user_text=f'请对比以下两张图片。机柜类型：{cabinet_type}',
      images=[b64, ref_b64],
      temperature=0.1,
      max_tokens=4096,
    )
  except Exception as e:
    logger.error('[Phase2] 视觉对比失败: %s', e)
    return _build_result(check_id, image_name, parsed)

  parsed2 = _parse_response(raw2)
  parsed2['cabinet_type'] = cabinet_type

  total_elapsed = time.perf_counter() - t0
  phase2_elapsed = total_elapsed - phase1_elapsed
  logger.info(
    '[Cabinet] 两阶段完成(VLM降级) type=%s has_risk=%s phase1=%.1fs phase2=%.1fs total=%.1fs',
    cabinet_type, parsed2.get('has_risk'), phase1_elapsed, phase2_elapsed, total_elapsed,
  )
  return _build_result(check_id, image_name, parsed2)


async def check_images_batch(
  image_data_list: list[tuple[bytes, str, str]],
  user_description: str = '',
) -> BatchCheckResult:
  """批量检测多张图片。

  Args:
      image_data_list: [(bytes, image_name, media_type), ...]
      user_description: 用户补充描述
  """
  import asyncio

  tasks = [
    check_image(data, name, mime, user_description)
    for data, name, mime in image_data_list
  ]
  results = await asyncio.gather(*tasks)

  total_risks = sum(1 for r in results if r.has_risk)
  high_count = sum(1 for r in results if r.risk_level == '高')
  mid_count = sum(1 for r in results if r.risk_level == '中')

  summary_parts = [f'共检测 {len(results)} 张图片']
  if total_risks == 0:
    summary_parts.append('未发现明显安全隐患')
  else:
    summary_parts.append(
      f'发现 {total_risks} 个风险项（高: {high_count}, 中: {mid_count}, 低: {total_risks - high_count - mid_count}）'
    )

  return BatchCheckResult(
    total=len(image_data_list),
    results=list(results),
    summary='；'.join(summary_parts),
  )


# 启动时加载参考图
_load_reference_images()

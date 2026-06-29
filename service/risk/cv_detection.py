"""
CV 机柜指示灯检测管线：ORB 特征匹配 + 单应矩阵 + LAB 亮灭判定
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# 模板目录 — 每个子目录含 standard.jpg + config.json
_TEMPLATE_DIR = Path(__file__).parent.parent.parent / 'template'
_CABINET_TEMPLATES: dict[str, dict] = {}


def load_template_configs() -> None:
  """加载所有机柜模板配置（standard.jpg + config.json）"""
  global _CABINET_TEMPLATES
  if not _TEMPLATE_DIR.exists():
    logger.warning('模板目录不存在: %s', _TEMPLATE_DIR)
    return
  for subdir in _TEMPLATE_DIR.iterdir():
    if not subdir.is_dir():
      continue
    config_path = subdir / 'config.json'
    standard_path = subdir / 'standard.jpg'
    if not config_path.exists() or not standard_path.exists():
      logger.warning('模板文件不完整: %s', subdir.name)
      continue
    try:
      config = json.loads(config_path.read_text(encoding='utf-8'))
      template_img = cv2.imread(str(standard_path))
      if template_img is None:
        logger.warning('无法读取模板图: %s', standard_path)
        continue
      cab_type = config['cabinet_type']
      _CABINET_TEMPLATES[cab_type] = {
        'config': config,
        'image': template_img,
      }
      logger.info('加载机柜模板: %s lights=%d', cab_type, config.get('total_lights', 0))
    except Exception:
      logger.exception('加载模板失败: %s', subdir.name)


def get_available_cabinet_types() -> list[str]:
  """返回已加载的机柜类型列表"""
  return list(_CABINET_TEMPLATES.keys())


def _orb_match_and_detect(
  user_img: np.ndarray,
  template_img: np.ndarray,
  config: dict,
) -> Optional[dict]:
  """ORB 配准 + ROI 映射 + LAB 亮灭检测"""
  orb = cv2.ORB_create(nfeatures=2000)
  kp1, des1 = orb.detectAndCompute(template_img, None)
  kp2, des2 = orb.detectAndCompute(user_img, None)

  if des1 is None or des2 is None or len(kp1) < 5 or len(kp2) < 5:
    logger.warning('[CV] 特征点不足 ref=%d user=%d',
                   len(kp1) if kp1 else 0, len(kp2) if kp2 else 0)
    return None

  # FLANN LSH matcher
  index_params = dict(algorithm=6, table_number=6, key_size=12, multi_probe_level=1)
  search_params = dict(checks=50)
  flann = cv2.FlannBasedMatcher(index_params, search_params)

  try:
    matches = flann.knnMatch(des1, des2, k=2)
  except Exception:
    logger.exception('[CV] FLANN 匹配失败')
    return None

  good = [m for m, n in matches if m.distance < 0.75 * n.distance]
  logger.info('[CV] ORB 匹配 good=%d/%d', len(good), len(matches))

  if len(good) < 10:
    logger.warning('[CV] 好匹配不足: %d', len(good))
    return None

  src_pts = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
  dst_pts = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)

  H, mask = cv2.findHomography(dst_pts, src_pts, cv2.RANSAC, 5.0)
  inliers = int(mask.sum()) if mask is not None else 0
  logger.info('[CV] 单应矩阵 inliers=%d', inliers)

  if H is None or inliers < 8:
    logger.warning('[CV] 单应矩阵计算失败 inliers=%d', inliers)
    return None

  H_inv = np.linalg.inv(H)

  # 用户图转 LAB
  user_lab = cv2.cvtColor(user_img, cv2.COLOR_BGR2LAB)
  uh, uw = user_lab.shape[:2]

  lights_config = config['lights']
  observed_list: list[dict] = []
  violations: list[dict] = []

  for light in lights_config:
    rx, ry, rw, rh = light['roi']
    # ROI 四个角点映射到用户图
    corners = np.float32([
      [rx, ry],
      [rx + rw, ry],
      [rx + rw, ry + rh],
      [rx, ry + rh],
    ]).reshape(-1, 1, 2)

    mapped = cv2.perspectiveTransform(corners, H_inv)
    xs = mapped[:, 0, 0]
    ys = mapped[:, 0, 1]
    ux1 = max(0, int(np.floor(xs.min())))
    uy1 = max(0, int(np.floor(ys.min())))
    ux2 = min(uw, int(np.ceil(xs.max())))
    uy2 = min(uh, int(np.ceil(ys.max())))

    if ux2 <= ux1 or uy2 <= uy1:
      observed = 'unknown'
    else:
      roi_lab = user_lab[uy1:uy2, ux1:ux2]
      l_mean = roi_lab[:, :, 0].mean()

      # 亮灯判据: L > 145 → 亮
      if l_mean > 145:
        observed = '亮'
      else:
        observed = '灭'

    observed_list.append({'label': light['label'], 'observed': observed})

    expected = light['expected']
    if observed != 'unknown' and observed != expected:
      violations.append({
        'category': '指示灯异常',
        'description': (
          f"{light['label']}：标准要求应为{expected}，实际观察为{observed}"
        ),
        'regulation': '消防设备日常巡检规范要求控制柜指示灯状态应与标准运行状态一致',
        'suggestion': f"请检查{light['label']}指示灯及其控制回路是否正常",
      })

  has_risk = len(violations) > 0
  if not has_risk:
    risk_level = 'none'
  elif len(violations) >= 3:
    risk_level = 'high'
  else:
    risk_level = 'medium'

  # 构建 description
  observed_desc = '，'.join(
    f"{o['label']}{o['observed']}" for o in observed_list
  )
  description = (
    f"该机柜为{config['cabinet_type']}。检查结果：{observed_desc}。"
    f"共检测{len(lights_config)}个指示灯。"
  )

  return {
    'has_risk': has_risk,
    'risk_level': risk_level,
    'risk_categories': ['指示灯异常'] if violations else [],
    'description': description,
    'violations': violations,
    'confidence': 0.92,
    'cabinet_type': config['cabinet_type'],
    'observed': observed_list,
  }


def cabinet_cv_detect(image_bytes: bytes, cabinet_type: str) -> Optional[dict]:
  """机柜指示灯 CV 检测入口

  Args:
      image_bytes: 图片原始字节
      cabinet_type: Phase 1 识别出的机柜类型名

  Returns:
      检测结果 dict，若模板缺失或 CV 失败返回 None
  """
  template = _CABINET_TEMPLATES.get(cabinet_type)
  if not template:
    logger.warning('[CV] 无模板: %s', cabinet_type)
    return None

  user_img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
  if user_img is None:
    logger.warning('[CV] 无法解码用户图片')
    return None

  t0 = time.perf_counter()
  result = _orb_match_and_detect(user_img, template['image'], template['config'])
  elapsed = (time.perf_counter() - t0) * 1000
  logger.info(
    '[CV] 检测完成 cabinet=%s has_risk=%s elapsed=%.0fms',
    cabinet_type, result.get('has_risk') if result else None, elapsed,
  )
  return result


# 启动时加载模板
load_template_configs()

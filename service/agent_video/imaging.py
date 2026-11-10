"""图像小工具：本地图片 → 视觉模型能吃的 data URL。

`model_gateway.vision(images=[...])` 只接受 **base64 字符串 / data URI / http(s) URL**，
传本地文件路径会被当成 base64 去解析，报 `Non-base64 digit found`（实测踩过：RITL 自检与
无文字页的视觉描述都因此静默失效）。
"""

import base64
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_MIME = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
         '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp'}


def to_data_url(path: str) -> str:
  """本地图片 → `data:image/png;base64,...`；已是 URL/data URI 或读不到时原样返回。"""
  s = str(path or '')
  if not s or s.startswith(('http://', 'https://', 'data:')):
    return s
  try:
    raw = Path(s).read_bytes()
    mime = _MIME.get(Path(s).suffix.lower(), 'image/png')
    return f'data:{mime};base64,{base64.b64encode(raw).decode("ascii")}'
  except Exception as e:  # noqa: BLE001 - 读不到就让调用方按原有错误路径处理
    logger.warning('图片转 data URL 失败（%s）: %s', s, e)
    return s

"""抽取每页文字（python-pptx），无文字页用视觉模型描述补位。

为什么要两层：解说词要"讲这页在说什么"，纯文字页用 python-pptx 直接取最准最便宜；
但封面/纯图表页往往一个文本框都没有，此时给视觉模型看一眼图片、拿一句客观描述，
比让模型凭空编要可靠得多（对齐 RITL 的思路：不确定就去看）。
"""

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DESCRIBE_SYSTEM = (
  '你在看一页 PPT 幻灯片。请用一到两句话客观描述这页的主要内容（标题、图表在说什么、'
  '关键数字），只描述你确实看到的，不要补充推测或建议。'
  '**不要**写"这是封面页/目录页/本次汇报的封面"这类版面身份说明——直接给出这页的标题与内容'
  '（封面就把标题写出来）。'
)


def _collect_shape_text(shape) -> list[str]:
  """递归收集形状里的可见文字（含组合形状；跳过表格/图表内部结构）。"""
  parts: list[str] = []
  try:
    if getattr(shape, 'has_text_frame', False) and shape.text_frame:
      text = '\n'.join(p.text for p in shape.text_frame.paragraphs if p.text)
      if text.strip():
        parts.append(text.strip())
    if getattr(shape, 'has_table', False) and shape.has_table:
      for row in shape.table.rows:
        cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
        if cells:
          parts.append(' | '.join(cells))
    if getattr(shape, 'shape_type', None) is not None and shape.shape_type == 6:  # GROUP
      for child in shape.shapes:
        parts.extend(_collect_shape_text(child))
  except Exception:  # noqa: BLE001 - 单个形状解析失败不影响整页
    pass
  return parts


def _extract_sync(ppt_path: str) -> list[str]:
  from pptx import Presentation

  prs = Presentation(ppt_path)
  pages: list[str] = []
  for slide in prs.slides:
    chunks: list[str] = []
    for shape in slide.shapes:
      chunks.extend(_collect_shape_text(shape))
    pages.append('\n'.join(chunks))
  return pages


async def extract_page_texts(ppt_path: str) -> list[str]:
  """PPT → 每页文字（python-pptx；无文字的页返回空串）。

  放在线程池里跑：python-pptx 是同步 CPU/IO 操作，别阻塞事件循环（旧 pipeline 同款处理）。
  """
  return await asyncio.to_thread(_extract_sync, ppt_path)


async def describe_image(image_path: str) -> str:
  """图片页 → 一句客观描述（视觉模型不可用时返回空串，不阻塞流程）。"""
  from core.model_gateway import model_gateway

  from .imaging import to_data_url

  try:
    text = await model_gateway.vision(
      system_prompt=_DESCRIBE_SYSTEM,
      user_text='这页幻灯片讲了什么？',
      images=[to_data_url(image_path)],
      temperature=0.2,
      max_tokens=400,
    )
    return (text or '').strip()
  except Exception as e:  # noqa: BLE001
    logger.warning('视觉描述失败（该页按无文字处理）: %s', e)
    return ''


async def page_texts_with_vision(ppt_path: str, images: list[str]) -> list[str]:
  """每页文字；无文字且已有渲染图的页，用视觉描述补位。"""
  texts = await extract_page_texts(ppt_path)
  if len(texts) < len(images):
    texts += [''] * (len(images) - len(texts))

  empty = [i for i, t in enumerate(texts[:len(images)]) if not (t or '').strip()]
  if not empty:
    return texts

  logger.info('%d 页无文字，改用视觉模型描述', len(empty))
  descriptions = await asyncio.gather(
    *(describe_image(p) for p in (images[i] for i in empty)),
    return_exceptions=True,
  )
  for idx, desc in zip(empty, descriptions):
    if isinstance(desc, str) and desc.strip():
      texts[idx] = desc
  return texts[:len(images)]

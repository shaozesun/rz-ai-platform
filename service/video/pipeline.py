"""PPT → video orchestration pipeline.

Orchestrates PPT-to-video conversion across local and remote services:
  1. python-pptx (local)  — extract slide text
  2. llm_text_generator   — generate narration scripts (remote :8010)
  3. ppt_to_images        — convert slides to PNG (remote :8020)
  3. TTS                  — synthesize audio in parallel (remote :8000 / local edge)
  4. ppt_video_renderer   — compose images + audio + subtitles → MP4 (remote :8030)
"""

import os
import json
import asyncio
from datetime import datetime
from typing import Optional

import httpx
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from config.settings import settings
import logging
from config.mongodb_conn import mongodb_manager
from models.video import VideoTaskStatus, VideoTaskParams
from service.video.tts import synthesize_speech, make_audio_url

logger = logging.getLogger(__name__)


async def run_video_pipeline(task_id: str):
  """Execute the full PPT-to-video pipeline for a given task."""
  db = mongodb_manager.db
  task = await db.video_tasks.find_one({'task_id': task_id})
  if not task:
    logger.error(f'Task not found: {task_id}')
    return

  file_path = task.get('file_path', '')
  params = VideoTaskParams(**task.get('params', {}))
  public_base = settings.PUBLIC_BASE_URL.rstrip('/')

  try:
    # ── Step 1: Extract text ──
    await _update_status(db, task_id, VideoTaskStatus.EXTRACTING, 5, '提取 PPT 文字...')
    texts = await _extract_text(file_path)
    logger.info(f'[{task_id}] Extracted {len(texts)} slides text')

    # ── Step 2: Generate narration scripts ──
    await _update_status(db, task_id, VideoTaskStatus.GENERATING_SCRIPT, 20, '生成解说词...')
    scripts = await _generate_scripts(texts)
    logger.info(f'[{task_id}] Generated {len(scripts)} scripts')

    # ── Step 3: Convert images + Synthesize audio (parallel) ──
    await _update_status(db, task_id, VideoTaskStatus.CONVERTING_IMAGES, 40,
                         '转换图片 + 合成语音...')
    images_future = _convert_images(file_path)
    audio_future = _synthesize_all(scripts, params.voice, public_base)
    images_result, audio_result = await asyncio.gather(images_future, audio_future)
    images = images_result
    logger.info(f'[{task_id}] Images: {len(images)}, Audio: {len(audio_result)}')
    durations = [a.get('duration', 0) for a in audio_result]
    logger.info(f'[{task_id}] Audio durations: {durations}')
    total_dur = sum(durations)
    logger.info(f'[{task_id}] Total audio duration: {total_dur:.1f}s')

    # ── Step 4: Render video ──
    await _update_status(db, task_id, VideoTaskStatus.RENDERING, 70, '合成视频...')
    video_url = await _render_video(images, audio_result, scripts, task_id)
    logger.info(f'[{task_id}] Video rendered: {video_url}')

    # ── Success ──
    await db.video_tasks.update_one(
      {'task_id': task_id},
      {'$set': {
        'status': VideoTaskStatus.SUCCESS,
        'progress': 100,
        'progress_text': '完成',
        'result_url': video_url,
        'updated_at': datetime.utcnow(),
      }}
    )

  except Exception as e:
    logger.error(f'[{task_id}] Pipeline failed at step: {e}')
    await db.video_tasks.update_one(
      {'task_id': task_id},
      {'$set': {
        'status': VideoTaskStatus.FAILED,
        'error': str(e),
        'updated_at': datetime.utcnow(),
      }}
    )


async def _update_status(db, task_id: str, status: VideoTaskStatus,
                         progress: int, text: str):
  await db.video_tasks.update_one(
    {'task_id': task_id},
    {'$set': {
      'status': status,
      'progress': progress,
      'progress_text': text,
      'updated_at': datetime.utcnow(),
    }}
  )


async def _call_plugin(endpoint: str, timeout: int = 300, **payload) -> dict:
  """Call a plugin API with the internal auth token."""
  headers = {}
  token = settings.PLUGIN_API_TOKEN
  if token:
    headers['X-API-Key'] = token
  async with httpx.AsyncClient(timeout=timeout) as client:
    resp = await client.post(endpoint, headers=headers, **payload)
    resp.raise_for_status()
    return resp.json()


# ── Step 1 ──

def _collect_shape_text(shape) -> list[str]:
  """Recursively collect visible text from a PPT shape (adapted from ppt_text_extractor)."""
  parts: list[str] = []

  if getattr(shape, 'shape_type', None) == MSO_SHAPE_TYPE.GROUP:
    for child in shape.shapes:
      parts.extend(_collect_shape_text(child))
    return parts

  if getattr(shape, 'has_text_frame', False):
    text = shape.text_frame.text.strip()
    if text:
      parts.append(text)

  if getattr(shape, 'has_table', False):
    for row in shape.table.rows:
      row_parts = [cell.text.strip() for cell in row.cells if cell.text.strip()]
      if row_parts:
        parts.append(' | '.join(row_parts))

  return parts


async def _extract_text(file_path: str) -> list[str]:
  """Extract visible text from each slide using local python-pptx."""
  try:
    prs = Presentation(file_path)
  except Exception as e:
    raise RuntimeError(f'无法读取 PPTX 文件: {e}')

  texts: list[str] = []
  for index, slide in enumerate(prs.slides):
    page_parts: list[str] = []
    for shape in slide.shapes:
      page_parts.extend(_collect_shape_text(shape))
    page_text = '\n'.join(p for p in page_parts if p).strip()
    texts.append(f'{index}:{page_text}')

  logger.info(f'Extracted text from {len(texts)} slides')
  return texts


# ── Step 2 ──

async def _generate_scripts(texts: list[str]) -> list[str]:
  """Generate narration script for each slide via LLM plugin."""
  scripts: list[str] = []
  for i, text in enumerate(texts):
    data = await _call_plugin(
      f'{settings.PLUGIN_LLM_GENERATOR_URL}/generate',
      json={
        'system_prompt': (
          '你是一个专业的教学视频解说员。请根据PPT页面内容，'
          '生成一段自然流畅的解说词。语言生动、简洁，适合口头朗读。'
          '字数控制在300字以内。只输出解说词文本，不要输出其他内容。'
        ),
        'user_content': f'PPT第{i+1}页内容:\n{text}',
        'temperature': 0.7,
        'max_tokens': 2048,
      },
    )
    script = data.get('output', '')
    scripts.append(script)
  return scripts


# ── Step 3 ──

async def _convert_images(file_path: str) -> list[str]:
  """Convert PPT slides to PNG images."""
  url = f'{settings.PLUGIN_IMAGE_CONVERTER_URL}/convert_from_upload'
  with open(file_path, 'rb') as f:
    data = await _call_plugin(
      url,
      files={'file': (os.path.basename(file_path), f,
                      'application/vnd.openxmlformats-officedocument.presentationml.presentation')},
      timeout=600,
    )
  return data.get('images', [])


async def _synthesize_all(scripts: list[str], voice: str,
                          base_url: str) -> list[dict]:
  """Synthesize audio for all scripts sequentially (Qwen TTS limit: 2 concurrent).

  Returns list of {audio_url, duration}.
  """
  results: list[dict] = []
  for i, script in enumerate(scripts):
    logger.info(f'Synthesizing audio {i+1}/{len(scripts)}...')
    path_or_url, duration = await synthesize_speech(script, voice)
    results.append({
      'audio_url': make_audio_url(path_or_url, base_url),
      'duration': duration,
    })
  return results


# ── Step 4 ──

async def _render_video(images: list[str], audios: list[dict],
                        scripts: list[str], task_id: str) -> str:
  """Compose images + audio + subtitles into final MP4 video."""
  slides = []
  for i in range(len(images)):
    audio = audios[i] if i < len(audios) else {}
    slides.append({
      'image_url': images[i] if i < len(images) else '',
      'audio_url': audio.get('audio_url', ''),
      'subtitle': scripts[i] if i < len(scripts) else '',
      'duration': audio.get('duration', 0),
    })

  data = await _call_plugin(
    f'{settings.PLUGIN_VIDEO_RENDERER_URL}/render_video',
    json={'output': json.dumps({'slides': slides, 'task_id': task_id})},
    timeout=1200,
  )
  logger.info(f'[{task_id}] Renderer response: {json.dumps(data, ensure_ascii=False)[:500]}')
  return data.get('video_url', '')

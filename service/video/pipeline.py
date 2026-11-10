"""PPT → video orchestration pipeline.

Orchestrates PPT-to-video conversion across local and remote services:
  1. python-pptx (local)  — extract slide text
  2. llm_text_generator   — generate narration scripts (remote :8010)
  3. ppt_to_images        — convert slides to PNG (remote :8020)
  3. TTS                  — synthesize audio (remote :8000)
  4. ppt_video_renderer   — compose images + audio + subtitles → MP4 (remote :8030)
"""

import os
import json
import time
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

import httpx
import redis.asyncio as aioredis
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from config.settings import settings
import logging
from config.mongodb_conn import mongodb_manager
from config.redis_conn import redis_manager
from core.scheduler.semaphore import DistributedSemaphore
from models.video import VideoTaskStatus, VideoTaskParams
from service.video.tts import synthesize_speech, make_audio_url

logger = logging.getLogger(__name__)

_ppt_executor = ThreadPoolExecutor(max_workers=1)


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
  audio_results = []  # 存原始路径，用于 finally 清理

  try:
    # ── Step 1: Extract text ──
    await _update_status(db, task_id, VideoTaskStatus.EXTRACTING, 5, '提取 PPT 文字...')
    texts = await _extract_text(file_path)
    logger.info(f'[{task_id}] Extracted {len(texts)} slides text')

    # ── Step 2: Generate narration scripts ──
    await _update_status(db, task_id, VideoTaskStatus.GENERATING_SCRIPT, 20, '生成解说词...')
    speech_texts, subtitle_texts = await _generate_scripts(
      texts, params.speech_language, params.subtitle_language)
    logger.info(f'[{task_id}] Generated {len(speech_texts)} scripts')

    # ── Step 3: Convert images + Synthesize audio (parallel) ──
    await _update_status(db, task_id, VideoTaskStatus.CONVERTING_IMAGES, 40,
                         '转换图片 + 合成语音...')
    images_future = _convert_images(file_path)
    audio_future = _synthesize_all(speech_texts, params.voice, public_base,
                                   params.speech_language)
    images_result, audio_result = await asyncio.gather(images_future, audio_future)
    images = images_result
    audio_results = audio_result  # 存下来供 finally 清理
    logger.info(f'[{task_id}] Images: {len(images)}, Audio: {len(audio_result)}')
    durations = [a.get('duration', 0) for a in audio_result]
    logger.info(f'[{task_id}] Audio durations: {durations}')
    total_dur = sum(durations)
    logger.info(f'[{task_id}] Total audio duration: {total_dur:.1f}s')

    # ── Step 4: Render video ──
    await _update_status(db, task_id, VideoTaskStatus.RENDERING, 70, '合成视频...')
    video_url = await _render_video(images, audio_result, subtitle_texts,
                                    params.aspect_ratio, params.resolution)
    logger.info(f'[{task_id}] Video rendered: {video_url}')

    # Download MP4 from renderer to local videos/ for fast serving
    os.makedirs(settings.VIDEO_DIR, exist_ok=True)
    local_video = os.path.join(settings.VIDEO_DIR, f'{task_id}.mp4')
    result_url = await _download_video(video_url, local_video)
    logger.info(f'[{task_id}] Video saved locally: {result_url}')

    # ── Success ──
    await db.video_tasks.update_one(
      {'task_id': task_id},
      {'$set': {
        'status': VideoTaskStatus.SUCCESS,
        'progress': 100,
        'progress_text': '完成',
        'result_url': result_url,
        'updated_at': datetime.now(),
      }}
    )

  except Exception as e:
    logger.error(f'[{task_id}] Pipeline failed at step: {e}')
    await db.video_tasks.update_one(
      {'task_id': task_id},
      {'$set': {
        'status': VideoTaskStatus.FAILED,
        'error': str(e),
        'updated_at': datetime.now(),
      }}
    )
  finally:
    _cleanup_files(file_path, audio_results)


def _cleanup_files(file_path: str, audio_results: list[dict]):
  """删除 PPT 原文件和本地 TTS 音频（不论成功失败都清理）。"""
  # 删 PPT
  if file_path and os.path.isfile(file_path):
    try:
      os.remove(file_path)
      logger.info(f'Cleaned up PPT: {file_path}')
    except OSError:
      pass

  # 删本地 TTS 文件（Qwen TTS 返回远程 URL，跳过）
  for a in audio_results:
    path = a.get('audio_path', '')
    if path and not path.startswith('http') and os.path.isfile(path):
      try:
        os.remove(path)
        logger.info(f'Cleaned up TTS audio: {path}')
      except OSError:
        pass


async def _update_status(db, task_id: str, status: VideoTaskStatus,
                         progress: int, text: str):
  await db.video_tasks.update_one(
    {'task_id': task_id},
    {'$set': {
      'status': status,
      'progress': progress,
      'progress_text': text,
      'updated_at': datetime.now(),
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


def _extract_text_sync(file_path: str) -> list[str]:
  """Synchronous text extraction from PPTX (runs in thread pool)."""
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


async def _extract_text(file_path: str) -> list[str]:
  """Extract visible text from each slide (offloaded to thread pool)."""
  loop = asyncio.get_running_loop()
  return await loop.run_in_executor(_ppt_executor, _extract_text_sync, file_path)


# ── Step 2 ──

async def _generate_scripts(texts: list[str], speech_language: str,
                            subtitle_language: str) -> tuple[list[str], list[str]]:
  """Generate narration script for each slide via LLM plugin (parallel).

  返回 (解说词, 字幕) 两份文本：8010 分别产出 `speech_text`（送 TTS）与
  `subtitle_text`（送渲染器），「英文解说 + 中文字幕」这类组合只能靠分开取实现。
  旧插件不返回新字段时按 `output` 回退，字幕回退到解说词（即改动前的行为）。
  """
  async def _gen_one(i: int, text: str) -> tuple[int, str, str]:
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
        'speech_language': speech_language,
        'subtitle_language': subtitle_language,
      },
    )
    speech = data.get('speech_text') or data.get('output', '')
    return i, speech, data.get('subtitle_text') or speech

  tasks = [_gen_one(i, text) for i, text in enumerate(texts)]
  results = await asyncio.gather(*tasks)
  # 按原始页序排列
  results.sort(key=lambda x: x[0])
  return [s for _, s, _ in results], [sub for _, _, sub in results]


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


_tts_slots: Optional[DistributedSemaphore] = None
_tts_redis: Optional[aioredis.Redis] = None
_tts_slot_key: str = ''
_tts_local_slots: Optional[asyncio.Semaphore] = None


def _get_tts_local_slots() -> asyncio.Semaphore:
  """本进程在飞的 TTS 请求上限。

  必须是进程级而非每次流水线各建一个：同一 worker 并发跑 3 个视频任务时，
  每任务一个信号量等于 3×N 个请求同时去抢全局槽位，等待时间失控。
  """
  global _tts_local_slots
  if _tts_local_slots is None:
    _tts_local_slots = asyncio.Semaphore(settings.TTS_LOCAL_CONCURRENCY)
  return _tts_local_slots


def _get_tts_slots() -> Optional[DistributedSemaphore]:
  """跨进程 TTS 槽位（Redis 信号量）。

  上游 tts_proxy 硬限同时 2 个请求，而生产是 --workers 4：进程内的 asyncio
  信号量最多只限住 1/4 的并发。建失败返回 None，调用方按 fail-open 处理。
  """
  global _tts_slots, _tts_redis, _tts_slot_key
  if _tts_slots is None:
    try:
      client = aioredis.Redis(
        host=settings.REDIS_HOST,
        port=settings.REDIS_PORT,
        password=settings.REDIS_PASSWORD or None,
        db=settings.REDIS_DB,
        decode_responses=True,
        socket_timeout=5,
      )
      key = redis_manager.key('video:tts:slots')
      _tts_slots = DistributedSemaphore(
        client, key, settings.TTS_MAX_CONCURRENCY,
      )
      _tts_redis, _tts_slot_key = client, key
    except Exception as e:
      logger.warning(f'TTS 跨进程配额初始化失败，降级为不限流: {e}')
  return _tts_slots


async def _touch_slot_ttl():
  """给槽位计数器续租约。

  DistributedSemaphore 是 INCR/DECR 计数、没有过期时间，worker 被 kill 会把
  计数永久漏在 Redis 里。加 TTL 后泄漏最迟 TTS_SLOT_LEASE_TTL 秒自愈 ——
  这正是「等不到槽位只好放行」那个兜底可以收紧的原因。
  """
  if _tts_redis is None or not _tts_slot_key:
    return
  try:
    await _tts_redis.expire(_tts_slot_key, settings.TTS_SLOT_LEASE_TTL)
  except Exception as e:
    logger.warning(f'TTS 槽位租约续期失败（不影响本次配额）: {e}')


async def _acquire_tts_slot(slots: Optional[DistributedSemaphore]) -> bool:
  """获取一个 TTS 槽位，返回是否真正持有（False = 放行但未计数，无需释放）。

  只对「Redis 不可用」fail-open；「槽位被别人占着」是正常排队，必须一直等。
  早期版本把两者混为一谈，等 60s 就放行，于是 50 页 PPT 在 60s 时成批涌向
  上游（上游硬限 2），429 成灾 —— 故障不是上游太忙，是我们没守住配额。
  """
  if slots is None:
    return False
  deadline = time.monotonic() + settings.TTS_SLOT_WAIT_TIMEOUT
  while time.monotonic() < deadline:
    try:
      if await slots.try_acquire():
        await _touch_slot_ttl()
        return True
    except Exception as e:
      logger.warning(f'TTS 槽位获取异常，降级为不限流: {e}')
      return False
    await asyncio.sleep(0.2)
  logger.error(f'TTS 槽位等待超过 {settings.TTS_SLOT_WAIT_TIMEOUT}s（异常），降级为不限流')
  return False


async def _synthesize_all(scripts: list[str], voice: str,
                          base_url: str,
                          speech_language: str = 'zh-CN') -> list[dict]:
  """Synthesize audio for all scripts.

  并发分两层：本地 TTS_LOCAL_CONCURRENCY 限制本进程在飞数（否则几十页会一次性
  堆成几十个等待者），跨进程由 Redis 配额 TTS_MAX_CONCURRENCY 统一限流，
  见 _get_tts_slots / _get_tts_local_slots。

  Returns list of {audio_url, duration}.
  """
  slots = _get_tts_slots()
  local_slots = _get_tts_local_slots()

  async def _synth_one(i: int, script: str) -> tuple[int, dict]:
    async with local_slots:
      held = await _acquire_tts_slot(slots)
      try:
        logger.info(f'Synthesizing audio {i+1}/{len(scripts)}...')
        path_or_url, duration = await synthesize_speech(
          script, voice, speech_language)
        return i, {
          'audio_url': make_audio_url(path_or_url, base_url),
          'duration': duration,
          'audio_path': path_or_url,  # 原始路径，用于清理本地文件
        }
      finally:
        if held and slots is not None:
          await slots.release()

  tasks = [_synth_one(i, s) for i, s in enumerate(scripts)]
  results = await asyncio.gather(*tasks)
  results.sort(key=lambda x: x[0])
  return [r for _, r in results]


# ── Step 4 ──

async def _render_video(images: list[str], audios: list[dict],
                        scripts: list[str],
                        aspect_ratio: str = '16:9',
                        quality: str = '1080p') -> str:
  """Compose images + audio + subtitles into final MP4 video."""
  slides = []
  for i in range(len(images)):
    audio = audios[i] if i < len(audios) else {}
    slides.append({
      'index': i + 1,
      'image_url': images[i] if i < len(images) else '',
      'audio_url': audio.get('audio_url', ''),
      'subtitle_text': scripts[i] if i < len(scripts) else '',
      'duration': audio.get('duration', 0),
    })

  data = await _call_plugin(
    f'{settings.PLUGIN_VIDEO_RENDERER_URL}/render_video',
    json={
      'aspect_ratio': aspect_ratio,
      'quality': quality,
      'slides': slides,
    },
    timeout=1200,
  )
  logger.info(f'Renderer response: {json.dumps(data, ensure_ascii=False)[:500]}')
  return data.get('video_url', '')


async def _download_video(remote_url: str, local_path: str) -> str:
  """Download MP4 from renderer to local storage. Returns local path on success,
  falls back to remote URL on failure so the pipeline doesn't break."""
  if not remote_url:
    logger.warning('Empty video URL from renderer, skipping download')
    return remote_url
  try:
    if not remote_url.startswith('http'):
      remote_url = settings.PLUGIN_VIDEO_RENDERER_URL.rstrip('/') + '/' + remote_url.lstrip('/')
    async with httpx.AsyncClient(timeout=600) as client:
      async with client.stream('GET', remote_url) as resp:
        resp.raise_for_status()
        with open(local_path, 'wb') as f:
          async for chunk in resp.aiter_bytes(65536):
            f.write(chunk)
    logger.info(f'Downloaded video: {remote_url} → {local_path}')
    return local_path
  except Exception as e:
    logger.warning(f'Failed to download video to local, fallback to remote URL: {e}')
    return remote_url

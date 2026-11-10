"""TTS voice synthesis with multiple backends: OpenAI TTS, Qwen TTS."""

import os
import uuid
import asyncio
import random
import traceback

import httpx
from config.settings import settings
import logging

logger = logging.getLogger(__name__)

TTS_OUTPUT_DIR = os.path.join(settings.UPLOAD_DIR, 'tts')
os.makedirs(TTS_OUTPUT_DIR, exist_ok=True)


async def synthesize_speech(script: str, voice: str = 'neutral',
                            speech_language: str = 'zh-CN') -> tuple[str, float]:
  """将单段文本合成为语音，返回 (url_or_path, duration_seconds)。

  后端由 TTS_PROVIDER 决定；失败即抛出，不再静默降级到其他后端。
  """
  provider = settings.TTS_PROVIDER

  if provider == 'qwen':
    try:
      return await _synthesize_qwen(script, voice, speech_language)
    except Exception as e:
      logger.error('Qwen TTS 失败:\n%s', traceback.format_exc())
      raise RuntimeError(f'Qwen TTS 合成失败: {e}') from e

  if provider == 'openai':
    output_path = os.path.join(TTS_OUTPUT_DIR, f'{uuid.uuid4().hex}.mp3')
    try:
      path = await _synthesize_openai(script, voice, output_path)
    except Exception as e:
      logger.error('OpenAI TTS 失败:\n%s', traceback.format_exc())
      raise RuntimeError(f'OpenAI TTS 合成失败: {e}') from e
    return path, _estimate_duration(script)

  raise RuntimeError(f'未知的 TTS_PROVIDER: {provider!r}')


async def _synthesize_openai(text: str, voice: str, output_path: str) -> str:
  """OpenAI TTS API (备选)."""
  voice_map = {'neutral': 'alloy', 'male': 'onyx', 'female': 'nova'}
  api_voice = voice_map.get(voice, 'alloy')

  async with httpx.AsyncClient(timeout=60) as client:
    resp = await client.post(
      f'{settings.LLM_BASE_URL}/audio/speech',
      headers={'Authorization': f'Bearer {settings.LLM_API_KEY}'},
      json={
        'model': 'tts-1',
        'input': text,
        'voice': api_voice,
        'response_format': 'mp3',
      },
    )
    resp.raise_for_status()
    with open(output_path, 'wb') as f:
      f.write(resp.content)

  logger.info(f'OpenAI TTS saved: {output_path}')
  return output_path


def _estimate_duration(text: str) -> float:
  """根据文本长度估算中文 TTS 音频时长，约 4 字/秒，下限 1.0 秒。"""
  return max(1.0, len(text) / 4.0)


# 上游壳的 MAX_TEXT_LENGTH 同名限制，两处必须一致（改一处无效）
QWEN_TTS_MAX_CHARS = 1200
QWEN_TTS_MAX_RETRIES = 3
# 退避基数：单段合成本身要几十秒、槽位周转约 25s，1/2/4s 的退避等于没退，
# 三枪会在上游腾出位子之前全部打完
QWEN_TTS_RETRY_BASE_DELAY = 5.0


async def _synthesize_qwen(text: str, voice: str,
                           speech_language: str = 'zh-CN') -> str:
  """Qwen TTS API (remote at port 8000). Returns a remote audio URL.

  Text is truncated to ``QWEN_TTS_MAX_CHARS`` chars, breaking at the last
  sentence boundary when possible.  Automatically retries on HTTP 429.
  """
  if not text or not text.strip():
    raise RuntimeError('TTS 文本为空，无法合成语音')

  text = text.strip()
  if len(text) > QWEN_TTS_MAX_CHARS:
    original_len = len(text)
    truncated = text[:QWEN_TTS_MAX_CHARS]
    # 中英断句符都要有：只认中文标点时，英文超限会硬切在单词中间
    for sep in ('。', '！', '？', '；', '.', '!', '?', ';', '\n'):
      idx = truncated.rfind(sep)
      if idx > QWEN_TTS_MAX_CHARS // 2:
        text = truncated[:idx + 1]
        break
    else:
      text = truncated
    logger.info(f'Qwen TTS text truncated: {original_len} -> {len(text)} chars')

  headers = {}
  token = settings.PLUGIN_API_TOKEN
  if token:
    headers['X-API-Key'] = token

  last_error = None
  for attempt in range(QWEN_TTS_MAX_RETRIES):
    try:
      async with httpx.AsyncClient(timeout=settings.TTS_REQUEST_TIMEOUT) as client:
        resp = await client.post(
          f'{settings.TTS_API_URL}/tts',
          headers=headers,
          data={'text': text, 'format': 'wav',
                'speech_language': speech_language},
        )
    except httpx.TransportError as e:
      # 传输层异常（ReadTimeout / ConnectError ...）在 await 处就抛出，原先没有 try
      # 会直接冲出整个重试循环：一次网络抖动 = 一段音频报废 = 整本视频报废。
      # 与 429 同属「可重试」，走同一套退避
      last_error = f'{type(e).__name__}: {e}'
      wait = QWEN_TTS_RETRY_BASE_DELAY * 2 ** attempt + random.uniform(0, 1)
      logger.warning(f'Qwen TTS 传输错误 (attempt {attempt+1}/{QWEN_TTS_MAX_RETRIES}), '
                     f'waiting {wait:.1f}s: {last_error}')
      await asyncio.sleep(wait)
      continue
    # 上游限流是 429，但 tts_proxy 把它包成 502 返回，只认 == 429 会让重试完全失效
    if resp.status_code == 429 or 'Upstream TTS HTTP 429' in resp.text:
      wait = QWEN_TTS_RETRY_BASE_DELAY * 2 ** attempt + random.uniform(0, 1)  # 抖动防同步
      last_error = resp.text
      logger.warning(f'Qwen TTS 429 (attempt {attempt+1}/{QWEN_TTS_MAX_RETRIES}), '
                     f'waiting {wait:.1f}s')
      await asyncio.sleep(wait)
      continue
    if resp.status_code >= 400:
      logger.error(f'Qwen TTS error {resp.status_code}: {resp.text}')
    resp.raise_for_status()
    data = resp.json()
    audio_url = data.get('audio_url') or data.get('url') or ''
    duration = float(data.get('duration', 0))
    if not audio_url:
      logger.error(f'Qwen TTS response missing audio_url: {data}')
      raise RuntimeError('Qwen TTS API 未返回音频地址')
    logger.info(f'Qwen TTS: duration={duration:.1f}s ({len(text)} chars)')
    return audio_url, duration

  raise RuntimeError(f'Qwen TTS 重试 {QWEN_TTS_MAX_RETRIES} 次后仍失败: {last_error}')


def make_audio_url(file_path: str, base_url: str) -> str:
  """返回音频的可访问 URL。

  如果 file_path 已经是完整 URL（如 Qwen TTS 返回的远程地址），直接返回；
  否则转换为本地的 /api/v1/video/audio/ 静态文件 URL。
  """
  if file_path.startswith('http://') or file_path.startswith('https://'):
    return file_path
  filename = os.path.basename(file_path)
  return f'{base_url}/api/v1/video/audio/{filename}'

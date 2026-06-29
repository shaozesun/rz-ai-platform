"""TTS voice synthesis with multiple backends: Edge TTS, OpenAI TTS, Qwen TTS."""

import os
import uuid
import asyncio

import httpx
from config.settings import settings
import logging

logger = logging.getLogger(__name__)

TTS_OUTPUT_DIR = os.path.join(settings.UPLOAD_DIR, 'tts')
os.makedirs(TTS_OUTPUT_DIR, exist_ok=True)


async def synthesize_speech(script: str, voice: str = 'neutral') -> tuple[str, float]:
  """将单段文本合成为语音，返回 (url_or_path, duration_seconds)。

  Qwen TTS 返回远程音频 URL，其他 provider 返回本地文件路径。
  """
  if settings.TTS_PROVIDER == 'qwen':
    return await _synthesize_qwen(script, voice)
  if settings.TTS_PROVIDER == 'openai':
    output_path = os.path.join(TTS_OUTPUT_DIR, f'{uuid.uuid4().hex}.mp3')
    path = await _synthesize_openai(script, voice, output_path)
    return path, 0.0
  output_path = os.path.join(TTS_OUTPUT_DIR, f'{uuid.uuid4().hex}.mp3')
  path = await _synthesize_edge(script, voice, output_path)
  return path, 0.0


async def _synthesize_edge(text: str, voice: str, output_path: str) -> str:
  """Edge TTS (默认)."""
  import edge_tts

  voice_map = {
    'neutral': 'zh-CN-XiaoxiaoNeural',
    'male': 'zh-CN-YunxiNeural',
    'female': 'zh-CN-XiaoyiNeural',
  }
  tts_voice = voice_map.get(voice, 'zh-CN-XiaoxiaoNeural')

  communicate = edge_tts.Communicate(text, tts_voice)
  await communicate.save(output_path)

  logger.info(f'Edge TTS saved: {output_path} ({len(text)} chars, voice={tts_voice})')
  return output_path


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


QWEN_TTS_MAX_CHARS = 300
QWEN_TTS_MAX_RETRIES = 3


async def _synthesize_qwen(text: str, voice: str) -> str:
  """Qwen TTS API (remote at port 8000). Returns a remote audio URL.

  Text is truncated to 300 chars (Qwen TTS limit), breaking at the last
  sentence boundary when possible.  Automatically retries on HTTP 429.
  """
  if not text or not text.strip():
    raise RuntimeError('TTS 文本为空，无法合成语音')

  text = text.strip()
  if len(text) > QWEN_TTS_MAX_CHARS:
    original_len = len(text)
    truncated = text[:QWEN_TTS_MAX_CHARS]
    for sep in ('。', '！', '？', '；', '\n'):
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
    async with httpx.AsyncClient(timeout=120) as client:
      resp = await client.post(
        f'{settings.TTS_API_URL}/tts',
        headers=headers,
        data={'text': text, 'format': 'wav'},
      )
    if resp.status_code == 429:
      wait = 2 ** attempt  # 1, 2, 4 seconds backoff
      last_error = resp.text
      logger.warning(f'Qwen TTS 429 (attempt {attempt+1}/{QWEN_TTS_MAX_RETRIES}), '
                     f'waiting {wait}s')
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

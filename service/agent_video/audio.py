"""逐页配音：复用 `service/video/tts.py` 的 TTS 后端 + **同一把跨进程配额**。

三个复用点，每个都有明确理由：

1. **TTS 后端**：qwen TTS 已配好且实测可用（5~12s/段），`synthesize_speech()` 自带
   429 重试与"代理把 429 包成 502"的判据（`service/video/tts.py`），没必要重写。
2. **跨进程配额**：上游 tts_proxy 硬限同时 2 个请求，而生产是 `--workers 4`。两条视频
   链路（表单页 + 对话窗口）必须共用**同一把 Redis 信号量**（key `rz:video:tts:slots`）。
   这里刻意 import 旧模块的私有 helper 取**同一个实例**：复制实现必漂移，而那段
   "排队要一直等、只对 Redis 故障 fail-open、租约续期"的语义是踩过坑写下的
   （见 `_acquire_tts_slot` docstring：早期 60s 放行导致 50 页成批涌向上游、429 成灾）。
3. **本地并发闸**：`_get_tts_local_slots()` 限制本进程在飞数，避免几十页一次性堆成
   几十个等待者。

下游要**本地音频文件**给 ffmpeg：qwen 返回的是远程 URL（tts_proxy 的对象存储），
这里负责下载到任务目录；openai 后端返回的本来就是本地路径。
"""

import asyncio
import logging
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_AUDIO_EXTS = ('.mp3', '.wav', '.m4a')


class AudioError(Exception):
  """配音失败。"""


def _quota():
  """取旧模块的配额对象与取/放槽位 helper（同一实例、同一 key）。

  取不到（旧模块被删/改名/依赖异常）时返回 (None, None, None) → 调用方按 fail-open 走。
  """
  try:
    from service.video.pipeline import (  # noqa: PLC0415 - 延迟导入避免循环
      _acquire_tts_slot, _get_tts_local_slots, _get_tts_slots,
    )
    return _get_tts_slots(), _get_tts_local_slots(), _acquire_tts_slot
  except Exception as e:  # noqa: BLE001 - 配额不可用不该拖垮配音
    logger.warning('TTS 配额不可用，降级为不限流: %s', e)
    return None, None, None


async def _download(url: str, dest: Path) -> Path:
  dest.parent.mkdir(parents=True, exist_ok=True)
  async with httpx.AsyncClient(timeout=120) as client:
    resp = await client.get(url)
    resp.raise_for_status()
    dest.write_bytes(resp.content)
  return dest


async def _probe_duration(path: Path) -> float:
  """用 ffprobe 读音频时长（TTS 未给 duration 时的兜底）。"""
  try:
    proc = await asyncio.create_subprocess_exec(
      'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
      '-of', 'default=noprint_wrappers=1:nokey=1', str(path),
      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return max(0.0, float((out or b'0').strip() or 0))
  except Exception:  # noqa: BLE001 - 探不到返回 0，合成时按默认时长处理
    return 0.0


async def synthesize_page(index: int, script: str, voice: str, work: Path,
                          speech_language: str = 'zh-CN') -> dict:
  """合成一页音频，返回 `{audio, duration}`（本地文件路径 + 秒数）。

  Args:
    speech_language: 配音语言（`zh-CN` / `en-US`），由 narration.speech_language() 归一化。
      **必须与文案语言一致**——TTS 不做翻译，拿中文文案按英文合成只会读出怪音。

  - 解说词为空 → 直接返回空音频（该页按静音短页处理，不算失败）
  - 失败 → 抛 AudioError，由编排层决定降级还是整单失败
  """
  if not script.strip():
    return {'audio': '', 'duration': 0.0}

  from service.video.tts import synthesize_speech

  slots, local_slots, acquire = _quota()
  if local_slots is None:
    local_slots = _NullLocal()
  async with local_slots:
    held = await acquire(slots) if acquire else False
    try:
      path_or_url, duration = await synthesize_speech(script, voice, speech_language)
    finally:
      if held and slots is not None:
        try:
          await slots.release()
        except Exception as e:  # noqa: BLE001
          logger.warning('TTS 释放槽位异常: %s', e)

  if not path_or_url:
    raise AudioError(f'第 {index + 1} 页未返回音频')

  if str(path_or_url).startswith('http'):
    ext = next((e for e in _AUDIO_EXTS if str(path_or_url).lower().endswith(e)), '.mp3')
    local = await _download(str(path_or_url), work / 'audio' / f'page_{index + 1:03d}{ext}')
  else:
    local = Path(str(path_or_url))
    if not local.is_file():
      raise AudioError(f'第 {index + 1} 页音频文件不存在: {local}')

  dur = float(duration or 0)
  if dur <= 0:
    dur = await _probe_duration(local)
  return {'audio': str(local), 'duration': dur}


class _NullLocal:
  """配额不可用时的空实现（async context manager，什么都不做）。"""

  async def __aenter__(self):
    return self

  async def __aexit__(self, *exc):
    return False

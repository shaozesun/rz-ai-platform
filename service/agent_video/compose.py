"""用 ffmpeg 把「每页图片 + 每页音频」合成为 MP4。

为什么自己合成而不调 8030 渲染插件：那台实测 500 且不可控；而合成这一步是纯确定性
的 ffmpeg 调用（图片定长 + 音轨），放在后端进程里做，产物直接落任务目录。

分两步（每页一段 → concat）而不是一条复杂 filtergraph：
- 每页单独编码，**改一页只需重编该页再 concat**（增量重跑的关键）
- 出错时能定位到具体页（filtergraph 一次性编码出错只给一句模糊报错）
"""

import asyncio
import logging
import shutil
from pathlib import Path

from config.settings import settings

from . import subtitles

logger = logging.getLogger(__name__)

# 目标分辨率（按画幅）；图片按比例缩放后居中留白，避免拉伸变形
RESOLUTIONS = {
  '16:9': (1920, 1080),
  '9:16': (1080, 1920),
  '1:1': (1080, 1080),
  '4:3': (1440, 1080),
}
DEFAULT_PAGE_SECONDS = 3.0   # 无音频页（解说词降级）的静音时长

# 静图动效（Ken Burns）：让静态页图缓慢推近/平移，避免整片"照片幻灯片"感。
# 幅度刻意很小（≈1.08 倍、整段匀速）——讲解视频以内容可读为先，动效只是"活着"。
# 为什么必须先放大再 zoompan：zoompan 直接在原尺寸上做运镜会因亚像素采样而抖动、边缘出黑边，
# 这是该滤镜的已知坑；先 scale 到 2 倍再 zoompan 到目标尺寸可消除。
MOTIONS = ('none', 'zoom_in', 'zoom_out', 'pan_left', 'pan_right', 'mixed')
_ZOOM_MAX = 1.08
_FADE_SECONDS = 0.4          # 片头淡入 / 片尾淡出（只在首末段，页间不做：会忽明忽暗）


def motion_for(motion: str, page_index: int) -> str:
  """决定第 page_index（1 起）页用哪种运镜。

  `mixed` 按页交替（推近/拉远/左右平移四选一轮换），避免整片同一种动效显得呆板；
  非 mixed 时原样返回（未知值退回 none，保证不会因参数写错而失败）。
  """
  m = (motion or settings.VIDEO_AGENT_MOTION or 'none').strip().lower()
  if m not in MOTIONS:
    logger.warning('未知的 motion=%r，退回 none', motion)
    m = 'none'
  if m == 'mixed':
    rotation = ('zoom_in', 'pan_right', 'zoom_out', 'pan_left')
    return rotation[(page_index - 1) % len(rotation)]
  return m


def _motion_filter(motion: str, w: int, h: int, frames: int) -> str:
  """单段运镜滤镜链（输入已 scale 到 w*2 宽）。

  zoompan 的 z/x/y 用的是**放大后**画布坐标：z 从 1 到 _ZOOM_MAX，x/y 保持画面中心，
  平移则让 x 或 y 从一端匀移到另一端。
  """
  if motion == 'none':
    return f'scale={w}:{h}'

  z_in = f"min(1+{(_ZOOM_MAX - 1) / max(1, frames)}*on,{_ZOOM_MAX})"
  z_out = f"max({_ZOOM_MAX}-{(_ZOOM_MAX - 1) / max(1, frames)}*on,1)"
  cx, cy = 'iw/2-(iw/zoom/2)', 'ih/2-(ih/zoom/2)'
  if motion == 'zoom_in':
    z, x, y = z_in, cx, cy
  elif motion == 'zoom_out':
    z, x, y = z_out, cx, cy
  elif motion == 'pan_left':
    z = str(_ZOOM_MAX)
    x, y = f'(iw-iw/zoom)*(1-on/{max(1, frames)})', cy
  else:  # pan_right
    z = str(_ZOOM_MAX)
    x, y = f'(iw-iw/zoom)*(on/{max(1, frames)})', cy
  return (
    f'scale={w * 2}:{h * 2}:force_original_aspect_ratio=decrease,'
    f'pad={w * 2}:{h * 2}:(ow-iw)/2:(oh-ih)/2:color=white,'
    f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps=25,setsar=1"
  )


class ComposeError(Exception):
  """视频合成失败。"""


def _subtitle_font() -> str:
  """字幕用的中文字体**文件路径**（drawtext 不吃字体名，也不走 fontconfig）。

  复用 `text_pages.require_font()`：它是"字体可用"这套判定的单一入口（校验 + 缓存），
  缺字体时给出可操作原因，而不是让整段视频静默没有字幕。
  """
  from . import text_pages

  try:
    return text_pages.require_font()
  except text_pages.CjkFontError as e:
    raise ComposeError(f'烧字幕需要中文字体：{e}') from e


def ffmpeg_available() -> bool:
  return shutil.which('ffmpeg') is not None


async def _run(cmd: list[str], timeout: int) -> None:
  proc = await asyncio.create_subprocess_exec(
    *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
  )
  try:
    _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
  except asyncio.TimeoutError:
    proc.kill()
    raise ComposeError(f'ffmpeg 超时（>{timeout}s）') from None
  if proc.returncode != 0:
    tail = (stderr or b'')[-400:].decode('utf-8', 'replace')
    raise ComposeError(f'ffmpeg 失败 rc={proc.returncode}: {tail}')


async def probe_duration(path: str) -> float:
  """ffprobe 读媒体时长（秒）；失败返回 0。"""
  try:
    proc = await asyncio.create_subprocess_exec(
      'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
      '-of', 'default=noprint_wrappers=1:nokey=1', str(path),
      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    return max(0.0, float((out or b'0').strip() or 0))
  except Exception:  # noqa: BLE001
    return 0.0


async def compose_page(
  *,
  image: str,
  audio: str,
  out_path: Path,
  aspect_ratio: str = '16:9',
  page_seconds: float = DEFAULT_PAGE_SECONDS,
  motion: str = 'none',
  fade_in: bool = False,
  fade_out: bool = False,
  subtitle: str = '',
) -> float:
  """合成单页片段：图片铺满整段音频时长（无音频则用静音补足 page_seconds）。

  **显式按时长裁剪（`-t`）而不是靠 `-shortest`**：`-loop 1` 的图片输入与音频混流时
  `-shortest` 不可靠（实测两段都成了 4.04s），必须自己算时长。

  Args:
    motion: 运镜预设（见 MOTIONS）；`none` 即静态（与改动前行为一致）。
    fade_in/fade_out: 仅**片头/片尾**传 True（页间不做，避免忽明忽暗）。
    subtitle: 该页字幕文本（配音语言与字幕语言不同时，与解说稿不是同一份）；非空时烧到底部。

  Returns:
    该片段实际时长（秒）。
  """
  w, h = RESOLUTIONS.get(aspect_ratio, RESOLUTIONS['16:9'])
  out_path.parent.mkdir(parents=True, exist_ok=True)

  has_audio = bool(audio) and Path(audio).is_file()
  if has_audio:
    dur = await probe_duration(audio) or page_seconds
  else:
    dur = page_seconds

  frames = max(1, int(round(dur * 25)))
  vf = _motion_filter(motion, w, h, frames)
  # 时长太短（<2×fade）时按比例收缩，避免 fade 吃掉整段
  fade = min(_FADE_SECONDS, max(0.0, dur / 3))
  if fade > 0.05:
    if fade_in:
      vf += f',fade=t=in:st=0:d={fade:.2f}'
    if fade_out:
      vf += f',fade=t=out:st={max(0.0, dur - fade):.2f}:d={fade:.2f}'

  # 字幕放最后：不参与上面的淡入淡出（片头整体变黑时字幕仍要看得清）
  if settings.VIDEO_AGENT_SUBTITLE and (subtitle or '').strip():
    vf += ',' + subtitles.drawtext_filters(
      subtitle, dur, w, h, _subtitle_font(), out_path.parent / f'{out_path.stem}.subs',
    )

  cmd = ['ffmpeg', '-y', '-loglevel', 'error', '-loop', '1', '-i', image]
  if has_audio:
    cmd += ['-i', audio]
  else:
    # 无音频页：生成等长静音轨，保证每页时长稳定、concat 不塌陷
    cmd += ['-f', 'lavfi', '-i', f'anullsrc=channel_layout=stereo:sample_rate=44100']
  cmd += [
    '-t', f'{dur:.3f}',
    # crf 28：画面基本静止（只有轻微运镜），默认 23 太浪费——实测 50 页成片到 150MB，
    # 28 在肉眼看不出差别的前提下体积能砍一半以上（浏览器播放也更顺）。
    '-c:v', 'libx264', '-crf', '28', '-pix_fmt', 'yuv420p',
    '-r', '25', '-vf', vf,
    # 语音用 64k 单声道足够（成片可能 20+ 分钟，128k 立体声纯属浪费 ≈ 每分钟多 0.5MB）
    '-c:a', 'aac', '-b:a', '64k', '-ac', '1', '-ar', '44100',
    # 音轨略短于 -t 时补静音，避免末字被切
    '-af', 'apad',
    str(out_path),
  ]
  # -tune stillimage 只在静止画面下有效（会压掉运动细节）；有运镜时不能用
  if motion == 'none':
    cmd.insert(cmd.index('-c:v'), '-tune')
    cmd.insert(cmd.index('-tune') + 1, 'stillimage')
  await _run(cmd, settings.VIDEO_AGENT_FFMPEG_TIMEOUT)
  return dur


async def concat_segments(segments: list[str], out_path: Path) -> None:
  """按顺序拼接片段（concat demuxer + 直接复制流，不重新编码）。"""
  if not segments:
    raise ComposeError('没有可拼接的片段')
  out_path.parent.mkdir(parents=True, exist_ok=True)
  list_file = out_path.parent / 'segments.txt'
  list_file.write_text(
    '\n'.join(f"file '{Path(s).resolve()}'" for s in segments), encoding='utf-8',
  )
  await _run(
    ['ffmpeg', '-y', '-loglevel', 'error',
     # genpts：片段各自时间基不同，直接 copy 会得到错乱的时长（实测 4.5s 拼成 8.08s）
     '-fflags', '+genpts',
     '-f', 'concat', '-safe', '0', '-i', str(list_file),
     '-c', 'copy',
     # faststart：moov 前置，浏览器可边下边播（否则要下完整个文件才播）
     '-movflags', '+faststart',
     str(out_path)],
    settings.VIDEO_AGENT_FFMPEG_TIMEOUT,
  )


def total_duration(pages: list[dict]) -> float:
  """按每页音频时长累计总时长（无音频页按默认秒数）。"""
  total = 0.0
  for p in pages:
    dur = float(p.get('duration') or 0)
    total += dur if dur > 0 else DEFAULT_PAGE_SECONDS
  return round(total, 2)

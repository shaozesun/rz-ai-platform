"""把每页解说词烧成画面底部字幕（ffmpeg `drawtext`）。

**为什么用 drawtext 而不是 `subtitles=`（libass）**：
1. `subtitles`/`ass` 滤镜依赖 libass——**本机 ffmpeg 没编 libass**（实测只有 drawtext），
   用 libass 会让"本地能跑/线上能跑"分叉，而这条链路的开发机就是 macOS；
2. libass 靠 fontconfig 找字体，等于把"LibreOffice 看不见中文字体"那个坑原样引回来。
`drawtext` 走 `fontfile=` 直接吃**字体文件路径**，两端行为一致，且正好复用
`settings.RZ_CJK_FONT`（与文字大纲排版、数据沙箱同一套字体）。

**字幕文本一律走 `textfile=`，不内联进滤镜字符串**：解说词是任意文本，里面有 ASCII
撇号时（如 `'旁路'`）会把 filtergraph 的引号状态搞乱——实测直接 `No such filter` 报错。
写文件让它当**数据**读，撇号/`%`/`:`/反斜杠/括号/分号/多行全都不需要转义。

字幕在**每页片段内**烧好（见 compose.compose_page），因此天然兼容增量重跑：
改一页只重编该页，字幕跟着该页解说词一起更新。
"""

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_CUE_CHARS = 42      # 单条字幕上限；超过就按标点再切（一屏塞太多读不过来）
MIN_CUE_CHARS = 8       # 太短的句子往后并，避免"大家好。"这种一闪而过的字幕
# 句末：中文标点直接切；ASCII 的 .!?; 要**后面跟空白 + 大写字母/引号/结尾**才算句末。
# 为什么加后面这个条件：英文里 `.` 更常见的身份是小数点或缩写点——`3.5`、`v1.2`、
# `e.g.` 全都不该断句（实测 `e.g.` 会被切成 `e.` / `g.`）。
_SENT_END = re.compile(r'(?<=[。！？；…])|(?<=[.!?;])(?=\s+[A-Z“"(\[]|\s*$)')
# 不该出现在行首的收尾符号（折行时若只剩它一个，宁可让本行稍微超宽）
_CLOSERS = '。，、；：！？）》」』】”’%…'
_CLAUSE = re.compile(r'(?<=[，、])|(?<=[,;:])(?=\s)')   # 长句再按逗号/顿号切（同理只认英文逗号+空白）


def _join(a: str, b: str) -> str:
  """拼两条字幕：两侧都是 ASCII 就补一个空格（拉丁文本靠空格分词；中文不需要，补了难看）。"""
  if a[-1:].isascii() and not a.endswith(' ') and b[:1].isascii() and not b.startswith(' '):
    return f'{a} {b}'
  return a + b


def _cut(text: str, limit: int) -> list[str]:
  """把超长文本（无可用标点）切成 ≤limit 的片段，**优先在空格处断**。

  为什么不直接 `text[i:i+limit]`：英文硬切会切进单词中间——实测出过
  `throu|gh`、`bypa|ss`、`lo|ad` 这种字幕，一眼就是坏的。
  """
  parts: list[str] = []
  while len(text) > limit:
    pos = text.rfind(' ', 0, limit + 1)
    if pos <= 0:                       # 没有可用空格（中文/超长英文单词）→ 只能硬切
      pos = limit
    parts.append(text[:pos].strip())
    text = text[pos:].lstrip()
  if text:
    parts.append(text)
  return parts


def split_cues(script: str, limit: int = MAX_CUE_CHARS) -> list[str]:
  """解说词 → 字幕条（按标点切、长句二次切、超长硬切、短句相邻合并）。中英通用。"""
  text = ' '.join((script or '').split())   # 顺带把换行/多空格压平，字幕里不需要
  if not text:
    return []

  units: list[str] = []
  for sentence in (p.strip() for p in _SENT_END.split(text) if p.strip()):
    if len(sentence) <= limit:
      units.append(sentence)
      continue
    buf = ''
    for clause in (c for c in _CLAUSE.split(sentence) if c):
      if len(clause) > limit:
        if buf:
          units.append(buf)
          buf = ''
        units.extend(_cut(clause, limit))
      elif len(buf) + len(clause) <= limit:
        buf += clause
      else:
        units.append(buf)
        buf = clause
    if buf:
      units.append(buf)

  cues: list[str] = []
  for unit in units:
    # 上一条太短就往里并（"大家好。"这种一闪而过的字幕没意义），但仍受上限约束
    if cues and len(cues[-1]) < MIN_CUE_CHARS and len(cues[-1]) + len(unit) <= limit:
      cues[-1] = _join(cues[-1], unit)
    else:
      cues.append(unit)
  return cues


def cue_timeline(cues: list[str], duration: float) -> list[tuple[str, float, float]]:
  """按**字数占比**把整段时长分给各条字幕，返回 `(文本, 起, 止)`。

  为什么按字数而不是均分：语音是匀速念的，字数占比就是最省事且够准的同步依据
  （均分会让"短句早退场、长句被截断"，一眼能看出不同步）。
  """
  duration = max(0.1, float(duration or 0))
  total_chars = sum(len(c) for c in cues) or 1
  out: list[tuple[str, float, float]] = []
  t = 0.0
  for cue in cues:
    span = duration * len(cue) / total_chars
    out.append((cue, t, min(duration, t + span)))
    t += span
  return out


def _char_width(ch: str) -> float:
  """字符宽度（以字号为单位）：CJK/全角约 1.0，拉丁/数字约 0.5，空格约 0.3。

  为什么按宽度而不是按字符数折行：同样 24 个字符，中文占满一行、英文只占半行
  （实测 `Welcome everyone. Today` 就折了，右边全空），按宽度算中英才都合理。
  """
  if ch == ' ':
    return 0.3
  return 0.5 if ord(ch) < 0x2E80 else 1.0


def _wrap(text: str, max_width: float) -> list[str]:
  """按行宽折行；英文整词不拆（超长单词才退回逐字切）。"""
  rows, cur, cur_w = [], '', 0.0
  for token in re.findall(r'[A-Za-z0-9]+|\s+|.', text):
    tw = sum(_char_width(c) for c in token)
    # 收尾标点不换行：否则会出现只挂着一个「。」的第二行（超一点宽度比这好看）
    if cur.strip() and cur_w + tw > max_width and not (len(token) == 1 and token in _CLOSERS):
      rows.append(cur.rstrip())
      cur, cur_w = ('' if token.isspace() else token), (0.0 if token.isspace() else tw)
      continue
    if tw > max_width:            # 单个超长词（长型号/URL）→ 逐字断，别让它溢出画面
      for ch in token:
        if cur_w + _char_width(ch) > max_width and cur.strip():
          rows.append(cur.rstrip())
          cur, cur_w = '', 0.0
        cur += ch
        cur_w += _char_width(ch)
      continue
    cur += token
    cur_w += tw
  if cur.strip():
    rows.append(cur.rstrip())
  return rows or ['']


def _esc(text: str) -> str:
  """filtergraph 单引号值内只需转义反斜杠与单引号（配 `expansion=none` 免掉 `%` 展开）。"""
  return text.replace('\\', '\\\\').replace("'", "\\'")


def drawtext_filters(
  script: str, duration: float, w: int, h: int, font: str, cue_dir: Path,
) -> str:
  """生成烧字幕用的 drawtext 片段（逗号分隔，直接拼进 `-vf`）；无解说词返回空串。

  Args:
    font: 中文字体**文件路径**（`settings.RZ_CJK_FONT`）——drawtext 不吃字体名。
    cue_dir: 存放字幕文本文件的目录（**每页一个**，各自独立，免得并行时互相覆盖）。
  """
  cues = cue_timeline(split_cues(script), duration)
  if not cues:
    return ''

  # 字号：0.030h（1080p ≈32px，接近电视字幕；原 0.040h 偏大、挡画面）
  size = max(16, int(h * 0.030))
  margin = int(h * 0.05)
  # 行宽上限按**字号倍**算：一屏最多 24 个汉字宽（≈48 个拉丁字符），再宽读着累
  max_width = max(8.0, min(24.0, w * 0.80 / size))
  # 描边而不是底框：原来的 `box=1:boxcolor=black@0.55` 是个半透明黑底框（用户嫌难看）；
  # 但纯白字在**白底页**（文本路径的页图就是白底）会看不见，所以留一圈极细深色描边——
  # 白底、深色 PPT、图片页都读得清，且不是"框"
  border_w = max(2, size // 16)
  cue_dir.mkdir(parents=True, exist_ok=True)

  parts: list[str] = []
  for i, (text, start, end) in enumerate(cues):
    path = cue_dir / f'cue_{i:03d}.txt'
    # 折行交给 drawtext 的多行渲染（y 用 h-text_h 贴底，多行会自动往上撑）
    path.write_text('\n'.join(_wrap(text, max_width)), encoding='utf-8')
    parts.append(
      f"drawtext=fontfile='{_esc(font)}':textfile='{_esc(str(path))}':expansion=none"
      f':fontsize={size}:fontcolor=white'
      f':borderw={border_w}:bordercolor=black@0.55:line_spacing={max(4, size // 4)}'
      f':x=(w-text_w)/2:y=h-text_h-{margin}'
      f":enable='between(t,{start:.2f},{end:.2f})'"
    )
  logger.debug('字幕 %d 条，文本目录 %s', len(cues), cue_dir)
  return ','.join(parts)

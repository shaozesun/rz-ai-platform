"""把纯文字/大纲排版成"页图"（没有 PPT 时的输入形态）。

为什么用 Pillow 而不是 HTML→截图：无浏览器/Node 依赖（镜像已经很大了），排版需求也简单
（标题 + 要点列表 + 页码），确定性好、几十毫秒一页。字体用 `settings.RZ_CJK_FONT`
（与数据沙箱同一套中文字体）：**缺字体直接报错**（`require_font()`），不退回默认字体——
否则汉字会静默变成方框/空白，成片看着"没有中文"却查不出原因。

**这一版的核心是"一个字都不能丢"**（用户原话：截断等于失败）。旧实现有三处静默截断：
超长条目被砍成 120 字、垂直放不下就画个 `…` 收尾、标题被 `[:40]` 截断——成片看着"空洞"，
实际上内容已经没了。现在改成：

1. **量真实像素宽折行**（`draw.textlength`），不做字符数估算、不设上限、不加省略号；
2. **自适应字号**：内容多就把字号压小（下限 `FONT_MIN_RATIO`），内容少就放大（上限
   `FONT_MAX_RATIO`）并把余量摊开，稀疏页不再大半空白；
3. **装不下就提前拆页**——拆页必须发生在**这里**（`plan_pages`/`split_pages`），不能等到渲染：
   页图、逐页解说词、字幕是**一一对应**的（`pipeline.run_task` 里 `render_pages(page_texts)` 与
   `generate_scripts(page_texts)` 同源），渲染时才拆会让解说词整页错位；
4. **自检**：规划出的每页内容 `_verify()` 必须与源文本逐字对应（去掉空白/标记后比较），
   对不上就抛 `LayoutError` → 任务失败，而不是交付残缺视频。

产出与 PPT 路径**完全同构**：一批 PNG 页图交给 pipeline 的下游（逐页解说词 → TTS → 合成），
所以文本任务天然享有增量重跑、RITL 自检、二次修改等全部能力。
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

THEMES = {
  'deep_blue': {'bg': (255, 255, 255), 'title_bg': (31, 78, 121), 'title_fg': (255, 255, 255),
                'text': (34, 34, 34), 'bullet': (31, 78, 121), 'footer': (140, 140, 140)},
  'dark': {'bg': (24, 26, 30), 'title_bg': (52, 58, 70), 'title_fg': (240, 240, 240),
           'text': (226, 226, 226), 'bullet': (120, 170, 255), 'footer': (140, 140, 140)},
}

RESOLUTIONS = {'16:9': (1920, 1080), '9:16': (1080, 1920), '1:1': (1080, 1080),
               '4:3': (1440, 1080)}

# 正文字号（按画布高度取比例）：上限给"内容很少"的页，下限给"内容很多"的页——
# 到了下限还装不下就拆页，**绝不缩到看不清、也绝不截断**
FONT_MAX_RATIO = 0.055
FONT_MIN_RATIO = 0.026
FONT_ABS_MIN = 18          # 绝对下限（像素），防止极端画幅下算出不可读的字号
LINE_SPACING = 1.45        # 行距 = 字号 × 该系数（旧实现固定 0.070h≈1.84 倍，太散）
BULLET_GAP_RATIO = 0.45    # 条目之间的额外间距（× 行距）
PARA_GAP_RATIO = 0.85      # 段落块之间的额外间距（× 行距）
MARGIN_RATIO = 0.08        # 左右页边距
FOOTER_RATIO = 0.09        # 页脚预留高度
BAR_MIN_RATIO = 0.16       # 标题条最小高度（封面更高，见 _bar_layout）
BAR_MAX_RATIO = 0.34       # 标题条最大高度；标题再长就降级成正文块，而不是截断
CONT_SUFFIX = '（续）'      # 同一节被拆到多页时，后续页标题的后缀

_BULLET_RE = re.compile(
  r'^(?:[-*•·]|\d+[.、)]|[一二三四五六七八九十]+[、.]'
  r'|(?:step|步骤|第)\s*\d+\s*(?:[:：.、步]|\s))\s*', re.I,
)
_MD_HEAD = re.compile(r'^#{1,6}\s*')
# 注意用**非捕获组**：re.split 会把捕获组的内容也返回，分隔线会各自变成一页
_SEP_RE = re.compile(r'^\s*(?:---+|\*\*\*+)\s*$', re.M)
_SENT_SPLIT = re.compile(r'(?<=[。！？!?；;…])')


class LayoutError(ValueError):
  """排版无法在给定画幅内无损完成（内容超上限 / 内部不一致）。"""


@dataclass
class Block:
  """页内一个内容块：`bullet` 带圆点，`para` 整段全宽（旧实现把整段画成一个大圆点）。"""

  kind: str
  text: str


@dataclass
class PagePlan:
  """一页的规划结果；`text` 是交给下游（解说词/字幕/渲染）的页文本。"""

  text: str
  heading: str
  blocks: list[Block]
  continued: bool = False


def _norm(text: str) -> str:
  """比较用的归一化：去掉空白与 Markdown/项目符号标记（只用于比对，不用于输出）。"""
  return re.sub(r'[\s#>•·*\-—–]+', '', text or '')


class CjkFontError(Exception):
  """中文字体不可用（文件缺失或 Pillow 打不开）。"""


_FONT_OK = False


def require_font() -> str:
  """确认中文字体真的能加载，返回路径；不行就抛 `CjkFontError`（进程内只验一次）。

  为什么不再退回 `ImageFont.load_default()`：Pillow 缺中文字体**不报错**，只是把汉字画成
  方框/空白——用户会拿到一整段没有汉字的成片，而日志里只有一条 warning。这与"缺字体就
  明确失败、给可操作原因"的口径一致（PPT 路径的 `cjk_render_available()` 同理）。
  """
  global _FONT_OK
  if not _FONT_OK:
    from PIL import ImageFont
    try:
      ImageFont.truetype(settings.RZ_CJK_FONT, 32)
    except Exception as e:  # noqa: BLE001
      raise CjkFontError(
        f'运行环境缺少中文字体文件（{settings.RZ_CJK_FONT}）：'
        'assets/fonts/wqy-microhei.ttc 需提交进 git 并重建镜像，或联系管理员'
      ) from e
    _FONT_OK = True
  return settings.RZ_CJK_FONT


# ── 折行（量真实像素宽）─────────────────────────────────────────────────

class _Measurer:
  """按字号缓存 token 宽度，避免逐字符重复量（一页要量上千次）。"""

  def __init__(self, draw):
    self._draw = draw
    self._cache: dict[tuple[int, str], float] = {}

  def width(self, text: str, font) -> float:
    return self._draw.textlength(text, font=font)

  def token(self, token: str, font) -> float:
    key = (font.size, token)
    w = self._cache.get(key)
    if w is None:
      w = self._draw.textlength(token, font=font)
      self._cache[key] = w
    return w


def _wrap(text: str, font, max_w: float, m: _Measurer) -> list[str]:
  """按像素宽折行；**不截断、不加省略号**，单 token 超宽才按字符硬切。

  用一个不变量兜底：切出来的各行拼起来（去掉空白）必须等于原文——切错了会当场发现。
  """
  rows: list[str] = []
  cur = ''
  cur_w = 0.0
  for token in re.findall(r'[A-Za-z0-9]+|\s+|.', text):
    if token.isspace() and not cur:
      continue
    tw = m.token(token, font) if token.strip() else m.token(' ', font) * len(token)
    if cur and cur_w + tw > max_w:
      rows.append(cur.rstrip())
      cur, cur_w = '', 0.0
      if token.isspace():
        continue
    if not cur and tw > max_w:            # 单个超长 token（长串型号/URL）→ 逐字切
      piece = ''
      piece_w = 0.0
      for ch in token:
        cw = m.token(ch, font)
        if piece and piece_w + cw > max_w:
          rows.append(piece)
          piece, piece_w = ch, cw
        else:
          piece += ch
          piece_w += cw
      cur, cur_w = piece, piece_w
      continue
    cur += token
    cur_w += tw
  if cur.strip() or not rows:
    rows.append(cur.rstrip())
  return rows


# ── 块模型 ──────────────────────────────────────────────────────────────

def _bullet_body(line: str) -> str | None:
  """命中项目符号则返回"去掉标记后的正文"；**纯符号标记丢掉，序号保留**。

  为什么保留序号：`- `、`• ` 这类标记只是视觉符号（画面上另有圆点），丢掉不算丢内容；
  但 `3.`、`Step 1:`、`第 2 步` 是**信息本身**——统一替换成 `- ` 会让"Step 1:"凭空消失
  （分页自检当场就抓到了这个 bug）。
  """
  match = _BULLET_RE.match(line)
  if not match:
    return None
  marker = match.group(0).strip()
  rest = line[match.end():].strip()
  keep = re.sub(r'^[-*•·\s]+$', '', marker)     # 纯符号 → 丢掉；数字/Step/第… → 保留
  return f'{keep} {rest}'.strip() if keep else rest


def _blocks(page_text: str) -> tuple[str, list[Block]]:
  """页文本 → (标题, 内容块)；首个非空行当标题，其余按有无项目符号分 bullet/para。

  连续的非符号行合并成**一个段落块**（按需折行），而不是每行一个块——否则英文段落会被
  打成一堆碎片、块间距也把版面撑散。
  """
  lines = [ln.rstrip() for ln in (page_text or '').splitlines() if ln.strip()]
  if not lines:
    return '', []
  # 首行**命中条目标记时不算标题**（那种输入整段都是条目，没有标题）——否则第一条会被当成
  # 标题、并在该节每一页重复出现（实测「200 条要点」多出 105 字，分页自检抓到的）
  blocks: list[Block] = []
  para: list[str] = []
  if _bullet_body(lines[0]) is None:
    heading = _MD_HEAD.sub('', lines[0]).strip()
    rest_lines = lines[1:]
  else:
    heading, rest_lines = '', lines
  for line in rest_lines:
    text = line.strip()
    body = _bullet_body(text)
    if body is not None:
      if para:
        blocks.append(Block('para', _join_para(para)))
        para = []
      blocks.append(Block('bullet', body))
    else:
      para.append(_MD_HEAD.sub('', text).strip())
  if para:
    blocks.append(Block('para', _join_para(para)))
  return heading, blocks


def _join_para(lines: list[str]) -> str:
  """把多行合成一段：中文之间不加空格，涉及拉丁字符时补空格。"""
  out = ''
  for line in lines:
    if not out:
      out = line
      continue
    need_space = (out[-1:].isascii() and out[-1:].strip() and
                  line[:1].isascii() and line[:1].strip())
    out += (' ' if need_space else '') + line
  return out


def _block_line(block: Block) -> str:
  return f'- {block.text}' if block.kind == 'bullet' else block.text


def _page_text(heading: str, blocks: list[Block], continued: bool) -> str:
  head = f'{heading}{CONT_SUFFIX}' if (continued and heading) else heading
  return '\n'.join([head] + [_block_line(b) for b in blocks]).strip()


# ── 版面与适配 ──────────────────────────────────────────────────────────

@dataclass
class _Geom:
  w: int
  h: int
  x0: int
  body_top: int
  body_w: int
  avail_h: float
  bullet_w: float
  bar_h: int
  bar_rows: list[str]
  bar_size: int
  demoted_heading: bool      # 标题太长时降级成正文块（保证不丢字）


def _font(size: int):
  from PIL import ImageFont
  return ImageFont.truetype(require_font(), size)


def _measure_draw():
  """只用于量文字宽度的 1×1 画布（不分配整页位图）。"""
  from PIL import Image, ImageDraw
  return ImageDraw.Draw(Image.new('RGB', (1, 1)))


def _bar_layout(heading: str, w: int, h: int, theme: str, cover: bool,
                m: _Measurer) -> tuple[list[str], int, int, bool]:
  """标题条排版：返回 (标题行, 字号, 条高, 是否降级进正文)。

  标题过长时**先缩字号**（下限 `FONT_ABS_MIN`），仍放不进上限高度就判为降级——
  由调用方把标题当成正文段落排版，**绝不截断**。
  """
  base = int(h * (0.32 if cover else BAR_MIN_RATIO))
  max_h = int(h * BAR_MAX_RATIO)
  size = int(h * (0.075 if cover else 0.045))
  while size >= FONT_ABS_MIN:
    font = _font(size)
    rows = _wrap(heading, font, w * 0.88, m) if heading else ['']
    bar_h = max(base, int(len(rows) * size * LINE_SPACING + size))
    if bar_h <= max_h:
      return rows, size, bar_h, False
    size = int(size * 0.85)
  return [heading], FONT_ABS_MIN, base, True


def _geom(heading: str, w: int, h: int, theme: str, cover: bool, m: _Measurer) -> _Geom:
  rows, size, bar_h, demoted = _bar_layout(heading, w, h, theme, cover, m)
  footer = int(h * FOOTER_RATIO)
  top = bar_h + int(h * 0.05)
  avail = max(0.0, h - footer - top)
  return _Geom(w=w, h=h, x0=int(w * MARGIN_RATIO), body_top=top,
               body_w=int(w * (1 - 2 * MARGIN_RATIO)), avail_h=avail,
               bullet_w=w * 0.03, bar_h=bar_h, bar_rows=rows, bar_size=size,
               demoted_heading=demoted)


@dataclass
class _Fit:
  size: int
  line_h: int
  rows: list[list[str]]       # 每个块折行后的行
  gaps: list[int]             # 每个块前的间距
  total_h: int


def _layout(blocks: list[Block], g: _Geom, size: int, m: _Measurer) -> _Fit:
  """给定字号算出版面（行、间距、总高）。"""
  line_h = max(1, round(size * LINE_SPACING))
  font = _font(size)
  rows: list[list[str]] = []
  gaps: list[int] = []
  total = 0.0
  for i, block in enumerate(blocks):
    avail_w = g.body_w - (g.bullet_w if block.kind == 'bullet' else 0)
    block_rows = _wrap(block.text, font, avail_w, m)
    gap = 0 if i == 0 else (BULLET_GAP_RATIO if block.kind == 'bullet'
                            else PARA_GAP_RATIO) * line_h
    rows.append(block_rows)
    gaps.append(int(gap))
    total += gap + len(block_rows) * line_h
  return _Fit(size=size, line_h=line_h, rows=rows, gaps=gaps, total_h=int(total))


def _fit(blocks: list[Block], g: _Geom, m: _Measurer) -> _Fit | None:
  """二分找"装得下"的最大字号；最小字号都装不下返回 None（由调用方拆页）。"""
  lo = max(FONT_ABS_MIN, int(g.h * FONT_MIN_RATIO))
  hi = max(lo, int(g.h * FONT_MAX_RATIO))
  best: _Fit | None = None
  while lo <= hi:
    mid = (lo + hi) // 2
    fit = _layout(blocks, g, mid, m)
    if fit.total_h <= g.avail_h:
      best = fit
      lo = mid + 1
    else:
      hi = mid - 1
  return best


def _verify(page_text: str, drawn: list[str], extra: str = '') -> None:
  """自检：这一页画出来的字必须与页文本逐字对应（去空白/标记后比较）。

  `extra` 是"画了但不属于页文本"的文字——只有封面会把视频标题画进标题条，
  期望值要把它算上，否则会误判；反过来，标题画了而正文本该有的行没画，这里照样会报错。

  这是"绝不截断"的最后一道闸：任何一处漏画/砍字都会当场抛错 → 任务失败，
  而不是悄悄交付一个残缺视频（用户明确：截断等于失败）。
  """
  if _norm(''.join(drawn)) != _norm(extra + page_text):
    raise LayoutError(
      f'排版自检失败（页面内容与源文本不一致，疑似漏画或截断）: '
      f'应为 {len(_norm(extra + page_text))} 字，实际画出 {len(_norm("".join(drawn)))} 字'
    )


# ── 分页规划 ────────────────────────────────────────────────────────────

def _sections(content: str) -> list[str]:
  """切成"节"：显式分隔线优先，其次 Markdown 标题，都没有就当一整节（由分页器按容量切）。"""
  text = (content or '').strip()
  if not text:
    return []
  if _SEP_RE.search(text):
    return [p.strip() for p in _SEP_RE.split(text) if p.strip()]
  if re.search(r'^#{1,6}\s', text, re.M):
    sections: list[str] = []
    cur: list[str] = []
    for line in text.splitlines():
      if re.match(r'^#{1,6}\s', line) and cur:
        sections.append('\n'.join(cur).strip())
        cur = [line]
      else:
        cur.append(line)
    if cur:
      sections.append('\n'.join(cur).strip())
    return [s for s in sections if s]
  return [text]


def _split_block(block: Block, g: _Geom, m: _Measurer) -> list[Block]:
  """把一个"单块就超容量"的块切成多块（按句子 → 再按行容量），每次切分都严格变短。

  **贪婪攒够容量再切**（而不是一次只吐一句）：2000 字的段落若一句一块会切出几百块，
  后面的分页每步都要重排一遍 → 组合爆炸（实测 3000 字要 >25s）。这里按句子累加宽度，
  攒到接近一页容量才切，块数少一个数量级。
  """
  out: list[Block] = []
  pending = block.text
  size = max(FONT_ABS_MIN, int(g.h * FONT_MIN_RATIO))
  font = _font(size)
  line_h = max(1, round(size * LINE_SPACING))
  avail_w = g.body_w - (g.bullet_w if block.kind == 'bullet' else 0)
  capacity_lines = max(1, int(g.avail_h // line_h))
  budget = capacity_lines * avail_w        # 一页大致能容纳的文字宽度（估算用，不必精确）
  while pending:
    rows = _wrap(pending, font, avail_w, m)
    if len(rows) <= capacity_lines:
      out.append(Block(block.kind, pending))
      break
    sentences = [s for s in _SENT_SPLIT.split(pending) if s]
    if len(sentences) > 1:
      take, used = '', 0.0
      for sentence in sentences:
        w = m.width(sentence, font)
        if take and used + w > budget:
          break
        take += sentence
        used += w
      if take and len(take) < len(pending):
        out.append(Block(block.kind, take.rstrip()))
        pending = pending[len(take):].lstrip()
        continue
    # 没有可切标点（或一句话就超容量）→ 按行容量硬切
    rows_keep = rows[:capacity_lines]
    cut = len(''.join(rows_keep).rstrip())
    if cut <= 0 or cut >= len(pending):
      cut = max(1, len(pending) // 2)
    out.append(Block(block.kind, pending[:cut].rstrip()))
    pending = pending[cut:].lstrip()
  return out


def _take_prefix(blocks: list[Block], g: _Geom, m: _Measurer) -> int:
  """这一页能完整装下的最长前缀块数（0 表示第一块自己就超容量 → 调用方拆块）。

  单遍增量累加（最小字号估容量）+ 少量 `_fit` 收敛：旧实现用二分前缀、每步重排全部块，
  块一多就是 O(块数²)。
  """
  size_min = max(FONT_ABS_MIN, int(g.h * FONT_MIN_RATIO))
  line_h = max(1, round(size_min * LINE_SPACING))
  font = _font(size_min)
  used = 0.0
  take = 0
  for i, block in enumerate(blocks):
    avail_w = g.body_w - (g.bullet_w if block.kind == 'bullet' else 0)
    rows = _wrap(block.text, font, avail_w, m)
    gap = 0 if i == 0 else (BULLET_GAP_RATIO if block.kind == 'bullet'
                            else PARA_GAP_RATIO) * line_h
    need = gap + len(rows) * line_h
    if used + need > g.avail_h:
      break
    used += need
    take += 1
  if take == 0:
    return 0
  while take > 1 and _fit(blocks[:take], g, m) is None:
    take -= 1                      # 估算偏乐观 → 回退到排得下的前缀
  # 估算偏保守（只按最小字号算容量）→ 再多装几块，让页面更饱满、页数更少
  while take < len(blocks) and _fit(blocks[:take + 1], g, m) is not None:
    take += 1
  return take if _fit(blocks[:take], g, m) is not None else 0


def _paginate(heading: str, blocks: list[Block], w: int, h: int, theme: str,
              cover: bool, m: _Measurer, max_pages: int) -> list[PagePlan]:
  """把一节内容切成若干页；放不下就切，切不动再拆块；页数超上限直接报错。"""
  pages: list[PagePlan] = []
  pending = list(blocks)
  first = True
  while pending or first:
    g = _geom(heading, w, h, theme, cover and first, m)
    if g.demoted_heading and heading:
      # 标题太长：降级进正文一起排（不截断），标题条留空。
      # **必须同时清掉 heading**——否则下一轮又判定"标题太长"再拼一次，无限累积（实测死循环）
      pending = [Block('para', heading)] + pending
      heading = ''
      heading_draw, g = '', _geom('', w, h, theme, cover and first, m)
    else:
      heading_draw = heading
    if not pending:
      # 只有标题、没有正文的节（如只有一行 `# 标题` 的封面）→ 就出一页标题页
      pages.append(PagePlan(text=heading_draw, heading=heading_draw, blocks=[],
                            continued=not first))
      break
    take = _take_prefix(pending, g, m)
    if take == 0:
      # 第一块自己就超容量 → 把它拆小（拆不动会抛错，不会死循环）
      head_block = pending[0]
      pieces = _split_block(head_block, g, m)
      if len(pieces) < 2:
        raise LayoutError(
          f'排版失败：单块内容在最小字号（{FONT_ABS_MIN}px）下仍装不进一页，'
          f'且无法继续拆分（内容：{head_block.text[:40]!r}…）'
        )
      pending = pieces + pending[1:]
      continue
    chunk, pending = pending[:take], pending[take:]
    pages.append(PagePlan(text=_page_text(heading_draw, chunk, not first),
                          heading=heading_draw, blocks=chunk, continued=not first))
    first = False
    if len(pages) > max_pages:
      raise LayoutError(
        f'内容过长：已排到 {max_pages} 页仍未结束，超过单份上限 {max_pages} 页；'
        f'请精简文字（或调高 settings.VIDEO_AGENT_MAX_PAGES）后重试'
      )
  return pages


def plan_pages(content: str, aspect_ratio: str = '16:9') -> list[PagePlan]:
  """文本 → 逐页规划（**唯一**的分页入口，见模块 docstring 的"为什么不能等到渲染再拆"）。"""
  w, h = RESOLUTIONS.get(aspect_ratio, RESOLUTIONS['16:9'])
  max_pages = max(1, int(settings.VIDEO_AGENT_MAX_PAGES))
  m = _Measurer(_measure_draw())
  plans: list[PagePlan] = []
  for section in _sections(content):
    heading, blocks = _blocks(section)
    if not blocks and not heading:
      continue
    plans.extend(_paginate(heading, blocks, w, h, 'deep_blue', len(plans) == 0, m,
                           max_pages - len(plans)))
  if not plans:
    return []
  # 整篇自检：所有页文本拼起来（去掉"（续）"标记）必须等于源文本——分页不许丢内容
  joined = _norm(''.join(p.text for p in plans).replace(CONT_SUFFIX, ''))
  source = _norm(content)
  if joined != source:
    raise LayoutError(
      f'分页自检失败：源文本 {len(source)} 字 vs 排版后 {len(joined)} 字（疑似切分丢字）'
    )
  return plans


def split_pages(content: str, aspect_ratio: str = '16:9') -> list[str]:
  """文本 → 每页一段文字（含标题行与 `- ` 项目符号），供下游逐页解说/渲染。"""
  return [p.text for p in plan_pages(content, aspect_ratio)]


# ── 渲染 ────────────────────────────────────────────────────────────────

def _render_one(index: int, total: int, page_text: str, title: str, aspect_ratio: str,
                theme: str, out_path: Path) -> str:
  from PIL import Image, ImageDraw

  w, h = RESOLUTIONS.get(aspect_ratio, RESOLUTIONS['16:9'])
  c = THEMES.get(theme, THEMES['deep_blue'])
  cover = index == 1 and bool(title)
  m = _Measurer(_measure_draw())

  heading, blocks = _blocks(page_text)
  bar_text = title if cover else heading

  # 封面：正标题画在标题条里；若页面自己的标题行与它不同，那行就照常进正文，**不丢**
  extra = ''
  if cover:
    same = heading and _norm(heading) == _norm(title)
    extra = title if not same else ''
    if heading and not same:
      blocks = [Block('para', heading)] + blocks
  elif not heading:
    bar_text = ''

  g = _geom(bar_text, w, h, theme, cover, m)
  if g.demoted_heading and bar_text:
    # 标题条放不下 → 降级进正文（绝不截断），标题条留空
    blocks = [Block('para', bar_text)] + blocks
    extra = ''
    g = _geom('', w, h, theme, cover, m)

  fit = _fit(blocks, g, m)
  if fit is None:
    raise LayoutError(
      f'第 {index} 页排版失败：内容在最小字号（{max(FONT_ABS_MIN, int(h * FONT_MIN_RATIO))}px）'
      f'下仍装不下（分页阶段本应提前拆页）'
    )

  img = Image.new('RGB', (w, h), c['bg'])
  d = ImageDraw.Draw(img)
  drawn: list[str] = []

  # 标题条
  if g.bar_rows and bar_text:
    d.rectangle([0, 0, w, g.bar_h], fill=c['title_bg'])
    bar_font = _font(g.bar_size)
    rows = g.bar_rows
    line_h = round(g.bar_size * LINE_SPACING)
    y0 = max(0, (g.bar_h - len(rows) * line_h) // 2)
    for i, row in enumerate(rows):
      tw = d.textlength(row, font=bar_font)
      d.text(((w - tw) / 2, y0 + i * line_h), row, font=bar_font, fill=c['title_fg'])
    # 标题条画的字全部计入自检（多行标题的每一行都要算）；
    # 封面上画的视频标题不属于页文本，用 `extra` 单独告诉自检（见 _verify）
    drawn.extend(rows)

  # 正文：把余量摊进间距，稀疏页不堆在顶部
  slack = max(0.0, g.avail_h - fit.total_h)
  extra_gap = min(fit.line_h * 1.5 - BULLET_GAP_RATIO * fit.line_h,
                  slack / max(1, len(blocks))) if slack else 0.0
  y = float(g.body_top) + min(slack * 0.25, fit.line_h)
  body_font = _font(fit.size)
  for block, rows, gap in zip(blocks, fit.rows, fit.gaps):
    y += gap + extra_gap
    x = g.x0
    if block.kind == 'bullet':
      dot = max(4, fit.size // 5)
      d.ellipse([x, y + fit.line_h * 0.36, x + dot, y + fit.line_h * 0.36 + dot],
                fill=c['bullet'])
      x = int(x + g.bullet_w)
    for row in rows:
      d.text((x, y), row, font=body_font, fill=c['text'])
      drawn.append(row)
      y += fit.line_h

  f_footer = _font(int(h * 0.026))
  d.text((int(w * MARGIN_RATIO), h - int(h * 0.06)), f'{index} / {total}',
         font=f_footer, fill=c['footer'])

  _verify(page_text, drawn, extra)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  img.save(out_path, format='PNG')
  return str(out_path)


def render_pages_sync(page_texts: list[str], work: Path, title: str,
                      aspect_ratio: str = '16:9', theme: str = 'deep_blue') -> list[str]:
  """同步渲染全部页图（在线程池里调）。"""
  out_dir = work / 'slides'
  total = len(page_texts)
  images = [
    _render_one(i, total, text, title, aspect_ratio, theme, out_dir / f'slide_{i:03d}.png')
    for i, text in enumerate(page_texts, start=1)
  ]
  logger.info('文字排版成 %d 页图（title=%r aspect=%s）', len(images), title, aspect_ratio)
  return images


async def render_pages(page_texts: list[str], work: Path, title: str,
                       aspect_ratio: str = '16:9', theme: str = 'deep_blue') -> list[str]:
  """把每页文字渲染成 PNG（放线程池，别阻塞事件循环）。"""
  if not page_texts:
    raise ValueError('没有可排版的内容（大纲为空）')
  if len(page_texts) > settings.VIDEO_AGENT_MAX_PAGES:
    raise ValueError(f'页数 {len(page_texts)} 超过上限 {settings.VIDEO_AGENT_MAX_PAGES} 页')
  return await asyncio.to_thread(
    render_pages_sync, page_texts, work, title, aspect_ratio, theme,
  )

"""RITL（Renderer-in-the-loop）自检：渲染出来的页面**看一眼再交付**。

为什么需要：agent 写解说词、LibreOffice 渲染、TTS 配音都是"盲操作"，出错时（页渲染
成空白、字体缺失导致方框/乱码、内容被裁掉）人眼一看就知道，但流水线不知道。业界
（Manim 那条线的 RITL：渲染→看画面→改，94% 渲染成功率）证明这一步是效果差异的关键。

两级，先便宜后昂贵：
1. **确定性检查**（Pillow，零成本）：全白/全黑页、尺寸异常 → 这些不用模型也能判
2. **视觉检查**（`model_gateway.vision()`）：把多页拼成一张 contact sheet **一次看多页**，
   问"标题是否被截断 / 内容缺失 / 乱码方框"，返回问题页号；只有命中才逐页复检（省调用）
"""

import json
import logging
import re
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

_BLANK_STDDEV = 1.5          # 灰度标准差低于此值视为"整页无内容"
_DARK_MEAN = 8.0             # 均值极低 → 全黑
_BRIGHT_MEAN = 250.0         # 均值极高 → 全白

_VISION_SYSTEM = (
  '你是视频质检员。用户会给你一张由多页 PPT 缩略图拼成的图（每格左上角标有页码）。'
  '**缩略图已缩小，细小文字本就模糊**——不要把"看不清细节"当成问题，只报**明显**缺陷：'
  '① 整页空白/几乎无内容；② 成片的 □□□ 方框（字体缺失）；③ 内容被硬切出页面边缘；'
  '④ 图片/图表区域明显损坏。\n'
  '只输出 JSON，不要解释，格式：{"issues": [{"page": 3, "reason": "整页空白"}]}；'
  '没有问题就输出 {"issues": []}。'
)

# 复检用：单页、原始分辨率，要求更严格
_VISION_RECHECK_SYSTEM = (
  '你是视频质检员。用户给你**单页 PPT 的原始分辨率截图**。请判断它是否存在以下缺陷：'
  '① 整页空白/几乎无内容；② 成片的 □□□ 方框（字体缺失）；③ 文字被裁切、溢出页面；'
  '④ 图片/图表损坏。**正常的一页就是正常的**，不要为了找问题而挑刺。\n'
  '只输出 JSON，格式：{"issue": {"problematic": true/false, "reason": "…"}}。'
)


def _load_font():
  """标 contact sheet 页码用的字体。

  这里**故意保留**默认字体兜底（与 `text_pages.require_font()` 的"缺字体就报错"不同）：
  它只画 `P3` 这种 ASCII 页码，`load_default()` 画得出来，而自检本身的价值是发现正文问题——
  为了一个装饰性标签让整轮质检失败是本末倒置。
  """
  from PIL import ImageFont
  try:
    return ImageFont.truetype(settings.RZ_CJK_FONT, 28)
  except Exception:  # noqa: BLE001 - 只画数字，兜底字体够用
    return ImageFont.load_default()


def deterministic_checks(images: list[str]) -> dict[int, dict]:
  """逐页做零成本检查；返回 `{页号(1-based): {ok, reason}}`（仅含判定结果的页）。"""
  from PIL import Image, ImageStat

  out: dict[int, dict] = {}
  for i, path in enumerate(images, start=1):
    try:
      with Image.open(path) as img:
        gray = img.convert('L')
        stat = ImageStat.Stat(gray)
        mean = float(stat.mean[0])
        stddev = float(stat.stddev[0])
      if stddev < _BLANK_STDDEV:
        reason = '整页近乎纯色（可能是空白页）'
        if mean <= _DARK_MEAN:
          reason = '整页纯黑（可能是渲染失败）'
        elif mean >= _BRIGHT_MEAN:
          reason = '整页纯白（可能是空白页）'
        out[i] = {'ok': False, 'reason': reason}
      else:
        out[i] = {'ok': True, 'reason': ''}
    except Exception as e:  # noqa: BLE001 - 读图失败也算问题页
      out[i] = {'ok': False, 'reason': f'无法读取该页图片: {e}'}
  return out


def build_contact_sheet(images: list[str], page_numbers: list[int], out_path: Path,
                        cols: int = 3, cell_w: int = 640) -> str:
  """把候选页拼成一张 contact sheet（每格左上角标页码），返回文件路径。"""
  from PIL import Image, ImageDraw

  subset = [(n, images[n - 1]) for n in page_numbers if 1 <= n <= len(images)]
  if not subset:
    return ''
  thumbs = []
  for n, path in subset:
    with Image.open(path) as img:
      ratio = cell_w / img.width
      thumbs.append((n, img.convert('RGB').resize((cell_w, max(1, int(img.height * ratio))))))

  cell_h = max(t.height for _, t in thumbs)
  rows = (len(thumbs) + cols - 1) // cols
  sheet = Image.new('RGB', (cols * cell_w, rows * cell_h), 'white')
  draw = ImageDraw.Draw(sheet)
  font = _load_font()
  for idx, (n, thumb) in enumerate(thumbs):
    x, y = (idx % cols) * cell_w, (idx // cols) * cell_h
    sheet.paste(thumb, (x, y))
    draw.rectangle([x, y, x + 96, y + 40], fill='black')
    draw.text((x + 8, y + 6), f'P{n}', fill='white', font=font)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  sheet.save(out_path, format='PNG')
  return str(out_path)


def _parse_issues(raw: str, valid: set[int]) -> list[dict]:
  """从视觉模型回复里宽松解析 issues（模型可能带 ```json 包裹或额外说明）。"""
  text = (raw or '').strip()
  m = re.search(r'\{.*\}', text, re.S)
  if not m:
    return []
  try:
    data = json.loads(m.group(0))
  except Exception:  # noqa: BLE001
    return []
  issues = data.get('issues') if isinstance(data, dict) else None
  if not isinstance(issues, list):
    return []
  out = []
  for it in issues:
    if not isinstance(it, dict):
      continue
    try:
      page = int(it.get('page'))
    except (TypeError, ValueError):
      continue
    if page in valid:
      out.append({'page': page, 'reason': str(it.get('reason') or '视觉检查发现问题')})
  return out


async def vision_check(images: list[str], page_numbers: list[int], work: Path) -> list[dict]:
  """对候选页做视觉检查（contact sheet 分批，避免一次塞太多图）。"""
  if not page_numbers:
    return []
  from core.model_gateway import model_gateway

  from .imaging import to_data_url

  per_sheet = max(1, settings.VIDEO_AGENT_RITL_SHEET_PAGES)
  issues: list[dict] = []
  for start in range(0, len(page_numbers), per_sheet):
    batch = page_numbers[start:start + per_sheet]
    sheet = build_contact_sheet(images, batch, work / 'checks' / f'sheet_{batch[0]}.png')
    if not sheet:
      continue
    try:
      raw = await model_gateway.vision(
        system_prompt=_VISION_SYSTEM,
        user_text=f'这是第 {batch[0]}~{batch[-1]} 页的缩略图，请按格式指出有问题的页。',
        # vision() 只认 base64/data URL，本地路径必须先编码（否则 400 Non-base64 digit）
        images=[to_data_url(sheet)],
        temperature=0.1,
        max_tokens=800,
      )
      candidates = _parse_issues(raw, set(batch))
    except Exception as e:  # noqa: BLE001 - 视觉模型不可用时自检降级为「无结论」，不阻塞交付
      logger.warning('视觉自检失败（跳过该批，不影响交付）: %s', e)
      continue
    # 复检：缩略图会让细小文字发虚，容易误判"裁切/乱码" → 逐页用**原始分辨率**确认后才算数
    for cand in candidates:
      if await _confirm(images, cand['page'], work):
        issues.append(cand)
      else:
        logger.info('视觉自检复检未确认，按正常处理：第 %s 页（%s）', cand['page'], cand['reason'])
  return issues


async def _confirm(images: list[str], page: int, work: Path) -> bool:
  """对疑似页做**单页原图**复检，返回是否确认有问题。"""
  from core.model_gateway import model_gateway

  from .imaging import to_data_url
  if not (1 <= page <= len(images)):
    return False
  try:
    raw = await model_gateway.vision(
      system_prompt=_VISION_RECHECK_SYSTEM,
      user_text=f'这是第 {page} 页的原始分辨率截图，请判断。',
      images=[to_data_url(images[page - 1])],
      temperature=0.1,
      max_tokens=300,
    )
    m = re.search(r'\{.*\}', raw or '', re.S)
    if not m:
      return False
    data = json.loads(m.group(0))
    issue = data.get('issue') if isinstance(data, dict) else None
    return bool(isinstance(issue, dict) and issue.get('problematic'))
  except Exception as e:  # noqa: BLE001 - 复检失败按"未确认"（宁可不误报）
    logger.warning('视觉复检失败（按正常页处理）第 %s 页: %s', page, e)
    return False


async def check_pages(images: list[str], work: Path) -> dict[int, dict]:
  """两级自检：确定性 + 视觉（视觉只查确定性检查通过、但可能被裁切/乱码的页）。

  Returns:
    `{页号(1-based): {ok, reason, source}}`；视觉不可用时只返回确定性结果。
  """
  results = deterministic_checks(images)
  if not settings.VIDEO_AGENT_RITL_ENABLED:
    return results

  # 只把"看起来有内容"的页交给视觉检查：空白页已由确定性检查判定，再看是浪费
  candidates = [n for n, r in results.items() if r.get('ok')]
  if not candidates:
    return results

  issues = await vision_check(images, candidates, work)
  for it in issues:
    page = it['page']
    results[page] = {'ok': False, 'reason': it['reason'], 'source': 'vision'}
  for n in candidates:
    results[n].setdefault('source', 'deterministic')
  return results

"""PPT → 逐页 PNG（LibreOffice 转 PDF，再用 pypdfium2 光栅化）。

为什么不自己解析 pptx 画图：PPT 的排版/图表/图片/母版无法用 python-pptx 还原，
只有 LibreOffice 能把页面渲染成"就是这个样子"。pypdfium2 是 PDFium 的 Python 绑定
（BSD 系许可，避免 PyMuPDF 的 AGPL），无需系统装 poppler。

**必须在后端进程里跑**（sandbox 禁 subprocess/exec/fork，见 core/sandbox/runner.py）：
soffice 是外部二进制，起一次进程才能转换。soffice 同机并发易锁冲突，故全模块串行
（`_SOFFICE_LOCK`），并加超时兜底。
"""

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

# soffice 是单实例应用，并发调用会争抢用户配置目录而失败 → 全局串行
_SOFFICE_LOCK = asyncio.Semaphore(1)


class SlideRenderError(Exception):
  """PPT 无法转换为图片。"""


# 发行版/Homebrew 装的 fontconfig 配置（Linux 容器走第一个；macOS 一般都没有）
_SYSTEM_FONTCONFIGS = (
  Path('/etc/fonts/fonts.conf'),                 # Debian/Ubuntu 等
  Path('/opt/homebrew/etc/fonts/fonts.conf'),    # Apple Silicon + Homebrew
  Path('/usr/local/etc/fonts/fonts.conf'),       # Intel macOS + Homebrew
)


def _fontconfig_file() -> str | None:
  """给 soffice 用的 fontconfig 配置路径；返回 None = 不注入，直接用系统配置。

  **只在系统没有 fontconfig 配置时才生成**（即 macOS 开发机）：容器里装了
  `fonts-noto-cjk`，系统配置能被 LO 正常读取，就是业界通行做法（Dify 的 Dockerfile
  同样只是装包），此时注入反而多此一举。

  为什么 macOS 上非注入不可（实测，不是猜）：这台机器的 LO **只认 fontconfig 的
  环境变量**——真实 HOME、用户级 `~/.config/fontconfig/fonts.conf`、标准
  `-env:UserInstallation` 全都试过，汉字墨迹一律为 0（把字体拷进 `~/Library/Fonts`
  也没用）；只有 `FONTCONFIG_FILE` / `FONTCONFIG_PATH` 能让它出汉字（墨迹 0 → 1600）。
  所以这里不是"绕开标准"，而是"这台机器上没有系统配置可继承"的兜底。

  配置里**先 include 各候选系统配置再加自己的字体目录**：`FONTCONFIG_FILE` 是**替换**
  而非合并语义，不 include 就会丢掉系统那套字体匹配/别名规则（`宋体 → Noto` 之类
  的回退正靠它）。当前分支下候选通常都不存在，include 是防以后改判定条件时静默丢规则。
  """
  global _FONTCONFIG_PATH
  if _FONTCONFIG_PATH != '':
    return _FONTCONFIG_PATH or None

  for sys_conf in _SYSTEM_FONTCONFIGS:
    if sys_conf.is_file():
      logger.info('LibreOffice 字体：系统配置 %s 可用，不注入自定义 fontconfig', sys_conf)
      _FONTCONFIG_PATH = None
      return None

  root = Path(__file__).resolve().parents[2]
  base = Path(tempfile.gettempdir()) / 'rz-fontconfig'
  cache = base / 'cache'
  dirs = [
    root / 'assets' / 'fonts',            # 仓库自带中文字体（wqy-microhei，已进 git）
    Path('/usr/share/fonts'),             # Linux：fonts-noto-cjk 等
    Path('/usr/local/share/fonts'),
    Path.home() / 'Library' / 'Fonts',    # macOS 开发机
    Path('/Library/Fonts'),
    Path('/System/Library/Fonts'),
    Path('/System/Library/Fonts/Supplemental'),
  ]
  try:
    cache.mkdir(parents=True, exist_ok=True)
    conf = base / 'fonts.conf'
    conf.write_text(
      '<?xml version="1.0"?>\n'
      '<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
      '<fontconfig>\n'
      + ''.join(
        f'  <include ignore_missing="yes">{p}</include>\n' for p in _SYSTEM_FONTCONFIGS
      )
      + ''.join(f'  <dir>{d}</dir>\n' for d in dirs)
      + f'  <cachedir>{cache}</cachedir>\n'
      '</fontconfig>\n',
      encoding='utf-8',
    )
    _FONTCONFIG_PATH = str(conf)
    logger.info(
      'LibreOffice 字体：系统无 fontconfig 配置，改用自带配置 %s（字体目录 %d 个）',
      conf, len(dirs),
    )
  except Exception as e:  # noqa: BLE001 - 生成失败就退回 LO 默认行为
    logger.warning('生成 fontconfig 配置失败（LO 将用默认字体路径）: %s', e)
    _FONTCONFIG_PATH = None
  return _FONTCONFIG_PATH


_FONTCONFIG_PATH = ''   # '' = 未尝试；None = 尝试过但不需要/不可用


def _find_soffice() -> str | None:
  """定位 soffice 可执行文件（PATH 优先，再试常见绝对路径）。

  macOS 用 brew cask 装的 LibreOffice **不在 PATH 里**（在 App 包内），开发机上必须靠
  这里的候选路径才能找到，否则本地永远报"未安装 LibreOffice"。
  """
  found = shutil.which('soffice') or shutil.which('libreoffice')
  if found:
    return found
  for p in (
    '/Applications/LibreOffice.app/Contents/MacOS/soffice',   # macOS（brew --cask）
    '/usr/bin/soffice',                                        # Debian/Ubuntu
    '/usr/lib/libreoffice/program/soffice',
    '/snap/bin/libreoffice',
  ):
    if Path(p).is_file():
      return p
  return None


def soffice_available() -> bool:
  return _find_soffice() is not None


_CJK_PROBE: bool | None = None
_CJK_PROBE_ERROR: str = ''      # 探针自身出错时的原因（预检脚本据此判定"没能验证"，不误报为 OK）


async def cjk_render_available(work: Path | None = None) -> bool:
  """探针：当前环境里 LibreOffice **能否渲染汉字**（进程内缓存）。

  为什么值得单独探：字体缺失时 LO **不报错**，只是把汉字画成空白/方框——用户看到的
  是一段"只有英文和数字"的视频（实测事故）。所以在建任务时就探一次，缺字体直接给出
  可操作的原因，而不是产出一批没有汉字的成品。
  """
  global _CJK_PROBE
  if _CJK_PROBE is not None:
    return _CJK_PROBE
  probe_dir = (work or (Path(tempfile.gettempdir()) / 'rz-cjk-probe'))
  try:
    from pptx import Presentation
    from pptx.util import Inches, Pt

    probe_dir.mkdir(parents=True, exist_ok=True)
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    tb = slide.shapes.add_textbox(Inches(0.5), Inches(2), Inches(9), Inches(2))
    run = tb.text_frame.paragraphs[0].add_run()
    run.text = '汉字测试'
    run.font.size = Pt(54)
    prs.save(probe_dir / 'cjk_probe.pptx')

    pdf = await _convert_to_pdf(str(probe_dir / 'cjk_probe.pptx'), probe_dir)
    ink = await asyncio.to_thread(_ink_pixels, pdf, probe_dir / 'probe.png')
    _CJK_PROBE = ink > 300        # 有汉字时数十个字的墨迹远超此阈值；缺字体时为 0
    logger.info('中文字体探针: 墨迹=%d → %s', ink, '可用' if _CJK_PROBE else '不可用')
  except Exception as e:  # noqa: BLE001 - 探针自身失败不阻断（按可用处理，避免误杀）
    global _CJK_PROBE_ERROR
    logger.warning('中文字体探针执行失败（跳过检查）: %s', e)
    _CJK_PROBE_ERROR = str(e)
    _CJK_PROBE = True
  return bool(_CJK_PROBE)


def _ink_pixels(pdf_path: Path, png_path: Path) -> int:
  """把 PDF 首页光栅化，返回灰度 < 100 的像素数（粗略的"有没有字"判据）。"""
  import pypdfium2 as pdfium
  from PIL import Image
  doc = pdfium.PdfDocument(str(pdf_path))
  try:
    img = doc[0].render(scale=1.5).to_pil().convert('L')
  finally:
    doc.close()
  img.save(png_path)
  px = img.load()
  return sum(1 for x in range(0, img.width, 2) for y in range(0, img.height, 2) if px[x, y] < 100)


async def _convert_to_pdf(ppt_path: str, out_dir: Path) -> Path:
  """soffice --headless --convert-to pdf（串行 + 超时）。"""
  soffice = _find_soffice()
  if not soffice:
    raise SlideRenderError('未找到 soffice（LibreOffice）；镜像需装 libreoffice-impress')

  out_dir.mkdir(parents=True, exist_ok=True)
  cmd = [
    soffice, '--headless', '--norestore', '--nolockcheck',
    '--convert-to', 'pdf', '--outdir', str(out_dir), ppt_path,
  ]
  env = {
    # 独立用户配置目录：多进程/并发时 soffice 会因共享 profile 而失败
    'HOME': str(out_dir),
    'PATH': '/usr/bin:/bin',
    'LANG': 'C.UTF-8',
  }
  fc = _fontconfig_file()
  if fc:
    env['FONTCONFIG_FILE'] = fc   # 仅系统无 fontconfig 配置时才有值（否则 None，用系统配置）
  async with _SOFFICE_LOCK:
    proc = await asyncio.create_subprocess_exec(
      *cmd, env=env,
      stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
      _, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=settings.VIDEO_AGENT_SOFFICE_TIMEOUT,
      )
    except asyncio.TimeoutError:
      proc.kill()
      raise SlideRenderError(
        f'soffice 转换超时（>{settings.VIDEO_AGENT_SOFFICE_TIMEOUT}s）'
      ) from None

  pdf = out_dir / (Path(ppt_path).stem + '.pdf')
  if proc.returncode != 0 or not pdf.is_file():
    tail = (stderr or b'')[-400:].decode('utf-8', 'replace')
    raise SlideRenderError(f'soffice 转换失败 rc={proc.returncode}: {tail}')
  return pdf


def _rasterize(pdf_path: Path, images_dir: Path, dpi: int) -> list[str]:
  """PDF 逐页渲染 PNG（pypdfium2），返回图片路径列表（页码顺序）。"""
  try:
    import pypdfium2 as pdfium
  except ImportError as e:  # pragma: no cover - 依赖缺失时给出明确提示
    raise SlideRenderError('未安装 pypdfium2（requirements.txt 需加）') from e

  images_dir.mkdir(parents=True, exist_ok=True)
  out: list[str] = []
  pdf = pdfium.PdfDocument(str(pdf_path))
  try:
    scale = max(0.5, dpi / 72.0)
    for i in range(len(pdf)):
      page = pdf[i]
      bitmap = page.render(scale=scale)
      img = bitmap.to_pil().convert('RGB')
      path = images_dir / f'slide_{i + 1:03d}.png'
      img.save(path, format='PNG')
      out.append(str(path))
  finally:
    pdf.close()
  return out


async def ppt_to_images(ppt_path: str, work: Path, dpi: int | None = None) -> list[str]:
  """PPT → 逐页 PNG 路径列表。

  Args:
    ppt_path: 会话上传目录里的 .pptx/.ppt 路径。
    work: 任务工作目录（PDF 与图片都落这里）。
    dpi: 渲染 DPI，默认取 settings.VIDEO_AGENT_SLIDES_DPI。

  Returns:
    按页码升序的 PNG 绝对路径列表。

  Raises:
    SlideRenderError: 文件不存在 / soffice 不可用 / 转换失败 / 页数为 0。
  """
  if not ppt_path or not Path(ppt_path).is_file():
    raise SlideRenderError(f'PPT 文件不存在: {ppt_path}')
  dpi = dpi or settings.VIDEO_AGENT_SLIDES_DPI

  pdf = await _convert_to_pdf(ppt_path, work / 'pdf')
  images = await asyncio.to_thread(_rasterize, pdf, work / 'slides', dpi)
  if not images:
    raise SlideRenderError('PPT 未渲染出任何页面')
  if len(images) > settings.VIDEO_AGENT_MAX_PAGES:
    raise SlideRenderError(
      f'页数 {len(images)} 超过上限 {settings.VIDEO_AGENT_MAX_PAGES} 页'
    )
  logger.info('PPT 渲染完成: %s → %d 页 (dpi=%d)', Path(ppt_path).name, len(images), dpi)
  return images

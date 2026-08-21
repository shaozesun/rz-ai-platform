"""上传文件内容提取 — 按扩展名分发到项目已有库，不引入新依赖。

提取结果不是"完整转 Markdown 塞进 prompt"，而是「大纲/结构概要 + 首屏内容预览」，
完整内容需要时靠 tools/upload_tools.py 的 read_uploaded_file 按需读取全文——避免大文档
一次性把大量 token 塞进上下文。

复用：
- .docx/.pptx 用项目已依赖的 python-docx/python-pptx（报告生成沙箱脚本已在用）
- .pdf 直接调用 repository/ingestion/loader.py 的 _load_pdf_uncached（已实现
  MinerU 本地/云端/CLI → 降级 pypdf 的完整链路，零新依赖）
- .xlsx/.csv 用 pandas（已依赖）
"""

import logging

logger = logging.getLogger(__name__)

# 首屏文本预览截断长度（大纲之外的正文摘要）
_TEXT_PREVIEW_CHARS = 2000
# 表格预览行数
_TABLE_PREVIEW_ROWS = 5


def _err(message: str) -> dict:
  return {'text_preview': '', 'outline': [], 'error': message}


def _extract_docx(path) -> dict:
  """提取 docx：标题样式作为大纲节点，全文段落拼接做预览。"""
  import docx

  try:
    doc = docx.Document(str(path))
  except Exception as e:
    return _err(f'docx 解析失败: {e}')

  outline = []
  full_text_parts = []
  for para in doc.paragraphs:
    text = para.text.strip()
    if not text:
      continue
    full_text_parts.append(text)
    style_name = (para.style.name or '') if para.style else ''
    if 'Heading' in style_name or 'Title' in style_name:
      outline.append(text)

  table_summary = []
  for i, table in enumerate(doc.tables):
    rows, cols = len(table.rows), len(table.columns)
    table_summary.append(f'表格{i + 1}（{rows}行×{cols}列）')

  full_text = '\n'.join(full_text_parts)
  preview = full_text[:_TEXT_PREVIEW_CHARS]
  if table_summary:
    preview += '\n\n[文档内表格] ' + '；'.join(table_summary)
  return {'text_preview': preview, 'outline': outline, 'error': None, 'full_text': full_text}


def _extract_pptx(path) -> dict:
  """提取 pptx：每张幻灯片标题作为大纲节点，按顺序拼接全部文本框内容做预览。"""
  from pptx import Presentation

  try:
    prs = Presentation(str(path))
  except Exception as e:
    return _err(f'pptx 解析失败: {e}')

  outline = []
  full_text_parts = []
  for i, slide in enumerate(prs.slides, 1):
    slide_title = ''
    slide_texts = []
    for shape in slide.shapes:
      if not shape.has_text_frame:
        continue
      text = shape.text_frame.text.strip()
      if not text:
        continue
      slide_texts.append(text)
      if not slide_title and shape == (slide.shapes.title if slide.shapes.title else None):
        slide_title = text
    if not slide_title and slide_texts:
      slide_title = slide_texts[0][:50]
    if slide_title:
      outline.append(f'第{i}页: {slide_title}')
    if slide_texts:
      full_text_parts.append(f'[第{i}页]\n' + '\n'.join(slide_texts))

  full_text = '\n\n'.join(full_text_parts)
  preview = full_text[:_TEXT_PREVIEW_CHARS]
  return {'text_preview': preview, 'outline': outline, 'error': None, 'full_text': full_text}


def _extract_pdf(path) -> dict:
  """提取 pdf：复用 RAG loader 的 _load_pdf_uncached（MinerU→降级 pypdf 完整链路）。"""
  from repository.ingestion.loader import _load_pdf_uncached

  try:
    documents = _load_pdf_uncached(str(path))
  except Exception as e:
    return _err(f'pdf 解析失败: {e}')

  if not documents:
    return _err('pdf 未提取到任何内容（可能是加密文档或扫描件）')

  full_text = '\n\n'.join(d.page_content for d in documents if d.page_content)
  if not full_text.strip():
    return _err('pdf 未提取到任何文本内容（可能是纯图片扫描件）')

  # 大纲：按页切分，取每页首行作为大致章节提示（无法拿到真实标题层级）
  outline = []
  for i, doc in enumerate(documents[:20], 1):  # 大纲最多列前 20 页，避免超长文档大纲过长
    first_line = (doc.page_content or '').strip().split('\n', 1)[0][:60]
    if first_line:
      outline.append(f'第{i}页: {first_line}')

  preview = full_text[:_TEXT_PREVIEW_CHARS]
  return {'text_preview': preview, 'outline': outline, 'error': None, 'full_text': full_text}


def _extract_table(path, *, is_csv: bool) -> dict:
  """提取 xlsx/csv：列名 + 前 N 行预览（表格类走 load_uploaded_table_as_dataset 分析，
  这里只做内容概览，不是完整数据物化）。"""
  import pandas as pd

  try:
    df = pd.read_csv(path) if is_csv else pd.read_excel(path, sheet_name=0)
  except Exception as e:
    return _err(f'表格解析失败: {e}')

  columns = list(df.columns)
  preview_rows = df.head(_TABLE_PREVIEW_ROWS).to_dict(orient='records')
  outline = [f'共 {len(df)} 行 × {len(columns)} 列', f'列名: {", ".join(str(c) for c in columns)}']
  preview_text = '\n'.join(str(row) for row in preview_rows)
  return {
    'text_preview': preview_text[:_TEXT_PREVIEW_CHARS],
    'outline': outline,
    'error': None,
    'rows': len(df),
    'columns': columns,
  }


_DISPATCH = {
  '.docx': _extract_docx,
  '.pptx': _extract_pptx,
  '.pdf': _extract_pdf,
}

# 图片不走文本提取，由 tools/upload_tools.py 的 analyze_uploaded_image 调视觉模型理解
_IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp'}


def extract_content(path) -> dict:
  """按扩展名分发提取，返回统一结构 {text_preview, outline, error, ...}。

  提取失败不抛异常，返回 error 字段，让上传流程不中断——用户仍能看到"文件已保存但
  内容无法自动解析"的友好提示。

  Returns:
    dict: 至少含 text_preview（str）、outline（list[str]）、error（str | None）；
      docx/pptx/pdf 额外含 full_text；xlsx/csv 额外含 rows/columns。
  """
  from pathlib import Path

  ext = Path(str(path)).suffix.lower()
  try:
    if ext in ('.xlsx', '.xls'):
      return _extract_table(path, is_csv=False)
    if ext == '.csv':
      return _extract_table(path, is_csv=True)
    if ext in _IMAGE_EXTENSIONS:
      return {
        'text_preview': '',
        'outline': ['图片文件（无文本内容；视觉分析请调用 analyze_uploaded_image 工具）'],
        'error': None,
        'kind': 'image',
      }
    handler = _DISPATCH.get(ext)
    if handler is None:
      return _err(f'不支持的文件类型: {ext}（支持 docx/pptx/pdf/xlsx/csv + 图片）')
    return handler(path)
  except Exception as e:
    logger.exception('提取上传文件内容失败: %s', path)
    return _err(f'内容提取异常: {e}')

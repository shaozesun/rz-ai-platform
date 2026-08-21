"""上传文件工具 — 让 Agent 能"回忆"起本会话上传过的文件并读取内容。

三个工具，走 raw_tools（始终注入，不经 deferred filter——文件上传是基础能力，
不该被隐藏在 tool_search 后面，跟 skill_manage/analysis_* 一致）：
- list_uploaded_files：列出本会话所有已上传文件 + 大纲/预览摘要
- read_uploaded_file：读取指定文件的完整提取内容
- load_uploaded_table_as_dataset：把已上传表格转成沙箱 dataset，走现有 analysis_exec 分析
"""

import json
import logging

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.sandbox import uploads as upload_storage
from core.sandbox import upload_extract
from core.sandbox.datasets import save_dataset
from tools.analysis_file_tools import _get_thread_id

logger = logging.getLogger(__name__)

_TABLE_EXTENSIONS = ('.xlsx', '.xls', '.csv')
_IMAGE_EXTENSIONS = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')
# 单张图片交给视觉模型的上限（原始字节），防大图 base64 撑爆请求
_IMAGE_MAX_BYTES = 10 * 1024 * 1024

# 视觉模型提取「可复刻为 HTML 看板」的风格规范
_IMAGE_STYLE_PROMPT = (
  '你是可视化看板风格分析助手。请从这张图片中提取可用于复刻 HTML 看板的视觉规范，'
  '只做客观描述，不要评价好坏，输出简洁条目：\n'
  '1. 整体配色：背景色、主色、强调色、渐变（尽量给出 hex 或具体描述）\n'
  '2. 布局结构：顶部标题栏 / KPI 指标行 / 图表栅格 / 左右分栏等区块划分\n'
  '3. 图表类型：用到了哪些图表（折线/柱状/环形/仪表盘/雷达等）及其大致位置\n'
  '4. 装饰风格：边框 / 发光 / 圆角 / 字体粗细 / 分隔线等\n'
  '输出用于后续直接据此填充 HTML 看板模板。'
)


def _err(msg: str) -> str:
  return json.dumps({'error': msg}, ensure_ascii=False)


@tool
async def list_uploaded_files(config: RunnableConfig = None) -> str:
  """列出本会话（当前对话）内用户已上传的所有文件，及各自内容的大纲/预览摘要。

  用于"回忆"起之前几轮上传过、这轮没提到的文件——用户问"我刚才传的文件里写了什么"
  这类问题时，先调本工具看看有哪些文件，再用 read_uploaded_file 读具体内容。
  """
  thread_id = _get_thread_id(config)
  entries = upload_storage.list_uploads(thread_id)
  if not entries:
    return json.dumps({'files': [], 'note': '本会话暂无已上传文件'}, ensure_ascii=False)

  result = []
  for entry in entries:
    path = upload_storage.get_upload_path(thread_id, entry['filename'])
    outline = []
    error = None
    kind = None
    if path is not None:
      extracted = upload_extract.extract_content(path)
      outline = extracted.get('outline') or []
      error = extracted.get('error')
      kind = extracted.get('kind')
    result.append({
      'filename': entry['filename'],
      'size': entry['size'],
      'extension': entry['extension'],
      'outline': outline,
      'error': error,
      'kind': kind,
    })
  return json.dumps({'files': result}, ensure_ascii=False)


@tool
async def read_uploaded_file(filename: str, config: RunnableConfig = None) -> str:
  """读取本会话内指定已上传文件的完整提取内容（正文全文，非摘要）。

  适合用户明确要求"看看那个文件里具体写了什么"、或需要参照文件完整内容做分析/生成时。
  表格文件（xlsx/csv）建议改用 load_uploaded_table_as_dataset 走沙箱分析而非直接读全文。

  Args:
    filename: 文件名（从 list_uploaded_files 返回结果中获取）
  """
  thread_id = _get_thread_id(config)
  path = upload_storage.get_upload_path(thread_id, filename)
  if path is None:
    return _err(f'文件不存在：{filename}（请先用 list_uploaded_files 确认文件名）')

  extracted = upload_extract.extract_content(path)
  if extracted.get('error'):
    return json.dumps({'error': extracted['error']}, ensure_ascii=False)

  if extracted.get('kind') == 'image':
    return json.dumps({
      'note': '这是图片文件，无文本内容；请调用 analyze_uploaded_image 工具做视觉分析',
    }, ensure_ascii=False)

  full_text = extracted.get('full_text')
  if full_text is None:
    # 表格类无 full_text，返回结构信息引导改用专用工具
    return json.dumps({
      'note': '这是表格文件，建议用 load_uploaded_table_as_dataset 加载后走 analysis_exec 分析',
      'outline': extracted.get('outline') or [],
    }, ensure_ascii=False)

  return json.dumps({'content': full_text}, ensure_ascii=False)


@tool
async def load_uploaded_table_as_dataset(filename: str, config: RunnableConfig = None) -> str:
  """把已上传的表格文件（xlsx/csv）物化为沙箱数据集，供 analysis_exec 用 pandas 分析。

  用户上传表格后要求"分析一下这份数据"时用本工具，之后按现有数据分析流程（analysis_exec
  写 pandas 代码）处理，与 analysis_load 从平台 API 拉取数据的产出格式一致。

  Args:
    filename: 表格文件名（从 list_uploaded_files 返回结果中获取），仅支持 xlsx/xls/csv
  """
  thread_id = _get_thread_id(config)
  path = upload_storage.get_upload_path(thread_id, filename)
  if path is None:
    return _err(f'文件不存在：{filename}（请先用 list_uploaded_files 确认文件名）')

  if path.suffix.lower() not in _TABLE_EXTENSIONS:
    return _err(f'{filename} 不是表格文件（仅支持 xlsx/xls/csv），文档类文件请用 read_uploaded_file')

  import pandas as pd

  try:
    df = pd.read_csv(path) if path.suffix.lower() == '.csv' else pd.read_excel(path, sheet_name=0)
  except Exception as e:
    return _err(f'表格解析失败：{e}')

  records = df.to_dict(orient='records')
  if not records:
    return _err(f'{filename} 是空表格，无数据可分析')

  info = save_dataset(thread_id, f'upload:{filename}', {}, records, dataset_name=path.stem)
  info['note'] = '已上传表格物化为数据集，请调用 analysis_exec 写 pandas 代码分析，df 已预载'
  return json.dumps(info, ensure_ascii=False)


@tool
async def analyze_uploaded_image(
  filename: str,
  instruction: str | None = None,
  config: RunnableConfig = None,
) -> str:
  """用视觉模型分析本会话已上传的图片，提取其视觉风格/布局/配色，用于按参考图风格生成 HTML 看板。

  用户说「参考这张图做个看板 / 照这个样式做」时用本工具。先 list_uploaded_files 确认
  图片文件名，再调本工具得到风格描述，随后按描述填充 dashboard_template 生成看板。

  Args:
    filename: 图片文件名（从 list_uploaded_files 返回结果中获取）
    instruction: 可选，额外指定要提取的重点，如「重点看图表配色」
  """
  thread_id = _get_thread_id(config)
  path = upload_storage.get_upload_path(thread_id, filename)
  if path is None:
    return _err(f'文件不存在：{filename}（请先用 list_uploaded_files 确认文件名）')
  if path.suffix.lower() not in _IMAGE_EXTENSIONS:
    return _err(
      f'{filename} 不是支持的图片（支持 png/jpg/jpeg/webp/gif/bmp），'
      f'文本类文件请用 read_uploaded_file'
    )

  import base64

  try:
    raw = path.read_bytes()
  except OSError as e:
    return _err(f'读取图片失败：{e}')
  if len(raw) > _IMAGE_MAX_BYTES:
    return _err('图片过大（>10MB），请压缩后重试')
  b64 = base64.b64encode(raw).decode('ascii')

  try:
    from core.model_gateway import model_gateway
    desc = await model_gateway.vision(
      system_prompt=_IMAGE_STYLE_PROMPT,
      user_text=instruction or '请描述这张图的视觉风格与布局，用于复刻 HTML 看板。',
      images=[b64],
    )
  except Exception as e:
    logger.warning('vision 分析图片失败: %s', e)
    return _err(f'视觉模型分析失败：{e}')

  max_chars = settings.AGENT_ANALYSIS_MAX_OUTPUT_CHARS
  if len(desc) > max_chars:
    desc = desc[:max_chars] + '…（已截断）'
  return json.dumps({'style': desc}, ensure_ascii=False)


def build_upload_tools() -> list:
  """返回上传相关工具列表（供 agent_service 追加到 raw_tools）。"""
  return [
    list_uploaded_files,
    read_uploaded_file,
    load_uploaded_table_as_dataset,
    analyze_uploaded_image,
  ]

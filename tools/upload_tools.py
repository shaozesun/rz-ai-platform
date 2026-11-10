"""上传文件工具 — 让 Agent 能"回忆"起本会话上传过的文件并读取内容。

四个工具，走 raw_tools（始终注入，不经 deferred filter——文件上传是基础能力，
不该被隐藏在 tool_search 后面，跟 skill_manage/analysis_* 一致）：
- list_uploaded_files：列出本会话所有已上传文件 + 大纲/预览摘要 + **结构概要**
- read_uploaded_file：读取指定文件的完整提取内容（含结构概要）
- load_uploaded_table_as_dataset：把已上传表格转成沙箱 dataset，走现有 analysis_exec 分析
- copy_uploaded_file_to_workspace：把上传的**文档**复制到会话工作区，让沙箱脚本能读写它
  （保格式地改文档——如翻译——必须先拿到原始文件，光有提取文本只能"重写"）
"""

import json
import logging
import shutil

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.agent.untrusted_content import sanitizing
from core.sandbox import uploads as upload_storage
from core.sandbox import upload_extract
from core.sandbox.datasets import save_dataset, thread_dir
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


def _resolve_upload(thread_id: str, filename: str):
  """定位已上传文件；filename 打错时尽量自纠，返回 (path|None, 实际文件名, 提示语)。

  为什么要自纠：模型经常"凭印象"重打文件名——实测把
  `副本1.5.1润泽科技-廊坊-AX-机柜上下电管理制度.docx` 打成带空格的
  `副本 1.5.1 润泽科技 - 廊坊-AX-...`，然后**连撞 3 次同一个错名**（没人告诉它正确的名字），
  最后放弃作业、说"由于技术限制"。所以：
  1) 先精确匹配；
  2) 再用**去掉所有空白**后的名字唯一匹配（"多加/少加空格"是最常见的一种错法，
     唯一命中时才采纳，不猜歧义）；
  3) 都不中 → 报错里**列出本会话真实文件名** + 最相近的一个。
  """
  import difflib

  path = upload_storage.get_upload_path(thread_id, filename)
  if path is not None:
    return path, path.name, ''

  names = [e['filename'] for e in upload_storage.list_uploads(thread_id)]
  if not names:
    return None, '', f'本会话没有已上传文件（你传的是：{filename}）——请先让用户上传'

  squeezed = ''.join(filename.split())
  hits = [n for n in names if ''.join(n.split()) == squeezed]
  if len(hits) == 1:
    path = upload_storage.get_upload_path(thread_id, hits[0])
    if path is not None:
      return path, hits[0], f'（文件名已按实际值纠正：{hits[0]}）'

  close = difflib.get_close_matches(filename, names, n=1, cutoff=0.35)
  hint = f'，是不是想找「{close[0]}」？' if close else ''
  return None, '', f'文件不存在：{filename}{hint}。本会话已上传：{names}'


@tool
@sanitizing
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
    structure = []
    if path is not None:
      extracted = upload_extract.extract_content(path)
      outline = extracted.get('outline') or []
      structure = extracted.get('structure') or []
      error = extracted.get('error')
      kind = extracted.get('kind')
    result.append({
      'filename': entry['filename'],
      'size': entry['size'],
      'extension': entry['extension'],
      'outline': outline,
      # 文档结构概要（段落/表格/图片/页眉页脚/备注…）：改文档前必须先知道要保住什么
      'structure': structure,
      'error': error,
      'kind': kind,
    })
  return json.dumps({'files': result}, ensure_ascii=False)


@tool
@sanitizing
async def read_uploaded_file(filename: str, config: RunnableConfig = None) -> str:
  """读取本会话内指定已上传文件的完整提取内容（正文全文，非摘要）。

  适合用户明确要求"看看那个文件里具体写了什么"、或需要参照文件完整内容做分析/生成时。
  表格文件（xlsx/csv）建议改用 load_uploaded_table_as_dataset 走沙箱分析而非直接读全文。

  Args:
    filename: 文件名（从 list_uploaded_files 返回结果中获取）
  """
  thread_id = _get_thread_id(config)
  path, _real_name, msg = _resolve_upload(thread_id, filename)
  if path is None:
    return _err(msg)

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

  result: dict = {'content': full_text}
  # 结构概要随全文一起给：要保格式地改文档（翻译/批量替换）时，
  # 模型必须先知道有哪些容器、哪些东西改不了
  structure = extracted.get('structure') or []
  if structure:
    result['structure'] = structure
  return json.dumps(result, ensure_ascii=False)


@tool
@sanitizing
async def load_uploaded_table_as_dataset(filename: str, config: RunnableConfig = None) -> str:
  """把已上传的表格文件（xlsx/csv）物化为沙箱数据集，供 analysis_exec 用 pandas 分析。

  用户上传表格后要求"分析一下这份数据"时用本工具，之后按现有数据分析流程（analysis_exec
  写 pandas 代码）处理，与 analysis_load 从平台 API 拉取数据的产出格式一致。

  Args:
    filename: 表格文件名（从 list_uploaded_files 返回结果中获取），仅支持 xlsx/xls/csv
  """
  thread_id = _get_thread_id(config)
  path, _real_name, msg = _resolve_upload(thread_id, filename)
  if path is None:
    return _err(msg)

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
@sanitizing
async def copy_uploaded_file_to_workspace(filename: str, config: RunnableConfig = None) -> str:
  """把本会话已上传的文件复制到**会话工作区**，供沙箱脚本读取和改写。

  什么时候用：要**保格式地改文档**时（如「把这份 Word/PPT 翻译成英文」「把文档里的
  XX 全部换成 YY」）。只读 `read_uploaded_file` 拿到的是**提取后的纯文本**，用它重写
  必然丢图片/表格/样式；要保住原格式，必须让脚本直接处理**原始文件**。

  之后怎么写脚本：`analysis_exec(dataset_id='workspace', file='<脚本名>.py')`
  （脚本先用 analysis_write_file 落到同一 workspace）。脚本的**工作目录就是 workspace**，
  所以直接用相对路径读 `./源文件名`、把产物写到 `./输出名`——产物会被自动扫成可下载卡片。

  Args:
    filename: 已上传文件名（从 list_uploaded_files 返回结果中获取）
  """
  thread_id = _get_thread_id(config)
  src, real_name, msg = _resolve_upload(thread_id, filename)
  if src is None:
    return _err(msg)
  if src.suffix.lower() in (*_TABLE_EXTENSIONS, *_IMAGE_EXTENSIONS):
    return _err(
      f'{filename} 是表格/图片，不用走这里：'
      '表格用 load_uploaded_table_as_dataset，图片用 analyze_uploaded_image',
    )

  ws = thread_dir(thread_id) / 'workspace'
  try:
    ws.mkdir(parents=True, exist_ok=True)
    target = ws / src.name
    shutil.copy2(src, target)
  except OSError as e:
    return _err(f'复制到工作区失败：{e}')

  return json.dumps({
    'dataset_id': 'workspace',
    'path': src.name,
    'note': (
      f'{src.name} 已在工作区（脚本 cwd 即此目录，用相对路径读写）。'
      "下一步：analysis_write_file(dataset_id='workspace', path='<脚本>.py', content=...) 落盘脚本 → "
      "analysis_exec(dataset_id='workspace', file='<脚本>.py') 执行；"
      '**不要**把原文重打一遍，要原地改、只替换文字，图片/表格/样式才保得住'
    ),
  }, ensure_ascii=False)


@tool
@sanitizing
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
  path, _real_name, msg = _resolve_upload(thread_id, filename)
  if path is None:
    return _err(msg)
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
    copy_uploaded_file_to_workspace,
    analyze_uploaded_image,
  ]

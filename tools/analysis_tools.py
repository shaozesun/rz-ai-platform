"""数据分析工具 — 全量数据物化 + 沙箱执行（DifySandbox/DeerFlow 模式）。

两个始终可见的 async 工具（走 raw_tools，不经同步异常包装）：

1. analysis_load — 调平台能力分页拉**全量**数据，落盘为数据集（CSV+meta），
   只向 LLM 返回摘要（dataset_id / rows / columns / 前几行预览），原始数据
   永不进入上下文。绕过 tools_factory 的 page_size 封顶，受
   AGENT_ANALYSIS_MAX_ROWS 上限保护。
2. analysis_exec — 在受限子进程跑用户 pandas 代码（预载 df），只返回计算结果。

用法边界（description 已告知 LLM）：单条/少量查询用平台能力工具；需要对
全量/大量数据做统计、聚合、趋势、分布分析时，先用 analysis_load 物化数据，
再用 analysis_exec 写 pandas 代码计算。
"""

import json
import logging
from urllib.parse import quote

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.sandbox.datasets import DatasetError, load_dataset_dir, save_dataset
from core.sandbox.runner import run_python
from service.query.base.platform import get_capability, get_caller
from tools.analysis_file_tools import _get_thread_id, _resolve_dataset_path, build_file_tools

logger = logging.getLogger(__name__)

# 响应里可能的记录列表键（按优先级探测；实现时可按实际平台响应微调）
_RECORD_KEYS = ('list', 'records', 'rows', 'items', 'data', 'result')
# 响应里可能的总数键
_TOTAL_KEYS = ('total', 'totalCount', 'count', 'pageCounts')


async def _emit_status(tool: str, message: str) -> None:
  """向前端发阶段进度事件（纯反馈，失败绝不能影响工具执行）。"""
  try:
    from langchain_core.callbacks import adispatch_custom_event
    await adispatch_custom_event('analysis_status', {'tool': tool, 'message': message})
  except Exception:
    pass


def _extract_records(resp) -> tuple[list, str | None]:
  """从平台响应里通用抽取记录列表。"""
  if isinstance(resp, list):
    return resp, None
  if isinstance(resp, dict):
    for key in _RECORD_KEYS:
      value = resp.get(key)
      if isinstance(value, list) and value:
        return value, key
    # fallback：取首个非空 list 值
    for value in resp.values():
      if isinstance(value, list) and value:
        return value, None
  return [], None


def _extract_total(resp) -> int | None:
  """从响应里尝试取总数，取不到返回 None。"""
  if not isinstance(resp, dict):
    return None
  for key in _TOTAL_KEYS:
    try:
      return int(resp.get(key))
    except (TypeError, ValueError):
      continue
  return None


@tool
async def analysis_load(
  capability_id: str,
  params: dict | None = None,
  dataset_name: str = 'data',
  config: RunnableConfig = None,
) -> str:
  """把平台能力的数据全量物化为数据集，供沙箱分析。

  适用于需要对全量/大量记录做统计聚合的场景（如「统计所有工单状态分布」）。
  仅当参数范围能覆盖全量数据时使用；只查单条/前几条用平台能力工具即可。

  Args:
    capability_id: 平台能力 id，如 'mgmt.query_person_maintenance_plan'
    params: 查询筛选参数（除分页外，如 uid/dept_id/company_id/year 等），
      分页由工具自动翻页拉全量
    dataset_name: 数据集展示名，用于 dataset_id 前缀
  """
  thread_id = _get_thread_id(config)

  cap = get_capability(capability_id)
  if cap is None:
    return json.dumps({
      'error': f'未知能力 {capability_id}，请先从分类清单确认可用的能力 id',
    }, ensure_ascii=False)
  caller = get_caller(capability_id)
  if caller is None:
    return json.dumps({'error': f'能力 {capability_id} 无可用后端'}, ensure_ascii=False)

  max_rows = settings.AGENT_ANALYSIS_MAX_ROWS
  page_size = settings.AGENT_ANALYSIS_PAGE_SIZE

  await _emit_status('analysis_load', '正在物化数据…')
  records: list[dict] = []
  page = 1
  truncated = False
  while True:
    if len(records) >= max_rows:
      truncated = True
      break
    resp = await caller(capability_id, {
      **(params or {}),
      'page': str(page),
      'page_size': str(page_size),
    })
    page_records, _ = _extract_records(resp)
    if not page_records:
      break
    records.extend(page_records)
    total = _extract_total(resp)
    if total is not None:
      if len(records) >= total:
        break
    elif len(page_records) < page_size:
      break
    page += 1
    # 工具执行期间事件流默认静默（直到 on_tool_end），全量分页可能超过
    # AGENT_EVENT_TIMEOUT 触发整轮超时；周期发进度既防静默超时，又给用户实时反馈
    if records and page % 5 == 0:
      await _emit_status('analysis_load', f'正在物化数据…已加载 {len(records)} 行')

  if not records:
    return json.dumps({
      'error': '该查询未返回数据，请调整筛选参数后重试',
    }, ensure_ascii=False)

  await _emit_status('analysis_load', f'数据已就绪（{len(records)} 行）')
  info = save_dataset(thread_id, capability_id, params or {}, records, dataset_name)
  info['truncated'] = truncated  # 是否因达到上限截断
  info['note'] = (
    '数据已物化到沙箱，原始记录未进入上下文。'
    '请调用 analysis_exec 写 pandas 代码分析，df 已预载。'
  )
  return json.dumps(info, ensure_ascii=False)


@tool
async def analysis_exec(
  dataset_id: str,
  python_code: str | None = None,
  file: str | None = None,
  config: RunnableConfig = None,
) -> str:
  """在受限沙箱中执行 pandas 代码分析已物化的数据集。

  两种提供代码的方式（二选一）：
  - python_code：直接给一小段代码，适合临时/简单计算；
  - file：先 analysis_write_file 把脚本落盘（如 report.py），再传 file='report.py'
    执行，配合 analysis_str_replace 局部改代码，适合几十上百行的复杂产物脚本
    （多页 PPT / 带排版的 PDF 报告），省 token 且精准修改。

  代码内可用预载变量 `df`（DataFrame）。print() 输出文字结果；如需图表数据，
  请把最终结果赋值给变量 `result`（DataFrame / Series / list[dict] / dict），
  工具会把它作为 chart_data 返回——组装 <chart> 标签时原样引用，禁止改写数值。
  如需生成可下载的产物文件（CSV/Excel/图表图片/PPT/PDF），在代码里把文件写到
  当前目录（如 df.to_csv('xxx.csv')、fig.savefig('xxx.png')），工具会自动回传，
  返回的 artifacts 列表里每条有 url 和 name——组装 <file url="..." name="..."/>
  标签时原样引用，禁止改写。计算应简洁（聚合/分布/趋势），不要打印原始全量数据。
  输出截断为上限字符。

  Args:
    dataset_id: analysis_load 返回的 dataset_id
    python_code: 待执行的 pandas 代码，如
      "result = df['status'].value_counts().reset_index()"
    file: 已落盘脚本的相对路径（与 python_code 二选一），如 'report.py'
  """
  thread_id = _get_thread_id(config)
  if bool(python_code) == bool(file):
    return json.dumps({
      'error': 'python_code 与 file 需二选一提供（一个为 None 另一个必填）',
    }, ensure_ascii=False)
  if file:
    resolved = _resolve_dataset_path(thread_id, dataset_id, file)
    if resolved is None:
      return json.dumps({'error': '数据集不存在或脚本路径非法'}, ensure_ascii=False)
    ds_dir, script = resolved
    if not script.is_file():
      return json.dumps({
        'error': f'脚本 {file} 不存在，请先用 analysis_write_file 落盘',
      }, ensure_ascii=False)
    try:
      python_code = script.read_text(encoding='utf-8', errors='replace')
    except OSError as e:
      return json.dumps({'error': f'读取脚本失败：{e}'}, ensure_ascii=False)
  else:
    try:
      ds_dir = load_dataset_dir(thread_id, dataset_id)
    except DatasetError as e:
      return json.dumps({'error': str(e)}, ensure_ascii=False)

  await _emit_status('analysis_exec', '正在分析…')
  result = await run_python(
    python_code,
    cwd=str(ds_dir),
    timeout=settings.AGENT_ANALYSIS_TIMEOUT,
    max_output_chars=settings.AGENT_ANALYSIS_MAX_OUTPUT_CHARS,
    max_memory_mb=settings.AGENT_ANALYSIS_MAX_MEMORY_MB,
    max_artifact_mb=settings.AGENT_ANALYSIS_MAX_ARTIFACT_MB,
    max_artifacts=settings.AGENT_ANALYSIS_MAX_ARTIFACTS,
    cjk_font_path=settings.RZ_CJK_FONT,
    extra_lib_path=settings.AGENT_ANALYSIS_EXTRA_LIB_PATH or None,
  )

  if result.get('artifacts'):
    await _emit_status('analysis_exec', '正在生成产物…')
  else:
    await _emit_status('analysis_exec', '分析完成')

  if result['timed_out']:
    return json.dumps({'error': result['error']}, ensure_ascii=False)
  if result['error'] and not result['stdout'].strip():
    detail = result['stderr'].strip()[-1500:]
    return json.dumps({'error': result['error'], 'stderr': detail}, ensure_ascii=False)

  out = result['stdout'].strip() or '（代码执行无输出）'
  if result['stderr'].strip():
    out += f'\n\n[stderr]\n{result["stderr"].strip()[:1000]}'

  payload = {'result': out}
  if result.get('chart_data') is not None:
    payload['chart_data'] = result['chart_data']
  if result.get('chart_note'):
    payload['note'] = result['chart_note']
  # 产物文件 → 生成鉴权下载 URL（URL 由工具权威生成，LLM 只原样引用）
  artifacts = result.get('artifacts') or []
  if artifacts:
    payload['artifacts'] = [{
      'name': a['name'],
      'url': (
        f'/api/v1/chat/agent/artifacts/{quote(thread_id)}/{quote(dataset_id)}/'
        f'{quote(a["name"])}'
      ),
      'size': a['size'],
    } for a in artifacts]
  if result.get('artifact_note'):
    payload['artifact_note'] = result['artifact_note']
  return json.dumps(payload, ensure_ascii=False)


def build_analysis_tools() -> list:
  """返回分析工具列表（供 agent_service 追加到 raw_tools）。

  物化 + 执行 + 文件操作（write/read/str_replace/ls/glob/grep）一起装配，
  agent_service 无需改动。
  """
  return [analysis_load, analysis_exec] + build_file_tools()

"""
Agent 对话 API 端点（流式 SSE + 沙箱产物下载）
"""
from __future__ import annotations

import logging
import math
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from core.rbac import require_permission
from models.agent_schemas import AgentRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/chat', tags=['agent'])

# 沙箱产物 media_type 映射（扩展名 → MIME）
_MEDIA_TYPES = {
  '.csv': 'text/csv',
  '.json': 'application/json',
  '.md': 'text/markdown',
  '.html': 'text/html',
  '.txt': 'text/plain',
  '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  '.pdf': 'application/pdf',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
}
# active content 强制下载，防脚本执行（参照 DeerFlow ACTIVE_CONTENT_MIME_TYPES）
_ACTIVE_CONTENT_TYPES = {'text/html', 'application/xhtml+xml', 'image/svg+xml'}

# records 表格解码：能力 → 返回字段 → (列名别名, {码: 中文})。
# 兼容 data.csv 英文原始列名与 view.csv 的中文表头；查不到的码保留原值。
# 枚举来源 = 接口常用筛选项那套 编号→中文（capabilities.py 参数文档里已固化）。
# 后续其它能力/字段按同形状追加即可。
_RECORD_DECODE: dict[str, dict[str, tuple[list[str], object]]] = {
  'mgmt.query_breakdown_ticket': {
    'specialty': (['specialty', '专业'], {'1': '供配电', '2': '制冷', '4': '智能化'}),
    'stage': (['stage', '阶段'], {'7': '故障派发', '2': '故障处理', '3': '故障验收', '5': '故障关闭'}),
    # 故障级别保留数字、只加“级”字：0→0级 / 3→3级（值可能是 0..N）
    'breakDownLevel': (['breakDownLevel', '故障级别', '级别'], lambda c: f'{c}级'),
  },
}


def _decode_record_columns(
  capability_id: str | None,
  columns: list,
  records: list[dict],
) -> None:
  """按能力+列名把枚举码解码成中文，就地修改 records。"""
  if not capability_id:
    return
  spec = _RECORD_DECODE.get(capability_id)
  if not spec:
    return
  col_lower = {str(c).lower(): c for c in columns}
  for aliases, mapping in spec.values():
    matched = None
    for alias in aliases:
      if alias.lower() in col_lower:
        matched = col_lower[alias.lower()]
        break
    if matched is None:
      continue
    for rec in records:
      v = rec.get(matched)
      if v is None:
        continue
      # 支持两种解码：静态映射 {码: 中文}，或函数（如 故障级别 → f'{码}级'）
      label = mapping(str(v)) if callable(mapping) else mapping.get(str(v))
      if label is not None:
        rec[matched] = label


_HUMAN_KEYS = ('name', 'label', 'title', 'dept_name', 'company_name', 'text')


def _collapse_object_cell(v):
  """把"长得像对象/数组"的单元格折叠成可读展示值。

  整理 view.csv 时模型可能把 发起人/审批人 这类嵌套对象直接写成
  "{'userId': .., 'name': '张森', 'headimg': ..}" 或 JSON 字符串，
  右侧表格会整段显示。这里统一：字典取其中的名称字段（name/label/title…）只留名称；
  数组取其中各对象的名称用顿号连接。解析失败原样返回。
  """
  if not isinstance(v, str):
    return v
  s = v.strip()
  if not (s.startswith('{') and s.endswith('}') or s.startswith('[') and s.endswith(']')):
    return v
  try:
    import json
    obj = json.loads(s)
  except Exception:
    try:
      import ast
      obj = ast.literal_eval(s)
    except Exception:
      return v
  if isinstance(obj, dict):
    for key in _HUMAN_KEYS:
      val = obj.get(key)
      if isinstance(val, (str, int)) and str(val).strip():
        return val
    return ''  # 无名称字段的对象不再整段展示
  if isinstance(obj, list):
    names: list[str] = []
    for item in obj:
      if isinstance(item, dict):
        for key in _HUMAN_KEYS:
          if item.get(key):
            names.append(str(item[key]))
            break
      elif isinstance(item, (str, int)):
        names.append(str(item))
    return '、'.join(names) if names else ''
  return v


def _json_safe_value(v):
  """清洗单个值为 JSON 合法值：非有限 float（inf/NaN）→ None，NumPy 标量 → 原生类型。

  平台 CSV 里可能有 inf/超大值或 numpy 类型，FastAPI 默认 json（allow_nan=False）
  会抛 ValueError: Out of range float 导致 records 接口 500。
  额外把对象/JSON 单元格折叠为名称，避免右侧表格整段显示 JSON。
  """
  try:
    if isinstance(v, float):
      return v if math.isfinite(v) else None
  except TypeError:
    return None
  try:
    import numpy as np
    if isinstance(v, np.generic):
      v = v.item()
      if isinstance(v, float):
        return v if math.isfinite(v) else None
  except Exception:
    pass
  return _collapse_object_cell(v)


def _json_safe_row(rec: dict) -> dict:
  return {k: _json_safe_value(v) for k, v in rec.items()}


@router.post('/agent')
@require_permission('ai:agent')
async def agent_chat(
  request: Request,
  body: AgentRequest,
  session_id: str = Query(..., description='会话 ID'),
  group_id: str = Query('default', description='知识库组 ID'),
):
  """Agent 对话 SSE 端点。

  用户正常对话，Agent 自主决定是否调用工具（RAG 检索、DCIM 查询等）。
  """
  message = (body.message or '').strip()
  if not message:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '内容为空'})

  user_id = request.state.user_id
  user_perms: set[str] = getattr(request.state, 'user_permissions', set())

  # 校验会话所有权：不存在则自动创建，属于他人则拒绝
  from service.rag.conversation.session_service import session_service

  session = await session_service.get_session(session_id)
  if session is None:
    from models.rag.session_models import SessionBase
    session = await session_service.create_session(SessionBase(
      session_name=message[:30] or 'Agent 对话',
      user_id=user_id,
      group_id=group_id,
    ))
    if not session:
      return JSONResponse(status_code=500, content={'ok': False, 'msg': '创建会话失败'})
    session_id = session.session_id
  elif session.user_id != user_id:
    return JSONResponse(status_code=403, content={'ok': False, 'msg': '无权访问该会话'})

  # 稳定 thread_id（参照 DeerFlow thread_id 即 session 级别）。
  # Checkpointer 通过 add_messages reducer 自动管理消息累积，无需手动拼历史。
  # promoted 跨轮重置由 streaming.py 中 update_state 保证。
  thread_id = f'rz-agent-{session_id}'

  from service.agent.agent_service import AgentService
  agent_service = AgentService()

  return StreamingResponse(
    agent_service.stream(
      message=message,
      thread_id=thread_id,
      user_id=user_id,
      session_id=session_id,
      permissions=user_perms,
      interaction_mode=body.interaction_mode,
      task_mode=body.task_mode,
      plan_confirmed=body.plan_confirmed,
      plan=body.plan,
      interview_answers=body.interview_answers,
      interview_action=body.interview_action,
      uploaded_filenames=body.uploaded_filenames,
    ),
    media_type='text/event-stream; charset=utf-8',
    headers={
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  )


@router.post('/agent/uploads')
@require_permission('ai:agent')
async def upload_agent_files(
  request: Request,
  session_id: str = Query(..., description='会话 ID'),
  files: list[UploadFile] = File(...),
):
  """上传文件到 Agent 对话（会话级持久化，不跨会话共享）。

  鉴权：登录（全局 AuthMiddleware）+ `ai:agent` 权限 + 会话归属校验（同 agent_chat）。
  返回的 filename 供下一条消息的 AgentRequest.uploaded_filenames 引用，触发
  <current_uploads> 自动注入（见 streaming.py）。
  """
  if not files:
    return JSONResponse(status_code=400, content={'ok': False, 'msg': '未提供文件'})

  user_id = request.state.user_id

  # 会话归属校验：与 agent_chat 一致，不存在或属于他人均拒绝（上传不负责建会话）
  from service.rag.conversation.session_service import session_service
  session = await session_service.get_session(session_id)
  if session is None:
    return JSONResponse(status_code=404, content={'ok': False, 'msg': '会话不存在'})
  if session.user_id != user_id:
    return JSONResponse(status_code=403, content={'ok': False, 'msg': '无权访问该会话'})

  thread_id = f'rz-agent-{session_id}'

  from core.sandbox import uploads as upload_storage
  from core.sandbox import upload_extract

  results = []
  for file in files:
    if not file.filename:
      continue
    content = await file.read()
    try:
      info = upload_storage.save_upload(thread_id, file.filename, content)
    except upload_storage.UploadError as e:
      results.append({'filename': file.filename, 'error': str(e)})
      continue

    upload_path = upload_storage.get_upload_path(thread_id, info['filename'])
    outline = []
    extract_error = None
    if upload_path is not None:
      extracted = upload_extract.extract_content(upload_path)
      outline = extracted.get('outline') or []
      extract_error = extracted.get('error')

    results.append({
      'filename': info['filename'],
      'size': info['size'],
      'extension': info['extension'],
      'outline': outline,
      'extract_error': extract_error,
    })

  return JSONResponse(content={'ok': True, 'files': results})


@router.get('/agent/artifacts/{thread_id}/{dataset_id}/records')
@require_permission('ai:agent')
async def get_agent_artifact_records(
  thread_id: str,
  dataset_id: str,
  request: Request,
  file: str | None = Query(None, description='要读取的数据集内 csv 文件名（可选；默认 view.csv / data.csv）'),
):
  """返回数据集 records（JSON），供前端右侧可交互表格渲染。

  注意：本路由必须定义在 `{filename}` 下载路由**之前**，否则 /records 会被
  当文件名吞掉返回 404。data.csv 由 analysis_load 物化（pandas to_csv），
  这里读回转 JSON `{columns, records}`。鉴权同 get_agent_artifact：
  登录 + ai:agent + 会话归属。
  """
  prefix = 'rz-agent-'
  if not thread_id.startswith(prefix):
    raise HTTPException(404, '数据集不存在')
  session_id = thread_id[len(prefix):]
  from service.rag.conversation.session_service import session_service
  session = await session_service.get_session(session_id)
  if session is None or session.user_id != request.state.user_id:
    raise HTTPException(403, '无权访问')

  from core.sandbox import datasets as sandbox_datasets
  ds_dir = None
  if not sandbox_datasets._SAFE_RE.search(dataset_id):
    try:
      ds_dir = sandbox_datasets.load_dataset_dir(thread_id, dataset_id)
    except sandbox_datasets.DatasetError:
      ds_dir = None
  if ds_dir is None:
    ds_dir = sandbox_datasets.find_artifact_dir(thread_id, 'data.csv')
  if ds_dir is None:
    raise HTTPException(404, '数据集不存在')

  data_csv = (ds_dir / 'data.csv').resolve()
  if not data_csv.is_relative_to(ds_dir.resolve()) or not data_csv.is_file():
    raise HTTPException(404, '数据集不存在')
  # agent 用 analysis_exec 整理过可读视图（view.csv：中文表头/关键列/枚举解码）时优先展示，
  # 否则回退原始 data.csv。显式传 ?file=xxx.csv 时读该 csv（防模型漏发 records 卡、误把
  # 列表导出成 .csv 下载卡的兜底：前端可把任意数据集 csv 变回可查看表格）。
  csv_path = data_csv
  if file:
    target = (ds_dir / Path(file).name).resolve()
    if (target.is_relative_to(ds_dir.resolve()) and target.is_file()
        and target.suffix.lower() == '.csv'):
      csv_path = target
    else:
      raise HTTPException(404, '数据集不存在')
  else:
    view_csv = (ds_dir / 'view.csv').resolve()
    if view_csv.is_relative_to(ds_dir.resolve()) and view_csv.is_file():
      csv_path = view_csv

  import pandas as pd
  df = pd.read_csv(csv_path)
  from config.settings import settings
  if len(df) > settings.AGENT_ANALYSIS_MAX_ROWS:
    df = df.head(settings.AGENT_ANALYSIS_MAX_ROWS)
  records = df.where(pd.notnull(df), None).to_dict(orient='records')

  # 枚举码 → 中文解码（依据 meta 里的数据来源能力 + 列名匹配）。
  capability_id = None
  try:
    import json
    meta_path = (ds_dir / 'meta.json')
    if meta_path.is_file():
      capability_id = json.loads(meta_path.read_text(encoding='utf-8')).get('capability_id')
  except Exception:
    capability_id = None
  _decode_record_columns(capability_id, list(df.columns), records)

  # 数据可能含 inf/NaN/NumPy 标量，直接 json 序列化会 500（Out of range float），
  # 逐格清洗成合法 JSON 值（非有限 float → None；NumPy 标量 → 原生类型）。
  return {'columns': list(df.columns), 'records': [_json_safe_row(r) for r in records]}


@router.get('/agent/artifacts/{thread_id}/{dataset_id}/{filename}')
@require_permission('ai:agent')
async def get_agent_artifact(
  thread_id: str,
  dataset_id: str,
  filename: str,
  request: Request,
):
  """下载 agent 数据分析沙箱生成的产物文件。

  鉴权：登录（全局 AuthMiddleware）+ `ai:agent` 权限 + 会话归属校验。
  URL 由 analysis_exec 工具权威生成（/api/v1/chat/agent/artifacts/...）。
  """
  # 会话归属校验：thread_id = rz-agent-{session_id}（复用 agent_chat 的约定）
  prefix = 'rz-agent-'
  if not thread_id.startswith(prefix):
    raise HTTPException(404, '产物不存在')
  session_id = thread_id[len(prefix):]
  from service.rag.conversation.session_service import session_service
  session = await session_service.get_session(session_id)
  if session is None or session.user_id != request.state.user_id:
    raise HTTPException(403, '无权访问')

  # filename 只取 basename + resolve 校验仍在数据集目录内
  filename = Path(filename).name
  from core.sandbox import datasets as sandbox_datasets
  # 精确查找：dataset_id 合法 且 数据集存在时用精确目录；否则（LLM 编造 dataset_id）
  # 兜底按文件名在本人会话目录里反查最新产物——修复前端实时 404「看板加载失败」。
  ds_dir = None
  if not sandbox_datasets._SAFE_RE.search(dataset_id):
    try:
      ds_dir = sandbox_datasets.load_dataset_dir(thread_id, dataset_id)
    except sandbox_datasets.DatasetError:
      ds_dir = None
  if ds_dir is None:
    ds_dir = sandbox_datasets.find_artifact_dir(thread_id, filename)
  if ds_dir is None:
    raise HTTPException(404, '产物不存在')

  file_path = (ds_dir / filename).resolve()
  if not file_path.is_relative_to(ds_dir.resolve()) or not file_path.is_file():
    raise HTTPException(404, '产物不存在')

  media_type = _MEDIA_TYPES.get(file_path.suffix.lower(), 'application/octet-stream')
  disposition = 'attachment' if media_type in _ACTIVE_CONTENT_TYPES else 'inline'
  encoded = quote(file_path.name)
  return FileResponse(
    file_path,
    media_type=media_type,
    headers={'Content-Disposition': f"{disposition}; filename*=UTF-8''{encoded}"},
  )

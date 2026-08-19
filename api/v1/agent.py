"""
Agent 对话 API 端点（流式 SSE + 沙箱产物下载）
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
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
      plan_confirmed=body.plan_confirmed,
      plan=body.plan,
      interview_answers=body.interview_answers,
      interview_action=body.interview_action,
    ),
    media_type='text/event-stream; charset=utf-8',
    headers={
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  )


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

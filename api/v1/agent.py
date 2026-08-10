"""
Agent 对话 API 端点（流式 SSE）
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.rbac import require_permission
from models.agent_schemas import AgentRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/chat', tags=['agent'])


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

  # thread_id 用 session_id 保证同一会话的 Agent 状态持久化
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
    ),
    media_type='text/event-stream; charset=utf-8',
    headers={
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive',
      'X-Accel-Buffering': 'no',
    },
  )

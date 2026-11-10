"""对话窗口「讲解视频」的查询与播放端点（独立路由，不动旧的 /video/*）。

路由前缀 `/api/v1/chat/agent-video/` —— 与 agent 对话产物同族，前端在对话里以
视频卡片打开；任务数据在独立集合 `agent_video_tasks`，**不会**出现在表单页
`VideoPage` 的列表里（那套走 `video_tasks` + `/api/v1/video/*`，两边互不影响）。

鉴权：登录（全局 AuthMiddleware）+ `ai:video` 权限 + 会话归属校验（按 task 的
thread_id 反查 session.user_id）。
"""

import logging
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from config.settings import settings
from core.rbac import require_permission
from service.agent_video import service as video_service
from service.agent_video import store

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/chat/agent-video')



def _thread_of(session_id: str) -> str:
  return f'rz-agent-{session_id}'


async def _ensure_session_owned(session_id: str, request: Request) -> None:
  from service.rag.conversation.session_service import session_service
  session = await session_service.get_session(session_id)
  if session is None:
    raise HTTPException(404, '会话不存在')
  if session.user_id != request.state.user_id:
    raise HTTPException(403, '无权访问该会话')


async def _load_owned_task(task_id: str, request: Request) -> dict:
  """取任务并校验会话归属（不属于当前用户一律 403，与 agent 产物下载同一约定）。"""
  task = await store.get_task(task_id)
  if not task:
    raise HTTPException(404, '任务不存在')

  thread_id = task.get('thread_id', '')
  prefix = 'rz-agent-'
  if not thread_id.startswith(prefix):
    raise HTTPException(404, '任务不存在')

  await _ensure_session_owned(thread_id[len(prefix):], request)
  return task


@router.get('')
@require_permission('ai:video')
async def list_agent_video_tasks(
  request: Request,
  session_id: str = Query(..., description='会话 ID'),
):
  """列出本会话的讲解视频任务（前端自动盯进度 + 刷新后补视频卡片用）。"""
  await _ensure_session_owned(session_id, request)
  tasks = await video_service.list_session_tasks(_thread_of(session_id))
  return JSONResponse({'ok': True, 'tasks': tasks})


@router.get('/{task_id}')
@require_permission('ai:video')
async def get_agent_video_task(task_id: str, request: Request):
  """查任务状态（前端轮询用；agent 侧另有 video_status 工具）。"""
  task = await _load_owned_task(task_id, request)
  pages = task.get('pages') or []
  return JSONResponse({
    'ok': True,
    'task_id': task_id,
    'status': task['status'],
    'progress': task.get('progress', 0),
    'progress_text': task.get('progress_text', ''),
    'page_count': len(pages),
    'duration': task.get('duration', 0),
    'error': task.get('error', ''),
    'video_url': (video_service.sign_video_url(task_id)
                  if task['status'] == store.STATUS_SUCCESS else ''),
    'pages': [{'index': p['index'], 'status': p.get('status'),
               'duration': p.get('duration', 0)} for p in pages],
  })


@router.get('/{task_id}/video')
async def get_agent_video_file(
  task_id: str,
  request: Request,
  t: str = Query('', description='签名 token（服务端下发的播放链接自带；无需登录态即可播）'),
):
  """播放/下载合成好的 MP4（`Content-Type: video/mp4`，支持 Range 边下边播）。

  两条鉴权路径（**不用 @require_permission**：签名路径下中间件不放行任何登录态）：
  - 带 `?t=<签名 token>`：token 即授权（Redis 短期、TTL 1h、与 task_id 绑定），
    仍需**会话归属校验**——token 泄漏也只能看自己会话的视频
  - 无签名：走登录态（中间件注入的 user_id/permissions），要求 `ai:video` 且会话归属
  """
  if t:
    # 预签名语义：token（192 位随机、TTL 1h、与 task_id 绑定）即授权。签名请求没有登录态，
    # 无法也不该做"会话归属"判断——token 本身就是能力凭证；不猜、不崩。
    if video_service.resolve_video_url_token(t) != task_id:
      raise HTTPException(401, '播放链接无效或已过期，请刷新页面')
    task = await store.get_task(task_id)
    if not task:
      raise HTTPException(404, '任务不存在')
    # 若请求恰好带登录态（浏览器同源自动带的场景），额外按会话归属校验，多一层保险
    requester = getattr(request.state, 'user_id', None)
    if requester:
      await _ensure_session_owned(task.get('thread_id', '')[len('rz-agent-'):], request)
  else:
    perms: set = getattr(request.state, 'user_permissions', set())
    if 'ai:video' not in perms and '*' not in perms:
      raise HTTPException(403, '没有权限: ai:video')
    task = await _load_owned_task(task_id, request)
  video_path = task.get('video_path') or ''
  if task.get('status') != store.STATUS_SUCCESS or not video_path:
    raise HTTPException(409, f'视频尚未生成完成（当前状态：{task.get("status")}）')

  path = Path(video_path).resolve()
  # 只允许读会话数据目录内的文件（防路径穿越/读到库外文件）
  data_root = Path(settings.AGENT_ANALYSIS_DATA_DIR).resolve()
  if not path.is_file() or not path.is_relative_to(data_root):
    raise HTTPException(404, '视频文件不存在')

  filename = f"{Path(task.get('source_name') or 'video').stem}.mp4"
  # 文件名可能是中文（PPT 名），而 HTTP 头只能 latin-1 —— 直接塞中文会 UnicodeEncodeError
  # 让整个请求 500（实测踩过：中文名 PPT 的视频下载全挂）。按 RFC 5987 用 filename* 编码。
  from urllib.parse import quote
  return FileResponse(
    path, media_type='video/mp4', filename=None,
    headers={
      # 允许浏览器边下边播 + 拖动进度（ffmpeg 已用 +faststart 前置 moov）
      'Accept-Ranges': 'bytes',
      'Content-Disposition': f"inline; filename*=UTF-8''{quote(filename)}",
    },
  )

"""Video generation API routes.

Protected by ai:video permission. Users upload PPT files,
submit video generation tasks, and download the generated MP4.
"""

import os
import asyncio
from datetime import datetime

import httpx
from fastapi import APIRouter, UploadFile, File, Request, HTTPException, Form
from fastapi.responses import FileResponse, StreamingResponse

from config.settings import settings
import logging
from config.mongodb_conn import mongodb_manager
from config.redis_conn import redis_manager
from core.auth_engine import verify_access_token
from core.rbac import require_permission
from models.video import (
  VideoTask, VideoTaskPublic, VideoTaskStatus, VideoTaskParams,
)
from service.video.pipeline import run_video_pipeline

router = APIRouter()

logger = logging.getLogger(__name__)


@router.post('/video/tasks')
@require_permission('ai:video')
async def create_video_task(
  request: Request,
  file: UploadFile = File(...),
  aspect_ratio: str = Form('16:9'),
  resolution: str = Form('1080p'),
  voice: str = Form('neutral'),
  subtitle_style: str = Form('default'),
):
  """Upload a PPT file and create a video generation task."""
  # Validate file extension
  filename = file.filename or 'untitled.pptx'
  ext = os.path.splitext(filename)[1].lower()
  if ext not in ('.ppt', '.pptx'):
    raise HTTPException(400, '仅支持 .ppt / .pptx 文件')

  # Save uploaded file
  os.makedirs(settings.UPLOAD_DIR, exist_ok=True)
  import uuid
  file_id = uuid.uuid4().hex
  saved_name = f'{file_id}{ext}'
  file_path = os.path.join(settings.UPLOAD_DIR, saved_name)
  content = await file.read()
  with open(file_path, 'wb') as f:
    f.write(content)

  user = request.state.current_user
  task = VideoTask(
    user_id=user['user_id'],
    user_phone=user.get('phone', ''),
    original_filename=filename,
    file_path=file_path,
    params=VideoTaskParams(
      aspect_ratio=aspect_ratio,
      resolution=resolution,
      voice=voice,
      subtitle_style=subtitle_style,
    ),
  )

  db = mongodb_manager.db
  await db.video_tasks.insert_one(task.model_dump())

  # Launch pipeline in background
  asyncio.create_task(run_video_pipeline(task.task_id))

  logger.info(f'Video task created: {task.task_id} by {user["phone"]}')
  return {'ok': True, 'data': {'task_id': task.task_id}}


@router.get('/video/tasks')
@require_permission('ai:video')
async def list_video_tasks(request: Request):
  """List the current user's video generation tasks."""
  user_id = request.state.user_id
  db = mongodb_manager.db
  cursor = db.video_tasks.find({'user_id': user_id}).sort('created_at', -1)
  tasks = []
  async for doc in cursor:
    tasks.append(VideoTaskPublic(
      task_id=doc['task_id'],
      original_filename=doc.get('original_filename', ''),
      params=doc.get('params', {}),
      status=doc['status'],
      progress=doc.get('progress', 0),
      progress_text=doc.get('progress_text', ''),
      result_url=doc.get('result_url'),
      error=doc.get('error'),
      created_at=doc['created_at'],
    ))
  return {'ok': True, 'data': [t.model_dump(mode='json') for t in tasks]}


@router.get('/video/tasks/{task_id}')
@require_permission('ai:video')
async def get_video_task(task_id: str, request: Request):
  """Get a single task's details."""
  db = mongodb_manager.db
  doc = await db.video_tasks.find_one({'task_id': task_id})
  if not doc:
    raise HTTPException(404, '任务不存在')
  if doc['user_id'] != request.state.user_id:
    raise HTTPException(403, '无权查看此任务')

  return {
    'ok': True,
    'data': VideoTaskPublic(
      task_id=doc['task_id'],
      original_filename=doc.get('original_filename', ''),
      params=doc.get('params', {}),
      status=doc['status'],
      progress=doc.get('progress', 0),
      progress_text=doc.get('progress_text', ''),
      result_url=doc.get('result_url'),
      error=doc.get('error'),
      created_at=doc['created_at'],
    ).model_dump(mode='json'),
  }


@router.get('/video/tasks/{task_id}/video')
@require_permission('ai:video')
async def get_video_file(task_id: str, request: Request):
  """Stream/download the generated video file."""
  db = mongodb_manager.db
  doc = await db.video_tasks.find_one({'task_id': task_id})
  if not doc:
    raise HTTPException(404, '任务不存在')
  if doc['user_id'] != request.state.user_id:
    raise HTTPException(403, '无权访问')
  if doc.get('status') != VideoTaskStatus.SUCCESS:
    raise HTTPException(400, '视频尚未生成完成')

  result_url = doc.get('result_url')
  if not result_url:
    raise HTTPException(400, '视频地址为空')

  # If result_url is a local file path, serve it directly
  if os.path.isfile(result_url):
    return FileResponse(result_url, media_type='video/mp4',
                        filename=f'{task_id}.mp4')

  # Proxy remote video with Content-Length from the source, so the browser
  # can show proper duration and all <video> controls work.
  async with httpx.AsyncClient(timeout=30) as client:
    head = await client.head(result_url)
    head.raise_for_status()
    file_size = int(head.headers.get('Content-Length', 0))
    accept_ranges = head.headers.get('Accept-Ranges', '')

  async def stream_remote():
    async with httpx.AsyncClient(timeout=300) as client:
      async with client.stream('GET', result_url) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes(chunk_size=65536):
          yield chunk

  headers = {'Content-Disposition': f'inline; filename="{task_id}.mp4"'}
  if file_size:
    headers['Content-Length'] = str(file_size)
  if accept_ranges:
    headers['Accept-Ranges'] = accept_ranges

  return StreamingResponse(stream_remote(), media_type='video/mp4',
                           headers=headers)


@router.delete('/video/tasks/{task_id}')
@require_permission('ai:video')
async def delete_video_task(task_id: str, request: Request):
  """Delete a video task and its generated files."""
  db = mongodb_manager.db
  # 原子操作：先查找并删除，防止并发删除竞态
  doc = await db.video_tasks.find_one_and_delete({
    'task_id': task_id,
    'user_id': request.state.user_id,
  })
  if not doc:
    # 检查是任务不存在还是无权删除
    existing = await db.video_tasks.find_one({'task_id': task_id})
    if existing:
      raise HTTPException(403, '无权删除此任务')
    raise HTTPException(404, '任务不存在')

  # 清理文件（文件清理失败不影响 DB 已删除的结果）
  file_path = doc.get('file_path')
  if file_path and os.path.isfile(file_path):
    try:
      os.remove(file_path)
    except OSError:
      pass
  result_url = doc.get('result_url')
  if result_url and os.path.isfile(result_url):
    try:
      os.remove(result_url)
    except OSError:
      pass

  logger.info(f'Video task deleted: {task_id} by user {request.state.user_id}')
  return {'ok': True}


@router.get('/video/audio/{filename}')
async def serve_audio_file(filename: str):
  """Serve TTS audio files. Called internally by the renderer plugin."""
  import re
  safe = re.sub(r'[^a-zA-Z0-9_.-]', '', filename)
  file_path = os.path.join(settings.UPLOAD_DIR, 'tts', safe)
  if not os.path.isfile(file_path):
    raise HTTPException(404, '音频文件不存在')
  return FileResponse(file_path, media_type='audio/mpeg')

"""定时任务执行：复用 AgentService().stream() 跑一轮并记录结果"""

import json
import logging
import re
import time
from datetime import datetime

from config.settings import settings
from models.scheduled_task import RunStatus
from service.scheduled_task.repository import create_run, finish_run

logger = logging.getLogger(__name__)

# 与 service/agent/streaming.py 的 <file> 标签解析一致
_FILE_TAG_RE = re.compile(r'<file\b([^>]*)/>')
_FILE_URL_RE = re.compile(r'url="([^"]*)"')

_AGENT_ARTIFACT_PREFIX = '/api/v1/chat/agent/artifacts/'


async def run_task(task) -> None:
  """执行一个定时任务：调 Agent 跑一轮，捕获最终回复 + 产物 URL，写运行记录。"""
  from service.agent.agent_service import AgentService

  run = await create_run(task.task_id)
  thread_id = _thread_id_for(task, run.run_id)
  session_id = thread_id.removeprefix('rz-agent-')

  final_text = ''
  error = ''
  start = time.time()
  try:
    svc = AgentService()
    async for chunk in svc.stream(
      message=task.prompt,
      thread_id=thread_id,
      user_id=settings.SCHEDULED_TASK_SERVICE_USER_ID,
      session_id=session_id,
      permissions=set(_split_permissions()),
      task_mode=task.task_mode,
    ):
      payload = _parse_sse(chunk)
      if payload is None:
        continue
      event = payload.get('event')
      data = payload.get('data') or {}
      if event == 'text':
        final_text += data.get('content', '')
      elif event == 'error':
        error = data.get('message', '')
  except Exception as e:
    logger.exception('定时任务执行失败 task_id=%s', task.task_id)
    error = str(e)

  duration_ms = int((time.time() - start) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await finish_run(
    run.run_id,
    status=status,
    final_reply=final_text,
    artifact_urls=_extract_artifact_urls(final_text),
    error=error,
    duration_ms=duration_ms,
  )
  logger.info('定时任务执行完成 task_id=%s status=%s duration_ms=%s',
              task.task_id, status, duration_ms)


def _split_permissions() -> list[str]:
  return [p.strip() for p in settings.SCHEDULED_TASK_SERVICE_PERMISSIONS.split(',') if p.strip()]


def _thread_id_for(task, run_id: str) -> str:
  if task.thread_strategy == 'reuse':
    return f'rz-agent-sched-{task.task_id}'
  return f'rz-agent-sched-{task.task_id}-{run_id}'


def _parse_sse(chunk: str) -> dict | None:
  line = chunk.strip()
  if not line.startswith('data:'):
    return None
  try:
    return json.loads(line[5:].strip())
  except Exception:
    return None


def _extract_artifact_urls(text: str) -> list[str]:
  """从最终回复里提取 <file url="..."/> 中的产物 URL（去重，保留顺序）。"""
  urls: list[str] = []
  seen: set[str] = set()
  for m in _FILE_TAG_RE.finditer(text):
    url_m = _FILE_URL_RE.search(m.group(1))
    if url_m:
      url = url_m.group(1)
      if url.startswith(_AGENT_ARTIFACT_PREFIX) and url not in seen:
        seen.add(url)
        urls.append(url)
  return urls

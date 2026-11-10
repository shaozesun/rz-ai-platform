"""定时任务执行：复用 AgentService().stream() 跑一轮并记录结果"""

import asyncio
import json
import logging
import random
import re
import time
import uuid

from config.settings import settings
from models.scheduled_task import RunStatus
from service.scheduled_task.repository import create_notification, create_run, finish_run

logger = logging.getLogger(__name__)

# 与 service/agent/streaming.py 的 <file> 标签解析一致
_FILE_TAG_RE = re.compile(r'<file\b([^>]*)/>')
_FILE_URL_RE = re.compile(r'url="([^"]*)"')

_AGENT_ARTIFACT_PREFIX = '/api/v1/chat/agent/artifacts/'

# 可重试的错误特征。上游（Qwen3.5 端点）对**长流式生成**不稳：实测 7 轮报告里 4 轮中途
# 卡死（`StreamChunkTimeoutError`：120s 无 chunk，卡死点随机）。报告无人值守，抖一次就丢一份，
# 故对这类**瞬时**错误自动重试；权限/参数这类确定性错误不重试（重试只是浪费时间）。
#
# `回复生成中断` 必须有：它是 `streaming.py:738` 的**兜底分支**，网络层异常
# （实测 `httpcore.ReadError`——连接挂了几十分钟后被上游断开）没有专门的短路分支，
# 就落在这里。原先只登记了 `流式响应中断`（:687，StreamChunkTimeout 那条），
# 于是最容易发生的那种断流被判成"确定性错误"、整份报告直接丢（2026-10-14 实测）。
_RETRYABLE_MARKERS = (
  '流式响应中断', '回复生成中断', 'StreamChunkTimeout', 'TimeoutError',
  '系统繁忙', '502', '503', '504',
)


def _is_retryable(error: str) -> bool:
  return any(m in (error or '') for m in _RETRYABLE_MARKERS)


def _retry_budget(task) -> int:
  """重试次数：**只有预设报告**才有（用户自定义任务失败时主人看得见，重试反而让手动触发变慢）。"""
  from service.scheduled_task.report_templates import is_preset_key

  if not is_preset_key(getattr(task, 'template_id', '') or ''):
    return 0
  return max(0, settings.SCHEDULED_TASK_REPORT_RETRY)


async def run_task(task) -> None:
  """执行一个定时任务：调 Agent 跑一轮，捕获最终回复 + 产物 URL，写运行记录。

  预设报告会按 `SCHEDULED_TASK_REPORT_RETRY` 对**可重试错误**自动重试：每次换新 thread
  （卡死的会话留了半截状态），最终把成功那次的 thread 写进运行记录，供「查看对话」。
  """
  # 事件统计表预设（`incident__…`）：纯确定性流水线，不建 agent、不调 LLM
  if (getattr(task, 'template_id', '') or '').startswith('incident__'):
    from service.scheduled_task.incident_stats.workflow import run_incident_task

    await run_incident_task(task)
    return

  # 告警不合规检查预设（`compliance__…`）：同样是确定性流水线
  if (getattr(task, 'template_id', '') or '').startswith('compliance__'):
    from service.scheduled_task.compliance.workflow import run_compliance_task

    await run_compliance_task(task)
    return

  # 文档更新检查预设（`doc_update__…`）：确定性快照比对，不建 agent、不调 LLM
  if (getattr(task, 'template_id', '') or '').startswith('doc_update__'):
    from service.doc_update.workflow import run_doc_update_task

    await run_doc_update_task(task)
    return

  # 登记了确定性 spec 的预设（如事件工单）走工作流：取数/统计/出图/排版全平台完成，
  # 只留一次无工具的 LLM 调用来写解读 —— 不再付 agent 那 ~29.5k tokens 的固定开销与多轮 prefill。
  from service.scheduled_task.report import get_report_spec

  spec = get_report_spec(getattr(task, 'template_id', '') or '')
  if spec is not None:
    from service.scheduled_task.report.workflow import run_report_workflow

    await run_report_workflow(task, spec)
    return

  from service.agent.agent_service import AgentService

  run_id = uuid.uuid4().hex
  thread_id = _thread_id_for(task, run_id)
  # 报告期号：平台按窗口算好落库（前端「期刊目录」按期分组/筛选要用，不必解析文件名）
  preset_key = getattr(task, 'template_id', '') or ''
  period_key, period_label = _period_of(preset_key)
  run = await create_run(
    task.task_id, run_id=run_id, thread_id=thread_id,
    preset_key=preset_key, period_key=period_key, period_label=period_label,
  )
  session_id = thread_id.removeprefix('rz-agent-')

  final_text = ''
  error = ''
  start = time.time()
  try:
    svc = AgentService()
    # 分析报告：**窗口在平台算好、前置注入到指令里** —— 实测模型自行推算"上一个完整自然周"
    # 会错一整周（今天 10-12 周一，应取 10-05~10-11，取了 09-28~10-04），且图题与正文不一致。
    # 非预设任务 window_hint 返回空串，行为完全不变。
    from service.scheduled_task.report_templates import window_hint
    hint = window_hint(getattr(task, 'template_id', '') or '')
    message = f'{hint}\n\n{task.prompt}' if hint else task.prompt
    async for chunk in svc.stream(
      message=message,
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

  # 预设报告：对**可重试错误**自动重试（换新 thread，避免沿用卡死会话的半截状态）。
  # 让无人值守的报告不会因为上游抖一次就整份丢掉；用户自定义任务不重试（主人看得见失败，
  # 且重试会让"手动触发"变慢）。最终把**成功那次的 thread** 写进运行记录供「查看对话」。
  retried = 0
  budget = _retry_budget(task)
  while error and retried < budget and _is_retryable(error):
    retried += 1
    wait = settings.SCHEDULED_TASK_REPORT_RETRY_DELAY + random.uniform(0, 5)
    logger.warning('预设报告遇可重试错误，%0.1fs 后重试（第 %d 次）task_id=%s error=%s',
                   wait, retried, task.task_id, error[:120])
    await asyncio.sleep(wait)
    attempt_thread = f'rz-agent-sched-{task.task_id}-{run_id}-r{retried}'
    attempt_session = attempt_thread.removeprefix('rz-agent-')
    final_text, error = '', ''
    start = time.time()
    try:
      svc = AgentService()
      async for chunk in svc.stream(
        message=message,
        thread_id=attempt_thread,
        user_id=settings.SCHEDULED_TASK_SERVICE_USER_ID,
        session_id=attempt_session,
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
      logger.exception('定时任务重试失败 task_id=%s', task.task_id)
      error = str(e)
    duration_ms += int((time.time() - start) * 1000)
    if not error:
      thread_id, session_id = attempt_thread, attempt_session

  if retried and error:
    error = f'{error}（已自动重试 {retried} 次）'

  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await finish_run(
    run.run_id,
    status=status,
    final_reply=final_text,
    artifact_urls=_extract_artifact_urls(final_text),
    error=error,
    duration_ms=duration_ms,
    thread_id=thread_id,   # 成功那次（重试过就是重试的 thread），供「查看对话」
  )
  await create_notification(
    user_id=task.created_by,
    task_id=task.task_id,
    task_name=task.name,
    run_id=run.run_id,
    status=status.value,
    title=f"定时任务「{task.name}」执行{'成功' if status == RunStatus.SUCCESS else '失败'}",
    body=(f'（首次遇上游中断，已自动重试成功）{final_text}' if retried and not error
          else (error or final_text)).strip()[:80],
  )
  logger.info('定时任务执行完成 task_id=%s status=%s duration_ms=%s retried=%d',
              task.task_id, status, duration_ms, retried)


def _period_of(preset_key: str) -> tuple[str, str]:
  """预设 → （期号 key, 期号文案）。实现已挪到 `report_templates.period_of`，
  与确定性报告工作流共用一份，避免两处各写一套日期推算。"""
  from service.scheduled_task.report_templates import period_of

  return period_of(preset_key)


def _split_permissions() -> list[str]:
  return [p.strip() for p in settings.SCHEDULED_TASK_SERVICE_PERMISSIONS.split(',') if p.strip()]


def _thread_id_for(task, run_id: str) -> str:
  if task.thread_strategy == 'reuse':
    # 对话建任务时可指定复用某个会话（带上下文）；否则用任务专属固定 thread
    return task.thread_id or f'rz-agent-sched-{task.task_id}'
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

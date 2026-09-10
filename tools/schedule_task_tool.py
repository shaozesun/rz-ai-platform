"""schedule_task 工具 — 用户通过对话用自然语言创建/管理定时任务（执行指令自定义）。

复刻 DeerFlow schedule_task tool 的思路，适配我们的单例 Agent + 已建好的
service/scheduled_task/ 持久化：
- 用户在对话里说「每天8点生成巡检报告」「每2小时汇总告警」「3分钟后提醒我」，
  LLM 把自然语言解析成调度参数（schedule_type + cron_expr/interval_seconds/once_at）。
- 按用户隔离：created_by 取 core/agent/user_context.get_current_user_id()。
- 权限：工具内校验 scheduled_task:manage（guardrail 只认 ai:agent，需显式校验）。
- 同名限制：当前用户已存在同名任务则拒绝（避免 delete/pause 按名匹配歧义）。
"""

import json
import logging
from datetime import datetime
from typing import Literal

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from core.agent.middlewares.guardrail import get_current_permissions
from core.agent.user_context import get_current_user_id
from models.scheduled_task import ScheduledTaskCreate, ScheduledTaskUpdate
from service.scheduled_task import repository as tasks_repo

logger = logging.getLogger(__name__)


@tool
async def schedule_task(
  action: Literal['create', 'list', 'delete', 'pause', 'resume'],
  name: str = '',
  prompt: str = '',
  schedule_type: str = 'interval',
  cron_expr: str = '',
  interval_seconds: int = 0,
  once_at: str = '',
  task_mode: str = 'normal',
  thread_strategy: str = 'new',
  config: RunnableConfig = None,
) -> str:
  """创建、查看、启停、删除定时任务（到点自动执行一轮 Agent，执行指令由用户自定义，如定时报告、巡检、数据汇总等，不限于此）。

  用户用自然语言表达定时需求时，把它解析为调度参数并调用本工具：
  - 一次性：schedule_type='once'，once_at 填 ISO 时间（如需要当前时间，可先调用
    get_current_time 获取）；例如「3分钟后提醒我」→ once_at = 当前时间+3分钟；
  - 间隔：schedule_type='interval'，interval_seconds 填秒数（3600=每小时、
    7200=每2小时、86400=每天）；
  - cron：schedule_type='cron'，cron_expr 填 cron 表达式（'0 8 * * *'=每天8点、
    '0 9 * * 1'=每周一9点、'*/30 * * * *'=每30分钟）。
  prompt 为到点后喂给 Agent 的执行指令（如「查询昨日各机房告警并生成巡检报告」），
  应把用户意图写成完整、自包含的一句话。task_mode 默认 normal，数据类报告可用
  full_analysis。thread_strategy：'new' 每次新会话 / 'reuse' 复用当前会话上下文。

  Args:
    action: create（新建）/ list（列出我的任务）/ delete（删除）/ pause（停用）/
      resume（启用）。delete/pause/resume 用 name 定位。
    name: 任务名（当前用户内唯一）。
    prompt: create 时必填，到点执行的任务指令。
    schedule_type: 'once' | 'interval' | 'cron'。
    cron_expr: cron 模式必填。
    interval_seconds: interval 模式必填（秒）。
    once_at: once 模式必填（ISO 8601）。
    task_mode: 'normal' | 'full_analysis'。
    thread_strategy: 'new' | 'reuse'。
  """
  if 'scheduled_task:manage' not in get_current_permissions():
    return json.dumps({'error': '无权限：需要 scheduled_task:manage 权限才能管理定时任务'}, ensure_ascii=False)

  user_id = get_current_user_id()
  if not user_id:
    return json.dumps({'error': '无法识别当前用户身份，定时任务仅对登录用户开放'}, ensure_ascii=False)

  tid = (config.get('configurable') or {}).get('thread_id') if config else None

  try:
    if action == 'list':
      tasks = await tasks_repo.list_tasks_by_creator(user_id)
      items = [{
        'name': t.name,
        'schedule': _schedule_text(t),
        'enabled': t.enabled,
        'next_run_at': t.next_run_at.isoformat() if t.next_run_at else None,
      } for t in tasks]
      return json.dumps({'tasks': items}, ensure_ascii=False)

    if action == 'create':
      if not name.strip() or not prompt.strip():
        raise ValueError('create 需要 name 和 prompt')
      if await tasks_repo.get_task_by_name_and_creator(name.strip(), user_id):
        raise ValueError(f'已存在同名任务「{name.strip()}」，请换一个名称或先删除')
      create_data = _build_create(
        name, prompt, schedule_type, cron_expr, interval_seconds, once_at,
        task_mode, thread_strategy, tid,
      )
      task = await tasks_repo.create_task(create_data, created_by=user_id)
      return json.dumps({
        'ok': True, 'task_id': task.task_id, 'name': task.name,
        'schedule': _schedule_text(task),
        'next_run_at': task.next_run_at.isoformat() if task.next_run_at else None,
      }, ensure_ascii=False)

    # delete / pause / resume：按 name 定位当前用户的任务
    if not name.strip():
      raise ValueError(f'{action} 需要 name')
    task = await tasks_repo.get_task_by_name_and_creator(name.strip(), user_id)
    if not task:
      raise ValueError(f'未找到任务「{name.strip()}」')
    if action == 'delete':
      await tasks_repo.delete_task(task.task_id)
      return json.dumps({'ok': True, 'message': f'任务「{task.name}」已删除'}, ensure_ascii=False)
    enabled = action == 'resume'
    await tasks_repo.update_task(task.task_id, ScheduledTaskUpdate(enabled=enabled))
    return json.dumps({
      'ok': True, 'message': f'任务「{task.name}」已{"启用" if enabled else "停用"}',
    }, ensure_ascii=False)

  except ValueError as e:
    return json.dumps({'error': str(e)}, ensure_ascii=False)


def _build_create(
  name: str, prompt: str, schedule_type: str, cron_expr: str, interval_seconds: int,
  once_at: str, task_mode: str, thread_strategy: str, tid: str | None,
) -> ScheduledTaskCreate:
  schedule_type = (schedule_type or 'interval').lower()
  if schedule_type not in ('once', 'interval', 'cron'):
    raise ValueError(f'不支持的调度类型：{schedule_type}')

  once_at_dt = None
  if schedule_type == 'once':
    try:
      once_at_dt = datetime.fromisoformat(once_at) if once_at else None
    except ValueError:
      raise ValueError(f'无法解析时间：{once_at}（请用 ISO 8601 格式）')
    if once_at_dt is None:
      raise ValueError('once 模式需要 once_at')
  if schedule_type == 'interval' and not (interval_seconds and interval_seconds > 0):
    raise ValueError('interval 模式需要 interval_seconds > 0')
  if schedule_type == 'cron':
    if not cron_expr.strip():
      raise ValueError('cron 模式需要 cron_expr')
    # 用 compute_next_run 验证表达式合法性（croniter 无效会抛异常）
    from service.scheduled_task.repository import compute_next_run
    from models.scheduled_task import ScheduleType
    try:
      compute_next_run(ScheduleType.CRON, None, None, None, cron_expr)
    except Exception:
      raise ValueError(f'无效的 cron 表达式：{cron_expr}')

  return ScheduledTaskCreate(
    name=name.strip(),
    prompt=prompt.strip(),
    schedule_type=schedule_type,
    cron_expr=cron_expr.strip() if schedule_type == 'cron' else None,
    interval_seconds=interval_seconds if schedule_type == 'interval' else None,
    once_at=once_at_dt,
    task_mode=task_mode or 'normal',
    thread_strategy=thread_strategy or 'new',
    thread_id=tid if thread_strategy == 'reuse' else None,
  )


def _schedule_text(task) -> str:
  if task.schedule_type == 'once':
    return f'一次性 {task.once_at.isoformat() if task.once_at else ""}'
  if task.schedule_type == 'interval':
    s = task.interval_seconds or 0
    if s % 3600 == 0:
      return f'每 {s // 3600} 小时'
    if s % 60 == 0:
      return f'每 {s // 60} 分钟'
    return f'每 {s} 秒'
  return f'Cron {task.cron_expr}'

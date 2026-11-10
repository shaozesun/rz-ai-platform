"""定时任务管理 API（定时报告/巡检）

覆盖：任务定义 CRUD、手动触发、运行历史、任务级产物下载。
权限：scheduled_task:manage（管理员角色预置）。响应统一 {ok, data} 包装（对齐项目约定）。
"""

import asyncio
import mimetypes
import re

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from core.rbac import require_permission
from models.rag.message_models import MessageListResponse
from models.scheduled_task import (
  ScheduledTaskCreate,
  ScheduledTaskUpdate,
)
from service.rag.conversation.message_service import message_service
from service.scheduled_task import repository as tasks_repo
from service.scheduled_task.runner import run_task

router = APIRouter(prefix='/scheduled-tasks', tags=['scheduled-tasks'])


async def _owned_task(task_id: str, user_id: str):
  """取「当前用户自己的」任务：不存在 → 404，存在但不是本人建的 → 403。

  **定时任务按用户完全隔离**（和视频生成页一致）：任何 task_id 入口（查看/改/删/触发/
  运行历史/运行对话/产物下载）都必须先过这里。此前只判"任务存在"，于是任何人拿到
  task_id 就能看别人的运行对话与产物（2026-10-10 修）。
  """
  task = await tasks_repo.get_task(task_id)
  if not task:
    raise HTTPException(404, '任务不存在')
  if task.created_by != user_id:
    raise HTTPException(403, '无权访问此任务')
  return task


@router.post('')
@require_permission('scheduled_task:manage')
async def create_task(body: ScheduledTaskCreate, request: Request):
  user_id = getattr(request.state, 'user_id', '') or 'system'
  task = await tasks_repo.create_task(body, created_by=user_id)
  return {'ok': True, 'data': task.model_dump(mode='json')}


@router.get('')
@require_permission('scheduled_task:manage')
async def list_tasks(request: Request, offset: int = 0, limit: int = Query(100, le=500)):
  """当前用户自己的定时任务（**用户隔离**，不列他人的）。"""
  user_id = getattr(request.state, 'user_id', '')
  tasks = await tasks_repo.list_tasks(user_id, limit=limit, offset=offset)
  return {'ok': True, 'data': [t.model_dump(mode='json') for t in tasks]}


@router.get('/notifications')
async def list_notifications(request: Request, limit: int = Query(20, le=50)):
  """当前用户的任务完成通知（铃铛轮询用，仅靠 middleware 登录校验，普通用户为空）。"""
  user_id = getattr(request.state, 'user_id', '')
  if not user_id:
    return {'ok': True, 'data': []}
  items = await tasks_repo.list_notifications(user_id, limit=limit)
  return {'ok': True, 'data': [i.model_dump(mode='json') for i in items]}


# ==================== 分析报告（系统预设 · 共享只读）====================
# 报告是**系统级共享产出**（类似新闻）：全平台只跑一份、所有人看同一份，用户不参与配置
# （没有新建/启停/删除）。因此这一组是**只读**端点，权限是 ai:agent（不是管理员级的
# scheduled_task:manage），并且**不做按人隔离**——数据本身就是公司全部门的公共口径。
# 注意：必须声明在 `/{task_id}` 之前，否则会被路径参数吃掉。

async def _preset_tasks() -> dict[str, dict]:
  """预设 key → 对应的系统任务文档（**从库里取**：园区是运行时数据，不能再对着代码常量查）。"""
  from config.mongodb_conn import mongodb_manager
  from service.scheduled_task import report_templates as report_tpl
  from service.scheduled_task.repository import SYSTEM_USER_ID, TASKS_COLLECTION

  cursor = mongodb_manager.db[TASKS_COLLECTION].find({
    'created_by': SYSTEM_USER_ID, 'enabled': True,
    'template_id': {'$nin': [None, '']},
  })
  out: dict[str, dict] = {}
  async for d in cursor:
    if report_tpl.is_preset_key(d.get('template_id', '')):
      out[d['template_id']] = d
  return out


def _teaser(final_reply: str, limit: int = 110) -> str:
  """从运行记录里取**一句摘要预览**，给列表当 excerpt。

  列表**不能**回 `final_reply` 原文（整轮过程文本、动辄数 KB）。但一个只有元信息的目录
  读起来像日志 —— 调研过的"期刊/归档"类列表（newsletter archive、issue index）里，
  **每一条都带一句 one-line excerpt**，这是它们能扫视的关键。所以只取一句：

  优先取**要点行**（`-`/`•` 开头）—— 报告工作流写入的第一条执行摘要信息量最大；
  没有要点时退回第一条非抬头行（跳过「《…》已生成（…）」这种样板句），仍取不到就留空。
  """
  clean = re.sub(r'<file\b[^>]*/>', '', final_reply or '')
  lines = [ln.strip() for ln in clean.splitlines() if ln.strip()]
  for ln in lines:
    if ln[0] in '-•·*':
      return ln.lstrip('-•·* ').strip()[:limit]
  for ln in lines:
    if not ln.startswith('《') and not ln.startswith('完整报告'):
      return ln[:limit]
  return ''


@router.get('/presets')
@require_permission('ai:agent')
async def list_report_presets(
  request: Request,
  offset: int = Query(0, ge=0),
  limit: int = Query(20, ge=1, le=200),
  ticket_type: str = Query('', description='按工单类型筛：change/breakdown/question/event/threshold'),
  space: str = Query('', description='按空间筛：nationwide / by_park'),
  period: str = Query('', description='按周期筛：weekly / monthly'),
  park: str = Query('', description='按园区筛：nationwide 或园区短名（如 廊坊）'),
  days: int = Query(0, ge=0, le=3650, description='只要最近 N 天的产出；0=不限'),
):
  """预设清单 + **按期倒序的产出记录（分页/筛选）**，供报告页做「期刊目录」。

  产出项**只回元信息**（期号/时间/状态/产物…），**不回 `final_reply`** ——
  那是整轮过程文本、且动辄数 KB，列表用不上（详情看产物 PDF）。
  """
  from datetime import datetime, timedelta, timezone

  from models.scheduled_task import _ensure_utc
  from service.scheduled_task import report_templates as report_tpl

  tasks = await _preset_tasks()
  # 预设清单**由库驱动**（园区是运行时发现的）；筛选（类型/园区/空间/周期）同时作用于清单与产出
  presets = []
  keys: list[str] = []
  meta_by_key: dict[str, dict] = {}      # 顺带留给下面给产出补「工单类型/空间」用
  for key, task in sorted(tasks.items()):
    meta = report_tpl.preset_meta(key, task.get('name', ''))
    if not meta:
      continue
    meta_by_key[key] = meta
    if ticket_type and meta['ticket_type'] != ticket_type:
      continue
    if space and meta['space'] != space:
      continue
    if period and meta['period'] != period:
      continue
    if park and meta['park'] != park:
      continue
    keys.append(key)
    presets.append({
      **meta,
      'task_id': task.get('task_id', ''),
      'enabled': bool(task.get('enabled')),
      # Mongo 里是 naive UTC：不补时区，前端 new Date() 会按本地时间解析 → 显示差 8 小时
      'next_run_at': _ensure_utc(task.get('next_run_at')),
    })
  task_ids = [t['task_id'] for k, t in tasks.items() if k in keys and t.get('task_id')]
  since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
  # 注意：**按 task_id 过滤就够了**（每个预设 = 一条系统任务，1:1）——不要额外按
  # `preset_key` 过滤：那个字段是 2026-10-13 才加的，历史 run 没有它，会被整批漏掉。
  runs, total = await tasks_repo.list_runs_for_tasks(
    task_ids, limit=limit, offset=offset, since=since,
  )
  # 按状态的**全量**计数（不受分页影响）：筛选条要显示「✓x ✗y」，
  # 让前端数"已加载那一页"会与 total 自相矛盾（实测 26 份时只能数出 2 条）。
  status_counts = await tasks_repo.count_runs_by_status(task_ids, since=since)
  key_of = {t['task_id']: k for k, t in tasks.items() if t.get('task_id')}
  name_of = {k: t.get('name', '') for k, t in tasks.items()}
  runs_out = []
  for r in runs:
    key = r.preset_key or key_of.get(r.task_id, '')
    meta = meta_by_key.get(key) or {}
    runs_out.append({
      'run_id': r.run_id, 'status': r.status.value, 'triggered_at': r.triggered_at.isoformat(),
      'duration_ms': r.duration_ms, 'error': r.error, 'artifact_urls': r.artifact_urls,
      'preset_key': key,
      'preset_name': name_of.get(key, ''),
      # 拆分字段供列表拼行标题用：前端渲染「工单类型 · 空间 · 期号」（报告名/期号整串
      # 在一条记录里拼好，避免前端再解析 preset_key）。
      'ticket_name': meta.get('ticket_name', ''),
      'space_label': meta.get('space_label', ''),
      'period_key': r.period_key,
      'period_label': r.period_label or meta.get('period_label', ''),
      # 周期类型（每周/每月）：`period_label` 是**期号**（2026 第 41 周），列表行标题
      # 需要的是**频率**（每周/每月）—— 两者同名不同义，别混用
      'period_type_label': meta.get('period_label', ''),
      # 一句话摘要（见 `_teaser`）：列表每行要有内容预览才扫得动，不给整段 final_reply
      'teaser': _teaser(r.final_reply),
    })
  return {'ok': True, 'data': {'presets': presets, 'runs': runs_out, 'total': total,
                              'status_counts': status_counts}}


def _download_name(task: dict, filename: str, path) -> str:
  """下载时给用户的**可读中文文件名**（磁盘上的文件名保持 ASCII 不动）。

  为什么要分开：磁盘名必须是纯 ASCII —— 产物 URL 段与磁盘目录名要逐字相等，
  而 `resolve_artifact_path` **不解码 URL**，中文名会让下载 404（踩过）。
  但用户下载时看到 `event-report-2026-10-05_2026-10-11.pdf` 是"没定好"的名字，
  所以这里另外拼一个人类可读名（Starlette 会按 RFC 5987 编码 Content-Disposition 的中文）。
  """
  base = (task.get('name') or '分析报告').replace(' · ', '_').replace(' ', '')
  m = re.search(r'(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})', filename or '')
  if m:
    base = f'{base}_{m.group(1)}至{m.group(2)}'
  return f'{base}{path.suffix or ".pdf"}'


async def _preset_run_or_404(run_id: str):
  """取预设报告的某次运行；不属于任何预设任务的 run 一律 404（防止拿它读用户任务的记录）。"""
  from service.scheduled_task import report_templates as report_tpl
  from service.scheduled_task.repository import SYSTEM_USER_ID

  run = await tasks_repo.get_run(run_id)
  if not run:
    raise HTTPException(404, '运行记录不存在')
  from config.mongodb_conn import mongodb_manager
  from service.scheduled_task.repository import TASKS_COLLECTION

  task = await mongodb_manager.db[TASKS_COLLECTION].find_one({
    'task_id': run.task_id, 'created_by': SYSTEM_USER_ID,
  })
  # 判据用 `is_preset_key()`（＝`parse_key` 能解析），**与清单端点 `_preset_tasks()` 同一套**。
  # ⚠️ 这里原先查的是 `template_id ∈ report_tpl.PRESET_KEYS`（一份静态列表）—— 那份列表
  # 在 2026-10-15 清理 report_templates 死代码时被一并删掉，而本文件仍在引用 →
  # `AttributeError` → 端点 500 → 报告页"点开都是请求失败"（用户实测）。静态列表本身也是隐患：
  # 园区是运行时数据，列表不可能跟上，园区级预设的 run 会被误判成"不存在"。
  if not task or not report_tpl.is_preset_key(task.get('template_id') or ''):
    raise HTTPException(404, '运行记录不存在')
  return run, task


@router.get('/presets/runs/{run_id}/messages')
@require_permission('ai:agent')
async def get_preset_run_messages(run_id: str, request: Request):
  """报告正文（结论 + 图表）：按该次运行的 thread_id 拉会话消息。"""
  run, _task = await _preset_run_or_404(run_id)
  if not run.thread_id:
    return MessageListResponse(messages=[], total=0)
  session_id = run.thread_id.removeprefix('rz-agent-')
  messages = await message_service.get_messages(session_id)
  return MessageListResponse(messages=messages, total=len(messages))


@router.get('/presets/runs/{run_id}/artifacts/{filename}')
@require_permission('ai:agent')
async def download_preset_artifact(run_id: str, filename: str, request: Request):
  """报告产物下载（通常是 PDF）。共享报告，按 run 归属校验即可，无需用户归属。"""
  run, task = await _preset_run_or_404(run_id)
  path = tasks_repo.resolve_artifact_path(run, filename)
  if path is None:
    raise HTTPException(404, '产物不存在')
  return FileResponse(path, filename=_download_name(task, filename, path))


# ==================== 告警不合规检查（系统预设 · 共享只读）====================
# 契约同分析报告：共享产出、只读、权限 ai:agent、不按人隔离。
# 页面在**没有任何产出时**也要能展示「要查什么、口径是什么」→ `/spec` 由后端注册表
# （`checks.CHECKS`）驱动，加检查项时前端一行不用改。
# 同样必须声明在 `/{task_id}` 之前。

async def _fetch_compliance_tasks(only_enabled: bool = False) -> dict[str, dict]:
  """compliance 预设 key → 系统任务文档。含已停用的（历史产出仍要能看）。"""
  from config.mongodb_conn import mongodb_manager
  from service.scheduled_task.compliance import presets as compliance_presets
  from service.scheduled_task.repository import SYSTEM_USER_ID, TASKS_COLLECTION

  query: dict = {'created_by': SYSTEM_USER_ID,
                 'template_id': {'$regex': f'^{compliance_presets.PREFIX}'}}
  if only_enabled:
    query['enabled'] = True
  out: dict[str, dict] = {}
  async for d in mongodb_manager.db[TASKS_COLLECTION].find(query):
    out[d.get('template_id', '')] = d
  return out


async def _compliance_run_or_404(run_id: str):
  """取不合规检查的某次运行；不是本族预设的 run 一律 404。"""
  from service.scheduled_task.compliance import presets as compliance_presets
  from service.scheduled_task.repository import SYSTEM_USER_ID, TASKS_COLLECTION
  from config.mongodb_conn import mongodb_manager

  run = await tasks_repo.get_run(run_id)
  if not run:
    raise HTTPException(404, '运行记录不存在')
  task = await mongodb_manager.db[TASKS_COLLECTION].find_one({
    'task_id': run.task_id, 'created_by': SYSTEM_USER_ID,
  })
  if not task or not compliance_presets.is_preset_key(task.get('template_id') or ''):
    raise HTTPException(404, '运行记录不存在')
  return run, task


def _filename_of(url: str) -> str:
  return (url or '').rsplit('/', 1)[-1]


def _run_workbook(run):
  """本次运行的 xlsx 产物路径（文件名纯 ASCII，`resolve_artifact_path` 可直接反查）。

  不合规检查与事件统计表共用：两族产物都是本族 run 里的一份 xlsx，定位方式一致。
  """
  for url in run.artifact_urls or []:
    name = _filename_of(url)
    if name.endswith('.xlsx'):
      path = tasks_repo.resolve_artifact_path(run, name)
      if path is not None:
        return path, name
  return None, ''


def _compliance_run_out(run) -> dict:
  return {
    'run_id': run.run_id,
    'status': run.status.value,
    'triggered_at': run.triggered_at.isoformat(),
    'duration_ms': run.duration_ms,
    'error': run.error,
    'period_key': run.period_key,
    'period_label': run.period_label,
    'teaser': _teaser(run.final_reply),
    # 各检查项命中数（卡片墙直接用；失败/老记录为空 dict）
    'check_counts': run.check_counts,
    # 页面用 run_id + filename 走本组下载端点（run.artifact_urls 是会话级地址，
    # 共享查看者不是会话所有者，直接用会 403）
    'files': [_filename_of(u) for u in (run.artifact_urls or []) if _filename_of(u)],
  }


@router.get('/compliance/spec')
@require_permission('ai:agent')
async def get_compliance_spec(request: Request):
  """检查项注册表（页面空状态与筛选徽标的数据源）。"""
  from service.scheduled_task.compliance import checks

  return {'ok': True, 'data': {
    'title': checks.TITLE,
    'checks': [dict(c) for c in checks.CHECKS],
    'cadences': ['班次'],
  }}


_DAY_RE = r'^(\d{4}-\d{2}-\d{2})?$'


def _period_bounds(start: str, end: str) -> tuple[str | None, str | None]:
  """日期区间 → `period_key` 的字典序区间（右端开区间＝结束日 + 1 天）。

  不合规检查的期号是 `YYYY-MM-DD-n/-d`（日期部分＝班次**结束**那天），字符串序即日期序，
  所以 `>= start` 且 `< end+1d` 正好含起止两天的白班/夜班。格式不合法抛 ValueError。
  """
  from datetime import date, timedelta

  period_from = date.fromisoformat(start).isoformat() if start else None
  period_to_excl = (
    (date.fromisoformat(end) + timedelta(days=1)).isoformat() if end else None
  )
  return period_from, period_to_excl


@router.get('/compliance')
@require_permission('ai:agent')
async def list_compliance_runs(
  request: Request,
  offset: int = Query(0, ge=0),
  limit: int = Query(20, ge=1, le=200),
  days: int = Query(0, ge=0, le=3650, description='只要最近 N 天的产出；0=不限'),
  start: str = Query('', pattern=_DAY_RE, description='YYYY-MM-DD，按期号日期下限'),
  end: str = Query('', pattern=_DAY_RE, description='YYYY-MM-DD，按期号日期上限（含当天）'),
  cadence: str = Query('', description='预留：night / day（按期号后缀 -n/-d 过滤）'),
):
  """不合规检查产出目录（按期倒序，分页 + 天数/日期区间筛选）。"""
  from datetime import datetime, timedelta, timezone

  try:
    period_from, period_to_excl = _period_bounds(start, end)
  except ValueError:
    raise HTTPException(status_code=400, detail='日期格式不合法，应为 YYYY-MM-DD')
  if period_from and period_to_excl and period_from >= period_to_excl:
    return {'ok': True, 'data': {'runs': [], 'total': 0, 'status_counts': {}}}

  tasks = await _fetch_compliance_tasks()
  task_ids = [t['task_id'] for t in tasks.values() if t.get('task_id')]
  since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
  if cadence in ('night', 'day'):
    # 班次筛只在内存里做（每天最多 2 期，量级很小）；分页在过滤后切
    runs, _ = await tasks_repo.list_runs_for_tasks(
      task_ids, limit=2000, offset=0, since=since,
      period_from=period_from, period_to_excl=period_to_excl,
    )
    suffix = '-n' if cadence == 'night' else '-d'
    runs = [r for r in runs if (r.period_key or '').endswith(suffix)]
    total = len(runs)
    runs = runs[offset:offset + limit]
  else:
    runs, total = await tasks_repo.list_runs_for_tasks(
      task_ids, limit=limit, offset=offset, since=since,
      period_from=period_from, period_to_excl=period_to_excl,
    )
  status_counts = await tasks_repo.count_runs_by_status(
    task_ids, since=since, period_from=period_from, period_to_excl=period_to_excl,
  )
  return {'ok': True, 'data': {
    'runs': [_compliance_run_out(r) for r in runs],
    'total': total,
    'status_counts': status_counts,
  }}


def _cell(value):
  """xlsx 单元格 → JSON 安全值（openpyxl 可能回 datetime/Decimal）。"""
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  return str(value)


def _sheet_rows(ws, max_rows: int = 5000) -> tuple[list[str], list[dict]]:
  """读一张表：**第一个「非空单元格 ≥ 2」的行当表头**（标题行只有 1 个非空单元格）。

  新产物的 sheet 第 1 行是标题、第 2 行才是表头；老产物表头就在第 1 行。按内容判定
  而不是固定行号，两种都能读。
  """
  columns: list[str] = []
  records: list[dict] = []
  for row in ws.iter_rows(values_only=True):
    values = [_cell(v) for v in row]
    if not columns:
      header = ['' if v is None else str(v) for v in values]
      if sum(1 for v in header if v != '') >= 2:
        columns = header
      continue
    if all(v is None or v == '' for v in values):
      continue
    if len(records) >= max_rows:
      break
    records.append({columns[i]: values[i] for i in range(len(columns)) if i < len(values)})
  return columns, records


def _read_sheet(path, sheet_name: str, max_rows: int = 5000) -> tuple[list[str], list[dict]]:
  from openpyxl import load_workbook

  wb = load_workbook(path, read_only=True, data_only=True)
  try:
    return _sheet_rows(wb[sheet_name], max_rows)
  finally:
    wb.close()


@router.get('/compliance/runs/{run_id}/sheets')
@require_permission('ai:agent')
async def get_compliance_run_sheets(run_id: str, request: Request):
  """本次产出的 sheet 清单 + 各检查项命中数（详情页顶部汇总卡带用）。"""
  from openpyxl import load_workbook

  from service.scheduled_task.compliance import checks

  run, _task = await _compliance_run_or_404(run_id)
  path, name = _run_workbook(run)
  if path is None:
    raise HTTPException(404, '产物不存在')

  wb = load_workbook(path, read_only=True, data_only=True)
  try:
    row_count: dict[str, int] = {}
    for check in checks.CHECKS:
      ws = wb[check['sheet']] if check['sheet'] in wb.sheetnames else None
      # 直接数数据行（标题行/表头由 `_sheet_rows` 跳过），0 行时是 0
      row_count[check['key']] = len(_sheet_rows(ws)[1]) if ws else 0
  finally:
    wb.close()

  summary = [{**{k: c[k] for k in ('key', 'name', 'cadence', 'description', 'sheet')},
              'rows': row_count.get(c['key'], 0)} for c in checks.CHECKS]
  return {'ok': True, 'data': {
    'run_id': run.run_id,
    'period_key': run.period_key,
    'period_label': run.period_label,
    'triggered_at': run.triggered_at.isoformat(),
    'filename': name,
    'summary': summary,
    'sheets': [{'key': s['key'], 'name': s['name'], 'rows': s['rows']} for s in summary],
  }}


@router.get('/compliance/runs/{run_id}/sheets/{key}')
@require_permission('ai:agent')
async def get_compliance_sheet(run_id: str, key: str, request: Request):
  """单个检查项明细（预览与下载同一份文件；行数上限 5000 防呆）。"""
  from service.scheduled_task.compliance import checks

  if key not in checks.COLUMNS:
    raise HTTPException(404, '检查项不存在')
  run, _task = await _compliance_run_or_404(run_id)
  path, _name = _run_workbook(run)
  if path is None:
    raise HTTPException(404, '产物不存在')
  sheet = next(c['sheet'] for c in checks.CHECKS if c['key'] == key)
  columns, records = _read_sheet(path, sheet)
  # **不套 `{ok, data}`**：这个端点的消费方是 `RecordsTable`（受控组件按 `{columns, records}`
  # 解析），与 agent 的 `/records` 端点同形。
  return {'key': key, 'name': sheet, 'columns': columns, 'records': records}


@router.get('/compliance/runs/{run_id}/artifacts/{filename}')
@require_permission('ai:agent')
async def download_compliance_artifact(run_id: str, filename: str, request: Request):
  """xlsx 下载（用户看到中文名，磁盘名保持 ASCII）。"""
  run, task = await _compliance_run_or_404(run_id)
  path = tasks_repo.resolve_artifact_path(run, filename)
  if path is None:
    raise HTTPException(404, '产物不存在')
  base = (run.period_label or task.get('name') or '不合规检查').replace(' · ', '_')
  return FileResponse(
    path, filename=f'{base}{path.suffix or ".xlsx"}',
    media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  )


# ==================== 数据中心事件统计表（系统预设 · 共享只读）====================
# 入口是飞书园区群通报（速报 + 首报），产出是「园区 × 班次」的 xlsx，没有对话/报告正文。
# 不合规检查页顶部要放一张跨满宽的大卡展示最近一期，所以这里按 period_key 分组、
# 只回元信息（期号/窗口/园区/事件条数/状态/文件），**不回 final_reply 原文**。
# 同样必须声明在 `/{task_id}` 之前。

_INCIDENT_COUNT_RE = re.compile(r'共\s*(\d+)\s*条已通报事件')
# 班次口径的窗口带时分（`2026-10-26 17:30 ~ 2026-10-27 08:30`），周报只有日期
_INCIDENT_WINDOW_RE = re.compile(
  r'（(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)\s*~\s*(\d{4}-\d{2}-\d{2}(?: \d{2}:\d{2})?)'
)


async def _fetch_incident_tasks() -> dict[str, dict]:
  """incident 预设 key → 系统任务文档。含已停用的（历史产出仍要能看）。"""
  from config.mongodb_conn import mongodb_manager
  from service.scheduled_task.incident_stats import presets as incident_presets
  from service.scheduled_task.repository import SYSTEM_USER_ID, TASKS_COLLECTION

  cursor = mongodb_manager.db[TASKS_COLLECTION].find({
    'created_by': SYSTEM_USER_ID,
    'template_id': {'$regex': f'^{incident_presets.PREFIX}'},
  })
  out: dict[str, dict] = {}
  async for d in cursor:
    if incident_presets.parse_key(d.get('template_id') or ''):
      out[d['template_id']] = d
  return out


def _incident_count(final_reply: str) -> int | None:
  """从 `final_reply` 里取「共 N 条已通报事件」；空表写明「本期无通报事件」→ 0。

  不额外落库一个字段：这条口径只在生成时写一次，而 run 记录本来就带 `final_reply`，
  为此加字段/改模型得连带迁移老数据，不值当。取不到时回 `None`（卡片显示 `—`）。
  """
  text = final_reply or ''
  m = _INCIDENT_COUNT_RE.search(text)
  if m:
    return int(m.group(1))
  return 0 if '本期无通报事件' in text else None


async def _incident_run_or_404(run_id: str):
  """取事件统计表的某次运行；不属于该族预设的 run 一律 404。"""
  from config.mongodb_conn import mongodb_manager
  from service.scheduled_task.incident_stats import presets as incident_presets
  from service.scheduled_task.repository import SYSTEM_USER_ID, TASKS_COLLECTION

  run = await tasks_repo.get_run(run_id)
  if not run:
    raise HTTPException(404, '运行记录不存在')
  task = await mongodb_manager.db[TASKS_COLLECTION].find_one({
    'task_id': run.task_id, 'created_by': SYSTEM_USER_ID,
  })
  if not task or not incident_presets.parse_key(task.get('template_id') or ''):
    raise HTTPException(404, '运行记录不存在')
  return run, task


@router.get('/incidents')
@require_permission('ai:agent')
async def list_incident_periods(
  request: Request,
  periods: int = Query(4, ge=1, le=52),
  period: str = Query('shift', pattern='^(shift|weekly)$'),
):
  """最近 N 个周期的产出，每期含运行状态与事件条数。

  **一期一条 all run（全部园区合并成一张表）**；历史期仍是旧的「一园区一条 run」，
  两种 run 同时存在时由 `_pick_incident_runs` 二选一，**绝不累加**（同一批行会算两次）。
  **排序按 `triggered_at` 而不是 `period_key`**：班次期号 `2026-10-27-n` / `-d` 同一天里
  `-n` 字典序在后，但它其实属于「从昨天傍晚开始的夜班」；周报 `2026-W43` 的 `W` 又大于
  班次的数字结尾。期号排序会把顺序打乱，只有实际触发时间能表达「哪一期更新」。
  """
  from service.scheduled_task.incident_stats import presets as incident_presets
  from service.scheduled_task.report_templates import PARKS

  tasks = await _fetch_incident_tasks()
  task_ids = [t['task_id'] for t in tasks.values() if t.get('task_id')]
  if not task_ids:
    return {'ok': True, 'data': {'periods': []}}

  runs, _ = await tasks_repo.list_runs_for_tasks(task_ids, limit=1000, offset=0)
  key_of = {t['task_id']: k for k, t in tasks.items() if t.get('task_id')}

  by_period: dict[str, list] = {}
  for r in runs:
    if not r.period_key:
      continue
    parsed = incident_presets.parse_key(r.preset_key or key_of.get(r.task_id, ''))
    if parsed and parsed[1] != period:
      continue
    by_period.setdefault(r.period_key, []).append(r)

  park_order = {p['slug']: i for i, p in enumerate(PARKS)}
  park_name = {p['slug']: p['name'] for p in PARKS}
  park_name[incident_presets.ALL_PARKS] = '全部园区'
  latest = {k: max(r.triggered_at for r in v) for k, v in by_period.items()}
  out = []
  for key in sorted(by_period, key=lambda k: latest[k], reverse=True)[:periods]:
    parks, total, window, finished, failed = [], 0, '', 0, 0
    picked = _pick_incident_runs(by_period[key], key_of)
    for park, r in sorted(picked.items(),
                          key=lambda kv: (park_order.get(kv[0], 99), kv[1].triggered_at)):
      count = _incident_count(r.final_reply)
      if count:
        total += count
      m = _INCIDENT_WINDOW_RE.search(r.final_reply or '')
      if m and not window:
        window = f'{m.group(1)} ~ {m.group(2)}'
      finished += 1 if r.status.value == 'success' else 0
      failed += 1 if r.status.value == 'failed' else 0
      parks.append({
        'park': park,
        'park_label': park_name.get(park, park),
        'run_id': r.run_id,
        'status': r.status.value,
        'count': count,
        'triggered_at': r.triggered_at.isoformat(),
        'duration_ms': r.duration_ms,
        'error': r.error,
        'files': [_filename_of(u) for u in (r.artifact_urls or []) if _filename_of(u)],
      })
    parks.sort(key=lambda x: park_order.get(x['park'], 99))
    out.append({
      'period_key': key,
      'period_label': by_period[key][0].period_label or key,
      'window': window,
      'parks': parks,
      'total_count': total,
      'parks_total': len(parks),
      'finished': finished,
      'failed': failed,
    })
  return {'ok': True, 'data': {'periods': out}}


_SUMMARY_SHEET = '事件汇总'


def _pick_incident_runs(runs: list, key_of: dict) -> dict[str, object]:
  """同期并存旧 per-park run 与新 all run 时的选择规则 → `{park: run}`。

  1. 有 **success 的 all run** → 只取它（再叠加 per-park 会把同一批行算两遍）；
  2. 没有 → 回退旧 per-park runs（历史期行为不变）；
  3. 只有 failed 的 all run → 也返回它，前端才看得到 `error` 与失败徽标。
  每个 park 只保留 `triggered_at` 最新的一条（`runs` 已按倒序）。
  """
  from service.scheduled_task.incident_stats import presets as incident_presets

  latest: dict[str, object] = {}
  for run in runs:
    parsed = incident_presets.parse_key(run.preset_key or key_of.get(run.task_id, ''))
    if parsed:
      latest.setdefault(parsed[0], run)
  all_run = latest.get(incident_presets.ALL_PARKS)
  if all_run is not None and all_run.status.value == 'success':
    return {incident_presets.ALL_PARKS: all_run}
  per_park = {p: r for p, r in latest.items() if p != incident_presets.ALL_PARKS}
  return per_park or latest


async def _incident_period_runs(period_key: str) -> tuple[dict, dict]:
  """该期的可用运行（选择规则见 `_pick_incident_runs`）。

  `_runs_query` 按 `period_key` 字典序区间筛，`period_key + '\\x00'` 正好是「精确等于
  该期」的右开区间（期号可能带 `-n/-d`，不能用日期补位）。返回 `(key_of, picked)`。
  """
  tasks = await _fetch_incident_tasks()
  key_of = {t['task_id']: k for k, t in tasks.items() if t.get('task_id')}
  task_ids = list(key_of)
  if not task_ids:
    return {}, {}
  runs, _ = await tasks_repo.list_runs_for_tasks(
    task_ids, limit=500, offset=0,
    period_from=period_key, period_to_excl=period_key + '\x00',
  )
  return key_of, _pick_incident_runs(runs, key_of)


def _incident_period_meta(period_key: str, latest: dict, run=None) -> dict:
  """期号/标签/窗口/失败数 —— 没有任何可用产物时也要能回给前端。"""
  any_run = run or next(iter(latest.values()), None)
  match = _INCIDENT_WINDOW_RE.search((any_run.final_reply if any_run else '') or '')
  return {
    'period_key': period_key,
    'period_label': (any_run.period_label if any_run else '') or period_key,
    'window': f'{match.group(1)} ~ {match.group(2)}' if match else '',
    'failed': sum(1 for r in latest.values() if r.status.value == 'failed'),
  }


def _incident_unavailable_reason(latest: dict) -> str:
  """产物缺失是正常状态（文件过期/被清），不是错误 —— 由前端显示空状态。"""
  if not latest:
    return '该期没有运行记录'
  return '该期产物文件已不存在（磁盘上没有对应 xlsx），无法预览明细'


def _incident_usable_runs(latest: dict) -> list[tuple[str, object, object]]:
  """有 xlsx 产物的成功运行，按 `PARKS` 顺序（合并表的园区先后固定）。"""
  from service.scheduled_task.report_templates import PARKS

  order = {p['slug']: i for i, p in enumerate(PARKS)}
  usable = []
  for park, run in latest.items():
    if run.status.value != 'success':
      continue
    path, _name = _run_workbook(run)
    if path is not None:
      usable.append((park, run, path))
  usable.sort(key=lambda item: order.get(item[0], 99))
  return usable


@router.get('/incidents/periods/{period_key}/sheets')
@require_permission('ai:agent')
async def get_incident_period_sheets(period_key: str, request: Request):
  """整期（所有园区合并）的 Sheet 清单与行数 —— 大卡点开后看板顶部用。"""
  from openpyxl import load_workbook

  _key_of, latest = await _incident_period_runs(period_key)
  usable = _incident_usable_runs(latest)
  if not usable:
    return {'ok': True, 'data': {
      **_incident_period_meta(period_key, latest),
      'available': False,
      'reason': _incident_unavailable_reason(latest),
      'sheets': [],
    }}

  wb = load_workbook(usable[0][2], read_only=True, data_only=True)
  try:
    names = list(wb.sheetnames)
  finally:
    wb.close()
  detail = next((n for n in names if n != _SUMMARY_SHEET), names[0])

  sheets = []
  for name in [detail, *[n for n in names if n != detail]]:
    rows = 0
    for _park, _run, path in usable:
      try:
        rows += len(_read_sheet(path, name)[1])
      except KeyError:      # 该园区产物里没有这张 Sheet
        continue
    sheets.append({'key': name, 'name': name, 'rows': rows})

  return {'ok': True, 'data': {
    **_incident_period_meta(period_key, latest, usable[0][1]),
    'available': True,
    'sheets': sheets,
  }}


@router.get('/incidents/periods/{period_key}/sheets/{name}')
@require_permission('ai:agent')
async def get_incident_period_sheet(period_key: str, name: str, request: Request):
  """整期合并的单个 Sheet：列与 xlsx 完全一致。

  现在是「一期一条 all run、一份合并 xlsx」，拼表逻辑只是历史期的兼容路径（那时一园区
  一份产物，靠拼接才凑齐整期）。
  """
  _key_of, latest = await _incident_period_runs(period_key)
  usable = _incident_usable_runs(latest)
  if not usable:
    return {
      'key': name, 'name': name, 'columns': [], 'records': [],
      'available': False,
      'reason': _incident_unavailable_reason(latest),
    }

  columns: list[str] = []
  records: list[dict] = []
  found = False
  for _park, _run, path in usable:
    try:
      cols, rows = _read_sheet(path, name)
    except KeyError:
      continue
    found = True
    if not columns:
      columns = cols
    records.extend(rows)
  if not found:
    raise HTTPException(404, 'Sheet 不存在')

  if name == _SUMMARY_SHEET:
    # 每份产物都自带一行「总计」，合并后只留一行（按各列重算）
    parks = [r for r in records if str(r.get('园区') or '') != '总计']
    if parks:
      parks.append({'园区': '总计', **{
        col: sum(int(r.get(col) or 0) for r in parks)
        for col in columns if col != '园区'
      }})
    return {'key': name, 'name': name, 'columns': columns, 'records': parks,
            'available': True}

  # 明细沿用生成侧口径（发生时间升序 + 事件名称），序号在整期范围内连续
  records.sort(key=lambda r: (str(r.get('发生时间') or ''), str(r.get('事件名称') or '')))
  for i, record in enumerate(records, 1):
    if '序号' in record:
      record['序号'] = i
  return {'key': name, 'name': name, 'columns': columns, 'records': records,
          'available': True}


@router.get('/incidents/runs/{run_id}/artifacts/{filename}')
@require_permission('ai:agent')
async def download_incident_artifact(run_id: str, filename: str, request: Request):
  """xlsx 下载（用户看到中文名，磁盘名保持 ASCII）。"""
  run, task = await _incident_run_or_404(run_id)
  path = tasks_repo.resolve_artifact_path(run, filename)
  if path is None:
    raise HTTPException(404, '产物不存在')
  parsed = task.get('name') or '数据中心事件统计表'
  base = parsed.replace(' · ', '_').replace(' ', '')
  return FileResponse(
    path, filename=f'{base}{path.suffix or ".xlsx"}',
    media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  )


# ==================== 文档更新检查（部门 × 25 项 · 共享只读）====================
# 逐部门（A1…A18）共享文档检查 25 条月度项（口径见 service/doc_update）。读数不下载；
# 「立即检查」才真的去取一期（5~15 分钟），用 Redis 租约防重复触发（见 sync_state）。
def _doc_update_progress() -> dict | None:
  from service.doc_update import sync_state
  return sync_state.read()


_DOC_PERIOD_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')


@router.get('/doc-update/overview')
@require_permission('ai:agent')
async def get_doc_update_overview(
  request: Request,
  period: str = Query('', description='历史期 YYYY-MM；空 = 最新一期'),
):
  """指定期（默认最新）快照 + 与上一期的比对（页面与筛选徽标的数据源）。"""
  from service.doc_update import presets, workflow
  from service.doc_update.verdict import summary_text

  if period and not _DOC_PERIOD_RE.match(period):
    raise HTTPException(400, '期间格式应为 YYYY-MM')
  if not presets.enabled():
    return {'ok': True, 'data': {
      'enabled': False, 'latest': None, 'verdict': None,
      'summary': '', 'running': False, 'progress': None,
      'dept_columns': [], 'catalog': [], 'periods': [], 'missing_period': '',
    }}
  overview = await workflow.build_overview(period or None)
  latest, verdict = overview['latest'], overview['verdict']
  progress = _doc_update_progress()
  return {'ok': True, 'data': {
    'enabled': True,
    'dept_columns': overview.get('dept_columns') or [],
    'catalog': overview.get('catalog') or [],
    'periods': overview.get('periods') or [],
    'missing_period': overview.get('missing_period') or '',
    'latest': ({
      'snapshot_id': latest.get('snapshot_id'),
      'taken_at': latest.get('taken_at'),
      'period_key': latest.get('period_key'),
      'period_label': latest.get('period_label'),
      'file_count': latest.get('file_count'),
      'elapsed': latest.get('elapsed'),
      'scope': latest.get('scope') or 'tree',
      'dept_count': len(latest.get('depts') or []),
    } if latest else None),
    'verdict': verdict,
    'summary': summary_text(verdict) if latest else '',
    'running': progress is not None,
    'progress': progress,
  }}


@router.post('/doc-update/sync')
@require_permission('ai:agent')
async def trigger_doc_update_sync(request: Request):
  """后台跑一期快照（约 5~15 分钟）；已在跑则 `started=false`，不排队。"""
  from service.doc_update import presets, sync_state, workflow

  if not presets.enabled():
    raise HTTPException(400, '文档更新检查未开启（DOC_UPDATE_CHECK_ENABLED=false）')

  user_id = getattr(request.state, 'user_id', '') or ''
  lease = sync_state.acquire('manual', user_id)
  if lease is None:
    return {'ok': True, 'data': {'started': False, 'reason': '已有一次检查在跑'}}

  async def _run():
    try:
      await workflow.perform_sync(created_by=user_id, progress=lease.update)
    except Exception as e:  # noqa: BLE001  后台任务失败只记日志，页面靠轮询看结果
      import logging
      logging.getLogger(__name__).warning('文档更新检查手动同步失败: %s', e)
    finally:
      lease.release()

  asyncio.create_task(_run())
  return {'ok': True, 'data': {'started': True}}


# ==================== 红外巡检图片质量检测（按任务勾选 · 共享只读）====================
# 用户选部门 → 看任务列表 → 勾要检的任务 → 后台逐图调 Gradio 判清晰度。
# 「已检测」状态来自 Mongo `infrared_quality_tasks`，默认只勾未检测的（用户口径：
# 每月只更新一部分任务，查过的不重复查）。口径/失败降级见 service/infrared_quality。
def _ir_enabled() -> bool:
  from config.settings import settings
  return bool(settings.INFRARED_QUALITY_ENABLED)


def _ir_task_payload(task: dict, checked: dict | None) -> dict:
  info = checked or {}
  checked_at = info.get('checked_at')
  return {
    'task_id': str(task.get('inspectionTaskId') or task.get('taskId') or ''),
    'inspector': str(task.get('inspector') or ''),
    'status': task.get('status'),
    'device_count': int(task.get('deviceCount') or 0),
    'photo_count': info.get('photo_count'),
    'start_time': task.get('startTime') or 0,
    'end_time': task.get('endTime') or 0,
    'checked_at': checked_at.isoformat() if hasattr(checked_at, 'isoformat') else checked_at,
    'result_status': info.get('result_status') or '',
    'counts': info.get('counts') or {},
  }


@router.get('/infrared-quality/tasks')
@require_permission('ai:agent')
async def get_infrared_quality_tasks(
  request: Request,
  dept_id: str = Query('', description='部门 id；空 = 用第一个可用部门'),
  refresh: bool = Query(False, description='绕过任务列表缓存'),
):
  """部门任务列表 × 已检测状态（不含明细行）。"""
  from service.doc_update import depts as doc_depts
  from service.infrared_quality import fetch, state, store

  if not _ir_enabled():
    return {'ok': True, 'data': {
      'enabled': False, 'departments': [], 'tasks': [], 'dept': None,
      'running': False, 'progress': None, 'latest_run': None,
    }}

  columns = await doc_depts.dept_columns()
  if not dept_id:
    if not any(c.get('dept_id') for c in columns):
      # 首次使用：解析部门 id（逐公司一次，结果落 Mongo 缓存）
      await doc_depts.ensure_registry()
      columns = await doc_depts.dept_columns()
    ready = [c for c in columns if c.get('dept_id')]
    dept_id = ready[0]['dept_id'] if ready else ''

  dept = next((c for c in columns if c.get('dept_id') == dept_id), None)
  checked: dict[str, dict] = {}
  tasks: list[dict] = []
  if dept:
    try:
      rows = await fetch.list_tasks(dept_id, refresh=refresh)
    except Exception as e:  # noqa: BLE001  接口抖动不该让页面整个打不开
      import logging
      logging.getLogger(__name__).warning('红外任务列表获取失败: %s', e)
      rows = []
    checked = {c['task_id']: c for c in await store.list_checked(dept_id)}
    for t in rows:
      tid = str(t.get('inspectionTaskId') or t.get('taskId') or '')
      tasks.append(_ir_task_payload(t, checked.get(tid)))
    tasks.sort(key=lambda t: t['end_time'] or t['start_time'] or 0, reverse=True)

  latest = await store.latest_run(dept_id) if dept_id else None
  if latest:
    # run 文档里存的是旧口径计数（含「无图片」行），按任务重算后求和
    latest = dict(latest)
    latest['counts'] = store.sum_counts(
      [checked.get(tid, {}).get('counts') for tid in (latest.get('task_ids') or [])])
  progress = state.read()
  return {'ok': True, 'data': {
    'enabled': True,
    'departments': columns,
    'dept': dept,
    'tasks': tasks,
    'running': progress is not None,
    'progress': progress,
    'latest_run': store.run_public(latest),
  }}


@router.post('/infrared-quality/check')
@require_permission('ai:agent')
async def trigger_infrared_quality_check(request: Request):
  """后台检测勾选的任务；租约被占则 `started=false`，不排队。"""
  from service.doc_update import depts as doc_depts
  from service.infrared_quality import fetch, state, workflow

  if not _ir_enabled():
    raise HTTPException(400, '红外质量检测未开启（INFRARED_QUALITY_ENABLED=false）')

  body = await request.json()
  dept_id = str(body.get('dept_id') or '')
  task_ids = [str(t) for t in (body.get('task_ids') or []) if str(t)]
  refresh = bool(body.get('refresh'))
  if not dept_id or not task_ids:
    raise HTTPException(400, 'dept_id 与 task_ids 不能为空')

  columns = await doc_depts.dept_columns()
  dept = next((c for c in columns if c.get('dept_id') == dept_id), None)
  if not dept:
    raise HTTPException(404, '部门不存在')

  rows = await fetch.list_tasks(dept_id)
  wanted = set(task_ids)
  selected = [r for r in rows if str(r.get('inspectionTaskId')) in wanted]
  # 进行中的任务没有检测时间与图片，不允许检（前端已禁用，这里兜底）
  selected = [r for r in selected if str(r.get('status')) != '1']
  if not selected:
    return {'ok': True, 'data': {
      'started': False, 'reason': '所选任务无可用明细（可能都在进行中）',
    }}

  user_id = getattr(request.state, 'user_id', '') or ''
  lease = state.acquire('manual', user_id)
  if lease is None:
    return {'ok': True, 'data': {'started': False, 'reason': '已有一次检测在跑，请稍候'}}

  async def _run():
    try:
      await workflow.perform_check(dept, selected, created_by=user_id,
                                   progress=lease.update, refresh=refresh)
    except Exception as e:  # noqa: BLE001  后台任务失败只记日志，页面靠轮询看结果
      import logging
      logging.getLogger(__name__).warning('红外质量检测失败: %s', e)
    finally:
      lease.release()

  asyncio.create_task(_run())
  return {'ok': True, 'data': {'started': True, 'task_ids': [
    str(r.get('inspectionTaskId')) for r in selected] }}


@router.get('/infrared-quality/records')
@require_permission('ai:agent')
async def get_infrared_quality_records(
  request: Request,
  run_id: str = Query('', description='本轮检测（合并各任务行）'),
  task_id: str = Query('', description='单个任务的最新结果'),
):
  """直接回 `{columns, records}`（给 RecordsTable，不套 `{ok, data}`）。"""
  from service.infrared_quality import workflow

  if not run_id and not task_id:
    raise HTTPException(400, 'run_id 或 task_id 至少给一个')
  return await workflow.records_for(run_id=run_id, task_id=task_id)


@router.get('/infrared-quality/image')
@require_permission('ai:agent')
async def get_infrared_quality_image(
  request: Request,
  task_id: str = Query(..., description='巡检任务 id'),
  name: str = Query(..., description='图片文件名'),
):
  """本地留存的巡检原图（检测时从预签名地址下载的那一份）。

  S3 预签名地址 7 天过期，检测时本地留一份，明细读库时「图片链接」改指这里。
  """
  from service.infrared_quality import workflow

  path = workflow.image_file(task_id, name)
  if not path:
    raise HTTPException(404, '图片不存在或已清理')
  return FileResponse(path, media_type=mimetypes.guess_type(path)[0] or 'image/jpeg')


@router.get('/{task_id}')
@require_permission('scheduled_task:manage')
async def get_task(task_id: str, request: Request):
  user_id = getattr(request.state, 'user_id', '')
  task = await _owned_task(task_id, user_id)
  return {'ok': True, 'data': task.model_dump(mode='json')}


@router.patch('/{task_id}')
@require_permission('scheduled_task:manage')
async def update_task(task_id: str, body: ScheduledTaskUpdate, request: Request):
  user_id = getattr(request.state, 'user_id', '')
  await _owned_task(task_id, user_id)
  task = await tasks_repo.update_task(task_id, body)
  if not task:
    raise HTTPException(404, '任务不存在')
  return {'ok': True, 'data': task.model_dump(mode='json')}


@router.delete('/{task_id}')
@require_permission('scheduled_task:manage')
async def delete_task(task_id: str, request: Request):
  user_id = getattr(request.state, 'user_id', '')
  await _owned_task(task_id, user_id)
  if not await tasks_repo.delete_task(task_id):
    raise HTTPException(404, '任务不存在')
  return {'ok': True}


@router.post('/{task_id}/trigger')
@require_permission('scheduled_task:manage')
async def trigger_task(task_id: str, request: Request):
  """手动立即触发一次（不改调度计划）"""
  user_id = getattr(request.state, 'user_id', '')
  task = await _owned_task(task_id, user_id)
  asyncio.create_task(run_task(task))
  return {'ok': True}


@router.get('/{task_id}/runs')
@require_permission('scheduled_task:manage')
async def list_runs(
  task_id: str, request: Request,
  offset: int = 0, limit: int = Query(50, le=200),
):
  user_id = getattr(request.state, 'user_id', '')
  await _owned_task(task_id, user_id)
  runs = await tasks_repo.list_runs(task_id, limit=limit, offset=offset)
  return {'ok': True, 'data': [r.model_dump(mode='json') for r in runs]}


@router.get('/{task_id}/runs/{run_id}/messages')
@require_permission('scheduled_task:manage')
async def get_run_messages(task_id: str, run_id: str, request: Request):
  """运行完整对话：按 run 的 thread_id 拉取会话消息（供前端「查看对话」展示）。"""
  user_id = getattr(request.state, 'user_id', '')
  await _owned_task(task_id, user_id)
  run = await tasks_repo.get_run(run_id)
  if not run or run.task_id != task_id:
    raise HTTPException(404, '运行记录不存在')
  if not run.thread_id:
    return MessageListResponse(messages=[], total=0)
  session_id = run.thread_id.removeprefix('rz-agent-')
  messages = await message_service.get_messages(session_id)
  return MessageListResponse(messages=messages, total=len(messages))


@router.get('/{task_id}/runs/{run_id}/artifacts/{filename}')
@require_permission('scheduled_task:manage')
async def download_artifact(task_id: str, run_id: str, filename: str, request: Request):
  """任务级产物下载（绕过现有 /chat/agent/artifacts 的 session 归属校验）。

  绕过的是 **session** 归属（定时任务线程无真实用户 session），不是**用户**归属——
  仍要先确认这个任务属于当前用户。
  """
  user_id = getattr(request.state, 'user_id', '')
  await _owned_task(task_id, user_id)
  run = await tasks_repo.get_run(run_id)
  if not run or run.task_id != task_id:
    raise HTTPException(404, '运行记录不存在')
  path = tasks_repo.resolve_artifact_path(run, filename)
  if path is None:
    raise HTTPException(404, '产物不存在')
  return FileResponse(path, filename=path.name)

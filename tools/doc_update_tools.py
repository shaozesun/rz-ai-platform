"""文档更新检查工具（A 部门 × 25 项月度矩阵）。

口径全在 `service/doc_update/`（与定时预设同一套），对话侧不复算：
- `sync=false`（默认）：读库里最新一期快照 + 上一期做比对，**不下载**（秒回）
- `sync=true`：真取一期（逐部门遍历共享目录，约 5~15 分钟），期间推 SSE status 防静默超时
- 带 `name` 时只取匹配到的项（各部门的该项），约 1~3 秒
"""

import json
import logging
from datetime import timezone
from zoneinfo import ZoneInfo

from langchain.tools import tool
from langchain_core.runnables.config import RunnableConfig

from config.settings import settings
from core.agent.untrusted_content import sanitizing

logger = logging.getLogger(__name__)

_NOTE = (
  '覆盖 25 条**月度**文档 × 各公司 A 部门（廊坊 A1…A18、平湖/惠州/佛山/重庆各 2~3 个）'
  '的共享目录（实时/年度/季度/周等不在本功能范围）；同名部门按公司区分。'
  '按期提交型（目录里有月份文件夹，如能源月报）只看本期应到月份有没有文件；'
  '原地更新型（无月份文件夹）按内容哈希与上期比对。'
  '「未找到」＝该部门下未查询到相关文件；「下载失败」＝取不到字节或类型校验不过。'
  '两者都**不是**「未更新」。'
)
_NOTE_BASELINE = (
  _NOTE + '原地更新型没有上期快照，本次只建基线；按期提交型已直接判定。'
)
_PROGRESS_EVERY = 5


def _err(message: str) -> str:
  return json.dumps({'error': message}, ensure_ascii=False)


def _summary_counts(verdict: dict) -> dict:
  counts = verdict.get('counts') or {}
  return {
    'updated': counts.get('updated', 0),
    'stale': counts.get('stale', 0),
    'missing': counts.get('missing', 0),
    'failed': counts.get('failed', 0),
    'baseline': counts.get('baseline', 0),
  }


def _iso(value):
  # snapshot / verdict 里的时间是 datetime，直接 json.dumps 会抛错。
  # 库里存 aware UTC、Mongo 读回来是 naive UTC —— 统一转成本地时区并带上偏移，
  # 否则模型会把「本月首查 06:52」当成早上的检查（实际是 UTC）。
  if not hasattr(value, 'isoformat'):
    return value
  if value.tzinfo is None:
    value = value.replace(tzinfo=timezone.utc)
  return value.astimezone(ZoneInfo(settings.SCHEDULED_TASK_TIMEZONE or 'Asia/Shanghai')).isoformat()


def _note_for(verdict: dict) -> str:
  if verdict.get('scope') == 'dept':
    return _NOTE_BASELINE if verdict.get('baseline') else _NOTE
  if verdict.get('baseline'):
    return _NOTE + '本期只建了基线，没有上期可比；下个月跑完那一期后才出现「已更新/未更新」。'
  return _NOTE


def _payload(verdict: dict, snapshot_taken_at, synced: bool,
             extra: dict | None = None) -> dict:
  return {
    'period_label': verdict.get('period_label') or '',
    'snapshot_taken_at': _iso(snapshot_taken_at),
    # cross=与上一期（上月）比 / baseline=没有上期，只建基线
    'mode': verdict.get('mode') or ('baseline' if verdict.get('baseline') else 'cross'),
    'baseline': bool(verdict.get('baseline')),
    'summary': _summary_counts(verdict),
    # summary 的键名 → 中文展示词（已更新/未更新/未找到/下载失败/基线）
    'labels': verdict.get('labels') or {},
    'dept_count': verdict.get('dept_count') or 0,
    'depts': [
      {
        'company_name': dept.get('company_name') or '',
        'dept_name': dept.get('dept_name') or '',
        'summary': {
          'updated': (dept.get('counts') or {}).get('updated', 0),
          'stale': (dept.get('counts') or {}).get('stale', 0),
          'missing': (dept.get('counts') or {}).get('missing', 0),
          'failed': (dept.get('counts') or {}).get('failed', 0),
          'baseline': (dept.get('counts') or {}).get('baseline', 0),
        },
        'error': dept.get('error') or '',
      }
      for dept in verdict.get('depts') or []
    ],
    'items': [
      {
        'company_name': item.get('company_name') or '',
        'dept_name': item.get('dept_name') or '',
        'number': item.get('number'),
        'name': item.get('name'),
        'layout': item.get('layout') or '',
        'latest_period': item.get('latest_period') or '',
        'actual_dirs': item.get('actual_dirs') or [],
        'status': item.get('status_label'),
        'file_count': item.get('file_count'),
        'changed_days_ago': item.get('changed_days_ago'),
        'error': item.get('error') or '',
      }
      for item in verdict.get('items') or []
    ],
    'warnings': verdict.get('warnings') or [],
    'synced': synced,
    'note': _note_for(verdict),
    **(extra or {}),
  }


def _refresh_note(stored: bool) -> str:
  if stored:
    return '只刷新了这一项，其余项保持上次结果；本期快照已合并更新。'
  return ('本期还没有整期快照，本次结果未落库（不制造半份基线）；'
          '建议先跑一次整期检查建立基线。')


def _catalog_pairs(items) -> list[dict]:
  return [{'number': i.number_label, 'name': i.name} for i in items]


@tool
@sanitizing
async def doc_update_check(sync: bool = False, name: str = '',
                           config: RunnableConfig = None) -> str:
  """逐个公司、逐个部门检查「平台每月所需更新文件目录」里的 25 条月度文档本月是否更新。

  平台对**每家公司的每个 A 部门**（廊坊 A1…A18，平湖/惠州/佛山/重庆各 2~3 个）的
  共享文档目录分别检查，**不要**自己用 analysis_load 或文件接口复算，也不要用文档里的
  updateTime/uploadTime 判断（那两个字段不可信）。有月份文件夹的项看本期应到月份有没有
  文件；无月份文件夹的项按 sha256 与上期快照比对。

  返回 `summary` 计数、按部门汇总的 `depts` 与逐项 `items`（带 `company_name`/`dept_name`；
  同名部门用 `company_name` 区分）。
  判定**只与上一期（上月）快照比**：同月手动查多少次，结论都不变。
  没有上期时 `mode='baseline'`（`baseline=true`），只说明"已建立基线"，不要说"未更新"。

  Args:
    sync: 是否**现在**去下载并重算哈希。默认 false = 直接读最近一次快照的比对
      结果，秒回。只有用户明确说「现在查一下 / 重新检查 / 更新一下」时才传 true。
      带 `name` 时只下载匹配到的那几项（约 1~3 秒），不动其余项。
    name: 要看**某一个**文档时传它的**文件名**（如「负载管理统计分析表」
      「供配电程序文档目录」），不要传编号、也不要为了一个文件传 sync 跑整期。
      留空 = 全部 25 条月度项。命中多项（如只说「隐患排查表」）会返回
      `candidates` 让用户指明，此时**不会下载**任何文件。
  """
  if not settings.DOC_UPDATE_CHECK_ENABLED:
    return _err('文档更新检查未开启（DOC_UPDATE_CHECK_ENABLED=false）。')

  from core.agent.user_context import get_current_user_id
  from service.doc_update import catalog, workflow

  keyword = (name or '').strip()
  if keyword:
    items = catalog.resolve_items(keyword)
    if not items:
      return json.dumps({
        'error': f'没找到匹配「{keyword}」的文档（本功能只覆盖 25 条月度项）。',
        'available': _catalog_pairs(catalog.MONTHLY_ITEMS),
        'note': '请用清单里的文件名再问一次，或让用户确认是不是实时/年度/季度项。',
      }, ensure_ascii=False)
    if len(items) > 1:
      return json.dumps({
        'error': f'「{keyword}」匹配到多项，请让用户指明是哪一个（本次未下载）。',
        'candidates': _catalog_pairs(items),
      }, ensure_ascii=False)

  async def _progress(done: int, total: int, item_name: str) -> None:
    if done % _PROGRESS_EVERY and done != total:
      return
    try:
      from langchain_core.callbacks import adispatch_custom_event
      await adispatch_custom_event('doc_update_status', {
        'tool': 'doc_update_check',
        'message': f'正在检查文档更新：{done}/{total}（{item_name}）',
      })
    except Exception:  # noqa: BLE001 - 进度只是反馈
      pass

  if not sync:
    overview = await workflow.build_overview()
    latest = overview['latest']
    if not latest:
      return json.dumps({
        'baseline': True, 'mode': 'baseline', 'summary': _summary_counts({}),
        'items': [],
        'note': '还没有任何快照。若用户要现在检查，请以 sync=true 再调一次。' + _NOTE,
      }, ensure_ascii=False)
    verdict = overview['verdict']
    if keyword:
      verdict = workflow.subset_verdict(verdict, {i.seq for i in items})
    return json.dumps(
      _payload(verdict, latest.get('taken_at'), synced=False),
      ensure_ascii=False,
    )

  try:
    if keyword:
      result = await workflow.refresh_items(
        items, created_by=get_current_user_id() or 'agent', progress=_progress,
      )
      return json.dumps(
        _payload(result['verdict'], (result.get('snapshot') or {}).get('taken_at'),
                 synced=True, extra={
                   'stored': result['stored'],
                   'refresh_note': _refresh_note(result['stored']),
                 }),
        ensure_ascii=False,
      )
    result = await workflow.perform_sync(
      created_by=get_current_user_id() or 'agent', progress=_progress,
    )
  except Exception as e:  # noqa: BLE001
    logger.exception('[DocUpdate] 对话取快照失败')
    return _err(f'检查失败：{type(e).__name__}: {str(e)[:200]}')

  snapshot = result.get('snapshot') or {}
  return json.dumps(
    _payload(result['verdict'], snapshot.get('taken_at'), synced=True),
    ensure_ascii=False,
  )

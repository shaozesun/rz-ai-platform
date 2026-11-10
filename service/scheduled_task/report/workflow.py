"""报告工作流编排：取数 → 统计出图 → 详情 → 解读 → 排版 → PDF → 落库。

`runner.run_task` 对**登记了 spec 的预设**走这里；其余（其他 5 类预设 + 用户自定义任务）
完全走原来的 agent 路径，行为不变。

落库与产物路径**严格对齐** agent 路径，下游（报告页 / 下载端点 / 通知铃铛）零改动：
- `finish_run` 必写 status / artifact_urls / error / duration_ms
- 产物落 `{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/`，URL 段与磁盘目录名逐字相同
- **文件名纯 ASCII**：`api/v1/scheduled_tasks.py` 的 `resolve_artifact_path` 对 URL 不做解码，
  中文名会让下载/预览 404 且静默
- **无论如何都落库**（异常也必须 finish_run FAILED，否则记录永远停在 PENDING、页面卡「生成中」）
- **本期 0 单不是失败**：不生成 PDF，但落一条 SUCCESS 记录（「本期无工单」），页面可看出检查过
"""

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pandas as pd

from config.settings import settings
from core.sandbox.datasets import detail_cache_path, load_dataset_dir, save_dataset
from models.scheduled_task import RunStatus
from service.scheduled_task import repository as repo
from service.scheduled_task.report.common import _delta_text
from service.scheduled_task.report import (
  alarm_decision, alarm_link, highlights, impact, risks)
from service.scheduled_task.report.narrate import narrate, summarize_details
from service.scheduled_task.report.quality import assess_details
from service.scheduled_task.report.render import (
  build_html, draw_level_duration, render_pdf, run_compute,
)
from service.scheduled_task.report_templates import (
  PERIODS, discover_parks, period_of, prev_window, window_for,
)
from tools.analysis_tools import fetch_all_pages, fetch_row_details

logger = logging.getLogger(__name__)

_ARTIFACT_PREFIX = '/api/v1/chat/agent/artifacts/'
_DETAIL_TOP_N = 5        # 对重复次数最高的问题取详情（详情的原因/处理结果原文，是
                         # 「备件等待」这类有据可依的建议的唯一来源，3 条不够）
_SCOPE_NATIONWIDE = 'nationwide'
# 取数的有限重试：只吃上游短暂抖动（实测单发也会偶发失败），3 小时级故障重试无用
_RETRY_DELAYS = (5, 15)


class _NoTickets(Exception):
  """本期 0 单：不生成报告，但要留一条「本期无工单」的运行记录。"""


def _is_retryable(e: Exception) -> bool:
  """上游瞬时故障才算可重试：网络错误 / pen-mgmt 业务信封错误 / 5xx。

  参数、权限、配置类错误重试无意义（`MgmtError` 也用于这些场景，宁可多等 20 秒）。
  """
  from service.query.mgmt.http import MgmtError

  if isinstance(e, (MgmtError, httpx.TransportError)):
    return True
  return isinstance(e, httpx.HTTPStatusError) and e.response.status_code >= 500


async def _with_retry(what: str, fetch):
  """执行一次取数，失败按 `_RETRY_DELAYS` 退避重试；不在重试范围的异常直接抛。"""
  for attempt in range(len(_RETRY_DELAYS) + 1):
    try:
      return await fetch()
    except Exception as e:
      if attempt >= len(_RETRY_DELAYS) or not _is_retryable(e):
        raise
      delay = _RETRY_DELAYS[attempt]
      logger.warning('%s 失败（%s: %s），%ds 后重试（第 %d/%d 次）',
                     what, type(e).__name__, str(e)[:200], delay,
                     attempt + 1, len(_RETRY_DELAYS))
      await asyncio.sleep(delay)


async def _scope_params(park: str) -> tuple[dict, str]:
  """按预设 key 里的空间维度给出**确定性**取数参数 + 给报告读者看的范围名称。

  全国：`all_companies=true`（服务端逐公司合并）；园区：`company_id=<pen kingdee>`。
  园区清单固定在 `report_templates.PARKS`（新增园区改那张表），按 slug 反查 kingdee。
  返回的第二个值是「统计范围」的**业务表述**，写进 window.json 供第 13 章「数据说明」用
  （报告里要写人话，不能写 all_companies 这种参数名）。
  """
  if park == _SCOPE_NATIONWIDE:
    return {'all_companies': 'true'}, '全国各园区'
  for p in await discover_parks():
    if p['slug'] == park:
      return {'company_id': p['kingdee']}, p['name']
  raise RuntimeError(f'园区 {park} 不在平台可见公司列表里（可能已下线）')


async def _fetch_window(capability: str, scope: dict, start, end, user_id, max_rows):
  params = {**scope, 'start_time': start.isoformat(), 'end_time': end.isoformat()}
  records, truncated = await fetch_all_pages(capability, params,
                                             user_id=user_id, max_rows=max_rows)
  if truncated:
    logger.warning('报告取数达到上限 %d 行，本期统计不完整', max_rows)
  return records, params


def _cache_details(thread_id: str, dataset_id: str, details: dict, period: str,
                   window: tuple) -> None:
  """详情落盘（best-effort）：为后续「月报复用周报详情」打底，本轮不读取。

  放 `{会话目录}/detail_cache/{dataset_id}.json`（数据集目录之外，不会被当产物下载），
  淘汰数据集时由 `datasets._evict_if_needed` 连带删除。写失败只记日志，绝不影响报告。
  """
  if not details.get('rows'):
    return
  try:
    path = detail_cache_path(thread_id, dataset_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
      {'period': period, 'window': [window[0].isoformat(), window[1].isoformat()],
       'rows': details['rows']}, ensure_ascii=False), encoding='utf-8')
  except Exception as exc:                 # noqa: BLE001 — 缓存失败不是报告失败
    logger.warning('详情缓存写入失败（忽略）：%s', exc)


def _summary_text(spec, cur_win, stats: dict, narr: dict, pdf_name: str, thread_id: str,
                  dataset_id: str) -> str:
  """`final_reply`：与 agent 路径同形状（含 `<file>` 标签），便于排查与通知展示。"""
  unit = getattr(spec, 'count_unit', '起')
  lines = [f'《{spec.title}》已生成（{cur_win[0]} ~ {cur_win[1]}）。',
           f'共 {stats["total"]} {unit}，环比 {_delta_text(stats["mom"]["delta"])} {unit}。']
  lines += [f'- {p}' for p in (narr or {}).get('执行摘要', [])]
  url = f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{pdf_name}'
  lines.append(f'完整报告见 PDF 产物：<file url="{url}" name="{pdf_name}"/>')
  return '\n'.join(lines)


async def _notify_outcome(task, run, status, no_tickets: bool,
                          final_text: str, error: str) -> None:
  """写完成通知；**预设失败要发给运维**（`created_by='system'` 谁都看不到）。

  同一天的预设失败按批次聚合成一条（"本批 N 份"），否则上游一次故障会刷几十条铃铛。
  用户自建任务的成败仍只通知创建人。
  """
  recipients = [task.created_by]
  batch_key = ''
  if status == RunStatus.FAILED and task.created_by == repo.SYSTEM_USER_ID:
    recipients = await repo.alert_user_ids() or [task.created_by]
    batch_key = repo.alert_batch_key()

  if batch_key:
    title = '预设报告执行失败'
    body = f'「{task.name}」{(final_text or error).strip()[:120]}'
  else:
    title = (f'定时任务「{task.name}」本期无工单' if no_tickets else
             f"定时任务「{task.name}」执行{'成功' if status == RunStatus.SUCCESS else '失败'}")
    body = (final_text or error).strip()[:80]

  for user_id in recipients:
    await repo.create_notification(
      user_id=user_id, task_id=task.task_id, task_name=task.name, run_id=run.run_id,
      status=status.value, title=title, body=body, batch_key=batch_key,
    )


async def run_report_workflow(task, spec, now=None) -> None:
  """跑一份确定性报告。**不抛异常**（全部落库），由页面/通知体现成败。

  `now` 只给**补跑历史期次**用（传目标期的「次月 1 日」，与该期正常调度的触发时刻等价）；
  常规调度不传，按当前时刻算窗口与期号。
  """
  run_id = uuid.uuid4().hex
  thread_id = f'rz-agent-sched-{task.task_id}-{run_id}'
  period_key, period_label = period_of(task.template_id, now)
  run = await repo.create_run(task.task_id, run_id=run_id, thread_id=thread_id,
                              preset_key=task.template_id, period_key=period_key,
                              period_label=period_label)
  start_ts = time.time()
  error, final_text, artifacts = '', '', []
  no_tickets = False

  try:
    parsed = task.template_id.split('__')
    park, period = parsed[1], parsed[2]
    cur_win, prev_win = window_for(period, now), prev_window(period, now)
    # 平台能力注册：agent 路径是在 `AgentService._ensure_agent()` 里做的，工作流不建 agent
    # ——不补这一句，`get_capability` 会返回 None、取数直接失败（实测：RuntimeError 未知能力）。
    # 先判再注册，避免每次报告都重跑一遍注册（register_platform 幂等，但会清检索索引缓存）。
    from service.query.base.platform import get_capability

    if get_capability(spec.capability) is None:
      from service.query.bootstrap import register_enabled_platforms

      register_enabled_platforms()

    scope, scope_label = await _scope_params(park)
    user_id = settings.SCHEDULED_TASK_SERVICE_USER_ID or None

    cur_recs, cur_params = await _with_retry(
      '本期取数', lambda: _fetch_window(spec.capability, scope, cur_win[0], cur_win[1],
                                        user_id, spec.max_rows))
    if not cur_recs:
      raise _NoTickets()
    prev_recs, _ = await _with_retry(
      '上一期取数', lambda: _fetch_window(spec.capability, scope, prev_win[0], prev_win[1],
                                          user_id, spec.max_rows))

    info = save_dataset(thread_id, spec.capability, cur_params, cur_recs,
                        dataset_name=f'{spec.ticket}-report')
    dataset_id = info['dataset_id']
    ds_dir = load_dataset_dir(thread_id, dataset_id)
    # 上一期落成 prev.csv（沙箱统计脚本按文件名读；没有上一期就不写，脚本会自动降级）
    if prev_recs:
      pd.DataFrame(prev_recs).to_csv(ds_dir / 'prev.csv', index=False)
    (ds_dir / 'window.json').write_text(json.dumps(
      {'cur': [cur_win[0].isoformat(), cur_win[1].isoformat()],
       'prev': [prev_win[0].isoformat(), prev_win[1].isoformat()],
       'scope': scope_label, 'period': period}, ensure_ascii=False), encoding='utf-8')

    stats = await run_compute(ds_dir, Path(__file__).with_name(f'{spec.ticket}_compute.py'))

    # 详情：月报取代表单（5 条）；周报取**本期全部工单**（逐单看根因/处置，与月报形成区分）。
    # 代表单由统计层给出（`stats['detail_targets']`）：事件单＝按名称聚类的重复问题；
    # 故障单＝重复故障 + 设备类型 Top 补足（重复可能为 0）。周报优先放代表单，再补全量。
    targets = [str(n) for n in (stats.get('detail_targets') or [])[:_DETAIL_TOP_N] if n]
    if period == 'weekly':
      all_nos = [str(n).strip() for n in (stats.get('detail_all') or []) if str(n).strip()]
      nos = list(dict.fromkeys(targets + all_nos))      # 保序去重，代表单优先
      cap = max(1, int(settings.SCHEDULED_TASK_REPORT_WEEKLY_DETAIL_MAX))
      if len(nos) > cap:
        logger.warning('周报详情 %d 个工单编号超过上限 %d，只取前 %d 个（代表单优先）',
                       len(nos), cap, cap)
        nos = nos[:cap]
      detail_max = len(nos)
    else:
      nos = targets or [r['ticketNo'] for r in (stats.get('repeat') or [])[:_DETAIL_TOP_N]
                        if r.get('ticketNo')]
      detail_max = _DETAIL_TOP_N
    if nos:
      t_fetch = time.time()
      details = await _with_retry(
        '详情取数', lambda: fetch_row_details(
          thread_id, dataset_id, nos, user_id=user_id, max_rows=detail_max))
      rows = (details or {}).get('rows') or []
      # 全国口径：列表记录带 `companyName`（`_aggregate_companies` 逐公司回填），写回详情行
      # 供渲染层前置「园区」列。园区级取数没有该列 → 全空 → 不写任何键，行为不变。
      park_by_no = {str(r.get('ticketNo') or '').strip(): str(r.get('companyName') or '').strip()
                    for r in cur_recs if str(r.get('ticketNo') or '').strip()}
      if any(park_by_no.values()):
        for row in rows:
          park = park_by_no.get(str(row.get('ticketNo') or '').strip(), '')
          if park:
            row['park'] = park
      failed = sum(1 for r in rows if r.get('error'))
      logger.info('详情取数：请求 %d 条，成功 %d 条，失败 %d 条，耗时 %.1fs（period=%s）',
                  len(nos), len(rows) - failed, failed, time.time() - t_fetch, period)
      _cache_details(thread_id, dataset_id, details if rows else {}, period, cur_win)
    else:
      details = {}

    # 长文本归纳（第 10 章逐单表）：只对 spec 声明了 `detail_condense_fields` 的报告调用
    # （目前仅阈值单）—— 其余类型这一句直接返回 {}，不产生网络调用。
    details['summaries'] = await summarize_details(details, spec)

    # 影响时长分析（详情口径）：只对声明了 `impact_fields` 的类型做（当前仅事件单）。
    # 它与第 4 章的验收时长是**两个口径**，不能互相换算；有效样本不足时 analyze 自带降级。
    if getattr(spec, 'impact_fields', ()):
      details['impact'] = impact.analyze(details)
      imp = details['impact']
      if imp.get('available'):
        logger.info('影响时长：有效 %d/%d 单，P50 %s，最长 %s',
                    imp['valid'], imp['n'], imp['readable']['p50'], imp['readable']['max'])
      else:
        logger.info('影响时长分析降级：%s', imp.get('reason'))
      # 第 4 章的图在统计阶段只能用验收时长画（详情在这之后才取到）→ 周报用影响时长
      # **二次绘制覆盖同名文件**（文件名与图号都不变）。失败只记 warning：图退回验收口径，
      # 不让整份报告失败（标题/表格/事实由同一个 `facts.impact_level` 判定，会一并回退）。
      bl = (imp.get('by_level') or {}) if imp.get('available') else {}
      if period == 'weekly' and bl.get('levels') and bl.get('matrix'):
        try:
          await draw_level_duration(ds_dir, bl)
        except Exception as exc:  # noqa: BLE001 —— 出图失败不能拖垮报告
          logger.warning('影响时长图二次绘制失败（第 4 章退回验收口径）：%s', exc)

    # 详情质检（启发式）：详情取到后、解读之前做，渲染与解读共用同一份结果。
    # 事件/故障 spec 未声明 quality_fields → 只做字段汇总（也不声明）→ n=0，渲染层不出表。
    details['quality'] = assess_details(details, spec)
    n_q = details['quality']['n']
    if n_q:
      logger.info('详情质检：%d 条，依据具体 %d 条，疑似复制 %d 条，原因笼统 %d 条，'
                  '总结模板化 %d 条，未填总结 %d 条', n_q, details['quality']['specific_n'],
                  details['quality']['copy_n'], details['quality']['vague_reason_n'],
                  details['quality']['boilerplate_n'], details['quality']['no_summary_n'])

    # 风险分析（确定性）：按各类型关键指标分级，给「建议排查方向」；改进项优先对应到
    # 其中 高/中 等级的风险项，避免每章硬凑建议。不新增取数、不新增 LLM 调用。
    details['risk_analysis'] = risks.evaluate(stats, spec)

    # 正面结论（确定性）：`summary` 是**先印在第 12 章章首**的一段话（LLM 挂掉也照出），
    # `rows` 仍作依据给解读层。与风险分析一正一反，不恢复 A/B/C 处置评级。
    details['highlights'] = highlights.evaluate(stats, details, spec)
    if details['highlights'].get('summary'):
      logger.info('本期整体表现：%s', details['highlights']['summary'])

    # 重复类目关联 DCIM 告警（事件单专属）：取代表单发生时间点查 ±2 分钟，
    # 多候选由模型仲裁，给出告警值与四档阈值。失败只记日志，不影响报告。
    if spec.ticket == 'event':
      details['alarm_links'] = await alarm_link.link_repeat_alarms(
        stats, cur_recs, details, cur_win, park=park,
        store=alarm_decision.AlarmDecisionStore(),
        force_refresh=settings.SCHEDULED_TASK_REPORT_ALARM_REFRESH)
      link = details['alarm_links']
      logger.info('重复类目关联告警：可用=%s，已关联 %s 类，仲裁 %s 次，复用判定 %s 次%s',
                  link.get('available'), link.get('linked_n'), link.get('llm_calls'),
                  link.get('cache_hits'),
                  '' if link.get('available') else f"，原因：{link.get('reason')}")

    narr = await narrate(stats, details, spec)

    existing = {p.stem for p in (ds_dir / 'charts').glob('*.png')} if (ds_dir / 'charts').is_dir() else set()
    (ds_dir / 'report.html').write_text(
      build_html(stats, details, narr, spec, existing,
                 generated_at=datetime.now().strftime('%Y-%m-%d %H:%M'),
                 scope_label=scope_label,
                 period_label=PERIODS[period]['label']),
      encoding='utf-8')

    pdf_name = f'{spec.filename}-{cur_win[0]}_{cur_win[1]}.pdf'
    pdf_path = await render_pdf(ds_dir, pdf_name)
    if not pdf_path.is_file():          # 落库前断言：URL 段与磁盘必须对得上
      raise RuntimeError(f'PDF 未落盘：{pdf_path}')

    artifacts = [f'{_ARTIFACT_PREFIX}{thread_id}/{dataset_id}/{pdf_name}']
    final_text = _summary_text(spec, cur_win, stats, narr, pdf_name, thread_id, dataset_id)
  except _NoTickets:
    # 本期 0 单不是失败：留一条成功记录说明「检查过、没有工单」，页面上有据可查
    no_tickets = True
    final_text = (f'本期无工单，未生成报告。\n'
                  f'{spec.title}｜{cur_win[0]} ~ {cur_win[1]}')
    logger.info('本期无工单，跳过报告生成 task_id=%s window=%s~%s',
                task.task_id, cur_win[0], cur_win[1])
  except Exception as e:
    logger.exception('报告工作流失败 task_id=%s', task.task_id)
    error = f'{type(e).__name__}: {str(e)[:400]}'

  duration_ms = int((time.time() - start_ts) * 1000)
  status = RunStatus.FAILED if error else RunStatus.SUCCESS
  await repo.finish_run(run.run_id, status=status, final_reply=final_text,
                        artifact_urls=artifacts, error=error, duration_ms=duration_ms,
                        thread_id=thread_id)
  await _notify_outcome(task, run, status, no_tickets, final_text, error)
  logger.info('报告工作流完成 task_id=%s status=%s duration_ms=%s',
              task.task_id, status.value, duration_ms)

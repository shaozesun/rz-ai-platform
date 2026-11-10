"""一轮检测的编排：取详情 → 展平设备 → 下载图片（并发）→ 分批推理 → 落库。

失败口径（fail-closed，任何一环拿不到结论都记「检测失败」而不是默认放过）：
- 推理服务预检不可达 → 整轮失败（runs/tasks 落 failed + 明确错误），不逐图空转；
- 单个任务详情失败 → 该任务 failed，不中断其它任务（旧结果保留）；
- 单图下载/推理失败 → 该行「检测失败」+ 说明，其余照常；
- 连续 3 批连接失败 → 剩余标「检测服务中断」，该任务 `partial`。

**只对有图片的设备出行**：任务详情里的设备是设备台账，未参与本次巡检的那些没有
`picture`/`detectionTime`，不能算「检测失败」；整任务一张图都没有 → `result_status='no_image'`。
历史文档里已存的「无图片」行在读取时过滤（不删库内数据）。
"""

import asyncio
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

from config.settings import settings
from service.infrared_quality import cache, columns, fetch, predict, store, verdict

logger = logging.getLogger(__name__)

_MAX_BATCH_FAILURES = 3
_UNSAFE_NAME = re.compile(r'[^A-Za-z0-9_-]')


def _noop(**_kwargs) -> None:
  return None


def _safe_name(value: str) -> str:
  """任务/设备 id → 可用作目录名与文件名的安全串（防路径穿越）。"""
  return _UNSAFE_NAME.sub('_', str(value or ''))[:120]


def _image_name(device_id: str, picture: str) -> str:
  """本地留存文件名：设备 id + 原图后缀（后缀从预签名 URL 推断，默认 .jpg）。"""
  return f'{_safe_name(device_id)}{_ext_of(picture)}'


def _task_dir(task_id: str) -> str:
  return os.path.join(settings.INFRARED_QUALITY_IMAGE_DIR, _safe_name(task_id))


def image_file(task_id: str, name: str) -> str:
  """校验并返回本地图片路径；名字非法或文件不存在返回空串。"""
  if not name or os.path.basename(name) != name:
    return ''
  path = os.path.join(_task_dir(task_id), name)
  return path if os.path.isfile(path) else ''


def _missing_local(task_id: str, devices: list[dict]) -> list[dict]:
  """本地还没有图的设备（按 `_image_name` 判），这些需要下载。"""
  return [d for d in devices
          if not image_file(task_id, _image_name(d['device_id'], d['picture']))]


def photo_devices(devices: list[dict]) -> list[dict]:
  """只留本次巡检真正有图片的设备；其余是设备台账，不参与质量判定也不出行。"""
  return [d for d in devices if d.get('picture')]


def _visible(row: dict) -> bool:
  """历史结果里「无图片」的行不算检测结论，读取时不再展示。"""
  return row.get('说明') != '无图片'


def _ext_of(url: str) -> str:
  name = str(url or '').split('?', 1)[0].rsplit('/', 1)[-1]
  dot = name.rfind('.')
  if 0 < dot < len(name) - 1 and len(name) - dot <= 6:
    return name[dot:]
  return '.jpg'


async def _detail_with_retry(task_id: str) -> dict:
  last: Exception | None = None
  for attempt in (1, 2):
    try:
      return await fetch.task_detail(task_id)
    except Exception as exc:  # noqa: BLE001
      last = exc
      logger.warning('红外任务详情第 %s 次失败 %s: %s', attempt, task_id, exc)
      if attempt == 1:
        await asyncio.sleep(1)
  raise last  # type: ignore[misc]


async def _download_all(devices: list[dict], dest_dir: str) -> dict[str, str]:
  """并发下载有图片的设备 → `{device_id: 本地路径}`；失败不抛，缺的键即失败。

  落到**持久目录**（不是临时目录）：预签名地址 7 天过期后原图取不到，本地这一份是
  判定结论的证据。重检时同名覆盖。
  """
  sem = asyncio.Semaphore(max(1, settings.INFRARED_QUALITY_DOWNLOAD_CONCURRENCY))
  out: dict[str, str] = {}

  async def one(idx: int, dev: dict) -> None:
    device_id = dev['device_id']
    dest = os.path.join(dest_dir, _image_name(device_id, dev['picture']))
    async with sem:
      try:
        await fetch.download_image(dev['picture'], dest)
        out[device_id] = dest
      except Exception as exc:  # noqa: BLE001
        logger.debug('红外图片下载失败 %s: %s', device_id, exc)

  await asyncio.gather(*[one(i, d) for i, d in enumerate(devices)])
  return out


async def _check_task(task: dict, dept: dict, *, created_by: str, refresh: bool,
                      progress, phase_total: int, phase_done: int) -> dict:
  """检一个任务，返回落库文档。异常由调用方兜（这里只处理已知失败）。"""
  task_id = str(task.get('taskId') or task.get('inspectionTaskId') or '')
  started = time.monotonic()
  doc = _task_base(task_id, dept, task, created_by)

  try:
    detail = await _detail_with_retry(task_id)
  except Exception as exc:  # noqa: BLE001
    doc.update(result_status='failed', error=f'任务详情获取失败：{exc}',
               elapsed=round(time.monotonic() - started, 2))
    await _keep_previous(task_id, doc)
    return doc

  devices = fetch.flatten_devices(detail)
  photos = photo_devices(devices)
  # 详情的设备是台账（可能几百台），deviceCount 才是本次巡检的台数，别再写成展平数
  doc['device_count'] = int(detail.get('deviceCount') or len(devices) or 0)
  doc['photo_count'] = len(photos)
  doc['inspector'] = str(detail.get('inspector') or task.get('inspector') or '')
  doc['start_time'] = detail.get('startTime') or task.get('startTime') or 0
  doc['end_time'] = detail.get('endTime') or task.get('endTime') or 0

  if not photos:
    doc.update(rows=[], counts=columns.counts_of([]),
               elapsed=round(time.monotonic() - started, 2),
               checked_at=datetime.now(timezone.utc), checked_by=created_by,
               result_status='no_image',
               error=f'该任务无巡检图片（{len(devices)} 台设备均无图片）')
    return doc

  rows: list[dict] = []
  pending: list[dict] = []
  hits: list[dict] = []
  cached = {}
  if not refresh:
    cached = await cache.get_many(task_id, [d['device_id'] for d in photos])

  for i, dev in enumerate(photos):
    row = columns.build_row(
      seq=i + 1,
      company=dept.get('company_name') or '',
      dept=dept.get('dept_name') or '',
      task_id=task_id,
      inspector=doc['inspector'],
      type_name=dev['type_name'],
      name=dev['name'],
      num=dev['num'],
      code=dev['code'],
      site=dev['installation_site'],
      detection_time=dev['detection_time'],
      max_temp=dev['max_temperature'],
      upper=dev['upper_limit'],
      verdict='', score=None, note='', picture=dev['picture'],
    )
    # 隐藏键（不在 COLUMNS 里）：本地留存文件名，records 端据此把「图片链接」改写成平台端点
    row['_image'] = _image_name(dev['device_id'], dev['picture'])
    hit = cached.get(dev['device_id']) if dev['device_id'] else None
    if hit and hit.get('label'):
      judgement, note = verdict.classify(hit.get('label') or '', '')
      row.update(判定=judgement, 模型分数='' if hit.get('score') is None
                 else str(hit.get('score')), 说明=note)
      hits.append(dev)
      rows.append(row)
      continue
    pending.append({'row': row, 'dev': dev})

  predicted: dict[str, dict] = {}
  broken = False
  # 下载阶段按「本地缺不缺图」决定：pending 已有本地图直接复用；缓存命中的设备也补图
  # （老任务重跑一次检测即可补齐证据，不必勾「重新推理」）。补图失败只记日志，不影响判定。
  paths: dict[str, str] = {}
  to_fetch: list[dict] = []
  for p in pending:
    local = image_file(task_id, p['row']['_image'])
    if local:
      paths[p['dev']['device_id']] = local
    else:
      to_fetch.append(p['dev'])
  backfill = _missing_local(task_id, hits)
  if to_fetch or backfill:
    dest_dir = _task_dir(task_id)
    os.makedirs(dest_dir, exist_ok=True)
    progress(done=phase_done, total=phase_total, phase='predict',
             current=f'{task_id} · 下载 {len(to_fetch) + len(backfill)} 张')
    paths.update(await _download_all(to_fetch + backfill, dest_dir))
  if pending:
    total_imgs = len(pending)
    done_imgs = 0
    batches = predict.chunks([p for p in pending if p['dev']['device_id'] in paths],
                             settings.INFRARED_QUALITY_BATCH)
    failures = 0
    for batch_no, batch in enumerate(batches):
      if broken:
        break
      progress(done=phase_done, total=phase_total, phase='predict',
               current=f'{task_id} · {done_imgs}/{total_imgs} 张')
      local_paths = [paths[p['dev']['device_id']] for p in batch]
      try:
        result = await predict.client().predict_batch(local_paths)
        failures = 0
      except Exception as exc:  # noqa: BLE001
        failures += 1
        logger.warning('红外推理批失败（%s）: %s', task_id, exc)
        for p in batch:
          p['row'].update(判定=verdict.FAILED, 说明='推理服务中断')
        done_imgs += len(batch)
        if failures >= _MAX_BATCH_FAILURES:
          broken = True
        continue
      for p, path in zip(batch, local_paths):
        hit = result.get(path) or {'label': '', 'score': None, 'error': ''}
        judgement, note = verdict.classify(hit.get('label') or '', hit.get('error') or '')
        p['row'].update(
          判定=judgement,
          模型分数='' if hit.get('score') is None else str(hit.get('score')),
          说明=note,
        )
        predicted[p['dev']['device_id']] = hit
      done_imgs += len(batch)
    # 下载失败的图
    for p in pending:
      if p['dev']['device_id'] not in paths:
        judgement, note = verdict.download_failed(OSError('download'))
        p['row'].update(判定=judgement, 说明=note)
    # 因连续失败而没跑到批次的图
    if broken:
      for p in pending:
        if not p['row'].get('判定'):
          p['row'].update(判定=verdict.FAILED, 说明='检测服务中断')

  # pending 里的行按原顺序插回
  for p in pending:
    rows.append(p['row'])
  rows.sort(key=lambda r: r.get('序号') or 0)

  doc['rows'] = rows
  doc['counts'] = columns.counts_of(rows)
  doc['elapsed'] = round(time.monotonic() - started, 2)
  doc['checked_at'] = datetime.now(timezone.utc)
  doc['checked_by'] = created_by
  doc['result_status'] = 'partial' if broken else 'success'
  doc['error'] = '检测服务中断：部分图片未完成推理' if broken else ''

  if predicted:
    # 只缓存**模型原始标签**（sharp/borderline…）：缓存键要与 classify 的输入同义，
    # 存成「合格」这种判定词会让下次重检全部判成「检测失败」。
    await cache.put_many(task_id, [
      {'device_id': did, 'label': hit.get('label') or '', 'score': hit.get('score')}
      for did, hit in predicted.items() if hit.get('label')
    ])
  return doc


def _task_base(task_id: str, dept: dict, task: dict, created_by: str) -> dict:
  return {
    '_id': task_id,
    'dept_id': dept.get('dept_id') or '',
    'dept_name': dept.get('dept_name') or '',
    'company_id': dept.get('company_id') or '',
    'company_name': dept.get('company_name') or '',
    'inspector': str(task.get('inspector') or ''),
    'status': task.get('status'),
    'device_count': int(task.get('deviceCount') or 0),
    'start_time': task.get('startTime') or 0,
    'end_time': task.get('endTime') or 0,
    'checked_at': None,
    'checked_by': created_by,
    'result_status': '',
    'error': '',
    'elapsed': 0,
    'counts': {},
    'rows': [],
  }


async def _keep_previous(task_id: str, doc: dict) -> None:
  """本轮失败时保留上次的明细：旧结果仍是用户看过的结论，不能因为一次取数失败清空。"""
  try:
    prev = await store.get_task(task_id)
  except Exception:  # noqa: BLE001
    return
  if not prev or not prev.get('rows'):
    return
  doc['rows'] = prev['rows']
  doc['counts'] = prev.get('counts') or {}
  doc['checked_at'] = prev.get('checked_at')


def _local_image_url(task_id: str, name: str) -> str:
  """本地留存图片的平台端点；文件不存在返回空串（老记录回退原预签名链接）。"""
  if not name or not image_file(task_id, name):
    return ''
  return (f'/api/v1/scheduled-tasks/infrared-quality/image'
          f'?task_id={quote(task_id, safe="")}&name={quote(name, safe="")}')


def _merge_counts(items: list[dict]) -> dict[str, int]:
  total = {'合格': 0, '待复核': 0, '不合格': 0, '检测失败': 0}
  for item in items:
    for key, value in (item.get('counts') or {}).items():
      if key in total:
        total[key] += int(value or 0)
  return total


async def perform_check(dept: dict, tasks: list[dict], created_by: str = '',
                        progress=None, refresh: bool = False) -> dict:
  """勾选的任务逐个检测；返回 `{run_id, status, counts, error, task_ids}`。"""
  progress = progress or _noop
  run_id = uuid.uuid4().hex
  task_ids = [str(t.get('taskId') or t.get('inspectionTaskId') or '') for t in tasks]
  task_ids = [t for t in task_ids if t]
  started = time.monotonic()
  await store.create_run({
    'run_id': run_id,
    'dept_id': dept.get('dept_id') or '',
    'dept_name': dept.get('dept_name') or '',
    'company_id': dept.get('company_id') or '',
    'company_name': dept.get('company_name') or '',
    'task_ids': task_ids,
    'created_by': created_by,
    'status': 'running',
    'refresh': bool(refresh),
    'created_at': datetime.now(timezone.utc),
  })

  try:
    await predict.client().preflight()
  except predict.InfraredUnavailable as exc:
    error = str(exc)
    for task in tasks:
      task_id = str(task.get('taskId') or task.get('inspectionTaskId') or '')
      doc = _task_base(task_id, dept, task, created_by)
      doc.update(result_status='failed', error=error,
                 elapsed=round(time.monotonic() - started, 2))
      await store.save_task(task_id, doc)
    await store.finish_run(run_id, status='failed', counts={}, error=error,
                           elapsed=round(time.monotonic() - started, 2))
    return {'run_id': run_id, 'status': 'failed', 'counts': {}, 'error': error,
            'task_ids': task_ids}

  docs: list[dict] = []
  for i, task in enumerate(tasks):
    task_id = str(task.get('taskId') or task.get('inspectionTaskId') or '')
    progress(done=i, total=len(tasks), phase='detail', current=task_id)
    try:
      doc = await _check_task(task, dept, created_by=created_by, refresh=refresh,
                              progress=progress, phase_total=len(tasks), phase_done=i + 1)
    except Exception as exc:  # noqa: BLE001  单任务异常不能拖垮整轮
      logger.exception('红外检测任务失败 %s', task_id)
      doc = _task_base(task_id, dept, task, created_by)
      doc.update(result_status='failed', error=f'检测异常：{exc}')
      await _keep_previous(task_id, doc)
    docs.append(doc)
    await store.save_task(task_id, doc)
    progress(done=i + 1, total=len(tasks), phase='detail', current=task_id)

  counts = _merge_counts(docs)
  failed_tasks = [d for d in docs if d.get('result_status') == 'failed']
  partial = any(d.get('result_status') == 'partial' for d in docs)
  if docs and len(failed_tasks) == len(docs):
    status = 'failed'
  elif failed_tasks or partial:
    status = 'partial'
  else:
    status = 'success'
  error = '；'.join(f'{d["_id"]}: {d.get("error")}' for d in failed_tasks[:3])
  elapsed = round(time.monotonic() - started, 2)
  await store.finish_run(run_id, status=status, counts=counts, error=error,
                         elapsed=elapsed, task_ids=task_ids)
  return {'run_id': run_id, 'status': status, 'counts': counts, 'error': error,
          'task_ids': task_ids}


async def records_for(run_id: str = '', task_id: str = '') -> dict:
  """`{columns, records}`；`run_id` 合并各任务行并重排序号，`task_id` 取单个任务。

  历史文档里可能存着「无图片」的行（未巡检的台账设备），读取时过滤掉、不删库。
  """
  rows: list[dict] = []
  if run_id:
    run = await store.get_run(run_id)
    for tid in (run or {}).get('task_ids') or []:
      doc = await store.get_task(tid)
      rows.extend((doc or {}).get('rows') or [])
  elif task_id:
    doc = await store.get_task(task_id)
    rows = list((doc or {}).get('rows') or [])
  rows = [r for r in rows if _visible(r)]
  rows.sort(key=lambda r: (str(r.get('巡检任务') or ''), r.get('序号') or 0))
  out: list[dict] = []
  for i, r in enumerate(rows):
    row = dict(r, 序号=i + 1)
    local = _local_image_url(str(row.get('巡检任务') or ''), str(row.get('_image') or ''))
    if local:
      row['图片链接'] = local
    out.append(row)
  return {'columns': columns.COLUMNS, 'records': out}

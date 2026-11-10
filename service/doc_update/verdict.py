"""快照比对（纯函数，不连库）：两份快照 → 25 项状态。

**只有两种口径**（`evaluate` 返回的 `mode`）：
- `cross` 跨期：本期 vs **上一期（上月）** → 「已更新 / 未更新」（唯一判定口径）
- `baseline` 基线：没有上期可比，只建基线，**不说「未更新」**

参照物只能是上一期快照。**不得**拿同月早先的检查结果（或任何「上次执行」）当参照——
否则「文件已更新」会被下一次检查自己抹成「未更新」。参照由调用方（`store.previous`）保证。

状态键（`updated/stale/missing/failed`）：
- `updated`：sha256 与参照快照不同（参照里没有这个 fid 也算新增）
- `stale`：sha256 与参照快照相同
- `missing` 未找到：未查询到相关文件（按编号前缀匹配不到）
- `failed` 下载失败：取不到字节或类型校验不过 —— **不算「未更新」**
"""

from datetime import datetime, timezone

STATUS_UPDATED = 'updated'
STATUS_STALE = 'stale'
STATUS_MISSING = 'missing'
STATUS_FAILED = 'failed'
STATUS_BASELINE = 'baseline'

MODE_CROSS = 'cross'
MODE_BASELINE = 'baseline'

# 未找到项的说明文案（写快照时用它；读老快照时把旧文案归一成同一句）
MISSING_TEXT = '未查询到相关文件'
_LEGACY_MISSING_TEXT = '整树中未找到该编号的文件'

STATUS_LABELS = {
  STATUS_UPDATED: '已更新',
  STATUS_STALE: '未更新',
  STATUS_MISSING: '未找到',
  STATUS_FAILED: '下载失败',
  STATUS_BASELINE: '基线',
}

_STATUS_ORDER = (STATUS_UPDATED, STATUS_STALE, STATUS_MISSING, STATUS_FAILED,
                 STATUS_BASELINE)


def _prev_index(previous: dict | None) -> dict[str, str]:
  idx: dict[str, str] = {}
  for item in (previous or {}).get('items') or []:
    for f in item.get('files') or []:
      fid = str(f.get('fid') or '')
      if fid:
        idx[fid] = f.get('sha256') or ''
  return idx


def _prev_flat_index(previous: dict | None) -> dict[str, str]:
  """上期快照里所有文件的 sha256，按 fid 索引（部门分支用）。"""
  idx: dict[str, str] = {}
  for dept in (previous or {}).get('depts') or []:
    for item in dept.get('items') or []:
      for f in item.get('files') or []:
        fid = str(f.get('fid') or '')
        if fid:
          idx[fid] = f.get('sha256') or ''
  return idx


def _fids(snapshot: dict | None) -> set[str]:
  out: set[str] = set()
  for item in (snapshot or {}).get('items') or []:
    for f in item.get('files') or []:
      fid = str(f.get('fid') or '')
      if fid:
        out.add(fid)
  return out


def _days_ago(value, now: datetime | None) -> int | None:
  if not isinstance(value, datetime):
    return None
  ref = now or datetime.now(timezone.utc)
  if value.tzinfo is None:
    value = value.replace(tzinfo=timezone.utc)
  return max(0, (ref - value).days)


def _max_dt(values) -> datetime | None:
  dates = [v for v in values if isinstance(v, datetime)]
  return max(dates) if dates else None


def _file_verdict(f: dict, prev_sha: str, baseline: bool, labels: dict) -> dict:
  sha = f.get('sha256') or ''
  if not sha:
    status = STATUS_FAILED
  elif baseline:
    status = STATUS_BASELINE
  elif sha != prev_sha:
    status = STATUS_UPDATED
  else:
    status = STATUS_STALE
  return {**f, 'status': status, 'status_label': labels[status]}


def _item_status(files: list[dict], baseline: bool) -> str:
  if not files:
    return STATUS_MISSING
  if not any(f.get('sha256') for f in files):
    return STATUS_FAILED
  if baseline:
    return STATUS_BASELINE
  if any(f['status'] == STATUS_UPDATED for f in files):
    return STATUS_UPDATED
  return STATUS_STALE


def _error_text(text) -> str:
  """老快照里存的是旧文案，读出来统一成当前口径。"""
  text = text or ''
  return MISSING_TEXT if text == _LEGACY_MISSING_TEXT else text


def _period_status(files: list[dict], expected: str) -> tuple[str, str]:
  """按期提交型：本期应到期（或更晚）有文件即已更新，否则未更新。返回 (状态, 最新期)。"""
  periods = [f.get('period') or '' for f in files]
  periods = [p for p in periods if p]
  latest = max(periods) if periods else ''
  if latest and expected and latest >= expected:
    return STATUS_UPDATED, latest
  return STATUS_STALE, latest


def _flat_status(files: list[dict], prev: dict[str, str], baseline: bool) -> str:
  if not any(f.get('sha256') for f in files):
    return STATUS_FAILED
  if baseline:
    return STATUS_BASELINE
  if any((f.get('sha256') or '') != prev.get(str(f.get('fid') or ''), '')
         for f in files):
    return STATUS_UPDATED
  return STATUS_STALE


def _dept_key(dept: dict) -> tuple[str, str]:
  """部门键：公司 + dept_id（dept_id 空时退回名称）。"""
  return (str(dept.get('company_id') or ''),
          str(dept.get('dept_id') or dept.get('dept_name') or ''))


def _evaluate_depts(latest: dict, previous: dict | None,
                    now: datetime | None) -> dict:
  """部门分支：逐「部门 × 清单项」按 `layout` 判状态。"""
  prev_snapshot = previous if (previous or {}).get('depts') else None
  baseline = not prev_snapshot
  mode = MODE_BASELINE if baseline else MODE_CROSS
  labels = STATUS_LABELS
  prev = _prev_flat_index(prev_snapshot)
  prev_depts = {_dept_key(d) for d in (prev_snapshot or {}).get('depts') or []}
  counts = {key: 0 for key in _STATUS_ORDER}
  items: list[dict] = []
  warnings: list[dict] = []
  changed: list[dict] = []
  depts: list[dict] = []

  for dept in latest.get('depts') or []:
    dept_id = str(dept.get('dept_id') or '')
    dept_name = dept.get('dept_name') or ''
    company_id = str(dept.get('company_id') or '')
    company_name = dept.get('company_name') or ''
    dept_error = dept.get('error') or ''
    # 上期没有这个部门（新增公司/部门）→ 原地更新型按基线算，不误报「已更新」
    dept_baseline = baseline or _dept_key(dept) not in prev_depts
    dept_counts = {key: 0 for key in _STATUS_ORDER}

    for item in dept.get('items') or []:
      raw = item.get('files') or []
      layout = item.get('layout') or 'flat'
      expected = item.get('expected_period') or ''
      files = [dict(f) for f in raw]

      if dept_error:
        status = STATUS_FAILED
        latest_period = item.get('latest_period') or ''
      elif not files:
        status = STATUS_MISSING
        latest_period = item.get('latest_period') or ''
      elif layout == 'period':
        status, latest_period = _period_status(files, expected)
      else:
        status = _flat_status(files, prev, dept_baseline)
        latest_period = item.get('latest_period') or ''
      if not latest_period:
        latest_period = item.get('latest_period') or ''

      for f in files:
        if layout == 'period':
          period_value = f.get('period') or ''
          f_status = STATUS_UPDATED if (period_value and period_value >= expected) \
            else STATUS_STALE
        else:
          f_status = _file_verdict(f, prev.get(str(f.get('fid') or ''), ''),
                                   dept_baseline, labels)['status']
        f['status'] = f_status
        f['status_label'] = labels[f_status]

      counts[status] = counts.get(status, 0) + 1
      dept_counts[status] += 1

      changed_at = _max_dt([f.get('last_changed_at') for f in files if f.get('sha256')])
      for f in files:
        if f.get('warn'):
          warnings.append({
            'company_id': company_id, 'company_name': company_name,
            'dept_id': dept_id, 'dept_name': dept_name,
            'number': item.get('number') or '', 'name': item.get('name') or '',
            'file_name': f.get('file_name') or '', 'warn': f['warn'],
          })
        if f['status'] == STATUS_UPDATED and layout != 'period':
          changed.append({
            'company_id': company_id, 'company_name': company_name,
            'dept_id': dept_id, 'dept_name': dept_name,
            'number': item.get('number') or '', 'name': item.get('name') or '',
            'file_name': f.get('file_name') or '',
            'changed_at': f.get('last_changed_at'),
          })

      items.append({
        'company_id': company_id,
        'company_name': company_name,
        'dept_id': dept_id,
        'dept_name': dept_name,
        'seq': item.get('seq'),
        'number': item.get('number') or '',
        'name': item.get('name') or '',
        'cadence': item.get('cadence') or '',
        'note': item.get('note') or '',
        'layout': layout,
        'expected_period': expected,
        'latest_period': latest_period,
        # 实际命中的目录名（页面展示用，老快照没有这个字段就是空数组）
        'actual_dirs': item.get('actual_dirs') or [],
        'status': status,
        'status_label': labels[status],
        'error': dept_error or _error_text(item.get('error')),
        'file_count': len(files),
        'changed_at': changed_at,
        'changed_days_ago': _days_ago(changed_at, now),
        'files': files,
      })

    depts.append({
      'company_id': company_id,
      'company_name': company_name,
      'dept_id': dept_id,
      'dept_name': dept_name,
      'root_fid': dept.get('root_fid') or '',
      'error': dept_error,
      'baseline': dept_baseline,
      'counts': dept_counts,
      'total': sum(dept_counts.values()),
    })

  latest_fids = _dept_fids(latest)
  prev_fids = _dept_fids(prev_snapshot)
  return {
    'baseline': baseline,
    'mode': mode,
    'labels': labels,
    'scope': 'dept',
    'period_key': latest.get('period_key') or '',
    'period_label': latest.get('period_label') or '',
    'taken_at': latest.get('taken_at'),
    'counts': counts,
    'total': len(items),
    'dept_count': len(depts),
    'depts': depts,
    'items': items,
    'changes': {
      'changed': changed,
      'added_fids': sorted(latest_fids - prev_fids),
      'removed_fids': sorted(prev_fids - latest_fids) if prev_snapshot else [],
    },
    'warnings': warnings,
  }


def _dept_fids(snapshot: dict | None) -> set[str]:
  out: set[str] = set()
  for dept in (snapshot or {}).get('depts') or []:
    for item in dept.get('items') or []:
      for f in item.get('files') or []:
        fid = str(f.get('fid') or '')
        if fid:
          out.add(fid)
  return out


def evaluate(latest: dict | None, previous: dict | None,
             now: datetime | None = None) -> dict:
  """比对两份快照。

  `previous` 必须是**上一期（上月）**快照；`None` 表示没有上期，只建基线。
  **不要**传同月早先的检查结果 —— 那会把「已更新」在下次检查时抹成「未更新」。
  快照带 `depts` 时走部门分支，否则走旧单树分支。
  """
  if (latest or {}).get('depts'):
    return _evaluate_depts(latest, previous, now)
  baseline = not previous
  mode = MODE_BASELINE if baseline else MODE_CROSS
  labels = STATUS_LABELS
  prev = _prev_index(previous)
  counts = {key: 0 for key in _STATUS_ORDER}
  items: list[dict] = []
  warnings: list[dict] = []
  changed: list[dict] = []

  for item in (latest or {}).get('items') or []:
    raw = item.get('files') or []
    files = [
      _file_verdict(f, prev.get(str(f.get('fid') or ''), ''), baseline, labels)
      for f in raw
    ]
    status = _item_status(files, baseline)
    counts[status] = counts.get(status, 0) + 1

    changed_at = _max_dt([f.get('last_changed_at') for f in files if f.get('sha256')])
    for f in files:
      if f.get('warn'):
        warnings.append({
          'number': item.get('number') or '',
          'name': item.get('name') or '',
          'file_name': f.get('file_name') or '',
          'warn': f['warn'],
        })
      if f['status'] == STATUS_UPDATED:
        changed.append({
          'number': item.get('number') or '',
          'name': item.get('name') or '',
          'file_name': f.get('file_name') or '',
          'changed_at': f.get('last_changed_at'),
        })

    items.append({
      'seq': item.get('seq'),
      'number': item.get('number') or '',
      'name': item.get('name') or '',
      'cadence': item.get('cadence') or '',
      'note': item.get('note') or '',
      'status': status,
      'status_label': labels[status],
      'error': _error_text(item.get('error')),
      'file_count': len(files),
      'changed_at': changed_at,
      'changed_days_ago': _days_ago(changed_at, now),
      'files': files,
    })

  latest_fids = _fids(latest)
  prev_fids = _fids(previous)
  return {
    'baseline': baseline,
    'mode': mode,
    'labels': labels,
    'period_key': (latest or {}).get('period_key') or '',
    'period_label': (latest or {}).get('period_label') or '',
    'taken_at': (latest or {}).get('taken_at'),
    'counts': counts,
    'total': len(items),
    'items': items,
    'changes': {
      'changed': changed,
      'added_fids': sorted(latest_fids - prev_fids),
      'removed_fids': sorted(prev_fids - latest_fids) if previous else [],
    },
    'warnings': warnings,
  }


def summary_text(verdict: dict) -> str:
  """一句话结论（工具/页面抬头共用）：基线期不说「未更新」。"""
  counts = verdict.get('counts') or {}
  total = verdict.get('total') or 0
  if verdict.get('scope') == 'dept':
    parts = (f"已更新 {counts.get(STATUS_UPDATED, 0)} · "
             f"未更新 {counts.get(STATUS_STALE, 0)} · "
             f"未找到 {counts.get(STATUS_MISSING, 0)} · "
             f"下载失败 {counts.get(STATUS_FAILED, 0)}")
    if counts.get(STATUS_BASELINE):
      parts += f" · 基线 {counts.get(STATUS_BASELINE, 0)}"
    companies = {d.get('company_id') or d.get('company_name')
                 for d in verdict.get('depts') or []}
    prefix = f'{len(companies)} 家公司 · ' if len(companies) > 1 else ''
    head = (f"{verdict.get('period_label') or ''} {prefix}"
            f"{verdict.get('dept_count') or 0} 个部门 × 共 {total} 格：")
    return f'{head}{parts}'
  if verdict.get('baseline'):
    return f'已建立基线（{total} 项），下期起比对是否更新。'
  parts = (f"已更新 {counts.get(STATUS_UPDATED, 0)} · "
           f"未更新 {counts.get(STATUS_STALE, 0)} · "
           f"未找到 {counts.get(STATUS_MISSING, 0)} · "
           f"下载失败 {counts.get(STATUS_FAILED, 0)}")
  return f'{verdict.get("period_label") or ""} 共 {total} 项：{parts}'

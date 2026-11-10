"""问题工单报告的固定统计与绘图 —— **全确定性，0 次 LLM**。

与其它三类同契约（`compute_stats` / `draw_charts` / `main`），但**维度刻意不同构**：
问题单的价值在**存量健康度**（压单多久、录入多慢、哪些问题反复发生），
不是"验收快不快"或"哪类设备坏得多"。

实测口径（2026-10-17 用真实接口核对）：

- 列表接口的 `start_time/end_time` 过滤的是 **`occurrenceTime`（问题发生时间）**：
  08-21 发生、08-28 创建的单出现在 08-21 窗口，08-28 窗口返回 0 条。故统计期归属
  以发生时间为准；极少数缺失时用创建时间补位并单独计数。
- 列表**没有关闭/解决时间**（`deleteTime` 是软删标记）→ 只能统计「当前仍未关闭」的
  滞留天数，不能统计真实解决时长。
- `stage` / `status` 是**当前快照**，不是流转历史。
- `checkEndApprove` 实测普遍未回填，故验收时长不做主指标。
"""

import json
import re
from pathlib import Path

import pandas as pd

# 共用纯助手：本进程 import 走这条；沙箱里源码已拼接注入（import 失败即忽略）
try:
  from service.scheduled_task.report.common import (
    CHART_MIN_ROWS, _add_compare, _all_ticket_nos, _band_score, _bar_width,
    _build_scorecard, _date_axis, _day_str, _draw_rhythm, _draw_scorecard, _dumbbell, _emit,
    _human_minutes, _int_ticks, _legend_below, _month_rhythm,
    _delta_text, _matrix_counts, _mom_attribution, _mom_top, _pct, _pct_text,
    _peak_day_extras, _repeat_problems, _scale_score, _stacked_barh, _top_card, _trim0,
    _window_trend,
  )
except ImportError:      # 沙箱：名字已随 common 源码注入
  pass

SPECIALTY_LABEL = {1: '供配电', 2: '制冷', 4: '智能化', 8: '其他'}
LEVEL_LABEL = {0: '0级', 1: '1级', 2: '2级', 3: '3级', 4: '4级'}
STAGE_LABEL = {-1: '驳回期', 2: '处理期', 3: '验收期', 5: '关闭期', 7: '派发期'}
STAGE_ORDER = ['处理期', '验收期', '派发期', '关闭期', '驳回期']
STATUS_LABEL = {0: '无', 1: '通过', 2: '驳回', 3: '审批中', 4: '实施中',
                5: '撤回', 6: '退回', 7: '验收中', 8: '审批通过'}
_UNKNOWN = '未标注'
_CLOSED_STAGE = 5

# 滞留 6 档（天，固定边界 → 跨期可比；右开区间）
_AGING_BANDS = (
  ('≤7天', lambda d: d <= 7),
  ('8-14天', lambda d: 7 < d <= 14),
  ('15-30天', lambda d: 14 < d <= 30),
  ('31-60天', lambda d: 30 < d <= 60),
  ('61-90天', lambda d: 60 < d <= 90),
  ('>90天', lambda d: d > 90),
)
_AGING_LABELS = [b[0] for b in _AGING_BANDS]

# 周报 4 档：窗口只有 7 天，8-14/15-30/31-60/61-90/>90 天五档必然全空，月报档位直接套过来
# 会让整个老化章变成一根柱。周报改看「本期内发生且未关闭」的 1/3/5/7 天滞留。
_WEEKLY_AGING_BANDS = (
  ('≤1天', lambda d: d <= 1),
  ('1-3天', lambda d: 1 < d <= 3),
  ('3-5天', lambda d: 3 < d <= 5),
  ('5-7天', lambda d: 5 < d <= 7),
)


def _aging_bands(window: dict) -> tuple:
  """按周期选滞留档位：周报 4 档（≤7 天），月报 6 档（≤90+ 天）。"""
  return _WEEKLY_AGING_BANDS if (window or {}).get('period') == 'weekly' else _AGING_BANDS

# 录入滞后 5 档（小时，固定边界）
_LAG_BANDS = (
  ('≤1时', 0, 1),
  ('1-24时', 1, 24),
  ('1-3天', 24, 72),
  ('3-7天', 72, 168),
  ('>7天', 168, 10 ** 9),
)
_LAG_LABELS = [b[0] for b in _LAG_BANDS]

_EQUIP_SPLIT = re.compile(r'[、，,；;。\n]+')


def _num(df: pd.DataFrame, column: str) -> pd.Series:
  """取数值列（缺失列返回全 NaN，非法值转 NaN）。"""
  if column not in df.columns:
    return pd.Series([float('nan')] * len(df), index=df.index, dtype='float64')
  return pd.to_numeric(df[column], errors='coerce')


def _human_hours(h) -> str:
  if h is None:
    return '—'
  return f'{_trim0(h)} 小时' if h < 48 else f'{_trim0(h / 24)} 天'


def _dist(values: list) -> dict:
  """数值分布（天/小时由调用方决定单位）：P50/P90/均值/最长 + 可读表述。"""
  vals = [float(v) for v in values if v is not None and v >= 0]
  if not vals:
    return {'p50': None, 'p90': None, 'mean': None, 'max': None, 'readable': {}}
  s = pd.Series(vals)
  q50, q90 = float(s.quantile(0.5)), float(s.quantile(0.9))
  return {'p50': round(q50, 1), 'p90': round(q90, 1),
          'mean': round(float(s.mean()), 1), 'max': round(float(s.max()), 1),
          'readable': {'p50': _human_minutes(q50 * 1440),
                       'p90': _human_minutes(q90 * 1440),
                       'mean': _human_minutes(float(s.mean()) * 1440),
                       'max': _human_minutes(float(s.max()) * 1440)}}


def _dist_hours(values: list) -> dict:
  """录入滞后分布（小时）。"""
  vals = [float(v) for v in values if v is not None and v >= 0]
  if not vals:
    return {'p50': None, 'p90': None, 'mean': None, 'max': None, 'readable': {}}
  s = pd.Series(vals)
  q50, q90 = float(s.quantile(0.5)), float(s.quantile(0.9))
  return {'p50': round(q50, 1), 'p90': round(q90, 1),
          'mean': round(float(s.mean()), 1), 'max': round(float(s.max()), 1),
          'readable': {'p50': _human_hours(q50), 'p90': _human_hours(q90),
                       'mean': _human_hours(float(s.mean())),
                       'max': _human_hours(float(s.max()))}}


def _scope(df: pd.DataFrame, window: dict, which: str = 'cur'
           ) -> tuple[pd.DataFrame, int, int, int]:
  """按「问题发生时间」归属统计期；缺失时用创建时间补位。

  返回 (保留的帧, 缺时间数, 窗口外数, 补位条数)。`which='prev'` 用上一期窗口。
  """
  if df is None or not len(df):
    return (df if df is not None else pd.DataFrame()), 0, 0, 0
  win = (window or {}).get(which) or []
  if len(win) < 2:
    return df.copy(), 0, 0, 0
  start, end = str(win[0])[:10], str(win[1])[:10]
  occ, cre = _num(df, 'occurrenceTime'), _num(df, 'createTime')
  occ_has = occ.notna() & (occ > 0)
  cre_has = cre.notna() & (cre > 0)
  occ_days = _day_str(occ.where(occ_has))
  cre_days = _day_str(cre.where(cre_has))
  in_occ = occ_has & occ_days.notna() & (occ_days >= start) & (occ_days <= end)
  in_cre = (~occ_has) & cre_has & cre_days.notna() & (cre_days >= start) & (cre_days <= end)
  mask = in_occ | in_cre
  # 两个时间都没有才算"缺时间"；有创建时间但落在窗口外属于**别的统计期**，不计缺失
  missing = int((~occ_has & ~cre_has).sum())
  out = int((occ_has & ~in_occ).sum() + (~occ_has & cre_has & ~in_cre).sum())
  return df[mask].copy(), missing, out, int(in_cre.sum())


def _day_of(series: pd.Series) -> pd.Series:
  return _day_str(pd.to_numeric(series, errors='coerce'))


def _label_counts(series: pd.Series, order: list[str] | None = None) -> list[dict]:
  """标签列 → 稳定的 [{label,count,pct}]（给定 order 时按固定顺序）。"""
  vals = series.dropna().map(lambda v: str(v).strip()).replace('', _UNKNOWN)
  total = int(len(vals))
  vc = vals.value_counts()
  labels = order + [x for x in vc.index if x not in order] if order else sorted(vc.index)
  return [{'label': str(k), 'count': int(vc.get(k, 0)), 'pct': _pct(vc.get(k, 0), total)}
          for k in labels if int(vc.get(k, 0)) > 0]


def _level_series(df: pd.DataFrame) -> pd.Series:
  """级别码 → 「N级」；空值 → 未标注。"""
  vals = _num(df, 'level')
  return vals.map(lambda v: LEVEL_LABEL.get(int(v), f'{int(v)}级')
                  if pd.notna(v) else _UNKNOWN)


def _stage_series(df: pd.DataFrame) -> pd.Series:
  vals = _num(df, 'stage')
  return vals.map(lambda v: STAGE_LABEL.get(int(v), _UNKNOWN)
                  if pd.notna(v) else _UNKNOWN)


def _space_column(df: pd.DataFrame) -> str:
  return 'companyName' if 'companyName' in df.columns else ('dc' if 'dc' in df.columns else '')


def _in_window_end(window: dict, which: str = 'cur') -> pd.Timestamp | None:
  win = (window or {}).get(which) or []
  if len(win) < 2:
    return None
  return (pd.Timestamp(str(win[1])[:10], tz='Asia/Shanghai')
          + pd.Timedelta(days=1))


def _aging_days(df: pd.DataFrame, window: dict) -> dict:
  """未关闭问题的滞留天数（统计期末 − 发生时间），按行索引返回。"""
  end = _in_window_end(window)
  occ = _num(df, 'occurrenceTime')
  out: dict = {}
  if end is None:
    return out
  for i in df.index:
    ms = occ.at[i]
    if pd.notna(ms) and ms > 0:
      t = pd.to_datetime(int(ms), unit='ms', utc=True).tz_convert('Asia/Shanghai')
      out[i] = max(0.0, (end - t).total_seconds() / 86400.0)
  return out


def _aging_block(df: pd.DataFrame, window: dict, aging: dict) -> dict:
  """存量老化（仅未关闭）：规模、分档、分位、园区×档矩阵、最老单。"""
  stage = _num(df, 'stage')
  open_idx = [i for i in df.index if pd.notna(stage.at[i]) and int(stage.at[i]) != _CLOSED_STAGE]
  open_n = len(open_idx)
  vals = [aging[i] for i in open_idx if i in aging]
  bands = _aging_bands(window)
  labels = [b[0] for b in bands]
  counts = [sum(1 for v in vals if fn(v)) for _, fn in bands]
  space = _space_column(df)
  dcs, matrix = [], []
  if space:
    names = sorted({str(df.at[i, space]).strip() for i in open_idx
                    if str(df.at[i, space]).strip() not in ('', 'nan', 'None')})
    dcs = names
    matrix = [[sum(1 for i in open_idx if str(df.at[i, space]).strip() == name
                   and i in aging and fn(aging[i])) for _, fn in bands]
              for name in names]

  rows = []
  for i in sorted(open_idx,
                  key=lambda i: (-(aging.get(i) or 0), str(df.at[i, 'ticketNo']).strip()
                                 if 'ticketNo' in df.columns else '')):
    d = aging.get(i)
    rows.append({'ticketNo': str(df.at[i, 'ticketNo']).strip()
                 if 'ticketNo' in df.columns else '',
                 '空间': str(df.at[i, space]) if space else '',
                 '问题': str(df.at[i, 'projectName']) if 'projectName' in df.columns else '',
                 '阶段': STAGE_LABEL.get(int(stage.at[i]), _UNKNOWN)
                 if pd.notna(stage.at[i]) else _UNKNOWN,
                 '滞留天数': round(d, 1) if d is not None else None})
    if len(rows) >= 10:
      break

  over_days = 3 if (window or {}).get('period') == 'weekly' else 30
  return {**_dist(vals), 'open_n': open_n, 'rate': _pct(open_n, len(df)),
          'bins': labels, 'counts': counts,
          # over_30_n 保留给旧消费方/月报断言；周报的「超期」口径由 over_days/over_n 决定
          'over_days': over_days,
          'over_n': sum(1 for v in vals if v > over_days),
          'over_label': f'超 {over_days} 天未关闭',
          'over_30_n': sum(1 for v in vals if v > 30),
          'rows': rows, 'dc_aging': {'dcs': dcs, 'bins': labels, 'matrix': matrix}}


def _entry_lag_block(df: pd.DataFrame) -> dict:
  """录入滞后（创建 − 发生，小时）；负值属补录/脏数据，剔除并计数。"""
  occ, cre = _num(df, 'occurrenceTime'), _num(df, 'createTime')
  vals, invalid_n, missing_n = [], 0, 0
  for i in df.index:
    o, c = occ.at[i], cre.at[i]
    if pd.isna(o) or pd.isna(c) or o <= 0 or c <= 0:
      missing_n += 1
      continue
    diff = float((c - o) / 3600000.0)
    if diff < 0:
      invalid_n += 1
    else:
      vals.append(diff)
  counts = [sum(1 for v in vals if lo <= v < hi) for _, lo, hi in _LAG_BANDS]
  return {**_dist_hours(vals), 'valid': len(vals), 'invalid_n': invalid_n,
          'missing_n': missing_n, 'bins': _LAG_LABELS, 'counts': counts}


def _problem_objects(df: pd.DataFrame) -> dict:
  """涉及设备/系统：按中英文标点切分后计数（自由文本字段，平台未结构化）。"""
  if 'involveEquipment' not in df.columns:
    return {'rows': [], 'covered_n': 0, 'blank_n': int(len(df)), 'classes': 0}
  counts: dict = {}
  covered = 0
  for raw in df['involveEquipment']:
    text = '' if pd.isna(raw) else str(raw)
    items = [x.strip(' 。.') for x in _EQUIP_SPLIT.split(text)]
    items = [x for x in items if x and x not in ('nan', 'None', '无', '—')]
    if items:
      covered += 1
    for item in items:
      counts[item] = counts.get(item, 0) + 1
  rows = sorted(({'name': k, 'count': int(v)} for k, v in counts.items()),
                key=lambda r: (-r['count'], r['name']))[:10]
  return {'rows': rows, 'covered_n': covered,
          'blank_n': int(len(df)) - covered, 'classes': len(counts)}


def _normalize_names(df: pd.DataFrame) -> pd.DataFrame:
  """问题名称去首尾、折叠连续空白（源数据里同名存在前导空格）。"""
  if 'projectName' not in df.columns:
    return df
  out = df.copy()
  out['projectName'] = out['projectName'].map(
    lambda v: ' '.join(str(v).split()) if pd.notna(v) else '')
  return out


def _park_block(cur: pd.DataFrame, prev: pd.DataFrame, total: int) -> list[dict]:
  space = _space_column(cur)
  if not space:
    return []
  prev_counts = prev[space].value_counts().to_dict() if space in prev.columns else {}
  rows = []
  for name, cnt in cur[space].value_counts().items():
    n = int(cnt)
    rows.append({'name': str(name), 'count': n, 'pct': _pct(n, total),
                 'prev': int(prev_counts.get(name, 0))})
  _add_compare(rows, int(len(prev)))
  return rows


def _specialty_block(cur: pd.DataFrame, prev: pd.DataFrame) -> list[dict]:
  """专业分布（本期/上一期）。未知码不冒充「N级」，用「专业N」原样陈列。"""
  if 'specialty' not in cur.columns:
    return []
  vals = _num(cur, 'specialty').dropna()
  total = int(len(vals))
  counts = vals.value_counts()
  rows = []
  for value in sorted(counts.index):
    label = SPECIALTY_LABEL.get(int(value), f'专业{int(value)}')
    n = int(counts[value])
    rows.append({'label': label, 'count': n, 'pct': _pct(n, total), 'prev': 0})
  if 'specialty' in prev.columns:
    prev_counts: dict = {}
    for value, n in prev['specialty'].value_counts().items():
      label = SPECIALTY_LABEL.get(int(value), f'专业{int(value)}')
      prev_counts[label] = prev_counts.get(label, 0) + int(n)
    for row in rows:
      row['prev'] = prev_counts.get(row['label'], 0)
  # 上一期占比的分母＝上一期非空专业计数（本期 `total` 也是非空计数）
  _add_compare(rows, int(prev['specialty'].notna().sum())
               if 'specialty' in prev.columns else 0)
  return rows


def _level_stage_block(cur: pd.DataFrame, levels: list[dict]) -> dict:
  """级别 × 当前阶段矩阵（仅列出现过的阶段；未标注单独成列，保证行合计等于该级别单量）。"""
  level_labels = [r['label'] for r in levels]
  if not len(cur):
    return {'levels': level_labels, 'stages': [], 'matrix': [],
            'rows': [{'级别': lb, '合计': 0} for lb in level_labels]}
  lv, stages = _level_series(cur), _stage_series(cur)
  present = {s for s in stages.unique() if s != _UNKNOWN}
  stage_labels = [s for s in STAGE_ORDER if s in present]
  if _UNKNOWN in set(stages):
    stage_labels.append(_UNKNOWN)
  if not stage_labels:
    stage_labels = list(STAGE_ORDER)
  rows, matrix = [], []
  for label in level_labels:
    sub = stages[lv == label]
    vals = [int((sub == s).sum()) for s in stage_labels]
    rows.append({'级别': label, **dict(zip(stage_labels, vals)), '合计': int(sum(vals))})
    matrix.append(vals)
  return {'levels': level_labels, 'stages': stage_labels, 'matrix': matrix, 'rows': rows}


def _quality_block(cur: pd.DataFrame, repeat: list[dict]) -> dict:
  total = int(len(cur))
  stage = _num(cur, 'stage')
  closed_n = int((stage == _CLOSED_STAGE).sum()) if len(cur) else 0
  reject_n = int((_num(cur, 'status') == 2).sum()) if len(cur) else 0
  repeat_n = int(sum(r['count'] for r in repeat))
  return {
    'close_n': closed_n, 'close_base': total, 'close_rate': _pct(closed_n, total),
    'reject_n': reject_n, 'reject_base': total, 'reject_rate': _pct(reject_n, total),
    'open_n': total - closed_n, 'open_rate': _pct(total - closed_n, total),
    'repeat_n': repeat_n, 'repeat_base': total, 'repeat_rate': _pct(repeat_n, total),
    'repeat_classes': len(repeat),
  }


# ==================== 综合评分（雷达图）====================

_SCORE_DIMS = (
  ('规模控制', '单量较上一期增长按增幅扣分，持平或下降满分（无上期基数不扣分）。'),
  ('闭环进度', '期末已关闭的问题单占比（100 − 未关闭率）。'),
  ('老化管控', '未关闭单中超期滞留单占比越低越好（0 单未关闭时记满分）。'),
  ('录入及时', '从问题发生到建单的滞后 P90 分档给分：≤1 小时满分，越长越低。'),
  ('重复管控', '未落在重复问题簇中的问题单占比（100 − 重复单占比）。'),
)

# 录入滞后 P90（小时）→ 分：≤1h 100、≤24h 85、≤72h 70、≤168h 55，更长 40
_LAG_SCORE_BANDS = ((1, 100), (24, 85), (72, 70), (168, 55), (10 ** 9, 40))


def _score_inputs(df: pd.DataFrame, window: dict) -> dict:
  """本期/上一期各算一遍的评分原始指标（与章节统计同口径）。"""
  total = int(len(df)) if df is not None else 0
  aging = _aging_days(df, window)
  aging_block = _aging_block(df, window, aging)
  entry_lag = _entry_lag_block(df)
  quality = _quality_block(df, _repeat_problems(df))
  open_n, over_n = int(aging_block['open_n']), int(aging_block['over_n'])
  return {
    'total': total,
    'close_rate': quality['close_rate'],
    'over_rate': _pct(over_n, open_n) if open_n else 0.0,
    'lag_p90': entry_lag.get('p90'),
    'repeat_rate': quality['repeat_rate'],
  }


def _scores(metrics: dict, prev_total: int) -> dict:
  """指标 → 0–100 分（各维度均"越高越好"）。"""
  return {
    '规模控制': _scale_score(metrics['total'], prev_total),
    '闭环进度': float(metrics['close_rate'] or 0),
    '老化管控': 100.0 - float(metrics['over_rate'] or 0),
    '录入及时': _band_score(metrics['lag_p90'], _LAG_SCORE_BANDS),
    '重复管控': 100.0 - float(metrics['repeat_rate'] or 0),
  }


def _detail_targets(cur: pd.DataFrame, aging: dict, repeat: list[dict]) -> list[str]:
  """代表单：最老的未关闭 → 重复问题代表 → 编号兜底，去重取 5。"""
  nos: list[str] = []

  def _no(i) -> str:
    return str(cur.at[i, 'ticketNo']).strip() if 'ticketNo' in cur.columns else ''

  def add(no: str) -> None:
    no = (no or '').strip()
    if no and no not in nos:
      nos.append(no)

  stage = _num(cur, 'stage')
  open_idx = [i for i in cur.index
              if pd.notna(stage.at[i]) and int(stage.at[i]) != _CLOSED_STAGE]
  for i in sorted(open_idx, key=lambda i: (-(aging.get(i) or 0), _no(i))):
    if len(nos) >= 3:
      break
    add(_no(i))
  for r in repeat:
    if len(nos) >= 5:
      break
    add(r.get('ticketNo', ''))
  for i in sorted(cur.index, key=_no):
    if len(nos) >= 5:
      break
    add(_no(i))
  return nos[:5]


def compute_stats(df_cur: pd.DataFrame, df_prev: pd.DataFrame, window: dict) -> dict:
  """本期/上一期问题单 → 报告需要的全部统计（确定性，可单测）。"""
  raw_cur = int(len(df_cur)) if df_cur is not None else 0
  cur, missing_start, out_of_window, fallback_n = _scope(df_cur, window, 'cur')
  prev, _, _, _ = _scope(df_prev, window, 'prev')
  cur, prev = _normalize_names(cur), _normalize_names(prev)
  total, prev_total = int(len(cur)), int(len(prev))

  cur_days = _day_of(cur['occurrenceTime']) if 'occurrenceTime' in cur.columns \
    else pd.Series([], dtype=object)
  day_counts = cur_days.value_counts().sort_index()
  trend = _window_trend(cur, prev, 'occurrenceTime', window)
  mom = {'prev_total': prev_total, 'delta': total - prev_total,
         'pct': _pct(total - prev_total, prev_total) if prev_total else None}

  levels = _level_series(cur)
  level = _label_counts(levels, [LEVEL_LABEL[k] for k in sorted(LEVEL_LABEL)])
  level_stage = _level_stage_block(cur, level)
  specialty = _specialty_block(cur, prev)
  park = _park_block(cur, prev, total)

  # 园区 × 专业（横向堆叠条）：只在全国口径（有 companyName）且园区/专业均 ≥2 时产出
  parks = [p['name'] for p in park] if 'companyName' in cur.columns else []
  spec_vals = [SPECIALTY_LABEL.get(int(v)) if pd.notna(v) else None
               for v in _num(cur, 'specialty')]
  specialties = [SPECIALTY_LABEL[c] for c in sorted(SPECIALTY_LABEL)
                 if sum(1 for v in spec_vals if v == SPECIALTY_LABEL[c]) > 0]
  park_specialty = {'parks': [], 'specialties': [], 'matrix': []}
  if len(parks) >= 2 and len(specialties) >= 2:
    park_specialty = {
      'parks': parks, 'specialties': specialties,
      'matrix': _matrix_counts(cur['companyName'].astype(str).tolist(), spec_vals,
                               parks, specialties),
    }

  aging = _aging_days(cur, window)
  aging_block = _aging_block(cur, window, aging)
  entry_lag = _entry_lag_block(cur)
  objects = _problem_objects(cur)
  repeat = _repeat_problems(cur, str((window or {}).get('scope') or ''))
  quality = _quality_block(cur, repeat)

  # 综合评分：上一期按与本期完全相同的口径重算（老化/录入滞后都用上一期窗口期末）
  prev_window = {'cur': (window or {}).get('prev'),
                 'period': (window or {}).get('period')}
  cur_m, prev_m = _score_inputs(cur, window), _score_inputs(prev, prev_window)
  cur_scores = _scores(cur_m, prev_m['total'])
  prev_scores = _scores(prev_m, cur_m['total'])
  scorecard = _build_scorecard([
    (dim, meaning, cur_scores[dim], prev_scores[dim]) for dim, meaning in _SCORE_DIMS
  ])

  space = _space_column(cur)
  mom_key = '园区环比变动最大' if space == 'companyName' else '部门环比变动最大'
  peak = _peak_day_extras(cur, cur_days, day_counts, total, SPECIALTY_LABEL)
  extras = {
    '各园区老化构成': aging_block['dc_aging']['matrix'] and [
      {'空间': name,
       **{lab: aging_block['dc_aging']['matrix'][i][j]
          for j, lab in enumerate(aging_block['dc_aging']['bins'])}}
      for i, name in enumerate(aging_block['dc_aging']['dcs'])],
    '最老未关闭单': aging_block['rows'][:5],
    mom_key: _mom_attribution(cur, prev, SPECIALTY_LABEL),
    '重复问题集中点': [
      {'问题': r['name'][:40], '次数': r['count'], '涉及机房数': len(r['dcs']),
       '最集中机房': r['集中机房'], '该机房单数': r['集中机房单数']}
      for r in repeat[:5]],
  }
  if peak:
    extras['峰值日构成'] = peak

  top_open = sorted(
    ({'ticketNo': str(cur.at[i, 'ticketNo']).strip() if 'ticketNo' in cur.columns else '',
      '空间': str(cur.at[i, space]) if space else '',
      '问题': str(cur.at[i, 'projectName']) if 'projectName' in cur.columns else '',
      '滞留天数': round(aging[i], 1)} for i in aging if aging[i] > 0),
    key=lambda r: (-r['滞留天数'], r['ticketNo']))[:5]
  extras['滞留最久未关闭单'] = top_open

  repeat_share = quality['repeat_rate']
  kpi = {
    'total': {'value': str(total), 'sub': f'上一期 {prev_total} 单'},
    'daily_avg': {'value': f'{round(total / max(len(trend["labels"]), 1), 1)}',
                  'sub': f'覆盖 {len(trend["labels"])} 天'},
    'mom': {'value': (_delta_text(mom["delta"]) if prev_total else '—'),
            'sub': (_pct_text(mom['pct'], signed=True)
                    if mom['pct'] is not None else '无上期基数')},
    'top_level': _top_card(level, '最多级别', '单'),
    'backlog': {'value': str(aging_block['open_n']),
                'sub': (f'占 {aging_block["rate"]}%（期末仍未关闭）'
                        if total else '无数据')},
    'aged_30': {'value': str(aging_block['over_n']),
                'sub': (f'占未关闭 {_pct(aging_block["over_n"], aging_block["open_n"])}%'
                        if aging_block['open_n'] else '无未关闭单')},
    'entry_lag_p90': {'value': (entry_lag['readable'].get('p90') or '—'),
                      'sub': f'{entry_lag["valid"]} 单可统计'},
    'repeat': {'value': f'{quality["repeat_n"]} 单',
               'sub': f'占 {repeat_share}% · {quality["repeat_classes"]} 类'},
  }

  out = {
    'window': window, 'total': total, 'prev_total': prev_total,
    'daily_avg': round(total / max(len(trend['labels']), 1), 1),
    'mom': mom, 'trend': trend, 'level': level, 'level_stage': level_stage,
    'specialty': specialty, 'park': park, 'park_specialty': park_specialty,
    'aging': {k: v for k, v in aging_block.items() if k != 'dc_aging'},
    'dc_aging': aging_block['dc_aging'],
    'entry_lag': entry_lag, 'problem_objects': objects, 'repeat': repeat,
    'quality': quality, 'kpi': kpi, 'extras': extras, 'scorecard': scorecard,
    'scope_filter': {'raw': raw_cur, 'kept': total, 'out_of_window': out_of_window,
                     'missing_start': missing_start, 'fallback_n': fallback_n,
                     'basis': '问题发生时间（缺失时用创建时间）',
                     'missing_label': '缺问题发生时间'},
    'detail_targets': _detail_targets(cur, aging, repeat),
    'detail_all': (_all_ticket_nos(cur)
                   if (window or {}).get('period') == 'weekly' else []),
    # 样本过少（一两条）：不出图、也不写图表解析 —— 章节仍以表格呈现（渲染层读这个键）
    'charts_suppressed': total < CHART_MIN_ROWS,
  }
  # 月报专属维度（周报**不写这两个键**，输出逐字不变）
  if (window or {}).get('period') == 'monthly':
    out['rhythm'] = _month_rhythm(trend, window, '单')
    out['mom_top'] = _mom_top(
      {'level': level, 'park': park, 'specialty': specialty},
      [('级别', 'level'), ('园区', 'park'), ('专业', 'specialty')])
  # 周报专属：老化档位只到 7 天，KPI 文案与数据说明都要与月报区分（月报输出保持原样）
  if (window or {}).get('period') == 'weekly':
    out['kpi_labels'] = {'aged_30': aging_block['over_label']}
    out['notes_extra'] = [
      '周报窗口只有 7 天，存量老化只覆盖本期发生且期末未关闭的问题；'
      '「超 3 天未关闭」与月报「超 30 天未关闭」口径不同，不能跨周期直接比较。']
  return out


# ==================== 绘图（9 张，全确定性；图表上不加数据标签） ====================

def draw_charts(df_cur: pd.DataFrame, df_prev: pd.DataFrame, stats: dict, plt, rz_save,
                rz_heat=(), rz_level=()) -> None:
  """画 9 张图到 charts/（数据全部取自 stats，绘图不重算口径）。"""
  if stats.get('charts_suppressed'):
    return
  colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
  _CUR, _PREV = colors[0], colors[1]

  _draw_scorecard(stats.get('scorecard'), '00_scorecard', plt, rz_save)

  # 图 1 日趋势
  fig, ax = plt.subplots()
  ax.plot(range(len(stats['trend']['cur'])), stats['trend']['cur'], marker='o',
          label='本期', color=_CUR)
  ax.plot(range(len(stats['trend']['prev'])), stats['trend']['prev'], marker='o',
          linestyle='--', label='上一期', color=_PREV)
  _date_axis(ax, stats['trend']['labels'])
  ax.set_xlabel('日期'), ax.set_ylabel('问题单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '01_trend', plt, rz_save)

  # 图 1b 月内周节奏（工作日 vs 周末 日均）：仅月报有数据，周报直接跳过
  if stats.get('rhythm'):
    _draw_rhythm(stats['rhythm'], '01b_month_rhythm', plt, rz_save)

  # 图 2 级别 × 当前阶段（横向堆叠，级别在 y 轴）
  ls = stats['level_stage']
  if ls['levels'] and ls['stages']:
    fig, ax = plt.subplots(figsize=(9.0, max(2.6, 0.5 * len(ls['levels']) + 1.4)))
    lefts = [0] * len(ls['levels'])
    for j, stage in enumerate(ls['stages']):
      vals = [row[j] if j < len(row) else 0 for row in ls['matrix']]
      ax.barh(ls['levels'], vals, left=lefts, height=_bar_width(len(ls['levels'])),
              color=colors[j % len(colors)], label=stage, edgecolor='white',
              linewidth=0.8)
      lefts = [l + v for l, v in zip(lefts, vals)]
    ax.invert_yaxis()
    ax.set_xlabel('问题单量')
    _int_ticks(ax, 'x')
    # 图矮时 -0.16 会压住 x 轴标签（2026-10-18 实测：图例与 0/1 刻度重叠）
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.28),
              ncol=min(4, len(ls['stages'])), frameon=False)
    _emit(fig, '02_level_stage', plt, rz_save)

  # 图 3 未关闭问题滞留时长分布
  ag = stats['aging']
  fig, ax = plt.subplots()
  ax.bar(ag['bins'], ag['counts'], width=_bar_width(len(ag['bins'])), color=_CUR)
  ax.set_xlabel('未关闭滞留时长'), ax.set_ylabel('问题单量')
  ax.set_xticks(range(len(ag['bins'])))
  ax.set_xticklabels(ag['bins'], rotation=30, ha='right')
  _int_ticks(ax)
  _emit(fig, '03_aging_hist', plt, rz_save)

  # 图 4 各园区未关闭问题滞留结构（横向堆叠）
  da = stats['dc_aging']
  if da['dcs'] and da['bins']:
    fig, ax = plt.subplots(figsize=(9.0, max(2.6, 0.5 * len(da['dcs']) + 1.4)))
    order = sorted(range(len(da['dcs'])), key=lambda i: -sum(da['matrix'][i]))
    names = [da['dcs'][i] for i in order]
    lefts = [0] * len(names)
    for j, lab in enumerate(da['bins']):
      vals = [da['matrix'][i][j] for i in order]
      ax.barh(names, vals, left=lefts, height=_bar_width(len(names)),
              color=rz_heat[j % len(rz_heat)] if rz_heat else colors[j % len(colors)],
              label=lab, edgecolor='white', linewidth=0.8)
      lefts = [l + v for l, v in zip(lefts, vals)]
    ax.invert_yaxis()
    ax.set_xlabel('未关闭问题单量')
    _int_ticks(ax, 'x')
    # 档位图例排一行（周报 4 档 / 月报 6 档）；`_legend_below` 的 -0.16 在图矮时会压住
    # x 轴标题，这里再往下放
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.30),
              ncol=min(6, len(da['bins'])), frameon=False)
    _emit(fig, '04_dc_aging', plt, rz_save)

  # 图 5 各园区问题单量（本期 vs 上一期）
  if stats['park']:
    fig, ax = plt.subplots()
    _dumbbell(ax, [p['name'] for p in stats['park']],
              [p['count'] for p in stats['park']],
              [p['prev'] for p in stats['park']], colors)
    _emit(fig, '05_park', plt, rz_save)

  # 图 6 各专业问题单量（横向）
  sp = stats['specialty']
  if sp:
    sp_sorted = sorted(sp, key=lambda r: r['count'])
    fig, ax = plt.subplots(figsize=(8.4, max(2.4, 0.45 * len(sp_sorted) + 1.2)))
    ax.barh([r['label'] for r in sp_sorted], [r['count'] for r in sp_sorted],
            height=_bar_width(len(sp_sorted)), color=_CUR)
    ax.set_xlabel('问题单量')
    _int_ticks(ax, 'x')
    _emit(fig, '06_specialty', plt, rz_save)

  # 图 7 涉及设备/系统 Top10（横向）
  ob = stats['problem_objects']['rows']
  if ob:
    ob_sorted = sorted(ob, key=lambda r: r['count'])
    fig, ax = plt.subplots(figsize=(9.4, max(2.6, 0.42 * len(ob_sorted) + 1.2)))
    ax.barh([r['name'][:24] for r in ob_sorted], [r['count'] for r in ob_sorted],
            height=_bar_width(len(ob_sorted)), color=_CUR)
    ax.set_xlabel('涉及问题单量')
    _int_ticks(ax, 'x')
    _emit(fig, '07_equipment_top', plt, rz_save)

  # 图 8 录入滞后分布
  el = stats['entry_lag']
  fig, ax = plt.subplots()
  ax.bar(el['bins'], el['counts'], width=_bar_width(len(el['bins'])), color=_CUR)
  ax.set_xlabel('录入滞后（发生 → 建单）'), ax.set_ylabel('问题单量')
  ax.set_xticks(range(len(el['bins'])))
  ax.set_xticklabels(el['bins'], rotation=30, ha='right')
  _int_ticks(ax)
  _emit(fig, '08_entry_lag_hist', plt, rz_save)

  # 图 9 重复问题 Top10（横向）
  rp = stats['repeat'][:10]
  if rp:
    rp_sorted = sorted(rp, key=lambda r: r['count'])
    fig, ax = plt.subplots(figsize=(9.4, max(2.6, 0.42 * len(rp_sorted) + 1.2)))
    ax.barh([r['name'][:24] for r in rp_sorted], [r['count'] for r in rp_sorted],
            height=_bar_width(len(rp_sorted)), color=_CUR)
    ax.set_xlabel('重复发生单量')
    _int_ticks(ax, 'x')
    _emit(fig, '09_repeat_top', plt, rz_save)

  # 图 10 各园区 × 专业构成（横向堆叠条；单园区或单专业时不出）
  ps = stats.get('park_specialty') or {}
  if ps.get('parks') and ps.get('specialties'):
    fig, ax = plt.subplots(figsize=(9.0, max(2.6, 0.5 * len(ps['parks']) + 1.4)))
    _stacked_barh(ax, ps['parks'], ps['specialties'], ps['matrix'], colors)
    _emit(fig, '10_park_specialty', plt, rz_save)


def main(plt, rz_save, rz_heat=(), rz_level=()) -> None:
  """沙箱入口：读 cwd 下的 data.csv / prev.csv → 写 stats.json + charts/。"""
  cur = pd.read_csv('data.csv')
  prev_path = Path('prev.csv')
  prev = pd.read_csv(prev_path) if prev_path.is_file() else pd.DataFrame()
  window_path = Path('window.json')
  window = json.loads(window_path.read_text(encoding='utf-8')) if window_path.is_file() else {}
  stats = compute_stats(cur, prev, window)
  stats_for_json = {k: v for k, v in stats.items() if not k.startswith('_')}
  Path('stats.json').write_text(json.dumps(stats_for_json, ensure_ascii=False, indent=2),
                                encoding='utf-8')
  cur_scope, _, _, _ = _scope(cur, window, 'cur')
  prev_scope, _, _, _ = _scope(prev, window, 'prev')
  draw_charts(cur_scope, prev_scope, stats, plt, rz_save, rz_heat, rz_level)
  print(json.dumps({'rows_cur': stats['total'], 'rows_prev': stats['prev_total'],
                    'kept': stats['scope_filter']['kept'],
                    'charts': len(list(Path('charts').glob('*.png')))}, ensure_ascii=False))

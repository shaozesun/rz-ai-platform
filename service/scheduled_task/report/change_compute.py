"""变更工单报告的固定统计与绘图 —— **全确定性，0 次 LLM**。

与 `event_compute` / `breakdown_compute` 同契约（`compute_stats` / `draw_charts` / `main`），
但维度**刻意不同构**：变更单的价值在**变更管理的过程质量**，不是"哪类问题最多"。

实测口径（2026-10-16 用真实数据核对）：

- 列表接口的 `start_time/end_time` 是**计划实施窗口的交集过滤**
  （`scheduledStart <= end AND scheduledEnd >= start`），不是创建时间。一周查到 140 条，
  其中 35 条计划开始早于窗口、部分计划结束到 2027 年 —— 直接按返回值统计会**跨期重复计数**。
  本模块在统计前**本地按「计划实施开始时间落在统计期」归属**（`_scope`），一张单只归一期。
- `modifyTypeV2`（1 计划/2 非计划/3 紧急）可能为空，老数据只有 `modifyType` 的中文值。
- `stage`/`status` 是**当前快照**，不是流转历史；`checkBeginApprove` 是审批、
  `checkEndApprove` 是验收（同一列结构：`{'approveTime': ms, 'timeOut': bool}`）。
- `executeEndTime < executeStartTime` 的脏数据实测存在 → 不算有效实施，计入 `invalid_exec_n`。
"""

import json
from pathlib import Path

import pandas as pd

# 共用纯助手：本进程 import 走这条；沙箱里源码已拼接注入（import 失败即忽略）
try:
  from service.scheduled_task.report.common import (
    CHART_MIN_ROWS, _add_compare, _all_ticket_nos, _bar_width, _build_scorecard,
    _counts_by_label,
    _date_axis, _day_str, _delta_text, _dumbbell, _donut, _dots_rate,
    _draw_rhythm, _draw_scorecard, _emit, _human_minutes, _int_ticks, _legend_below,
    _level_palette,
    _matrix_counts, _month_rhythm, _mom_attribution, _mom_top,
    _park_level_extras, _parse_accept, _pct, _pct_text, _rate_score, _scale_score,
    _stacked_barh, _timeout_tiers, _top_card, _window_trend,
  )
except ImportError:      # 沙箱：名字已随 common 源码注入
  pass

SPECIALTY_LABEL = {1: '供配电', 2: '制冷', 4: '智能化', 8: '其他'}
LEVEL_LABEL = {1: '低风险', 2: '中风险', 3: '高风险'}
_LEVEL_CODE = {v: k for k, v in LEVEL_LABEL.items()}
CHANGE_TYPE_LABEL = {1: '计划型变更', 2: '非计划型变更', 3: '紧急变更'}
STAGE_LABEL = {-1: '驳回', 1: '审批期', 2: '实施期', 3: '验收期', 5: '关闭期', 7: '等待期'}
STATUS_LABEL = {0: '无', 1: '通过', 2: '驳回', 3: '审批中', 4: '实施中',
                5: '撤回', 6: '退回', 7: '验收中', 8: '审批通过', 9: '延期中'}
_UNKNOWN = '未标注'

# 审批/验收时长分箱（分钟，固定边界 → 跨期可比）
_APPROVE_BINS = [0, 60, 240, 1440, 4320, 10 ** 9]
_APPROVE_BIN_LABELS = ['<1时', '1-4时', '4-24时', '1-3天', '>3天']

# 完成偏差 7 档（相对计划结束；负＝提前，0＝按时）
_DELAY_BANDS = (
  ('提前 ≥1天', lambda d: d <= -1440),
  ('提前 1-24时', lambda d: -1440 < d < -60),
  ('提前 <1时', lambda d: -60 <= d < 0),
  ('按时', lambda d: d == 0),
  ('延迟 <1时', lambda d: 0 < d <= 60),
  ('延迟 1-24时', lambda d: 60 < d <= 1440),
  ('延迟 >1天', lambda d: d > 1440),
)

# 综合评分维度（0~100）：变更单关注"量、按计划、审批快、收得掉、少紧急"
_SCORE_DIMS = (
  ('规模控制', '单量较上一期增长按增幅扣分，持平或下降满分（无上期基数不扣分）。'),
  ('计划达成', '实施在计划结束时间前完成的比例（按时完成率）。'),
  ('审批时效', '平台未标记审批超时的变更单占比。'),
  ('闭环质量', '未积压（阶段非关闭期）比例与非驳回率加权（60% / 40%）。'),
  ('紧急变更管控', '非紧急变更的占比。'),
)


def _score_inputs(df: pd.DataFrame, window: dict) -> dict:
  """评分所需的单期指标（本期/上一期同一套算法）。"""
  total = int(len(df)) if df is not None else 0
  timing = _timing(df)
  types = _type_series(df)
  schedule = _schedule_block(df, timing, types)
  approve = _approve_block(df, 'checkBeginApprove')
  open_block = _open_block(df, window)
  reject_mask = (_num(df, 'status') == 2) | (_num(df, 'stage') == -1)
  reject_n = int(reject_mask.sum()) if total else 0
  em_n = int((types == '紧急变更').sum()) if total else 0
  return {
    'total': total,
    'on_time_rate': schedule['on_time_rate'],
    'approve_timeout_rate': approve['timeout_rate'],
    'open_rate': open_block['rate'],
    'reject_rate': _pct(reject_n, total),
    'emergency_rate': _pct(em_n, total),
  }


def _scores(metrics: dict, prev_total: int) -> dict:
  return {
    '规模控制': _scale_score(metrics['total'], prev_total),
    '计划达成': (float(metrics['on_time_rate'])
                 if metrics['on_time_rate'] is not None else 0.0),
    '审批时效': _rate_score(metrics['approve_timeout_rate']),
    '闭环质量': (0.6 * (100 - (metrics['open_rate'] or 0))
                 + 0.4 * (100 - metrics['reject_rate'])),
    '紧急变更管控': 100.0 - metrics['emergency_rate'],
  }


def _num(df: pd.DataFrame, column: str) -> pd.Series:
  """取数值列（缺失列返回全 NaN，非法值转 NaN）。"""
  if column not in df.columns:
    return pd.Series([float('nan')] * len(df), index=df.index, dtype='float64')
  return pd.to_numeric(df[column], errors='coerce')


def _scope(df: pd.DataFrame, window: dict, which: str = 'cur') -> tuple[pd.DataFrame, int, int]:
  """按「计划实施开始时间」把记录归属到统计期；返回 (保留的帧, 缺计划开始数, 窗口外数)。

  `which='cur'` 用本期窗口、`'prev'` 用上一期窗口。日期比较用**北京时间日期串**，
  与 `_day_str` 同一口径，不依赖宿主机时区。
  """
  if df is None or not len(df):
    return (df if df is not None else pd.DataFrame()), 0, 0
  if 'scheduledStartTime' not in df.columns:
    return df.iloc[0:0].copy(), int(len(df)), 0
  win = (window or {}).get(which) or []
  if len(win) < 2:
    return df.copy(), 0, 0
  start, end = str(win[0])[:10], str(win[1])[:10]
  ms = _num(df, 'scheduledStartTime')
  has = ms.notna() & (ms > 0)
  days = _day_str(ms.where(has))
  mask = has & days.notna() & (days >= start) & (days <= end)
  return df[mask].copy(), int((~has).sum()), int((has & ~mask).sum())


def _day_of(series: pd.Series) -> pd.Series:
  return _day_str(pd.to_numeric(series, errors='coerce'))


def _window_days(window: dict, which: str) -> list[str]:
  """统计期的**完整日期轴**（含无单的日期）。

  趋势图不能只画"有单的日期"：本期只有 5 天有单、上一期 7 天都有单时，两条线会按各自
  的日期集合错位，事实化后「日期 + 上一期数值」也会错配（周末零单不是"没有这一天"）。
  """
  win = (window or {}).get(which) or []
  if len(win) < 2:
    return []
  start, end = str(win[0])[:10], str(win[1])[:10]
  return [d.strftime('%Y-%m-%d') for d in pd.date_range(start, end, freq='D')]


def _type_series(df: pd.DataFrame) -> pd.Series:
  """变更类型：`modifyTypeV2` 优先，空则用 `modifyType` 的中文值，都没有记「未标注」。"""
  v2 = _num(df, 'modifyTypeV2')
  legacy = (df['modifyType'].astype(str).str.strip()
            if 'modifyType' in df.columns else pd.Series([''] * len(df), index=df.index))
  out = []
  for code, text in zip(v2.tolist(), legacy.tolist()):
    label = ''
    if code == code:                       # not NaN
      label = CHANGE_TYPE_LABEL.get(int(code), '')
    if not label and text and text not in ('nan', 'None'):
      label = text
    out.append(label or _UNKNOWN)
  return pd.Series(out, index=df.index)


def _dist(values: list) -> dict:
  """时长（分钟）分布：P50/P90/P95/均值/最长 + 可读表述。"""
  vals = [float(v) for v in values if v is not None and v >= 0]
  if not vals:
    return {'p50': None, 'p90': None, 'p95': None, 'mean': None, 'max': None,
            'readable': {}}
  s = pd.Series(vals)
  q50, q90, q95 = (float(s.quantile(q)) for q in (0.5, 0.9, 0.95))
  return {'p50': round(q50, 1), 'p90': round(q90, 1), 'p95': round(q95, 1),
          'mean': round(float(s.mean()), 1), 'max': round(float(s.max()), 1),
          'readable': {'p50': _human_minutes(q50), 'p90': _human_minutes(q90),
                       'p95': _human_minutes(q95), 'mean': _human_minutes(float(s.mean())),
                       'max': _human_minutes(float(s.max()))}}


def _pctl(values: list, q: float):
  vals = sorted(float(v) for v in values if v is not None)
  if not vals:
    return None
  return round(float(pd.Series(vals).quantile(q)), 1)


def _aligned(values: list, df: pd.DataFrame) -> pd.Series:
  """`_parse_accept` 的分钟列表按行对齐；列缺失时它是空列表 → 补同长度的 None。"""
  if values and len(values) == len(df):
    return pd.Series(values, index=df.index, dtype='object')
  return pd.Series([None] * len(df), index=df.index, dtype='object')


def _stage_row(name: str, basis: str, values) -> dict:
  """一个环节的耗时行（分钟 → P50/P90 + 可读串，可读串由平台算好，模型照抄）。"""
  vals = [float(v) for v in values if v is not None and v >= 0]
  d = _dist(vals)
  return {'环节': name, '口径': basis, '可统计': len(vals),
          'p50': d['p50'], 'p90': d['p90'],
          'p50可读': d['readable'].get('p50'), 'p90可读': d['readable'].get('p90')}


def _flow_durations(df: pd.DataFrame, timing: dict) -> tuple[list[dict], list[dict]]:
  """各环节耗时（审批/实施/验收/全流程）+ 分等级时效 —— **单园区专属**。

  **关闭期耗时算不出**：列表只有计划/实施/审批/验收字段，没有关闭时间戳，所以「全流程」
  只能取「创建→实施完成」，三段之和不等于它（中间的空闲与等待期未计入）。
  """
  appr_min, _ = _parse_accept(df, 'checkBeginApprove')
  acc_min, _ = _parse_accept(df, 'checkEndApprove')
  rows = [
    _stage_row('审批', '审批记录时长（checkBeginApprove）', appr_min),
    _stage_row('实施', '实际开始→实际结束', timing['actual'].values()),
    _stage_row('验收', '验收记录时长（checkEndApprove）', acc_min),
  ]
  create_ms, end_ms = _num(df, 'createTime'), _num(df, 'executeEndTime')
  full, inverted = [], 0
  for i in timing['actual']:
    c, e = create_ms.at[i], end_ms.at[i]
    if pd.isna(c) or c <= 0 or pd.isna(e) or e <= 0:
      continue
    if e < c:
      inverted += 1
      continue
    full.append(float((e - c) / 60000.0))
  # 口径列只写最必要的：「无关闭时间戳 / 三段之和不等于它」已在表下的 note 里，不重复
  basis = '创建→实施完成'
  if inverted:
    basis += f'（倒挂 {inverted} 单已剔除）'
  rows.append(_stage_row('全流程', basis, full))
  return rows, _by_level_duration(df, timing, _aligned(appr_min, df), _aligned(acc_min, df))


def _by_level_duration(df: pd.DataFrame, timing: dict, appr: pd.Series,
                       acc: pd.Series) -> list[dict]:
  """按变更等级（`ticketType`）的审批/实施/验收 P50 与按时率；样本 0 的等级不出行。"""
  lv = _num(df, 'ticketType')
  actual = pd.Series(timing['actual']).reindex(df.index)
  delay = pd.Series(timing['delay']).reindex(df.index)
  rows = []
  for code in sorted(LEVEL_LABEL):
    idx = df.index[lv == code]
    n = int(len(idx))
    if not n:
      continue
    ot = (delay.loc[idx] <= 0).where(delay.loc[idx].notna())
    ot_base = int(ot.notna().sum())
    rows.append({
      '等级': LEVEL_LABEL[code], '单数': n,
      '审批P50分钟': _pctl(pd.to_numeric(appr.loc[idx], errors='coerce').dropna().tolist(), .5),
      '实施P50分钟': _pctl(actual.loc[idx].dropna().tolist(), .5),
      '验收P50分钟': _pctl(pd.to_numeric(acc.loc[idx], errors='coerce').dropna().tolist(), .5),
      '按时率': _pct(int(ot.sum()), ot_base) if ot_base else None,
    })
  return rows


def _dept_series(df: pd.DataFrame) -> pd.Series | None:
  """部门列（`dc` 已由客户端换成中文部门名）→ 去空白，空值/字面量 nan 归 None。"""
  if df is None or 'dc' not in df.columns:
    return None
  s = df['dc'].astype(str).str.strip()
  return s.where(~s.isin(['', 'nan', 'None', 'NaN']))


def _dept_block(cur: pd.DataFrame, prev: pd.DataFrame, timing: dict,
                cur_types: pd.Series, approve_minutes: list, total: int) -> dict | None:
  """单园区报告的部门维度（全国级帧带 `companyName`，不进本函数）。

  单园区预设只有 1 个园区 → 「园区横向对比」整章退化成空；部门是园区内部唯一可比的横向
  维度（`dc` 一直在数据里，此前只用于「最慢部门」）。分位样本 <3 记 None（表里显示「—」）。
  """
  names = _dept_series(cur)
  if names is None:
    return None
  prev_names = _dept_series(prev)
  prev_counts = (prev_names.dropna().value_counts().to_dict()
                 if prev_names is not None else {})
  delay = pd.Series(timing['delay']).reindex(cur.index)
  frame = pd.DataFrame({
    'name': names,
    'em': (cur_types == '紧急变更').reindex(cur.index).fillna(False),
    'reject': ((_num(cur, 'status') == 2) | (_num(cur, 'stage') == -1)).reindex(cur.index)
              .fillna(False),
    'approve': pd.to_numeric(_aligned(approve_minutes, cur), errors='coerce'),
    'actual': pd.Series(timing['actual']).reindex(cur.index),
    'ontime': (delay <= 0).where(delay.notna()),
  })
  frame = frame[frame['name'].notna()]
  if not len(frame):
    return None
  rows = []
  for name, cnt in frame['name'].value_counts().items():
    sub = frame[frame['name'] == name]
    n = int(cnt)
    prev_n = int(prev_counts.get(name, 0))
    em = int(sub['em'].sum())
    rej = int(sub['reject'].sum())
    ot_n, ot_base = int(sub['ontime'].sum()), int(sub['ontime'].notna().sum())
    appr = sub['approve'].dropna()
    act = sub['actual'].dropna()
    rows.append({
      'name': name, 'count': n, 'prev': prev_n, 'delta': n - prev_n,
      'pct': _pct(n, total),
      'emergency_n': em, 'emergency_rate': _pct(em, n),
      'on_time_n': ot_n, 'on_time_base': ot_base,
      'on_time_rate': _pct(ot_n, ot_base) if ot_base else None,
      'reject_n': rej, 'reject_rate': _pct(rej, n),
      'approve_p50': _pctl(appr.tolist(), .5) if len(appr) >= 3 else None,
      'exec_p50': _pctl(act.tolist(), .5) if len(act) >= 3 else None,
    })
  # 补齐可比字段（上期占比/单量变化/百分点变化）：解读层不得自己相减（见 `_add_compare`）
  _add_compare(rows, int(len(prev)) if prev is not None else 0)
  return {'classes': len(rows), 'rows': rows}


def _approve_block(df: pd.DataFrame, column: str) -> dict:
  """审批/验收时长 + 平台超时标记（`checkBeginApprove` / `checkEndApprove`）。"""
  minutes, flags = _parse_accept(df, column)
  valid = [m for m in minutes if m is not None]
  base = [f for f in flags if f is not None]
  timeout_n = sum(1 for f in base if f)
  out = {**_dist(valid), 'available': bool(valid), 'valid': len(valid),
         'rows': int(len(df)), 'timeout_n': timeout_n, 'timeout_base': len(base),
         'timeout_rate': _pct(timeout_n, len(base)) if base else None}
  if valid:
    binned = pd.cut(pd.Series(valid), bins=_APPROVE_BINS, labels=_APPROVE_BIN_LABELS,
                    include_lowest=True, right=False)
    counts = binned.value_counts().reindex(_APPROVE_BIN_LABELS, fill_value=0)
    out['hist'] = {'labels': list(_APPROVE_BIN_LABELS),
                   'counts': [int(v) for v in counts.tolist()]}
  else:
    out['hist'] = {'labels': list(_APPROVE_BIN_LABELS),
                   'counts': [0] * len(_APPROVE_BIN_LABELS)}
  return out


def _slowest_dept(df: pd.DataFrame, minutes: list, top: int = 5) -> list[dict]:
  """审批时长最慢的部门（样本 ≥3，按 P90 降序）。"""
  if 'dc' not in df.columns:
    return []
  pw = pd.DataFrame({'部门': df['dc'].astype(str).values, 'minutes': minutes})
  pw = pw.dropna(subset=['minutes'])
  pw = pw[~pw['部门'].isin(['', 'nan', 'None'])]
  if not len(pw):
    return []
  g = pw.groupby('部门')['minutes']
  agg = pd.DataFrame({'单数': g.count(), 'P50分钟': g.quantile(.5).round(1),
                      'P90分钟': g.quantile(.9).round(1)}).reset_index()
  agg = agg[agg['单数'] >= 3]
  agg = agg.sort_values(['P90分钟', '部门'], ascending=[False, True]).head(top)
  return [{'部门': r['部门'], '单数': int(r['单数']), 'P50分钟': float(r['P50分钟']),
           'P90分钟': float(r['P90分钟'])} for _, r in agg.iterrows()]


def _stage_status_rows(df: pd.DataFrame) -> list[dict]:
  """阶段 × 状态联合计数（当前快照）。

  不能按标签静态配对 —— 状态 9「延期中」与阶段是正交的（审批期的单也可能延期中），
  只能按 (stage, status) 联合 groupby。按单量降序、同量按码值排序，保证跨期稳定。
  """
  if 'stage' not in df.columns or 'status' not in df.columns or not len(df):
    return []

  def _code(v) -> int:
    return int(v) if pd.notna(v) else 9999

  frame = pd.DataFrame({'stage': _num(df, 'stage'), 'status': _num(df, 'status')})
  frame = frame.dropna(how='all')
  if not len(frame):
    return []
  total = len(frame)
  rows = []
  for (stage, status), n in frame.value_counts(dropna=False).items():
    rows.append({
      '阶段': STAGE_LABEL.get(_code(stage), '未知'),
      '状态': STATUS_LABEL.get(_code(status), '未知'),
      '单数': int(n), '占比': _pct(int(n), total),
      '_s': _code(stage), '_u': _code(status),
    })
  rows.sort(key=lambda r: (-r['单数'], r['_s'], r['_u']))
  for r in rows:
    del r['_s'], r['_u']
  return rows


def _timing(df: pd.DataFrame) -> dict:
  """计划/实际时间对齐结果（逐行索引 → 分钟），一次算好给统计与代表单共用。"""
  ss, se = _num(df, 'scheduledStartTime'), _num(df, 'scheduledEndTime')
  es, ee = _num(df, 'executeStartTime'), _num(df, 'executeEndTime')
  planned, actual, start_delay, delay = {}, {}, {}, {}
  invalid_exec_n = 0
  for i in df.index:
    s_ok = bool(pd.notna(ss.at[i]) and pd.notna(se.at[i]) and ss.at[i] > 0 and se.at[i] > 0
                and se.at[i] >= ss.at[i])
    r_ok = bool(pd.notna(es.at[i]) and pd.notna(ee.at[i]) and es.at[i] > 0 and ee.at[i] > 0)
    e_ok = r_ok and ee.at[i] >= es.at[i]
    if r_ok and not e_ok:
      invalid_exec_n += 1
    if s_ok:
      planned[i] = float((se.at[i] - ss.at[i]) / 60000.0)
    if e_ok:
      actual[i] = float((ee.at[i] - es.at[i]) / 60000.0)
    if s_ok and es.at[i] > 0 and pd.notna(es.at[i]):
      start_delay[i] = float((es.at[i] - ss.at[i]) / 60000.0)
    if s_ok and e_ok:
      delay[i] = float((ee.at[i] - se.at[i]) / 60000.0)
  return {'planned': planned, 'actual': actual, 'start_delay': start_delay,
          'delay': delay, 'sched_valid': len(planned), 'exec_valid': len(actual),
          'invalid_exec_n': invalid_exec_n}


def _schedule_block(df: pd.DataFrame, timing: dict, cur_types: pd.Series) -> dict:
  """计划 vs 实际：时长、开始偏差、按时率、偏差分档、按类型/园区。"""
  delays = list(timing['delay'].values())
  base = len(delays)
  on_time_n = sum(1 for d in delays if d <= 0)
  late_n = sum(1 for d in delays if d > 0)
  early_n = sum(1 for d in delays if d < 0)
  sd = list(timing['start_delay'].values())
  sd_late_n = sum(1 for d in sd if d > 0)

  # 计划 vs 实际散点：同一单两个口径都有效且 >0（线性绘制在 draw_charts）
  scatter = []
  for i in sorted(set(timing['planned']) & set(timing['actual'])):
    p, a = timing['planned'][i], timing['actual'][i]
    if p > 0 and a > 0:
      scatter.append([round(p, 1), round(a, 1)])

  by_type = []
  for label in CHANGE_TYPE_LABEL.values():
    idxs = [i for i in timing['delay'] if cur_types.at[i] == label]
    n = len(idxs)
    ot = sum(1 for i in idxs if timing['delay'][i] <= 0)
    by_type.append({'类型': label, '参与统计': n, '按时单数': ot,
                    '按时率': _pct(ot, n) if n else None})

  by_park = []
  if 'companyName' in df.columns:
    for name, _ in df['companyName'].value_counts().items():
      idxs = [i for i in timing['delay'] if df.at[i, 'companyName'] == name]
      n = len(idxs)
      if not n:
        continue
      ot = sum(1 for i in idxs if timing['delay'][i] <= 0)
      by_park.append({'园区': name, '参与统计': n, '按时单数': ot, '按时率': _pct(ot, n)})

  worst = []
  def _no(i) -> str:
    return str(df.at[i, 'ticketNo']).strip() if 'ticketNo' in df.columns else ''
  for i, d in sorted(timing['delay'].items(), key=lambda kv: (-kv[1], _no(kv[0]))):
    if d <= 0:
      break
    worst.append({'ticketNo': _no(i),
                  '园区': str(df.at[i, 'companyName']) if 'companyName' in df.columns else '',
                  '项目名称': str(df.at[i, 'projectName'])
                  if 'projectName' in df.columns else '',
                  '延迟分钟': round(d, 1), '延迟可读': _human_minutes(d)})
    if len(worst) >= 5:
      break

  return {
    'sched_valid': timing['sched_valid'], 'exec_valid': timing['exec_valid'],
    'invalid_exec_n': timing['invalid_exec_n'],
    'missing_exec_n': int(len(df)) - timing['exec_valid'] - timing['invalid_exec_n'],
    'planned': _dist(list(timing['planned'].values())),
    'actual': _dist(list(timing['actual'].values())),
    'start_delay': {'p50': _pctl(sd, .5), 'p90': _pctl(sd, .9),
                    'late_n': sd_late_n, 'late_rate': _pct(sd_late_n, len(sd)) if sd else None},
    'on_time_n': on_time_n, 'on_time_base': base,
    'on_time_rate': _pct(on_time_n, base) if base else None,
    'late_n': late_n, 'late_rate': _pct(late_n, base) if base else None,
    'early_n': early_n,
    'delay_hist': [{'档位': label, '单数': sum(1 for d in delays if fn(d))}
                   for label, fn in _DELAY_BANDS],
    'scatter': scatter,
    'by_type': by_type, 'by_park': by_park, 'worst': worst,
  }


def _open_block(df: pd.DataFrame, window: dict) -> dict:
  """当前未关闭（阶段≠关闭期）与计划结束偏差（统计期末 − 计划结束，正=已超期、负=未到期）。

  「未关闭」不等于「积压」：实测 29 单未关闭里大部分计划结束还在统计期之后（负值），
  它们只是尚未实施，不该被读成超期。故单独给出 `overdue_n`（真的过点了的单数）。
  """
  if 'stage' not in df.columns or not len(df):
    return {'n': 0, 'rate': None, 'overdue_n': 0, 'rows': []}
  open_df = df[_num(df, 'stage') != 5]
  win = (window or {}).get('cur') or []
  ref = pd.Timestamp(str(win[1])[:10]) if len(win) > 1 else pd.Timestamp.today().normalize()
  se = _num(df, 'scheduledEndTime')
  rows = []
  for i, row in open_df.iterrows():
    age = None
    ms = se.at[i]
    if pd.notna(ms) and ms > 0:
      end_day = (pd.to_datetime(int(ms), unit='ms', utc=True)
                 .tz_convert('Asia/Shanghai').normalize().tz_localize(None))
      age = round((ref - end_day).total_seconds() / 86400, 1)
    rows.append({'ticketNo': str(row.get('ticketNo', '')).strip(),
                 '园区': str(row.get('companyName', '')),
                 '项目名称': str(row.get('projectName', '')),
                 '阶段': STAGE_LABEL.get(int(row['stage']), str(row['stage']))
                 if pd.notna(row['stage']) else '—',
                 '超期天数': age})
  rows.sort(key=lambda r: (-(r['超期天数'] if r['超期天数'] is not None else -10 ** 9),
                           r['ticketNo']))
  n = int(len(open_df))
  overdue_n = sum(1 for r in rows if r['超期天数'] is not None and r['超期天数'] > 0)
  return {'n': n, 'rate': _pct(n, len(df)),
          'overdue_n': overdue_n, 'rows': rows[:10]}


def _change_type_block(df: pd.DataFrame, cur_types: pd.Series,
                       prev_types: pd.Series, total: int, prev_total: int) -> dict:
  """变更类型/紧急度：本期与上期、按等级构成、各园区紧急占比（全部为具名行）。"""
  prev_counts = prev_types.value_counts().to_dict()
  rows = []
  for label in list(CHANGE_TYPE_LABEL.values()) + [_UNKNOWN]:
    n = int((cur_types == label).sum())
    if label == _UNKNOWN and not n:
      continue
    rows.append({'label': label, 'count': n, 'prev': int(prev_counts.get(label, 0)),
                 'pct': _pct(n, total)})
  _add_compare(rows, prev_total)

  by_level = []
  lv_col = 'ticketType'
  for label in CHANGE_TYPE_LABEL.values():
    sub = df[cur_types == label]
    row = {'类型': label, '单数': int(len(sub))}
    for lv_label in LEVEL_LABEL.values():
      code = _LEVEL_CODE[lv_label]
      row[lv_label] = int((_num(sub, lv_col) == code).sum()) if lv_col in sub.columns else 0
    row['高风险占比'] = _pct(row['高风险'], len(sub))
    by_level.append(row)

  by_park = []
  if 'companyName' in df.columns:
    for name, _ in df['companyName'].value_counts().items():
      sub = df[df['companyName'] == name]
      n = int(len(sub))
      em = int((cur_types.loc[sub.index] == '紧急变更').sum())
      by_park.append({'园区': name, '总单数': n, '紧急单数': em, '紧急占比': _pct(em, n)})

  em_n = int((cur_types == '紧急变更').sum())
  unplan_n = int((cur_types == '非计划型变更').sum())
  plan_n = int((cur_types == '计划型变更').sum())
  return {'rows': rows,
          'planned_n': plan_n, 'planned_rate': _pct(plan_n, total),
          'unplanned_n': unplan_n, 'unplanned_rate': _pct(unplan_n, total),
          'emergency_n': em_n, 'emergency_rate': _pct(em_n, total),
          'by_level': by_level, 'by_park': by_park}


def _detail_targets(df: pd.DataFrame, cur_types: pd.Series, timing: dict) -> list[str]:
  """第 10 章代表单：完成延迟最大 → 紧急且高风险 → 实际时长最长 → 编号补齐，去重取 5。"""
  nos: list[str] = []

  def _no(i) -> str:
    return str(df.at[i, 'ticketNo']).strip() if 'ticketNo' in df.columns else ''

  def add(i) -> None:
    no = _no(i)
    if no and no not in nos:
      nos.append(no)

  for i, _ in sorted(timing['delay'].items(), key=lambda kv: (-kv[1], _no(kv[0]))):
    if len(nos) >= 2:
      break
    add(i)

  high = _num(df, 'ticketType')
  em_high = [i for i in df.index
             if cur_types.at[i] == '紧急变更' and pd.notna(high.at[i]) and int(high.at[i]) == 3]
  em_high.sort(key=lambda i: (-(timing['actual'].get(i) or 0), _no(i)))
  for i in em_high:
    if len(nos) >= 4:
      break
    add(i)

  longest = sorted(timing['actual'].items(), key=lambda kv: (-kv[1], _no(kv[0])))
  for i, _ in longest:
    if len(nos) >= 5:
      break
    add(i)

  for i in sorted(df.index, key=lambda i: _no(i)):
    if len(nos) >= 5:
      break
    add(i)
  return nos[:5]


def compute_stats(df_cur: pd.DataFrame, df_prev: pd.DataFrame, window: dict) -> dict:
  """本期/上一期变更单 → 报告需要的全部统计（确定性，可单测）。"""
  raw_cur = int(len(df_cur)) if df_cur is not None else 0
  cur, missing_start, out_of_window = _scope(df_cur, window, 'cur')
  prev, _, _ = _scope(df_prev, window, 'prev')
  total, prev_total = int(len(cur)), int(len(prev))

  cur_days = _day_of(cur['scheduledStartTime']) if 'scheduledStartTime' in cur.columns \
    else pd.Series([], dtype=object)
  mom = {'prev_total': prev_total, 'delta': total - prev_total,
         'pct': _pct(total - prev_total, prev_total) if prev_total else None}
  # 趋势按**窗口完整日期轴**对齐（见 `common._window_trend`）；上一期按位置对位
  if len(cur_days):
    trend = _window_trend(cur, prev, 'scheduledStartTime', window)
  else:
    trend = {'labels': [], 'cur': [], 'prev': [], 'prev_days': [],
             'peak_day': '', 'peak_n': 0, 'low_day': '', 'low_n': 0}
  if not len(prev):                       # 无上一期：契约上 `prev` 为空列表（不是补 0 的等长轴）
    trend['prev'], trend['prev_days'] = [], []
  day_count = len(trend['labels'])

  level = (_counts_by_label(_num(cur, 'ticketType'), LEVEL_LABEL)
           if 'ticketType' in cur.columns else [])
  specialty = (_counts_by_label(cur['specialty'], SPECIALTY_LABEL)
               if 'specialty' in cur.columns else [])
  prev_specialty = {}
  if 'specialty' in prev.columns:
    prev_specialty = {SPECIALTY_LABEL.get(int(k), str(k)): int(v)
                      for k, v in prev['specialty'].value_counts().items()}
  for row in specialty:
    row['prev'] = prev_specialty.get(row['label'])
  # 上一期占比的分母＝上一期非空计数（与 `_counts_by_label` 的本期口径一致）
  _add_compare(specialty, int(prev['specialty'].notna().sum())
               if 'specialty' in prev else 0)

  prev_park = {}
  if 'companyName' in prev.columns:
    prev_park = {k: int(v) for k, v in prev['companyName'].value_counts().items()}
  park = []
  if 'companyName' in cur.columns:
    for name, cnt in cur['companyName'].value_counts().items():
      n = int(cnt)
      park.append({'name': name, 'count': n, 'pct': _pct(n, total),
                   'prev': prev_park.get(name, 0)})
  _add_compare(park, prev_total)

  # 园区 × 变更等级（横向堆叠条的数据源；对齐型结构 → 由 facts 摊平后喂模型）
  park_level = {'parks': [p['name'] for p in park], 'levels': [],
                'matrix': [[] for _ in park]}
  if park and 'ticketType' in cur.columns:
    for code in sorted(LEVEL_LABEL):
      label = LEVEL_LABEL[code]
      col = [int((_num(cur[cur['companyName'] == p['name']], 'ticketType') == code).sum())
             for p in park]
      if sum(col) > 0:
        park_level['levels'].append(label)
        for i, v in enumerate(col):
          park_level['matrix'][i].append(v)

  # 园区 × 专业（横向堆叠条的数据源）：单园区或无专业列时不产出（渲染层不出图）
  parks = [p['name'] for p in park]
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

  cur_types = _type_series(cur)
  prev_types = _type_series(prev)
  change_type = _change_type_block(cur, cur_types, prev_types, total, prev_total)

  timing = _timing(cur)
  schedule = _schedule_block(cur, timing, cur_types)

  approve = _approve_block(cur, 'checkBeginApprove')
  accept = _approve_block(cur, 'checkEndApprove')
  approve_minutes, _ = _parse_accept(cur, 'checkBeginApprove')
  open_block = _open_block(cur, window)

  reject_mask = (_num(cur, 'status') == 2) | (_num(cur, 'stage') == -1)
  reject_n = int(reject_mask.sum()) if len(cur) else 0
  exceptions = [
    {'异常': '驳回', '单数': reject_n},
    {'异常': '撤回', '单数': int((_num(cur, 'status') == 5).sum()) if len(cur) else 0},
    {'异常': '退回', '单数': int((_num(cur, 'status') == 6).sum()) if len(cur) else 0},
    {'异常': '延期中', '单数': int((_num(cur, 'status') == 9).sum()) if len(cur) else 0},
  ]
  flow = {
    'stage': _counts_by_label(_num(cur, 'stage'), STAGE_LABEL) if 'stage' in cur.columns else [],
    'status': (_counts_by_label(_num(cur, 'status'), STATUS_LABEL)
               if 'status' in cur.columns else []),
    # 阶段/状态两张边际表数字一一对应（是同一批单的两个视角）→ 合并成一张联合表下发
    'stage_status': _stage_status_rows(cur),
    'approve': approve, 'accept': accept,
    'exceptions': exceptions, 'open': open_block,
    'slowest_dept': _slowest_dept(cur, approve_minutes),
  }

  # ---- 单园区专属：部门维度 + 各环节耗时（全国级帧带 companyName，一律不写这些键）----
  dept = None
  if 'companyName' not in cur.columns:
    dept = _dept_block(cur, prev, timing, cur_types, approve_minutes, total)
    flow['durations'], flow['by_level_duration'] = _flow_durations(cur, timing)
    flow['exception_rates'] = [{'异常': r['异常'], '单数': r['单数'],
                                '占比': _pct(r['单数'], total)}
                               for r in exceptions]

  quality = {
    'approve_timeout_rate': approve['timeout_rate'],
    'approve_timeout_n': approve['timeout_n'],
    'accept_timeout_rate': accept['timeout_rate'],
    'accept_timeout_n': accept['timeout_n'],
    'reject_n': reject_n, 'reject_rate': _pct(reject_n, total),
    'open_n': open_block['n'], 'open_rate': open_block['rate'],
  }

  # ---- 综合评分（雷达图）：本期与上一期同口径；上一期以「上期窗口末」为参照日 ----
  prev_window = {'cur': (window or {}).get('prev'),
                 'period': (window or {}).get('period')}
  cur_m = _score_inputs(cur, window)
  prev_m = _score_inputs(prev, prev_window)
  cur_scores = _scores(cur_m, prev_m['total'])
  prev_scores = _scores(prev_m, cur_m['total'])
  scorecard = _build_scorecard([
    (dim, meaning, cur_scores[dim], prev_scores[dim]) for dim, meaning in _SCORE_DIMS
  ])

  longest_actual = sorted(timing['actual'].items(), key=lambda kv: (-kv[1], str(kv[0])))[:3]
  # 单园区查询不带 companyName（只有全国聚合才补）→ 环比归因退回到「部门 × 专业」
  mom_key = '园区环比变动最大' if 'companyName' in cur.columns else '部门环比变动最大'
  extras = {
    '各园区级别构成': _park_level_extras(cur, _num(cur, 'ticketType'), LEVEL_LABEL)
    if 'ticketType' in cur.columns else [],
    mom_key: _mom_attribution(cur, prev, SPECIALTY_LABEL),
    '审批超时分档': _timeout_tiers(cur, approve_minutes, SPECIALTY_LABEL),
    '最慢部门': flow['slowest_dept'],
    '紧急变更集中园区': change_type['by_park'],
    '未关闭积压': open_block['rows'],
    '最长变更单': [{'ticketNo': str(cur.at[i, 'ticketNo']).strip()
                    if 'ticketNo' in cur.columns else '',
                    '实际分钟': round(v, 1), '可读': _human_minutes(v)} for i, v in longest_actual],
  }

  kpi = {
    'total': {'value': str(total), 'sub': f'上一期 {prev_total} 单'},
    'daily_avg': {'value': f'{round(total / max(day_count, 1), 1)}',
                  'sub': f'覆盖 {day_count} 天'},
    'mom': {'value': (_delta_text(mom["delta"]) if prev_total else '—'),
            'sub': (_pct_text(mom['pct'], signed=True)
                    if mom['pct'] is not None else '无上期基数')},
    'top_level': _top_card(level, '最多等级', '单'),
    'emergency': {'value': (f'{change_type["emergency_rate"]}%' if total else '—'),
                  'sub': f'紧急 {change_type["emergency_n"]} 单 · '
                         f'非计划 {change_type["unplanned_n"]} 单'},
    'on_time': {'value': (f'{schedule["on_time_rate"]}%'
                          if schedule['on_time_rate'] is not None else '—'),
                'sub': f'{schedule["on_time_n"]} / {schedule["on_time_base"]} 单'},
    'approve_timeout': {'value': (f'{approve["timeout_rate"]}%'
                                  if approve['timeout_rate'] is not None else '—'),
                        'sub': f'{approve["timeout_n"]} / {approve["timeout_base"]} 单'},
    'backlog': {'value': str(open_block['n']),
                'sub': (f'占 {open_block["rate"]}%（期未仍未关闭）'
                        if open_block['rate'] is not None else '无数据')},
  }

  out = {
    'window': window, 'total': total, 'prev_total': prev_total,
    'daily_avg': round(total / max(day_count, 1), 1),
    'mom': mom, 'trend': trend, 'level': level, 'specialty': specialty,
    'park': park, 'park_level': park_level, 'park_specialty': park_specialty,
    'change_type': change_type,
    'flow': flow, 'schedule': schedule, 'quality': quality,
    'kpi': kpi, 'extras': extras, 'scorecard': scorecard,
    'scope_filter': {'raw': raw_cur, 'kept': total, 'out_of_window': out_of_window,
                     'missing_start': missing_start, 'basis': '计划实施开始时间'},
    'detail_targets': _detail_targets(cur, cur_types, timing),
    'detail_all': (_all_ticket_nos(cur)
                   if (window or {}).get('period') == 'weekly' else []),
    'charts_suppressed': total < CHART_MIN_ROWS,
  }
  if dept:
    out['dept'] = dept
  # 月报专属维度（周报**不写这两个键**，输出逐字不变）
  if (window or {}).get('period') == 'monthly':
    out['rhythm'] = _month_rhythm(trend, window, '单')
    out['mom_top'] = _mom_top(
      {'level': level, 'park': park, 'specialty': specialty,
       'change_type': change_type},
      [('等级', 'level'), ('园区', 'park'), ('专业', 'specialty'),
       ('变更类型', 'change_type')])
  return out


# ==================== 绘图（全确定性；图表上不加数据标签） ====================

# 时长刻度阶梯（小时）：1 分钟到 30 天。线性轴上取「刻度数不超 7」的最细一档，
# 标签再经 `_human_minutes` 变成「30 分钟 / 2 小时 / 7 天」这类可读串。
_DUR_TICK_STEPS = (1 / 60, 2 / 60, 5 / 60, 10 / 60, 15 / 60, 30 / 60,
                   1, 2, 4, 6, 12, 24, 48, 72, 168, 336, 720)


def _duration_ticks(hi: float, max_ticks: int = 7) -> list[float]:
  """线性时长轴的刻度（小时）：`[0, step, 2*step, …]`，`hi` 内取一档友好阶梯。"""
  step = _DUR_TICK_STEPS[-1]
  for cand in _DUR_TICK_STEPS:
    if hi / cand <= max_ticks:
      step = cand
      break
  return [round(i * step, 6) for i in range(int(hi // step) + 1)]


def draw_charts(df_cur: pd.DataFrame, df_prev: pd.DataFrame, stats: dict, plt, rz_save,
                rz_heat=(), rz_level=()) -> None:
  """画图到 charts/（数据全部取自 stats，绘图不重算口径）。"""
  if stats.get('charts_suppressed'):
    return                                  # 样本 < CHART_MIN_ROWS：图表没有分布可言，章节用表格呈现
  colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
  _MAIN, _CUR, _PREV = colors[0], colors[0], colors[1]
  # 图 0 综合评分（雷达：本期 vs 上一期；分数见第 2 章表格）
  _draw_scorecard(stats.get('scorecard'), '00_scorecard', plt, rz_save)

  # 图 1 日趋势
  fig, ax = plt.subplots()
  ax.plot(range(len(stats['trend']['cur'])), stats['trend']['cur'], marker='o',
          label='本期', color=_CUR)
  ax.plot(range(len(stats['trend']['prev'])), stats['trend']['prev'], marker='o',
          linestyle='--', label='上一期', color=_PREV)
  _date_axis(ax, stats['trend']['labels'])
  ax.set_xlabel('日期'), ax.set_ylabel('变更单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '01_trend', plt, rz_save)

  # 图 1b 月内周节奏（工作日 vs 周末 日均）：仅月报有数据，周报直接跳过
  if stats.get('rhythm'):
    _draw_rhythm(stats['rhythm'], '01b_month_rhythm', plt, rz_save)

  # 图 2 变更等级分布（构成 ≤6 类 → 环形图；更多类别保留柱形）
  lv = stats['level']
  labels, counts = [r['label'] for r in lv], [r['count'] for r in lv]
  if len(labels) <= 6:
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    _donut(ax, labels, counts, _level_palette(labels, rz_level or colors))
  else:
    fig, ax = plt.subplots()
    ax.bar(labels, counts, width=_bar_width(len(labels)),
           color=_level_palette(labels, rz_level or colors))
    ax.set_xlabel('变更等级'), ax.set_ylabel('变更单量')
    _int_ticks(ax)
  _emit(fig, '02_level', plt, rz_save)

  # 图 3 各园区（本期 vs 上一期）；单园区报告没有园区维度 → 不出图（渲染层给"无可用数据"）
  if stats['park']:
    fig, ax = plt.subplots()
    _dumbbell(ax, [p['name'] for p in stats['park']],
              [p['count'] for p in stats['park']],
              [p['prev'] for p in stats['park']], colors)
    _emit(fig, '03_park', plt, rz_save)

  # 图 4 各园区 × 变更等级结构（横向堆叠条）
  pl = stats.get('park_level') or {}
  pl_parks, pl_levels = pl.get('parks') or [], pl.get('levels') or []
  pl_matrix = pl.get('matrix') or []
  if pl_parks and pl_levels:
    order = sorted(range(len(pl_parks)),
                   key=lambda i: -(sum(pl_matrix[i]) if i < len(pl_matrix) else 0))
    park_names = [pl_parks[i] for i in order]
    pal = _level_palette(pl_levels, rz_level or colors)
    fig, ax = plt.subplots(figsize=(9.0, max(2.6, 0.5 * len(park_names) + 1.4)))
    lefts = [0] * len(park_names)
    for j, lab in enumerate(pl_levels):
      vals = [(pl_matrix[i][j] if i < len(pl_matrix) and j < len(pl_matrix[i]) else 0)
              for i in order]
      ax.barh(park_names, vals, left=lefts, height=_bar_width(len(park_names)),
              color=pal[j], label=lab, edgecolor='white', linewidth=0.8)
      lefts = [l + v for l, v in zip(lefts, vals)]
    ax.invert_yaxis()
    ax.set_xlabel('变更单量')
    _int_ticks(ax, 'x')
    _legend_below(ax)
    _emit(fig, '04_park_level', plt, rz_save)

  # 图 5 各变更类型：上一期总量 → 逐类型增减 → 本期总量（瀑布分解）
  # 原为哑铃图，与图 12「各部门变更单量」同型、在单园区周报里相邻出现（用户 2026-10-30 要求换型）。
  # 表格只有「本期/上一期/占比」三个静态数，读不出**增量由哪类贡献**，这张图补的正是这点。
  tr = stats['change_type']['rows']
  total_n = int(stats.get('total') or 0)
  prev_n = int(stats.get('prev_total') or 0)
  if tr:
    steps = [{'label': r['label'], 'delta': int(r['count']) - int(r['prev'])}
             for r in tr]
    residual = total_n - prev_n - sum(s['delta'] for s in steps)
    if residual:                # 上一期有「未标注」而本期没有：补一步，桥接才闭合
      steps.append({'label': _UNKNOWN, 'delta': residual})
    labels = (['上一期'] if prev_n else []) + [s['label'] for s in steps] + ['本期']
    fig, ax = plt.subplots(figsize=(8.2, 4.8))
    bar_h = _bar_width(len(labels))
    base = prev_n
    if prev_n:
      ax.barh(0, prev_n, height=bar_h, color=colors[0], edgecolor='white',
              linewidth=0.8)
    start_i = 1 if prev_n else 0
    for i, s in enumerate(steps, start=start_i):
      d = s['delta']
      # 增减步统一用中蓝：方向由「落在连接线左/右」表达，不按好坏上色
      # （单量增长只是规模扩大，不是恶化，2026-10-30 用户口径）
      ax.barh(i, abs(d), left=(base if d >= 0 else base + d), height=bar_h,
              color=colors[3], edgecolor='white', linewidth=0.8)
      if prev_n or i > start_i:           # 楼层连接线：上一步终点高度连到本步
        ax.plot([base, base], [i - 1, i], color=colors[5], linewidth=1,
                linestyle='--', zorder=1)
      base += d
    ax.plot([base, base], [start_i + len(steps) - 1, start_i + len(steps)],
            color=colors[5], linewidth=1, linestyle='--', zorder=1)
    ax.barh(len(labels) - 1, total_n, height=bar_h, color=colors[0],
            edgecolor='white', linewidth=0.8)
    ax.set_yticks(range(len(labels)))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel('变更单量')
    ax.set_xlim(0, max(total_n, prev_n, 1) * 1.08)
    ax.grid(True, axis='x')
    ax.grid(False, axis='y')
    _int_ticks(ax, 'x')
    from matplotlib.patches import Patch
    handles = [Patch(facecolor=colors[0], label='合计'),
               Patch(facecolor=colors[3], label='逐类型增减')]
    _legend_below(ax, ncol=2, handles=handles,
                  labels=[h.get_label() for h in handles])
    _emit(fig, '05_change_type', plt, rz_save)

  # 当前阶段分布不再出图（用户 2026-10-29：这张图没必要），第 8 章只留阶段/状态表。

  # 图 6 审批时长区间分布
  hist = stats['flow']['approve']['hist']
  fig, ax = plt.subplots()
  ax.bar(hist['labels'], hist['counts'], width=_bar_width(len(hist['labels'])),
         color=_MAIN)
  ax.set_xlabel('审批时长'), ax.set_ylabel('变更单量')
  ax.set_xticks(range(len(hist['labels'])))
  ax.set_xticklabels(hist['labels'], rotation=30, ha='right')
  _int_ticks(ax)
  _emit(fig, '07_approve_hist', plt, rz_save)

  # 图 8 实施完成偏差分布（相对计划结束）
  dh = stats['schedule']['delay_hist']
  fig, ax = plt.subplots()
  ax.bar([r['档位'] for r in dh], [r['单数'] for r in dh], width=_bar_width(len(dh)),
         color=_MAIN)
  ax.set_xlabel('完成时间相对计划结束'), ax.set_ylabel('变更单量')
  ax.set_xticks(range(len(dh)))
  ax.set_xticklabels([r['档位'] for r in dh], rotation=30, ha='right')
  _int_ticks(ax)
  _emit(fig, '08_delay_hist', plt, rz_save)

  # 图 9 计划 vs 实际实施时长（**线性坐标**；对角线＝计划＝实际，点在下方＝实际更短）
  # 用户 2026-10-29 明确要求改回线性：对数轴虽然能拉开 3 个数量级，但观感与其余图不一致。
  pts = stats['schedule'].get('scatter') or []
  if len(pts) >= 3:
    xs = [p[0] / 60.0 for p in pts]
    ys = [p[1] / 60.0 for p in pts]
    hi = max(max(xs), max(ys)) * 1.05
    # 画布用**默认 8.2×5.2**（与其余图同基准）。2026-10-29 用户先问「为什么是扁的」、
    # 于是改成方形 + `set_aspect('equal')` 让对角线正好 45°；当天又反馈「太方了」并要求
    # 「恢复到之前样子」→ 回宽画布。注意 `set_aspect('equal')` 与 tight bbox 一起用，
    # 存出来的图**必然是正方形**（只改 figsize 无效），所以这里连同 aspect 一起去掉；
    # 参考线仍画 [0, hi] 对角线，只是视觉角度不再是 45°（正文从未宣称是 45°）。
    fig, ax = plt.subplots()
    ax.plot([0, hi], [0, hi], linestyle='--', linewidth=1.0, color='#9aa7b4',
            label='计划＝实际')
    ax.scatter(xs, ys, s=26, alpha=0.65, color=_MAIN, edgecolors='white', linewidths=0.5,
               label='变更单')
    ticks = _duration_ticks(hi)
    ax.set_xlim(0, hi), ax.set_ylim(0, hi)
    ax.set_xticks(ticks), ax.set_yticks(ticks)
    ax.set_xticklabels([_human_minutes(t * 60) for t in ticks], rotation=30, ha='right')
    ax.set_yticklabels([_human_minutes(t * 60) for t in ticks])
    ax.set_xlabel('计划实施时长'), ax.set_ylabel('实际实施时长')
    _legend_below(ax, ncol=2, y=-0.18)           # x 刻度 30° 斜写，图例要让出这块高度
    _emit(fig, '11_plan_actual', plt, rz_save)

  # 图 10 各园区按时完成率（点线图，离 100% 的差距直接可见；率低的在上）
  bp = [r for r in stats['schedule']['by_park'] if r['按时率'] is not None]
  if bp:
    bp = sorted(bp, key=lambda r: r['按时率'])
    fig, ax = plt.subplots(figsize=(8.4, max(2.6, 0.45 * len(bp) + 1.2)))
    _dots_rate(ax, [r['园区'] for r in bp], [r['按时率'] for r in bp], colors)
    _emit(fig, '09_on_time_by_park', plt, rz_save)

  # 图 11 各园区 × 专业构成（横向堆叠条；单园区或单专业时不出）
  ps = stats.get('park_specialty') or {}
  if ps.get('parks') and ps.get('specialties'):
    fig, ax = plt.subplots(figsize=(9.0, max(2.6, 0.5 * len(ps['parks']) + 1.4)))
    _stacked_barh(ax, ps['parks'], ps['specialties'], ps['matrix'], colors)
    _emit(fig, '10_park_specialty', plt, rz_save)

  # 图 12 各部门变更单量（本期 vs 上一期）：单园区专属，全国级没有该键
  dep = (stats.get('dept') or {}).get('rows') or []
  if len(dep) >= 2:
    # 高度夹在 4.8~6.0：16 个部门取 6.0（宽高比 1.37）、6 个取 4.8（1.71），与常规图同量级
    fig, ax = plt.subplots(figsize=(8.2, min(6.0, max(4.8, 0.30 * len(dep) + 1.6))))
    _dumbbell(ax, [r['name'] for r in dep], [r['count'] for r in dep],
              [r['prev'] for r in dep], colors)
    # 高图里轴坐标偏移会被放大成一整条空白带；-0.06 又会让图例与 x 轴标题「工单量」
    # 挤在同一行（读起来像一串标签）→ −0.12 让两者分层
    if len(dep) > 8:
      _legend_below(ax, ncol=2, y=-0.12)
    _emit(fig, '12_dept', plt, rz_save)

  # 图 13 各变更等级 审批/实施/验收 P50（横向分组条；单园区专属）
  bld = (stats.get('flow') or {}).get('by_level_duration') or []
  if bld:
    labels = [r['等级'] for r in bld]
    series = [('审批', [r['审批P50分钟'] for r in bld]),
              ('实施', [r['实施P50分钟'] for r in bld]),
              ('验收', [r['验收P50分钟'] for r in bld])]
    # 只有 3 行等级：`_bar_width` 是按"类别多、柱不许太宽"调的，横向 3 行时整组只占
    # 0.3 行距、柱细得像细线 → 这里按组宽 0.7 本地算，柱与柱之间留白描边
    w = round(0.7 / len(series), 3)
    offs = [round(-w * (len(series) - 1) / 2 + i * w, 3) for i in range(len(series))]
    ys = list(range(len(labels)))[::-1]
    # 3 档等级取 4.8（1.71）；档位多时也不超过 6.0，与其余图体量一致
    fig, ax = plt.subplots(figsize=(8.2, min(6.0, max(4.8, 0.8 * len(labels) + 1.6))))
    for j, (name, vals) in enumerate(series):
      # 缺分位的序列记 NaN（matplotlib 不画），不要用 0 顶替 —— 0 会被读成"只要几分钟"
      ax.barh([y + offs[j] for y in ys],
              [float('nan') if v is None else v / 60.0 for v in vals],
              height=w, color=colors[j], label=name,
              edgecolor='white', linewidth=1.0)
    ax.set_yticks(ys)
    ax.set_yticklabels(labels)
    ax.set_xlabel('时长（小时）')
    _legend_below(ax)
    _emit(fig, '13_stage_duration', plt, rz_save)


def main(plt, rz_save, rz_heat=(), rz_level=()) -> None:
  """沙箱入口：读 cwd 下的 data.csv / prev.csv → 写 stats.json + charts/。"""
  cur = pd.read_csv('data.csv')
  prev_path = Path('prev.csv')
  prev = pd.read_csv(prev_path) if prev_path.is_file() else pd.DataFrame()
  window_path = Path('window.json')
  window = json.loads(window_path.read_text(encoding='utf-8')) if window_path.is_file() else {}
  stats = compute_stats(cur, prev, window)
  Path('stats.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2),
                                encoding='utf-8')
  cur_scope, _, _ = _scope(cur, window, 'cur')
  prev_scope, _, _ = _scope(prev, window, 'prev')
  draw_charts(cur_scope, prev_scope, stats, plt, rz_save, rz_heat,
              rz_level or globals().get('RZ_LEVEL_COLORS') or ())
  print(json.dumps({'rows_cur': stats['total'], 'rows_prev': stats['prev_total'],
                    'kept': stats['scope_filter']['kept'],
                    'charts': len(list(Path('charts').glob('*.png')))}, ensure_ascii=False))

"""阈值工单报告的固定统计与绘图 —— **全确定性，0 次 LLM**。

与其它四类同契约（`compute_stats` / `draw_charts` / `main`），但维度**刻意不同构**：
阈值单没有等级、没有审批时长，价值在**调整意图、发起集中度、热点对象与批量规模、
计划提前期、执行达成**，不是"哪类设备坏得多"或"积压多久"。

实测口径（2026-10-17 用真实接口核对）：

- 列表接口的 `start_time/end_time` 是**计划实施窗口的交集过滤**（同变更单），
  一张计划跨窗口的单会在两个窗口都返回 → 统计前必须**本地按「计划实施开始时间」归属**，
  否则跨期重复计数（`_scope`）。
- `projectName` 是自由文本，既描述调整意图也并列多个对象（`、`/`，`/`；`/`/`）。
  意图与对象类型都是**固定关键词归类**，未识别单独陈列，不推断。
- `initiator` 是 Python 字面量字符串（`{'userId':…,'name':…}`），用 `ast.literal_eval`；
  解析失败回退正则取 name，再失败记「未标注」。
- `stage` / `status` 是**当前快照**，不是流转历史；本类列表**没有审批/验收时长字段**。
- `executeEnd < executeStart` 的脏数据实测存在 → 不算有效实施，计入 `invalid_exec_n`。
"""

import ast
import json
import re
from pathlib import Path

import pandas as pd

# 共用纯助手：本进程 import 走这条；沙箱里源码已拼接注入（import 失败即忽略）
try:
  from service.scheduled_task.report.common import (
    CHART_MIN_ROWS, _add_compare, _all_ticket_nos, _bar_width, _build_scorecard,
    _date_axis, _day_str,
    _dumbbell, _dots_rate, _draw_rhythm, _draw_scorecard, _emit, _human_minutes,
    _int_ticks, _legend_below, _delta_text, _matrix_counts, _month_rhythm,
    _mom_attribution, _mom_top, _pct, _pct_text, _scale_score, _top_card,
    _window_trend,
  )
except ImportError:      # 沙箱：名字已随 common 源码注入
  pass

SPECIALTY_LABEL = {1: '供配电', 2: '制冷', 4: '智能化', 8: '其他'}
STAGE_LABEL = {-1: '驳回', 1: '审批期', 2: '处理期', 3: '验收期', 5: '关闭期', 7: '派发期'}
STATUS_LABEL = {0: '无', 1: '通过', 2: '驳回', 3: '审批中', 4: '实施中',
                5: '撤回', 6: '退回', 7: '验收中', 8: '审批通过', 9: '延期中'}
_UNKNOWN = '未标注'
_CLOSED_STAGE = 5

# 调整意图关键词表：**顺序不可换**（先命中先归类）。取消优先于"关闭"，
# "关闭"优先于"常规修改"——否则"…阈值关闭"会被"修改/设置"误吞。
# 真实数据校对（2026-10-17，2240 单）：「增加」实测全部是**新增阈值项**
# （"增加UPS…阈值"），不是调高数值，故归入常规修改；调高/调低关键词真实数据里
# 尚未出现，保留规则以便将来使用。同义词（更改/更正/添加/增设/上电）按业务补入。
_INTENT_RULES = (
  ('取消阈值', ('取消', '删除')),
  ('关闭屏蔽', ('屏蔽', '关闭', '停用', '禁用', '关机', '退租', '下架')),
  ('恢复启用', ('恢复', '启用', '开启', '解除')),
  ('提高阈值', ('提高', '上调', '提升', '增大')),
  ('降低阈值', ('降低', '下调', '减少', '减小')),
  ('常规修改', ('修改', '调整', '变更', '更改', '更正', '设置', '添加', '增设',
               '新增', '增加', '更新', '加电', '下电', '上电')),
)
INTENT_LABELS = [r[0] for r in _INTENT_RULES] + ['未识别']

# 调整对象类型：固定顺序关键词，先命中先归类（温湿度优先于供配电，
# 因为"弱电间温湿度报警"的主对象是温湿度，不是配电）
_OBJECT_RULES = (
  ('温湿度类', ('温湿度', '温度', '湿度', '温感')),
  ('供配电类', ('UPS', 'STS', 'EPS', '电池', '内阻', '配电', '电量', '电流', '电压',
               '开关', '列头柜', '抽屉柜', '加电', '下电', '上电')),
  ('制冷类', ('空调', '冷机', '风墙', '冷冻', '冷却', '蓄冷', '风机', '风扇', 'CDU')),
  ('漏水类', ('漏水', '漏液', '渗水', '水浸', '液位')),
  ('消防类', ('烟感', '气灭', '消防', '火警')),
  ('弱电智能化类', ('门禁', '视频', '监控', '大屏', '通讯', '动环', 'BA')),
)
OBJECT_LABELS = [r[0] for r in _OBJECT_RULES] + ['其他/未识别']

# 计划提前期 6 档（分钟，右闭）
_LEAD_BANDS = (
  ('≤1天', lambda m: m <= 1440),
  ('1-3天', lambda m: 1440 < m <= 4320),
  ('3-7天', lambda m: 4320 < m <= 10080),
  ('7-14天', lambda m: 10080 < m <= 20160),
  ('14-30天', lambda m: 20160 < m <= 43200),
  ('>30天', lambda m: m > 43200),
)
_LEAD_LABELS = [b[0] for b in _LEAD_BANDS]

# 实际开始偏差 7 档（分钟，相对计划开始；负＝提前，0＝按时）
_DELAY_BANDS = (
  ('提前>1天', lambda d: d <= -1440),
  ('提前1-24时', lambda d: -1440 < d < -60),
  ('提前<1时', lambda d: -60 <= d < 0),
  ('按时', lambda d: d == 0),
  ('延迟<1时', lambda d: 0 < d <= 60),
  ('延迟1-24时', lambda d: 60 < d <= 1440),
  ('延迟>1天', lambda d: d > 1440),
)
_DELAY_LABELS = [b[0] for b in _DELAY_BANDS]

# 批量规模：名称里并列对象的片段数（**不是设备精确数**，报告口径要写明）
_BATCH_LABELS = ['1', '2-3', '4-9', '≥10']
_BATCH_SPLIT = re.compile(r'[、，,；;/]')
_INITIATOR_NAME_RE = re.compile(r"['\"]name['\"]\s*:\s*['\"]([^'\"]+)['\"]")


def _num(df: pd.DataFrame, column: str) -> pd.Series:
  """取数值列（缺失列返回全 NaN，非法值转 NaN）。"""
  if column not in df.columns:
    return pd.Series([float('nan')] * len(df), index=df.index, dtype='float64')
  return pd.to_numeric(df[column], errors='coerce')


def _pctl(values: list, q: float):
  vals = sorted(float(v) for v in values if v is not None)
  if not vals:
    return None
  return round(float(pd.Series(vals).quantile(q)), 1)


def _dist(values: list) -> dict:
  """时长（分钟）分布：P50/P90/均值/最长 + 可读表述。"""
  vals = [float(v) for v in values if v is not None and v >= 0]
  if not vals:
    return {'p50': None, 'p90': None, 'mean': None, 'max': None, 'readable': {}}
  s = pd.Series(vals)
  q50, q90 = float(s.quantile(0.5)), float(s.quantile(0.9))
  return {'p50': round(q50, 1), 'p90': round(q90, 1),
          'mean': round(float(s.mean()), 1), 'max': round(float(s.max()), 1),
          'readable': {'p50': _human_minutes(q50), 'p90': _human_minutes(q90),
                       'mean': _human_minutes(float(s.mean())),
                       'max': _human_minutes(float(s.max()))}}


def _scope(df: pd.DataFrame, window: dict, which: str = 'cur'
           ) -> tuple[pd.DataFrame, int, int]:
  """按「计划实施开始时间」归属统计期；返回 (保留帧, 缺计划开始数, 窗口外数)。"""
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


def _label_counts(series: pd.Series, labels: dict, order: list[str] | None = None,
                  keep_zero: bool = False) -> list[dict]:
  """码值列 → 稳定的 [{label,count,pct}]（给定 order 时按固定顺序）。"""
  vals = series.dropna().map(lambda v: labels.get(int(v), _UNKNOWN)
                             if pd.notna(v) else _UNKNOWN)
  total = int(len(vals))
  vc = vals.value_counts()
  seq = order + [x for x in vc.index if x not in order] if order else sorted(vc.index)
  rows = []
  for label in seq:
    n = int(vc.get(label, 0))
    if n or keep_zero:
      rows.append({'label': str(label), 'count': n, 'pct': _pct(n, total)})
  return rows


def _space_column(df: pd.DataFrame) -> str:
  return 'companyName' if 'companyName' in df.columns else ('dc' if 'dc' in df.columns else '')


def _normalize_text(value) -> str:
  """去首尾、折叠连续空白（源数据里同名存在前导空格）。"""
  return ' '.join(str(value).split()) if pd.notna(value) else ''


def _intent_of(text: str) -> str:
  upper = text.upper()
  for label, keywords in _INTENT_RULES:
    if any(kw in upper for kw in keywords):
      return label
  return '未识别'


def _object_type_of(text: str) -> str:
  upper = text.upper()
  for label, keywords in _OBJECT_RULES:
    if any(kw in upper for kw in keywords):
      return label
  return '其他/未识别'


def _initiator_name(raw) -> tuple[str, bool]:
  """`{'userId':…,'name':…}` 字面量 → (姓名, 是否解析成功)。"""
  if pd.isna(raw):
    return _UNKNOWN, False
  text = str(raw).strip()
  if not text:
    return _UNKNOWN, False
  try:
    parsed = ast.literal_eval(text)
    if isinstance(parsed, dict):
      name = str(parsed.get('name') or '').strip()
      if name:
        return name, True
  except (ValueError, SyntaxError):
    pass
  match = _INITIATOR_NAME_RE.search(text)
  if match:
    return match.group(1).strip(), True
  return _UNKNOWN, False


def _batch_size(name: str) -> int:
  """名称里并列对象的片段数（空名返回 0）。"""
  if not name:
    return 0
  return len([x for x in _BATCH_SPLIT.split(name) if x.strip()])


def _timing(df: pd.DataFrame) -> dict:
  """计划/实际时间对齐结果（逐行索引 → 分钟），统计与代表单共用。"""
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
    if s_ok and r_ok:
      start_delay[i] = float((es.at[i] - ss.at[i]) / 60000.0)
    if s_ok and e_ok:
      delay[i] = float((ee.at[i] - se.at[i]) / 60000.0)
  return {'planned': planned, 'actual': actual, 'start_delay': start_delay,
          'delay': delay, 'sched_valid': len(planned), 'exec_valid': len(actual),
          'invalid_exec_n': invalid_exec_n}


def _lead_block(df: pd.DataFrame) -> dict:
  """计划提前期（计划开始 − 创建，分钟）；负值属补录/脏数据，剔除并计数。"""
  ss, cre = _num(df, 'scheduledStartTime'), _num(df, 'createTime')
  vals, invalid_n, missing_n = [], 0, 0
  for i in df.index:
    s, c = ss.at[i], cre.at[i]
    if pd.isna(s) or pd.isna(c) or s <= 0 or c <= 0:
      missing_n += 1
      continue
    diff = float((s - c) / 60000.0)
    if diff < 0:
      invalid_n += 1
    else:
      vals.append(diff)
  hist = {'labels': list(_LEAD_LABELS),
          'counts': [sum(1 for v in vals if fn(v)) for _, fn in _LEAD_BANDS]}
  return {**_dist(vals), 'valid': len(vals), 'invalid_n': invalid_n,
          'missing_n': missing_n, 'hist': hist}


def _initiator_block(df: pd.DataFrame, total: int) -> dict:
  """发起人集中度：Top10 + 类别数 + 首位占比（未解析记「未标注」）。"""
  names, unknown_n = [], 0
  for raw in (df['initiator'] if 'initiator' in df.columns else []):
    name, ok = _initiator_name(raw)
    unknown_n += 0 if ok else 1
    names.append(name)
  counts = pd.Series(names).value_counts()
  top = [{'name': str(k), 'count': int(v), 'pct': _pct(int(v), total)}
         for k, v in counts.head(10).items()]
  return {'top': top, 'classes': int(len(counts)), 'unknown_n': unknown_n,
          'top_share': _pct(int(counts.iloc[0]), total) if len(counts) else 0.0}


def _hot_block(df: pd.DataFrame, total: int) -> dict:
  """热点调整对象：`projectName` 归一化后 Top10；空名剔除并计数。"""
  names = [_normalize_text(v) for v in
           (df['projectName'] if 'projectName' in df.columns else [])]
  valid = [n for n in names if n]
  counts = pd.Series(valid).value_counts()
  rows = [{'name': str(k), 'count': int(v), 'pct': _pct(int(v), total)}
          for k, v in counts.head(10).items()]
  return {'rows': rows, 'blank_n': int(len(names)) - len(valid),
          'classes': int(len(counts)),
          'top_share': _pct(int(counts.iloc[0]), total) if len(counts) else 0.0}


def _batch_block(df: pd.DataFrame) -> dict:
  """批量规模：名称片段数分档（口径是**名称并列对象计数**，不是设备精确数）。"""
  sizes = [_batch_size(_normalize_text(v)) for v in
           (df['projectName'] if 'projectName' in df.columns else [])]
  sizes = [s for s in sizes if s > 0]
  counts = [0] * len(_BATCH_LABELS)
  for s in sizes:
    if s == 1:
      counts[0] += 1
    elif s <= 3:
      counts[1] += 1
    elif s <= 9:
      counts[2] += 1
    else:
      counts[3] += 1
  return {'valid': len(sizes), 'p50': _pctl(sizes, .5), 'max': max(sizes) if sizes else 0,
          'hist': {'labels': list(_BATCH_LABELS), 'counts': counts}}


def _execution_block(df: pd.DataFrame, timing: dict) -> dict:
  """执行达成：按时率（实施结束 ≤ 计划结束）、开始偏差、时长、按专业按时率。"""
  delays = list(timing['delay'].values())
  base = len(delays)
  on_time_n = sum(1 for d in delays if d <= 0)
  sd = list(timing['start_delay'].values())
  sd_late_n = sum(1 for d in sd if d > 0)

  by_specialty = []
  if 'specialty' in df.columns:
    special = _num(df, 'specialty')
    for code in sorted({int(v) for v in special.dropna()}):
      label = SPECIALTY_LABEL.get(code, f'专业{code}')
      idxs = [i for i in timing['delay']
              if pd.notna(special.at[i]) and int(special.at[i]) == code]
      n = len(idxs)
      if not n:
        continue
      ot = sum(1 for i in idxs if timing['delay'][i] <= 0)
      by_specialty.append({'专业': label, '参与统计': n, '按时单数': ot,
                           '按时率': _pct(ot, n)})

  return {
    'sched_valid': timing['sched_valid'], 'exec_valid': timing['exec_valid'],
    'invalid_exec_n': timing['invalid_exec_n'],
    'missing_exec_n': int(len(df)) - timing['exec_valid'] - timing['invalid_exec_n'],
    'on_time_n': on_time_n, 'on_time_base': base,
    'on_time_rate': _pct(on_time_n, base) if base else None,
    'start_delay': {'p50': _pctl(sd, .5), 'p90': _pctl(sd, .9),
                    'late_n': sd_late_n, 'late_rate': _pct(sd_late_n, len(sd)) if sd else None,
                    'hist': [{'档位': label, '单数': sum(1 for d in sd if fn(d))}
                             for label, fn in _DELAY_BANDS]},
    'planned': _dist(list(timing['planned'].values())),
    'actual': _dist(list(timing['actual'].values())),
    'by_specialty': by_specialty,
  }


def _flow_block(df: pd.DataFrame, window: dict) -> dict:
  """当前阶段/状态快照与未关闭积压（未关闭＝阶段≠关闭期；超期只计正天数）。"""
  stage_nums = _num(df, 'stage')
  open_df = df[stage_nums != _CLOSED_STAGE] if 'stage' in df.columns else df.iloc[0:0]
  win = (window or {}).get('cur') or []
  ref = pd.Timestamp(str(win[1])[:10]) if len(win) > 1 else pd.Timestamp.today().normalize()
  se = _num(df, 'scheduledEndTime')
  space = _space_column(df)

  rows = []
  for i, row in open_df.iterrows():
    age = None
    ms = se.at[i]
    if pd.notna(ms) and ms > 0:
      end_day = (pd.to_datetime(int(ms), unit='ms', utc=True)
                 .tz_convert('Asia/Shanghai').normalize().tz_localize(None))
      age = round((ref - end_day).total_seconds() / 86400, 1)
    st = row.get('stage')
    rows.append({'ticketNo': str(row.get('ticketNo', '')).strip(),
                 '空间': str(row.get(space, '')) if space else '',
                 '项目名称': str(row.get('projectName', '')),
                 '阶段': STAGE_LABEL.get(int(st), _UNKNOWN) if pd.notna(st) else _UNKNOWN,
                 '超期天数': age})
  rows.sort(key=lambda r: (-(r['超期天数'] if r['超期天数'] is not None else -10 ** 9),
                           r['ticketNo']))
  n = int(len(open_df))
  overdue_n = sum(1 for r in rows if r['超期天数'] is not None and r['超期天数'] > 0)
  return {
    'stage': _label_counts(stage_nums, STAGE_LABEL) if 'stage' in df.columns else [],
    'status': (_label_counts(_num(df, 'status'), STATUS_LABEL)
               if 'status' in df.columns else []),
    'open': {'n': n, 'rate': _pct(n, len(df)), 'overdue_n': overdue_n, 'rows': rows[:10]},
    'rejected_n': int(((stage_nums == -1) | (_num(df, 'status') == 2)).sum()),
    'pending_approval_n': int(((stage_nums == 1) | (_num(df, 'status') == 3)).sum()),
  }


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


def _intent_block(cur_texts: list[str], prev_texts: list[str], total: int) -> dict:
  cur = pd.Series([_intent_of(t) for t in cur_texts])
  prev = pd.Series([_intent_of(t) for t in prev_texts])
  cur_counts, prev_counts = cur.value_counts(), prev.value_counts()
  rows = []
  for label in INTENT_LABELS:
    n = int(cur_counts.get(label, 0))
    if n or int(prev_counts.get(label, 0)):
      rows.append({'label': label, 'count': n, 'pct': _pct(n, total),
                   'prev': int(prev_counts.get(label, 0))})
  _add_compare(rows, int(len(prev_texts)))
  return {'rows': rows, 'unrecognized_n': int(cur_counts.get('未识别', 0))}


def _object_types_block(cur_texts: list[str], total: int) -> list[dict]:
  series = pd.Series([_object_type_of(t) for t in cur_texts])
  counts = series.value_counts()
  return [{'label': label, 'count': int(counts.get(label, 0)),
           'pct': _pct(int(counts.get(label, 0)), total)}
          for label in OBJECT_LABELS if int(counts.get(label, 0))]


def _detail_targets(df: pd.DataFrame, hot: dict, sizes: dict) -> list[str]:
  """第 10 章代表单：热点对象 → 批量片段多 → 编号补齐，去重取 5。"""
  nos: list[str] = []

  def _no(i) -> str:
    return str(df.at[i, 'ticketNo']).strip() if 'ticketNo' in df.columns else ''

  def add(no: str) -> None:
    no = (no or '').strip()
    if no and no not in nos:
      nos.append(no)

  name_values = ([_normalize_text(v) for v in df['projectName']]
                 if 'projectName' in df.columns else [''] * len(df))
  names = pd.Series(name_values, index=df.index)
  for item in hot['rows']:
    sub = list(df.index[names == item['name']])
    for i in sorted(sub, key=_no):
      if len(nos) >= 2:
        break
      add(_no(i))
    if len(nos) >= 2:
      break
  for i in sorted(df.index, key=lambda i: (-sizes.get(i, 0), _no(i))):
    if len(nos) >= 5:
      break
    add(_no(i))
  for i in sorted(df.index, key=_no):
    if len(nos) >= 5:
      break
    add(_no(i))
  return nos[:5]


# ==================== 关键指标综合评分（0~100，越高越好） ====================
# 对齐 DcOps 演练报告的「关键指标」思路：把分散在各章的指标折成 6 个**业务维度**的分数，
# 用雷达图一眼看出本期强项/短板与环比变化。**全部确定性**（无 LLM），评分规则同时写进
# 第 13 章数据说明，避免"分数怎么来的"说不清。
_SCORE_DIMS = ('规模控制', '计划前瞻', '执行达成', '闭环质量', '高危管控', '数据规范')
_SCORE_MEANINGS = {
  '规模控制': '单量较上一期增长按增幅扣分，持平或下降满分（无上期基数不扣分）。',
  '计划前瞻': '计划提前期（建单→计划开始）越充足分越高，按 P50 分档。',
  '执行达成': '按计划结束时间完成实施的比例（按时执行率）。',
  '闭环质量': '工单关闭率与非驳回率加权（关闭率 60%，非驳回 40%）。',
  '高危管控': '非「取消阈值 / 关闭屏蔽」类高风险调整的占比。',
  '数据规范': '调整意图可识别率、发起人解析率、计划与实施时间完整率的平均值。',
}
_HIGH_RISK_INTENTS = ('取消阈值', '关闭屏蔽')   # 关闭/取消监控类调整，风险最高


def _lead_score(lead: dict) -> float:
  """计划前瞻：按提前期 P50 分档（越充足分越高），无有效提前期记 0。"""
  p50 = lead.get('p50')
  if not lead.get('valid') or p50 is None:
    return 0.0
  for cut, score in ((1440, 100), (480, 85), (240, 70), (120, 55), (60, 40)):
    if p50 >= cut:
      return float(score)
  return 20.0


def _score_once(total: int, prev_total: int, lead: dict, timing: dict,
                execution: dict, quality: dict, high_risk_n: int,
                unrecognized_n: int, initiator_unknown_n: int) -> dict[str, float]:
  """一个统计期的 6 项得分（同一口径算本期与上一期，雷达两条线可直接比）。"""
  n = max(int(total), 1)
  recognized = (total - unrecognized_n) / n * 100
  parsed = (total - initiator_unknown_n) / n * 100
  times_ok = (timing['sched_valid'] + timing['exec_valid']) / (2 * n) * 100
  return {
    '规模控制': _scale_score(total, prev_total),
    '计划前瞻': _lead_score(lead),
    '执行达成': execution.get('on_time_rate') or 0.0,
    '闭环质量': 0.6 * (quality.get('close_rate') or 0.0)
                + 0.4 * (100 - (quality.get('reject_rate') or 0.0)),
    '高危管控': 100 - high_risk_n / n * 100,
    '数据规范': (recognized + parsed + times_ok) / 3,
  }


def _prev_quality(prev: pd.DataFrame, flow: dict, total: int) -> dict:
  closed = int((_num(prev, 'stage') == _CLOSED_STAGE).sum()) if total else 0
  return {'reject_rate': _pct(flow.get('rejected_n') or 0, total),
          'close_rate': _pct(closed, total)}


def compute_stats(df_cur: pd.DataFrame, df_prev: pd.DataFrame, window: dict) -> dict:
  """本期/上一期阈值单 → 报告需要的全部统计（确定性，可单测）。"""
  raw_cur = int(len(df_cur)) if df_cur is not None else 0
  cur, missing_start, out_of_window = _scope(df_cur, window, 'cur')
  prev, _, _ = _scope(df_prev, window, 'prev')
  total, prev_total = int(len(cur)), int(len(prev))

  trend = _window_trend(cur, prev, 'scheduledStartTime', window)
  mom = {'prev_total': prev_total, 'delta': total - prev_total,
         'pct': _pct(total - prev_total, prev_total) if prev_total else None}

  cur_names = [_normalize_text(v) for v in
               (cur['projectName'] if 'projectName' in cur.columns else [])]
  prev_names = [_normalize_text(v) for v in
                (prev['projectName'] if 'projectName' in prev.columns else [])]
  intent = _intent_block(cur_names, prev_names, total)
  intent_vals = [_intent_of(t) for t in cur_names]
  objects = _object_types_block(cur_names, total)
  hot = _hot_block(cur, total)
  initiator = _initiator_block(cur, total)
  specialty = _specialty_block(cur, prev)
  park = _park_block(cur, prev, total)

  # 交叉维度：意图 × 园区（热力图）、发起人 Top5 × 意图（表格）。
  # 单园区查询不带 companyName → 无空间维度，不产出（渲染层不出图/表）。
  park_names = [p['name'] for p in park] if 'companyName' in cur.columns else []
  intent_labels = [r['label'] for r in intent['rows']]
  intent_park = {'intents': [], 'parks': [], 'matrix': []}
  if len(park_names) >= 2 and len(intent_labels) >= 2:
    intent_park = {
      'intents': intent_labels, 'parks': park_names,
      'matrix': _matrix_counts(intent_vals, cur['companyName'].astype(str).tolist(),
                               intent_labels, park_names),
    }
  ini_names = ([_initiator_name(v)[0] for v in cur['initiator']]
               if 'initiator' in cur.columns else [])
  ini_top = [r['name'] for r in initiator['top'][:5]]
  initiator_intent = {'initiators': [], 'intents': [], 'matrix': []}
  if ini_top and len(intent_labels) >= 2:
    initiator_intent = {
      'initiators': ini_top, 'intents': intent_labels,
      'matrix': _matrix_counts(ini_names, intent_vals, ini_top, intent_labels),
    }
  batch = _batch_block(cur)
  lead = _lead_block(cur)

  timing = _timing(cur)
  execution = _execution_block(cur, timing)
  flow = _flow_block(cur, window)

  # 上一期的同一组指标（只服务综合评分；雷达两条线口径必须一致）
  prev_timing = _timing(prev)
  prev_execution = _execution_block(prev, prev_timing)
  prev_flow = _flow_block(prev, window)
  prev_lead = _lead_block(prev)
  prev_initiator = _initiator_block(prev, prev_total)
  prev_quality = _prev_quality(prev, prev_flow, prev_total)
  cur_high = sum(r['count'] for r in intent['rows'] if r['label'] in _HIGH_RISK_INTENTS)
  prev_high = sum(r['prev'] for r in intent['rows'] if r['label'] in _HIGH_RISK_INTENTS)
  prev_unrecognized = sum(1 for t in prev_names if _intent_of(t) == '未识别')
  cur_quality = {
    'reject_rate': _pct(flow['rejected_n'], total),
    'close_rate': (_pct(int((_num(cur, 'stage') == _CLOSED_STAGE).sum()), total)
                   if total else 0.0),
  }
  cur_scores = _score_once(total, prev_total, lead, timing, execution, cur_quality,
                           cur_high, intent['unrecognized_n'], initiator['unknown_n'])
  prev_scores = _score_once(prev_total, total, prev_lead, prev_timing, prev_execution,
                            prev_quality, prev_high, prev_unrecognized,
                            prev_initiator['unknown_n'])
  scorecard = _build_scorecard([
    (dim, _SCORE_MEANINGS[dim], cur_scores[dim], prev_scores[dim])
    for dim in _SCORE_DIMS
  ])

  sizes ={i: _batch_size(_normalize_text(cur.at[i, 'projectName']))
           if 'projectName' in cur.columns else 0 for i in cur.index}
  space = _space_column(cur)
  lead_minutes = {}
  if 'scheduledStartTime' in cur.columns and 'createTime' in cur.columns:
    ss, cre = _num(cur, 'scheduledStartTime'), _num(cur, 'createTime')
    for i in cur.index:
      if pd.notna(ss.at[i]) and pd.notna(cre.at[i]) and ss.at[i] > 0 and cre.at[i] > 0:
        lead_minutes[i] = float((ss.at[i] - cre.at[i]) / 60000.0)

  mom_key = '园区环比变动最大' if space == 'companyName' else '部门环比变动最大'
  rejected_n = flow['rejected_n']
  quality = {
    'reject_n': rejected_n, 'reject_rate': _pct(rejected_n, total),
    'close_n': int((_num(cur, 'stage') == _CLOSED_STAGE).sum()) if len(cur) else 0,
    'close_rate': _pct(int((_num(cur, 'stage') == _CLOSED_STAGE).sum()), total)
    if len(cur) else 0.0,
    'on_time_rate': execution['on_time_rate'],
  }

  longest_lead = [{'ticketNo': str(cur.at[i, 'ticketNo']).strip()
                   if 'ticketNo' in cur.columns else '',
                   '项目名称': _normalize_text(cur.at[i, 'projectName'])
                   if 'projectName' in cur.columns else '',
                   '发起人': _initiator_name(cur.at[i, 'initiator'])[0]
                   if 'initiator' in cur.columns else _UNKNOWN,
                   '提前期分钟': round(v, 1), '提前期可读': _human_minutes(v)}
                  for i, v in sorted(lead_minutes.items(),
                                     key=lambda kv: (-kv[1],
                                                     str(cur.at[kv[0], 'ticketNo']).strip()
                                                     if 'ticketNo' in cur.columns else ''))[:5]]
  extras = {
    '最热调整对象': hot['rows'][:5],
    '最长提前期单': longest_lead,
    '发起人集中度': {'首位占比': initiator['top_share'], 'Top5': initiator['top'][:5],
                     '发起人数': initiator['classes']},
    mom_key: _mom_attribution(cur, prev, SPECIALTY_LABEL),
    '未关闭积压': flow['open']['rows'],
  }

  top_intent = _top_card(intent['rows'], '主要方向', '单')
  kpi = {
    'total': {'value': str(total), 'sub': f'上一期 {prev_total} 单'},
    'daily_avg': {'value': f'{round(total / max(len(trend["labels"]), 1), 1)}',
                  'sub': f'覆盖 {len(trend["labels"])} 天'},
    'mom': {'value': (_delta_text(mom["delta"]) if prev_total else '—'),
            'sub': (_pct_text(mom['pct'], signed=True)
                    if mom['pct'] is not None else '无上期基数')},
    'intent_top': top_intent,
    'lead_p50': {'value': (lead['readable'].get('p50') or '—'),
                 'sub': f'{lead["valid"]} 单可统计'},
    'on_time': {'value': (f'{execution["on_time_rate"]}%'
                          if execution['on_time_rate'] is not None else '—'),
                'sub': f'{execution["on_time_n"]} / {execution["on_time_base"]} 单'},
    'initiator_top': {
      'value': initiator['top'][0]['name'] if initiator['top'] else '—',
      'sub': (f'{initiator["top"][0]["count"]} 单 · 占 {initiator["top_share"]}%'
              if initiator['top'] else '无数据')},
    'hot_share': {
      'value': (f'{hot["top_share"]}%' if hot['rows'] else '—'),
      'sub': (f'{hot["rows"][0]["name"][:20]}' if hot['rows'] else '无数据')},
  }

  out = {
    'window': window, 'total': total, 'prev_total': prev_total,
    'daily_avg': round(total / max(len(trend['labels']), 1), 1),
    'mom': mom, 'trend': trend, 'intent': intent, 'specialty': specialty,
    'park': park, 'object_types': objects, 'hot_items': hot, 'initiator': initiator,
    'intent_park': intent_park, 'initiator_intent': initiator_intent,
    'batch': batch, 'lead': lead, 'execution': execution, 'threshold_flow': flow,
    'quality': quality, 'kpi': kpi, 'extras': extras, 'scorecard': scorecard,
    'scope_filter': {'raw': raw_cur, 'kept': total, 'out_of_window': out_of_window,
                     'missing_start': missing_start, 'basis': '计划实施开始时间'},
    'detail_targets': _detail_targets(cur, hot, sizes),
    'detail_all': (_all_ticket_nos(cur)
                   if (window or {}).get('period') == 'weekly' else []),
    'charts_suppressed': total < CHART_MIN_ROWS,
  }
  # 月报专属维度（周报**不写这两个键**，输出逐字不变）
  if (window or {}).get('period') == 'monthly':
    out['rhythm'] = _month_rhythm(trend, window, '单')
    out['mom_top'] = _mom_top(
      {'park': park, 'specialty': specialty, 'intent': intent},
      [('园区', 'park'), ('专业', 'specialty'), ('调整意图', 'intent')])
  return out


# ==================== 绘图（11 张，全确定性；图表上不加数据标签） ====================

def draw_charts(df_cur: pd.DataFrame, df_prev: pd.DataFrame, stats: dict, plt, rz_save,
                rz_heat=(), rz_level=()) -> None:
  """画 10 张图到 charts/（数据全部取自 stats，绘图不重算口径）。"""
  if stats.get('charts_suppressed'):
    return                                  # 样本 < CHART_MIN_ROWS：图表没有分布可言，章节用表格呈现
  colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
  _CUR, _PREV = colors[0], colors[1]
  # 每张单序列图换一个主题色（全篇同一深蓝太单调）：分布类用蓝、及时性用绿、延迟类用橙
  _C_BLUE, _C_GREEN, _C_ORANGE, _C_MID = (colors[0], colors[1], colors[2],
                                          colors[3] if len(colors) > 3 else colors[0])

  # 图 0 关键指标综合评分（雷达：本期 vs 上一期；顶点不标数字，分数见章内表格）
  _draw_scorecard(stats.get('scorecard'), '00_scorecard', plt, rz_save)

  # 图 1 日趋势
  fig, ax = plt.subplots()
  ax.plot(range(len(stats['trend']['cur'])), stats['trend']['cur'], marker='o',
          label='本期', color=_CUR)
  ax.plot(range(len(stats['trend']['prev'])), stats['trend']['prev'], marker='o',
          linestyle='--', label='上一期', color=_PREV)
  _date_axis(ax, stats['trend']['labels'])
  ax.set_xlabel('日期'), ax.set_ylabel('阈值单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '01_trend', plt, rz_save)

  # 图 1b 月内周节奏（工作日 vs 周末 日均）：仅月报有数据，周报直接跳过
  if stats.get('rhythm'):
    _draw_rhythm(stats['rhythm'], '01b_month_rhythm', plt, rz_save)

  # 图 2 调整意图（本期 vs 上一期，哑铃图看变化方向）
  it = stats['intent']['rows']
  if it:
    fig, ax = plt.subplots(figsize=(8.4, max(2.6, 0.45 * len(it) + 1.2)))
    _dumbbell(ax, [r['label'] for r in it], [r['count'] for r in it],
              [r['prev'] for r in it], colors)
    _emit(fig, '02_intent', plt, rz_save)

  # 图 3 各专业阈值单量（横向）
  sp = stats['specialty']
  if sp:
    sp_sorted = sorted(sp, key=lambda r: r['count'])
    fig, ax = plt.subplots(figsize=(8.4, max(2.4, 0.45 * len(sp_sorted) + 1.2)))
    ax.barh([r['label'] for r in sp_sorted], [r['count'] for r in sp_sorted],
            height=_bar_width(len(sp_sorted)), color=_C_BLUE)
    ax.set_xlabel('阈值单量')
    _int_ticks(ax, 'x')
    _emit(fig, '03_specialty', plt, rz_save)

  # 图 4 发起人 Top10（横向）
  ini = stats['initiator']['top']
  if ini:
    ini_sorted = sorted(ini, key=lambda r: r['count'])
    fig, ax = plt.subplots(figsize=(9.0, max(2.6, 0.42 * len(ini_sorted) + 1.2)))
    ax.barh([r['name'] for r in ini_sorted], [r['count'] for r in ini_sorted],
            height=_bar_width(len(ini_sorted)), color=_C_GREEN)
    ax.set_xlabel('发起阈值单量')
    _int_ticks(ax, 'x')
    _emit(fig, '04_initiator', plt, rz_save)

  # 图 5 调整对象类型（横向）
  ob = stats['object_types']
  if ob:
    ob_sorted = sorted(ob, key=lambda r: r['count'])
    fig, ax = plt.subplots(figsize=(8.8, max(2.4, 0.45 * len(ob_sorted) + 1.2)))
    ax.barh([r['label'] for r in ob_sorted], [r['count'] for r in ob_sorted],
            height=_bar_width(len(ob_sorted)), color=_C_MID)
    ax.set_xlabel('阈值单量')
    _int_ticks(ax, 'x')
    _emit(fig, '05_object_types', plt, rz_save)

  # 图 6 批量规模分档
  bh = stats['batch']['hist']
  fig, ax = plt.subplots()
  ax.bar(bh['labels'], bh['counts'], width=_bar_width(len(bh['labels'])), color=_C_BLUE)
  ax.set_xlabel('名称并列对象片段数'), ax.set_ylabel('阈值单量')
  _int_ticks(ax)
  _emit(fig, '06_batch_hist', plt, rz_save)

  # 图 7 计划提前期分档
  lh = stats['lead']['hist']
  fig, ax = plt.subplots()
  ax.bar(lh['labels'], lh['counts'], width=_bar_width(len(lh['labels'])), color=_C_GREEN)
  ax.set_xlabel('计划提前期（计划开始 − 创建）'), ax.set_ylabel('阈值单量')
  _int_ticks(ax)
  _emit(fig, '07_lead_hist', plt, rz_save)

  # 图 8 实际开始偏差分档
  dh = stats['execution']['start_delay']['hist']
  fig, ax = plt.subplots()
  ax.bar([r['档位'] for r in dh], [r['单数'] for r in dh], width=_bar_width(len(dh)),
         color=_C_ORANGE)
  ax.set_xlabel('实际开始相对计划开始'), ax.set_ylabel('阈值单量')
  ax.set_xticks(range(len(dh)))
  ax.set_xticklabels([r['档位'] for r in dh], rotation=30, ha='right')
  _int_ticks(ax)
  _emit(fig, '08_start_delay_hist', plt, rz_save)

  # 图 9 各专业按时执行率（点线图，带 100% 参考线）
  bs = [r for r in stats['execution']['by_specialty'] if r['按时率'] is not None]
  if bs:
    bs = sorted(bs, key=lambda r: r['按时率'])
    fig, ax = plt.subplots(figsize=(8.4, max(2.4, 0.45 * len(bs) + 1.2)))
    _dots_rate(ax, [r['专业'] for r in bs], [r['按时率'] for r in bs], colors)
    _emit(fig, '09_on_time_by_specialty', plt, rz_save)

  # 图 10 调整意图 × 园区（热力图）
  ip = stats.get('intent_park') or {}
  if ip.get('intents') and ip.get('parks'):
    matrix = ip['matrix']
    fig, ax = plt.subplots(figsize=(max(4.6, 1.1 * len(ip['parks']) + 2.0),
                                    max(3.0, 0.55 * len(ip['intents']) + 1.6)))
    from matplotlib.colors import LinearSegmentedColormap
    colormap = (LinearSegmentedColormap.from_list('rz_heat', list(rz_heat))
                if rz_heat else 'Blues')
    im = ax.imshow(matrix, cmap=colormap, aspect='auto')
    ax.set_xticks(range(len(ip['parks'])))
    ax.set_xticklabels(ip['parks'], rotation=20, ha='right')
    ax.set_yticks(range(len(ip['intents'])))
    ax.set_yticklabels(ip['intents'])
    ax.grid(False)
    cbar = fig.colorbar(im, ax=ax, fraction=0.035, pad=0.02)
    cbar.set_label('阈值单量')
    _emit(fig, '10_intent_park', plt, rz_save)


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
  draw_charts(cur_scope, prev_scope, stats, plt, rz_save, rz_heat, rz_level)
  print(json.dumps({'rows_cur': stats['total'], 'rows_prev': stats['prev_total'],
                    'kept': stats['scope_filter']['kept'],
                    'charts': len(list(Path('charts').glob('*.png')))}, ensure_ascii=False))

"""两类报告共用的**纯助手** —— 事件工单与故障工单都用它，保证口径与写法只有一份。

⚠️ **沙箱里不能 import 本模块**（`python -I` 忽略 PYTHONPATH/user site）：
`render.run_compute()` 会把**本文件源码**与该类型的 compute 源码**拼接后注入**沙箱，
所以这里只能放"纯函数 + 沙箱已有的依赖（pd/plt）"，不能 import 项目内其它模块。

compute 模块（`event_compute` / `breakdown_compute`）本进程 import 时靠一段
`try: from ...common import …` 拿到这些名字；在沙箱里那段 import 会失败并被忽略
（源码已拼接，名字本就在同一命名空间）。
"""

import ast

import pandas as pd

# 图表最小样本：本期少于此单量时不出图 —— 一两条数据的"分布"没有形状，
# 只剩 0/1 刻度和 100% 占比，图反而比表格更难读（2026-10-18 实测：全国周报仅 1 单仍出 9 张图）。
CHART_MIN_ROWS = 3

# 部门表最多印几行。**渲染、事实、解读三处必须同一切片** —— 否则模型会点名表里
# 看不到的部门（2026-10-29：要点里出现 B1M4 分部，而表格只印前 12 行）。
DEPT_TABLE_LIMIT = 12


def _trim0(value: float) -> str:
  """数值去掉无意义的 `.0`（12.0 → 12），真实差异保留一位（0.9 → 0.9）。

  **负零归一成 0**：`round(-0.04, 1)` 得到 `-0.0`，`%g` 会印出「-0」（报告里实测
  出现过「提前期 -1.9 分钟（-0 小时）」）。四舍五入到一位后等于 0 就是 0，不该带负号。
  """
  num = round(float(value), 1)
  if num == 0:
    num = 0.0
  return f'{num:g}'


def _pct(n, base):
  """占比（%）：一位小数；刚好整数值时不带小数（100.0 → 100，PDF 里不再满屏 `.0`）。"""
  if not base:
    return 0
  value = round(n / base * 100, 1)
  return int(value) if float(value).is_integer() else value


def _pct_text(value, signed: bool = False) -> str:
  """百分比显示：整数值不带小数（100% / +0%），其余保留一位（16.7%）；None → —。

  **负零归一成 0**：否则 -0.04% 会被印成「-0%」（与 `_trim0` 同一类问题）。
  """
  if value is None:
    return '—'
  num = round(float(value), 1)
  if num == 0:
    num = 0.0
  text = f'{num:+.1f}' if signed else f'{num:.1f}'
  if text.endswith('.0'):
    text = text[:-2]
  return text + '%'


def _delta_text(delta) -> str:
  """增减量显示：0 不带正号（「+0」会被读成"增加了 0"，2026-10-23 用户实测反馈）。"""
  delta = int(delta or 0)
  return '0' if delta == 0 else f'{delta:+d}'


def _int_ticks(ax, axis: str = 'y') -> None:
  """计数量轴强制整数刻度（样本小时 matplotlib 会给出 0.0/0.2/… 这类无意义小数）。"""
  from matplotlib.ticker import MaxNLocator
  (ax.yaxis if axis == 'y' else ax.xaxis).set_major_locator(MaxNLocator(integer=True))


def _date_axis(ax, labels, max_ticks: int = 8) -> None:
  """日期刻度抽稀：标签数超过 `max_ticks` 时等距抽稀（**强制保留最后一天**）并 45° 旋转。

  月报 30/31 个 `MM-DD` 挤在 9.6in 图上会糊成一团（用户实测反馈）；周报 7 个标签走
  全量分支、旋转 0 —— 与旧输出逐字一致（e2e 快照可验）。
  """
  labels = [str(x) for x in (labels or [])]
  n = len(labels)
  if not n:
    return
  if n <= max_ticks:
    ax.set_xticks(range(n))
    ax.set_xticklabels(labels)
    return
  step = -(-(n - 1) // (max_ticks - 1))     # 向上取整，不引 math
  idx = sorted(set(list(range(0, n, step)) + [n - 1]))
  ax.set_xticks(idx)
  ax.set_xticklabels([labels[i] for i in idx], rotation=45, ha='right')


def _day_str(ms_series: pd.Series) -> pd.Series:
  """"毫秒时间戳 → 北京时间日期串。显式转时区，不依赖宿主机 TZ。"""
  return (pd.to_datetime(ms_series, unit='ms', utc=True)
          .dt.tz_convert('Asia/Shanghai').dt.strftime('%Y-%m-%d'))

def _window_trend(df_cur: pd.DataFrame, df_prev: pd.DataFrame,
                  column: str, window: dict) -> dict:
  """按**窗口完整日期轴**统计日趋势（含无单的日期），上一期按位置对齐到本期日期轴。

  为什么不用「有单的日期集合」：本期 5 天有单、上一期 7 天有单时，两条线会各自从
  自己的第一天开始画，日期与数值整体错位；事实化后「日期 + 上一期数值」也会错配
  （2026-10-16 变更报告实测）。上一期与本期长度可能不同（月报 30/31 天），故按**位置**
  对齐，缺位补 0。
  """
  def _axis(which: str) -> list[str]:
    win = (window or {}).get(which) or []
    if len(win) < 2:
      return []
    start, end = str(win[0])[:10], str(win[1])[:10]
    return [d.strftime('%Y-%m-%d') for d in pd.date_range(start, end, freq='D')]

  def _counts(df: pd.DataFrame) -> dict:
    if df is None or not len(df) or column not in df.columns:
      return {}
    days = _day_str(pd.to_numeric(df[column], errors='coerce'))
    return {str(k): int(v) for k, v in days.value_counts().items()}

  cur_axis, prev_axis = _axis('cur'), _axis('prev')
  cur_counts, prev_counts = _counts(df_cur), _counts(df_prev)
  # 窗口缺失时回退到「有单的日期集合」（保持旧调用方的行为，趋势不至于变空）
  if not cur_axis:
    cur_axis = sorted(cur_counts)
  if not prev_axis and prev_counts:
    prev_axis = sorted(prev_counts)
  # 上一期**按位置**对位到本期日期轴；月报 30/31 天时末位没有对位日期（记 ''，不是 0）
  prev_days = [prev_axis[i] if i < len(prev_axis) else '' for i in range(len(cur_axis))]
  cur = [cur_counts.get(d, 0) for d in cur_axis]
  prev = [prev_counts.get(d, 0) for d in prev_days]
  if cur:
    low_i = min(range(len(cur)), key=lambda i: (cur[i], cur_axis[i]))
    peak_i = max(range(len(cur)), key=lambda i: (cur[i], cur_axis[i]))
    peak_day, peak_n = cur_axis[peak_i], cur[peak_i]
    low_day, low_n = cur_axis[low_i], cur[low_i]
  else:
    peak_day, peak_n, low_day, low_n = '', 0, '', 0
  return {'labels': [d[5:] for d in cur_axis], 'cur': cur, 'prev': prev,
          'prev_days': prev_days,
          'peak_day': peak_day, 'peak_n': peak_n, 'low_day': low_day, 'low_n': low_n}


_DOW_LABELS = ('周一', '周二', '周三', '周四', '周五', '周六', '周日')


def _month_rhythm(trend: dict, window: dict, unit: str = '起') -> dict:
  """月报专属「周内节奏」：按**日趋势的完整日期轴**切自然周 + 工作日/周末 + 星期分布。

  直接吃 `_window_trend` 的结果（位置对齐），不再从帧里重算日期 —— 否则补位/缺失口径
  可能与趋势图不一致。按自然周（周一为界）分组；首末周必然不足 7 天，故每桶都带
  `days` 与 `avg`（日均）—— **比较长短不一的周只能用日均**，原始单量会误判。
  """
  win = (window or {}).get('cur') or []
  cur = (trend or {}).get('cur') or []
  if len(win) < 2 or not cur:
    return {}
  start, end = str(win[0])[:10], str(win[1])[:10]
  axis = [d.strftime('%Y-%m-%d') for d in pd.date_range(start, end, freq='D')]
  counts = {d: int(v) for d, v in zip(axis, cur)}

  weeks: list[dict] = []
  cur_week = None
  dow_counts = [0] * 7
  for d in axis:
    ts = pd.Timestamp(d)
    idx = ts.weekday()
    n = counts.get(d, 0)
    dow_counts[idx] += n
    week_key = ts.isocalendar()[1]
    if week_key != cur_week:
      weeks.append({'label': f'第{len(weeks) + 1}周', 'start': d, 'end': d,
                    'days': 0, 'count': 0, 'work_days': 0, 'work_count': 0,
                    'rest_days': 0, 'rest_count': 0})
      cur_week = week_key
    w = weeks[-1]
    w['end'] = d
    w['days'] += 1
    w['count'] += n
    if idx < 5:
      w['work_days'] += 1
      w['work_count'] += n
    else:
      w['rest_days'] += 1
      w['rest_count'] += n
  for w in weeks:
    w['avg'] = round(w['count'] / w['days'], 1) if w['days'] else 0
    w['work_avg'] = round(w['work_count'] / w['work_days'], 1) if w['work_days'] else 0
    w['rest_avg'] = round(w['rest_count'] / w['rest_days'], 1) if w['rest_days'] else 0

  work = {'label': '工作日', 'days': sum(1 for d in axis if pd.Timestamp(d).weekday() < 5),
          'count': sum(counts.get(d, 0) for d in axis if pd.Timestamp(d).weekday() < 5)}
  rest = {'label': '周末', 'days': sum(1 for d in axis if pd.Timestamp(d).weekday() >= 5),
          'count': sum(counts.get(d, 0) for d in axis if pd.Timestamp(d).weekday() >= 5)}
  for b in (work, rest):
    b['avg'] = round(b['count'] / b['days'], 1) if b['days'] else 0
  return {'weeks': weeks, 'split': [work, rest],
          'dow': [{'label': lab, 'count': n} for lab, n in zip(_DOW_LABELS, dow_counts)],
          'total': sum(counts.get(d, 0) for d in axis), 'unit': unit}


def _parse_accept(df: pd.DataFrame, column: str = 'checkEndApprove') -> tuple[list, list]:
  """解析审批/验收列 → (时长分钟列表, 是否超时列表)；解析不到的为 None。

  该列是**字符串**形如 `{'approveTime': 225307815, 'timeOut': True}` —— Python 字面量
  **不是 JSON**（`True` 不是 `true`），用 `ast.literal_eval`；`json.loads` 会失败。
  事件的 `checkEndApprove` 是验收，变更单的 `checkBeginApprove` 是审批、`checkEndApprove`
  是验收，故列名参数化；字段整体缺失或全空时返回 ([], [])，由调用方标"数据不可得"。
  """
  if column not in df.columns:
    return [], []
  minutes: list = []
  timeouts: list = []
  for raw in df[column]:
    mins, to = None, None
    if isinstance(raw, str) and raw.strip():
      try:
        parsed = ast.literal_eval(raw)
      except (ValueError, SyntaxError):
        parsed = None
      if isinstance(parsed, dict):
        ms = parsed.get('approveTime')
        if isinstance(ms, (int, float)) and ms > 0:
          mins = float(ms) / 60000.0
        flag = parsed.get('timeOut')
        if isinstance(flag, bool):
          to = flag
    minutes.append(mins)
    timeouts.append(to)
  return minutes, timeouts

def _top_card(rows: list[dict], _label: str, unit: str = '起') -> dict:
  """指标卡：取**计数最多**的那一项（计数并列时按名称升序，保证跨期稳定）。

  `unit` 随工单类型走（事件/故障「起」、变更「单」），免得卡片与全篇单位不一致。
  """
  if not rows:
    return {'value': '—', 'sub': '无数据'}
  top = sorted(rows, key=lambda r: (-r['count'], r['label']))[0]
  return {'value': top['label'], 'sub': f'{top["count"]} {unit}（{top["pct"]}%）'}

def _human_minutes(m) -> str:
  """分钟 → 可读时长。**统计与解读共用同一个换算**（模型不得自己换单位）。

  超过 60 分钟才换算成小时/天：48 分钟写成「0.8 小时」反而不好读（用户 2026-10-29 要求）。
  """
  if m is None:
    return '—'
  if m < 60:
    return f'{_trim0(m)} 分钟'
  hours = m / 60.0
  return (f'{_trim0(hours)} 小时' if hours < 48
          else f'{_trim0(hours / 24)} 天')

def _counts_by_label(series: pd.Series, labels: dict | None = None) -> list[dict]:
  """按值计数 → 稳定的 [{label,count,pct}]（按值的自然顺序排，保证跨期一致）。"""
  counts = series.value_counts().to_dict()
  total = int(series.notna().sum())
  out = []
  for value in sorted(counts):
    label = labels.get(value, f'{value}级') if labels else f'{value}级'
    n = int(counts[value])
    out.append({'label': label, 'count': n, 'pct': _pct(n, total)})
  return out

def _add_compare(rows: list[dict], prev_total) -> list[dict]:
  """给具名计数行补齐**可比的上一期字段**：`prev_pct` / `delta_count` / `delta_pp`。

  为什么要补：行里原来只有本期的 `pct` 和上一期的**计数** `prev`，解读层拿到的就是
  这两样 —— 实测它把 `40%`（占本期）和 `27`（上期计数）相减，写出「智能化占比 40%，
  较上期增加 13%」（真实变化是 40% vs 48.2%，即 −8.2 个百分点）。可比量必须在**平台
  算好**再下发，和「占比/换算不算模型自己算」是同一条规矩。

  - `prev_pct`：上一期占比，分母是**该块自己的上一期总量**（不是本期总量）。
  - `delta_count`：本期计数 − 上一期计数。
  - `delta_pp`：本期占比 − 上一期占比，单位是**百分点**（不是 %）。
  - 无上一期数据（`prev_total` 为 0/None）时三者一律 `None`：写 0 会被读成
    「上一期占比 0%」，是另一句假话。
  """
  for row in rows or []:
    prev_n = row.get('prev')
    if prev_n is None or not prev_total:
      row['prev_pct'] = row['delta_count'] = row['delta_pp'] = None
      continue
    prev_n = int(prev_n)
    row['prev_pct'] = _pct(prev_n, prev_total)
    row['delta_count'] = int(row.get('count') or 0) - prev_n
    row['delta_pp'] = (round(float(row['pct']) - float(row['prev_pct']), 1)
                       if row.get('pct') is not None else None)
  return rows


def _repeat_problems(df: pd.DataFrame, park_label: str = '') -> list[dict]:
  """按 `projectName` 聚类出**全部**重复发生的问题（≥2 次），按 (次数降序, 名称升序) 排。

  `park_label` 是单园区报告的园区名（`window['scope']`）：单园区取数**不带
  `companyName` 列**，只靠 `companyName` 会让「涉及园区」整列为空；有园区名时兜底。

  ⚠️ **不要在这里截断**：质量指标（重复问题类别数、涉及单数、占比）与 KPI 卡片都基于它 ——
  截断会让这些数字系统性少报（2026-10-14 实测：截到 top10 时是 10 类/23 单，
  实际 16 类/35 单）。展示层只取前若干条，那是渲染的事。

  并列名次要按名称二次排序：否则同次数的类目顺序取决于 `value_counts` 的内部顺序，
  跨期/跨次不稳定，违背"固定口径"的前提。
  """
  if 'projectName' not in df.columns:
    return []
  names = df['projectName'].astype(str).str.strip()
  counts = names.value_counts()
  rows = sorted(((str(n), int(c)) for n, c in counts[counts >= 2].items()),
                key=lambda t: (-t[1], t[0]))
  out = []
  for name, cnt in rows:
    sub = df[names == name]
    parks = sorted({str(p) for p in sub.get('companyName', []) if isinstance(p, str)})
    if not parks and park_label:
      parks = [str(park_label)]
    dcs = sorted({str(d) for d in sub.get('dc', []) if isinstance(d, str)})
    # 代表单编号：取排最前的一行（供第 10 章服务端按 ticketNo 换 id 取详情）
    no = ''
    if 'ticketNo' in sub.columns:
      vals = sorted(str(v) for v in sub['ticketNo'].tolist()
                    if isinstance(v, str) and v.strip())
      no = vals[0] if vals else ''
    # 同一问题在**单个机房**内的最大重复次数：区分「同一机房反复同一告警」（要治设备）
    # 与「多机房各一次」（要治共性），这是"重复问题"里唯一还没被算出来的维度
    top_dc, top_n = '', 0
    if dcs:
      dc_counts = sub['dc'].value_counts()
      top_dc, top_n = str(dc_counts.index[0]), int(dc_counts.iloc[0])
    out.append({'name': name, 'count': cnt, 'parks': parks, 'dcs': dcs, 'ticketNo': no,
                '集中机房': top_dc, '集中机房单数': top_n})
  return out


_DETAIL_ALL_LIMIT = 500


def _all_ticket_nos(df: pd.DataFrame, limit: int = _DETAIL_ALL_LIMIT) -> list[str]:
  """本期全部 ticketNo（去重、保序、去空/NaN）——周报逐单取详情用。

  为什么放在这里：五个 compute 都要用，且**沙箱里不能读 settings**（本文件源码被拼接注入）。
  上限只防 stats.json 过大；真正的取数上限在 workflow 里按配置再裁一次。
  """
  if 'ticketNo' not in df.columns:
    return []
  out, seen = [], set()
  for v in df['ticketNo'].tolist():
    if pd.isna(v):
      continue
    s = str(v).strip()
    if not s or s in ('nan', 'None') or s in seen:
      continue
    seen.add(s)
    out.append(s)
    if len(out) >= limit:
      break
  return out


# ==================== 可归因切片（两类共用） ====================
# 这些切片是「解读能点名具体对象」的前提：不喂它们，模型写不出"哪个园区高等级多""最慢的是
# 哪个部门"，只能给放之四海而皆准的空话（用户实测反馈过）。列名不同（level/breakDownLevel）
# 用**传入的 Series** 抹平，专业编码表由调用方传入（`SPECIALTY_LABEL`）。

def _park_level_extras(df: pd.DataFrame, lv_series: pd.Series,
                       labels: dict | None = None) -> list[dict]:
  """各园区级别构成**与占比**：图表解析要写"哪个园区高等级占比高"。

  占比由平台算好（解读层禁四则运算）—— 否则模型会自己除（实测写出"4 级占比 83.3%"是它自算的）。
  `labels` 把级别码换成业务名（变更单是低/中/高风险）；不传仍用「N级」。
  """
  if 'companyName' not in df.columns or len(df) == 0:
    return []
  lvs = sorted(int(v) for v in lv_series.dropna().unique())
  out = []
  for name, _ in df['companyName'].value_counts().items():
    sub = lv_series[df['companyName'] == name]
    total = int(sub.notna().sum())
    row: dict = {'园区': name, '合计': total}
    for lv in lvs:
      n = int((sub == lv).sum())
      label = labels.get(lv, f'{lv}级') if labels else f'{lv}级'
      row[label] = n
      row[f'{label}占比'] = _pct(n, total)
    out.append(row)
  return out

def _park_duration_extras(df: pd.DataFrame, minutes: list) -> list[dict]:
  """各园区时长（样本≥3 的园区、按 P90 降序 Top8）：第 7 章的图说明要写"园区差异"。"""
  if 'companyName' not in df.columns:
    return []
  pw = pd.DataFrame({'园区': df['companyName'], 'minutes': minutes}).dropna(subset=['园区'])
  if not len(pw):
    return []
  g = pw.groupby('园区')['minutes']
  agg = pd.DataFrame({'单数': g.count(), 'P50分钟': g.quantile(.5).round(1),
                      'P90分钟': g.quantile(.9).round(1)}).reset_index()
  agg = agg[agg['单数'] >= 3]          # 样本太少的园区不参与排名
  return [{'园区': r['园区'], '单数': int(r['单数']), 'P50分钟': float(r['P50分钟']),
           'P90分钟': float(r['P90分钟'])}
          for _, r in agg.sort_values(['P90分钟', '园区'], ascending=[False, True])
          .head(8).iterrows()]

def _timeout_tiers(df: pd.DataFrame, minutes: list, specialty_label: dict) -> list[dict]:
  """>{1,3,7} 天各多少单，每档按部门 Top3 与专业分解（供行动计划点名具体对象）。"""
  tier = pd.DataFrame({'minutes': minutes})
  if 'dc' in df.columns:
    tier['dc'] = df['dc'].values
  if 'specialty' in df.columns:
    tier['specialty'] = df['specialty'].values
  out = []
  for label, th in (('>1 天', 1440), ('>3 天', 4320), ('>7 天', 10080)):
    sub = tier[tier['minutes'] > th]
    if not len(sub):
      continue
    row = {'阈值': label, '单数': int(len(sub))}
    if 'dc' in sub.columns:
      vc = sub['dc'].value_counts()
      row['按部门Top3'] = [{'部门': str(k), '单数': int(v)} for k, v in vc.head(3).items()]
    if 'specialty' in sub.columns:
      vc = sub['specialty'].value_counts()
      row['按专业'] = [{'专业': specialty_label.get(int(k), str(k)), '单数': int(v)}
                     for k, v in vc.items()]
    out.append(row)
  return out

def _mom_attribution(df_cur, df_prev, specialty_label: dict, top: int = 5) -> list[dict]:
  """本期/上一期按「空间 × 专业」求差，取 |Δ| 最大若干项（"少的那些是谁贡献的"）。

  ⚠️ 单园区查询的列表**不带 `companyName`**（该字段只有全国聚合时才补），此时改用
  **部门（`dc`）× 专业**（"哪个部门变化最大"）—— 否则只剩专业一个键，`groupby` 的索引
  退化成标量，`k[-1]` 直接 TypeError（2026-10-16 真实数据实测崩溃）。
  """
  if 'companyName' in df_cur.columns and 'companyName' in df_prev.columns:
    space = 'companyName'
  elif 'dc' in df_cur.columns and 'dc' in df_prev.columns:
    space = 'dc'
  else:
    space = ''
  keys = ([space] if space else []) + [
    c for c in ('specialty',) if c in df_cur.columns and c in df_prev.columns]
  if not keys:
    return []
  idx = df_cur.groupby(keys).size().index.union(df_prev.groupby(keys).size().index)
  cur_g = df_cur.groupby(keys).size().reindex(idx, fill_value=0)
  prev_g = df_prev.groupby(keys).size().reindex(idx, fill_value=0)
  delta = cur_g - prev_g
  order = delta.abs().sort_values(ascending=False).index[:top]
  out = []
  for k in order:
    vals = k if isinstance(k, tuple) else (k,)
    row = {'园区': '', '部门': '', '专业': '',
           '本期': int(cur_g[k]), '上一期': int(prev_g[k]), '增减': int(delta[k])}
    for name, v in zip(keys, vals):
      if name == 'companyName':
        row['园区'] = str(v)
      elif name == 'dc':
        row['部门'] = str(v)
      else:
        try:
          row['专业'] = specialty_label.get(int(v), str(v))
        except (TypeError, ValueError):
          row['专业'] = str(v)
    out.append({k2: v2 for k2, v2 in row.items() if v2 != ''})
  return out

def _mom_top(stats: dict, dims, top: int = 5) -> list[dict]:
  """月报专属「环比变化 TOP」：跨维度挑 |本期 − 上一期| 最大的若干项。

  只吃**已带 `count`/`prev` 的具名计数块**（`_add_compare` 补过 `delta_count`/`delta_pp`）：
  `prev is None`（无可比数据）与双零项跳过，`prev=0` 的新增项保留。各维度混排后按变化量
  取前 N，回答"这个月到底是谁在变"。数字全部平台算好，模型只照抄。
  """
  candidates: list[dict] = []
  for dim_label, key in dims:
    block = stats.get(key)
    rows = block.get('rows') if isinstance(block, dict) else block
    for row in rows or []:
      if not isinstance(row, dict):
        continue
      name = row.get('label') or row.get('name')
      if not name:
        continue
      prev = row.get('prev')
      if prev is None:
        continue
      cur_n, prev_n = int(row.get('count') or 0), int(prev)
      if cur_n == 0 and prev_n == 0:
        continue
      delta = row.get('delta_count')
      item = {'维度': dim_label, '项目': str(name), '本期': cur_n, '上一期': prev_n,
              '增减': int(delta) if delta is not None else cur_n - prev_n}
      if row.get('delta_pp') is not None:
        item['占比变化'] = row['delta_pp']
      candidates.append(item)
  candidates.sort(key=lambda r: (-abs(r['增减']), r['维度'], r['项目']))
  return candidates[:top]


def _peak_day_extras(df: pd.DataFrame, cur_days: pd.Series, day_counts: pd.Series,
                     total: int, specialty_label: dict) -> dict:
  """峰值日构成：那一天的专业/园区分布（与日均对比）。"""
  if not len(day_counts) or 'occurrenceTime' not in df.columns:
    return {}
  peak = str(day_counts.idxmax())
  sub = df[cur_days == peak]
  row: dict = {'日期': peak, '单数': int(len(sub)),
               '日均': round(total / max(len(day_counts), 1), 1)}
  if 'specialty' in sub.columns:
    vc = sub['specialty'].value_counts()
    row['按专业'] = [{'专业': specialty_label.get(int(k), str(k)), '单数': int(v)}
                   for k, v in vc.head(4).items()]
  if 'companyName' in sub.columns:
    vc = sub['companyName'].value_counts()
    row['按园区Top3'] = [{'园区': str(k), '单数': int(v)} for k, v in vc.head(3).items()]
  return row

def _repeat_concentration(repeat: list[dict]) -> list[dict]:
  """重复 Top5 的"集中机房"（同一机房反复 vs 多机房各一次）。"""
  return [{'问题': r['name'][:40], '次数': r['count'], '涉及机房数': len(r['dcs']),
           '最集中机房': r['集中机房'], '该机房单数': r['集中机房单数']}
          for r in (repeat or [])[:5]]


# ==================== 绘图（两类共用；图表上不加数据标签） ====================

def _level_palette(labels: list, palette: list) -> list:
  """级别标签（升序）→ 从给定色板**等跨度**取 len(labels) 个色，跨期/跨园区稳定。

  级别数少于色板时颜色跨度更大（如 3 级取首/中/末 → 蓝/橙/红），相邻段不易同色难辨。
  """
  n = len(palette)
  k = max(len(labels), 1)
  if k <= 1 or n <= 1:
    return list(palette[:1]) or list(palette)
  idx = [round(i * (n - 1) / (k - 1)) for i in range(k)]
  return [palette[j] for j in idx]

def _bar_width(n: int, series: int = 1) -> float:
  """单根柱宽（占一个类别间距的比例）：**类别越少越窄**。

  matplotlib 默认 0.8，而图宽固定 9.6in、A4 满宽排版 —— 类别只有 3~4 个时一根柱就占
  36mm，整张图变成一排大色块（用户实测反馈"事件级别分布和专业这种类型少的，柱形特别宽"）。
  这条曲线让 3~4 类 ≈0.35、10 类以上 ≈0.6，中间平滑过渡；多序列时整组按同一密度分布，
  组内再留 12% 的缝，两根柱不会贴死。
  """
  group = min(0.62, max(0.30, 0.24 + 0.036 * max(int(n), 1)))
  return round(group if series <= 1 else group / series * 0.88, 3)


def _bar_offsets(n: int, series: int = 1) -> list[float]:
  """同组各根柱相对类别中心的横坐标偏移（居中、对称）；单序列返回 `[0.0]`。"""
  if series <= 1:
    return [0.0]
  w = _bar_width(n, series)
  span = w * series
  return [round(-span / 2 + w / 2 + i * w, 3) for i in range(series)]


def _legend_below(ax, ncol: int = 4, y: float | None = None,
                  handles=None, labels=None) -> None:
  """图例放**图下方居中**（对齐参考件；默认的 'best' 会飘到右上角压住数据）。

  `y` 缺省时按 x 刻度标签的旋转角自动下移：`03_park` 的公司名斜着写，图例停在 −0.16
  正好压在上面（用户截图可见）。无旋转仍用 −0.16，与旧输出逐字一致。

  `handles`/`labels` 给双轴图用（帕累托的柱子与累计折线在两个 Axes 上）：传合并后的
  句柄即可，位置仍按本轴刻度旋转自动下移。
  """
  if y is None:
    ticks = ax.get_xticklabels()
    rot = abs(float(ticks[0].get_rotation())) if ticks else 0.0
    y = -0.16 - (0.14 if rot >= 10 else 0.0) - (0.06 if rot >= 30 else 0.0)
  kwargs = {'loc': 'upper center', 'bbox_to_anchor': (0.5, y), 'ncol': ncol,
            'frameon': False}
  if handles is None:            # `legend(None, None, …)` 会报 TypeError，只能不传参数
    ax.legend(**kwargs)
  else:
    ax.legend(handles, labels, **kwargs)

def _emit(fig, name: str, plt, rz_save) -> None:
  """存图后立刻关闭 figure（同一进程要连画 8 张，别攒着占内存）。"""
  rz_save(fig, name)
  plt.close(fig)


def _draw_rhythm(rhythm, name: str, plt, rz_save) -> None:
  """月报「月内周节奏」分组柱：x=第1..N周，两序列=工作日/周末**日均**。

  画日均不画单量：首末周天数不足 7 天，画单量等于把长短不一的周放进同一根尺子。
  图上不加任何文字/数字（硬规则），均值轴也不强制整数刻度。
  """
  weeks = (rhythm or {}).get('weeks') or []
  if not weeks:
    return
  colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
  xs = list(range(len(weeks)))
  w = _bar_width(len(xs), 2)
  left_off, right_off = _bar_offsets(len(xs), 2)
  fig, ax = plt.subplots()
  ax.bar([x + left_off for x in xs], [w2['work_avg'] for w2 in weeks], width=w,
         label='工作日日均', color=colors[0])
  ax.bar([x + right_off for x in xs], [w2['rest_avg'] for w2 in weeks], width=w,
         label='周末日均', color=colors[1])
  ax.set_xticks(xs)
  ax.set_xticklabels([wk['label'] for wk in weeks])
  ax.set_xlabel('月内周次'), ax.set_ylabel('日均单量')
  ax.grid(True, axis='y')
  _legend_below(ax)
  _emit(fig, name, plt, rz_save)


# ==================== 综合评分（雷达图）：五类报告共用 ====================
# 把分散在各章的率/时长折成 3~6 个业务维度的 0~100 分，一眼看出强项/短板与环比。
# **全部确定性**：评分规则写进各 spec 的「数据说明」，维度含义由各 compute 逐维给出。

def _clamp_score(value) -> float:
  return max(0.0, min(100.0, float(value)))


def _round_score(value) -> float:
  """分值取整：整数不带小数（100 / 85），其余保留一位（81.8）。"""
  value = round(_clamp_score(value), 1)
  return int(value) if float(value).is_integer() else value


def _scale_score(total, prev_total) -> float:
  """规模控制：单量增长按增幅扣分，持平或下降满分；无上期基数不扣分。"""
  if not prev_total:
    return 100.0
  growth = (int(total) - int(prev_total)) / float(prev_total) * 100
  return 100.0 - max(0.0, growth)


def _volume_verdict(total, prev_total, unit: str = '起') -> dict | None:
  """单量方向判据（确定性）：下降 / 上升 / 持平 + 口径说明。

  为什么要有：解读层只拿到「环比 −4」这类**无极性**差值时，会按运维报告的先验把它当
  异常去解释（2026-10-27 实测：26/51 份报告把单量下降写成「疑似漏报/采集失效」，而同页
  「规模控制」评分是 100）。这里把平台**已有**的口径直接告诉模型——与 `_scale_score`
  同源（增长扣分、持平或下降满分），**不新增判断标准**。

  无上期基数（或基数 ≤ 0）返回 `None`：没有对比就不评判，避免"无对比也说改善"。
  """
  try:
    cur, prev = int(total), int(prev_total)
  except (TypeError, ValueError):
    return None
  if prev <= 0:
    return None
  delta = cur - prev
  if delta < 0:
    direction, word = '下降', '减少'
  elif delta > 0:
    direction, word = '上升', '增加'
  else:
    direction, word = '持平', '持平'
  detail = (f'本期 {cur} {unit}，与上一期 {prev} {unit} 持平' if delta == 0
            else f'本期 {cur} {unit}，较上一期 {prev} {unit}{word} {abs(delta)} {unit}')
  return {
    '方向': direction,
    '说明': detail,
    '口径': ('单量下降为正向（与第 2 章综合评分「规模控制」同口径：增长扣分、持平或下降满分）；'
             '增长如实写「规模扩大」，不写成「恶化」'),
  }


def _rate_score(rate) -> float:
  """率越低越好：100 − rate；字段拿不到（None）记 0 分。"""
  return 100.0 - float(rate) if rate is not None else 0.0


def _band_score(value, bands) -> float:
  """分档给分：bands=((上限, 分数), …) 升序；超过最后一档取该档分数，None → 0。"""
  if value is None:
    return 0.0
  for cut, score in bands:
    if float(value) <= cut:
      return float(score)
  return float(bands[-1][1]) if bands else 0.0


def _build_scorecard(items) -> dict:
  """[(维度, 含义, 本期分, 上一期分)] → 渲染与绘图共用的 scorecard 结构。"""
  rows, cur, prev = [], [], []
  for dim, meaning, cur_score, prev_score in items:
    c, p = _round_score(cur_score), _round_score(prev_score)
    rows.append({'维度': dim, '含义': meaning, '本期': c, '上一期': p})
    cur.append(c)
    prev.append(p)
  return {'rows': rows, 'cur': cur, 'prev': prev}


def _draw_scorecard(sc, name, plt, rz_save) -> None:
  """雷达图：上一期虚线 + 本期实线；环线不标数字（图上不加数值标签的硬规则）。"""
  rows = (sc or {}).get('rows') or []
  if len(rows) < 3:
    return
  from math import pi
  colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
  cur_c, prev_c = colors[0], colors[1]
  labels = [r['维度'] for r in rows]
  n = len(labels)
  angles = [i * 2 * pi / n for i in range(n)] + [0]
  cur_vals = [float(v) for v in sc['cur']] + [float(sc['cur'][0])]
  prev_vals = [float(v) for v in sc['prev']] + [float(sc['prev'][0])]
  fig, ax = plt.subplots(figsize=(5.4, 4.8), subplot_kw={'polar': True})
  ax.set_theta_offset(pi / 2)
  ax.set_theta_direction(-1)
  ax.set_xticks(angles[:-1])
  ax.set_xticklabels(labels)
  ax.set_ylim(0, 100)
  ax.set_yticks([20, 40, 60, 80, 100])
  ax.set_yticklabels([])              # 环线不标数字：图上不加任何数值标签（硬规则）
  ax.grid(color='#dfe8f2')
  ax.plot(angles, prev_vals, color=prev_c, linewidth=1.6, linestyle='--',
          label='上一期')
  ax.fill(angles, prev_vals, color=prev_c, alpha=0.10)
  ax.plot(angles, cur_vals, color=cur_c, linewidth=2.0, label='本期')
  ax.fill(angles, cur_vals, color=cur_c, alpha=0.18)
  _legend_below(ax)
  _emit(fig, name, plt, rz_save)


def _dumbbell(ax, labels: list, cur: list, prev: list, colors: list,
              cur_label: str = '本期', prev_label: str = '上一期') -> None:
  """横向哑铃图：同一对象的上一期 → 本期两点用细线连接。

  分组柱在 5~7 类时柱体互相挤压、读者先数柱再看差；哑铃图把注意力放在**变化方向与
  幅度**上。**不写数值标签**（用户硬规则）。
  """
  if not labels:
    return
  ys = list(range(len(labels)))[::-1]
  cur, prev = list(cur)[:len(labels)], list(prev)[:len(labels)]
  ax.set_yticks(ys)
  ax.set_yticklabels(labels)
  for y, p, c in zip(ys, prev, cur):
    ax.plot([p, c], [y, y], color='#c9d3df', linewidth=2.2, zorder=1,
            solid_capstyle='round')
  ax.scatter(prev, ys, s=46, facecolors='white', edgecolors='#8a97a6',
             linewidths=1.5, zorder=2, label=prev_label)
  ax.scatter(cur, ys, s=56, color=colors[0], zorder=3, label=cur_label)
  ax.set_xlabel('工单量')
  ax.set_xlim(0, max([*cur, *prev, 1]) * 1.15)
  ax.grid(True, axis='x')
  ax.grid(False, axis='y')
  ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.16), ncol=2,
            frameon=False)


def _donut(ax, labels: list, values: list, colors: list) -> None:
  """环形图（构成对比）：楔形上不写数值，类别靠图例（图例是名称，不是数据标签）。"""
  vals = [float(v) for v in values]
  if not vals or sum(vals) <= 0:
    return
  wedges, _ = ax.pie(
    vals, startangle=90, counterclock=False,
    colors=[colors[i % len(colors)] for i in range(len(vals))],
    wedgeprops={'width': 0.42, 'edgecolor': 'white', 'linewidth': 1.2})
  ax.set_aspect('equal')
  ax.set_xticks([])
  ax.set_yticks([])
  ax.grid(False)
  ax.legend(wedges, labels, loc='upper center', bbox_to_anchor=(0.5, -0.02),
            ncol=min(max(len(labels), 1), 4), frameon=False)


def _dots_rate(ax, labels: list, values: list, colors: list) -> None:
  """0–100% 点线图：横向从 0 拉到各对象的比率，100% 画参考线。

  比率用柱形画时"离 100% 还差多少"要靠读者自己比对坐标轴；点线 + 参考线直接读差距。
  **不写数值标签**。
  """
  if not labels:
    return
  ys = list(range(len(labels)))[::-1]
  vals = [0.0 if v is None else float(v) for v in values]
  color = (colors or ['#134b70'])[0]
  ax.set_yticks(ys)
  ax.set_yticklabels(labels)
  ax.hlines(ys, 0, vals, color=color, linewidth=3.0, alpha=0.35)
  ax.scatter(vals, ys, s=52, color=color, zorder=3)
  ax.axvline(100, color='#8a97a6', linewidth=1, linestyle='--', zorder=1)
  ax.set_xlim(0, 100)
  ax.set_xlabel('按时率（%）')
  ax.grid(True, axis='x')
  ax.grid(False, axis='y')
  ax.spines['left'].set_visible(False)
  ax.tick_params(axis='y', length=0)


# 时长档顺序色（快→慢）：深蓝 → 中蓝 → 浅蓝 → 绿 → 橙 → 红，与 `event_compute._LEVEL_DUR_LABELS`
# 同序取色。色值全部取自 `core/sandbox/report_style.py` 的既有色板（沙箱不能 import 该模块，
# 故以字面量给出；改色请同步那边，别另发明）。**只服务验收时长**（第 4 章月报的 6 档联合图）。
_TIME_LADDER = ('#134b70', '#2d7fb9', '#7ba7cf', '#2a9d6e', '#d98c2a', '#CB2920')

# 影响时长 5 档的顺序色（快→慢）：蓝 → 浅蓝 → 近白 → 橙 → 红，**由用户 2026-10-29 指定**
# （AntV G2「条形图-diverging」示例的全五色，含此前因近白被判不可见的 `#D9F0F6`）。
# 档数必须与 `impact._IMPACT_LABELS` 一致：一档一色，否则 `_stacked_barh` 会循环取色、
# 最慢一档反而拿到最冷色。自检 `test_report_spec` 钉住这条。
_IMPACT_LADDER = ('#3561A7', '#80B2D3', '#D9F0F6', '#EC7743', '#CB2920')


def _stacked_barh(ax, rows: list, cols: list, matrix: list, colors: list,
                  ncol: int = 4) -> None:
  """横向堆叠条：`rows[i]` 为纵轴类别，`cols[j]` 为堆叠段，`matrix[i][j]` 为计数。

  `ncol` 是图例列数：段数比默认的 4 多时（如时长 6 档）传段数本身，让图例排成一行；
  否则折成 4+2 两行，与 x 轴标签挤在一起。
  """
  if not rows or not cols:
    return
  ys = list(range(len(rows)))[::-1]
  left = [0] * len(rows)
  height = _bar_width(len(rows))
  for j, col in enumerate(cols):
    vals = [matrix[i][j] if i < len(matrix) and j < len(matrix[i]) else 0
            for i in range(len(rows))]
    ax.barh(ys, vals, left=left, height=height, color=colors[j % len(colors)],
            edgecolor='white', linewidth=0.8, label=col)
    left = [a + b for a, b in zip(left, vals)]
  ax.set_yticks(ys)
  ax.set_yticklabels(rows)
  ax.set_xlabel('工单量')
  ax.grid(True, axis='x')
  ax.grid(False, axis='y')
  _int_ticks(ax, 'x')
  _legend_below(ax, ncol=ncol)


def _matrix_counts(row_vals, col_vals, row_labels: list,
                   col_labels: list) -> list[list[int]]:
  """两个分类序列 → 按给定标签顺序的计数矩阵；不在标签集内的组合忽略（缺省为 0）。"""
  ri = {label: i for i, label in enumerate(row_labels)}
  ci = {label: j for j, label in enumerate(col_labels)}
  matrix = [[0] * len(col_labels) for _ in row_labels]
  for r, c in zip(row_vals, col_vals):
    i, j = ri.get(r), ci.get(c)
    if i is not None and j is not None:
      matrix[i][j] += 1
  return matrix

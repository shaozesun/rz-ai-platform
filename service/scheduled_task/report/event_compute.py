"""事件工单报告的固定统计与绘图 —— **全确定性，0 次 LLM**。

两种用法（沿用 `core/sandbox/report_style.py` 已确立的模式）：

1. **沙箱子进程**：workflow 读本文件源码 + 追加 `main(rz_setup(), rz_save)` 注入执行
   （沙箱是 `python -I`，不能 import 项目模块，只能注入源码）。
2. **本进程 import**：`compute_stats` 是纯 pandas，可直接对真实 `data.csv` 做**数字断言** ——
   这是"固定口径"相较于"让模型算"最大的好处。

因此本模块**顶层只能有 import 与函数定义**，不能有副作用；也不要直接引用沙箱预置的
`rz_setup`/`rz_save`（那会让 pyflakes 报 undefined name，也让它无法在本进程 import）——
它们是**参数**，由调用方传入。
"""

import json
from pathlib import Path

import pandas as pd

# 两类报告共用的纯助手（口径/写法只有一份）。**本进程 import 走这条**；沙箱里源码已被
# 拼接注入到同一命名空间，这条 import 会失败并被忽略 —— 所以是 try/except 而不是普通 import。
try:
  from service.scheduled_task.report.common import (
    CHART_MIN_ROWS, _add_compare, _all_ticket_nos, _bar_offsets, _bar_width,
    _build_scorecard, _counts_by_label,
    _date_axis, _day_str, _delta_text, _draw_rhythm, _draw_scorecard, _emit,
    _human_minutes, _int_ticks, _legend_below, _matrix_counts, _month_rhythm,
    _mom_attribution, _mom_top, _park_duration_extras, _park_level_extras, _parse_accept,
    _pct, _pct_text, _peak_day_extras, _rate_score, _repeat_concentration,
    _repeat_problems, _scale_score, _stacked_barh, _TIME_LADDER, _timeout_tiers,
    _top_card, _window_trend,
  )
except ImportError:      # 沙箱：名字已随 common 源码注入，无需 import
  pass

# 专业编码（依据 docs/dcim/工单列表接口字段映射确认清单.md 与 ticket_codes.py）
SPECIALTY_LABEL = {1: '供配电', 2: '制冷', 4: '智能化', 8: '其他'}

# 验收时长分箱（分钟）：**固定边界**，跨期可比；最后一个区间吃掉所有上溢
_ACCEPT_BINS = [0, 30, 60, 120, 240, 480, 720, 1440, 2880, 4320, 10 ** 9]
_ACCEPT_BIN_LABELS = ['<30分', '30-60分', '1-2时', '2-4时', '4-8时',
                      '8-12时', '12-24时', '1-2天', '2-3天', '>3天']

# 级别 × 验收时长的联合分布用**粗档**（6 档）：与上面 10 档总分布互补 —— 联合表要一眼看出
# 「高等级单多落在慢档」还是「各档均匀」。档位按**实测分布**定：一周 165 单里 156 单的验收
# 时长在 8 小时以上（集中在 8~24 时与 1~3 天）—— 若按「≤30分…>8时」五档切，这些全挤进最后
# 一档，联合矩阵退化成一行，看不出结构，故把 8 小时以上再拆成三段。
_LEVEL_DUR_BINS = [0, 30, 120, 480, 1440, 4320, 10 ** 9]
_LEVEL_DUR_LABELS = ['≤30分', '30分-2时', '2-8时', '8-24时', '1-3天', '>3天']

# 热力图最多展示的机房数（机房 17 个，全画会挤成一团）
_HEATMAP_MAX_DC = 12

# 综合评分维度（0~100）：事件单关注"量、快、准、不重复"
_SCORE_DIMS = (
  ('规模控制', '单量较上一期增长按增幅扣分，持平或下降满分（无上期基数不扣分）。'),
  ('响应时效', '验收未超时的事件单占比（100 − 验收超时率）。'),
  ('处置质量', '未被平台驳回的事件单占比。'),
  ('重复管控', '未落在重复问题簇中的事件单占比。'),
)


def _score_inputs(df: pd.DataFrame) -> dict:
  """评分所需的单期指标（本期/上一期用同一套算法，保证两条线可直接比）。"""
  total = int(len(df)) if df is not None else 0
  _, timeouts = _parse_accept(df)
  valid = [t for t in timeouts if t is not None]
  timeout_rate = _pct(sum(1 for t in valid if t), len(valid)) if valid else None
  reject_n = int((df['status'] == 2).sum()) if 'status' in df.columns else 0
  repeat_n = sum(r['count'] for r in _repeat_problems(df))
  return {'total': total, 'timeout_rate': timeout_rate,
          'reject_rate': _pct(reject_n, total), 'repeat_rate': _pct(repeat_n, total)}


def _scores(metrics: dict, prev_total: int) -> dict:
  return {
    '规模控制': _scale_score(metrics['total'], prev_total),
    '响应时效': _rate_score(metrics['timeout_rate']),
    '处置质量': 100.0 - metrics['reject_rate'],
    '重复管控': 100.0 - metrics['repeat_rate'],
  }


def _level_dur_bin(minutes: float) -> str:
  """验收分钟数 → 联合分析时长档（区间左闭右开，与 `_LEVEL_DUR_BINS` 同序）。"""
  for i in range(len(_LEVEL_DUR_LABELS) - 1):
    if minutes < _LEVEL_DUR_BINS[i + 1]:
      return _LEVEL_DUR_LABELS[i]
  return _LEVEL_DUR_LABELS[-1]







def compute_stats(df_cur: pd.DataFrame, df_prev: pd.DataFrame, window: dict) -> dict:
  """本期/上一期原始记录 → 报告需要的全部统计数字（只出原生类型，可 json.dump）。

  Args:
    df_cur: 本期记录（来自 `mgmt.query_event_ticket`）
    df_prev: 上一期记录（同能力，用于环比；可为空 DataFrame）
    window: {'cur': (start, end), 'prev': (start, end)}，均为 ISO 日期串
  """
  total = int(len(df_cur))
  prev_total = int(len(df_prev))
  cur_days = _day_str(df_cur['occurrenceTime']) if 'occurrenceTime' in df_cur else pd.Series([])
  day_counts = cur_days.value_counts().sort_index()

  # ---- 环比 ----
  mom = {'prev_total': prev_total, 'delta': total - prev_total,
         'pct': _pct(total - prev_total, prev_total) if prev_total else None}

  # ---- 日趋势（**窗口完整日期轴**；上一期按位置对位，缺位记 ''）----
  # 只画"有单的日期"会让两条线各自从自己的第一天开始画：实测本期 6 天、上期 7 天时
  # 整条对比线错位，事实化后「日期 + 上一期数值」也跟着错配。
  trend = _window_trend(df_cur, df_prev, 'occurrenceTime', window)
  day_count = len(trend['labels'])

  # ---- 级别 / 专业 ----
  level = _counts_by_label(df_cur['level']) if 'level' in df_cur else []
  specialty = (_counts_by_label(df_cur['specialty'], SPECIALTY_LABEL)
               if 'specialty' in df_cur else [])
  prev_specialty = {}
  if 'specialty' in df_prev:
    prev_specialty = {SPECIALTY_LABEL.get(k, str(k)): int(v)
                      for k, v in df_prev['specialty'].value_counts().items()}
  for row in specialty:
    row['prev'] = prev_specialty.get(row['label'])
  # 上一期占比的分母＝上一期同一列的非空计数（与本期 `_counts_by_label` 的口径一致）
  _add_compare(specialty, int(df_prev['specialty'].notna().sum())
               if 'specialty' in df_prev else 0)

  # ---- 园区（含环比）----
  prev_park = {}
  if 'companyName' in df_prev:
    prev_park = {k: int(v) for k, v in df_prev['companyName'].value_counts().items()}
  park = []
  if 'companyName' in df_cur:
    for name, cnt in df_cur['companyName'].value_counts().items():
      n = int(cnt)
      park.append({'name': name, 'count': n, 'pct': _pct(n, total),
                   'prev': prev_park.get(name, 0)})
  _add_compare(park, prev_total)

  # ---- 园区 × 专业（堆叠柱）----
  park_specialty = {'parks': [p['name'] for p in park], 'specialties': [],
                    'matrix': [[] for _ in park]}
  if park:
    for code, name in sorted(SPECIALTY_LABEL.items()):
      col = []
      for p in park:
        sub = df_cur[df_cur['companyName'] == p['name']]
        col.append(int((sub['specialty'] == code).sum()) if 'specialty' in df_cur else 0)
      if sum(col) > 0:
        park_specialty['specialties'].append(name)
        for i, v in enumerate(col):
          park_specialty['matrix'][i].append(v)

  # ---- 验收时长（处理时效的核心口径）----
  minutes, timeouts = _parse_accept(df_cur)
  valid = [m for m in minutes if m is not None]
  to_valid = [t for t in timeouts if t is not None]
  accept = {'available': bool(valid), 'valid': len(valid), 'rows': total}
  if valid:
    s = pd.Series(valid)
    binned = pd.cut(s, bins=_ACCEPT_BINS, labels=_ACCEPT_BIN_LABELS,
                    include_lowest=True, right=False)
    hist_counts = binned.value_counts().reindex(_ACCEPT_BIN_LABELS, fill_value=0)
    accept.update({
      'p50': round(float(s.quantile(0.5)), 1),
      'p90': round(float(s.quantile(0.9)), 1),
      'p95': round(float(s.quantile(0.95)), 1),
      'min': round(float(s.min()), 1),
      'max': round(float(s.max()), 1),
      'mean': round(float(s.mean()), 1),
      # 可读表述**由平台算好**（渲染与解读共用）：模型拿不到换算任务，
      # 实测它自己换会把「P95=3937.9 分钟」写成"超过 65 分钟"（实为 65.6 小时）。
      'readable': {
        'p50': _human_minutes(float(s.quantile(0.5))),
        'p90': _human_minutes(float(s.quantile(0.9))),
        'p95': _human_minutes(float(s.quantile(0.95))),
        'mean': _human_minutes(float(s.mean())),
        'max': _human_minutes(float(s.max())),
      },
      'hist': {'labels': list(_ACCEPT_BIN_LABELS),
               'counts': [int(v) for v in hist_counts.tolist()]},
      'timeout_n': sum(1 for t in to_valid if t),
      'timeout_base': len(to_valid),
      'timeout_rate': (_pct(sum(1 for t in to_valid if t), len(to_valid))
                       if to_valid else None),
    })
    # 按级别的时长分布（箱线图）；级别标签与 level 分布保持一致
    level_of = (df_cur['level'] if 'level' in df_cur else pd.Series([None] * total))
    by_level: dict[str, list] = {}
    for lv, mins in zip(level_of.tolist(), minutes):
      if mins is None or lv is None or pd.isna(lv):
        continue
      by_level.setdefault(f'{int(lv)}级', []).append(round(mins, 2))
    accept['by_level'] = {k: by_level[k] for k in sorted(by_level)}
    # 紧凑分位（给模型的版本）：原始的 by_level 是 158 个分钟数，整份喂进去只烧 token
    accept['by_level_stat'] = {
      k: {'单数': len(v),
          'P50分钟': round(pd.Series(v).quantile(.5), 1),
          'P90分钟': round(pd.Series(v).quantile(.9), 1)}
      for k, v in accept['by_level'].items()
    }
  else:
    accept.update({'timeout_n': None, 'timeout_base': 0, 'timeout_rate': None,
                   'hist': {'labels': list(_ACCEPT_BIN_LABELS),
                            'counts': [0] * len(_ACCEPT_BIN_LABELS)},
                   'by_level': {}})

  # ---- 级别 × 验收时长（联合分布）----
  # 级别数字越小越严重（《事件等级表》3.2.2），单看级别分布看不出"高等级单是否处置更慢"；
  # 这里给的是**联合矩阵 + 各级分位/超时率**，判断留给解读层（平台不写等级评分）。
  level_duration = {'levels': [], 'bins': list(_LEVEL_DUR_LABELS), 'matrix': [],
                    'by_level': []}
  if valid and 'level' in df_cur:
    lv_labels = [row['label'] for row in level]     # 与第 4 章级别表同序，便于对照
    level_of = df_cur['level'].tolist()
    lv_vals: list[str] = []
    bin_vals: list[str] = []
    per_level: dict[str, dict] = {}
    for lv, mins, to in zip(level_of, minutes, timeouts):
      if mins is None or lv is None or pd.isna(lv):
        continue
      label = f'{int(lv)}级'
      if label not in lv_labels:
        continue
      lv_vals.append(label)
      bin_vals.append(_level_dur_bin(mins))
      stat = per_level.setdefault(label, {'单数': 0, '分钟': [], '超时单数': 0, '超时基数': 0})
      stat['单数'] += 1
      stat['分钟'].append(mins)
      if to is not None:
        stat['超时基数'] += 1
        stat['超时单数'] += int(bool(to))
    by_level_rows = []
    for label in lv_labels:
      stat = per_level.get(label)
      if not stat:
        continue
      s = pd.Series(stat['分钟'])
      by_level_rows.append({
        '级别': label, '单数': stat['单数'], '占比': _pct(stat['单数'], len(lv_vals)),
        'P50分钟': round(float(s.quantile(0.5)), 1),
        'P90分钟': round(float(s.quantile(0.9)), 1),
        '超时单数': stat['超时单数'],
        '超时率': _pct(stat['超时单数'], stat['超时基数']),
      })
    if lv_vals:
      # 等级段对比（1–2 级 vs 3–4 级）：**平台算好**，解读层不做减法/比大小
      bands = []
      for name, keep in (('1-2级', lambda k: k <= 2), ('3-4级', lambda k: k >= 3)):
        members = [lab for lab in lv_labels if lab in per_level and keep(int(lab[:-1]))]
        if not members:
          continue
        mins_all: list[float] = []
        to_n = to_base = 0
        for lab in members:
          item = per_level[lab]
          mins_all += item['分钟']
          to_n += item['超时单数']
          to_base += item['超时基数']
        s = pd.Series(mins_all)
        bands.append({
          '等级段': name, '级别': '、'.join(members), '单数': len(mins_all),
          '占比': _pct(len(mins_all), len(lv_vals)),
          'P50分钟': round(float(s.quantile(0.5)), 1),
          'P90分钟': round(float(s.quantile(0.9)), 1),
          '超时单数': to_n, '超时率': _pct(to_n, to_base),
        })
      level_duration = {
        'levels': lv_labels,
        'bins': list(_LEVEL_DUR_LABELS),
        'matrix': _matrix_counts(lv_vals, bin_vals, lv_labels, _LEVEL_DUR_LABELS),
        'by_level': by_level_rows,
        'bands': bands,
      }

  # ---- 机房 × 级别（热力图）----
  dc_level = {'dcs': [], 'levels': [], 'matrix': []}
  if 'dc' in df_cur and 'level' in df_cur and total:
    top_dcs = [str(k) for k in df_cur['dc'].value_counts().head(_HEATMAP_MAX_DC).index]
    lv_labels = [row['label'] for row in level]
    matrix = []
    for dc in top_dcs:
      sub = df_cur[df_cur['dc'] == dc]
      matrix.append([int((sub['level'] == int(lab[:-1])).sum()) for lab in lv_labels])
    dc_level = {'dcs': top_dcs, 'levels': lv_labels, 'matrix': matrix}

  # ---- 告警质量（只用有字段支撑的派生指标）----
  reject_n = int((df_cur['status'] == 2).sum()) if 'status' in df_cur else 0
  repeat = _repeat_problems(df_cur, str((window or {}).get('scope') or ''))
  repeat_n = sum(r['count'] for r in repeat)
  quality = {
    'timeout_rate': accept.get('timeout_rate'),
    'timeout_n': accept.get('timeout_n'),
    'timeout_base': accept.get('timeout_base') or 0,
    'reject_n': reject_n,
    'reject_rate': _pct(reject_n, total),
    'repeat_classes': len(repeat),
    'repeat_n': repeat_n,
    'repeat_rate': _pct(repeat_n, total),
  }

  # ---- 综合评分（雷达图）：本期与上一期同口径（规模控制按对方总量算增幅）----
  cur_m, prev_m = _score_inputs(df_cur), _score_inputs(df_prev)
  cur_scores = _scores(cur_m, prev_m['total'])
  prev_scores = _scores(prev_m, cur_m['total'])
  scorecard = _build_scorecard([
    (dim, meaning, cur_scores[dim], prev_scores[dim]) for dim, meaning in _SCORE_DIMS
  ])

  # ---- 供解读用的补充聚合（**喂聚合、不喂明细**：几 KB，但足以让它写"为什么"）----
  # 只给聚合数字时，模型写不出归因，只能给笼统判断（用户反馈"每章只有一两句空话"）。
  # 注意排序都要有确定性 tie-break，保持"同一份数据 → 同一份报告"。
  extras: dict = {}
  if valid:
    work = pd.DataFrame({
      'dc': df_cur['dc'] if 'dc' in df_cur.columns else None,
      'minutes': minutes,
      'ticketNo': df_cur['ticketNo'] if 'ticketNo' in df_cur.columns else None,
      'name': df_cur['projectName'] if 'projectName' in df_cur.columns else None,
    }).dropna(subset=['dc', 'minutes'])
    if len(work):
      def _slow(grp):
        return pd.Series({'单数': len(grp), 'P50分钟': round(grp['minutes'].quantile(.5), 1),
                          'P90分钟': round(grp['minutes'].quantile(.9), 1)})
      agg = work.groupby('dc').apply(_slow, include_groups=False).reset_index()
      agg = agg[agg['单数'] >= 3]           # 样本太少的部门不参与排名
      # 键名一律中文：这份 JSON 直接给模型看，英文键（dc/P90）有被抄进正文的风险
      extras['最慢部门'] = [
        {'部门': r['dc'], '单数': int(r['单数']), 'P50分钟': r['P50分钟'], 'P90分钟': r['P90分钟']}
        for _, r in agg.sort_values(['P90分钟', 'dc'], ascending=[False, True]).head(3).iterrows()
      ]
    longest = work.sort_values(['minutes', 'ticketNo'], ascending=[False, True]).head(3)
    extras['最长验收单'] = [
      {'部门': r['dc'], '分钟': round(float(r['minutes']), 1), '事件': str(r['name'])[:40]}
      for _, r in longest.iterrows()]

  # 园区环比变动（升/降各取最多，便于解读"哪些园区异常"）
  movers = [{'园区': p['name'], '本期': p['count'], '上一期': p['prev'],
             '环比': p['count'] - p['prev']} for p in park]
  movers.sort(key=lambda m: (-abs(m['环比']), m['园区']))
  extras['园区环比变动最大'] = movers[:4]

  # ---- 可归因切片（两类共用，实现见 common.py；本节只做装配）----
  # 这些切片是解读能"点名具体对象"的前提（第 4 章的园区级别构成、第 7 章的园区时长差异、
  # 行动计划的具体阈值档…），不喂它们模型只能写空话。
  if valid and 'companyName' in df_cur.columns:
    extras['各园区时长'] = _park_duration_extras(df_cur, minutes)
  if 'companyName' in df_cur.columns and 'level' in df_cur.columns and len(df_cur):
    extras['各园区级别构成'] = _park_level_extras(df_cur, df_cur['level'])
  if valid:
    tiers = _timeout_tiers(df_cur, minutes, SPECIALTY_LABEL)
    if tiers:
      extras['超时分档'] = tiers
  if prev_total:
    attribution = _mom_attribution(df_cur, df_prev, SPECIALTY_LABEL)
    if attribution:
      extras['环比归因'] = attribution
  peak_row = _peak_day_extras(df_cur, cur_days, day_counts, total, SPECIALTY_LABEL)
  if peak_row:
    extras['峰值日构成'] = peak_row
  if repeat:
    extras['重复问题集中度'] = _repeat_concentration(repeat)

  # ---- KPI 卡片 ----
  kpi = {
    'total': {'value': str(total), 'sub': f'上一期 {prev_total} 起'},
    'daily_avg': {'value': f'{round(total / max(day_count, 1), 1)}',
                  'sub': f'覆盖 {day_count} 天'},
    'mom': {'value': (_delta_text(mom["delta"]) if prev_total else '—'),
            'sub': (_pct_text(mom['pct'], signed=True)
                    if mom['pct'] is not None else '无上期基数')},
    # ⚠️ 必须**按计数取最大**，不能用位置下标：
    # `level` / `specialty` 是按**编码**排序的（1供配电/2制冷/4智能化、1~4级），
    # 那是为了让表格显示顺序稳定。之前用 level[-1] / specialty[0] 取"最多的那个"，
    # 结果卡片显示"最多级别 4级(49 起)""专业集中度 供配电(32 起)"，而实际最多的是
    # 3级 83 起 / 制冷 98 起 —— 卡片与正文自相矛盾（2026-10-15 用户当场指出，当天每版都错）。
    'top_level': _top_card(level, '最多级别'),
    'top_specialty': _top_card(specialty, '专业集中度'),
    'park_top2': ({'value': f'{sum(p["count"] for p in park[:2])}', 'sub':
                   f'{park[0]["name"][:6]} 等前 2 个园区'}
                  if len(park) >= 2 else {'value': '—', 'sub': '无数据'}),
    'accept_timeout': ({'value': f'{accept["timeout_rate"]}%',
                        'sub': f'{accept["timeout_n"]}/{accept["timeout_base"]} 单验收超时'}
                       if accept.get('timeout_rate') is not None
                       else {'value': '—', 'sub': '验收字段未回填'}),
    'repeat': {'value': str(len(repeat)), 'sub': f'涉及 {repeat_n} 单（{quality["repeat_rate"]}%）'},
  }

  out = {
    'window': window,
    'total': total, 'prev_total': prev_total,
    'daily_avg': round(total / max(day_count, 1), 1),
    'mom': mom, 'trend': trend, 'level': level, 'specialty': specialty,
    'park': park, 'park_specialty': park_specialty,
    'accept': accept, 'level_duration': level_duration, 'dc_level': dc_level,
    'quality': quality,
    'repeat': repeat, 'kpi': kpi, 'extras': extras, 'scorecard': scorecard,
    # 第 10 章要取详情的代表单（工作流按它去 fetch_row_details）：事件单＝按名称聚类的重复问题
    'detail_targets': [r['ticketNo'] for r in repeat if r.get('ticketNo')],
    # 周报逐单取详情用；月报返回空列表（唯一消费方只在周报读取，不影响月报输出）
    'detail_all': (_all_ticket_nos(df_cur)
                   if (window or {}).get('period') == 'weekly' else []),
    # 样本过少（一两条）：不出图、也不写图表解析 —— 章节仍以表格呈现
    'charts_suppressed': total < CHART_MIN_ROWS,
  }
  # 月报专属维度（周报**不写这两个键**，输出逐字不变）
  if (window or {}).get('period') == 'monthly':
    out['rhythm'] = _month_rhythm(trend, window, '起')
    out['mom_top'] = _mom_top(
      {'level': level, 'park': park, 'specialty': specialty},
      [('级别', 'level'), ('园区', 'park'), ('专业', 'specialty')])
  return out




def draw_charts(df_cur: pd.DataFrame, df_prev: pd.DataFrame, stats: dict, plt, rz_save,
                rz_heat=()) -> None:
  """画 8 张图到 charts/。`plt`/`rz_save`/`rz_heat` 都由调用方传入（沙箱预置 / 单测自备）。

  **沙箱预置的名字一律当参数传**，不在本文件里直接引用 —— 否则 pyflakes 会报
  `undefined name`（它看不到沙箱注入的全局），而项目把这类报错当真实缺陷处理。

  两条贯穿始终的规范：
  1. **不给图加数据标签**（数值标注、饼图 autopct、点标注）—— 所有数字走正文与表格。
  2. **克制用色**（照 DcOps 参考报告）：单序列图整张一个主色；只有"本期 vs 上一期"这类
     多序列对比才上第二/第三个颜色（绿/橙）；堆叠图按类别取色。
  """
  if stats.get('charts_suppressed'):
    return
  colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
  n = len(colors)
  # 图 0 综合评分（雷达：本期 vs 上一期；分数见第 2 章表格）
  _draw_scorecard(stats.get('scorecard'), '00_scorecard', plt, rz_save)
  # 配色照 DcOps 参考报告：**主体用主色深蓝，对比基准用绿/橙**（参考件里"本次演练"是深蓝，
  # "园区平均/全国平均"才是绿/橙）。上一轮我把本期画成绿色是反的，这里改回来。
  _MAIN, _CUR, _PREV, _ACCENT = colors[0], colors[0], colors[1], colors[3]

  # 图 1 日趋势（本期 vs 上一期）：**本期深蓝 / 上一期绿**（主体用主色，基准用副色）
  fig, ax = plt.subplots()
  ax.plot(range(len(stats['trend']['cur'])), stats['trend']['cur'], marker='o',
          label='本期', color=_CUR)
  ax.plot(range(len(stats['trend']['prev'])), stats['trend']['prev'], marker='o',
          linestyle='--', label='上一期', color=_PREV)
  _date_axis(ax, stats['trend']['labels'])
  ax.set_xlabel('日期'), ax.set_ylabel('工单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '01_trend', plt, rz_save)

  # 图 1b 月内周节奏（工作日 vs 周末 日均）：仅月报有数据，周报直接跳过
  if stats.get('rhythm'):
    _draw_rhythm(stats['rhythm'], '01b_month_rhythm', plt, rz_save)

  # 图 2 事件级别分布：**单序列统一主色**（一张图一个颜色是惯例，参考报告亦然；
  # 逐根换色会显得花）
  fig, ax = plt.subplots()
  ax.bar([r['label'] for r in stats['level']], [r['count'] for r in stats['level']],
         width=_bar_width(len(stats['level'])), color=_MAIN)
  ax.set_xlabel('事件级别'), ax.set_ylabel('工单量')
  _int_ticks(ax)
  _emit(fig, '02_level', plt, rz_save)

  # 图 2b 级别 × 验收时长（横向堆叠条）：行＝级别，段＝时长档。
  # 配色用**冷→暖的顺序色**（`_TIME_LADDER`）：原来用 `plt.cm.Blues` 同族深浅，6 档挤在一根
  # 条上根本分不开（用户 2026-10-28 实测反馈「区分度太低」）。同族色只适合两三个档的渐变；
  # 档位多时既要靠**色相**拉开，又要保持「快→慢」的方向感（蓝/青＝快，橙/红＝慢）。
  ld = stats.get('level_duration') or {}
  if ld.get('levels') and ld.get('bins') and any(any(r) for r in ld.get('matrix') or []):
    fig, ax = plt.subplots(figsize=(9.4, max(2.6, 0.52 * len(ld['levels']) + 1.6)))
    ladder = [_TIME_LADDER[j % len(_TIME_LADDER)] for j in range(len(ld['bins']))]
    _stacked_barh(ax, ld['levels'], ld['bins'], ld['matrix'], ladder,
                  ncol=len(ld['bins']))
    _emit(fig, '02b_level_duration', plt, rz_save)

  # 图 3 各园区（本期 vs 上一期）：本期深蓝 / 上一期绿，同图 1
  fig, ax = plt.subplots()
  names = [p['name'] for p in stats['park']]
  xs = range(len(names))
  width = _bar_width(len(names), 2)
  cur_off, prev_off = _bar_offsets(len(names), 2)
  ax.bar([x + cur_off for x in xs], [p['count'] for p in stats['park']], width=width,
         label='本期', color=_CUR)
  ax.bar([x + prev_off for x in xs], [p['prev'] for p in stats['park']], width=width,
         label='上一期', color=_PREV)
  ax.set_xticks(list(xs)), ax.set_xticklabels(names, rotation=15)
  ax.set_ylabel('工单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '03_park', plt, rz_save)

  # 图 4 各园区 × 专业（堆叠）：每个专业一色（同一专业在各园区同色）
  fig, ax = plt.subplots()
  ps = stats['park_specialty']
  bottom = [0] * len(ps['parks'])
  width = _bar_width(len(ps['parks']))
  for i, sp in enumerate(ps['specialties']):
    vals = [row[i] for row in ps['matrix']]
    ax.bar(ps['parks'], vals, bottom=bottom, width=width, label=sp,
           color=colors[i % n], edgecolor='white', linewidth=0.8)
    bottom = [b + v for b, v in zip(bottom, vals)]
  ax.set_xticklabels(ps['parks'], rotation=15)
  ax.set_ylabel('工单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '04_park_specialty', plt, rz_save)

  # 专业总量**不出图**（2026-10-29）：专业分布并入第 5 章，总量表就是上面堆叠图的列合计；
  # 原先这里的 `05_specialty` 环形图 spec 从未声明（孤儿图，白画），已删。`stats['specialty']`
  # 仍被园区章的表、`highlights`/`risks` 与事实层使用，不能删。

  # 图 5 验收时长区间分布（直方图）：区间是**有序**的 → 单色才对
  fig, ax = plt.subplots()
  hist = stats['accept']['hist']
  ax.bar(hist['labels'], hist['counts'], width=_bar_width(len(hist['labels'])),
         color=_MAIN)
  ax.set_xlabel('验收时长'), ax.set_ylabel('工单量')
  ax.set_xticklabels(hist['labels'], rotation=30, ha='right')
  _int_ticks(ax)
  _emit(fig, '06_accept_hist', plt, rz_save)

  # 图 6 机房 × 级别（热力图）：顺序色图 + colorbar（顺序色图不该用彩虹色）；
  # Blues 与新主色（深蓝）同族
  dl = stats['dc_level']
  if dl['dcs'] and dl['levels']:
    fig, ax = plt.subplots(figsize=(9.6, max(3.2, 0.42 * len(dl['dcs']) + 1.6)))
    from matplotlib.colors import LinearSegmentedColormap
    im = ax.imshow(dl['matrix'],
                   cmap=LinearSegmentedColormap.from_list('rz_heat', list(rz_heat)),
                   aspect='auto')
    ax.set_xticks(range(len(dl['levels']))), ax.set_xticklabels(dl['levels'])
    ax.set_yticks(range(len(dl['dcs']))), ax.set_yticklabels(dl['dcs'])
    ax.set_xlabel('事件级别'), ax.set_ylabel('机房（部门）')
    ax.grid(False)
    fig.colorbar(im, ax=ax, label='工单量')
    _emit(fig, '07_dc_level', plt, rz_save)

  # 图 7 验收时长分布（按事件级别）
  by_level = stats['accept'].get('by_level') or {}
  groups = [k for k in sorted(by_level) if by_level[k]]
  if groups:
    fig, ax = plt.subplots()
    # 统一浅蓝箱体 + 深蓝中位线：级别已由横轴标签区分，不必再逐组换色（参考报告的克制做法）
    ax.boxplot([by_level[k] for k in groups], patch_artist=True,
               boxprops={'facecolor': '#d6e6f4', 'edgecolor': _MAIN},
               medianprops={'color': _MAIN, 'linewidth': 1.8},
               whiskerprops={'color': _MAIN}, capprops={'color': _MAIN},
               flierprops={'markerfacecolor': 'none', 'markeredgecolor': _ACCENT,
                           'markersize': 5})
    ax.set_xticks(range(1, len(groups) + 1)), ax.set_xticklabels(groups)
    ax.set_xlabel('事件级别'), ax.set_ylabel('验收时长（分钟）')
    _emit(fig, '08_accept_box', plt, rz_save)


def main(plt, rz_save, rz_heat=()) -> None:
  """沙箱入口：读 cwd 下的 data.csv / prev.csv → 写 stats.json + charts/。"""
  cur = pd.read_csv('data.csv')
  prev_path = Path('prev.csv')
  prev = pd.read_csv(prev_path) if prev_path.is_file() else pd.DataFrame()
  window_path = Path('window.json')
  window = json.loads(window_path.read_text(encoding='utf-8')) if window_path.is_file() else {}
  stats = compute_stats(cur, prev, window)
  Path('stats.json').write_text(
    json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
  draw_charts(cur, prev, stats, plt, rz_save, rz_heat)
  # 只回一行摘要（完整 stats 走文件，不进沙箱 stdout）
  print(json.dumps({'rows_cur': stats['total'], 'rows_prev': stats['prev_total'],
                    'charts': len(list(Path('charts').glob('*.png')))}, ensure_ascii=False))

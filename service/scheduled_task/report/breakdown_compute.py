"""故障工单报告的固定统计与绘图 —— **全确定性，0 次 LLM**。

统计结构**刻意与 `event_compute` 同构**（同样的键：`trend/level/specialty/park/park_specialty/
accept/dc_level/quality/repeat/kpi/extras`），这样渲染层与解读契约只需要换"标签"，
不需要两套骨架 —— 报告格式统一正是用户的要求。

故障单与事件单**实测差异**（2026-10-15 用真实数据核对，127 行 × 29 列）：

- **`faultHandlingTime`**：故障处理时长（**毫秒**，119/127 有值；与 `faultHandlingDuration`
  的 `"10分38秒"` 吻合并换算一致）→ 这才是故障的"处理时效"口径，比事件单只能用的
  **验收时长**贴切得多。本节把它映射到与事件单同构的 `accept` 槽位。
- **`breakdownEquipmentType`**：故障设备类型（127/127 有值、**32 类且已是中文**：
  冷水机组/UPS蓄电池/冷却塔/CDU/视频监控系统…）→ 故障单最有价值的维度（直接指向设备治理），
  出**帕累托图**。
- `checkEndApprove.approveTime`（验收时长，112/127）**同时存在** → 与处理时长对比，
  回答"卡在处理还是卡在验收"。
- `deleteTime` 恒为 0（同事件单：**没有结束时间**）。
- 详情里另有 `equipmentBrand/equipmentModel`（品牌/型号）与 `replaceSparePartsVOS`（换件）——
  「故障根因与设备质量分析」章的详情依据，由 `tools/analysis_tools.py` 的详情白名单裁出来。
"""

import json
from pathlib import Path

import pandas as pd

# 共用纯助手：本进程 import 走这条；沙箱里源码已拼接注入（import 失败即忽略）
try:
  from service.scheduled_task.report.common import (
    CHART_MIN_ROWS, _add_compare, _all_ticket_nos, _bar_offsets, _bar_width,
    _build_scorecard, _counts_by_label,
    _date_axis, _day_str, _delta_text, _draw_rhythm, _draw_scorecard, _emit,
    _human_minutes, _int_ticks, _legend_below, _level_palette, _month_rhythm,
    _mom_attribution, _mom_top, _park_duration_extras, _park_level_extras,
    _parse_accept, _pct, _pct_text, _peak_day_extras, _rate_score,
    _repeat_concentration, _scale_score, _timeout_tiers, _top_card, _window_trend,
  )
except ImportError:      # 沙箱：名字已随 common 源码注入
  pass

# 专业编码（同 ticket_codes.py；故障单的 specialty 与事件单同一套）
SPECIALTY_LABEL = {1: '供配电', 2: '制冷', 4: '智能化', 8: '其他'}

# 处理时长分箱（分钟，固定边界 → 跨期可比）：故障通常比事件快得多，故粒度更细
_DUR_BINS = [0, 15, 30, 60, 120, 240, 480, 1440, 4320, 10 ** 9]
_DUR_BIN_LABELS = ['<15分', '15-30分', '30-60分', '1-2时', '2-4时',
                   '4-8时', '8-12时', '12-24时', '>1天']

# 热力图最多展示的机房数
_HEATMAP_MAX_DC = 12
# 帕累托图取前 N 类设备
_PARETO_TOP = 10


def _parse_ms(series: pd.Series) -> list:
  """毫秒数值列 → 分钟列表（非数值/<=0 记 None）。"""
  out = []
  for v in series:
    try:
      ms = float(v)
    except (TypeError, ValueError):
      out.append(None)
      continue
    out.append(ms / 60000.0 if ms > 0 else None)
  return out


# 综合评分维度（0~100）：故障单关注"量、处理快、闭环、不重复、高危不压手"
_SCORE_DIMS = (
  ('规模控制', '单量较上一期增长按增幅扣分，持平或下降满分（无上期基数不扣分）。'),
  ('处理时效', '故障处理时长不超过 24 小时的单占比。'),
  ('闭环质量', '故障关闭率与非驳回率加权（关闭率 60%，非驳回 40%）。'),
  ('重复故障管控', '未落在重复故障簇（同类设备同一地点）中的单占比。'),
  ('高危故障管控', '4 级及以上且未关闭的故障占比越低分越高。'),
)


def _score_inputs(df: pd.DataFrame, lv_col: str) -> dict:
  """评分所需的单期指标（本期/上一期同一套算法）。"""
  total = int(len(df)) if df is not None else 0
  minutes = _parse_ms(df['faultHandlingTime']) if 'faultHandlingTime' in df.columns \
      else [None] * total
  valid = [m for m in minutes if m is not None]
  repeat = _repeat_by_equipment(df) if total else []
  risk = _build_risk(df, minutes, lv_col, repeat) if total else {'high_open_n': 0}
  return {
    'total': total,
    'over_1d_rate': (_pct(sum(1 for m in valid if m > 1440), len(valid))
                     if valid else None),
    'closed_rate': _pct(int((df['stage'] == 5).sum()) if 'stage' in df.columns else 0,
                        total),
    'reject_rate': _pct(int((df['status'] == 2).sum()) if 'status' in df.columns else 0,
                        total),
    'repeat_rate': _pct(sum(r['count'] for r in repeat), total),
    'high_open_rate': _pct(int(risk.get('high_open_n') or 0), total),
  }


def _scores(metrics: dict, prev_total: int) -> dict:
  return {
    '规模控制': _scale_score(metrics['total'], prev_total),
    '处理时效': _rate_score(metrics['over_1d_rate']),
    '闭环质量': 0.6 * metrics['closed_rate'] + 0.4 * (100 - metrics['reject_rate']),
    '重复故障管控': 100.0 - metrics['repeat_rate'],
    '高危故障管控': 100.0 - metrics['high_open_rate'],
  }


def compute_stats(df_cur: pd.DataFrame, df_prev: pd.DataFrame, window: dict) -> dict:
  """本期/上一期故障单 → 报告需要的全部统计（键与 `event_compute` 同构）。"""
  total = int(len(df_cur))
  prev_total = int(len(df_prev))
  cur_days = _day_str(df_cur['occurrenceTime']) if 'occurrenceTime' in df_cur else pd.Series([])
  day_counts = cur_days.value_counts().sort_index()

  mom = {'prev_total': prev_total, 'delta': total - prev_total,
         'pct': _pct(total - prev_total, prev_total) if prev_total else None}

  # 窗口完整日期轴（本期 6 天有单、上期 7 天时不再错位）；上一期按位置对位
  trend = _window_trend(df_cur, df_prev, 'occurrenceTime', window)
  day_count = len(trend['labels'])

  # 故障级别（列表字段名是 breakDownLevel，不是 level）
  lv_col = 'breakDownLevel' if 'breakDownLevel' in df_cur.columns else 'level'
  level = _counts_by_label(df_cur[lv_col]) if lv_col in df_cur else []
  specialty = (_counts_by_label(df_cur['specialty'], SPECIALTY_LABEL)
               if 'specialty' in df_cur else [])
  prev_specialty = {}
  if 'specialty' in df_prev:
    prev_specialty = {SPECIALTY_LABEL.get(int(k), str(k)): int(v)
                      for k, v in df_prev['specialty'].value_counts().items()}
  for row in specialty:
    row['prev'] = prev_specialty.get(row['label'])
  # 上一期占比的分母＝上一期非空计数（与 `_counts_by_label` 的本期口径一致）
  _add_compare(specialty, int(df_prev['specialty'].notna().sum())
               if 'specialty' in df_prev else 0)

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

  # 园区 × 专业（堆叠）
  park_specialty = {'parks': [p['name'] for p in park], 'specialties': [],
                    'matrix': [[] for _ in park]}
  if park and 'specialty' in df_cur.columns:
    for code, name in sorted(SPECIALTY_LABEL.items()):
      col = [int((df_cur[(df_cur['companyName'] == p['name'])]['specialty'] == code).sum())
             for p in park]
      if sum(col) > 0:
        park_specialty['specialties'].append(name)
        for i, v in enumerate(col):
          park_specialty['matrix'][i].append(v)

  # 园区 × 故障级别（第 5 章横向堆叠条：每个园区一条横条、按级别堆叠）。
  # ⚠️ 只做**正向堆叠**、不做「diverging（正负分列）」：级别没有负向概念，且平台未定义
  # 级别数值的大小方向（代判"哪级更严重"是禁忌），故按级别编码升序取色，颜色只表示"第几级"。
  park_level = {'parks': [p['name'] for p in park], 'levels': [],
                'matrix': [[] for _ in park]}
  if park and lv_col in df_cur.columns:
    for row in level:                      # level 已按编码升序，且只含出现过的级别
      code = int(str(row['label'])[:-1])
      col = [int((df_cur[df_cur['companyName'] == p['name']][lv_col] == code).sum())
             for p in park]
      if sum(col) > 0:
        park_level['levels'].append(row['label'])
        for i, v in enumerate(col):
          park_level['matrix'][i].append(v)

  # 故障部门分布（按 `dc` 部门/分部，已解析成中文名）+ 环比增减（本期−上期）。
  # ⚠️ 不用 `occurrenceSite`：那是手写自由文本、**每单唯一**（实测 56 单 → 56 个地点、计数全为 1），
  # 做成 "Top10" 只会任取 10 个并列 1 的字符串，零信息量。部门才是能聚合的位置维度。
  dept = {'rows': [], 'classes': 0}
  if 'dc' in df_cur.columns:
    def _dc_counts(frame):
      if 'dc' not in frame.columns:
        return {}
      s = frame['dc'].astype(str).str.strip()
      s = s[~s.isin(['', 'nan', 'None'])]
      return {str(k): int(v) for k, v in s.value_counts().items()}
    cur_dc, prev_dc = _dc_counts(df_cur), _dc_counts(df_prev)
    # 键取并集：本期归零的部门（上期有、本期 0）也要出现，否则发散条形看不见"下降"
    keys = list(dict.fromkeys([*cur_dc.keys(), *prev_dc.keys()]))
    rows = [{'name': k, 'count': cur_dc.get(k, 0), 'prev': prev_dc.get(k, 0),
             'delta': cur_dc.get(k, 0) - prev_dc.get(k, 0),
             'pct': _pct(cur_dc.get(k, 0), total)}
            for k in keys]
    rows.sort(key=lambda r: (-r['count'], r['name']))
    _add_compare(rows, prev_total)
    dept = {'rows': rows, 'classes': len(rows)}

  # 设备类型（故障单标志性维度）：全部计数 + 帕累托前 N
  equip = (df_cur['breakdownEquipmentType'].astype(str).value_counts()
           if 'breakdownEquipmentType' in df_cur.columns else pd.Series([], dtype=int))
  equip_rows = [{'name': str(k), 'count': int(v), 'pct': _pct(int(v), total)}
                for k, v in equip.items()]
  pareto = equip_rows[:_PARETO_TOP]
  cum, running = [], 0
  for r in pareto:
    running += r['count']
    cum.append(_pct(running, max(sum(x['count'] for x in equip_rows), 1)))
  equipment = {'rows': equip_rows, 'pareto': pareto, 'cum_pct': cum,
               'classes': len(equip_rows)}

  # ---- 处理时效（**故障处理时长** faultHandlingTime，毫秒）----
  # 与事件单同构地放进 `accept` 槽位：渲染层只差"标签"，结构与表格形状完全一致
  minutes = _parse_ms(df_cur['faultHandlingTime']) if 'faultHandlingTime' in df_cur.columns else []
  acc_minutes, _ = _parse_accept(df_cur)      # 验收时长（同一列 checkEndApprove）
  valid = [m for m in minutes if m is not None]
  accept = {'available': bool(valid), 'valid': len(valid), 'rows': total}
  if valid:
    s = pd.Series(valid)
    binned = pd.cut(s, bins=_DUR_BINS, labels=_DUR_BIN_LABELS,
                    include_lowest=True, right=False)
    hist_counts = binned.value_counts().reindex(_DUR_BIN_LABELS, fill_value=0)
    acc_valid = [m for m in acc_minutes if m is not None]
    accept.update({
      'p50': round(float(s.quantile(0.5)), 1),
      'p90': round(float(s.quantile(0.9)), 1),
      'p95': round(float(s.quantile(0.95)), 1),
      'min': round(float(s.min()), 1), 'max': round(float(s.max()), 1),
      'mean': round(float(s.mean()), 1),
      'readable': {'p50': _human_minutes(float(s.quantile(0.5))),
                   'p90': _human_minutes(float(s.quantile(0.9))),
                   'p95': _human_minutes(float(s.quantile(0.95))),
                   'mean': _human_minutes(float(s.mean())),
                   'max': _human_minutes(float(s.max()))},
      'hist': {'labels': list(_DUR_BIN_LABELS),
               'counts': [int(v) for v in hist_counts.tolist()]},
      # 验收时长（做"卡在处理还是卡在验收"的对比）
      'approve_valid': len(acc_valid),
      'approve_p50': round(float(pd.Series(acc_valid).quantile(0.5)), 1) if acc_valid else None,
      'approve_p90': round(float(pd.Series(acc_valid).quantile(0.9)), 1) if acc_valid else None,
      # 超 24 小时未处理完的占比（比事件单的"验收超时率"更贴切，故障有自己的时长）
      'over_1d_n': int((s > 1440).sum()),
      'over_1d_rate': _pct(int((s > 1440).sum()), len(s)),
      'timeout_n': None, 'timeout_base': 0, 'timeout_rate': None,
    })
    lv_of = df_cur[lv_col] if lv_col in df_cur.columns else pd.Series([None] * total)
    by_level: dict[str, list] = {}
    for lv, mins in zip(lv_of.tolist(), minutes):
      if mins is None or lv is None or pd.isna(lv):
        continue
      by_level.setdefault(f'{int(lv)}级', []).append(round(mins, 2))
    accept['by_level'] = {k: by_level[k] for k in sorted(by_level)}
    accept['by_level_stat'] = {
      k: {'单数': len(v), 'P50分钟': round(pd.Series(v).quantile(.5), 1),
          'P90分钟': round(pd.Series(v).quantile(.9), 1)}
      for k, v in accept['by_level'].items()}
  else:
    accept.update({'p50': None, 'p90': None, 'p95': None, 'readable': {},
                   'hist': {'labels': list(_DUR_BIN_LABELS),
                            'counts': [0] * len(_DUR_BIN_LABELS)},
                   'approve_valid': 0, 'approve_p50': None, 'approve_p90': None,
                   'over_1d_n': 0, 'over_1d_rate': None,
                   'timeout_n': None, 'timeout_base': 0, 'timeout_rate': None,
                   'by_level': {}, 'by_level_stat': {}})
  # 各园区「处理 vs 验收」（第 8 章对比柱）：不管处理时长是否可得都算——能得几项算几项
  accept['compare'] = _park_duration_compare(df_cur, minutes, acc_minutes)

  # 设备类型 × 处理时长（第 6 章散点：x=单量, y=P50 处理时长 → 治理优先级 = 高频且高耗时）
  scatter = []
  if valid and 'breakdownEquipmentType' in df_cur.columns:
    sc = pd.DataFrame({'equip': df_cur['breakdownEquipmentType'].astype(str).values,
                       'minutes': minutes}).dropna(subset=['minutes'])
    if len(sc):
      g = sc.groupby('equip')['minutes']
      agg = pd.DataFrame({'单量': g.count(), 'P50分钟': g.quantile(.5).round(1)}).reset_index()
      scatter = [{'name': str(r['equip']), 'count': int(r['单量']), 'p50_min': float(r['P50分钟'])}
                 for _, r in agg.sort_values(['单量', 'equip'],
                                             ascending=[False, True]).iterrows()]
  equipment['scatter'] = scatter

  # 机房 × 级别（热力图）
  dc_level = {'dcs': [], 'levels': [], 'matrix': []}
  if 'dc' in df_cur.columns and lv_col in df_cur.columns and total:
    top_dcs = [str(k) for k in df_cur['dc'].value_counts().head(_HEATMAP_MAX_DC).index]
    lv_labels = [row['label'] for row in level]
    dc_level = {'dcs': top_dcs, 'levels': lv_labels,
                'matrix': [[int((df_cur[df_cur['dc'] == dc][lv_col] == int(lab[:-1])).sum())
                            for lab in lv_labels] for dc in top_dcs]}

  # 工单质量：只用有字段支撑的派生指标
  reject_n = int((df_cur['status'] == 2).sum()) if 'status' in df_cur.columns else 0
  closed_n = int((df_cur['stage'] == 5).sum()) if 'stage' in df_cur.columns else 0
  repeat = _repeat_by_equipment(df_cur)   # 口径见函数 docstring
  repeat_n = sum(r['count'] for r in repeat)
  quality = {
    'over_1d_rate': accept.get('over_1d_rate'),
    'over_1d_n': accept.get('over_1d_n'),
    'reject_n': reject_n,
    'reject_rate': _pct(reject_n, total),
    'closed_n': closed_n,
    'closed_rate': _pct(closed_n, total),
    'repeat_classes': len(repeat),
    'repeat_n': repeat_n,
    'repeat_rate': _pct(repeat_n, total),
    'timeout_rate': None, 'timeout_n': None, 'timeout_base': 0,
  }

  # ---- 综合评分（雷达图）：本期与上一期同口径；级别列按各自帧重选（缺列回退 level）----
  prev_lv_col = 'breakDownLevel' if 'breakDownLevel' in df_prev.columns else 'level'
  cur_m = _score_inputs(df_cur, lv_col)
  prev_m = _score_inputs(df_prev, prev_lv_col)
  cur_scores = _scores(cur_m, prev_m['total'])
  prev_scores = _scores(prev_m, cur_m['total'])
  scorecard = _build_scorecard([
    (dim, meaning, cur_scores[dim], prev_scores[dim]) for dim, meaning in _SCORE_DIMS
  ])

  # 现存风险清单（第 10 章，确定性）：未关闭 / 长时间未处理 / 重复故障
  risk = _build_risk(df_cur, minutes, lv_col, repeat)

  # 可归因切片（与事件单同名同义，供解读"点名对象"）
  extras: dict = {}
  if valid and 'dc' in df_cur.columns:
    w = pd.DataFrame({'dc': df_cur['dc'], 'minutes': minutes}).dropna(subset=['dc'])
    if len(w):
      g = w.groupby('dc')['minutes']
      agg = pd.DataFrame({'单数': g.count(), 'P50分钟': g.quantile(.5).round(1),
                          'P90分钟': g.quantile(.9).round(1)}).reset_index()
      agg = agg[agg['单数'] >= 3]
      extras['最慢部门'] = [{'部门': r['dc'], '单数': int(r['单数']),
                       'P50分钟': float(r['P50分钟']), 'P90分钟': float(r['P90分钟'])}
                      for _, r in agg.sort_values(['P90分钟', 'dc'],
                                                  ascending=[False, True]).head(3).iterrows()]
    longest = pd.DataFrame({'dc': df_cur['dc'], 'minutes': minutes,
                            'name': df_cur.get('projectName'),
                            'ticketNo': df_cur.get('ticketNo')}).dropna(subset=['minutes'])
    extras['最长处理单'] = [
      {'部门': r['dc'], '分钟': round(float(r['minutes']), 1), '故障': str(r['name'])[:40],
       '工单编号': str(r.get('ticketNo') or '')}
      for _, r in longest.sort_values(['minutes', 'ticketNo'],
                                      ascending=[False, True]).head(3).iterrows()]
  extras['设备类型Top'] = equip_rows[:8]
  movers = [{'园区': p['name'], '本期': p['count'], '上一期': p['prev'],
             '环比': p['count'] - p['prev']} for p in park]
  movers.sort(key=lambda m: (-abs(m['环比']), m['园区']))
  extras['园区环比变动最大'] = movers[:4]

  # ---- 可归因切片（两类共用，实现见 common.py；本节只做装配）----
  # 缺了这些，第 4/7 章的图说明只能写"数据缺失"（实为切片没喂），行动计划也点不到具体对象。
  if valid and 'companyName' in df_cur.columns:
    extras['各园区时长'] = _park_duration_extras(df_cur, minutes)
  if 'companyName' in df_cur.columns and lv_col in df_cur.columns and len(df_cur):
    extras['各园区级别构成'] = _park_level_extras(df_cur, df_cur[lv_col])
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
    'total': {'value': str(total), 'sub': f'上一期 {prev_total} 单'},
    'daily_avg': {'value': f'{round(total / max(day_count, 1), 1)}',
                  'sub': f'覆盖 {day_count} 天'},
    'mom': {'value': (_delta_text(mom["delta"]) if prev_total else '—'),
            'sub': (_pct_text(mom['pct'], signed=True)
                    if mom['pct'] is not None else '无上期基数')},
    'top_level': _top_card(level, '最多级别'),
    'top_specialty': _top_card(specialty, '专业集中度'),
    'dept_top': ({'value': dept['rows'][0]['name'][:8],
                  'sub': f"{dept['rows'][0]['count']} 单（{dept['rows'][0]['pct']}%）"
                         f" · 共 {dept['classes']} 个部门"}
                 if dept['rows'] else {'value': '—', 'sub': '无数据'}),
    'park_top2': ({'value': f'{sum(p["count"] for p in park[:2])}', 'sub':
                   f'{park[0]["name"][:6]} 等前 2 个园区'}
                  if len(park) >= 2 else {'value': '—', 'sub': '无数据'}),
    'p90_duration': ({'value': accept['readable'].get('p90', '—'),
                      'sub': f"超 1 天 {accept['over_1d_n']} 单（{accept['over_1d_rate']}%）"}
                     if accept.get('available') else {'value': '—', 'sub': '处理时长未回填'}),
    'equipment_top': ({'value': equipment['pareto'][0]['name'][:6],
                       'sub': f"{equipment['pareto'][0]['count']} 单（{equipment['pareto'][0]['pct']}%）"
                              f" · 共 {equipment['classes']} 类"}
                      if equipment['pareto'] else {'value': '—', 'sub': '无数据'}),
  }

  out = {
    'window': window, 'total': total, 'prev_total': prev_total,
    'daily_avg': round(total / max(day_count, 1), 1),
    'mom': mom, 'trend': trend, 'level': level, 'specialty': specialty,
    'park': park, 'park_specialty': park_specialty, 'park_level': park_level,
    'equipment': equipment,
    'dept': dept, 'accept': accept, 'dc_level': dc_level, 'quality': quality,
    'repeat': repeat, 'risk': risk, 'kpi': kpi, 'extras': extras,
    'scorecard': scorecard,
    # 第 10 章要取详情的代表单（工作流按它去 fetch_row_details）
    'detail_targets': _detail_targets(df_cur, repeat, equipment),
    'detail_all': (_all_ticket_nos(df_cur)
                   if (window or {}).get('period') == 'weekly' else []),
    'charts_suppressed': total < CHART_MIN_ROWS,
  }
  # 月报专属维度（周报**不写这两个键**，输出逐字不变）
  if (window or {}).get('period') == 'monthly':
    out['rhythm'] = _month_rhythm(trend, window, '起')
    out['mom_top'] = _mom_top(
      {'level': level, 'park': park, 'specialty': specialty, 'dept': dept},
      [('级别', 'level'), ('园区', 'park'), ('专业', 'specialty'), ('部门', 'dept')])
  return out


def _detail_targets(df: pd.DataFrame, repeat: list[dict], equipment: dict,
                    top_n: int = 5) -> list[str]:
  """第 10 章"取详情"的代表单编号：**先重复故障、不足则按设备类型 Top 补足**。

  为什么不能只看重复：全国·周窗口里"同类设备同地点 ≥2 次"很可能是 0（实测 56 单 → 0 类），
  于是最有治理价值的第 10 章（品牌/型号/备件/原因）会整章空着。设备类型才是故障单的主维度，
  所以按设备类型 Top 各取一张代表单兜底 —— 这一章本就叫「高频故障根因与备件分析」。
  """
  out: list[str] = []
  for r in repeat:
    no = r.get('ticketNo')
    if no and no not in out:
      out.append(no)
  if len(out) >= top_n or 'breakdownEquipmentType' not in df.columns:
    return out[:top_n]
  for row in equipment.get('rows') or []:
    sub = df[df['breakdownEquipmentType'].astype(str) == row['name']]
    nos = sorted(str(v) for v in sub.get('ticketNo', pd.Series(dtype=str)).tolist()
                 if isinstance(v, str) and v.strip())
    if nos and nos[0] not in out:
      out.append(nos[0])
    if len(out) >= top_n:
      break
  return out[:top_n]


def _repeat_by_equipment(df: pd.DataFrame) -> list[dict]:
  """故障单的「重复」= **同类设备在同一地点反复故障**。

  为什么不用事件单那套"按故障名称聚类"：实测故障名几乎不重名（本周 30 条 → 重复类别 0），
  按名称聚类会让这一整章永远空着。故障的重复要看"哪台/哪类设备在哪个机房反复出问题"，
  这才是指向设备治理的口径。
  """
  if not {'breakdownEquipmentType', 'occurrenceSite'} <= set(df.columns):
    return []
  key = (df['breakdownEquipmentType'].astype(str).str.strip() + ' @ '
         + df['occurrenceSite'].astype(str).str.strip())
  counts = key.value_counts()
  rows = sorted(((str(k), int(v)) for k, v in counts[counts >= 2].items()),
                key=lambda t: (-t[1], t[0]))
  out = []
  for name, cnt in rows:
    sub = df[key == name]
    tickets = sorted(str(v) for v in sub.get('ticketNo', pd.Series(dtype=str)).tolist()
                     if isinstance(v, str) and v.strip())
    parks = sorted({str(p) for p in sub.get('companyName', []) if isinstance(p, str)})
    dcs = sorted({str(d) for d in sub.get('dc', []) if isinstance(d, str)})
    out.append({'name': name, 'count': cnt, 'parks': parks, 'dcs': dcs,
                '集中机房': dcs[0] if dcs else '', '集中机房单数': cnt,
                'ticketNo': tickets[0] if tickets else ''})
  return out


def _park_duration_compare(df: pd.DataFrame, minutes: list, acc_minutes: list,
                           min_n: int = 3) -> list[dict]:
  """各园区「处理时长 P50 vs 验收时长 P50」—— 第 8 章对比柱（卡在处理还是卡在验收）。

  验收时长**经常未回填**：某园区没有验收样本时 `验收P50=None`，图/表按可得项渲染，不编。
  样本 < `min_n` 的园区不参与排名（与其余"按部门"口径一致）。
  """
  if 'companyName' not in df.columns:
    return []
  n = len(df)
  handle = minutes if len(minutes) == n else [None] * n
  approve = acc_minutes if len(acc_minutes) == n else [None] * n
  w = pd.DataFrame({'园区': df['companyName'].values, '处理': handle, '验收': approve})
  w = w.dropna(subset=['园区'])
  out = []
  for name, sub in w.groupby('园区'):
    h = sub['处理'].dropna()
    if len(h) < min_n:
      continue
    a = sub['验收'].dropna()
    out.append({'园区': str(name), '单数': int(len(h)),
                '处理P50': round(float(h.quantile(.5)), 1),
                '验收P50': round(float(a.quantile(.5)), 1) if len(a) else None})
  return sorted(out, key=lambda r: r['园区'])


def _build_risk(df: pd.DataFrame, minutes: list, lv_col: str, repeat: list[dict]) -> dict:
  """现存风险清单（**确定性**）：未关闭 / 长时间未处理（>3 天）/ 重复故障。

  为什么单列这一章：故障报告要回答"现在还有哪些没收干净"，这是管理层最关心的一张清单，
  且与事件报告的口径不同（事件单没有"未关闭/重复设备"这套风险口径）。每类附代表工单编号。
  """
  total = len(df)

  def _tickets(sub: pd.DataFrame, k: int = 5) -> list[str]:
    if 'ticketNo' not in sub.columns:
      return []
    return sorted(str(v) for v in sub['ticketNo'].tolist()
                  if isinstance(v, str) and v.strip())[:k]

  open_mask = (df['stage'] != 5).to_numpy() if 'stage' in df.columns else None
  long_mask = ((pd.Series(minutes) > 4320).to_numpy()
               if minutes and len(minutes) == total else None)
  high_open_n = 0
  if open_mask is not None and lv_col in df.columns:
    lv = pd.to_numeric(df[lv_col], errors='coerce')
    high_open_n = int(((lv >= 4) & open_mask).sum())

  open_n = int(open_mask.sum()) if open_mask is not None else 0
  long_n = int(long_mask.sum()) if long_mask is not None else 0
  repeat_n = sum(r['count'] for r in repeat)
  rows = [
    {'风险': '未关闭故障单', '单数': open_n, '口径': '阶段不为「故障关闭」',
     '代表工单': _tickets(df[open_mask]) if open_mask is not None else []},
    {'风险': '长时间未处理（超 3 天）', '单数': long_n, '口径': '故障处理时长超过 3 天',
     '代表工单': _tickets(df[long_mask]) if long_mask is not None else []},
    {'风险': '重复故障（设备类型 @ 地点）', '单数': repeat_n,
     '口径': '同类设备在同一地点发生 2 次及以上',
     '代表工单': [r['ticketNo'] for r in repeat[:5] if r.get('ticketNo')]},
  ]
  return {'open_n': open_n, 'open_rate': _pct(open_n, total),
          'long_n': long_n, 'long_rate': _pct(long_n, total),
          'high_open_n': high_open_n, 'rows': rows}


# ==================== 绘图（9 张，全确定性；图表上不加数据标签） ====================

def draw_charts(df_cur: pd.DataFrame, df_prev: pd.DataFrame, stats: dict, plt, rz_save,
                rz_heat=(), rz_level=()) -> None:
  """画 9 张图到 charts/。`plt`/`rz_save`/`rz_heat`/`rz_level` 由调用方传入（沙箱预置 / 单测自备）。"""
  if stats.get('charts_suppressed'):
    return                                  # 样本 < CHART_MIN_ROWS：图表没有分布可言，章节用表格呈现
  colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
  _MAIN, _CUR, _PREV, _ACCENT = colors[0], colors[0], colors[1], colors[3]
  _UP, _DOWN = colors[2], colors[1]          # 发散条形：增（恶化）＝橙，减（改善）＝绿
  # 图 0 综合评分（雷达：本期 vs 上一期；分数见第 2 章表格）
  _draw_scorecard(stats.get('scorecard'), '00_scorecard', plt, rz_save)

  # 图 1 日趋势（本期深蓝 / 上一期绿）
  fig, ax = plt.subplots()
  ax.plot(range(len(stats['trend']['cur'])), stats['trend']['cur'], marker='o',
          label='本期', color=_CUR)
  ax.plot(range(len(stats['trend']['prev'])), stats['trend']['prev'], marker='o',
          linestyle='--', label='上一期', color=_PREV)
  _date_axis(ax, stats['trend']['labels'])
  ax.set_xlabel('日期'), ax.set_ylabel('故障单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '01_trend', plt, rz_save)

  # 图 1b 月内周节奏（工作日 vs 周末 日均）：仅月报有数据，周报直接跳过
  if stats.get('rhythm'):
    _draw_rhythm(stats['rhythm'], '01b_month_rhythm', plt, rz_save)

  # 图 2 故障级别分布
  fig, ax = plt.subplots()
  ax.bar([r['label'] for r in stats['level']], [r['count'] for r in stats['level']],
         width=_bar_width(len(stats['level'])), color=_MAIN)
  ax.set_xlabel('故障级别'), ax.set_ylabel('故障单量')
  _int_ticks(ax)
  _emit(fig, '02_level', plt, rz_save)

  # 图 3 各园区（本期 vs 上一期）
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
  ax.set_ylabel('故障单量')
  _int_ticks(ax)
  _legend_below(ax)
  _emit(fig, '03_park', plt, rz_save)

  # 图 4 各园区 × 故障级别结构（横向堆叠条，第 5 章）—— 一眼横向比较"哪个园区的级别结构更重"。
  # 配色取用户提供的示例色板（蓝→橙→红，按级别升序等跨度取）；图内不画数据标签，靠轴读数。
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
    height = _bar_width(len(park_names))
    for j, lab in enumerate(pl_levels):
      vals = [(pl_matrix[i][j] if i < len(pl_matrix) and j < len(pl_matrix[i]) else 0)
              for i in order]
      ax.barh(park_names, vals, left=lefts, height=height, color=pal[j], label=lab,
              edgecolor='white', linewidth=0.8)
      lefts = [l + v for l, v in zip(lefts, vals)]
    ax.invert_yaxis()                          # 单量最大的园区在最上
    ax.set_xlabel('故障单量')
    _int_ticks(ax, 'x')
    _legend_below(ax)
    _emit(fig, '09_park_level', plt, rz_save)

  # 图 4 **设备类型帕累托**（柱 + 累计占比折线）—— 故障单的标志性图：
  # 一眼看出"哪几类设备贡献了 80% 的故障"。累计占比是**轴线数字**不是数据标签，不违反"图不加标签"。
  eq = stats['equipment']['pareto']
  if eq:
    fig, ax = plt.subplots()
    xs = range(len(eq))
    ax.bar(xs, [r['count'] for r in eq], width=_bar_width(len(eq)), color=_MAIN,
           label='故障单量')
    ax.set_xticks(list(xs))
    ax.set_xticklabels([r['name'] for r in eq], rotation=30, ha='right')
    ax.set_ylabel('故障单量')
    _int_ticks(ax)
    ax2 = ax.twinx()
    ax2.plot(list(xs), stats['equipment']['cum_pct'], marker='o', color=_ACCENT,
             label='累计占比')
    ax2.set_ylabel('累计占比（%）')
    ax2.set_ylim(0, 105)
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    _legend_below(ax, ncol=2, handles=h1 + h2, labels=l1 + l2)
    _emit(fig, '04_equipment_pareto', plt, rz_save)

  # 图 5 设备类型 × 处理时长 散点（x=单量, y=P50 处理时长）—— 故障单专属：
  # 右上象限＝"高频且高耗时"＝治理优先级最高。中位象限线是辅助线，不是数据标签（不加点标注）。
  sc = stats['equipment'].get('scatter') or []
  if len(sc) >= 2:
    fig, ax = plt.subplots()
    xs = [r['count'] for r in sc]
    ys = [r['p50_min'] for r in sc]
    ax.scatter(xs, ys, s=70, color=_MAIN, alpha=0.75, edgecolors='white',
               linewidths=0.8, zorder=3)
    ax.axvline(pd.Series(xs).median(), color=_ACCENT, linestyle='--', linewidth=1)
    ax.axhline(pd.Series(ys).median(), color=_ACCENT, linestyle='--', linewidth=1)
    ax.set_xlabel('故障单量'), ax.set_ylabel('处理时长 P50（分钟）')
    _int_ticks(ax, 'x')
    _emit(fig, '05_equipment_scatter', plt, rz_save)

  # 图 6 各部门故障环比增减（发散条形：本期 − 上期）—— 事件单没有的维度。
  # 增（恶化）＝橙向右，减（改善）＝绿向左，绕 0 分列；不画数据标签，靠轴读数。
  dept_rows = (stats.get('dept') or {}).get('rows') or []
  changed = [r for r in dept_rows if r.get('delta')]        # 只列有变化的部门，0 变化不占位置
  if changed:
    sel = sorted(changed, key=lambda r: -abs(r['delta']))[:12]   # 取变动最大的 12 个
    sel.sort(key=lambda r: r['delta'])                           # barh 自下而上 → 升序让增幅最大在最上
    fig, ax = plt.subplots(figsize=(8.4, max(2.6, 0.42 * len(sel) + 1.2)))
    ax.barh([r['name'] for r in sel], [r['delta'] for r in sel],
            height=_bar_width(len(sel)),
            color=[_UP if r['delta'] > 0 else _DOWN for r in sel])
    ax.axvline(0, color='#8a8f98', linewidth=0.8)
    ax.set_xlabel('环比增减（本期 − 上期，单）')
    _int_ticks(ax, 'x')
    _emit(fig, '06_dept_bar', plt, rz_save)

  # 图 7 处理时长区间分布（直方图）
  fig, ax = plt.subplots()
  hist = stats['accept']['hist']
  ax.bar(hist['labels'], hist['counts'], width=_bar_width(len(hist['labels'])),
         color=_MAIN)
  ax.set_xlabel('故障处理时长'), ax.set_ylabel('故障单量')
  ax.set_xticklabels(hist['labels'], rotation=30, ha='right')
  _int_ticks(ax)
  _emit(fig, '07_duration_hist', plt, rz_save)

  # 图 8 各园区「处理 vs 验收」P50（对比柱）—— 回答"卡在处理还是卡在验收"。
  # 验收常未回填：缺项的园区验收柱留空（nan 不绘制），不画 0 假装有数据。
  cmp = stats['accept'].get('compare') or []
  if cmp:
    fig, ax = plt.subplots()
    names = [r['园区'] for r in cmp]
    xs = range(len(names))
    width = _bar_width(len(names), 2)
    cur_off, prev_off = _bar_offsets(len(names), 2)
    ax.bar([x + cur_off for x in xs], [r['处理P50'] for r in cmp], width=width,
           label='处理时长', color=_CUR)
    ax.bar([x + prev_off for x in xs],
           [r['验收P50'] if r['验收P50'] is not None else float('nan') for r in cmp],
           width=width, label='验收时长', color=_PREV)
    ax.set_xticks(list(xs)), ax.set_xticklabels(names, rotation=15)
    ax.set_ylabel('时长 P50（分钟）')
    _legend_below(ax)
    _emit(fig, '08_duration_compare', plt, rz_save)


def main(plt, rz_save, rz_heat=(), rz_level=()) -> None:
  """沙箱入口：读 cwd 下的 data.csv / prev.csv → 写 stats.json + charts/。

  `rz_level`（示例级别色板）缺省时从沙箱命名空间取 `RZ_LEVEL_COLORS`（由样式注入）——
  这样 `render.run_compute` 对事件/故障两类 compute 用**同一条 `main(...)` 调用**，无需分支。
  """
  cur = pd.read_csv('data.csv')
  prev_path = Path('prev.csv')
  prev = pd.read_csv(prev_path) if prev_path.is_file() else pd.DataFrame()
  window_path = Path('window.json')
  window = json.loads(window_path.read_text(encoding='utf-8')) if window_path.is_file() else {}
  stats = compute_stats(cur, prev, window)
  Path('stats.json').write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
  draw_charts(cur, prev, stats, plt, rz_save, rz_heat,
              rz_level or globals().get('RZ_LEVEL_COLORS') or ())
  print(json.dumps({'rows_cur': stats['total'], 'rows_prev': stats['prev_total'],
                    'equipment_classes': stats['equipment']['classes'],
                    'charts': len(list(Path('charts').glob('*.png')))}, ensure_ascii=False))

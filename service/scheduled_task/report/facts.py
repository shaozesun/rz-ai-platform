"""报告的「内容选择」阶段 —— 把**按位置对齐的结构**摊平成带编号的自述句（确定性，0 次 LLM）。

为什么必须有这一段：此前流水线把 `stats` 里按位置对齐的数组（热力图 `dcs/levels/matrix`、
`园区×专业` 的 `parks/specialties/matrix`、帕累托的 `pareto/cum_pct`、直方图的
`labels/counts`）**原样**丢给模型，让模型自己按行列去数、去对齐。实测事故（2026-10-15）：

  热力图解读写「惠州A3分部 4 级事件分布密集且耗时长」，而那一格实为 **0**
  （真实是名字相近的 `A3分部 = [1,5,10,1]`，`惠州A3分部 = [0,4,10,0]`）。

这类错在业界叫 **intrinsic hallucination**（由**输入表示方式本身**诱发），解法是
**换表示**而不是改提示词。本模块把每个格子说成一句**坐标写全**的话并编号，模型只负责把
话写通顺，且每句必须指明依据（`narrate._validate_section` 校验）。

**边界（用户已拍板）**：只处理「必须对齐位置才读得懂」的结构。
「每条自带名字」的结构（级别/园区/专业/重复/设备类型表）读不出错，**原样保留、不在此重建**
—— 维度不收限。

⚠️ 与 `common.py` 不同：本模块**不进沙箱**（不被 `run_compute` 拼接），是服务端进程里的纯函数。
"""

from service.scheduled_task.report.common import (
  DEPT_TABLE_LIMIT, _delta_text, _human_minutes, _pct_text,
)


def is_weekly(stats: dict | None) -> bool:
  """按 `stats['window']['period']` 判周期；缺 window 一律按月报（老 stats.json 兼容）。"""
  return ((stats or {}).get('window') or {}).get('period') == 'weekly'


def impact_level(stats: dict | None, details: dict | None) -> dict | None:
  """周报且详情影响时长可用时返回 `impact.by_level` 矩阵，否则 None（月报/样本不足走验收口径）。

  **唯一判据**：第 4 章的图标题、表格与事实都必须用它判定，否则会出现「标题写着影响时长、
  表格却是验收时长」这类同章两口径混排（render/narrate/facts 三处共用本函数）。
  """
  if not is_weekly(stats):
    return None
  imp = (details or {}).get('impact') or {}
  if not imp.get('available'):
    return None
  bl = imp.get('by_level') or {}
  return bl if bl.get('levels') and bl.get('matrix') else None


def _has_chart(spec, key: str, role: str | None = None) -> bool:
  """本章规格是否声明了某张图 —— 事实只服务**报告真会画出来的图**（见 build_facts 说明）。

  给了 `role` 时还要**按章节收限**：图的 `section` 对应角色必须就是本章，
  否则某章会拿到别章的矩阵事实，写出离题结论。

  （事件报告 2026-10-29 起把专业并入园区章，`park` 角色因 `park_specialty` 图的
  `section` 正对本章而拿到该事实；这条原先靠「specialty 特例」实现，现已删除。）
  """
  roles = getattr(spec, 'roles', ()) or ()
  for c in (getattr(spec, 'charts', ()) or ()):
    if getattr(c, 'key', '') != key:
      continue
    if role is None:
      return True
    sec = getattr(c, 'section', 0)
    if 1 <= sec <= len(roles) and roles[sec - 1] == role:
      return True
  return False


def build_facts(stats: dict, spec, role: str, details: dict | None = None) -> list[dict]:
  """本章的「数据事实」：`[{'编号': 'F1', '事实': '...'}, ...]`；本章无对齐型结构时返回 `[]`。

  编号是**章内局部**的（每次调用从 F1 开始）—— 模型只在**同一章内**引用，不需要全局唯一。
  按 `role` 分派（与 `narrate._section_payload` 同一套角色）。

  **按 spec.charts 收限**：热力图 / 园区×专业 的事实只在**该类报告确实有这两张图**时才给
  —— 故障报告已不再画它们，若仍喂这些事实，会让模型在「园区对比」章写出「园区×专业」的
  离题要点。对齐型事实化只服务报告真正展示的结构。

  `details` 只有「影响时长（详情口径）」用得上（`details['impact']`）；不传即无影响时长事实。
  """
  lines: list[str] = []
  if role == 'trend':
    lines += _trend_facts(stats.get('trend'), spec)
    # 月报专属（周报不写这两个键 → 这里拿到 None/缺省，两个函数各自早退）
    lines += _rhythm_facts(stats.get('rhythm'), spec)
    lines += _mom_top_facts(stats.get('mom_top'), spec)
  elif role in ('park', 'specialty'):
    if _has_chart(spec, 'park_specialty', role):
      lines += _matrix_facts(stats.get('park_specialty'), 'parks', 'specialties', '园区×专业')
    if _has_chart(spec, 'park_level', role):
      lines += _matrix_facts(stats.get('park_level'), 'parks', 'levels', '园区×级别')
    # 单园区报告的部门维度（全国级不写该键 → 事实为空，输出逐字不变）
    if _has_chart(spec, 'dept', role):
      lines += _dept_facts(stats.get('dept'))
  elif role == 'equipment_specialty':
    lines += _matrix_facts(stats.get('park_specialty'), 'parks', 'specialties', '园区×专业')
    lines += _pareto_facts(stats.get('equipment'))
  elif role == 'duration':
    lines += _hist_facts(stats.get('accept'), spec)
    if _has_chart(spec, 'dc_level'):
      lines += _matrix_facts(stats.get('dc_level'), 'dcs', 'levels', '机房×级别')
    lines += _impact_facts((details or {}).get('impact'))
  elif role == 'level':
    bl = impact_level(stats, details)
    # 周报改用**影响时长**（详情口径）时本表与图都是它，事实必须同源 —— 否则模型会照着
    # 验收时长的句子去解读影响时长的图（两者数值差别很大：P50 0.2 小时 vs 38.5 小时）。
    if bl:
      if _has_chart(spec, 'level_duration', role):
        lines += _matrix_facts(bl, 'levels', 'bins', '级别×影响时长')
      lines += _impact_level_facts((details or {}).get('impact'))
    else:
      ld = stats.get('level_duration') or {}
      if _has_chart(spec, 'level_duration', role):
        lines += _matrix_facts(ld, 'levels', 'bins', '级别×验收时长')
      lines += _level_dur_facts(ld)
  # ---- 问题工单 ----
  elif role == 'level_stage':
    if _has_chart(spec, 'level_stage', role):
      lines += _matrix_facts(stats.get('level_stage'), 'levels', 'stages', '级别×阶段')
  elif role == 'aging':
    ag = stats.get('aging') or {}
    lines += _bands_facts(ag.get('bins'), ag.get('counts'), '未关闭问题滞留时长')
    if _has_chart(spec, 'dc_aging', role):
      lines += _matrix_facts(stats.get('dc_aging'), 'dcs', 'bins', '空间×滞留档')
  elif role == 'entry_lag':
    el = stats.get('entry_lag') or {}
    lines += _bands_facts(el.get('bins'), el.get('counts'), '录入滞后')
  elif role == 'park_specialty':
    if _has_chart(spec, 'park_specialty', role):
      lines += _matrix_facts(stats.get('park_specialty'), 'parks', 'specialties', '园区×专业')
  # ---- 变更工单：环节耗时（单园区专属，全国级 flow 里没有这些键）----
  elif role == 'flow':
    fl = stats.get('flow') or {}
    lines += _flow_duration_facts(fl.get('durations'))
    lines += _flow_level_facts(fl.get('by_level_duration'))
    lines += _flow_exception_facts(fl.get('exception_rates'))
  # ---- 阈值工单 ----
  elif role == 'threshold_intent':
    if _has_chart(spec, 'intent_park', role):
      lines += _matrix_facts(stats.get('intent_park'), 'intents', 'parks', '调整意图×园区')
  elif role == 'lead_time':
    h = (stats.get('lead') or {}).get('hist') or {}
    lines += _bands_facts(h.get('labels'), h.get('counts'), '计划提前期')
  elif role == 'execution':
    sd = (stats.get('execution') or {}).get('start_delay') or {}
    lines += _pair_facts(sd.get('hist'), '档位', '单数', '实际开始相对计划开始的偏差')
  return [{'编号': f'F{i}', '事实': t} for i, t in enumerate(lines, 1)]


def _trend_facts(t: dict | None, spec) -> list[str]:
  """日趋势：**日期与数值绑在一起**，且把比较也预先算好（模型不需要做减法/比大小）。

  为什么必须给比较：只给「10-05 本期 6（上期 7）」时，正文的「高于/低于几起」要模型自己
  推。2026-10-15 实测事故就是它把「10-11 的 5 起」写成「高于 10-05 的 6 起」——
  依据校验只能查编号，查不出数值关系真假。故这里直接输出增减量、排序、零单日与峰谷。
  """
  if not t or not t.get('labels'):
    return []
  unit = getattr(spec, 'count_unit', '起')
  labels = [str(x) for x in t['labels']]
  cur = [int(v) for v in (t.get('cur') or [])]
  prev = [int(v) for v in (t.get('prev') or [])]
  prev_days = [str(x) for x in (t.get('prev_days') or [])]
  n = len(labels)

  def _has_prev(i: int) -> bool:
    return i < len(prev_days) and bool(prev_days[i])

  def _delta(a: int, b: int) -> str:
    d = a - b
    if d > 0:
      return f'增加 {d} {unit}'
    if d < 0:
      return f'减少 {abs(d)} {unit}'
    return '持平'

  out: list[str] = []
  if any(_has_prev(i) for i in range(n)):
    prev_total = sum(prev[i] if i < len(prev) else 0 for i in range(n) if _has_prev(i))
    total = sum(cur)
    out.append(f'本期共 {total} {unit}，上一期 {prev_total} {unit}'
               f'（{_delta(total, prev_total)}）')
  for i, lab in enumerate(labels):
    if _has_prev(i):
      pv = prev[i] if i < len(prev) else 0
      out.append(f'{lab}：本期 {cur[i]} {unit}，上一期同日 {pv} {unit}'
                 f'（{_delta(cur[i], pv)}）')
    else:
      out.append(f'{lab}：本期 {cur[i]} {unit}（上一期无对位日期）')
  # 全平序列（周报常见：多数日期为 0，或每天都在 1~2 单）：排序无信息、峰谷句还会写成
  # 「峰值日=低谷日=所有日期」。此时只给一句平铺事实，模型无从编出"峰谷"。
  uniform = len(set(cur)) == 1
  if uniform:
    out.append(f'每日均为 {cur[0]} {unit}（共 {n} 天）')
  else:
    ranked = sorted(((v, lab) for lab, v in zip(labels, cur) if v > 0),
                    key=lambda x: (-x[0], x[1]))
    if ranked:
      out.append('本期单量由高到低：'
                 + ' > '.join(f'{lab} {v} {unit}' for v, lab in ranked))
  zeros = [lab for lab, v in zip(labels, cur) if v == 0]
  if zeros:
    out.append(f'无单日期：{"、".join(zeros)}（共 {len(zeros)} 天，均为 0 {unit}）')
  if cur and not uniform:
    mx, mn = max(cur), min(cur)
    peak = [lab for lab, v in zip(labels, cur) if v == mx]
    low = [lab for lab, v in zip(labels, cur) if v == mn]
    out.append(f'峰值日 {_days_with_value(peak, mx, unit)}；'
               f'低谷日 {_days_with_value(low, mn, unit)}')
  return out


def _days_with_value(days: list[str], value: int, unit: str) -> str:
  """并列时把日期全列出来（「10-09、10-10（各 12 起）」），避免只报一个误导。"""
  if len(days) == 1:
    return f'{days[0]}（{value} {unit}）'
  return f'{"、".join(days)}（各 {value} {unit}）'


def _num_text(value) -> str:
  """日均等一位小数：整数值不带 `.0`（2.0 → 2），其余保留一位（2.4）。"""
  num = round(float(value or 0), 1)
  return str(int(num)) if num.is_integer() else f'{num:.1f}'


def _rhythm_facts(rhythm: dict | None, spec) -> list[str]:
  """月报「周内节奏」→ 带编号事实：逐周（含日均）、工作日/周末、星期分布、排序句。

  **比较全部算好**（与日趋势同一原则）：节奏是月报最容易写错的地方（长短不一的周直接比
  单量会得出反的结论），所以这里把"比较请用日均"写进句子里，模型只照抄。
  """
  if not rhythm:
    return []
  unit = getattr(spec, 'count_unit', '起')
  out = ['月内节奏按月内自然周（周一为界）分组；首末周天数可能不足 7 天，'
         '**比较各周只看日均，不比单量**']
  weeks = rhythm.get('weeks') or []
  for w in weeks:
    out.append(f'{w["label"]}（{w["start"][5:]}~{w["end"][5:]}，{w["days"]} 天）'
               f'共 {w["count"]} {unit}，日均 {_num_text(w["avg"])} {unit}'
               f'（工作日日均 {_num_text(w["work_avg"])}、周末日均 '
               f'{_num_text(w["rest_avg"])}）')
  split = rhythm.get('split') or []
  if split:
    out.append('；'.join(f'{b["label"]} {b["days"]} 天共 {b["count"]} {unit}'
                         f'（日均 {_num_text(b["avg"])} {unit}）' for b in split))
  dow = rhythm.get('dow') or []
  if dow:
    out.append('星期分布：' + '、'.join(f'{d["label"]} {d["count"]} {unit}' for d in dow))
    ranked = sorted(((d['count'], i, d['label']) for i, d in enumerate(dow)),
                    key=lambda x: (-x[0], x[1]))
    out.append('按星期单量由高到低：'
               + ' > '.join(f'{lab} {n} {unit}' for n, _, lab in ranked))
  return out


def _mom_top_facts(mom_top: list | None, spec) -> list[str]:
  """月报「环比变化 TOP」→ 带编号事实（变化量/占比变化平台已算好，模型只引用）。"""
  if not mom_top:
    return []
  unit = getattr(spec, 'count_unit', '起')
  out = ['环比变化 TOP（按变化量绝对值取前 5，各维度分别取行后混排，同一对象只出现一次）']
  for r in mom_top:
    delta = int(r.get('增减') or 0)
    text = (f'{r["维度"]}「{r["项目"]}」：本期 {r["本期"]} {unit}，上一期 {r["上一期"]} '
            f'{unit}（{_delta_text(delta)} {unit}'
            if delta else
            f'{r["维度"]}「{r["项目"]}」：本期 {r["本期"]} {unit}，上一期 {r["上一期"]} '
            f'{unit}（持平')
    pp = r.get('占比变化')
    if pp is not None:
      text += ('；占比变化 '
               f'{_pct_text(pp, signed=True).rstrip("%")} 个百分点')
    out.append(text + '）')
  return out


def _matrix_facts(block: dict | None, row_key: str, col_key: str, note: str) -> list[str]:
  """对齐矩阵 → **坐标写全**的逐格自述句 + 「其余为 0」兜底。

  只逐格列**非零**格（热力图最多 12×4=48 格，全铺开是噪音）；兜底句明确交代"没列出来的都是 0"
  —— 本次事故的根因正是模型替我们**猜**了某一格是否密集，兜底句把"猜"这件事本身消掉。
  """
  if not block:
    return []
  rows = block.get(row_key) or []
  cols = block.get(col_key) or []
  matrix = block.get('matrix') or []
  if not rows or not cols:
    return []
  out = [f'{note}分布（{len(rows)} 行 × {len(cols)} 列）']
  zeros = 0
  for i, r in enumerate(rows):
    row = matrix[i] if i < len(matrix) else []
    for j, c in enumerate(cols):
      v = int(row[j]) if j < len(row) else 0
      if v:
        out.append(f'{r} {c} {v} 单')
      else:
        zeros += 1
  if zeros:
    out.append(f'其余 {zeros} 个{note}组合为 0 单')
  return out


def _pareto_facts(equipment: dict | None) -> list[str]:
  """帕累托：把平行的 `cum_pct` **折进同一条**（累计占比与设备类型绑死，不可能挂错行）。"""
  eq = equipment or {}
  pareto, cum = eq.get('pareto') or [], eq.get('cum_pct') or []
  if not pareto:
    return []
  out = []
  for i, r in enumerate(pareto):
    tail = f'，累计 {cum[i]}%）' if i < len(cum) else '）'
    out.append(f'{r["name"]} {r["count"]} 单（占 {r["pct"]}%{tail}')
  if eq.get('classes'):
    out.append(f'共 {eq["classes"]} 类设备，上图仅列出故障最多的 {len(pareto)} 类')
  return out


def _impact_facts(imp: dict | None) -> list[str]:
  """影响时长（详情口径）→ 逐档/逐级自述句；**不把级别×档矩阵喂给模型**（表格已展示）。

  与验收时长**刻意分开编号**：模型在同一章里同时看到两个时长块时，最容易把两者混着说，
  故每句都带「影响时长」四字作锚。
  """
  if not imp:
    return []
  if not imp.get('available'):
    return [f'影响时长：{imp.get("reason") or "数据不足"}，本期不做分布分析']
  out = [f'影响时长覆盖 {imp["valid"]}/{imp["n"]} 单（其余 {imp["missing"]} 单未填或无法解析）',
         f'影响时长 P50 {imp["readable"]["p50"]}、P90 {imp["readable"]["p90"]}、'
         f'最长 {imp["readable"]["max"]}',
         f'影响不足 1 小时的单 {imp["short_pct"]}%']
  h = imp.get('hist') or {}
  out += _bands_facts(h.get('labels'), h.get('counts'), '影响时长')
  # 按级别的分位句已上移到第 4 章（`_impact_level_facts`），此处不再重复同一维度
  return out


def _impact_level_facts(imp: dict | None) -> list[str]:
  """影响时长 × 级别：逐级别一句（N 单（占 X%），P50 …、P90 …、最长 …）。

  第 4 章周报改用**影响时长**口径时用；级别数字已由 `render`/解读层与验收口径同序陈述。
  """
  out = []
  for row in ((imp or {}).get('by_level') or {}).get('stats') or []:
    out.append(f'影响时长 {row["级别"]}：{row["单数"]} 单（占 {row["占比"]}%），'
               f'P50 {_human_minutes(row["P50分钟"])}、P90 {_human_minutes(row["P90分钟"])}、'
               f'最长 {_human_minutes(row["最长分钟"])}')
  return out


def _dept_facts(dept: dict | None) -> list[str]:
  """单园区「部门维度」→ 逐部门一句（单量/环比/审批与实施 P50，与部门表列一致）。

  三个率（紧急/按时/驳回）不再下发：表里已删掉这三列，喂给模型只会让它把这几个数
  再讲一遍（紧急占比在第 7 章、按时率在第 9 章、驳回率在第 8 章异常表各有出处）。
  """
  rows = (dept or {}).get('rows') or []
  if not rows:
    return []
  # **与第 5 章表格同一切片**：事实里多出的部门，模型会点名，而读者在表里找不到
  total = dept.get('classes') or len(rows)
  rows = rows[:DEPT_TABLE_LIMIT]
  out = [(f'部门维度共 {total} 个部门，按本期单量降序列出前 {len(rows)} 个'
          if total > len(rows) else f'部门维度共 {total} 个部门（按本期单量降序）')]
  for r in rows:
    diff = '持平' if not r['delta'] else f'{_delta_text(r["delta"])} 单'
    out.append(
      f'{r["name"]}：本期 {r["count"]} 单（占 {r["pct"]}%），上一期 {r["prev"]} 单（{diff}）；'
      f'审批 P50 {_human_minutes(r.get("approve_p50"))}、'
      f'实施 P50 {_human_minutes(r.get("exec_p50"))}')
  return out


def _flow_duration_facts(rows: list | None) -> list[str]:
  """各环节耗时（审批/实施/验收/全流程）→ 逐环节一句；口径不同、不可相加。"""
  rows = rows or []
  if not rows:
    return []
  out = ['各环节耗时（各环节口径不同，不可相加；全流程不含关闭期）']
  for r in rows:
    if not r.get('可统计'):
      out.append(f'{r["环节"]}（{r["口径"]}）：无可统计的单')
      continue
    out.append(f'{r["环节"]}（{r["口径"]}）：可统计 {r["可统计"]} 单，'
               f'P50 {r.get("p50可读") or "—"}、P90 {r.get("p90可读") or "—"}')
  return out


def _flow_level_facts(rows: list | None) -> list[str]:
  """分等级时效：逐等级一句（审批/实施/验收 P50 + 按时率）。"""
  rows = rows or []
  if not rows:
    return []
  out = ['分等级时效（P50）']
  for r in rows:
    ot = _pct_text(r['按时率']) if r.get('按时率') is not None else '无法判定'
    out.append(f'{r["等级"]}：{r["单数"]} 单，审批 P50 '
               f'{_human_minutes(r.get("审批P50分钟"))}、实施 P50 '
               f'{_human_minutes(r.get("实施P50分钟"))}、验收 P50 '
               f'{_human_minutes(r.get("验收P50分钟"))}，按时率 {ot}')
  return out


def _flow_exception_facts(rows: list | None) -> list[str]:
  """流程异常率（当前状态快照，一张单可能同时落在多类）。"""
  rows = rows or []
  if not rows:
    return []
  return (['流程异常率（当前状态快照，同一张单可能同时落在多类）']
          + [f'{r["异常"]} {r["单数"]} 单（占 {r["占比"]}%）' for r in rows])


def _bands_facts(labels, counts, note: str) -> list[str]:
  """分档直方图（bins/counts 或 hist.labels/counts）→ **逐档一句**，档位与计数绑死。"""
  labels = labels or []
  counts = counts or []
  if not labels:
    return []
  return [f'{note}分布（共 {len(labels)} 档）'] + [
    f'{lab} {counts[i] if i < len(counts) else 0} 单' for i, lab in enumerate(labels)]


def _pair_facts(rows, label_key: str, value_key: str, note: str) -> list[str]:
  """`[{档位, 单数}]` 形状的分档 → 逐档一句（档位名与数值在原记录里已绑定）。"""
  rows = rows or []
  if not rows:
    return []
  return [f'{note}分布（共 {len(rows)} 档）'] + [
    f'{r.get(label_key)} {r.get(value_key)} 单' for r in rows]


def _level_dur_facts(ld: dict | None) -> list[str]:
  """级别 × 验收时长：各级分位/超时率 + 等级段对比。

  比较**全部算好**（与日趋势同一原则）：同一句里给出两段的 P50 差值与超时率，模型不需要
  做减法，也就不会把「3 小时 vs 40 小时」写反（2026-10-15 趋势章同类事故的解法）。
  """
  ld = ld or {}
  rows = ld.get('by_level') or []
  if not rows:
    return []
  def _rate(v) -> str:
    return f'{v}%' if v is not None else '无法判定'

  out = []
  for r in rows:
    out.append(f'{r["级别"]} {r["单数"]} 单（占 {r["占比"]}%），验收时长 P50 '
               f'{_human_minutes(r["P50分钟"])}、P90 {_human_minutes(r["P90分钟"])}，'
               f'验收超时 {r["超时单数"]} 单（{_rate(r["超时率"])}）')
  bands = ld.get('bands') or []
  for b in bands:
    out.append(f'{b["等级段"]}（{b["级别"]}）合计 {b["单数"]} 单（占 {b["占比"]}%），'
               f'P50 {_human_minutes(b["P50分钟"])}、P90 {_human_minutes(b["P90分钟"])}，'
               f'超时率 {_rate(b["超时率"])}')
  if len(bands) == 2:
    hi, lo = bands[0], bands[1]
    diff = round(abs(float(hi['P50分钟']) - float(lo['P50分钟'])), 1)
    word = '更长' if hi['P50分钟'] > lo['P50分钟'] else (
      '更短' if hi['P50分钟'] < lo['P50分钟'] else '相同')
    tail = '' if word == '相同' else f' {_human_minutes(diff)}'
    out.append(f'对比：{hi["等级段"]}的验收时长 P50 比 {lo["等级段"]} {word}{tail}；'
               f'验收超时率 {_rate(hi["超时率"])} vs {_rate(lo["超时率"])}')
  return out


def _hist_facts(accept: dict | None, spec) -> list[str]:
  """直方图：**逐档一句**（档位与计数绑在一起）；档位**全列**（含 0 单档，只有 10 档，全给最省事）。"""
  h = (accept or {}).get('hist') or {}
  labels, counts = h.get('labels') or [], h.get('counts') or []
  if not labels:
    return []
  out = [f'{spec.duration_label}区间分布（共 {len(labels)} 档）']
  out += [f'{lab} {counts[i] if i < len(counts) else 0} 单' for i, lab in enumerate(labels)]
  return out

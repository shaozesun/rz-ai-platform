"""正面结论 —— 由**数据证据**判定的「本期整体表现」（纯函数、0 次 LLM、不进沙箱）。

为什么需要：2026-10-27 做过一轮「语气去偏」，只解决了「不要把单量下降写成漏报/采集失效」，
报告全篇仍然只有负向结论（用户 2026-10-28：「我说的积极方向的评价呢，为什么一点都没有」）。
**提示词只能约束"不许编"，给不出"该夸什么"** —— 夸什么必须由平台按已有数据判定，
否则模型要么硬吹（下次被投诉），要么干脆不写（这次的情形）。

2026-10-29 用户第二次反馈「综合评价里先说积极的」→ 由 `evaluate()` 生成一段
**确定性结论文字**排在该章最前（亮点表格撤掉）：LLM 挂掉也照出，每句带数据锚点。

2026-10-29 用户第三次反馈「为什么就一句话，正面评价那么难吗，多说一些」→ 判据从 3 类扩到
**11 类**，按固定顺序拼接、**上限 5 句**（再多就变成"把每个指标都夸一遍"）。`sentences`
逐句给渲染层（每句一个 `<p>`），`summary` 是拼好的一段话（喂解读层与日志）。

**边界（用户已拍板）**：不恢复 A/B/C 处置评级（`positives.py` 已随该要求删除）。那套等级与
阈值是平台自己硬写的、不构成业务结论；本模块只陈述**已经算好的指标达标/持平事实**。

判据（全部取自 `stats` / `details`，**不新增取数、不新增 LLM 调用**；键缺失或分母为 0
一律**不出这一句**，一句都没命中就返回空串 —— 不硬夸）：

| # | 结论句 | 判据 |
|---|---|---|
| 1 | 单量规模 | 有上一期基数且环比 `delta <= 0`（与第 2 章「规模控制」同源），补日均 |
| 2 | 风险总体 | `details.risk_analysis.overall == '—'` 且有可用行（风险表里没有高/中项） |
| 3 | 诊断/处置及时 | 详情影响时长可用且「影响不足 1 小时」≥70%；否则退回 `scorecard` 的「响应时效」/「处理时效」≥80 分 |
| 4 | 闭环完成 | 未关闭占比 <10%（变更/问题/阈值）；故障用关闭率 ≥90% |
| 5 | 按时完成 | 未按时完成率 <5%（变更）、未按时执行率 <10%（阈值） |
| 6 | 高等级占比 | 1–2 级占比 ≤20%（**平台经验线**，`risks` 无对应项） |
| 7 | 重复管控 | 事件重复占比 <15%；故障/问题重复占比 <10% |
| 8 | 处置规范 | 驳回率 <10%；无驳回率的类型退回 `scorecard` 的「处置质量」/「闭环质量」≥80 分 |
| 9 | 紧急变更占比 | 紧急变更 <10% |
| 10 | 录入及时 | 问题单录入滞后 P90 <8 小时 |
| 11 | 详情填报完整度 | 处置总结无空填且「依据具体」占比 ≥60% |

**去重**：风险总句命中时**跳过全部「率值类」句子**（4 闭环 / 5 按时 / 7 重复 / 8 规范 /
9 紧急 / 10 录入）—— `overall == '—'` 的含义正是这些指标全部低于中风险阈值，再说一遍就是
同一件事说两遍；只保留 1 规模 / 3 时效 / 6 等级 / 11 填报。**闭环句命中时**规范句不再引用
「闭环质量」（故障的 `闭环质量 = 0.6×关闭率 + 0.4×非驳回`，与闭环句重复）。

**阈值同源**：除第 6 条外，新增阈值都是 `risks.py` 各类「中风险」线的镜像（不达标 ⟺ 风险表
记「中/高」）。`test_highlights` 用**边界用例**锁死这一对应关系：指标正好等于中阈值时，
`risks.evaluate` 必须出「中」行、而正面句**不得**出现 —— 两处阈值漂移时测试会失败。
注意第 5 条要按**未按时率**判，不能按「按时率 ≥85%」：变更的 `未按时 ≥5%` 就是「中」，
按 85% 写会出现「未按时完成率 10%（中风险）＋按时完成 90%，按期完成」这种自相矛盾。

`evaluate()` 返回行结构（`rows`，供解读层当依据）、逐句 `sentences` 与拼好的 `summary`。
纯标准库，服务端与单测都能直接调用。
"""

_MIN_SCORE = 80
_MIN_SHORT_PCT = 70
_MIN_REJECT = 10
_MIN_SPECIFIC_PCT = 60
_MAX_SCORE_ROWS = 2
_MAX_SENTENCES = 5
# 「规模控制」是单量方向的分数化表达，与「单量规模」同源，重复列等于同一个成绩说两遍
_SKIP_DIMS = ('规模控制',)
_EFFICIENCY_DIMS = ('响应时效', '处理时效')   # 事件用前者，故障/问题等用后者
_QUALITY_DIMS = ('处置质量', '闭环质量')      # 事件用前者，故障用后者（关闭率+非驳回加权）
# 「处置质量」（事件的 100 − 驳回率）与驳回率是同一件事的两种写法：有驳回率就既不列它、
# 也不拿它当达标依据（它的 80 分线＝驳回率 20%，会让「驳回率 10.8%」也写成「处置规范」）。
# 「闭环质量」是加权合成分，与驳回率不是同一口径，照列。
_REJECT_MIRROR_DIMS = ('处置质量',)
# 以下阈值 = risks 各类「中风险」线的镜像（见模块 docstring 的「阈值同源」）
_MIN_CLOSED_RATE = 90      # 故障关闭率：<80 高 / 80–90 中 / ≥90 不列
_MAX_OPEN_PCT = 10         # 未关闭占比：≥20 高 / 10–20 中 / <10 不列
# 按时率按「未按时率」判，与 `risks` 的阈值同量纲（按时 95% ⟺ 未按时 5%）
_LATE_MID_CHANGE = 5       # 变更未按时完成率：≥15 高 / 5–15 中
_LATE_MID_THRESHOLD = 10   # 阈值未按时执行率：≥20 高 / 10–20 中
_MAX_REPEAT_EVENT = 15     # 事件重复占比：≥30 高 / 15–30 中
_MAX_REPEAT = 10           # 故障/问题重复占比：≥20 高 / 10–20 中
_MAX_EMERGENCY = 10        # 紧急变更占比：≥20 高 / 10–20 中
_MAX_ENTRY_LAG = 8         # 录入滞后 P90（小时）：≥24 高 / 8–24 中
_MAX_HIGH_LEVEL = 20       # 1–2 级占比：**平台经验线**，risks 无对应项


def _num(value):
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _t(value) -> str:
  """数字文案：整数不带小数（100），其余保留一位（86.5）。"""
  num = round(float(value), 1)
  return f'{0.0 if num == 0 else num:g}'


def _pct_signed(value) -> str:
  """带符号百分比：「-6.7%」「+12%」（整数值不带小数，负零归 0）。"""
  num = round(float(value), 1)
  if num == 0:
    num = 0.0
  text = f'{num:+.1f}'
  if text.endswith('.0'):
    text = text[:-2]
  return text + '%'


def _row(name: str, display: str, basis: str) -> dict:
  return {'亮点项': name, '本期表现': display, '数据依据': basis}


def _volume(stats: dict, spec) -> dict | None:
  """单量规模：持平或下降视为正向（平台口径与 `common._scale_score` 同源）。"""
  mom = stats.get('mom') or {}
  total = _num(stats.get('total'))
  prev = _num(mom.get('prev_total') if mom.get('prev_total') is not None
              else stats.get('prev_total'))
  delta = _num(mom.get('delta'))
  # 没有上一期基数就不评判（"无对比也说改善"是编出来的成绩）
  if total is None or not prev or prev <= 0 or delta is None or delta > 0:
    return None
  unit = getattr(spec, 'count_unit', '起')
  avg = _num(stats.get('daily_avg'))
  head = f'{_t(total)} {unit}' + (f'，日均 {_t(avg)} {unit}' if avg is not None else '')
  if delta == 0:
    display = f'{head}，与上一期持平（{_t(prev)} {unit}）'
  else:
    pct = _num(mom.get('pct'))
    tail = f'（{_pct_signed(pct)}）' if pct is not None else ''
    display = f'{head}，较上一期减少 {_t(abs(delta))} {unit}{tail}'
  return _row('单量规模', display,
              '单量环比持平或下降视为正向（与第 2 章「规模控制」评分同源）')


def _impact(details: dict | None) -> dict | None:
  """业务影响：影响时长可用且「影响不足 1 小时」占比达 70%（当前仅事件单有该口径）。"""
  imp = (details or {}).get('impact') or {}
  if not imp.get('available'):
    return None
  short = _num(imp.get('short_pct'))
  if short is None or short < _MIN_SHORT_PCT:
    return None
  p50 = (imp.get('readable') or {}).get('p50') or ''
  tail = f'（P50 {p50}）' if p50 and p50 != '—' else ''
  return _row('业务影响', f'影响不足 1 小时的单占 {_t(short)}%{tail}',
              f'影响不足 1 小时的单占比 ≥{_MIN_SHORT_PCT}% 视为业务影响可控'
              '（影响时长取自工单详情，与验收时长是两个口径）')


def _scorecard(stats: dict) -> dict | None:
  """综合评分：本期 ≥80 分的维度（降序取前 2），带上上一期分数便于看是否改善。"""
  rows = (stats.get('scorecard') or {}).get('rows') or []
  hits = []
  for r in rows:
    dim = str(r.get('维度') or '')
    cur = _num(r.get('本期'))
    if not dim or dim in _SKIP_DIMS or cur is None or cur < _MIN_SCORE:
      continue
    prev = _num(r.get('上一期'))
    tail = f'（上一期 {_t(prev)} 分）' if prev is not None else ''
    hits.append((cur, f'{dim} {_t(cur)} 分{tail}'))
  if not hits:
    return None
  hits.sort(key=lambda x: -x[0])                 # 稳定排序：同分保留维度定义顺序
  return _row('综合评分', '、'.join(text for _, text in hits[:_MAX_SCORE_ROWS]),
              f'综合评分 ≥{_MIN_SCORE} 分视为达标（满分 100；「规模控制」见上一行）')


def _score_map(stats: dict) -> dict:
  """`scorecard.rows` → `{维度: 本期分数}`（缺分数或维度名的行直接跳过）。"""
  out = {}
  for r in (stats.get('scorecard') or {}).get('rows') or []:
    dim = str(r.get('维度') or '')
    cur = _num(r.get('本期'))
    if dim and cur is not None:
      out[dim] = cur
  return out


def _say_volume(stats: dict, spec) -> str:
  """「单量 188 起，日均 26.9 起，较上一期减少 13 起（-6.7%），规模可控」——与 `_volume` 同源。"""
  row = _volume(stats, spec)
  return f'单量 {row["本期表现"]}，规模可控' if row else ''


def _say_risk(details: dict | None) -> str:
  """风险总体：风险表里没有任何 高/中 项（`overall == '—'` 且有可用行）。

  命中时它**替代**全部「率值类」句子 —— 那些指标必然都已低于中风险阈值，展开说是同一件事。
  句子里带「核对了多少项」，否则它与「平台什么也没算」无法区分。
  """
  ra = (details or {}).get('risk_analysis') or {}
  rows = ra.get('rows') or []
  if not rows or ra.get('overall') != '—':
    return ''
  return f'质量与风险 {len(rows)} 项指标均未触及中、高风险阈值，运行状态平稳'


def _say_efficiency(stats: dict, details: dict | None) -> str:
  """效率：优先用影响时长（业务影响真实持续多久），没有该口径就退回时效类评分。"""
  row = _impact(details)
  if row:
    return f'{row["本期表现"]}，诊断、通报与现场处置及时'
  scores = _score_map(stats)
  hits = [(d, scores[d]) for d in _EFFICIENCY_DIMS
          if scores.get(d) is not None and scores[d] >= _MIN_SCORE]
  if not hits:
    return ''
  text = '、'.join(f'{d} {_t(s)} 分' for d, s in hits)
  word = ('响应与处置及时' if len(hits) > 1
          else ('响应及时' if hits[0][0] == '响应时效' else '处置及时'))
  return f'{text}，{word}'


def _say_closed(stats: dict, spec) -> str:
  """闭环完成：未关闭占比 <10%（变更/问题/阈值），故障用关闭率 ≥90%。"""
  ticket = getattr(spec, 'ticket', '')
  q = stats.get('quality') or {}
  total = _num(stats.get('total'))
  if ticket == 'breakdown':
    rate = _num(q.get('closed_rate'))
    if rate is None or rate < _MIN_CLOSED_RATE:
      return ''
    closed_n = _num(q.get('closed_n'))
    tail = (f'（{_t(closed_n)}/{_t(total)} 单）'
            if closed_n is not None and total else '')
    return f'关闭率 {_t(rate)}%{tail}，闭环完成良好'
  if ticket == 'question':
    closed_n, base = _num(q.get('close_n')), _num(q.get('close_base'))
    if closed_n is None or not base:
      return ''
    open_n = base - closed_n
    rate = round(open_n / base * 100, 1)
  elif ticket == 'threshold':
    open_n = _num(((stats.get('threshold_flow') or {}).get('open') or {}).get('n'))
    base = total
    if open_n is None or not base:
      return ''
    rate = round(open_n / base * 100, 1)
  else:                                          # change（其余类型无该指标）
    open_n, base = _num(q.get('open_n')), total
    if open_n is None or not base:
      return ''
    rate = round(open_n / base * 100, 1)
  if rate >= _MAX_OPEN_PCT:
    return ''
  return f'未关闭 {_t(open_n)} 单（{_t(rate)}%），闭环完成良好'


def _say_on_time(stats: dict, spec) -> str:
  """按时率：变更未按时 <5%（即按时 >95%）、阈值未按时 <10%（即按时 >90%）。

  按**未按时率**判是为了和 `risks` 的「未按时完成率」同一量纲：按时正好 95% 时风险表
  记「中」，这里就不该再出「按期完成」。
  """
  ticket = getattr(spec, 'ticket', '')
  if ticket == 'change':
    blk, late_mid, label, word = (stats.get('schedule') or {}, _LATE_MID_CHANGE,
                                  '变更按时完成', '按期完成')
  elif ticket == 'threshold':
    blk, late_mid, label, word = (stats.get('execution') or {}, _LATE_MID_THRESHOLD,
                                  '阈值调整按时执行', '按期执行')
  else:
    return ''
  rate = _num(blk.get('on_time_rate'))
  if rate is None or round(100 - rate, 1) >= late_mid:
    return ''
  n, base = _num(blk.get('on_time_n')), _num(blk.get('on_time_base'))
  tail = f' {_t(n)}/{_t(base)} 单' if n is not None and base else ''
  return f'{label}{tail}（{_t(rate)}%），{word}'


def _level_split(stats: dict) -> tuple[float, float] | None:
  """`(1–2 级单数, 全部单数)`；级别标签不是「N级」形态时返回 None。"""
  rows = stats.get('level') or []
  high = total = 0.0
  for r in rows:
    label = str(r.get('label') or '')
    count = _num(r.get('count'))
    if not label.endswith('级') or count is None:
      continue
    try:
      lv = int(label[:-1])
    except ValueError:
      continue
    total += count
    if lv <= 2:
      high += count
  if not total:
    return None
  return high, total


def _say_high_level(stats: dict, spec) -> str:
  """高等级占比：1–2 级 ≤20%（平台经验线，`risks` 无对应项）。"""
  ticket = getattr(spec, 'ticket', '')
  if ticket not in ('event', 'breakdown'):
    return ''
  split = _level_split(stats)
  if split is None:
    return ''
  high, total = split
  pct = round(high / total * 100, 1)
  if pct > _MAX_HIGH_LEVEL:
    return ''
  noun, unit = ('事件', getattr(spec, 'count_unit', '起')) if ticket == 'event' \
    else ('故障', getattr(spec, 'count_unit', '起'))
  if not high:
    return f'本期未出现 1–2 级高等级{noun}（{_t(total)} {unit}均在 3–4 级）'
  return f'1–2 级{noun} {_t(high)} {unit}（{_t(pct)}%），仍以 3–4 级为主'


def _say_repeat(stats: dict, spec) -> str:
  """重复管控：事件重复占比 <15%；故障/问题 <10%。"""
  ticket = getattr(spec, 'ticket', '')
  q = stats.get('quality') or {}
  if ticket == 'event':
    noun, floor = '重复类事件', _MAX_REPEAT_EVENT
  elif ticket == 'breakdown':
    noun, floor = '重复故障', _MAX_REPEAT
  elif ticket == 'question':
    noun, floor = '重复问题', _MAX_REPEAT
  else:
    return ''
  n, rate = _num(q.get('repeat_n')), _num(q.get('repeat_rate'))
  if n is None or rate is None or rate >= floor:
    return ''
  return f'{noun}涉及 {_t(n)} 单（{_t(rate)}%），同一项反复出现的不多'


def _say_norm(stats: dict, skip_dims: tuple[str, ...] = ()) -> str:
  """规范：驳回率低或质量维度达 80 分。

  **有驳回率时「处置质量」不参与判定**：它就是 `100 − 驳回率`，它的 80 分线等于驳回率
  20% —— 拿它当依据会出现「驳回率 10.8%（风险表记『中』），处置质量 89.2 分 → 处置规范」
  这种自相矛盾。有驳回率就只按驳回率（<10%）判。「闭环质量」是加权合成分，照常参与；
  但闭环句（`_say_closed`）已命中时它也要跳过 —— 故障的闭环质量就是关闭率与驳回率的加权。
  """
  scores = _score_map(stats)
  parts = []
  reject = _num((stats.get('quality') or {}).get('reject_rate'))
  has_reject = reject is not None
  if has_reject and reject < _MIN_REJECT:
    parts.append(f'驳回率 {_t(reject)}%')
  skip = tuple(_REJECT_MIRROR_DIMS if has_reject else ()) + tuple(
    d for d in _QUALITY_DIMS if d in skip_dims)
  parts += [f'{d} {_t(scores[d])} 分' for d in _QUALITY_DIMS
            if d not in skip and scores.get(d) is not None and scores[d] >= _MIN_SCORE]
  if not parts:
    return ''
  return f'{"、".join(parts)}，处置规范'


def _say_emergency(stats: dict, spec) -> str:
  """紧急变更占比 <10%（变更单专属）。"""
  if getattr(spec, 'ticket', '') != 'change':
    return ''
  ct = stats.get('change_type') or {}
  rate, n = _num(ct.get('emergency_rate')), _num(ct.get('emergency_n'))
  if rate is None or rate >= _MAX_EMERGENCY:
    return ''
  tail = f' {_t(n)} 单' if n is not None else ''
  return f'紧急变更{tail}（{_t(rate)}%），变更计划性强'


def _say_entry_lag(stats: dict, spec) -> str:
  """录入及时：问题单录入滞后 P90 <8 小时。"""
  if getattr(spec, 'ticket', '') != 'question':
    return ''
  p90 = _num((stats.get('entry_lag') or {}).get('p90'))
  if p90 is None or p90 >= _MAX_ENTRY_LAG:
    return ''
  return f'录入滞后 P90 {_t(p90)} 小时，建单及时'


def _say_detail_fill(details: dict | None) -> str:
  """详情填报：处置总结无空填，且带具体依据的单占 ≥60%（避免只挑好的一半说）。"""
  q = (details or {}).get('quality') or {}
  n, missing = _num(q.get('n')), _num(q.get('no_summary_n'))
  specific = _num(q.get('specific_n'))
  if not n or missing != 0 or specific is None:
    return ''
  pct = round(specific / n * 100, 1)
  if pct < _MIN_SPECIFIC_PCT:
    return ''
  return f'处置总结 {_t(n)} 单全部填写，其中带具体依据的单占 {_t(pct)}%'


def _sentences(stats: dict, details: dict | None, spec) -> list[str]:
  """按固定顺序拼句，去重后截到 `_MAX_SENTENCES` 句。"""
  risk = _say_risk(details)
  closed = '' if risk else _say_closed(stats, spec)
  seq = [
    _say_volume(stats, spec),
    risk,
    _say_efficiency(stats, details),
    closed,
    '' if risk else _say_on_time(stats, spec),
    _say_high_level(stats, spec),
    '' if risk else _say_repeat(stats, spec),
    '' if risk else _say_norm(stats, ('闭环质量',) if closed else ()),
    '' if risk else _say_emergency(stats, spec),
    '' if risk else _say_entry_lag(stats, spec),
    _say_detail_fill(details),
  ]
  return [s for s in seq if s][:_MAX_SENTENCES]


def evaluate(stats: dict, details: dict | None, spec) -> dict:
  """返回 `{'rows': [...], 'summary': str, 'sentences': [...]}`。

  无达标信号时 `rows` 为空、`sentences` 为空列表、`summary` 为空串。
  行序固定（规模 → 影响 → 评分），最多 3 行 —— 亮点是结论摘要，不是把每个指标都夸一遍。
  """
  stats = stats or {}
  rows = [r for r in (_volume(stats, spec), _impact(details), _scorecard(stats)) if r]
  sentences = _sentences(stats, details, spec)
  summary = ''.join(f'{s}。' for s in sentences)
  return {'rows': rows, 'summary': summary, 'sentences': sentences}

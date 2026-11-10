"""质量与风险分析 —— 把「需要排查 / 治理的点」变成确定性结论（纯函数，不进沙箱）。

为什么需要：报告定位已改成「运行评价」（不硬找问题、建议不必凑数），但用户明确
**不是不做建议** —— 建议应当由**有数据证据的风险项**驱动，而不是每章硬凑一句。
本模块按各工单类型的关键指标分级（高 / 中），并给出「建议排查方向」，
让「综合评价」章的改进项能对应到具体风险项。

2026-10-29：事件/故障原先把「质量三项」单独成章，与本章是同一组指标的两份视图
（用户：「告警质量和风险那不都是一个维度吗」）→ 质量三项由 `_quality_rows` **并入本章**，
一张表同时给「指标 / 本期表现 / 口径 / 等级 / 判据 / 建议排查方向」。
质量行**恒列**（未达阈值等级记「—」，指标值不隐藏）；额外风险项接在其后。

本模块**只算不编**：

- 全部取自 `stats` 里已算好的率值与计数（不重读原始记录、不新增取数）；
- 每条给出 `本期表现 + 等级 + 判据`，**判据原文印进报告**，读者可自行复核；
- 数据不足（键缺失、分母为 0）就**不出这一行**；不猜、不给 0；
- 质量行按声明顺序固定排列；额外风险项高等级排前、最多 2 行 —— 风险分析是提示，
  不是把每个维度都罗列一遍。

纯标准库，服务端与单测都能直接调用（沙箱里用不到它）。
"""

_LEVEL_ORDER = {'高': 2, '中': 1}
_MAX_ROWS = 4          # 没有质量行的类型（变更/问题/阈值）：风险项上限
_EXTRA_MAX_ROWS = 2    # 有质量行的类型（事件/故障）：质量行之外的风险项上限

# 质量行的阈值与排查方向：`spec.quality_rows` 只给 (rate_key, 标签, 口径)，阈值留在这里，
# 避免「口径文案在一处、阈值在另一处」两处漂移。kind: 'rate' 越高越差 / 'rate_low' 越低越差。
_QUALITY_RULES = {
  'event': {
    'timeout_rate': (50, 20, 'rate', '核对超时单集中在哪些园区与级别，复核验收时限设置与流程节点'),
    'reject_rate': (20, 10, 'rate', '复盘驳回原因与验收标准是否对齐，减少返工'),
    'repeat_rate': (30, 15, 'rate',
                    '对高频重复告警做阈值与去重规则专项排查，区分真实反复与推送重复'),
  },
  'breakdown': {
    'over_1d_rate': (40, 20, 'rate',
                     '按设备类型与部门核对长时间未修复的卡点，复核备件与派单机制'),
    'reject_rate': (20, 10, 'rate', '复盘驳回原因与验收标准是否对齐，减少返工'),
    'closed_rate': (80, 90, 'rate_low', '核对未关闭单的阶段与阻塞原因，明确关单标准'),
  },
}


def _t(value) -> str:
  """数字文案：整数不带小数（50%），其余保留一位（12.5%）。"""
  num = round(float(value), 1)
  return f'{0.0 if num == 0 else num:g}'


def _share(n, base):
  """占比（%）：一位小数，整数值不带小数；分母无效返回 None。"""
  if n is None or not base:
    return None
  value = round(int(n) / int(base) * 100, 1)
  return int(value) if float(value).is_integer() else value


def _num(value):
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _rule(high: float, mid: float, unit: str = '%') -> str:
  """越高越差：判据文案由同一组阈值生成，等级必然可复核。"""
  return f'≥{_t(high)}{unit} 高 / {_t(mid)}–{_t(high)}{unit} 中 / <{_t(mid)}{unit} 不列'


def _rule_low(high: float, mid: float, unit: str = '%') -> str:
  """越低越差（如按时率、关闭率）。"""
  return f'<{_t(high)}{unit} 高 / {_t(high)}–{_t(mid)}{unit} 中 / ≥{_t(mid)}{unit} 不列'


def _level_high(value, high: float, mid: float):
  num = _num(value)
  if num is None:
    return None
  if num >= high:
    return '高'
  return '中' if num >= mid else None


def _level_low(value, high: float, mid: float):
  """越低越差（关闭率这类）：<high 高 / high–mid 中 / ≥mid 不列。"""
  num = _num(value)
  if num is None:
    return None
  if num < high:
    return '高'
  return '中' if num < mid else None


def _row(name: str, display: str, level, rule: str, action: str) -> dict | None:
  if not level:
    return None
  return {'风险项': name, '本期表现': display, '等级': level,
          '判据': rule, '建议排查方向': action}


def _rate_row(name, rate, high, mid, action, display=None):
  """率值行（`rate` 已是百分数）。"""
  num = _num(rate)
  level = _level_high(num, high, mid)
  if not level:
    return None
  return _row(name, display if display is not None else f'{_t(num)}%',
              level, _rule(high, mid), action)


def _share_row(name, n, base, high, mid, action, tail: str = ''):
  """计数行（`n / base` 折算占比）。"""
  pct = _share(n, base)
  level = _level_high(pct, high, mid)
  if not level:
    return None
  return _row(name, f'{n} / {base}（{_t(pct)}%）{tail}',
              level, _rule(high, mid), action)


def _closed_shortfall(name, closed_n, base, high, mid, action):
  """闭环率不足：未关闭占比 = 100 − 关闭率（越高越差）。"""
  if closed_n is None or not base:
    return None
  pct = _share(int(base) - int(closed_n), base)
  level = _level_high(pct, high, mid)
  if not level:
    return None
  return _row(name, f'{int(base) - int(closed_n)} / {base}（{_t(pct)}%）未关闭',
              level, _rule(high, mid), action)


def _repeat_top(repeat) -> str:
  """重复项里次数最多的一条（用于把风险落到具体告警/问题名）。"""
  rows = repeat if isinstance(repeat, list) else []
  candidates = [r for r in rows if isinstance(r, dict)]
  if not candidates:
    return ''
  top = max(candidates, key=lambda r: int(r.get('count') or 0))
  name = str(top.get('name') or top.get('问题') or top.get('项目') or '')
  count = top.get('count')
  return f'，最集中为「{name}」{count} 次' if name and count else ''


def _quality_rows(stats: dict, spec) -> list[dict]:
  """`spec.quality_rows` 声明的质量项 → 表格行（**恒列**，未达阈值等级为「—」）。

  取值与口径都在 `stats['quality']`（`_n` 为计数、`_base` 为分母，缺则用总量），
  阈值/排查方向在 `_QUALITY_RULES`。**只有率值缺失才整行不印** —— 低于阈值不是缺失，
  读者仍需要看到实际水平。
  """
  q = stats.get('quality') or {}
  total = stats.get('total')
  rules = _QUALITY_RULES.get(getattr(spec, 'ticket', '')) or {}
  out = []
  for rate_key, label, desc in getattr(spec, 'quality_rows', ()) or ():
    rule = rules.get(rate_key)
    if not rule:
      continue
    rate = _num(q.get(rate_key))
    if rate is None:
      continue
    high, mid, kind, action = rule
    n = q.get(rate_key.replace('_rate', '_n'))
    base = q.get(rate_key.replace('_rate', '_base')) or total
    note = desc
    if n is not None and base:
      note += f'（{n} / {base} 单）'
    if rate_key == 'repeat_rate':
      note += _repeat_top(stats.get('repeat'))
    if kind == 'rate_low':
      level, rule_text = _level_low(rate, high, mid), _rule_low(high, mid)
    else:
      level, rule_text = _level_high(rate, high, mid), _rule(high, mid)
    out.append({'风险项': label, '本期表现': f'{_t(rate)}%', '口径': note,
                '等级': level or '—', '判据': rule_text, '建议排查方向': action})
  return out


def _build_breakdown(stats: dict) -> list[dict]:
  """故障单的**补充**风险项：质量三项（超 1 天未处理完 / 驳回率 / 关闭率）已由
  `_quality_rows` 承载，这里只补它没覆盖的两项。"""
  quality = stats.get('quality') or {}
  risk = stats.get('risk') or {}
  total = stats.get('total')
  high_open = _num(risk.get('high_open_n'))
  high_level = None
  if high_open is not None:
    high_level = '高' if high_open >= 5 else ('中' if high_open >= 1 else None)
  return [
    _share_row('重复故障占比', quality.get('repeat_n'), total, 20, 10,
               '对同一设备类型 × 发生地点的重复故障做专项排查，评估根治方案',
               _repeat_top(stats.get('repeat'))),
    _row('高风险未关闭故障', f'{int(high_open)} 单' if high_open is not None else '',
         high_level, '≥5 单 高 / 1–4 单 中 / 0 单 不列',
         '优先清理高等级且未关闭的故障单，评估是否存在处置停滞或复发'),
  ]


def _build_change(stats: dict) -> list[dict]:
  change_type = stats.get('change_type') or {}
  schedule = stats.get('schedule') or {}
  quality = stats.get('quality') or {}
  total = stats.get('total')
  on_time = _num(schedule.get('on_time_rate'))
  late_rate = None if on_time is None else round(100 - on_time, 1)
  return [
    _rate_row('紧急变更占比', change_type.get('emergency_rate'), 20, 10,
              '核对紧急变更的触发原因，复查是否可通过计划性变更前置消化'),
    _rate_row('未按时完成率', late_rate, 15, 5,
              '核对延期集中在哪些类型与园区，复核计划时长与资源安排'),
    _closed_shortfall('未关闭工单', total - (quality.get('open_n') or 0)
                      if total and quality.get('open_n') is not None else None,
                      total, 20, 10, '核对未关闭变更的阻塞阶段，明确关闭条件'),
    _rate_row('审批超时率', quality.get('approve_timeout_rate'), 10, 5,
              '核对审批链条上的等待节点，评估审批时限与前置条件'),
  ]


def _build_question(stats: dict) -> list[dict]:
  aging = stats.get('aging') or {}
  entry = stats.get('entry_lag') or {}
  quality = stats.get('quality') or {}
  over_label = str(aging.get('over_label') or '')
  name = f'存量老化（超 {over_label}）' if over_label else '存量老化'
  lag = _num(entry.get('p90'))
  lag_level = _level_high(lag, 24, 8)
  return [
    _rate_row(name, aging.get('over_rate'), 30, 15,
              '核对超期未关闭问题的对象与责任部门，评估关闭标准与督办机制',
              display=(f'{aging.get("over_n")} / {aging.get("open_n")} 单未关闭'
                       f'（{_t(aging.get("over_rate"))}%）'
                       if aging.get('over_rate') is not None else None)),
    _row('录入滞后 P90', f'P90 {_t(lag)} 小时' if lag is not None else '',
         lag_level, '≥24 小时 高 / 8–24 小时 中 / <8 小时 不列',
         '核对录入滞后集中在哪些来源与部门，评估问题发现到建单的交接流程'),
    _closed_shortfall('未关闭工单', quality.get('close_n'),
                      quality.get('close_base'), 20, 10,
                      '核对未关闭问题的处置停滞点，明确关闭判定与复核'),
    _share_row('重复问题占比', quality.get('repeat_n'), quality.get('repeat_base'),
               20, 10, '对重复出现的问题做专项排查，评估是否存在未根治的共性隐患',
               _repeat_top(stats.get('repeat'))),
  ]


def _build_threshold(stats: dict) -> list[dict]:
  execution = stats.get('execution') or {}
  quality = stats.get('quality') or {}
  flow = stats.get('threshold_flow') or {}
  batch = stats.get('batch') or {}
  initiator = stats.get('initiator') or {}
  total = stats.get('total')
  on_time = _num(execution.get('on_time_rate'))
  late_rate = None if on_time is None else round(100 - on_time, 1)
  open_n = ((flow.get('open') or {}).get('n'))
  batch_max = _num(batch.get('max'))
  batch_level = _level_high(batch_max, 20, 10)
  return [
    _rate_row('未按时执行率', late_rate, 20, 10,
              '核对未按期执行的调整集中在哪些专业与园区，复核计划与实施窗口'),
    _share_row('审批驳回率', quality.get('reject_n'), total, 20, 10,
               '复盘驳回原因与申请材料规范，减少反复提交'),
    _share_row('未关闭工单', open_n, total, 20, 10,
               '核对未关闭调整所处的审批/实施阶段，明确关闭条件'),
    _row('最大批量调整规模', f'{int(batch_max)} 处并列' if batch_max is not None else '',
         batch_level, '≥20 处 高 / 10–19 处 中 / <10 处 不列',
         '核对批量调整是否走变更评审与分批实施，评估影响面'),
    _rate_row('发起人首位集中度', initiator.get('top_share'), 50, 30,
              '核对高频发起人的调整依据与复核机制，评估是否存在集中操作风险'),
  ]


_BUILDERS = {
  'breakdown': _build_breakdown,
  'change': _build_change,
  'question': _build_question,
  'threshold': _build_threshold,
}


def evaluate(stats: dict, spec) -> dict:
  """返回 `{'rows': [...], 'overall': '高|中|—'}`。

  `rows` = 质量行（恒列，按 `spec.quality_rows` 声明顺序）+ 补充风险项（高在前、
  最多 `_EXTRA_MAX_ROWS` 行）。**质量行不占**补充项名额 —— 它们是五类报告各自固定的
  指标视图，不该被风险项的排序挤掉。
  `rows` 为空表示本期没有任何指标可算（不代表不存在风险），渲染层给一句说明。
  """
  stats = stats or {}
  quality = _quality_rows(stats, spec)
  builder = _BUILDERS.get(getattr(spec, 'ticket', ''))
  extras = [r for r in (builder(stats) if builder else []) if r]
  extras.sort(key=lambda r: -_LEVEL_ORDER.get(r['等级'], 0))   # 稳定排序：同级保留定义顺序
  cap = _EXTRA_MAX_ROWS if quality else _MAX_ROWS
  rows = quality + extras[:cap]
  levels = [r['等级'] for r in rows if r['等级'] in _LEVEL_ORDER]
  overall = '高' if '高' in levels else ('中' if '中' in levels else '—')
  return {'rows': rows, 'overall': overall}

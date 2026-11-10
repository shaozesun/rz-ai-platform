"""事件「影响时长」分析（**详情口径**，纯函数、零 LLM、零 I/O）。

与第 4 章的「验收时长」是**两个口径**，绝不能互相换算：
验收时长来自列表 `checkEndApprove.approveTime`（流程走完用了多久，全量可取）；
影响时长来自详情 `influenceDuration`（业务影响持续多久，**只有详情接口有**）。

单位口径（2026-10-28 只读探测实测，别再猜）：平台给的是**已排好的可读串**，
形如 `'0天0小时3分钟'` / `'0天 0小时 19分钟'`（空格有无不稳定），不是毫秒也不是分钟数，
故按「天/小时/分钟/秒」逐项累加成分钟。

形态照 `risks.py`：服务端纯函数，输入已取到的详情行，输出结构化结果，
渲染与解读各自取用；样本不足时**降级**为一句说明，不硬出分布。
"""

import re

from service.scheduled_task.report.causes import CAUSE_KEYWORDS, classify
from service.scheduled_task.report.common import _human_minutes, _pct

# 影响时长的最短有效样本：少于此数不出分布（三条以下的"分布"只有形状、没有信息）
MIN_VALID = 3

# 影响时长档位 **5 档**（与验收时长的 6 档刻意不同，别改回共用一套）：
# 影响时长与验收时长是两个口径、本就不能互相换算，「共用档位便于对照」这个理由不成立；
# 而多出来的那档正好卡在 24 时上，一档一色才不会出现两档同色（用户 2026-10-29 指定 5 色）。
_IMPACT_LABELS = ['≤30分', '30分-2时', '2-8时', '8-24时', '>1天']
_IMPACT_EDGES = [0, 30, 120, 480, 1440]        # 左闭右开；>1440 分钟落最后一档


def _impact_bin(minutes: float) -> str:
  """分钟 → 档位标签（`_IMPACT_LABELS` 与 `_IMPACT_EDGES` 同序）。"""
  idx = 0
  for i, edge in enumerate(_IMPACT_EDGES):
    if minutes >= edge:
      idx = i
  return _IMPACT_LABELS[idx]

# 长尾单展示条数与各字段截断（PDF 表格不能撑爆）
LONG_TAIL_TOP = 5
_TRUNC = {'原因摘要': 120, '过程摘要': 200, '结果摘要': 80}

_UNITS = (('天', 1440.0), ('小时', 60.0), ('分钟', 1.0), ('秒', 1 / 60.0))


def _parse_minutes(text) -> float | None:
  """`'0天 0小时 19分钟'` → 19.0（分钟）。认不出返回 None，**不猜成 0**。

  0 是"影响很短"这一真实取值，与"字段没填/写的是别的东西"必须区分开，
  否则缺失会被算进最短那一档、把「多数事件影响很短」的结论带偏。
  """
  if not isinstance(text, str) or not text.strip():
    return None
  total = 0.0
  hit = False
  for unit, scale in _UNITS:
    m = re.search(r'(\d+(?:\.\d+)?)\s*' + unit, text)
    if m:
      total += float(m.group(1)) * scale
      hit = True
  return round(total, 1) if hit else None


def _quantile(values: list, q: float) -> float | None:
  """线性插值分位（与 pandas `Series.quantile` 默认口径一致，便于和第 4 章并读）。"""
  vals = sorted(float(v) for v in values if v is not None)
  if not vals:
    return None
  pos = (len(vals) - 1) * q
  lo, hi = int(pos), min(int(pos) + 1, len(vals) - 1)
  return round(vals[lo] + (vals[hi] - vals[lo]) * (pos - lo), 1)


def _level_of(raw) -> str:
  """级别字段 → `'3级'`（详情里是 `'3'`/`3`/`'3级'` 都可能，取首个数字）。"""
  m = re.search(r'\d+', str(raw or ''))
  return f'{m.group(0)}级' if m else ''


def _clip(value, limit: int) -> str:
  text = '' if value is None else str(value).strip()
  return text[:limit]


def _impact_rows(details: dict) -> list[dict]:
  """详情行 → 只留有影响时长可用的解析结果（按影响时长降序）。"""
  out = []
  for item in (details or {}).get('rows', []) or []:
    fields = item.get('fields') or {}
    minutes = _parse_minutes(fields.get('影响时长'))
    if minutes is None:
      continue
    out.append({
      'ticketNo': str(item.get('ticketNo') or '').strip(),
      'minutes': minutes,
      'raw': str(fields.get('影响时长') or '').strip(),
      'level': _level_of(fields.get('级别')),
      'fields': fields,
    })
  return out


def _hist(minutes: list) -> dict:
  counts = [0] * len(_IMPACT_LABELS)
  for m in minutes:
    counts[_IMPACT_LABELS.index(_impact_bin(m))] += 1
  return {'labels': list(_IMPACT_LABELS), 'counts': counts}


def _by_level(rows: list[dict]) -> dict:
  """级别 × 影响时长档矩阵 + 各级分位（无级别的行不进本表，仍计入整体分布）。"""
  levels = sorted({r['level'] for r in rows if r['level']})
  if not levels:
    return {'levels': [], 'bins': [], 'matrix': [], 'stats': []}
  matrix = [[0] * len(_IMPACT_LABELS) for _ in levels]
  for r in rows:
    if r['level']:
      matrix[levels.index(r['level'])][_IMPACT_LABELS.index(_impact_bin(r['minutes']))] += 1
  level_rows = [r for r in rows if r['level']]
  stats = []
  for level in levels:
    vals = [r['minutes'] for r in rows if r['level'] == level]
    stats.append({'级别': level, '单数': len(vals),
                  '占比': _pct(len(vals), len(level_rows)),
                  'P50分钟': _quantile(vals, 0.5), 'P90分钟': _quantile(vals, 0.9),
                  '最长分钟': max(vals)})
  return {'levels': levels, 'bins': list(_IMPACT_LABELS), 'matrix': matrix,
          'stats': stats}


def analyze(details: dict | None) -> dict:
  """详情行 → 影响时长分布 / 级别矩阵 / 长尾单 / 成因归类。

  返回 `available=False` 时只有 `valid/n/reason`，渲染与解读据此退化成一句说明。
  """
  rows_all = [r for r in ((details or {}).get('rows') or []) if (r.get('fields') or {})]
  rows = _impact_rows(details)
  n = len(rows_all)
  if len(rows) < MIN_VALID:
    return {'available': False, 'valid': len(rows), 'n': n,
            'reason': f'影响时长字段仅 {len(rows)}/{n} 单有值，不足以做分布分析'}

  minutes = [r['minutes'] for r in rows]
  p50 = _quantile(minutes, 0.5)
  p90 = _quantile(minutes, 0.9)
  mx = max(minutes)
  long_tail = sorted(rows, key=lambda r: r['minutes'], reverse=True)[:LONG_TAIL_TOP]
  return {
    'available': True, 'n': n, 'valid': len(rows), 'missing': n - len(rows),
    'hist': _hist(minutes),
    'p50': p50, 'p90': p90, 'max': mx, 'mean': round(sum(minutes) / len(minutes), 1),
    'readable': {'p50': _human_minutes(p50), 'p90': _human_minutes(p90),
                 'max': _human_minutes(mx)},
    'short_pct': _pct(sum(1 for m in minutes if m < 60), len(minutes)),
    'by_level': _by_level(rows),
    'long_tail': [{
      '编号': r['ticketNo'], '事件名称': _clip(r['fields'].get('事件名称'), 60),
      '级别': r['level'] or '—', '影响时长': r['raw'] or _human_minutes(r['minutes']),
      '原因摘要': _clip(r['fields'].get('原因分析'), _TRUNC['原因摘要']),
      '过程摘要': _clip(r['fields'].get('处理过程'), _TRUNC['过程摘要']),
      '结果摘要': _clip(r['fields'].get('处理结果'), _TRUNC['结果摘要']),
    } for r in long_tail],
    'causes': classify(details, CAUSE_KEYWORDS),
  }

"""日趋势的**确定性拦截闸**：只丢弃「能证明为假」的大小关系/差值/倍数句子。

为什么需要它：`facts._trend_facts` 已经把逐日数值、增减量、排序与峰谷算好并编号，
提示词也禁止模型自行比较；但 LLM 输出仍可能自行组织比较（2026-10-15 事故：
10-11 的 5 起被写成「高于 10-05 的 6 起」）。「依据」校验只查编号是否存在，
查不出数值关系真假，所以再加一道纯规则闸。

**保守原则**：只拦「双方都能解析成具体数值且关系确定相反」的句子。
解析不到日期、日期不在本期窗口、区间/百分比、上一期无对位数据 —— 一律放行。
宁可漏拦，不可误杀（报告少一句话可以接受，删掉正确结论不行）。

本模块**不依赖 pandas**，可在服务端进程与单测里直接调用（不进沙箱）。
"""

import re
import unicodedata

_DATE = r'(?:\d{1,2}月\d{1,2}[日号]|\d{1,2}[-/]\d{1,2})'
_UNIT = r'[起单个]'

_DATE_RE = re.compile(
  r'(?:(?P<y>\d{4})年)?(?P<m>\d{1,2})月(?P<d>\d{1,2})[日号]'
  r'|(?<!\d)(?P<m2>\d{1,2})[-/](?P<d2>\d{1,2})(?!\d)')

# 关系词：注意「高于/低于」是**严格**关系；「持平/相当」不在此列（容易误判，放行）
_REL_RE = re.compile(r'高于|超过|多于|大于|超出|领先|低于|少于|小于|不及|弱于')
_GT = {'高于', '超过', '多于', '大于', '超出', '领先'}

# 关系词后紧跟的锚点（比日期优先）
_ANCHOR_RE = re.compile(
  r'^(本期峰值|峰值|本期最高|最高|本期低谷|低谷|本期最低|最低|上一期同日|上一期|上期)')
_PEAK_ANCHORS = {'本期峰值', '峰值', '本期最高', '最高'}
_LOW_ANCHORS = {'本期低谷', '低谷', '本期最低', '最低'}

# 方向词表与判定集合**同源**：正则从词表拼出，避免「加进正则忘了加进集合」→ 每句都判违规。
# 含「骤减/激增」这类强化词：模型常写「较上一期同日14起骤减12起」，不认就漏拦。
_UP_WORDS = ('增加', '上升', '增长', '多', '骤增', '激增')
_DOWN_WORDS = ('减少', '下降', '降低', '少', '骤减', '锐减', '回落', '下滑')

# **方向词必填**（2026-10-30）：紧跟在「较上一期同日」后的数字，没有方向词时它是**基数**
# 而不是差值。旧写法把方向词设为可选组，`10-17单日2起较上一期同日14起骤减12起` 会把 14
# 当差值、判「实际 -12，文中 14」—— 生产日志里出现过同形态的误杀。
_SAME_DELTA_RE = re.compile(
  rf'较(?:上一期|上期)(?:同日|同位置|同期)(?:的)?\s*(?:\d+\s*{_UNIT}\s*)?'
  rf'(?P<dir>{"|".join(_UP_WORDS + _DOWN_WORDS)})\s*(?P<n>\d+)\s*{_UNIT}')

_DELTA_RE = re.compile(
  rf'(?:较|相比|比)\s*(?P<d>{_DATE})\s*(?:的)?\s*(?P<base>\d+)\s*{_UNIT}\s*'
  rf'(?P<dir>增加|减少|上升|下降|多|少)\s*(?P<n>\d+)\s*{_UNIT}')
_BARE_DELTA_RE = re.compile(
  rf'(?:比|较|相比)\s*(?P<d>{_DATE})\s*(?P<dir>多|少|增加|减少|高|低)\s*'
  rf'(?P<n>\d+)\s*{_UNIT}')

# 峰谷归属只认**紧邻**的搭配（「10-05 单量最多」「10-05 为峰值」「峰值出现在 10-05」）；
# 不能允许中间任意文字 —— 否则「虽低于本期峰值但也低于 10 月 5 日」会被误读成
# 「峰值是 10 月 5 日」（实测误杀）。
_PEAK_RE = re.compile(
  rf'(?:(?P<d1>{_DATE})(?:的)?(?:单量|工单量|数量|事件量|故障量|问题量|变更量|阈值工单量)?'
  rf'(?:为|是|达到|达|排在)?(?P<k1>最高|最多|峰值|最大|最低|最少|低谷|最小)'
  rf'|(?P<k2>最高|最多|峰值|最大|最低|最少|低谷|最小)(?:的)?(?:为|是|出现在|落在)'
  rf'(?P<d2>{_DATE}))')
_PEAK_WORDS = {'最高', '最多', '峰值', '最大'}
_LOW_WORDS = {'最低', '最少', '低谷', '最小'}
_PEAK_SKIP = ('时段', '小时', '分钟', '时刻', '区间')

_MULT_RE = re.compile(
  r'(?P<fan>翻\s*(?P<cn>[0-9一二三四五六七八九十百]+)\s*倍)'
  r'|(?P<num>\d+(?:\.\d+)?)\s*倍|(?P<double>翻倍|翻番)')

# 没有日期锚点时，只有句子**明说在比总量**才允许拿 `total/prev_total` 核对倍数。
# 否则任何「N 倍」都会被当成日单量之比 —— 2026-10-24 误杀实测：
# 「A3 分部审批 P50 … 是全场均值的 7.5 倍」被比成「实际 1.27 倍」（56/44，日趋势总量）。
_TOTAL_HINT_RE = re.compile(r'总量|总单量|合计|整体|全期|总数')

_UP = set(_UP_WORDS) | {'高'}
_DOWN = set(_DOWN_WORDS) | {'低'}


def _norm(text: str) -> str:
  """全角数字/标点归一 + 去掉空白（模型常写「10 月 5 日」）。"""
  s = unicodedata.normalize('NFKC', str(text or ''))
  return re.sub(r'\s+', '', s)


def _date_key(mo, da):
  return (int(mo), int(da))


def _date_positions(text: str) -> list[tuple[int, int, tuple[int, int]]]:
  """归一化文本里的日期提及 → `[(start, end, (月, 日))]`（按出现顺序）。"""
  out = []
  for m in _DATE_RE.finditer(text):
    if m.group('m'):
      out.append((m.start(), m.end(), _date_key(m.group('m'), m.group('d'))))
    else:
      out.append((m.start(), m.end(), _date_key(m.group('m2'), m.group('d2'))))
  return out


def build_index(trend: dict | None, unit: str = '起') -> dict:
  """把 `stats['trend']` 变成按 (月, 日) 查值的小索引；无趋势返回 `{}`（等于关闭闸门）。"""
  t = trend or {}
  labels = [str(x) for x in (t.get('labels') or [])]
  cur = [int(v) for v in (t.get('cur') or [])]
  prev = [int(v) for v in (t.get('prev') or [])]
  prev_days = [str(x) for x in (t.get('prev_days') or [])]
  values: dict = {}
  for i, lab in enumerate(labels):
    m = _DATE_RE.search(_norm(lab))
    if not m:
      continue
    key = _date_key(m.group('m2') or m.group('m'), m.group('d2') or m.group('d'))
    has_prev = i < len(prev_days) and bool(prev_days[i])
    values[key] = {
      'label': lab,
      'cur': cur[i] if i < len(cur) else 0,
      'prev': (prev[i] if i < len(prev) else 0) if has_prev else None,
    }
  if not values:
    return {}
  cur_all = [v['cur'] for v in values.values()]
  prev_vals = [v['prev'] for v in values.values() if v['prev'] is not None]
  return {
    'unit': unit,
    'values': values,
    'peak': max(cur_all),
    'low': min(cur_all),
    'total': sum(cur_all),
    'prev_total': sum(prev_vals) if prev_vals else None,
  }


def _segments(text: str) -> list[str]:
  """按句号/分号/换行切句：跨句的「它/该日」无法可靠绑定，不猜。"""
  return [seg for seg in re.split(r'[。！？；;\n]', text) if seg]


def _value(index: dict, key) -> int | None:
  item = index['values'].get(key)
  return None if item is None else item['cur']


def _prev_value(index: dict, key) -> int | None:
  item = index['values'].get(key)
  return None if item is None else item['prev']


def _check_size(seg: str, dates, index: dict) -> list[str]:
  """规则 1：跨日期大小比较（高于/低于…）。主语日期必须**唯一**，否则不猜。"""
  bad = []
  for m in _REL_RE.finditer(seg):
    rel = m.group(0)
    before = [d for d in dates if d[1] <= m.start()]
    if len(before) != 1:              # 0 个或多个候选 → 主语不明确，放行
      continue
    subj_key = before[0][2]
    subj = _value(index, subj_key)
    if subj is None:
      continue
    tail = seg[m.end():m.end() + 12]
    anchor = _ANCHOR_RE.match(tail)
    if anchor:
      name = anchor.group(1)
      if name in _PEAK_ANCHORS:
        obj = index['peak']
      elif name in _LOW_ANCHORS:
        obj = index['low']
      else:                            # 「上一期/上一期同日」→ 同一天的上期值
        obj = _prev_value(index, subj_key)
        if obj is None:
          continue
    else:
      after = [d for d in dates if d[0] >= m.end()]
      if not after:
        continue
      between = seg[m.end():after[0][0]]
      if _REL_RE.search(between):      # 中间又出现关系词，归属不确定
        continue
      obj = _value(index, after[0][2])
      if obj is None:
        continue
    if rel in _GT and not subj > obj:
      bad.append(f'「{rel}」与数值相反（{subj} vs {obj}）')
    elif rel not in _GT and not subj < obj:
      bad.append(f'「{rel}」与数值相反（{subj} vs {obj}）')
  return bad


def _check_same_delta(seg: str, dates, index: dict) -> list[str]:
  """规则 2：「较上一期同日增加/减少 N」的差值校验（方向词必填，见 `_SAME_DELTA_RE`）。

  兜底分支 `else: abs(actual) == n` 留着防词表漂移：万一有方向词只在正则里、不在
  `_UP/_DOWN` 里，退化成「绝对差值对得上就放行」—— 宁可漏拦，不可误杀。
  """
  bad = []
  for m in _SAME_DELTA_RE.finditer(seg):
    before = [d for d in dates if d[1] <= m.start()]
    if not before:
      continue
    key = before[-1][2]               # 就近取本句最近提到的日期
    cur_v, prev_v = _value(index, key), _prev_value(index, key)
    if cur_v is None or prev_v is None:
      continue
    n = int(m.group('n'))
    direction = m.group('dir')
    actual = cur_v - prev_v
    if direction in _UP:
      ok = actual == n
    elif direction in _DOWN:
      ok = actual == -n
    else:
      ok = abs(actual) == n
    if not ok:
      bad.append(f'「较上一期同日」差值不符（实际 {actual:+d}，文中 {direction or ""}{n}）')
  return bad


def _check_delta(seg: str, dates, index: dict) -> list[str]:
  """规则 3：显式做差（「较 10-05 的 6 起减少 1 起」/「比 10-05 少 1 起」）。"""
  bad = []
  for m in _DELTA_RE.finditer(seg):
    dm = _DATE_RE.search(m.group('d'))
    if not dm:
      continue
    key = _date_key(dm.group('m2') or dm.group('m'), dm.group('d2') or dm.group('d'))
    ref = _value(index, key)
    if ref is None:
      continue
    base = int(m.group('base'))
    if base != ref:
      bad.append(f'引用的「{m.group("d")}」值不符（文中 {base}，实际 {ref}）')
      continue
    n = int(m.group('n'))
    before = [d for d in dates if d[1] <= m.start()]
    if not before:
      continue
    subj = _value(index, before[-1][2])
    if subj is None:
      continue
    actual = subj - ref
    direction = m.group('dir')
    ok = actual == n if direction in _UP else (actual == -n if direction in _DOWN else False)
    if not ok:
      bad.append(f'做差结果不符（实际 {actual:+d}，文中 {direction}{n}）')
  for m in _BARE_DELTA_RE.finditer(seg):
    dm = _DATE_RE.search(m.group('d'))
    if not dm:
      continue
    key = _date_key(dm.group('m2') or dm.group('m'), dm.group('d2') or dm.group('d'))
    ref = _value(index, key)
    if ref is None:
      continue
    before = [d for d in dates if d[1] <= m.start()]
    if not before:
      continue
    subj = _value(index, before[-1][2])
    if subj is None:
      continue
    n = int(m.group('n'))
    direction = m.group('dir')
    actual = subj - ref
    ok = actual == n if direction in _UP else (actual == -n if direction in _DOWN else False)
    if not ok:
      bad.append(f'「比 {m.group("d")}」差值不符（实际 {actual:+d}，文中 {direction}{n}）')
  return bad


def _check_peak(seg: str, index: dict) -> list[str]:
  """规则 4：峰谷归属（「10-05 单量最多」但实际不是最多）。"""
  bad = []
  for m in _PEAK_RE.finditer(seg):
    date_s = m.group('d1') or m.group('d2')
    word = m.group('k1') or m.group('k2')
    if any(w in m.group(0) for w in _PEAK_SKIP):
      continue
    dm = _DATE_RE.search(date_s or '')
    if not dm:
      continue
    key = _date_key(dm.group('m2') or dm.group('m'), dm.group('d2') or dm.group('d'))
    val = _value(index, key)
    if val is None:
      continue
    if word in _PEAK_WORDS and val != index['peak']:
      bad.append(f'「{date_s} {word}」与数据不符（{val}，峰值 {index["peak"]}）')
    elif word in _LOW_WORDS and val != index['low']:
      bad.append(f'「{date_s} {word}」与数据不符（{val}，低谷 {index["low"]}）')
  return bad


def _check_multiples(seg: str, dates, index: dict) -> list[str]:
  """规则 5：倍数。brief 已禁止；只要能与数据对上就放行，对不上（或语义含混）就拦。

  **基数必须真的对得上**：有日期锚点 → 比同一天的上期值；没有锚点时，只有句子明说
  总量口径才用 `total/prev_total`。其余情况一律不猜 —— 句中的「N 倍」可能指时长、
  金额等别的指标，拿日单量总比去核对只会误杀（2026-10-24 实测）。
  """
  if not _MULT_RE.search(seg):
    return []
  m = _MULT_RE.search(seg)
  # 「翻 N 倍」中文语义含混（N 倍还是 N+1 倍），与数据无关，直接拦
  if m.group('fan'):
    return ['倍数表述无法与数据唯一对应']
  before = [d for d in dates if d[1] <= m.start()]
  key = before[-1][2] if before else None
  if key is not None:
    cur_v, prev_v = _value(index, key), _prev_value(index, key)
  elif _TOTAL_HINT_RE.search(seg):
    cur_v, prev_v = index.get('total'), index.get('prev_total')
  else:
    return []                          # 无日期锚点、也不是总量口径 → 不猜
  if cur_v is None or prev_v is None:
    return []                          # 没有可比基数 → 不猜
  if prev_v == 0:
    return ['倍数比较的上一期基数为 0'] if cur_v else []
  ratio = cur_v / prev_v
  if m.group('num'):
    claim = float(m.group('num'))
    if abs(ratio - claim) > 0.05:
      return [f'倍数不符（实际 {ratio:.2f} 倍，文中 {claim:g} 倍）']
    return []
  # 只剩「翻倍/翻番」
  if abs(ratio - 2.0) > 0.1:
    return [f'「翻倍」不符（实际 {ratio:.2f} 倍）']
  return []


def violations(text: str, index: dict | None) -> list[str]:
  """返回可证明的违规原因列表；空列表＝放行。"""
  if not index:
    return []
  norm = _norm(text)
  if not norm:
    return []
  bad: list[str] = []
  for seg in _segments(norm):          # 每个分句各自取日期位置（不能复用全句偏移）
    seg_dates = _date_positions(seg)
    bad += _check_size(seg, seg_dates, index)
    bad += _check_same_delta(seg, seg_dates, index)
    bad += _check_delta(seg, seg_dates, index)
    bad += _check_peak(seg, index)
    bad += _check_multiples(seg, seg_dates, index)
  return bad


def filter_texts(items, index: dict | None) -> tuple[list[str], list[tuple[str, str]]]:
  """批量过滤：返回 `(保留, [(被丢弃原文, 原因)])`。`index` 为空时原样放行。"""
  kept: list[str] = []
  dropped: list[tuple[str, str]] = []
  for it in items or []:
    text = str(it)
    bad = violations(text, index)
    if bad:
      dropped.append((text, '；'.join(bad)))
    else:
      kept.append(text)
  return kept, dropped

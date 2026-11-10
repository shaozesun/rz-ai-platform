"""跨期比较句的**数字闸**（纯标准库，可独立单测）。

为什么需要：解读层原先拿到的是「本期占比 `pct`」和「上一期**计数** `prev`」，模型把它们
相减，写出「智能化占比 40%，较上期增加 13%」——真值是 40% vs 48.2%，即 **−8.2 个百分点**
（2026-10-23 实测，全国故障报告）。数据层已补 `prev_pct`/`delta_pp`/`delta_count` 并改成
带单位的键名，提示词也要求"只能引用给好的变化字段"，但提示词不是硬保证 —— 这里再加一道
**确定性**检查：

> 凡带「较上期 / 环比 / 同比」这类**跨期比较口吻**的句子，句子里每个数字都必须在本章
> 给定数据（`payload`）里出现过；否则整句丢弃。

为什么只认跨期关键词：同章内部的对比（「A 园区占比 17.5%，高于全国均值 5.7 个百分点」）
是合法的，若按"数字必须出现过"一刀切会把正确结论删掉。

**保守原则**：没有比较关键词的句子、没有数字的句子一律放行；只拦"看起来在做跨期比较、
数字却在数据里找不到"的句子。宁可漏拦，不可误杀。

本模块**不依赖 pandas / 项目内其它模块**，服务端进程与单测都能直接调用（不进沙箱）。
"""

import re

# 跨期比较口吻。**只认上一期与环同比**（同章内园区/部门互比不在此列）。
COMPARE_RE = re.compile(
  r'较上期|较上一期|比上期|比上一期|与上期|与上一期|相比上期|相比上一期|'
  r'环比|同比|上期相比|上一期相比|上期对比|上一期对比')

_NUM_RE = re.compile(r'\d+(?:\.\d+)?')
# 中文断句：句号/感叹号/问号/分号/换行（分号前后通常是两个独立判断）
_SENT_SPLIT_RE = re.compile(r'(?<=[。！？；\n])')

# 「变化量」字段的键名（`narrate._compare_rows` / `_trim_stats` 下发的口径）。
# 只收这些键里的数字：核验「增加/减少 N」里的 N 是否真是变化量。
DELTA_KEYS = ('单量变化（本期−上期）', '占比变化（百分点，本期−上期）', 'delta', '环比',
              '增减')

# 「增加/减少」方向词后紧邻的数字（中间只允许「了/约/近/达到」这类虚词）
# **排除「至/到」**：「评分由 83.2 提升至 100 分」是**目标值**不是变化量。误当变化量，
# 会把「规模控制评分升至 100 分、整体改善」这类正确结论整句丢掉（2026-10-27 实测）。
_DIRECTION_RE = re.compile(
  r'(?:增加|减少|上升|下降|提升|提高|降低|增长|下滑)[^0-9。；\n至到]{0,4}(\d+(?:\.\d+)?)')


def collect_numbers(obj, out: set | None = None) -> set:
  """递归收集 payload 里数字的**字面形态**（含绝对值与一位小数）。

  绝对值也要收：数据里存的是 `delta_count = -7`，报告写「较上期减少 7 起」——
  正则抓到的是 `7`，不补 abs 就会把正确句子判成越界。
  """
  if out is None:
    out = set()
  if isinstance(obj, dict):
    for v in obj.values():
      collect_numbers(v, out)
  elif isinstance(obj, (list, tuple, set)):
    for v in obj:
      collect_numbers(v, out)
  elif isinstance(obj, bool) or obj is None:
    pass
  elif isinstance(obj, (int, float)):
    for v in (obj, abs(obj)):
      out.add(f'{v}')
      out.add(f'{round(float(v), 1)}')
  elif isinstance(obj, str):
    out.update(_NUM_RE.findall(obj))
  return out


def collect_deltas(obj, out: set | None = None) -> set:
  """只收集**变化量**的数字（= 可以合法出现在「增加/减少 N」里的 N）。

  两类来源：① `DELTA_KEYS` 字段里的全部数字；② **平台自己写的文本**里紧跟
  「增加/减少…」的数字（如 `facts` 的「（10-11 较上一期同日减少 2 起）」）——
  那些句子本来就是平台算好的，模型照抄不该被拦。
  """
  if out is None:
    out = set()
  if isinstance(obj, dict):
    for k, v in obj.items():
      if k in DELTA_KEYS:
        if isinstance(v, dict):
          # `环比` 块是 `{prev_total, delta, pct}`：只把**变化量**当合法变化量，
          # 否则「较上期增加 56」（56 是上一期总量）也能过闸
          for kk in ('delta', 'pct'):
            if kk in v:
              collect_numbers(v[kk], out)
        else:
          collect_numbers(v, out)
      else:
        collect_deltas(v, out)
  elif isinstance(obj, (list, tuple, set)):
    for v in obj:
      collect_deltas(v, out)
  elif isinstance(obj, str):
    out.update(m.group(1) for m in _DIRECTION_RE.finditer(obj))
  return out


def violations(text, allowed, deltas=None) -> list[tuple[str, list[str]]]:
  """返回带比较口吻且含**越界数字**的句子：`[(句子, [越界数字…]), …]`。

  `allowed` 由调用方用 `collect_numbers(payload)` 生成（通常再并入结构性数字）。
  `deltas`（`collect_deltas(payload)`）可选：给了就额外核验「增加/减少 N」里的 N
  必须是某个真变化量 —— 光靠"数字出现过"拦不住「本期占比 40%，较上期增加 40%」
  （40 在本期占比里出现过，但根本不是变化量）。
  """
  allowed = set(allowed or ())
  deltas = None if deltas is None else set(deltas)
  bad = []
  for sent in _SENT_SPLIT_RE.split(str(text or '')):
    sent = sent.strip()
    if not sent or not COMPARE_RE.search(sent):
      continue
    extra = []
    for n in _NUM_RE.findall(sent):
      if n not in allowed and n not in extra:
        extra.append(n)
    if deltas is not None:
      for m in _DIRECTION_RE.finditer(sent):
        n = m.group(1)
        if n not in deltas and n not in extra:
          extra.append(n)
    if extra:
      bad.append((sent, extra))
  return bad


def filter_texts(texts, allowed, deltas=None) -> tuple[list[str], list[tuple[str, list[str]]]]:
  """按条过滤：含越界比较句的**整条**丢弃。返回 `(保留, 丢弃明细)`。"""
  kept, bad = [], []
  for text in texts or []:
    text = str(text or '').strip()
    if not text:
      continue
    hit = violations(text, allowed, deltas)
    if hit:
      bad.extend(hit)
    else:
      kept.append(text)
  return kept, bad

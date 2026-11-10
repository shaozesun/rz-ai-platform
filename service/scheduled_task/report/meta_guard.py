"""「本章缺少…所以无法判断」这类自我否定句的闸（纯标准库，可独立单测）。

为什么需要：2026-10-29 园区级变更周报第 4 章要点写「本章缺少审批与验收时长的分位数据，
无法评估…」（第 8 章图 13 就印着分等级时长的 P50）、「数据未提供各园区或各部门的级别
构成对比，无法判断…」。用户多次表示不要这类「数据不可得」式元抱怨 —— 报告不提供的项
统一在第 13 章（数据说明）列出，正文只讲已有的数据。

规则（**保守**）：同一句**同时**含「缺失标记」与「无法推断标记」才丢弃。只说
「数据未提供 X」而不下推断的句子放行 —— 宁可漏拦，不可误杀（同 `trend_guard` 口径）。

本模块**不依赖 pandas / 项目内其它模块**（不进沙箱）。
"""

import re

# ① 缺失标记：必须是「缺东西」的口吻，不能只是出现"数据"二字
_MISSING_RE = re.compile(
  r'本章缺少|本章未提供|本章没有|数据未提供|数据未包含|数据不含|数据缺失|数据不全|'
  r'缺少.{0,8}数据|未提供.{0,6}数据')

# ② 推断标记：因为没数据所以没法下结论
# 「无法展开/无法进行/无法对比/无法分析…」是同一族的变体（2026-10-30 实测：「各园区数据未提供，
# 无法展开园区间差异对比」「由于数据未提供各园区或各部门的级别构成对比，无法分析…结构差异」）
_NO_CONCLUDE_RE = re.compile(
  r'无法判断|无法评估|无法比较|无法识别|无法确定|无法分析|无法区分|无法得出|'
  r'无法研判|无法量化|无法展开|无法进行|无法对比|难以判断|难以评估|难以比较|难以量化')

# 中文断句：句号/感叹号/问号/分号/换行
_SENT_SPLIT_RE = re.compile(r'(?<=[。！？；\n])')

_REASON = '以「数据缺失」为由下「无法判断」的结论（不提供的项统一在第 13 章列出）'


def violations(text) -> list[str]:
  """返回违规句子（空列表＝放行）。"""
  bad = []
  for sent in _SENT_SPLIT_RE.split(str(text or '')):
    sent = sent.strip()
    if sent and _MISSING_RE.search(sent) and _NO_CONCLUDE_RE.search(sent):
      bad.append(sent)
  return bad


def filter_texts(items) -> tuple[list[str], list[tuple[str, str]]]:
  """批量过滤：返回 `(保留, [(违规句子, 原因)])`。整条含违规句即丢弃。"""
  kept: list[str] = []
  dropped: list[tuple[str, str]] = []
  for it in items or []:
    text = str(it or '').strip()
    if not text:
      continue
    hit = violations(text)
    if hit:
      dropped.extend((s, _REASON) for s in hit)
    else:
      kept.append(text)
  return kept, dropped

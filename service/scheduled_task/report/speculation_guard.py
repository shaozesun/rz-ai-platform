"""无数据支撑的「数据可靠性猜测」闸（纯标准库，可独立单测）。

为什么需要：解读层会把**正常的好消息**写成风险 —— 2026-10-27 扫描当天 51 份报告，
**26 份**出现「单量下降 + 漏报/采集失效」同句（如"10-21 单量由上一期 8 起降至本期 4 起
（减少 4 起）…需确认是否存在漏报风险"），而同一页「规模控制」评分是 **100**
（平台口径：持平或下降满分）。根因是结构性的（方向判据没下发 + 每条必须带建议动作 +
摘要/行动计划 pain-first），已在 `narrate.py` / `common._volume_verdict` 各自修掉；
本闸是**确定性兜底**，与 `trend_guard` / `compare_guard` 同层。

规则（**保守**，宁可漏拦，不可误杀）：
  同一句里同时出现 ① 减量/低位词 ② 数据可靠性猜测词，且**整份报告的数据里没有任何
  缺失证据**（`缺失`/`非法`/`未回填`/`剔除`/`missing`/`fallback` 等计数 > 0）→
  判定为无依据的猜测，整句丢弃。
  只有一侧词、句中带否定（未发现/未见/不存在）、或数据确有缺失证据 —— 一律放行。

本模块**不依赖 pandas / 项目内其它模块**，服务端进程与单测都能直接调用（不进沙箱）。
"""

import re

# ① 减量 / 低位（只做"是否同句出现"的判断，不解析数值）
# 注意包含**负号数值**（「单量变化为 -16 单」实测就是这种写法，没有"减少"二字）。
# 「低于/偏少/不足/较少」是 2026-10-27 补的：原句「各专业单量均**低于** 5 单…建议核实是否漏报」
# 一个减量词都没命中，闸门形同虚设。
_DECREASE_RE = re.compile(
  r'下降|减少|降低|下滑|回落|低谷|偏低|骤降|锐减|零单|无工单|为\s*0(?![.\d])|[-−]\s*\d|'
  r'低于|偏少|不足|较少')

# ② 数据可靠性的猜测（没有证据就是猜测）
# 「监测粒度/上报偏保守」同样是这一类的变体：把"体量小"归因成"采得少"。
_SUSPECT_RE = re.compile(
  r'漏报|采集失效|数据失真|数据不准|统计失真|统计口径(?:变化|调整)|系统屏蔽|'
  r'监控盲区|采集异常|漏采|埋点缺失|数据缺失|数据丢失|监测粒度|上报偏保守')

# 句中已明确否定 → 不是猜测，放行
_NEGATION_RE = re.compile(r'未发现|未见|不存在|没有(?:任何)?(?:漏报|采集)')

# 数据里「确实缺了东西」的证据键（键名命中且计数 > 0 才允许谈漏报/采集失效）
_EVIDENCE_KEY_RE = re.compile(
  r'缺失|非法|未回填|不可统计|剔除|fallback|invalid|missing|未采集')

_REASON = '把单量下降/低位推测为数据可靠性问题，但报告数据里没有缺失证据'


def has_data_quality_evidence(data) -> bool:
  """数据里是否有「确实缺了数据」的正数证据（有才允许谈漏报/采集失效）。

  递归找键名匹配 `_EVIDENCE_KEY_RE` 且**数值 > 0** 的叶子；dict/list 逐层下探。
  父级键命中但值是嵌套结构时**不**直接放行（如 `数据可用性: {'实施时间缺失': 0}`
  全是 0 时不应放行），只看真正的叶子计数。
  """
  if isinstance(data, dict):
    for key, value in data.items():
      if _EVIDENCE_KEY_RE.search(str(key)):
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
          return True
      if has_data_quality_evidence(value):
        return True
    return False
  if isinstance(data, list):
    return any(has_data_quality_evidence(v) for v in data)
  return False


def violations(text: str, allow_data_suspicion: bool = False) -> list[str]:
  """返回违规原因（空列表＝放行）。`allow_data_suspicion=True` 时一律放行。"""
  if allow_data_suspicion or not text:
    return []
  if not (_DECREASE_RE.search(text) and _SUSPECT_RE.search(text)):
    return []
  if _NEGATION_RE.search(text):
    return []
  return [_REASON]


def filter_texts(items, allow_data_suspicion: bool = False
                 ) -> tuple[list[str], list[tuple[str, str]]]:
  """批量过滤：返回 `(保留, [(被丢弃原文, 原因)])`。"""
  kept: list[str] = []
  dropped: list[tuple[str, str]] = []
  for it in items or []:
    text = str(it)
    bad = violations(text, allow_data_suspicion)
    if bad:
      dropped.append((text, '；'.join(bad)))
    else:
      kept.append(text)
  return kept, dropped

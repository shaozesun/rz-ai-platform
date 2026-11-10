"""机构名审计（纯标准库，可独立单测）—— **只记日志，不删句**。

历史：2026-10-29 建模块时定位是「幻觉闸」，起因是园区级变更周报第 12 章 P0 写了
「重点解决 B1M4 分部、A1 分部及技术保障部的长时滞留问题」。**2026-10-30 复核推翻了
这个判断**：同一数据集 `data.csv` 的 `dc` 字段共 16 个部门，`B1M4分部`(2 单)、
`技术保障部`(1 单) 都在里面 —— 是**真实部门**，只是第 5 章表格按 `DEPT_TABLE_LIMIT=12`
截断、它们排第 14/16 位没印出来。用户看不到 ≠ 数据里没有，模型并没有编。

代价：撤掉删除行为前，24 份报告共删 67 句，其中 55 句是真实部门（模型只少写了园区
前缀或加了空格，如 `A1分部` ⊂ `平湖A1分部`），平湖 W43 故障周报第 7 章要点被整段删空。
故按「宁可漏拦，不可误杀」撤下删除行为：本模块只保留**抽取 + 比对**，由 `narrate.py`
打 warning 做事后审计。详见 docs/problems/report-invented-org-names.md「十」。

规则（白名单取「模型看到的内容」= 本次下发载荷，用于审计"点了本章没给的名字"）：
  模型输出的机构名（`X分部` / `X园区` / `X数据中心` / `X部`）若**不在本次下发载荷里
  出现过**，记一条 warning。**命中不丢句、不丢条目、不置空**。
  判定用**后缀**匹配：「由安全技术保障部」这类过度截取只要以某个白名单名字结尾即放行。

本模块**不依赖 pandas / 项目内其它模块**（不进沙箱）。
"""

import re

# 园区/分部类：A1分部 / B1M4分部 / 惠州A3分部 / A3数据中心。
# **body 必须含字母数字**（允许最多 3 个汉字的地区前缀）：纯中文「…园区」在散文里几乎
# 全是泛称 —— 2026-10-30 一次生成里「与其他园区」「单一园区」「无法展开园区」「但该园区」
# 四句被误伤。宁可漏拦（中文园区名编造少见），不可让正常句子成批消失。
# **容忍名字内部的空格**（`A2 分部`）：载荷写 `A2分部`，模型常写成带空格的版本，不认出来
# 就会掉进 `_DEPT_RE` 被截成「表明该分部」（2026-10-30 三份全国月报各误杀 3 句的根因）。
# 空格只允许出现在**字母数字之后**：前缀带空格会把「与 B1M4分部」连成「与B1M4分部」。
_ORG_RE = re.compile(
  r'[\u4e00-\u9fa5]{0,3}?[A-Za-z0-9][A-Za-z0-9\u4e00-\u9fa5 \u3000]{0,11}?(?:分部|园区|数据中心)')

# 部门类：技术保障部 / 网络运维部。后缀否定掉「部门/部分/干部/内部…」这些非机构名。
# **必须懒惰**：贪婪会把「A1分部及技术保障部」连成一个 token，既报错名字又漏掉真名。
_DEPT_RE = re.compile(
  r'[\u4e00-\u9fa5A-Za-z0-9]{2,10}?部(?!门|分|长|位|件|署|落|委|队)')

# 名称开头的连接词/介词不算名字的一部分（「A1分部**及**技术保障部」→ 技术保障部）
_LEAD_TRIM = '及与和、，,；;　并其的由在从对'

# 贪婪截取会把前文一起带进来（「公司内部」/「全部」），按**结尾**判定更稳。
# 「局部」是形容词/范围词，不是机构（2026-10-30 实测「风险分布呈现明显的局部聚集特征」
# 被截成机构名后整句丢弃）；「东部/南部/西部/北部」是方位词，同理。
_DEPT_STOP_SUFFIX = (
  '部门', '部分', '内部', '外部', '全部', '干部', '头部', '尾部', '中部', '底部',
  '顶部', '部位', '部长', '部件', '部委', '部署', '部落', '本部', '大部', '小部',
  '局部', '东部', '南部', '西部', '北部',
)

_SPACE_RE = re.compile(r'[ \u3000]+')

_ORG_SUFFIXES = ('数据中心', '分部', '园区')

# body 里出现这些字样，说明截到的是**句子**而不是机构名：
# 「1级事件的园区」「66单占该园区」「11单而其余四园区」——2026-10-30 全国事件周报 14 期
# 共误杀 17 句，其中 8 句是这一类（`_ORG_RE` 只要求 body 含字母数字，`66` 就通过了）。
_ORG_BODY_STOP_RE = re.compile(
  r'(?:的|该|此|其余|其他|全部|单一|不同|各|每|[0-9]+\s*(?:单|起|个|次|台|项|套))')

# 连接词：模型常把两个真名连写（「A2 与 A3 两个分部」），整串后缀匹配不上就会被判编造
_CONNECTIVE_RE = re.compile(r'[与和及、，,；;]')


def _clean(tok: str) -> str:
  """机构名内部空格归一：载荷写 `A2分部`、模型写 `A2 分部`，比对前必须同一形态。"""
  return _SPACE_RE.sub('', tok)


def _body(tok: str) -> str:
  """去掉类后缀后的 body（`A2分部` → `A2`）；不带类后缀原样返回。"""
  for suffix in _ORG_SUFFIXES:
    if tok.endswith(suffix):
      return tok[:-len(suffix)]
  return tok


def _split_joined(tok: str) -> list[str] | None:
  """`A2及A3分部` → `['A2分部', 'A3分部']`；无连接词或拆不出两段返回 None。

  只用于**放行**（拆开后每段都在白名单里才算数），不会因此多拦。
  """
  for suffix in _ORG_SUFFIXES:
    if tok.endswith(suffix):
      parts = [p for p in _CONNECTIVE_RE.split(tok[:-len(suffix)]) if p]
      return [p + suffix for p in parts] if len(parts) >= 2 else None
  return None


def tokens(text) -> set[str]:
  """抽取文本里成机构名形态的片段（内部空格已归一）。"""
  out: set[str] = set()
  s = str(text or '')
  for m in _ORG_RE.finditer(s):
    tok = _clean(m.group(0))
    if _ORG_BODY_STOP_RE.search(_body(tok)):   # 「66单占该园区」这类是句子，不是机构名
      continue
    out.add(tok)
  for m in _DEPT_RE.finditer(s):
    tok = _clean(m.group(0).lstrip(_LEAD_TRIM))
    if tok.endswith('分部'):                 # 「…分部」交给 `_ORG_RE`；这里命中的只可能是
      continue                              # 「表明该分部」这类把「分」吃进 body 的误截
    if len(tok) < 3:                        # 剥掉连接词后只剩「X部」的，不算机构名
      continue
    if any(tok.endswith(stop) for stop in _DEPT_STOP_SUFFIX):
      continue
    out.add(tok)
  return out


def allowed_from_payload(payload) -> set[str]:
  """本次**实际下发**给模型的载荷里出现过的机构名（递归取并集）。

  刻意不用全量 `stats`：模型没看到的名字，它写出来就是编的。
  """
  out: set[str] = set()

  def _walk(obj):
    if isinstance(obj, str):
      out.update(tokens(obj))
    elif isinstance(obj, dict):
      for k, v in obj.items():
        _walk(k)
        _walk(v)
    elif isinstance(obj, (list, tuple, set)):
      for v in obj:
        _walk(v)

  _walk(payload)
  return out


def violations(text, allowed) -> list[str]:
  """返回载荷里不存在的机构名（空列表＝放行）。"""
  allow = set(allowed or ())
  bad = []
  for tok in sorted(tokens(text)):
    if tok in allow:
      continue
    if any(tok.endswith(a) for a in allow):
      continue
    parts = _split_joined(tok)                 # 「A2及A3分部」拆开后全是真名就放行
    if parts and all(p in allow or any(p.endswith(a) for a in allow) for p in parts):
      continue
    bad.append(tok)
  return bad


def filter_texts(items, allowed) -> tuple[list[str], list[tuple[str, str]]]:
  """批量比对：返回 `(未命中, [(命中原文, 越界机构名)])`。

  **不用于删正文**（本模块已撤下删除行为）；报告链路只调 `violations()` 打日志。
  """
  kept: list[str] = []
  dropped: list[tuple[str, str]] = []
  for it in items or []:
    text = str(it or '').strip()
    if not text:
      continue
    bad = violations(text, allowed)
    if bad:
      dropped.append((text, '、'.join(bad)))
    else:
      kept.append(text)
  return kept, dropped

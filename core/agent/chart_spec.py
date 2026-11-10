"""图表标签装配 —— 把正文里 <chart …/> 的 group 引用展开成带数据体的自包含标签。

背景（2026-10-14 事故）：图表数据一度被搬到「旁路」——正文只写 `<chart group="组名"/>`，
数据走 SSE + 消息字段，由前端按组名 join。但落库那步漏传参数（且该机制从未提交、从未成功），
导致刷新/重开后图全断；又因为前端把「数据为空」和「类型不认识」写成同一句兜底文案，
连合法的 column/line/pie 也被误报成「不支持的图表类型」。

这里在落库前由**服务端**把数据写回标签**体**，恢复「数据与正文同源」：既拿回刷新不乱的性质，
又避开「模型手写 `data="[…]”` 当属性、被 JSON 双引号截断」的老毛病（数据由服务端序列化，
且写在标签体内而非属性里）。前端本就支持标签体（ChatArea 的 `seg.data || …`），无需改动。
"""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

# 与前端 web/src/components/ChatArea.tsx 的 CHART_TAG_RE 同构：
# 属性段（不含 >）+ 自闭合 `/>`，或 属性段 + `>数据体</chart>`。「已有体」的标签会被原样保留。
_CHART_TAG_RE = re.compile(r'<chart\b([^>]*?)(?:/>|>([\s\S]*?)</chart>)')

# 从属性串里取 group="…"（容忍单引号）
_GROUP_RE = re.compile(r'''\bgroup\s*=\s*(?:"([^"]*)"|'([^']*)')''')


def _group_of(attrs: str) -> str:
  """取标签属性里的 group 名；没有返回空串。"""
  match = _GROUP_RE.search(attrs or '')
  if not match:
    return ''
  return (match.group(1) or match.group(2) or '').strip()


def assemble_chart_bodies(content: str, chart_data: dict | None) -> tuple[str, list[str]]:
  """把正文里 `<chart …/>` 的 group 引用就地展开成带数据体的自包含标签。

  - 标签**已有体**（历史形态 ``<chart …>JSON</chart>``）→ 原样保留，不重复装配
  - 取属性里的 group 名 → 命中 chart_data → 重建为 ``<chart {attrs}>{json}</chart>``
  - 未命中 / 无 group → 原样保留，组名收进返回的「未命中列表」供调用方告警

  Returns:
    ``(新正文, 未命中的组名列表)``。纯函数；任一异常都兜底为「原样返回」——
    这里只负责给图表补数据，不能影响主流程。
  """
  if not content or '<chart' not in content:
    return content, []
  data = chart_data if isinstance(chart_data, dict) else {}
  missed: list[str] = []

  def _replace(match: re.Match) -> str:
    attrs = match.group(1) or ''
    if (match.group(2) or '').strip():
      return match.group(0)  # 已有数据体：历史形态，别动
    group = _group_of(attrs)
    rows = data.get(group) if group else None
    if rows is None:
      if group:
        missed.append(group)
      return match.group(0)
    return f'<chart{attrs}>{json.dumps(rows, ensure_ascii=False)}</chart>'

  try:
    return _CHART_TAG_RE.sub(_replace, content), missed
  except Exception:
    logger.exception('图表标签装配失败，正文原样返回')
    return content, []

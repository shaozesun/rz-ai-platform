"""web 平台能力声明 —— 联网检索与抓取。

**只在「联网调研」模式（`task_mode='research'`）或前端「联网搜索」开关
（`AgentRequest.web_search`）下可用**：该平台被列入
`service/query/base/platform.py` 的 `MODE_GATED_PLATFORMS`，因此
- 不进 `<available-tools>` 分类清单（构建期排除）
- 不进意图绑定表（planner 的闭合词汇表里没有它）
- 不进 `tool_search` 的检索目录（模型无法自己搜出来 promote）
非调研模式下连它的存在都看不到，硬调也会被 DeferredToolFilter 拦下。
"""

from __future__ import annotations

from service.query.base.capability import Capability

CAPABILITIES: tuple[Capability, ...] = (
  Capability(
    id='web.search',
    name='联网搜索',
    description=(
      '通过自建 SearXNG 检索公开互联网，返回标题、URL 与摘要片段。'
      '适用于需要外部公开信息的场景：产品/厂商资料、技术标准与规范、行业动态、'
      '通用概念解释等。**不接受内部数据查询**（内部数据请用 DCIM/综合管理平台工具）。'
      '需要的细节如果摘要不够，用 web.fetch 抓原页面全文。'
    ),
    params={
      'query': '检索关键词，用自然语言描述要找什么',
      'max_results': '返回条数上限（默认 5，最多 10）',
    },
    category='web',
    domain='search',
    kind='检索',
  ),
  Capability(
    id='web.fetch',
    name='网页抓取',
    description=(
      '抓取指定 URL 的网页正文并转为纯文本。用于读取 web.search 结果里某条链接的全文，'
      '或在已知 URL 时直接取内容。出于安全考虑：仅支持公网 http/https 地址，'
      '内网/回环/云元数据地址会被拒绝，重定向会逐跳复校验。'
    ),
    params={
      'url': '要抓取的网页地址（http/https，必须是公网地址）',
    },
    category='web',
    domain='fetch',
    kind='检索',
  ),
)

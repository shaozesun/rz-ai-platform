"""抓取类工具的 URL 安全校验（SSRF 防护）

为什么必须有：抓取工具接受的是**模型给的 URL**，而模型可能被网页/平台数据里的内容
诱导（提示词注入）去抓内网地址。rz 部署在数据中心内网里，一旦能 fetch 就能打到
综合管理平台（192.168.88.18 认证中心）、DCIM/pen 接口、以及云元数据端点
（169.254.169.254 / metadata.google.internal）。

形态照搬 DeerFlow `community/url_safety.py`：解析主机 → 逐个 IP 判类型 → 拒绝
私有/回环/链路本地/保留/组播/未指定。**rz 增量：重定向逐跳校验** —— 只校验初始 URL
不够，一个 302 跳到内网就绕过了，所以抓取侧必须 `follow_redirects=False` 手动跟跳，
每跳都调本模块校验（见 client.py）。

已知残余风险：校验的是「解析出的 IP」，与真正建立连接之间存在 TOCTOU 窗口
（DNS rebinding）。DeerFlow 同样如此，这里保持一致并记录；要彻底消除需要把校验过的
IP 直接用于连接（httpx 的 transport 层定制），超出当前范围。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 主机名黑名单：即使解析结果看起来正常也直接拒（云元数据端点常见别名）
_BLOCKED_HOSTNAMES = frozenset({'localhost', 'metadata.google.internal'})
_ALLOWED_SCHEMES = frozenset({'http', 'https'})


def resolve_host_addresses(hostname: str) -> list[ipaddress._BaseAddress]:
  """把主机名解析成全部 IP（解析失败返回空列表）。"""
  addresses: list[ipaddress._BaseAddress] = []
  try:
    infos = socket.getaddrinfo(hostname, None)
  except (socket.gaierror, UnicodeError, OSError):
    return addresses
  for info in infos:
    sockaddr = info[4]
    try:
      addresses.append(ipaddress.ip_address(sockaddr[0]))
    except ValueError:
      continue
  return addresses


def is_blocked_address(address: ipaddress._BaseAddress) -> bool:
  """非全局可路由地址一律拦（+ 组播）。

  用 `not is_global` 作为主判据，比 DeerFlow 逐个列 `is_private/is_loopback/...` 更严：
  实测 `100.64.0.0/10`（CGNAT，运营商/内网常用段）的 `is_private` 是 False，会被漏掉，
  而 `is_global` 是 False。同时它天然覆盖私有/回环/链路本地/保留/未指定/基准测试段
  （198.18/15）/文档段（192.0.2/24、203.0.113/24）。

  例外：**组播地址的 `is_global` 是 True**（如 224.0.0.1），必须单独拦。
  """
  return (not address.is_global) or address.is_multicast


def validate_public_http_url(
  url: str,
  *,
  allow_private_addresses: bool = False,
  action: str = 'fetch',
  resolver: Callable[[str], list] | None = None,
) -> str | None:
  """抓取前校验 URL：可放行返回 None，否则返回给模型看的错误串。

  Args:
    url: 待校验的 URL
    allow_private_addresses: 内网自建服务（如本地 SearXNG）需要时置 True
    action: 出现在错误信息里的动作名（fetch / search 等）
    resolver: 便于测试注入的解析函数
  """
  try:
    parsed = urlparse(url or '')
  except ValueError:
    return 'Error: URL 无法解析'

  if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
    return 'Error: 只支持 http:// 与 https:// 的 URL'

  if allow_private_addresses:
    return None

  hostname = parsed.hostname
  if not hostname:
    return 'Error: 无法从 URL 中解析出主机名'

  normalized = hostname.strip().rstrip('.').lower()
  if normalized in _BLOCKED_HOSTNAMES:
    return f'Error: 拒绝 {action} 内网/回环地址'

  try:
    literal_ip = ipaddress.ip_address(normalized)
  except ValueError:
    literal_ip = None

  if literal_ip is not None:
    candidates: list = [literal_ip]
  else:
    resolve = resolver or resolve_host_addresses
    candidates = resolve(hostname)
    if not candidates:
      return 'Error: 主机名无法解析'

  if any(is_blocked_address(addr) for addr in candidates):
    logger.warning('SSRF 拦截: %s → %s', url, [str(a) for a in candidates])
    return f'Error: 拒绝 {action} 内网、回环或元数据地址'
  return None

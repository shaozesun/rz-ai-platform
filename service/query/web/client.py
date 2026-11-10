"""web 平台调用器 —— 自建 SearXNG 检索 + 受控网页抓取 + 出站审计。

安全约束（见 url_safety.py 与计划）：
- 抓取只允许公网 http/https；**重定向逐跳复校验**（`follow_redirects=False` 手动跟跳），
  否则一个 302 跳内网就绕过 SSRF 防护
- 响应体有大小上限与超时，避免大页面拖垮进程
- SearXNG 是自建实例（`WEB_SEARCH_BASE_URL`），允许内网地址（`allow_private_addresses=True`）
  —— 它就是我们的出站 chokepoint，上游引擎由它去访问

审计：每次检索/抓取都 fire-and-forget 落 MongoDB `web_queries`（query/url 原文 +
来源域名）。出方向不做管控（已决定），**审计是唯一的追溯手段**，不要省。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

from config.settings import settings
from core.agent.user_context import get_current_user_id, get_current_thread_id
from service.query.web.url_safety import validate_public_http_url

logger = logging.getLogger(__name__)

_USER_AGENT = 'rz-ai-platform/1.0 (+internal research assistant)'
_MAX_RESULTS_HARD_CAP = 10
_HTML_TEXT_CAP = 400_000   # 解析前先截断，防超大页面
_REDIRECT_CODES = {301, 302, 303, 307, 308}


class WebError(Exception):
  """web 能力调用失败（消息面向模型，可读）。"""


def _audit(capability: str, target: str, *, ok: bool, result_count: int = 0,
           domains: list[str] | None = None, note: str = '') -> None:
  """出站审计（fire-and-forget，失败不影响主流程）。"""
  record = {
    'user_id': get_current_user_id(),
    'thread_id': get_current_thread_id(),
    'capability': capability,
    'target': target,          # query 或 url 原文 —— 追溯「问了什么 / 抓了哪」
    'ok': ok,
    'result_count': result_count,
    'domains': (domains or [])[:10],
    'note': note[:200],
    'created_at': datetime.now(),
  }
  try:
    from config.mongodb_conn import mongodb_manager
    mongodb_manager.sync_db[settings.WEB_AUDIT_COLLECTION].insert_one(record)
  except Exception as e:  # 审计失败不阻断检索，但必须可见
    logger.warning('web 审计落库失败: %s', e)


def _normalize_search(payload: dict, max_results: int) -> dict:
  """SearXNG JSON → 紧凑结果（title/url/snippet）。"""
  raw = payload.get('results') or []
  results = []
  for item in raw[:max_results]:
    if not isinstance(item, dict):
      continue
    url = str(item.get('url') or '').strip()
    if not url:
      continue
    results.append({
      'title': str(item.get('title') or '')[:200],
      'url': url[:500],
      'snippet': ' '.join(str(item.get('content') or '').split())[:400],
      'engine': str(item.get('engine') or '')[:40],
    })
  return {'results': results, 'count': len(results)}


def _html_to_text(html: str) -> str:
  """极简 HTML → 纯文本：去 script/style，去标签，反转义实体，压空白。"""
  import re
  text = re.sub(r'(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>', ' ', html)
  text = re.sub(r'(?s)<[^>]+>', ' ', text)
  text = unescape(text)
  return ' '.join(text.split())


async def _fetch_public_text(url: str) -> dict:
  """抓取公网 URL 的纯文本，**逐跳校验重定向**。"""
  max_redirects = max(0, int(settings.WEB_FETCH_MAX_REDIRECTS))
  timeout = float(settings.WEB_TIMEOUT)

  current = url
  async with httpx.AsyncClient(
    follow_redirects=False, timeout=timeout, headers={'User-Agent': _USER_AGENT},
  ) as client:
    for hop in range(max_redirects + 1):
      err = validate_public_http_url(current, action='fetch')
      if err:
        raise WebError(err)
      try:
        resp = await client.get(current)
      except httpx.HTTPError as e:
        raise WebError(f'Error: 抓取失败（{type(e).__name__}）') from e

      if resp.status_code in _REDIRECT_CODES:
        location = resp.headers.get('location')
        if not location:
          raise WebError('Error: 重定向缺少目标地址')
        if hop >= max_redirects:
          raise WebError(f'Error: 重定向次数超过上限（{max_redirects}）')
        # 相对 Location 要按当前 URL 解析，再回到循环顶部重新校验
        current = urljoin(current, location)
        continue

      if resp.status_code >= 400:
        raise WebError(f'Error: 目标返回 {resp.status_code}')

      ctype = (resp.headers.get('content-type') or '').lower()
      body = resp.text[:_HTML_TEXT_CAP]
      text = _html_to_text(body) if 'html' in ctype or '<' in body[:200] else ' '.join(body.split())
      max_chars = int(settings.WEB_FETCH_MAX_CHARS)
      truncated = len(text) > max_chars
      return {
        'url': current,
        'final_url': current,
        'content_type': ctype.split(';')[0],
        'text': text[:max_chars],
        'truncated': truncated,
      }

  raise WebError('Error: 抓取未完成（重定向链路异常）')


async def call(capability_id: str, params: dict, *, on_behalf_of: str | None = None) -> dict:
  """web 平台统一入口（契约同其它平台 caller）。"""
  if capability_id == 'web.search':
    return await _search(params)
  if capability_id == 'web.fetch':
    return await _fetch(params)
  raise WebError(f'未知 web 能力 {capability_id}')


async def _search(params: dict) -> dict:
  query = (params.get('query') or '').strip()
  if not query:
    raise WebError('Error: 缺少检索关键词 query')
  try:
    max_results = int(params.get('max_results') or settings.WEB_MAX_RESULTS)
  except (TypeError, ValueError):
    max_results = settings.WEB_MAX_RESULTS
  max_results = max(1, min(max_results, _MAX_RESULTS_HARD_CAP))

  base = (settings.WEB_SEARCH_BASE_URL or '').rstrip('/')
  if not base:
    _audit('web.search', query, ok=False, note='未配置 WEB_SEARCH_BASE_URL')
    raise WebError('Error: 联网检索未配置（WEB_SEARCH_BASE_URL 为空）')
  # 自建实例允许内网地址：它就是出站 chokepoint
  if validate_public_http_url(base, allow_private_addresses=True):
    raise WebError('Error: WEB_SEARCH_BASE_URL 配置非法')

  try:
    async with httpx.AsyncClient(timeout=float(settings.WEB_TIMEOUT)) as client:
      resp = await client.get(
        f'{base}/search',
        params={'q': query, 'format': 'json', 'safesearch': 1, 'language': 'auto'},
        headers={'User-Agent': _USER_AGENT},
      )
      resp.raise_for_status()
      payload = resp.json()
  except httpx.HTTPError as e:
    _audit('web.search', query, ok=False, note=type(e).__name__)
    raise WebError(f'Error: 检索服务不可用（{type(e).__name__}）') from e
  except ValueError as e:  # JSON 解析失败
    _audit('web.search', query, ok=False, note='bad json')
    raise WebError('Error: 检索服务返回格式异常（SearXNG 需开启 json 输出格式）') from e

  result = _normalize_search(payload, max_results)
  domains = sorted({urlparse(r['url']).hostname or '' for r in result['results']} - {''})
  _audit('web.search', query, ok=True, result_count=result['count'], domains=domains)
  logger.info('web.search q=%r → %d 条 (%s)', query, result['count'], ','.join(domains[:5]))
  return {'query': query, **result, 'note': '外部公开信息，非内部数据；引用时须标注来源'}


async def _fetch(params: dict) -> dict:
  url = (params.get('url') or '').strip()
  if not url:
    raise WebError('Error: 缺少 url')
  try:
    result = await _fetch_public_text(url)
  except WebError as e:
    _audit('web.fetch', url, ok=False, note=str(e)[:120])
    raise
  _audit('web.fetch', url, ok=True, result_count=1,
         domains=sorted({urlparse(result['final_url']).hostname or ''} - {''}))
  logger.info('web.fetch %s → %d 字符', result['final_url'], len(result['text']))
  return {**result, 'note': '网页内容属外部不可信信息，引用时须标注来源'}

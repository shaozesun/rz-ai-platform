"""browser 平台调用器 —— 发配方给 Playwright 旁路服务，落盘截图并返回产物。

分工：本模块只管「取哪个配方、填哪些参数、截图存哪」；页面怎么点由配方声明、
旁路服务按配方执行。截图落在 `{会话目录}/browser/`：
- 该目录没有 `meta.json`，不会被 `datasets._evict_if_needed` 当数据集淘汰；
- 文件名纯 ASCII + 时间戳，下载端点的 `find_artifact_dir` 能按名反查；
- **绝不覆盖**：O_EXCL 临时文件 + 硬链接发布 + inode 校验（对齐 DeerFlow）。
"""

from __future__ import annotations

import base64
import logging
import os
import re
from datetime import datetime
from urllib.parse import quote, urlparse

import httpx

from config.settings import settings
from core.agent.user_context import get_current_thread_id
from service.query.browser.recipes import (
  Recipe,
  RecipeError,
  get_recipe,
  resolve_params,
)

logger = logging.getLogger(__name__)

_PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
_TEXT_LIMIT = 6000
_TABLE_ROWS_LIMIT = 100
_TABLE_COLS_LIMIT = 10
_CELL_CHARS = 120

_STAGE_HINTS = {
  'recipe': '配方不合法',
  'login': '登录态失效或登录页结构变化，请运行 deploy/scripts/browser_login.py 重新登录',
  'login_page': '目标地址不是登录页（URL 可能不对），请用系统根地址打开后通过页面菜单进入',
  'login_conflict': '登录后又被踢回登录页（同一账号可能在别处登录），建议为浏览器配置专用账号',
  'navigate': '页面打不开（检查页面根地址与网络连通）',
  'apply_filters': '筛选控件定位失败（页面可能改版，需用 browser_probe 校准配方）',
  'ready': '等待页面就绪超时（页面可能改版或加载过慢）',
  'extract': '页面内容抽取失败（选择器可能已失效）',
  'screenshot': '截图失败',
  'timeout': '截图超时（页面加载过慢或范围太大）',
  # 交互浏览（BROWSER_EXPLORE_ENABLED）
  'session': '没有可用的浏览会话（可能已超时关闭），请先调用 browser__navigate 打开页面',
  'click': '元素编号已失效或点击失败，请先调用 browser__snapshot 重新编号',
  'blocked': '该元素被只读策略禁止点击（删除/提交等危险操作），请由人工完成，或改用只读方式浏览',
  'text': '读取页面文本失败',
  'record': '录制失败（请确认页面已打开；录制期间不要操作页面，且同时只能录一路）',
  'video': '视频生成失败（ffmpeg 不可用或未采集到帧），关键帧仍可用，可缩短时长重试',
}


class BrowserError(Exception):
  """页面截图失败（消息面向模型，可读且带下一步）。"""


def _base_url(system: str) -> str:
  if system == 'dcim':
    return (settings.BROWSER_DCIM_URL or settings.DCIM_BASE_URL).strip()
  if system == 'mgmt':
    return settings.BROWSER_MGMT_URL.strip()
  return ''


def _page_url(recipe: Recipe) -> str:
  base = _base_url(recipe.system)
  if not base:
    name = 'BROWSER_DCIM_URL' if recipe.system == 'dcim' else 'BROWSER_MGMT_URL'
    raise BrowserError(f'未配置 {name}（页面根地址），无法截图')
  path = recipe.path if recipe.path.startswith('/') else f'/{recipe.path}'
  return base.rstrip('/') + path


def _check_host(url: str, params: dict) -> None:
  """导航目标主机必须在 BROWSER_ALLOWED_HOSTS 内（空 = 不限，仅限可信内网）。"""
  hosts = {h.strip().lower() for h in settings.BROWSER_ALLOWED_HOSTS.split(',') if h.strip()}
  if not hosts:
    return

  def repl(m: re.Match) -> str:
    return str(params.get(m.group(1), '\x00'))
  host = (urlparse(_PLACEHOLDER.sub(repl, url)).hostname or '').lower()
  if '\x00' in host or host not in hosts:
    # 明确给出可用的地址：模型曾自行编造域名（mgmt.rzdata.com）被拒后转而放弃任务。
    roots = [
      f'综合管理平台 {settings.BROWSER_MGMT_URL}' if settings.BROWSER_MGMT_URL else '',
      f'动环监控 {settings.BROWSER_DCIM_URL}' if settings.BROWSER_DCIM_URL else '',
    ]
    roots = [r for r in roots if r]
    hint = f'；可用的页面根地址：{"；".join(roots)}' if roots else ''
    raise BrowserError(
      f'导航目标主机 {host or "(空)"} 不在允许访问的白名单（允许：{"、".join(sorted(hosts))}）'
      f'{hint}。请使用上述地址，不要自行猜测域名'
    )


def _credentials(system: str) -> tuple[str, str]:
  prefix = 'DCIM' if system == 'dcim' else 'MGMT'
  return (
    getattr(settings, f'BROWSER_{prefix}_USERNAME', ''),
    getattr(settings, f'BROWSER_{prefix}_PASSWORD', ''),
  )


def _build_request(recipe: Recipe, params: dict | None) -> dict:
  resolved = resolve_params(recipe, params)
  internal = dict(resolved)
  internal['base_url'] = _base_url(recipe.system)
  if recipe.login:
    username, password = _credentials(recipe.system)
    if not username or not password:
      raise BrowserError(
        f'配方需要登录但未配置服务账号（BROWSER_{recipe.system.upper()}_USERNAME/PASSWORD）'
      )
    internal['username'] = username
    internal['password'] = password

  page_url = _page_url(recipe)
  for url in [page_url, *(s.get('url') for s in recipe.steps if s.get('url'))]:
    if url:
      _check_host(url, internal)
  if recipe.login and recipe.login.get('url'):
    _check_host(recipe.login['url'], internal)

  return {
    'system': recipe.system,
    'params': internal,
    'full_page': recipe.full_page,
    'recipe': {
      'url': page_url,
      'steps': list(recipe.steps),
      'ready': recipe.ready,
      'extract': list(recipe.extract),
      'login': recipe.login or None,
      'wait_until': 'domcontentloaded',
    },
  }


def _friendly_error(data: dict) -> str:
  stage = str(data.get('stage') or '')
  hint = _STAGE_HINTS.get(stage, '未知阶段')
  detail = str(data.get('error') or '')[:300]
  return f'页面截图失败（{stage}）：{hint}。原始错误：{detail}'


async def _post_capture(payload: dict) -> dict:
  base = settings.BROWSER_SERVICE_URL.rstrip('/')
  if not base:
    raise BrowserError('未配置 BROWSER_SERVICE_URL')
  timeout = httpx.Timeout(float(settings.BROWSER_TIMEOUT) + 15.0, connect=5.0)
  headers = {'X-Browser-Token': settings.BROWSER_SERVICE_TOKEN}
  try:
    async with httpx.AsyncClient(timeout=timeout) as client:
      resp = await client.post(f'{base}/v1/capture', json=payload, headers=headers)
  except httpx.RequestError as e:
    raise BrowserError(f'页面截图服务不可达（{base}）：{e}') from e
  try:
    data = resp.json()
  except ValueError:
    raise BrowserError(f'页面截图服务返回非 JSON（HTTP {resp.status_code}）')
  if not data.get('ok'):
    raise BrowserError(_friendly_error(data))
  return data


def _publish_bytes(thread_id: str, base: str, data: bytes, ext: str = 'png') -> str:
  """原子发布会话产物（绝不覆盖同名文件），返回文件名。

  先在同目录建 O_EXCL 临时文件写入，再硬链接到目标名：目标已存在时 link 抛
  FileExistsError，换下一个序号重试 —— 并发/重复调用都不会覆盖既有文件。
  最后用 (st_dev, st_ino) 校验链接成功（同 DeerFlow 的 samestat 校验）。
  """
  from core.sandbox import datasets as sandbox_datasets

  out_dir = sandbox_datasets.thread_dir(thread_id) / 'browser'
  out_dir.mkdir(parents=True, exist_ok=True)
  stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
  base = f'{base}-{stamp}'
  for i in range(200):
    name = f'{base}.{ext}' if i == 0 else f'{base}-{i}.{ext}'
    final = out_dir / name
    tmp = out_dir / f'.{name}.{os.getpid()}-{i}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
      with os.fdopen(fd, 'wb') as fh:
        fh.write(data)
      st_tmp = os.stat(tmp)
      try:
        os.link(tmp, final)
      except FileExistsError:
        continue
      st_final = os.stat(final)
      if (st_tmp.st_dev, st_tmp.st_ino) != (st_final.st_dev, st_final.st_ino):
        final.unlink(missing_ok=True)
        raise BrowserError('产物发布校验失败（inode 不一致）')
      return name
    finally:
      try:
        tmp.unlink(missing_ok=True)
      except OSError:
        pass
  raise BrowserError('产物文件名冲突过多，未保存')


def _publish_png(thread_id: str, recipe_id: str, data: bytes) -> str:
  """原子发布截图（绝不覆盖同名文件），返回文件名。"""
  return _publish_bytes(thread_id, recipe_id, data, 'png')


def _shrink_tables(tables: list | None) -> list[dict]:
  out: list[dict] = []
  for table in (tables or [])[:2]:
    rows = table.get('rows') or []
    out.append({
      'name': table.get('name') or '',
      'headers': [str(h)[:_CELL_CHARS] for h in (table.get('headers') or [])[:_TABLE_COLS_LIMIT]],
      'rows': [
        [str(c)[:_CELL_CHARS] for c in (row or [])[:_TABLE_COLS_LIMIT]]
        for row in rows[:_TABLE_ROWS_LIMIT]
      ],
      'row_count': int(table.get('row_count') or len(rows)),
      'truncated': bool(table.get('truncated')) or len(rows) > _TABLE_ROWS_LIMIT,
    })
  return out


async def call(capability_id: str, params: dict, *, on_behalf_of: str | None = None) -> dict:
  """执行一条页面截图能力。参数与错误消息均面向模型，可直接告知用户下一步。"""
  if not settings.BROWSER_ENABLED:
    raise BrowserError('页面截图能力未启用（BROWSER_ENABLED=false）')
  recipe = get_recipe(capability_id)
  if recipe is None:
    raise BrowserError(f'未知的页面截图能力: {capability_id}')

  thread_id = get_current_thread_id()
  if not thread_id:
    raise BrowserError('缺少会话上下文，无法保存截图')

  try:
    payload = _build_request(recipe, params)
  except RecipeError as e:
    raise BrowserError(f'页面截图参数不合法：{e}') from e

  data = await _post_capture(payload)
  try:
    image = base64.b64decode(data.get('image_b64') or '')
  except (ValueError, TypeError) as e:
    raise BrowserError(f'截图数据解码失败：{e}') from e
  if not image:
    raise BrowserError('截图数据为空')

  name = _publish_png(thread_id, recipe.id, image)
  url = (
    f'/api/v1/chat/agent/artifacts/{quote(thread_id)}/browser/{quote(name)}'
  )
  page = data.get('page') or {}
  tables = _shrink_tables(data.get('tables'))
  text = str(data.get('text') or '')[: _TEXT_LIMIT]
  note = '截图已生成。要展示给用户时，用一行 <file url="…" name="…"/> 输出 artifacts[0] 的 url/name。'
  if data.get('image_truncated'):
    note += '（页面过长，截图仅覆盖可视区；需要完整内容请用表格/正文。）'

  logger.info(
    'browser 截图完成 capability=%s thread=%s file=%s bytes=%d',
    capability_id, thread_id, name, len(image),
  )
  return {
    'ok': True,
    'page': {'url': page.get('url') or '', 'title': page.get('title') or ''},
    'artifacts': [{'name': name, 'url': url, 'size': len(image)}],
    'text': text,
    'tables': tables,
    'note': note,
  }

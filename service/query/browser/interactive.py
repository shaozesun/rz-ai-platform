"""browser 平台交互能力 —— 模型逐步操作页面的 7 个工具（`BROWSER_EXPLORE_ENABLED`，默认关）。

配方模式只覆盖「页面固定 + 筛选固定」；以下三类覆盖不到：
  1) 从列表点进详情页/弹窗逐条看；
  2) 用户直接给一个内网链接，路径事先无法枚举；
  3) 登录后还要切菜单/选组织才能到达目标页。

**会话在旁路容器里**（单进程 → 任意后端 worker 都能命中同一会话）。会话 id 由当前
thread_id 确定性派生（sha1 前 16 位），**不进工具参数**：模型只管点页面，不管会话生命周期。

安全：导航目标先过 `BROWSER_ALLOWED_HOSTS`（复用 client._check_host），旁路服务用同一
环境变量兜底；截图沿用配方模式的会话产物链路（复用 client._publish_png，绝不覆盖）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from urllib.parse import quote, urlparse

import httpx

from config.settings import settings
from core.agent.user_context import get_current_thread_id
from service.query.base.capability import Capability
from service.query.browser import client
from service.query.browser.client import BrowserError
from service.query.browser.recipes import LOGIN_RECIPES

logger = logging.getLogger(__name__)

_MAX_ELEMENTS = 120
_NAME_CHARS = 100

_NAVIGATE_NOTE = (
  '已打开页面。elements 是本页可交互元素，click 时传它的 ref；也可传 text 按可见文字点击。'
  '内网文档页（共享文档 / 文档管理）按「公司 × 部门」隔离：先点顶部「运维管理」展开左侧栏，'
  '点「部门：xxx（下拉可切换）」切到目标分部（如 A1分部），再点左侧「共享文档」；'
  '列表显示「暂无数据」时先怀疑部门没切对，不要点「全部」（那是文件类型筛选）。'
  '树/脑图页面（节点名可能在 elements 之外）：先 browser__get_text 看层级文字，'
  '再用 click(text="节点名") 逐级进入，展开器名为「展开：节点名」。'
  '表格行文件名（如 廊坊-A1-…）也直接 click(text=…) 进入，单击无效改 count=2 双击。'
  '同名多个时返回 candidates，按 nth 重试（看 where：表格行是进入下一层，面包屑是往上层跳）；'
  '点不动时改用 count=2 双击或 button="right" 右键，'
  '再 snapshot 看菜单项。不要用 mgmt 的脑图查询代替页面导航。'
  'blocked:true 的元素是危险操作，禁止点击；'
  '页面异步刷新后 ref 会失效，需重新 snapshot。浏览结束请调用 browser__close。'
)

_CAPS: tuple[Capability, ...] = (
  Capability(
    id='browser.navigate',
    name='打开页面',
    description=(
      '打开一个内网系统页面（http/https），返回页面标题、地址与可交互元素编号 [ref]。'
      '这是交互浏览的第一步：必须先 navigate 才能 click/snapshot/get_text/screenshot/record。'
      '用于配方覆盖不到的页面（详情页、用户直接给的链接、需要切菜单的页面）；'
      '要精确数据请用对应平台的数据查询能力，不要靠浏览页面取数。'
      '这些内网系统是单页应用：只保证系统根地址能打开，业务页面要从根地址按元素名字点进去，'
      '不要自行拼接 /pen/... 之类的接口路径。登录由平台自动完成，不需要也不接受用户凭据。'
    ),
    params={
      'url': '要打开的页面地址（http/https）；优先用系统根地址，再通过菜单进入业务页',
      'system': '可选：目标系统，决定使用哪套登录态；不传则按地址自动推断',
    },
    param_enums={'system': ('dcim', 'mgmt')},
    category='page',
    domain='explore',
    kind='动作',
  ),
  Capability(
    id='browser.snapshot',
    name='刷新页面元素',
    description=(
      '重新扫描当前页面并刷新可交互元素编号（ref），不执行任何点击。'
      '页面异步更新后旧 ref 会失效，拿不准时先 snapshot 再 click。'
    ),
    category='page',
    domain='explore',
    kind='状态',
  ),
  Capability(
    id='browser.click',
    name='点击页面元素',
    description=(
      '点击页面元素，点击后返回新页面的元素编号。ref 与 text 二选一：'
      'ref 用 navigate/snapshot 返回的数字编号；text 用元素可见文字（树/脑图/菜单节点优先用它，'
      '先 browser__get_text 看准名称）。同名多个时不会猜，会返回 candidates，'
      '按 nth 选一个重试。点不动或需要菜单/进入目录时，用 count=2 双击或 button=right 右键，'
      '再 snapshot 看菜单项。点开新标签页会自动切过去（返回 popup:true）。'
      'ref 失效会明确报错，此时应先 browser__snapshot 重新编号再点。'
      'blocked:true 的元素（删除/提交/审批等）被只读策略禁止点击，会返回 blocked 错误，'
      '不要反复重试，请向用户说明需人工完成。'
    ),
    params={
      'ref': '可选：元素编号（navigate/snapshot 返回的 ref 数字），与 text 二选一',
      'text': '可选：元素可见文字，与 ref 二选一；树/脑图/菜单节点优先用它',
      'nth': '可选：同名元素序号（1 起，取自返回的 candidates）',
      'button': '可选：left 左键（默认）/ right 右键打开菜单',
      'count': '可选：1 单击（默认）/ 2 双击（进入目录或详情）',
    },
    param_enums={'button': ('left', 'right')},
    category='page',
    domain='explore',
    kind='动作',
  ),
  Capability(
    id='browser.get_text',
    name='读取页面文本',
    description=(
      '读取当前页面可见文本（默认 8000 字，上限 8000），用于读出页面内容或确认操作结果。'
    ),
    params={'max_chars': '可选：最多读取多少字符（整数，上限 8000）'},
    category='page',
    domain='explore',
    kind='状态',
  ),
  Capability(
    id='browser.screenshot',
    name='当前页面截图',
    description=(
      '对当前页面截图并保存为会话产物，返回 artifacts（url/name）。'
      '要给用户看图时，用一行 <file url="…" name="…"/> 输出截图的 url/name。'
    ),
    params={'full_page': '可选：true 截整页、false 只截可视区（默认 false）'},
    category='page',
    domain='explore',
    kind='动作',
  ),
  Capability(
    id='browser.record',
    name='录制页面视频',
    description=(
      '在当前页面录一段固定时长的屏幕（默认 30 秒，5~120 秒），返回按时间排列的关键帧截图'
      '（主交付）和可选的 webm 视频。必须先 browser__navigate 并进入目标页。'
      '录制的是当前页面：不重新加载，也不关闭浏览会话；录制期间不要同时调用其他页面工具。'
      '关键帧适合给用户看或交给后续分析；webm 无音频、无鼠标指针。只要静态截图请用 browser__screenshot。'
    ),
    params={
      'seconds': '可选：录制时长秒数（5~120，默认 30）',
      'keyframe_interval': '可选：关键帧间隔秒数（默认 5，最多 12 张，过密会自动放大间隔）',
      'video': '可选：是否同时生成 webm 视频（默认 true；只要关键帧传 false）',
    },
    category='page',
    domain='explore',
    kind='动作',
  ),
  Capability(
    id='browser.close',
    name='关闭浏览会话',
    description=(
      '关闭本会话的浏览器上下文并释放内存。一次浏览结束后应调用；不调用会在空闲超时后被回收。'
    ),
    category='page',
    domain='explore',
    kind='动作',
  ),
)

_ACTIONS = {cap.id.split('.', 1)[1] for cap in _CAPS}


def build_interactive_capabilities() -> tuple[Capability, ...]:
  """7 条交互能力（开关判断由 platform/bootstrap 负责）。"""
  return _CAPS


def tool_names() -> set[str]:
  """7 个工具名（browser__navigate 等），供 agent 构建时强制常驻可见。

  这批工具**必须成套出现**：若 navigate 被 promote 而 click 没被 promote，
  流程走到第二步就断。schema 都很小，常驻代价可忽略。
  """
  return {cap.id.replace('.', '__') for cap in _CAPS}


def handles(capability_id: str) -> bool:
  """是否属于交互能力的 7 个动作（供 platform caller 分派）。"""
  if not capability_id.startswith('browser.'):
    return False
  return capability_id.split('.', 1)[1] in _ACTIONS


def _session_id(thread_id: str) -> str:
  """一个线程一个会话；id 不暴露给模型，避免它管理会话生命周期。"""
  return hashlib.sha1(thread_id.encode('utf-8')).hexdigest()[:16]


def _system_for(url: str) -> str:
  """按页面根地址的主机名推断 system（路径不参与：/center 只是 mgmt 的一个页面）。"""
  host = (urlparse(url).hostname or '').lower()
  if not host:
    return ''
  dcim = (urlparse(settings.BROWSER_DCIM_URL or settings.DCIM_BASE_URL).hostname or '').lower()
  if dcim and host == dcim:
    return 'dcim'
  mgmt = (urlparse(settings.BROWSER_MGMT_URL).hostname or '').lower()
  if mgmt and host == mgmt:
    return 'mgmt'
  return ''


def _fill_credentials(obj, username: str, password: str):
  """登录模板里的 {username}/{password} 是平台自己的占位符（不是模型输入），直接替换。"""
  if isinstance(obj, str):
    return obj.replace('{username}', username).replace('{password}', password)
  if isinstance(obj, dict):
    return {k: _fill_credentials(v, username, password) for k, v in obj.items()}
  if isinstance(obj, list):
    return [_fill_credentials(v, username, password) for v in obj]
  return obj


def _prepare_navigate(body: dict, params: dict) -> None:
  url = str(params.get('url') or '').strip()
  parsed = urlparse(url)
  if parsed.scheme not in ('http', 'https') or not parsed.hostname:
    raise BrowserError('navigate 只接受完整的 http/https 地址')
  client._check_host(url, {})

  system = str(params.get('system') or '').strip() or _system_for(url)
  if system not in ('dcim', 'mgmt'):
    system = ''
  body.update({'url': url, 'system': system})

  login = LOGIN_RECIPES.get(system)
  if not login or not login.get('enabled'):
    return  # 未校准/未配置：依赖事先落盘的 storageState
  username, password = client._credentials(system)
  if not username or not password:
    raise BrowserError(
      f'需要登录但未配置服务账号（BROWSER_{system.upper()}_USERNAME/PASSWORD）'
    )
  body['login'] = _fill_credentials(
    {k: v for k, v in login.items() if k != 'enabled'}, username, password,
  )


async def _post_session(payload: dict) -> dict:
  base = settings.BROWSER_SERVICE_URL.rstrip('/')
  if not base:
    raise BrowserError('未配置 BROWSER_SERVICE_URL')
  if payload.get('action') == 'record':
    # 旁路服务侧超时是 BROWSER_TIMEOUT + seconds + 90，这里再多留传输/编码余量。
    seconds = int(payload.get('seconds') or settings.BROWSER_RECORD_SECONDS)
    timeout = httpx.Timeout(float(settings.BROWSER_TIMEOUT) + seconds + 120.0, connect=5.0)
  else:
    timeout = httpx.Timeout(float(settings.BROWSER_TIMEOUT) + 15.0, connect=5.0)
  headers = {'X-Browser-Token': settings.BROWSER_SERVICE_TOKEN}
  try:
    async with httpx.AsyncClient(timeout=timeout) as client_http:
      resp = await client_http.post(f'{base}/v1/session', json=payload, headers=headers)
  except httpx.RequestError as e:
    raise BrowserError(f'页面浏览服务不可达（{base}）：{e}') from e
  try:
    data = resp.json()
  except ValueError:
    raise BrowserError(f'页面浏览服务返回非 JSON（HTTP {resp.status_code}）')
  if not data.get('ok'):
    raise BrowserError(client._friendly_error(data))
  return data


def _shrink_elements(elements) -> list[dict]:
  out: list[dict] = []
  for el in (elements or [])[:_MAX_ELEMENTS]:
    item = {'ref': el.get('ref'), 'tag': el.get('tag') or ''}
    if el.get('role'):
      item['role'] = el['role']
    if el.get('type'):
      item['type'] = el['type']
    item['name'] = str(el.get('name') or '')[:_NAME_CHARS]
    if el.get('blocked'):
      item['blocked'] = True
    out.append(item)
  return out


def _page_payload(data: dict) -> dict:
  return {
    'ok': True,
    'url': str(data.get('url') or ''),
    'title': str(data.get('title') or ''),
    'elements': _shrink_elements(data.get('elements')),
  }


def _artifact_url(thread_id: str, name: str) -> str:
  return f'/api/v1/chat/agent/artifacts/{quote(thread_id)}/browser/{quote(name)}'


def _shape(action: str, data: dict, thread_id: str) -> dict:
  if action in ('navigate', 'snapshot', 'click'):
    if action == 'click' and data.get('ambiguous'):
      candidates = []
      for i, c in enumerate(data.get('candidates') or []):
        item = {
          'nth': i + 1,
          'tag': str(c.get('tag') or ''),
          'name': str(c.get('name') or '')[:_NAME_CHARS],
        }
        near = [str(n)[:40] for n in (c.get('near') or []) if str(n).strip()]
        if near:
          item['near'] = near
        candidates.append(item)
      total = int(data.get('total') or len(candidates))
      return {
        'ok': True,
        'clicked': False,
        'ambiguous': True,
        'total': total,
        'candidates': candidates,
        'note': (
          f'有 {total} 个文字相同的元素，未点击（不猜）。'
          '请按 candidates 的 near（邻近节点名）判断是哪一个，再用 nth 重试。'
        ),
      }
    out = _page_payload(data)
    if action == 'navigate':
      out['note'] = _NAVIGATE_NOTE
    elif action == 'click':
      out['clicked'] = True
      if data.get('popup'):
        out['popup'] = True
        out['note'] = '点击打开了新标签页，会话已切到新页面（elements 属于新页面）。'
      else:
        out['note'] = '点击后的页面元素已重新编号（ref 已刷新）。'
    return out

  if action == 'get_text':
    return {
      'ok': True,
      'text': str(data.get('text') or ''),
      'truncated': bool(data.get('truncated')),
    }

  if action == 'screenshot':
    try:
      image = base64.b64decode(data.get('image_b64') or '')
    except (ValueError, TypeError) as e:
      raise BrowserError(f'截图数据解码失败：{e}') from e
    if not image:
      raise BrowserError('截图数据为空')
    name = client._publish_png(thread_id, 'page_screenshot', image)
    url = _artifact_url(thread_id, name)
    page = data.get('page') or {}
    note = '截图已生成。要展示给用户时，用一行 <file url="…" name="…"/> 输出 artifacts[0]。'
    if data.get('image_truncated'):
      note += '（页面过长，截图仅覆盖可视区。）'
    return {
      'ok': True,
      'page': {'url': page.get('url') or '', 'title': page.get('title') or ''},
      'artifacts': [{'name': name, 'url': url, 'size': len(image)}],
      'note': note,
    }

  if action == 'record':
    page = data.get('page') or {}
    artifacts: list[dict] = []
    for i, item in enumerate(data.get('keyframes') or []):
      try:
        image = base64.b64decode(item.get('image_b64') or '')
      except (ValueError, TypeError) as e:
        raise BrowserError(f'关键帧数据解码失败：{e}') from e
      if not image:
        continue
      name = client._publish_bytes(thread_id, f'rec_frame_{i:02d}', image, 'jpg')
      artifacts.append({
        't': float(item.get('t') or 0.0),
        'name': name,
        'url': _artifact_url(thread_id, name),
        'size': len(image),
      })
    if not artifacts:
      raise BrowserError('录制未产生任何关键帧')

    video: dict | None = None
    video_b64 = data.get('video_b64') or ''
    if video_b64:
      try:
        blob = base64.b64decode(video_b64)
      except (ValueError, TypeError) as e:
        raise BrowserError(f'视频数据解码失败：{e}') from e
      if blob:
        name = client._publish_bytes(thread_id, 'rec_video', blob, 'webm')
        video = {'name': name, 'url': _artifact_url(thread_id, name), 'size': len(blob)}

    note = (
      '关键帧已按时间生成；展示给用户时逐张用一行 <file url="…" name="…"/> 输出 artifacts。'
      '视频（webm）无音频、无鼠标指针，用户明确要视频时再输出 video。'
    )
    video_error = str(data.get('video_error') or '')
    if video_error:
      note += f'本次未生成视频：{video_error}；关键帧仍可用。'
    return {
      'ok': True,
      'page': {'url': page.get('url') or '', 'title': page.get('title') or ''},
      'duration_s': int(data.get('duration_s') or 0),
      'frames_captured': int(data.get('frames_captured') or 0),
      'artifacts': artifacts,
      'video': video,
      'note': note,
    }

  if action == 'close':
    return {'ok': True, 'closed': bool(data.get('closed'))}

  raise BrowserError(f'未知的页面浏览动作: {action}')


async def call(capability_id: str, params: dict, *, on_behalf_of: str | None = None) -> dict:
  """执行一条交互能力。参数与错误消息均面向模型，可直接告知用户下一步。"""
  if not settings.BROWSER_EXPLORE_ENABLED:
    raise BrowserError('交互浏览能力未启用（BROWSER_EXPLORE_ENABLED=false）')
  action = capability_id.split('.', 1)[1] if capability_id.startswith('browser.') else ''
  if action not in _ACTIONS:
    raise BrowserError(f'未知的页面浏览能力: {capability_id}')

  thread_id = get_current_thread_id()
  if not thread_id:
    raise BrowserError('缺少会话上下文，无法操作页面')

  try:
    return await _run_action(action, params, thread_id)
  except BrowserError as e:
    # 成功才打日志会让失败完全静默（只能翻 checkpoint 反查），这里补一条。
    logger.warning('browser 交互动作失败 action=%s thread=%s error=%s', action, thread_id, e)
    raise


def _int_param(raw, default: int, label: str) -> int:
  text = str(raw if raw is not None else '').strip()
  if not text:
    return default
  if not text.lstrip('-').isdigit():
    raise BrowserError(f'{label} 必须是整数')
  return int(text)


def _bool_param(raw, default: bool) -> bool:
  text = str(raw if raw is not None else '').strip().lower()
  if not text:
    return default
  return text in ('1', 'true', 'yes', 'on')


async def _run_action(action: str, params: dict, thread_id: str) -> dict:
  body: dict = {'action': action, 'session_id': _session_id(thread_id)}
  if action == 'navigate':
    _prepare_navigate(body, params)
  elif action == 'click':
    ref = str(params.get('ref') or '').strip()
    text = str(params.get('text') or '').strip()
    if bool(ref) == bool(text):
      raise BrowserError('click 需要 ref 或 text 之一（二选一）')
    if ref:
      if not ref.isdigit():
        raise BrowserError('ref 必须是 snapshot/navigate 返回的数字编号')
      body['ref'] = ref
    else:
      body['text'] = text

    nth = str(params.get('nth') or '').strip()
    if nth:
      if not nth.isdigit() or int(nth) < 1:
        raise BrowserError('nth 必须是从 1 开始的整数（取自 candidates 的序号）')
      body['nth'] = nth

    button = str(params.get('button') or 'left').strip().lower()
    if button not in ('left', 'right'):
      raise BrowserError('button 只能是 left 或 right')
    body['button'] = button

    count = str(params.get('count') or '1').strip()
    if count not in ('1', '2'):
      raise BrowserError('count 只能是 1（单击）或 2（双击）')
    if button == 'right' and count == '2':
      raise BrowserError('不支持右键双击：请单独用 button="right" 或 count=2')
    body['count'] = count
  elif action == 'get_text':
    raw = str(params.get('max_chars') or '').strip()
    if raw:
      if not raw.isdigit():
        raise BrowserError('max_chars 必须是整数')
      body['max_chars'] = raw
  elif action == 'screenshot':
    raw = str(params.get('full_page') or '').strip().lower()
    if raw:
      body['full_page'] = raw in ('1', 'true', 'yes', 'on')
  elif action == 'record':
    seconds = _int_param(
      params.get('seconds'), settings.BROWSER_RECORD_SECONDS, 'seconds',
    )
    seconds = max(5, min(seconds, settings.BROWSER_MAX_RECORD_SECONDS))
    interval = _int_param(
      params.get('keyframe_interval'), settings.BROWSER_RECORD_KEYFRAME_SECONDS,
      'keyframe_interval',
    )
    body['seconds'] = seconds
    body['keyframe_interval'] = max(1, min(interval, seconds))
    body['video'] = _bool_param(params.get('video'), True)

  data = await _post_session(body)
  logger.info('browser 交互动作完成 action=%s thread=%s', action, thread_id)
  return _shape(action, data, thread_id)

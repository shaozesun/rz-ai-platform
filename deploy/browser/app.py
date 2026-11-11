"""Playwright 旁路服务 —— 执行声明式配方：导航 → 设筛选 → 等就绪 → 截图 → 抽取。

生产后端是 `uvicorn --workers 4`，有状态的浏览器会话不能放在后端进程里，故独立成容器：
后端发配方（页面固定、参数可变），本服务只做通用执行，不持有任何业务知识。

接口：
  GET  /health       Chromium 可用性 + 活跃会话数
  POST /v1/capture   执行配方 + 截图 + 抽表格/正文（无状态，每请求一个 context）
  POST /v1/session   交互浏览（按 session_id 保持 context/page，模型逐步点）
  POST /v1/inspect   dump 可交互元素与候选选择器（配食谱用，不暴露给模型）

失败一律返回 `{ok: false, stage, error}`，stage ∈
recipe | login | login_page | login_conflict | navigate | apply_filters | ready |
extract | screenshot | session | click | blocked | text | record | video | timeout。

安全边界：只执行请求里的步骤；导航目标由后端白名单（BROWSER_ALLOWED_HOSTS）先行校验，
本服务可选再用同一环境变量兜底；除 `/health` 外均校验 `X-Browser-Token`。
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import hmac
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from playwright.async_api import Browser, Locator, Page, async_playwright

# ---- 环境配置（与后端 settings 同名，compose 统一注入） ----
TOKEN = os.environ.get('BROWSER_SERVICE_TOKEN', '')
STATE_DIR = Path(os.environ.get('BROWSER_STATE_DIR', '/state'))
MAX_CONCURRENCY = max(1, int(os.environ.get('BROWSER_MAX_CONCURRENCY', '2')))
CAPTURE_TIMEOUT = float(os.environ.get('BROWSER_TIMEOUT', '60'))
ALLOWED_HOSTS = {
  h.strip().lower() for h in os.environ.get('BROWSER_ALLOWED_HOSTS', '').split(',') if h.strip()
}
MAX_IMAGE_BYTES = int(float(os.environ.get('BROWSER_MAX_IMAGE_MB', '8')) * 1024 * 1024)
MAX_TEXT_CHARS = 8000
MAX_TABLE_ROWS = 500

# 交互浏览：会话数量/空闲回收（Chromium context 常驻约 100~200MB，须与 mem_limit 配套）
MAX_SESSIONS = max(1, int(os.environ.get('BROWSER_MAX_SESSIONS', '4')))
SESSION_IDLE_SECONDS = float(os.environ.get('BROWSER_SESSION_IDLE_SECONDS', '900'))
_REAP_INTERVAL_S = 60

# 交互录屏（browser__record）：CDP 采帧 + ffmpeg 合成 webm，关键帧同时交付。
RECORD_DEFAULT_SECONDS = max(5, int(os.environ.get('BROWSER_RECORD_SECONDS', '30')))
RECORD_MAX_SECONDS = max(5, int(os.environ.get('BROWSER_MAX_RECORD_SECONDS', '120')))
RECORD_DEFAULT_INTERVAL = max(1, int(os.environ.get('BROWSER_RECORD_KEYFRAME_SECONDS', '5')))
RECORD_MAX_KEYFRAMES = max(1, int(os.environ.get('BROWSER_MAX_KEYFRAMES', '12')))
RECORD_FPS = min(15, max(1, int(os.environ.get('BROWSER_RECORD_FPS', '10'))))
RECORD_MAX_VIDEO_BYTES = int(float(os.environ.get('BROWSER_MAX_VIDEO_MB', '30')) * 1024 * 1024)
_RECORD_FFMPEG_TIMEOUT_S = 180

# 点击黑名单（只读浏览）：名字命中危险动词的元素在快照里标 blocked，click 时直接拒绝。
# 只读账号仍可能允许确认告警/屏蔽/改阈值等业务写操作，这一层是给模型的显式护栏。
# `BROWSER_CLICK_DENYLIST` 整体覆盖内置表（逗号分隔）；显式设为空串 = 关闭拦截。
_DEFAULT_CLICK_DENYLIST = (
  '删除', '移除', '清除', '清空', '重置', '复位', '恢复',
  '提交', '保存', '确认', '编辑', '修改', '新增', '创建', '导入', '上传', '导出', '打印',
  '撤回', '撤销', '取消', '发布', '审批', '驳回', '拒绝', '通过',
  '下发', '控制', '启停', '启动', '停止', '重启', '关机', '开机',
  '停用', '启用', '禁用', '注销', '退出', '屏蔽', '抑制', '解绑', '绑定',
)


def _load_click_denylist() -> tuple[str, ...]:
  raw = os.environ.get('BROWSER_CLICK_DENYLIST')
  if raw is None:
    return _DEFAULT_CLICK_DENYLIST
  words: list[str] = []
  for part in raw.split(','):
    word = part.strip()
    if word and word not in words:
      words.append(word)
  return tuple(words)


CLICK_DENYLIST = _load_click_denylist()

# 点击超时压到 8s：ref 失效时要尽快失败让模型重新 snapshot，不要卡满 15s
_CLICK_TIMEOUT_MS = 8000
_POST_CLICK_LOAD_TIMEOUT_MS = 3000

DEFAULT_STEP_TIMEOUT_MS = 15000
DEFAULT_VIEWPORT = {'width': 1440, 'height': 900}

# 登录判定：token 失效时前端会先渲染旧界面、再被接口 401 踢回登录页（实测残影约 0.7s），
# 所以命中「已登录标记」后还要等页面静默复查一次；未命中时用登录表单做短探测，
# 判断不了就快速失败，别把「不是登录页」盲等到 15s 再误报成登录态失效。
LOGIN_PROBE_MS = 5000

# 参数占位符：{park} / {hours}；只有请求里显式给了 params 的同名键才放行。
_PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')
# 会话 id 由后端从 thread_id 派生（sha1 前 16 位），只允许安全字符
_SESSION_ID_RE = re.compile(r'^[A-Za-z0-9_-]{8,64}$')

_browser: Browser | None = None
_sem: asyncio.Semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
# 录制单独限流：长录制不能占着 _sem 把其他截图/会话饿住；同时只允许一路。
_record_sem: asyncio.Semaphore = asyncio.Semaphore(1)
_playwright = None


@dataclass
class LiveSession:
  """一个常驻浏览器上下文：同一会话的操作靠 lock 串行（模型可能并行发 snapshot+click）。"""

  context: object
  page: Page
  system: str
  created_at: float
  last_used: float
  lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_sessions: dict[str, LiveSession] = {}
_sessions_lock = asyncio.Lock()
_reaper: asyncio.Task | None = None


class RecipeError(Exception):
  """配方本身不合法（缺字段/未知占位符/未知动作）。"""


class LoginPageMissing(RecipeError):
  """当前地址不是登录页（通常是 URL 拼错，不是凭据问题）。"""


def _now_iso() -> str:
  from datetime import datetime
  return datetime.now().astimezone().isoformat(timespec='seconds')


# ============================ 启动 / 关闭 ============================

@asynccontextmanager
async def lifespan(_app: FastAPI):
  global _browser, _playwright, _reaper
  STATE_DIR.mkdir(parents=True, exist_ok=True)
  _playwright = await async_playwright().start()
  _browser = await _playwright.chromium.launch(
    headless=True,
    args=[
      '--no-sandbox',
      '--disable-dev-shm-usage',       # /dev/shm 默认 64MB，不关必崩
      '--disable-gpu',
      '--font-render-hinting=none',
      # 监控页视频需要免点击自动播放，否则录制/截图得到的是播放器占位图
      '--autoplay-policy=no-user-gesture-required',
    ],
  )
  _reaper = asyncio.create_task(_reap_loop())
  try:
    yield
  finally:
    if _reaper is not None:
      _reaper.cancel()
      with suppress(asyncio.CancelledError):
        await _reaper
      _reaper = None
    await _close_all_sessions()
    if _browser is not None:
      await _browser.close()
    if _playwright is not None:
      await _playwright.stop()


app = FastAPI(title='browser-sidecar', version='1.0.0', lifespan=lifespan)


def _require_token(x_browser_token: str | None) -> None:
  if not TOKEN:
    return  # 未配置 token（本地自测）：放行
  if not x_browser_token or not hmac.compare_digest(x_browser_token, TOKEN):
    raise HTTPException(status_code=401, detail='invalid browser token')


# ============================ 配方渲染 / 定位 ============================

def _render(obj, params: dict):
  """把配方里的 `{name}` 替换成 params[name]；未知占位符直接判配方错误。

  递归处理 dict/list/str，非字符串原样返回。
  """
  if isinstance(obj, str):
    def repl(m: re.Match) -> str:
      key = m.group(1)
      if key not in params:
        raise RecipeError(f'未知参数占位符 {{{key}}}')
      val = params[key]
      if val is None:
        raise RecipeError(f'参数 {key} 为空')
      text = str(val)
      if '\n' in text or '\x00' in text:
        raise RecipeError(f'参数 {key} 含非法字符')
      return text
    return _PLACEHOLDER.sub(repl, obj)
  if isinstance(obj, dict):
    return {k: _render(v, params) for k, v in obj.items()}
  if isinstance(obj, list):
    return [_render(v, params) for v in obj]
  return obj


def _locate(scope: Page | Locator, spec) -> Locator:
  """定位器 DSL：字符串 = CSS；dict 用语义定位。

  `{by: 'text', name: '查询', exact: false}`
  `{by: 'role', role: 'button', name: '提交'}`
  `{by: 'label', name: '用户名'}` / `{by: 'testid', name: 'search'}` / `{by: 'css', css: '#id'}`
  """
  if isinstance(spec, str):
    return scope.locator(spec)
  if not isinstance(spec, dict):
    raise RecipeError(f'非法定位器: {spec!r}')
  by = spec.get('by', 'css')
  exact = bool(spec.get('exact', False))
  if by == 'css':
    css = spec.get('css') or spec.get('value')
    if not css:
      raise RecipeError('css 定位器缺少 css/value')
    return scope.locator(css)
  if by == 'text':
    name = spec.get('name') or spec.get('text') or spec.get('value')
    if not name:
      raise RecipeError('text 定位器缺少 name/text')
    return scope.get_by_text(name, exact=exact)
  if by == 'role':
    role = spec.get('role')
    if not role:
      raise RecipeError('role 定位器缺少 role')
    return scope.get_by_role(role, name=spec.get('name') or None, exact=exact)
  if by == 'label':
    name = spec.get('name') or spec.get('label') or spec.get('value')
    if not name:
      raise RecipeError('label 定位器缺少 name')
    return scope.get_by_label(name, exact=exact)
  if by == 'testid':
    name = spec.get('name') or spec.get('value')
    if not name:
      raise RecipeError('testid 定位器缺少 name')
    return scope.get_by_test_id(name)
  raise RecipeError(f'未知定位方式: {by}')


# ============================ 步骤执行 ============================

async def _run_step(page: Page, step: dict) -> None:
  action = step.get('action')
  timeout = int(step.get('timeout_ms', DEFAULT_STEP_TIMEOUT_MS))
  if action == 'goto':
    await _goto(page, step.get('url') or '', timeout, step.get('wait_until', 'domcontentloaded'))
    return
  if action == 'wait_ms':
    await asyncio.sleep(float(step.get('ms', 500)) / 1000.0)
    return
  if action == 'set_content':
    await page.set_content(step.get('html') or '', wait_until='domcontentloaded')
    return
  if action == 'wait_for':
    loc = _locate(page, step.get('selector') or step.get('locator'))
    await loc.first.wait_for(state=step.get('state', 'visible'), timeout=timeout)
    return
  if action == 'wait_load':
    await page.wait_for_load_state(step.get('state', 'networkidle'), timeout=timeout)
    return

  loc = _locate(page, step.get('selector') or step.get('locator')).first
  if action == 'click':
    await loc.click(timeout=timeout)
  elif action == 'fill':
    await loc.fill(str(step.get('value', '')), timeout=timeout)
  elif action == 'select':
    await loc.select_option(str(step.get('value', '')), timeout=timeout)
  elif action == 'check':
    await loc.check(timeout=timeout)
  elif action == 'uncheck':
    await loc.uncheck(timeout=timeout)
  elif action == 'press':
    await loc.press(str(step.get('key', 'Enter')), timeout=timeout)
  elif action == 'hover':
    await loc.hover(timeout=timeout)
  else:
    raise RecipeError(f'未知步骤动作: {action}')


async def _goto(page: Page, url: str, timeout: int, wait_until: str) -> None:
  if not url:
    raise RecipeError('goto 缺少 url')
  parsed = urlparse(url)
  if parsed.scheme in ('http', 'https') and ALLOWED_HOSTS:
    host = (parsed.hostname or '').lower()
    if host not in ALLOWED_HOSTS:
      raise RecipeError(f'导航目标不在白名单: {host}')
  await page.goto(url, wait_until=wait_until, timeout=timeout)


async def _wait_ready(page: Page, ready) -> None:
  if not ready:
    return
  loc = _locate(page, ready.get('selector') or ready.get('locator')).first
  await loc.wait_for(
    state=ready.get('state', 'visible'),
    timeout=int(ready.get('timeout_ms', DEFAULT_STEP_TIMEOUT_MS)),
  )


# ============================ 抽取 ============================

_TABLE_JS = """
(el) => {
  const t = el.tagName === 'TABLE' ? el : el.querySelector('table');
  if (!t) return null;
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  let headRow = t.querySelector('thead tr');
  let bodyRows = [...t.querySelectorAll('tbody tr')];
  if (!bodyRows.length) {
    const all = [...t.querySelectorAll('tr')];
    if (headRow) {
      bodyRows = all.filter(tr => !tr.closest('thead'));
    } else {
      const first = all[0];
      if (first && first.querySelectorAll('th').length) {
        headRow = first;
        bodyRows = all.slice(1);
      } else {
        bodyRows = all;
      }
    }
  }
  const headers = headRow ? [...headRow.querySelectorAll('th,td')].map(c => norm(c.innerText)) : [];
  // 只取可见行：页面筛选常是 display:none，不做这层过滤会把整表都抽回来
  const visible = bodyRows.filter(tr => getComputedStyle(tr).display !== 'none');
  const rows = visible.slice(0, %d).map(tr =>
    [...tr.querySelectorAll('td,th')].map(c => norm(c.innerText))
  );
  return {headers, rows, row_count: visible.length, truncated: visible.length > %d};
}
""" % (MAX_TABLE_ROWS, MAX_TABLE_ROWS)


async def _extract(page: Page, specs) -> dict:
  text_parts: list[str] = []
  tables: list[dict] = []
  html_parts: list[str] = []
  for spec in specs or []:
    kind = spec.get('kind', 'text')
    loc = _locate(page, spec.get('selector') or spec.get('locator'))
    if await loc.count() == 0:
      continue
    el = loc.first
    if kind == 'text':
      part = await el.inner_text()
      limit = int(spec.get('max_chars', MAX_TEXT_CHARS))
      text_parts.append(part[:limit])
    elif kind == 'html':
      part = await el.inner_html()
      limit = int(spec.get('max_chars', MAX_TEXT_CHARS))
      html_parts.append(part[:limit])
    elif kind == 'table':
      data = await el.evaluate(_TABLE_JS)
      if data:
        if spec.get('name'):
          data['name'] = spec['name']
        tables.append(data)
    else:
      raise RecipeError(f'未知抽取类型: {kind}')
  text = '\n'.join(p for p in text_parts if p.strip())
  return {
    'text': text[:MAX_TEXT_CHARS],
    'tables': tables,
    'html': '\n'.join(html_parts)[:MAX_TEXT_CHARS] if html_parts else '',
  }


# ============================ 登录态 ============================

def _state_path(system: str) -> Path:
  safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', system or 'default')
  return STATE_DIR / f'{safe}.json'


async def _save_state(context, system: str) -> bool:
  if not system:
    return False
  path = _state_path(system)
  try:
    tmp = path.with_suffix('.json.tmp')
    await context.storage_state(path=str(tmp))
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return True
  except Exception:
    return False


def _login_locator(login: dict):
  """登录配方里第一个带定位器的步骤 —— 它在，就说明当前页确实是登录页。"""
  for step in login.get('steps') or []:
    if isinstance(step, dict):
      spec = step.get('selector') or step.get('locator')
      if spec:
        return spec
  return None


async def _visible_after(page: Page, spec, timeout_ms: int) -> bool:
  """等元素可见（`timeout_ms=0` 表示只看一眼）；超时/找不到一律 False，不抛。"""
  if not spec:
    return False
  try:
    loc = _locate(page, spec).first
    if timeout_ms <= 0:
      return bool(await loc.is_visible())
    await loc.wait_for(state='visible', timeout=timeout_ms)
    return True
  except Exception:
    return False


async def _page_title(page: Page) -> str:
  with suppress(Exception):
    return await page.title()
  return ''


async def _ensure_login(page: Page, context, login: dict, system: str) -> bool:
  """按需跑登录配方，返回是否真的执行了登录（调用方据此决定要不要回到目标页）。

  check 必须「等」而不是「看一眼」：内网 SPA 在 DOMContentLoaded 时还没渲染，
  瞬时 count 会把已登录页面误判成登录页（实测 mgmt 首页约 1.1s 后才出现「人员管理」）。

  反向也要防「假已登录」：token 失效时前端会先用本地缓存渲染旧界面（实测约 0.7s），
  之后接口 401 才触发 forceLogout 跳登录页。命中标记后必须等页面静默再复查，
  残影消失就照常走登录；否则会跳过登录、storageState 永远停在失效状态。
  """
  if not login:
    return False
  default_timeout = int(login.get('timeout_ms', DEFAULT_STEP_TIMEOUT_MS))
  check = login.get('check')
  if check:
    spec = check.get('selector') or check.get('locator')
    timeout = int(check.get('timeout_ms') or min(default_timeout, 8000))
    if await _visible_after(page, spec, timeout):
      # 多等一小会儿再复查：旧界面残影常在标记出现后 ~0.7s 才被 401 踢掉
      await _settle(page, min_wait_ms=600)
      if await _visible_after(page, spec, 0):
        return False
      # 标记已消失：token 失效，继续走下面的登录步骤

  # 确实需要登录；先确认当前页是登录页，避免把拼错的 URL 盲等成「登录态失效」
  probe = _login_locator(login)
  if probe and not await _visible_after(page, probe, LOGIN_PROBE_MS):
    raise LoginPageMissing(
      f'目标地址不是登录页（当前页面：{await _page_title(page) or "无标题"}，{page.url}）。'
      '内网系统是单页应用：请用系统根地址打开，再按页面元素进入业务页面，'
      '不要自行拼接 /pen/... 之类的接口路径。'
    )

  login_url = login.get('url')
  if login_url:
    await _goto(page, login_url, default_timeout, 'domcontentloaded')
  for step in login.get('steps') or []:
    step = dict(step)
    step.setdefault('timeout_ms', default_timeout)
    await _run_step(page, step)
  await _wait_ready(page, login.get('ready'))
  if login.get('save_state', True):
    await _save_state(context, system)
  return True


# ============================ 会话（交互浏览） ============================

_SNAPSHOT_JS = r"""
(payload) => {
  const start = payload.start;
  const deny = payload.deny || [];
  // 先清掉本 frame 上一轮的数据，避免 SPA 残留的隐藏节点抢先命中 [data-rz-ref="N"]
  for (const stale of document.querySelectorAll("[data-rz-ref], [data-rz-blocked]")) {
    stale.removeAttribute("data-rz-ref");
    stale.removeAttribute("data-rz-blocked");
  }
  // start 是跨 frame 的全局起始编号（snapshot 依次在每个 frame 执行）：
  // 保证整套 ref 唯一，click 时逐个 frame 都能查到这个号。
  const results = [];
  let ref = start;
  const norm = (s) => (s || "").replace(/\s+/g, "").trim();
  // 元素名只折叠空白、不删空格：innerText 里的换行会让「部门：\n技术保障部」难读。
  const flat = (s) => (s || "").replace(/\s+/g, " ").trim();
  // 树/脑图/菜单节点：jsMind 的 jmnode 既无 cursor:pointer 也无 role/onclick，
  // 只靠下面那条指针扫描会整棵树都看不见（2026-11-11 事故：只能盲点展开器 +）。
  const baseSel =
    "a, button, input, textarea, select, [role=button], [role=link], [role=tab], [role=checkbox], [onclick], " +
    "jmnode, [role=treeitem], [role=menuitem], .el-tree-node__content, " +
    ".ant-tree-node-content-wrapper, .ant-tree-title, .layui-tree-txt";
  const bases = new Set(document.querySelectorAll(baseSel));
  const candidates = new Set(bases);

  // 内网系统的菜单常是带 cursor:pointer 的 div/li/span：既没有 role 也没有 onclick
  // （综合管理平台的「运维管理」「事件管理」就是这样）。不补这一条，模型拿不到它们的 ref。
  // 只收「有可见文字」的指针元素：纯图标元素没有名字，仍由 button/a 兜底。
  const all = document.body ? document.body.querySelectorAll("*") : [];
  let scanned = 0;
  for (const el of all) {
    if (++scanned > 20000) break;
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "svg" || tag === "path") continue;
    let style;
    try { style = window.getComputedStyle(el); } catch (e) { continue; }
    if (style.cursor !== "pointer") continue;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (style.visibility === "hidden" || style.display === "none") continue;
    const label = (el.getAttribute("aria-label") || el.getAttribute("title") ||
      el.innerText || "").trim();
    if (!label) continue;
    candidates.add(el);
  }

  // 按文档顺序编号：父元素先拿 ref，重复的嵌套指针元素（menuLi 里的 menuLi_text）
  // 靠 closest 跳过。表单/链接等基础元素不受影响，仍逐个编号。
  const ordered = Array.from(candidates).sort((a, b) => {
    if (a === b) return 0;
    return (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_PRECEDING) ? 1 : -1;
  });
  const treeSel =
    "jmnode, [role=treeitem], [role=menuitem], .el-tree-node__content, " +
    ".ant-tree-node-content-wrapper, .ant-tree-title, .layui-tree-txt";
  const numbered = [];
  for (const el of ordered) {
    if (results.length >= 200) break;
    if (!bases.has(el) && el.closest("[data-rz-ref]")) continue;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const visible = rect.width > 0 && rect.height > 0 &&
      style.visibility !== "hidden" && style.display !== "none";
    if (!visible) continue;
    // 树/菜单里父子节点常是同一段文字（.ant-tree-node-content-wrapper 套 .ant-tree-title），
    // 同一行给两个 ref 只会让模型重复点。只对树类元素去重，不碰 a/button 这类基础元素。
    const rawText = norm(el.innerText || "");
    if (rawText && el.matches(treeSel) &&
        numbered.some((p) => p.el !== el && p.el.contains(el) && p.text === rawText)) {
      continue;
    }
    ref += 1;
    el.setAttribute("data-rz-ref", String(ref));
    numbered.push({el, text: rawText});
    // 表单控件的可识别名：优先 aria/name → 已填值 → 占位符；再拼上最近的表单标签
    // （内网系统的 el-select 全是「请选择」，没有标签就分不清是公司还是专业）。
    const isField = ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName);
    let base = (el.getAttribute("aria-label") || el.getAttribute("name") || "").trim();
    if (isField) {
      const value = el.type === "password" ? "" : (el.value || "").trim();
      base = base || value || (el.getAttribute("placeholder") || "").trim();
    }
    if (!base) base = flat(el.innerText || "");
    let name = base;
    // jsMind 展开器只会渲染成裸 + / -，模型看不出它在展开哪个分支 → 改成可读名称。
    if (el.tagName.toLowerCase() === "jmexpander") {
      const verb = (el.innerText || "").trim() === "+" ? "展开" : "收起";
      let owner = "";
      const nid = el.getAttribute("nodeid");
      if (nid) {
        const ownerNode = document.querySelector(`jmnode[nodeid="${CSS.escape(nid)}"]`);
        if (ownerNode) owner = (ownerNode.innerText || "").trim();
      }
      if (!owner) {
        const prev = el.previousElementSibling;
        if (prev && prev.tagName.toLowerCase() === "jmnode") {
          owner = (prev.innerText || "").trim();
        }
      }
      name = owner ? `${verb}：${owner}` : verb;
    }
    if (isField) {
      let item = el.closest(".el-form-item");
      if (!item) {
        let node = el.parentElement;
        for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
          if (node.querySelector("label, .el-form-item__label")) { item = node; break; }
        }
      }
      const lab = item && item.querySelector("label, .el-form-item__label");
      const label = lab ? (lab.innerText || "").trim().replace(/[:：]\s*$/, "") : "";
      if (label && base && base !== label && !base.startsWith(label)) name = label + "：" + base;
      else if (label && !base) name = label;
    }
    // 综合管理平台的「公司 / 部门」是两个整段下拉（.company-and-dept-label），
    // 没有 role、名字里又带换行，模型认不出它是选择器 → 名字后补一句确定性提示。
    if (el.classList && el.classList.contains("company-and-dept-label")) {
      name = flat(name) + "（下拉可切换）";
    }
    if (name.length > 120) name = name.slice(0, 120) + "…";
    // 危险元素标记：命中黑名单（中文包含；拉丁不分大小写）或文件上传控件。
    // 标记同时写进 DOM 属性，click 时优先读它、失效时再现场重算。
    const lowerName = name.toLowerCase();
    let blocked = el.tagName === "INPUT" && el.type === "file" ? "文件上传" : "";
    if (!blocked) {
      blocked = deny.find(
        (k) => name.includes(k) || lowerName.includes(k.toLowerCase())
      ) || "";
    }
    if (blocked) el.setAttribute("data-rz-blocked", blocked);
    const item = {
      ref,
      tag: el.tagName.toLowerCase(),
      role: el.getAttribute("role") || "",
      type: el.getAttribute("type") || "",
      name,
    };
    if (blocked) item.blocked = true;
    results.push(item);
  }
  return {elements: results, end_ref: ref};
}
"""

# 按文字定位可点元素：树/脑图节点给不出稳定 ref（或 200 个上限外），
# 用可见文字找更可靠。跨 frame 由 Python 侧传 start 做全局编号。
_FIND_BY_TEXT_JS = r"""
(payload) => {
  const norm = (s) => (s || "").replace(/\s+/g, "").trim();
  // 快照只给「公司/部门」下拉追加了「（下拉可切换）」提示，元素真实文本里没有。
  // 模型会照抄快照名字来 click，匹配前必须先剥掉这个标记（全角括号）。
  const hint = "（下拉可切换）";
  const want = norm(payload.text).split(hint).join("");
  const start = payload.start || 0;
  for (const stale of document.querySelectorAll("[data-rz-hit]")) {
    stale.removeAttribute("data-rz-hit");
  }
  if (!want || !document.body) return {count: 0, exact: false, items: [], end: start};

  const visible = (el) => {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return false;
    let style;
    try { style = window.getComputedStyle(el); } catch (e) { return false; }
    return style.visibility !== "hidden" && style.display !== "none";
  };
  const labelOf = (el) => norm(
    el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText || ""
  );

  // 先精确匹配；一个都没有才退到「包含」，并限制长度防止命中 body 这类巨型容器。
  const exact = [];
  const loose = [];
  const all = document.body.querySelectorAll("*");
  let scanned = 0;
  for (const el of all) {
    if (++scanned > 20000) break;
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "svg" || tag === "path") continue;
    if (!visible(el)) continue;
    const label = labelOf(el);
    if (!label) continue;
    if (label === want) exact.push(el);
    else if (label.includes(want) && label.length <= want.length + 40) loose.push(el);
  }
  let hits = exact.length ? exact : loose;
  // 只留最深的命中：父容器与子节点文字相同（<div><span>2026</span></div>）时保留子节点。
  hits = hits.filter((el) => !hits.some((other) => other !== el && el.contains(other)));
  hits.sort((a, b) =>
    (a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_PRECEDING) ? 1 : -1
  );

  // 同名节点常有多个（两个「2026」），给最近的 3 个树/脑图节点名帮模型区分。
  const nodeSel =
    "jmnode, [role=treeitem], .el-tree-node__content, " +
    ".ant-tree-node-content-wrapper, .ant-tree-title, .layui-tree-txt";
  const nodes = Array.from(document.querySelectorAll(nodeSel)).filter(visible);
  const nearOf = (el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;
    return nodes
      .filter((n) => n !== el && !n.contains(el) && !el.contains(n))
      .map((n) => {
        const nr = n.getBoundingClientRect();
        const dx = nr.left + nr.width / 2 - cx;
        const dy = nr.top + nr.height / 2 - cy;
        return {n, d: dx * dx + dy * dy};
      })
      .sort((a, b) => a.d - b.d)
      .slice(0, 3)
      .map((x) => (x.n.innerText || "").trim().slice(0, 40))
      .filter(Boolean);
  };

  // 同名元素常跨区域（面包屑与表格里各有一个「2026」）。给一句所在区域提示，
  // 模型才能选对 nth：点面包屑是往上层跳，点表格行才是进入下一层。
  const zoneOf = (el) => {
    const hit = el.closest(
      "tr, nav, [role=navigation], .el-breadcrumb, [class*=breadcrumb], " +
      ".el-menu, [role=menu], .el-tree, .ant-tree, ul[role=tree]"
    );
    if (!hit) return "";
    if (hit.matches("tr")) return "表格行";
    if (hit.matches("nav, [role=navigation]")) return "导航";
    if (hit.matches(".el-breadcrumb, [class*=breadcrumb]")) return "面包屑";
    if (hit.matches(".el-menu, [role=menu]")) return "菜单";
    return "树";
  };

  const items = hits.map((el, i) => {
    const index = start + i;
    el.setAttribute("data-rz-hit", String(index));
    return {
      index,
      tag: el.tagName.toLowerCase(),
      name: (el.innerText || "").trim().slice(0, 80),
      where: zoneOf(el),
      near: nearOf(el),
    };
  });
  return {count: items.length, exact: exact.length > 0, items, end: start + items.length};
}
"""

# 点中的是不是浮层里的选项：只有这类点击之后需要把鼠标移开收起浮层。
_IN_OVERLAY_JS = r"""
(el) => !!el.closest(
  ".el-dropdown-menu, .el-select-dropdown, .el-popover, .el-picker-panel, " +
  "[role=menu], .ant-dropdown, .ant-select-dropdown, .ant-popover"
)
"""

# click 前的兜底重算：SPA 重渲染会丢掉 data-rz-blocked，按当前文本再判一次。
_CLICK_BLOCKED_JS = r"""
(el, deny) => {
  const text = (el.innerText || el.value || el.getAttribute("aria-label") || "").trim();
  const lower = text.toLowerCase();
  if (el.tagName === "INPUT" && el.type === "file") return {name: text, hit: "文件上传"};
  const hit = deny.find((k) => text.includes(k) || lower.includes(k.toLowerCase()));
  return hit ? {name: text, hit} : null;
}
"""


def _context_args(system: str) -> dict:
  """会话/捕获共用的 context 参数（内网自签证书 + 固定时区语言 + 登录态）。"""
  args = {
    'viewport': DEFAULT_VIEWPORT,
    'ignore_https_errors': True,
    'locale': 'zh-CN',
    'timezone_id': 'Asia/Shanghai',
  }
  state = _state_path(system)
  if state.is_file():
    args['storage_state'] = str(state)
  return args


async def _new_session(session_id: str, system: str) -> LiveSession:
  context = await _browser.new_context(**_context_args(system))
  page = await context.new_page()
  page.set_default_timeout(DEFAULT_STEP_TIMEOUT_MS)
  now = time.monotonic()
  session = LiveSession(context=context, page=page, system=system,
                        created_at=now, last_used=now)
  _sessions[session_id] = session
  return session


async def _close_session(session_id: str) -> bool:
  async with _sessions_lock:
    session = _sessions.pop(session_id, None)
  if session is None:
    return False
  with suppress(Exception):
    await session.context.close()
  return True


async def _close_all_sessions() -> None:
  async with _sessions_lock:
    sessions = list(_sessions.values())
    _sessions.clear()
  for session in sessions:
    with suppress(Exception):
      await session.context.close()


async def _get_session(session_id: str) -> LiveSession | None:
  async with _sessions_lock:
    return _sessions.get(session_id)


async def _ensure_session(session_id: str, system: str) -> LiveSession:
  """取会话；不存在则建，system 变了就换 context（storageState 不同），超上限淘汰最久未用。"""
  async with _sessions_lock:
    existing = _sessions.get(session_id)
    if existing is not None and existing.system == system:
      return existing
    if existing is not None:
      _sessions.pop(session_id, None)
      with suppress(Exception):
        await existing.context.close()
    while len(_sessions) >= MAX_SESSIONS:
      victim_id = min(_sessions, key=lambda sid: _sessions[sid].last_used)
      victim = _sessions.pop(victim_id)
      with suppress(Exception):
        await victim.context.close()
    return await _new_session(session_id, system)


async def _reap_loop() -> None:
  while True:
    await asyncio.sleep(_REAP_INTERVAL_S)
    cutoff = time.monotonic() - SESSION_IDLE_SECONDS
    async with _sessions_lock:
      stale = [sid for sid, s in _sessions.items() if s.last_used < cutoff]
    for sid in stale:
      await _close_session(sid)


async def _settle(page: Page, timeout_ms: int = 4000, min_wait_ms: int = 0) -> None:
  """SPA 在 DOMContentLoaded 之后才渲染/拉数，快照前等网络静默；有上限，绝不阻塞。

  逐个 frame 等：菜单点击后业务页在新建的 iframe 里加载，只等主文档会拿到空页面。
  `min_wait_ms` 先等一小会儿再判 networkidle —— 点击触发的请求常被防抖/动画推迟，
  立刻判定会在请求发出前就「已静默」，截到加载中的空表格。
  """
  if min_wait_ms > 0:
    with suppress(Exception):
      await page.wait_for_timeout(min_wait_ms)
  half = max(600, timeout_ms // 2)
  with suppress(Exception):
    await page.wait_for_load_state('networkidle', timeout=half)
  # 主文档落定后业务页可能才新建 iframe：重新取帧表再等一轮
  frames = page.frames
  per_frame = max(400, timeout_ms // max(1, len(frames)))
  for frame in frames:
    if frame == page.main_frame:
      continue
    with suppress(Exception):
      await frame.wait_for_load_state('networkidle', timeout=per_frame)
  # 数据到位后加载遮罩常还有一段淡出动画，留一点收尾时间再快照/截图
  with suppress(Exception):
    await page.wait_for_timeout(400)


async def _snapshot(page: Page) -> dict:
  """合并主文档与所有 iframe 的可交互元素。

  内网平台（如综合管理平台）把业务页放在 iframe 里，只扫描主文档会得到空页面。
  ref 跨 frame 连续编号（JS 收 start、返回 end_ref），click 时逐个 frame 查找即可。
  """
  elements: list[dict] = []
  ref = 0
  for frame in page.frames:
    if len(elements) >= 200:
      break
    try:
      data = await frame.evaluate(_SNAPSHOT_JS, {'start': ref, 'deny': list(CLICK_DENYLIST)})
    except Exception:
      continue  # frame 正在导航/已 detach：跳过，其余 frame 照常返回
    if not isinstance(data, dict):
      continue
    elements.extend(data.get('elements') or [])
    ref = int(data.get('end_ref') or ref)
  return {
    'url': page.url,
    'title': await page.title(),
    'elements': elements[:200],
  }


def _check_nav_target(url: str) -> None:
  parsed = urlparse(url)
  if parsed.scheme not in ('http', 'https'):
    raise RecipeError('navigate 只允许 http/https 地址')
  host = (parsed.hostname or '').lower()
  if ALLOWED_HOSTS and host not in ALLOWED_HOSTS:
    raise RecipeError(f'导航目标不在白名单: {host}')


async def _navigate_session(session_id: str, body: dict) -> dict:
  url = str(body.get('url') or '')
  _check_nav_target(url)
  system = str(body.get('system') or '')
  wait_until = str(body.get('wait_until') or 'domcontentloaded')
  session = await _ensure_session(session_id, system)
  async with session.lock:
    session.last_used = time.monotonic()
    stage = 'navigate'
    try:
      await _goto(session.page, url, DEFAULT_STEP_TIMEOUT_MS, wait_until)
      login = body.get('login')
      if login:
        stage = 'login'
        did_login = await _ensure_login(session.page, session.context, login, system)
        stage = 'navigate'
        if did_login:
          # 登录页跳转过，回到目标页
          await _goto(session.page, url, DEFAULT_STEP_TIMEOUT_MS, wait_until)
      await _settle(session.page)
      if login:
        check = login.get('check') or {}
        spec = check.get('selector') or check.get('locator')
        if spec and not await _visible_after(session.page, spec, 0):
          return {
            'ok': False,
            'stage': 'login_conflict',
            'error': (
              f'登录后又被踢回登录页（当前：{await _page_title(session.page) or "无标题"}，'
              f'{session.page.url}）。该账号可能在别处登录（同一账号单会话），'
              '建议为浏览器配置专用只读账号。'
            ),
          }
      data = await _snapshot(session.page)
      data.update({'ok': True, 'system': system, 'session_id': session_id})
      return data
    except LoginPageMissing as e:
      return {'ok': False, 'stage': 'login_page', 'error': str(e)}
    except RecipeError as e:
      return {'ok': False, 'stage': stage, 'error': str(e)}
    except Exception as e:
      return {'ok': False, 'stage': stage, 'error': f'{type(e).__name__}: {e}'}


def _as_bool(value, default: bool = False) -> bool:
  if value is None or value == '':
    return default
  if isinstance(value, str):
    return value.strip().lower() in ('1', 'true', 'yes', 'on')
  return bool(value)


def _record_seconds(body: dict) -> int:
  """录制时长：缺省 30s，钳到 [5, RECORD_MAX_SECONDS]。"""
  raw = body.get('seconds')
  try:
    seconds = int(float(raw)) if raw not in (None, '') else RECORD_DEFAULT_SECONDS
  except (TypeError, ValueError):
    seconds = RECORD_DEFAULT_SECONDS
  return max(5, min(seconds, RECORD_MAX_SECONDS))


def _keyframe_times(body: dict, seconds: int) -> list[float]:
  """按间隔生成关键帧时间点；总数超过上限时改成等距（仍含 0 与末尾）。"""
  raw = body.get('keyframe_interval')
  try:
    interval = int(float(raw)) if raw not in (None, '') else RECORD_DEFAULT_INTERVAL
  except (TypeError, ValueError):
    interval = RECORD_DEFAULT_INTERVAL
  interval = max(1, min(interval, max(1, seconds)))

  times: list[float] = []
  t = 0.0
  while t < seconds - 0.05:
    times.append(round(t, 3))
    t += interval
  times.append(float(seconds))
  if len(times) > RECORD_MAX_KEYFRAMES:
    step = seconds / max(1, RECORD_MAX_KEYFRAMES - 1)
    times = [round(i * step, 3) for i in range(RECORD_MAX_KEYFRAMES)]
  return times


async def _assemble_webm(tmp: Path, captured: list[float], seconds: int,
                         fallback_b64: str | None) -> bytes:
  """把采集帧按固定 fps 铺成时间轴，ffmpeg 合成 webm（VP8 优先）。"""
  cap_dir = tmp / 'cap'
  timeline = tmp / 'timeline'
  timeline.mkdir(exist_ok=True)

  if not captured:
    # 纯静态页面可能收不到 screencast 帧：用最后一张关键帧合成静态视频
    if not fallback_b64:
      return b''
    cap_dir.mkdir(exist_ok=True)
    (cap_dir / '000000.jpg').write_bytes(base64.b64decode(fallback_b64))
    captured = [0.0]

  total = max(1, int(round(seconds * RECORD_FPS)))
  for i in range(total):
    t = i / RECORD_FPS
    idx = max(0, bisect.bisect_right(captured, t) - 1)
    src = cap_dir / f'{idx:06d}.jpg'
    dst = timeline / f'{i:06d}.jpg'
    with suppress(FileExistsError):
      os.link(src, dst)

  out = tmp / 'rec.webm'
  for codec in ('libvpx', 'libvpx-vp9'):
    out.unlink(missing_ok=True)
    cmd = [
      'ffmpeg', '-y', '-loglevel', 'error',
      '-framerate', str(RECORD_FPS), '-i', str(timeline / '%06d.jpg'),
      '-c:v', codec, '-b:v', '2M', '-deadline', 'realtime', '-cpu-used', '5',
      '-pix_fmt', 'yuv420p', str(out),
    ]
    try:
      await asyncio.to_thread(
        subprocess.run, cmd, timeout=_RECORD_FFMPEG_TIMEOUT_S,
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
      )
    except Exception:
      continue
    if out.is_file() and out.stat().st_size > 0:
      return out.read_bytes()
  return b''


async def _record_session(session: LiveSession, body: dict) -> dict:
  """在当前页面录制固定时长：关键帧（主交付）+ 可选 webm（CDP screencast + ffmpeg）。

  不重新导航、不关闭会话；录制期间持有 session.lock，同会话其他动作会排队。
  """
  page = session.page
  seconds = _record_seconds(body)
  times = _keyframe_times(body, seconds)
  with_video = _as_bool(body.get('video'), True)
  tmp = Path(tempfile.mkdtemp(prefix='rz-rec-', dir='/tmp'))
  cap_dir = tmp / 'cap'
  cap_dir.mkdir(exist_ok=True)
  keyframes: list[dict] = []
  captured: list[float] = []
  video_error = ''
  cdp = None

  try:
    start = time.monotonic()

    if with_video:
      try:
        cdp = await session.context.new_cdp_session(page)
        await cdp.send('Page.enable')

        def _on_frame(params: dict) -> None:
          # 不 ack Chromium 会停止发帧
          async def _ack() -> None:
            with suppress(Exception):
              await cdp.send('Page.screencastFrameAck',
                             {'sessionId': params.get('sessionId')})
          asyncio.ensure_future(_ack())
          now = time.monotonic() - start
          idx = len(captured)
          # 按 RECORD_FPS 节流，避免高帧率页面把内存/磁盘打满
          if idx and now - captured[-1] < 1.0 / RECORD_FPS:
            return
          data = base64.b64decode(params.get('data') or '')
          if not data:
            return
          with suppress(Exception):
            (cap_dir / f'{idx:06d}.jpg').write_bytes(data)
            captured.append(now)

        cdp.on('Page.screencastFrame', _on_frame)
        await cdp.send('Page.startScreencast', {
          'format': 'jpeg', 'quality': 70,
          'maxWidth': 1280, 'maxHeight': 1280, 'everyNthFrame': 1,
        })
      except Exception as e:
        video_error = f'启动屏幕采集失败：{type(e).__name__}: {e}'
        if cdp is not None:
          with suppress(Exception):
            await cdp.detach()
        cdp = None

    # 关键帧按绝对时间点采样（漂移不累积）
    for t_off in times:
      wait = (start + t_off) - time.monotonic()
      if wait > 0:
        await asyncio.sleep(wait)
      image = await page.screenshot(type='jpeg', quality=75, full_page=False)
      keyframes.append({
        't': float(t_off),
        'image_b64': base64.b64encode(image).decode('ascii'),
      })

    # 采样结束就停录，合成阶段不再占用 CDP
    if cdp is not None:
      with suppress(Exception):
        await cdp.send('Page.stopScreencast')
      with suppress(Exception):
        await cdp.detach()
      cdp = None

    video_b64 = ''
    video_bytes = 0
    if with_video and not video_error:
      if shutil.which('ffmpeg') is None:
        video_error = 'ffmpeg 不可用'
      else:
        fallback = keyframes[-1]['image_b64'] if keyframes else None
        try:
          blob = await _assemble_webm(tmp, captured, seconds, fallback)
        except Exception as e:
          blob = b''
          video_error = f'视频合成失败：{type(e).__name__}: {e}'
        if blob:
          if RECORD_MAX_VIDEO_BYTES and len(blob) > RECORD_MAX_VIDEO_BYTES:
            video_error = (
              f'视频超过 {RECORD_MAX_VIDEO_BYTES // (1024 * 1024)}MB，仅交付关键帧'
            )
          else:
            video_b64 = base64.b64encode(blob).decode('ascii')
            video_bytes = len(blob)
        elif not video_error:
          video_error = '视频合成失败（未生成有效文件）'

    return {
      'ok': True,
      'page': {'url': page.url, 'title': await page.title()},
      'duration_s': seconds,
      'frames_captured': len(captured),
      'keyframes': keyframes,
      'video_b64': video_b64,
      'video_bytes': video_bytes,
      'video_error': video_error,
    }
  finally:
    if cdp is not None:
      with suppress(Exception):
        await cdp.send('Page.stopScreencast')
      with suppress(Exception):
        await cdp.detach()
    shutil.rmtree(tmp, ignore_errors=True)


async def _resolve_click_target(page: Page, body: dict):
  """按 ref 或 text 定位待点元素。

  返回 `(locator, None)` 或 `(None, 载荷)`；载荷可能是错误，也可能是
  `ok/clicked=False/ambiguous` 的候选清单（同名多个时**不猜**，交模型用 nth 选）。
  """
  ref_raw = str(body.get('ref') or '').strip()
  text = str(body.get('text') or '').strip()
  if bool(ref_raw) == bool(text):
    return None, {'ok': False, 'stage': 'click',
                  'error': 'click 需要 ref 或 text 之一（二选一）'}

  if ref_raw:
    if not ref_raw.isdigit():
      return None, {'ok': False, 'stage': 'click', 'error': 'ref 必须是正整数'}
    ref = int(ref_raw)
    # ref 跨 frame 连续编号，逐个 frame 找（page.locator 不穿透 iframe）
    for frame in page.frames:
      loc = frame.locator(f'[data-rz-ref="{ref}"]')
      if await loc.count() > 0:
        return loc.first, None
    return None, {'ok': False, 'stage': 'click',
                  'error': f'元素 [{ref}] 已失效，请先调用 browser__snapshot 重新编号'}

  hits: list[dict] = []
  frames: dict[int, object] = {}
  start = 0
  for frame in page.frames:
    try:
      data = await frame.evaluate(_FIND_BY_TEXT_JS, {'text': text, 'start': start})
    except Exception:
      continue  # frame 正在导航/已 detach：跳过
    if not isinstance(data, dict):
      continue
    for item in data.get('items') or []:
      idx = int(item.get('index') or 0)
      hits.append(item)
      frames[idx] = frame
    start = int(data.get('end') or start)

  if not hits:
    return None, {'ok': False, 'stage': 'click',
                  'error': f'页面上没有文字包含「{text}」的可点元素，'
                           '请先 browser__get_text 确认节点真实名称'}

  nth_raw = str(body.get('nth') or '').strip()
  nth = int(nth_raw) if nth_raw.isdigit() else 0
  if len(hits) > 1 and not nth:
    return None, {
      'ok': True, 'clicked': False, 'ambiguous': True, 'total': len(hits),
      'candidates': [
        {'index': h.get('index'), 'tag': h.get('tag'), 'name': h.get('name'),
         'where': h.get('where') or '', 'near': h.get('near') or []}
        for h in hits[:12]
      ],
    }
  if nth:
    if not 1 <= nth <= len(hits):
      return None, {'ok': False, 'stage': 'click',
                    'error': f'nth 超出范围：共 {len(hits)} 个同名元素（1~{len(hits)}）'}
    chosen = hits[nth - 1]
  else:
    chosen = hits[0]

  idx = int(chosen.get('index') or 0)
  frame = frames.get(idx)
  if frame is None:
    return None, {'ok': False, 'stage': 'click', 'error': '元素已失效，请重新定位'}
  loc = frame.locator(f'[data-rz-hit="{idx}"]')
  if await loc.count() == 0:
    return None, {'ok': False, 'stage': 'click', 'error': '元素已失效，请重新定位'}
  return loc.first, None


async def _do_session_action(action: str, session: LiveSession, body: dict) -> dict:
  page = session.page
  if action == 'record':
    return await _record_session(session, body)
  if action == 'snapshot':
    await _settle(page)
    return {'ok': True, **(await _snapshot(page))}

  if action == 'click':
    button = str(body.get('button') or 'left').strip().lower()
    count = str(body.get('count') or '1').strip()
    if button not in ('left', 'right'):
      return {'ok': False, 'stage': 'click', 'error': 'button 只能是 left 或 right'}
    if count not in ('1', '2'):
      return {'ok': False, 'stage': 'click', 'error': 'count 只能是 1 或 2'}
    if button == 'right' and count == '2':
      return {'ok': False, 'stage': 'click', 'error': '不支持右键双击'}

    target, problem = await _resolve_click_target(page, body)
    if problem is not None:
      return problem

    # 只读浏览护栏：先读快照打的标记；SPA 重渲染丢了属性时按当前文本重算一次。
    blocked = ''
    with suppress(Exception):
      blocked = await target.get_attribute('data-rz-blocked') or ''
    info = None
    with suppress(Exception):
      info = await target.evaluate(_CLICK_BLOCKED_JS, list(CLICK_DENYLIST))
    if not blocked and info:
      blocked = str(info.get('hit') or '')
    if blocked:
      name = str((info or {}).get('name') or '')
      label = f'「{name}」' if name else '该元素'
      return {'ok': False, 'stage': 'blocked',
              'error': f'{label}属于只读浏览禁止点击的操作（命中「{blocked}」），'
                       '如需执行请由人工完成；浏览请继续用菜单或详情链接'}
    with suppress(Exception):
      await target.scroll_into_view_if_needed(timeout=_CLICK_TIMEOUT_MS)
    # 点击目标是浮层里的选项（公司/部门下拉）时，Element UI 不会自己收起浮层。
    in_overlay = False
    with suppress(Exception):
      in_overlay = bool(await target.evaluate(_IN_OVERLAY_JS))
    # 目标不在浮层里：先移开指针，收起上一次遗留的 hover 浮层（否则它盖住目标、
    # 点击一直等到 8s 超时）。目标已在浮层里则不能提前移开，否则选项会消失。
    if not in_overlay:
      with suppress(Exception):
        await page.mouse.move(1, 1)
    pages_before = list(session.context.pages)
    try:
      if count == '2':
        await target.dblclick(timeout=_CLICK_TIMEOUT_MS)
      elif button == 'right':
        await target.click(button='right', timeout=_CLICK_TIMEOUT_MS)
      else:
        await target.click(timeout=_CLICK_TIMEOUT_MS)
    except Exception as e:
      return {'ok': False, 'stage': 'click',
              'error': f'点击失败（页面可能已变化），请重新 snapshot：{e}'}
    # Element UI 的 hover 下拉（公司/部门选择器）点完选项后仍停在鼠标下方，
    # 浮层没收到 mouseleave 就一直盖着页面，下一次点击会被拦截（实测 8s 超时）。
    # 只在「刚点了浮层里的选项」时把指针移回左上角让它收起——点下拉本身（标签）
    # 时不能移开，否则浮层立刻关闭、模型看不到里面的选项。
    if in_overlay:
      with suppress(Exception):
        await page.mouse.move(1, 1)
    # 点击常以 target=_blank 打开新标签页；不接管会话就会一直停在旧 page，
    # 模型再也看不到目标页（内网目录树「双击进入」的常见形态）。
    def _new_pages() -> list:
      return [p for p in session.context.pages if p not in pages_before and not p.is_closed()]

    new_pages = _new_pages()
    if not new_pages:
      # window.open 可能比 click 返回稍晚一点；给一个很短的事件等待，不要漏掉新标签页。
      with suppress(Exception):
        await session.context.wait_for_event('page', timeout=1500)
      new_pages = _new_pages()
    if new_pages:
      session.page = new_pages[-1]
      page = session.page
      with suppress(Exception):
        await page.wait_for_load_state('domcontentloaded', timeout=_POST_CLICK_LOAD_TIMEOUT_MS)
      await _settle(page, _POST_CLICK_LOAD_TIMEOUT_MS, min_wait_ms=600)
      return {'ok': True, 'clicked': True, 'popup': True, **(await _snapshot(page))}
    # SPA 跳转不触发 load 事件，等网络静默一会儿再快照，绝不阻塞
    with suppress(Exception):
      await page.wait_for_load_state('domcontentloaded', timeout=_POST_CLICK_LOAD_TIMEOUT_MS)
    await _settle(page, _POST_CLICK_LOAD_TIMEOUT_MS, min_wait_ms=600)
    return {'ok': True, 'clicked': True, **(await _snapshot(page))}

  if action == 'get_text':
    limit = MAX_TEXT_CHARS
    raw = body.get('max_chars')
    if raw not in (None, ''):
      try:
        limit = int(raw)
      except (TypeError, ValueError):
        return {'ok': False, 'stage': 'text', 'error': 'max_chars 必须是整数'}
    limit = max(1, min(limit, MAX_TEXT_CHARS))
    # 业务页在 iframe 里时，主文档 innerText 只有外框；逐 frame 取再拼接。
    parts: list[str] = []
    for frame in page.frames:
      try:
        parts.append(await frame.inner_text('body'))
      except Exception:
        continue
    text = '\n'.join(p for p in parts if p)
    return {'ok': True, 'text': text[:limit], 'truncated': len(text) > limit}

  if action == 'screenshot':
    # 载入尚未完成时截图会得到空表格/转圈，先等页面静默（有上限）
    await _settle(page)
    full_page = _as_bool(body.get('full_page'), False)
    image = await page.screenshot(full_page=full_page, type='png')
    truncated = False
    if MAX_IMAGE_BYTES and len(image) > MAX_IMAGE_BYTES:
      image = await page.screenshot(full_page=False, type='png')
      truncated = True
    return {
      'ok': True,
      'page': {'url': page.url, 'title': await page.title()},
      'image_b64': base64.b64encode(image).decode('ascii'),
      'image_bytes': len(image),
      'image_truncated': truncated,
      'captured_at': _now_iso(),
    }

  return {'ok': False, 'stage': 'session', 'error': f'未知会话动作: {action}'}


async def _session_action(body: dict) -> dict:
  if _browser is None or not _browser.is_connected():
    return {'ok': False, 'stage': 'screenshot', 'error': 'Chromium 不可用'}
  action = str(body.get('action') or '')
  session_id = str(body.get('session_id') or '')
  if not _SESSION_ID_RE.match(session_id):
    raise RecipeError('session_id 不合法')
  if action == 'navigate':
    return await _navigate_session(session_id, body)
  if action == 'close':
    return {'ok': True, 'closed': await _close_session(session_id)}

  session = await _get_session(session_id)
  if session is None:
    return {'ok': False, 'stage': 'session',
            'error': '会话不存在或已过期，请先调用 browser__navigate 打开页面'}
  async with session.lock:
    session.last_used = time.monotonic()
    try:
      result = await _do_session_action(action, session, body)
    except Exception as e:
      stage = {'click': 'click', 'get_text': 'text', 'record': 'record'}.get(action, 'session')
      return {'ok': False, 'stage': stage, 'error': f'{type(e).__name__}: {e}'}
  return result


# ============================ 捕获 ============================

async def _capture(body: dict) -> dict:
  if _browser is None or not _browser.is_connected():
    return {'ok': False, 'stage': 'screenshot', 'error': 'Chromium 不可用'}
  recipe = body.get('recipe')
  if not isinstance(recipe, dict):
    raise RecipeError('缺少 recipe')
  params = body.get('params') or {}
  if not isinstance(params, dict):
    raise RecipeError('params 必须是对象')
  recipe = _render(recipe, params)

  system = str(body.get('system') or '')
  full_page = bool(body.get('full_page', True))
  state = _state_path(system)
  context_args = {
    'viewport': DEFAULT_VIEWPORT,
    'ignore_https_errors': bool(body.get('ignore_https_errors', True)),  # 内网自签证书
    'locale': 'zh-CN',
    'timezone_id': 'Asia/Shanghai',
  }
  if state.is_file():
    context_args['storage_state'] = str(state)

  context = await _browser.new_context(**context_args)
  page = await context.new_page()
  page.set_default_timeout(DEFAULT_STEP_TIMEOUT_MS)
  stage = 'navigate'
  try:
    url = recipe.get('url')
    if not url:
      raise RecipeError('recipe 缺少 url')
    await _goto(page, url, DEFAULT_STEP_TIMEOUT_MS, recipe.get('wait_until', 'domcontentloaded'))

    login = recipe.get('login')
    if login:
      stage = 'login'
      if await _ensure_login(page, context, login, system):
        # 登录后回到目标页（登录页跳转过）
        stage = 'navigate'
        await _goto(page, url, DEFAULT_STEP_TIMEOUT_MS, recipe.get('wait_until', 'domcontentloaded'))

    stage = 'apply_filters'
    for step in recipe.get('steps') or []:
      await _run_step(page, step)

    stage = 'ready'
    await _wait_ready(page, recipe.get('ready'))

    stage = 'extract'
    extracted = await _extract(page, recipe.get('extract'))

    stage = 'screenshot'
    image = await page.screenshot(full_page=full_page, type='png')
    truncated = False
    if MAX_IMAGE_BYTES and len(image) > MAX_IMAGE_BYTES:
      # 超长页面：退回可视区截图，避免几万像素的巨图撑爆内存与响应体
      image = await page.screenshot(full_page=False, type='png')
      truncated = True

    return {
      'ok': True,
      'page': {'url': page.url, 'title': await page.title()},
      'image_b64': base64.b64encode(image).decode('ascii'),
      'image_bytes': len(image),
      'image_truncated': truncated,
      'text': extracted['text'],
      'tables': extracted['tables'],
      'html': extracted['html'],
      'captured_at': _now_iso(),
    }
  except LoginPageMissing as e:
    return {'ok': False, 'stage': 'login_page', 'error': str(e)}
  except RecipeError:
    raise
  except Exception as e:
    return {'ok': False, 'stage': stage, 'error': f'{type(e).__name__}: {e}'}
  finally:
    await context.close()


# ============================ 探查 ============================

_INSPECT_JS = """
() => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const pick = (el) => ({
    tag: el.tagName.toLowerCase(),
    type: el.getAttribute('type') || '',
    text: norm(el.innerText).slice(0, 60),
    id: el.id || '',
    name: el.getAttribute('name') || '',
    role: el.getAttribute('role') || '',
    aria: el.getAttribute('aria-label') || '',
    placeholder: el.getAttribute('placeholder') || '',
    testid: el.getAttribute('data-testid') || '',
    cls: (el.className && typeof el.className === 'string' ? el.className : '').slice(0, 120),
  });
  const sel = 'button,a,input,select,textarea,[role=button],[role=tab],[data-testid]';
  const nodes = [...document.querySelectorAll(sel)].slice(0, 200).map(pick);
  const tables = [...document.querySelectorAll('table')].slice(0, 20).map((t, i) => {
    const head = t.querySelector('thead tr');
    return {
      index: i,
      id: t.id || '',
      cls: (t.className || '').slice(0, 120),
      headers: head ? [...head.querySelectorAll('th,td')].map(c => norm(c.innerText)) : [],
      row_count: t.querySelectorAll('tbody tr').length || t.querySelectorAll('tr').length,
    };
  });
  return {elements: nodes, tables};
}
"""


async def _inspect(body: dict) -> dict:
  if _browser is None or not _browser.is_connected():
    return {'ok': False, 'stage': 'screenshot', 'error': 'Chromium 不可用'}
  recipe = body.get('recipe')
  if not isinstance(recipe, dict):
    raise RecipeError('缺少 recipe')
  recipe = _render(recipe, body.get('params') or {})
  context_args = {
    'viewport': DEFAULT_VIEWPORT,
    'ignore_https_errors': True,
    'locale': 'zh-CN',
    'timezone_id': 'Asia/Shanghai',
  }
  state = _state_path(str(body.get('system') or ''))
  if state.is_file():
    context_args['storage_state'] = str(state)
  context = await _browser.new_context(**context_args)
  page = await context.new_page()
  stage = 'navigate'
  try:
    await _goto(page, recipe.get('url') or '', DEFAULT_STEP_TIMEOUT_MS,
                recipe.get('wait_until', 'domcontentloaded'))
    stage = 'apply_filters'
    for step in recipe.get('steps') or []:
      await _run_step(page, step)
    stage = 'ready'
    await _wait_ready(page, recipe.get('ready'))
    stage = 'extract'
    dumped = await page.evaluate(_INSPECT_JS)
    dumped['page'] = {'url': page.url, 'title': await page.title()}
    dumped['ok'] = True
    return dumped
  except RecipeError:
    raise
  except Exception as e:
    return {'ok': False, 'stage': stage, 'error': f'{type(e).__name__}: {e}'}
  finally:
    await context.close()


# ============================ 路由 ============================

@app.get('/health')
async def health():
  ok = _browser is not None and _browser.is_connected()
  return {
    'ok': ok,
    'chromium': _browser.version if ok else None,
    'state_dir': str(STATE_DIR),
    'max_concurrency': MAX_CONCURRENCY,
    'capture_timeout': CAPTURE_TIMEOUT,
    'sessions': len(_sessions),
    'max_sessions': MAX_SESSIONS,
    'record_max_seconds': RECORD_MAX_SECONDS,
    'max_video_mb': round(RECORD_MAX_VIDEO_BYTES / (1024 * 1024), 1),
  }


@app.post('/v1/capture')
async def capture(body: dict, x_browser_token: str | None = Header(default=None)):
  _require_token(x_browser_token)
  async with _sem:
    try:
      result = await asyncio.wait_for(_capture(body), timeout=CAPTURE_TIMEOUT)
    except asyncio.TimeoutError:
      return JSONResponse(
        status_code=504,
        content={'ok': False, 'stage': 'timeout', 'error': f'捕获超时（{CAPTURE_TIMEOUT:.0f}s）'},
      )
    except RecipeError as e:
      return JSONResponse(status_code=400, content={'ok': False, 'stage': 'recipe', 'error': str(e)})
  return result


@app.post('/v1/session')
async def session_endpoint(body: dict, x_browser_token: str | None = Header(default=None)):
  _require_token(x_browser_token)
  if str(body.get('action') or '') == 'record':
    # 录制可能几十秒：不占全局 _sem（否则长录制会饿死其他截图/会话），
    # 改用独立的单路信号量；已有录制直接快速失败，不排队。
    if _record_sem.locked():
      return {'ok': False, 'stage': 'record', 'error': '已有录制在进行中，请等待结束或缩短时长'}
    timeout = CAPTURE_TIMEOUT + _record_seconds(body) + 90
    async with _record_sem:
      try:
        result = await asyncio.wait_for(_session_action(body), timeout=timeout)
      except asyncio.TimeoutError:
        # 超时不关会话：动作可能已经在页面上生效，关掉反而丢掉现场
        return JSONResponse(
          status_code=504,
          content={'ok': False, 'stage': 'timeout', 'error': f'录制超时（{timeout:.0f}s）'},
        )
      except RecipeError as e:
        return JSONResponse(
          status_code=400, content={'ok': False, 'stage': 'recipe', 'error': str(e)},
        )
    return result

  async with _sem:
    try:
      result = await asyncio.wait_for(_session_action(body), timeout=CAPTURE_TIMEOUT)
    except asyncio.TimeoutError:
      # 超时不关会话：动作可能已经在页面上生效，关掉反而丢掉现场
      return JSONResponse(
        status_code=504,
        content={'ok': False, 'stage': 'timeout', 'error': f'会话操作超时（{CAPTURE_TIMEOUT:.0f}s）'},
      )
    except RecipeError as e:
      return JSONResponse(status_code=400, content={'ok': False, 'stage': 'recipe', 'error': str(e)})
  return result


@app.post('/v1/inspect')
async def inspect(body: dict, x_browser_token: str | None = Header(default=None)):
  _require_token(x_browser_token)
  async with _sem:
    try:
      result = await asyncio.wait_for(_inspect(body), timeout=CAPTURE_TIMEOUT)
    except asyncio.TimeoutError:
      return JSONResponse(
        status_code=504,
        content={'ok': False, 'stage': 'timeout', 'error': f'探查超时（{CAPTURE_TIMEOUT:.0f}s）'},
      )
    except RecipeError as e:
      return JSONResponse(status_code=400, content={'ok': False, 'stage': 'recipe', 'error': str(e)})
  return result

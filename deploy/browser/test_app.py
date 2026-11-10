"""旁路服务自测（纯 assert，不依赖 pytest）。

在旁路容器内运行：`python test_app.py`；
本机没装 playwright/Chromium 时打印 SKIP 并退出 0（不误报失败）。

覆盖：参数插值（含未知占位符拒绝）、筛选后表格抽取、截图字节/尺寸、就绪超时给出明确 stage；
交互会话：navigate → snapshot → click → get_text → screenshot → close，失效 ref 与无会话报错；
录屏：navigate → record → 会话保持 → close，关键帧/视频分段降级与无会话报错；
通用能力：cursor:pointer 的纯 div 菜单可被识别/点击、iframe 内元素进快照且正文可读；
登录判定：旧界面残影不被当成已登录（等静默后复查）、目标地址不是登录页时快速报 login_page。
"""

import asyncio
import base64
import importlib.util
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if importlib.util.find_spec('playwright') is None:
  print('SKIP: playwright 未安装（请在旁路容器内运行本自测）')
  raise SystemExit(0)

import app as sidecar  # noqa: E402
from app import (  # noqa: E402
  RecipeError,
  _capture,
  _check_nav_target,
  _ensure_login,
  _render,
  _session_action,
  app as sidecar_app,
)

FIXTURE = """
<!doctype html>
<html><head><meta charset="utf-8"><title>测试页</title></head>
<body>
  <select id="dept">
    <option value="all">全部</option>
    <option value="A部">A部</option>
    <option value="B部">B部</option>
  </select>
  <input id="kw" placeholder="关键字">
  <button id="go" onclick="filterRows()">查询</button>
  <a id="detail" href="#" onclick="showDetail(); return false;">详情</a>
  <div id="detail-box" style="display:none">详情内容</div>
  <!-- 通用能力用例：内网 SPA 的菜单常是无 role/onclick 的 cursor:pointer 元素 -->
  <div id="pseudo-menu" style="cursor:pointer">模拟菜单</div>
  <div id="menu-result">未点击</div>
  <!-- 通用能力用例：业务页常在 iframe 里，snapshot/get_text 必须穿透 -->
  <iframe id="sub" srcdoc="<!doctype html><html><body><p>子页正文</p><button>子页按钮</button></body></html>"></iframe>
  <!-- 只读护栏用例：危险按钮与文件上传控件在快照里标 blocked、点击被拒 -->
  <button id="del" onclick="document.getElementById('menu-result').innerText='已删除'">删除记录</button>
  <button id="save">保存</button>
  <input id="upload" type="file">
  <table id="grid">
    <thead><tr><th>姓名</th><th>部门</th></tr></thead>
    <tbody>
      <tr><td>张三</td><td>A部</td></tr>
      <tr><td>李四</td><td>B部</td></tr>
      <tr><td>张五</td><td>B部</td></tr>
    </tbody>
  </table>
  <script>
    function filterRows() {
      const dept = document.getElementById('dept').value;
      const kw = document.getElementById('kw').value.trim();
      document.querySelectorAll('#grid tbody tr').forEach(tr => {
        const cells = [...tr.children].map(td => td.innerText);
        const okDept = dept === 'all' || cells[1] === dept;
        const okKw = !kw || cells[0].includes(kw);
        tr.style.display = (okDept && okKw) ? '' : 'none';
      });
    }
    function showDetail() {
      document.getElementById('detail-box').style.display = 'block';
    }
    document.getElementById('pseudo-menu').addEventListener('click', () => {
      document.getElementById('menu-result').innerText = '菜单已点击';
    });
  </script>
</body></html>
"""

RECIPE = {
  'url': 'about:blank',
  'steps': [
    {'action': 'set_content', 'html': FIXTURE},
    {'action': 'select', 'selector': '#dept', 'value': '{dept}'},
    {'action': 'fill', 'selector': '#kw', 'value': '{kw}'},
    {'action': 'click', 'selector': '#go'},
  ],
  'ready': {'selector': '#grid', 'state': 'visible', 'timeout_ms': 3000},
  'extract': [
    {'kind': 'table', 'selector': '#grid', 'name': '人员'},
    {'kind': 'text', 'selector': 'body'},
  ],
}


# 录屏用例：页面持续变化，保证 CDP screencast 有帧可采
ANIM_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><title>录屏页</title></head>
<body>
  <div id="tick">0</div>
  <script>
    let n = 0;
    setInterval(() => {
      n += 1;
      document.getElementById('tick').textContent = String(n);
      document.body.style.background = (n % 2) ? '#eef' : '#fff';
    }, 100);
  </script>
</body></html>
"""


# 登录判定用例：token 失效时旧界面会先显示一会儿再被踢回登录页（残影）。
# 「已登录标记」先显示 300ms 再消失、登录表单出现；登录按钮点下后标记永久回来。
LOGIN_FLASH_HTML = """
<!doctype html>
<html><head><meta charset="utf-8"><title>登录页</title></head>
<body>
  <div id="marker">已登录标记</div>
  <form id="login-form" style="display:none">
    <input id="user">
    <input id="pass" type="password">
    <button id="submit" type="button">登录</button>
  </form>
  <script>
    setTimeout(() => {
      document.getElementById('marker').remove();
      document.getElementById('login-form').style.display = 'block';
    }, 300);
    document.getElementById('submit').addEventListener('click', () => {
      document.getElementById('login-form').style.display = 'none';
      const d = document.createElement('div');
      d.id = 'marker';
      d.textContent = '已登录标记';
      document.body.appendChild(d);
    });
  </script>
</body></html>
"""

LOGIN_FLASH_RECIPE = {
  'check': {'selector': '#marker'},
  'steps': [
    {'action': 'fill', 'selector': '#user', 'value': 'u'},
    {'action': 'fill', 'selector': '#pass', 'value': 'p'},
    {'action': 'click', 'selector': '#submit'},
  ],
  'ready': {'selector': '#marker', 'state': 'visible', 'timeout_ms': 3000},
  'save_state': False,
  'timeout_ms': 3000,
}


def _png_size(data: bytes) -> tuple[int, int]:
  assert data[:8] == b'\x89PNG\r\n\x1a\n', '不是 PNG'
  width = int.from_bytes(data[16:20], 'big')
  height = int.from_bytes(data[20:24], 'big')
  return width, height


def test_render() -> None:
  out = _render({'url': 'http://x/{park}/list', 'v': '{hours}'}, {'park': '廊坊', 'hours': 24})
  assert out == {'url': 'http://x/廊坊/list', 'v': '24'}, out
  try:
    _render({'v': '{missing}'}, {})
  except RecipeError as e:
    assert 'missing' in str(e)
  else:
    raise AssertionError('未知占位符应判配方错误')
  try:
    _render({'v': '{x}'}, {'x': 'a\nb'})
  except RecipeError:
    pass
  else:
    raise AssertionError('参数含换行应被拒绝')
  print('  [ok] 参数插值 / 未知占位符 / 非法字符')


async def _run_capture(body: dict) -> dict:
  async with sidecar_app.router.lifespan_context(sidecar_app):
    return await _capture(body)


def test_capture() -> None:
  try:
    result = asyncio.run(_run_capture({
      'recipe': RECIPE,
      'params': {'dept': 'B部', 'kw': '张'},
    }))
  except Exception as e:
    if 'Executable doesn' in str(e) or 'BrowserType.launch' in str(e):
      print(f'  [skip] 本机没有 Chromium: {e}')
      return
    raise

  assert result['ok'] is True, result
  assert result['page']['title'] == '测试页', result['page']
  tables = result['tables']
  assert tables and tables[0]['headers'] == ['姓名', '部门'], tables
  assert tables[0]['rows'] == [['张五', 'B部']], tables[0]['rows']
  assert tables[0]['row_count'] == 1, tables[0]

  image = base64.b64decode(result['image_b64'])
  assert len(image) > 1000, f'截图太小: {len(image)}'
  width, height = _png_size(image)
  assert width == 1440, width
  assert height >= 200, height
  # 正文取 inner_text（按渲染结果），被筛掉的行不出现；表格抽取按可见性过滤同样只剩 1 行
  assert '张五' in result['text'] and '李四' not in result['text'], result['text']
  print(f'  [ok] 捕获：截图 {width}x{height} / {len(image)}B，表格筛选命中 1 行')


def test_ready_timeout() -> None:
  bad = {
    'url': 'about:blank',
    'steps': [{'action': 'set_content', 'html': FIXTURE}],
    'ready': {'selector': '#nope', 'state': 'visible', 'timeout_ms': 300},
  }
  try:
    result = asyncio.run(_run_capture({'recipe': bad}))
  except Exception as e:
    if 'Executable doesn' in str(e) or 'BrowserType.launch' in str(e):
      print(f'  [skip] 本机没有 Chromium: {e}')
      return
    raise
  assert result['ok'] is False, result
  assert result['stage'] == 'ready', result
  assert 'Timeout' in result['error'] or '超时' in result['error'], result
  print(f"  [ok] 就绪超时 stage=ready：{result['error'][:60]}")


def test_unknown_param() -> None:
  bad = {'url': 'about:blank', 'steps': [{'action': 'fill', 'selector': '#kw', 'value': '{nope}'}]}
  try:
    asyncio.run(_run_capture({'recipe': bad}))
  except RecipeError:
    print('  [ok] 未知参数占位符在捕获前被拒绝')
  else:
    raise AssertionError('未知参数应在捕获前抛 RecipeError')


class _FixtureHandler(BaseHTTPRequestHandler):
  def do_GET(self):  # noqa: N802
    body = (ANIM_HTML if self.path.startswith('/anim') else FIXTURE).encode('utf-8')
    self.send_response(200)
    self.send_header('Content-Type', 'text/html; charset=utf-8')
    self.send_header('Content-Length', str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  def log_message(self, *args):  # 静音访问日志
    pass


def _start_fixture_server() -> ThreadingHTTPServer:
  server = ThreadingHTTPServer(('127.0.0.1', 0), _FixtureHandler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  return server


def _ref_of(elements: list[dict], name: str) -> int:
  for el in elements:
    if name in str(el.get('name') or ''):
      return int(el['ref'])
  raise AssertionError(f'快照里找不到元素 {name}: {elements}')


async def _session_flow(url: str) -> None:
  sid = 'selftest-session-01'
  nav = await _session_action({'action': 'navigate', 'session_id': sid, 'url': url})
  assert nav['ok'] is True, nav
  assert nav['title'] == '测试页', nav
  assert nav['elements'], nav

  # 通用能力：cursor:pointer 的纯 div 菜单、iframe 内元素都要出现在快照里
  _ref_of(nav['elements'], '模拟菜单')
  _ref_of(nav['elements'], '子页按钮')
  text0 = await _session_action({'action': 'get_text', 'session_id': sid, 'max_chars': '4000'})
  assert '子页正文' in text0['text'], text0['text']

  # 只读护栏：危险元素标 blocked，点击返回 blocked 且不产生副作用
  del_el = next((el for el in nav['elements'] if el.get('name') == '删除记录'), None)
  assert del_el and del_el.get('blocked') is True, nav['elements']
  save_el = next((el for el in nav['elements'] if el.get('name') == '保存'), None)
  assert save_el and save_el.get('blocked') is True, nav['elements']
  upload_el = next((el for el in nav['elements'] if el.get('type') == 'file'), None)
  assert upload_el and upload_el.get('blocked') is True, nav['elements']
  denied = await _session_action(
    {'action': 'click', 'session_id': sid, 'ref': str(del_el['ref'])},
  )
  assert denied['ok'] is False and denied['stage'] == 'blocked', denied
  assert '删除' in denied['error'], denied
  after_denied = await _session_action(
    {'action': 'get_text', 'session_id': sid, 'max_chars': '4000'},
  )
  assert '已删除' not in after_denied['text'], after_denied['text']

  snap = await _session_action({'action': 'snapshot', 'session_id': sid})
  assert snap['ok'] is True and snap['url'].startswith('http://127.0.0.1'), snap

  text = await _session_action({'action': 'get_text', 'session_id': sid, 'max_chars': '50'})
  assert text['ok'] is True and len(text['text']) <= 50, text

  # 点「详情」把隐藏区块显示出来 → 再读正文应能看到
  detail_ref = _ref_of(nav['elements'], '详情')
  clicked = await _session_action({'action': 'click', 'session_id': sid, 'ref': str(detail_ref)})
  assert clicked['ok'] is True, clicked
  after = await _session_action({'action': 'get_text', 'session_id': sid, 'max_chars': '4000'})
  assert '详情内容' in after['text'], after['text']

  # 指针元素可点击（无 role/onclick 也能点）
  menu_ref = _ref_of(clicked['elements'], '模拟菜单')
  menu_clicked = await _session_action({'action': 'click', 'session_id': sid, 'ref': str(menu_ref)})
  assert menu_clicked['ok'] is True, menu_clicked
  after_menu = await _session_action({'action': 'get_text', 'session_id': sid, 'max_chars': '4000'})
  assert '菜单已点击' in after_menu['text'], after_menu['text']

  shot = await _session_action({'action': 'screenshot', 'session_id': sid, 'full_page': 'false'})
  assert shot['ok'] is True, shot
  image = base64.b64decode(shot['image_b64'])
  width, height = _png_size(image)
  assert width == 1440 and height >= 200, (width, height)

  # 失效 ref：明确报 click，要求重新 snapshot
  stale = await _session_action({'action': 'click', 'session_id': sid, 'ref': '9999'})
  assert stale['ok'] is False and stale['stage'] == 'click', stale

  closed = await _session_action({'action': 'close', 'session_id': sid})
  assert closed == {'ok': True, 'closed': True}, closed
  gone = await _session_action({'action': 'snapshot', 'session_id': sid})
  assert gone['ok'] is False and gone['stage'] == 'session', gone


def test_session() -> None:
  server = _start_fixture_server()
  # 本机自测不设白名单，否则 127.0.0.1 会被导航白名单拦住
  saved_hosts = set(sidecar.ALLOWED_HOSTS)
  sidecar.ALLOWED_HOSTS.clear()
  try:
    url = f'http://127.0.0.1:{server.server_address[1]}/'

    async def runner():
      async with sidecar_app.router.lifespan_context(sidecar_app):
        await _session_flow(url)

    asyncio.run(runner())
  except Exception as e:
    if 'Executable doesn' in str(e) or 'BrowserType.launch' in str(e):
      print(f'  [skip] 本机没有 Chromium: {e}')
      return
    raise
  finally:
    sidecar.ALLOWED_HOSTS.update(saved_hosts)
    server.shutdown()
  print('  [ok] 交互会话：navigate/snapshot/get_text/click/screenshot/close + 失效 ref')


def test_record() -> None:
  server = _start_fixture_server()
  saved_hosts = set(sidecar.ALLOWED_HOSTS)
  sidecar.ALLOWED_HOSTS.clear()
  base = f'http://127.0.0.1:{server.server_address[1]}'
  try:
    async def runner():
      async with sidecar_app.router.lifespan_context(sidecar_app):
        sid = 'selftest-record-01'
        nav = await _session_action({
          'action': 'navigate', 'session_id': sid, 'url': f'{base}/anim',
        })
        assert nav['ok'] is True, nav
        rec = await _session_action({
          'action': 'record', 'session_id': sid,
          'seconds': 5, 'keyframe_interval': 1, 'video': True,
        })
        assert rec['ok'] is True, rec
        assert rec['duration_s'] == 5, rec
        assert len(rec['keyframes']) >= 2, rec['keyframes']
        assert all(k['image_b64'] for k in rec['keyframes']), rec['keyframes']

        # 录制结束只停录，不关会话
        snap = await _session_action({'action': 'snapshot', 'session_id': sid})
        assert snap['ok'] is True, snap

        if shutil.which('ffmpeg'):
          if rec.get('video_b64'):
            image = base64.b64decode(rec['video_b64'])
            assert image.startswith(b'\x1aE\xdf\xa3'), image[:4]
            assert rec['video_bytes'] > 0, rec
          else:
            # 纯静态页可能没有采集帧，但关键帧必须仍然交付
            assert rec.get('video_error'), rec
        else:
          assert 'ffmpeg' in rec.get('video_error', ''), rec

        closed = await _session_action({'action': 'close', 'session_id': sid})
        assert closed == {'ok': True, 'closed': True}, closed

    asyncio.run(runner())

    async def no_session():
      async with sidecar_app.router.lifespan_context(sidecar_app):
        gone = await _session_action({
          'action': 'record', 'session_id': 'selftest-record-none',
        })
        assert gone['ok'] is False and gone['stage'] == 'session', gone

    asyncio.run(no_session())
  except Exception as e:
    if 'Executable doesn' in str(e) or 'BrowserType.launch' in str(e):
      print(f'  [skip] 本机没有 Chromium: {e}')
      return
    raise
  finally:
    sidecar.ALLOWED_HOSTS.update(saved_hosts)
    server.shutdown()
  print('  [ok] 录屏：关键帧/视频/会话保持/无会话报错')


def test_navigate_rejects_non_http() -> None:
  for bad in ('file:///etc/passwd', 'about:blank', 'javascript:alert(1)'):
    try:
      _check_nav_target(bad)
    except RecipeError:
      continue
    raise AssertionError(f'{bad} 应被拒绝')
  print('  [ok] navigate 拒绝非 http/https')


def test_login_flash_marker() -> None:
  """旧界面残影先命中「已登录标记」，等页面静默后标记消失 —— 仍必须执行登录。"""

  async def runner():
    async with sidecar_app.router.lifespan_context(sidecar_app):
      ctx = await sidecar._browser.new_context(viewport=sidecar.DEFAULT_VIEWPORT)
      page = await ctx.new_page()
      page.set_default_timeout(sidecar.DEFAULT_STEP_TIMEOUT_MS)
      try:
        await page.set_content(LOGIN_FLASH_HTML, wait_until='domcontentloaded')
        did_login = await _ensure_login(page, ctx, LOGIN_FLASH_RECIPE, '')
        marker = await page.locator('#marker').is_visible()
        form = await page.locator('#login-form').is_visible()
        return did_login, marker, form
      finally:
        await ctx.close()

  try:
    did_login, marker, form = asyncio.run(runner())
  except Exception as e:
    if 'Executable doesn' in str(e) or 'BrowserType.launch' in str(e):
      print(f'  [skip] 本机没有 Chromium: {e}')
      return
    raise
  assert did_login is True, '残影不该被当成已登录'
  assert marker is True and form is False, (marker, form)
  print('  [ok] 登录残影：等页面静默后复查，仍然执行了登录')


def test_login_not_login_page() -> None:
  """目标地址不是登录页时快速失败（stage=login_page），不盲等登录表单超时。"""
  server = _start_fixture_server()
  saved_hosts = set(sidecar.ALLOWED_HOSTS)
  saved_probe = sidecar.LOGIN_PROBE_MS
  sidecar.ALLOWED_HOSTS.clear()
  sidecar.LOGIN_PROBE_MS = 400  # 缩短探测等待，保持自测秒级完成
  try:
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    recipe = {
      'url': url,
      'login': {
        'check': {'selector': '#no-marker'},
        'steps': [{'action': 'fill', 'selector': '#no-login-input', 'value': 'x'}],
        'timeout_ms': 500,
        'save_state': False,
      },
      'extract': [{'kind': 'text', 'selector': 'body'}],
    }
    t0 = time.monotonic()
    try:
      result = asyncio.run(_run_capture({'recipe': recipe}))
    except Exception as e:
      if 'Executable doesn' in str(e) or 'BrowserType.launch' in str(e):
        print(f'  [skip] 本机没有 Chromium: {e}')
        return
      raise
    elapsed = time.monotonic() - t0
    assert result['ok'] is False, result
    assert result['stage'] == 'login_page', result
    assert elapsed < 10, elapsed
    print(f'  [ok] 非登录页快速失败：{elapsed:.1f}s stage=login_page')
  finally:
    sidecar.ALLOWED_HOSTS.clear()
    sidecar.ALLOWED_HOSTS.update(saved_hosts)
    sidecar.LOGIN_PROBE_MS = saved_probe
    server.shutdown()


def main() -> None:
  print('browser sidecar self-test')
  test_render()
  test_unknown_param()
  test_capture()
  test_ready_timeout()
  test_navigate_rejects_non_http()
  test_session()
  test_record()
  test_login_flash_marker()
  test_login_not_login_page()
  print('全部通过')


if __name__ == '__main__':
  main()

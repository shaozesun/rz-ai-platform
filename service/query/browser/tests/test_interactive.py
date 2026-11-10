"""browser 交互能力自测（纯 assert，不连网、不启浏览器）。

运行：`python -m service.query.browser.tests.test_interactive`
覆盖：7 条能力/工具名/分派、会话 id 派生、system 推断、登录模板占位符替换、
导航校验（协议/主机白名单）、元素裁剪、返回整形（含截图/录屏落盘）、关闭开关与非法参数。

真实页面流程（navigate→snapshot→click→screenshot→record→close）在旁路容器里由
`deploy/browser/test_app.py` 覆盖（需 Chromium）。
"""

import asyncio
import base64
import re
import shutil
import tempfile
from pathlib import Path

from config.settings import settings
from core.agent import user_context
from service.query.browser import interactive
from service.query.browser.client import BrowserError
from service.query.browser.interactive import (
  _fill_credentials,
  _prepare_navigate,
  _session_id,
  _shape,
  _shrink_elements,
  _system_for,
  build_interactive_capabilities,
  call,
  handles,
  tool_names,
)

_PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 32


def _patch(name: str, value) -> object:
  old = getattr(settings, name)
  setattr(settings, name, value)
  return old


def test_capability_set() -> None:
  caps = build_interactive_capabilities()
  ids = {c.id for c in caps}
  assert ids == {
    'browser.navigate', 'browser.snapshot', 'browser.click',
    'browser.get_text', 'browser.screenshot', 'browser.record', 'browser.close',
  }, ids
  for cap in caps:
    assert cap.category == 'page' and cap.domain == 'explore', cap.id
    assert handles(cap.id), cap.id
  # 两个写副作用动作（type/back）刻意不做
  assert not handles('browser.type') and not handles('browser.back')
  assert not handles('mgmt.query_my_info')
  assert tool_names() == {c.id.replace('.', '__') for c in caps}
  print('  [ok] 7 条交互能力（navigate/snapshot/click/get_text/screenshot/record/close）')


def test_session_id() -> None:
  a = _session_id('rz-agent-abc')
  b = _session_id('rz-agent-abc')
  c = _session_id('rz-agent-xyz')
  assert a == b and a != c
  assert len(a) == 16 and re.fullmatch(r'[0-9a-f]{16}', a), a
  print('  [ok] 会话 id 由 thread_id 确定性派生')


def test_system_for() -> None:
  o1 = _patch('BROWSER_DCIM_URL', 'http://dcim.example.com:8080/')
  o2 = _patch('BROWSER_MGMT_URL', 'https://mgmt.example.com/')
  try:
    assert _system_for('http://dcim.example.com:8080/alarms') == 'dcim'
    assert _system_for('https://mgmt.example.com/center') == 'mgmt'
    assert _system_for('http://other.example.com/x') == ''
    assert _system_for('file:///etc/passwd') == ''
  finally:
    _patch('BROWSER_DCIM_URL', o1)
    _patch('BROWSER_MGMT_URL', o2)
  print('  [ok] system 按主机名推断（路径不参与）')


def test_fill_credentials() -> None:
  out = _fill_credentials(
    {'steps': [{'value': '{username}'}, {'value': '{password}'}], 'url': '{username}'},
    'u1', 'p1',
  )
  assert out['steps'][0]['value'] == 'u1' and out['steps'][1]['value'] == 'p1'
  assert out['url'] == 'u1'
  print('  [ok] 登录模板占位符递归替换')


def test_prepare_navigate() -> None:
  o1 = _patch('BROWSER_ALLOWED_HOSTS', 'dcim.example.com')
  o2 = _patch('BROWSER_DCIM_URL', 'http://dcim.example.com/')
  try:
    for bad in ('file:///etc/passwd', 'about:blank', 'javascript:alert(1)'):
      try:
        _prepare_navigate({}, {'url': bad})
      except BrowserError:
        pass
      else:
        raise AssertionError(f'应拒绝非 http(s): {bad}')

    try:
      _prepare_navigate({}, {'url': 'http://evil.example.com/x'})
    except BrowserError:
      pass
    else:
      raise AssertionError('应拒绝白名单外主机')

    body: dict = {}
    _prepare_navigate(body, {'url': 'http://dcim.example.com/alarms'})
    assert body == {'url': 'http://dcim.example.com/alarms', 'system': 'dcim'}, body
    assert 'login' not in body, '模板未校准不应带 login'

    body = {}
    _prepare_navigate(body, {'url': 'http://dcim.example.com/x', 'system': 'bogus'})
    assert body['system'] == '', body  # 非法 system 归空，不拼进 URL
  finally:
    _patch('BROWSER_ALLOWED_HOSTS', o1)
    _patch('BROWSER_DCIM_URL', o2)
  print('  [ok] 导航校验（协议/白名单/system 归一/未校准不带 login）')


def test_prepare_navigate_login() -> None:
  o_allowed = _patch('BROWSER_ALLOWED_HOSTS', '')
  o_url = _patch('BROWSER_DCIM_URL', 'http://dcim.example.com/')
  o_user = _patch('BROWSER_DCIM_USERNAME', 'svc')
  o_pass = _patch('BROWSER_DCIM_PASSWORD', 'secret')
  old_login = interactive.LOGIN_RECIPES['dcim']
  interactive.LOGIN_RECIPES['dcim'] = {
    'enabled': True,
    'check': {'selector': '#app'},
    'url': '',
    'steps': [
      {'action': 'fill', 'selector': '#u', 'value': '{username}'},
      {'action': 'fill', 'selector': '#p', 'value': '{password}'},
    ],
    'ready': {'selector': '#main', 'state': 'visible'},
    'save_state': True,
  }
  try:
    body: dict = {}
    _prepare_navigate(body, {'url': 'http://dcim.example.com/alarms'})
    login = body['login']
    assert login['steps'][0]['value'] == 'svc' and login['steps'][1]['value'] == 'secret'
    assert 'enabled' not in login, 'enabled 不应下发给旁路服务'

    _patch('BROWSER_DCIM_PASSWORD', '')
    try:
      _prepare_navigate({}, {'url': 'http://dcim.example.com/alarms'})
    except BrowserError:
      pass
    else:
      raise AssertionError('启用登录但缺账号应报错')
  finally:
    interactive.LOGIN_RECIPES['dcim'] = old_login
    _patch('BROWSER_ALLOWED_HOSTS', o_allowed)
    _patch('BROWSER_DCIM_URL', o_url)
    _patch('BROWSER_DCIM_USERNAME', o_user)
    _patch('BROWSER_DCIM_PASSWORD', o_pass)
  print('  [ok] 登录模板：占位符替换 / 缺账号报错 / enabled 不下发')


def test_shrink_elements() -> None:
  els = [
    {'ref': i, 'tag': 'a', 'name': 'x' * 300, 'role': 'link', 'type': ''}
    for i in range(200)
  ]
  els[1]['blocked'] = True
  out = _shrink_elements(els)
  assert len(out) == 120
  assert len(out[0]['name']) == 100
  assert out[0]['role'] == 'link' and 'type' not in out[0]
  # 只读护栏：危险元素的 blocked 标记必须原样透传给模型
  assert out[1]['blocked'] is True and 'blocked' not in out[0], out[:2]
  print('  [ok] 元素裁剪（120 个 / 名称 100 字 / 省略空字段 / blocked 透传）')


def test_blocked_stage_hint() -> None:
  from service.query.browser import client as browser_client

  assert 'blocked' in browser_client._STAGE_HINTS
  msg = browser_client._friendly_error({'stage': 'blocked', 'error': '命中「删除」'})
  assert '只读策略' in msg and '命中「删除」' in msg, msg
  print('  [ok] blocked 错误提示（点击危险元素）')


def test_shape() -> None:
  page = {'url': 'http://dcim.example.com/x', 'title': '告警',
          'elements': [{'ref': 1, 'tag': 'a', 'name': '详情'}]}
  nav = _shape('navigate', page, 't')
  assert nav['ok'] and nav['title'] == '告警' and nav['note'] and nav['elements'][0]['ref'] == 1
  assert _shape('close', {'closed': True}, 't') == {'ok': True, 'closed': True}

  txt = _shape('get_text', {'text': 'abc', 'truncated': True}, 't')
  assert txt == {'ok': True, 'text': 'abc', 'truncated': True}

  old = _patch('AGENT_ANALYSIS_DATA_DIR', tempfile.mkdtemp(prefix='browser-it-'))
  try:
    shot = _shape('screenshot', {
      'image_b64': base64.b64encode(_PNG).decode(),
      'page': {'url': page['url'], 'title': page['title']},
    }, 'rz-agent-t')
    art = shot['artifacts'][0]
    assert art['size'] == len(_PNG) and art['name'].endswith('.png')
    assert '/browser/' in art['url'] and 'rz-agent-t' in art['url']
    saved = Path(settings.AGENT_ANALYSIS_DATA_DIR) / 'rz-agent-t' / 'browser' / art['name']
    assert saved.read_bytes() == _PNG

    try:
      _shape('screenshot', {'image_b64': ''}, 'rz-agent-t')
    except BrowserError:
      pass
    else:
      raise AssertionError('空截图应报错')

    rec = _shape('record', {
      'page': {'url': page['url'], 'title': page['title']},
      'duration_s': 3,
      'frames_captured': 2,
      'keyframes': [
        {'t': 0.0, 'image_b64': base64.b64encode(_PNG).decode()},
        {'t': 1.5, 'image_b64': base64.b64encode(_PNG).decode()},
      ],
      'video_b64': base64.b64encode(b'\x1aE\xdf\xa3webm').decode(),
      'video_error': '',
    }, 'rz-agent-t')
    assert rec['duration_s'] == 3 and rec['frames_captured'] == 2, rec
    assert [a['t'] for a in rec['artifacts']] == [0.0, 1.5], rec['artifacts']
    assert all(a['name'].endswith('.jpg') for a in rec['artifacts']), rec['artifacts']
    assert rec['video'] and rec['video']['name'].endswith('.webm'), rec['video']
    for art in rec['artifacts']:
      saved = Path(settings.AGENT_ANALYSIS_DATA_DIR) / 'rz-agent-t' / 'browser' / art['name']
      assert saved.read_bytes() == _PNG, art['name']
    video_saved = Path(settings.AGENT_ANALYSIS_DATA_DIR) / 'rz-agent-t' / 'browser' / rec['video']['name']
    assert video_saved.read_bytes().startswith(b'\x1aE\xdf\xa3'), rec['video']['name']

    rec2 = _shape('record', {
      'keyframes': [{'t': 0.0, 'image_b64': base64.b64encode(_PNG).decode()}],
      'video_error': 'ffmpeg 不可用',
    }, 'rz-agent-t')
    assert rec2['video'] is None and '关键帧仍可用' in rec2['note'], rec2

    try:
      _shape('record', {'keyframes': []}, 'rz-agent-t')
    except BrowserError:
      pass
    else:
      raise AssertionError('没有关键帧应报错')
  finally:
    shutil.rmtree(settings.AGENT_ANALYSIS_DATA_DIR, ignore_errors=True)
    _patch('AGENT_ANALYSIS_DATA_DIR', old)
  print('  [ok] 返回整形（页面/文本/截图/录屏落盘/关闭）')


def test_call_guards() -> None:
  async def run() -> None:
    # 开关关闭：任何调用都应立刻拒绝
    try:
      await call('browser.navigate', {'url': 'http://x/'})
    except BrowserError:
      pass
    else:
      raise AssertionError('开关关闭应拒绝')

    # 开关打开后，以下错误都应在发请求之前抛出
    o_enabled = _patch('BROWSER_EXPLORE_ENABLED', True)
    try:
      token = user_context._current_thread_id.set('')
      try:
        try:
          await call('browser.navigate', {'url': 'http://x/'})
        except BrowserError:
          pass
        else:
          raise AssertionError('无 thread 上下文应拒绝')
      finally:
        user_context._current_thread_id.reset(token)

      user_context.set_current_thread_id('rz-agent-guard')
      try:
        for cap, params in (
          ('browser.unknown', {}),
          ('browser.click', {'ref': 'abc'}),
          ('browser.get_text', {'max_chars': 'x'}),
          ('browser.record', {'seconds': 'abc'}),
          ('browser.record', {'keyframe_interval': 'x'}),
        ):
          try:
            await call(cap, params)
          except BrowserError:
            pass
          else:
            raise AssertionError(f'应拒绝: {cap} {params}')
      finally:
        user_context.set_current_thread_id('')
    finally:
      _patch('BROWSER_EXPLORE_ENABLED', o_enabled)

  o_enabled = _patch('BROWSER_EXPLORE_ENABLED', False)
  try:
    asyncio.run(run())
  finally:
    _patch('BROWSER_EXPLORE_ENABLED', o_enabled)
  print('  [ok] call 守卫（开关/会话上下文/未知能力/非法参数）')


def test_record_params() -> None:
  """录制参数在发请求前钳制：秒数下限 5 / 上限 MAX、间隔下限 1、video 布尔解析。"""

  async def run() -> None:
    captured: list[dict] = []

    async def fake_post(payload: dict) -> dict:
      captured.append(dict(payload))
      return {
        'ok': True,
        'page': {'url': 'http://x/', 'title': 't'},
        'duration_s': payload.get('seconds'),
        'frames_captured': 0,
        'keyframes': [{'t': 0.0, 'image_b64': base64.b64encode(_PNG).decode()}],
        'video_b64': '',
        'video_error': 'ffmpeg 不可用',
      }

    old_post = interactive._post_session
    patches = {
      'AGENT_ANALYSIS_DATA_DIR': tempfile.mkdtemp(prefix='browser-rec-'),
      'BROWSER_EXPLORE_ENABLED': True,
      'BROWSER_RECORD_SECONDS': 30,
      'BROWSER_MAX_RECORD_SECONDS': 120,
      'BROWSER_RECORD_KEYFRAME_SECONDS': 5,
    }
    old = {name: _patch(name, value) for name, value in patches.items()}
    interactive._post_session = fake_post
    user_context.set_current_thread_id('rz-agent-rec')
    try:
      await call('browser.record', {})
      assert captured[-1]['seconds'] == 30, captured[-1]
      assert captured[-1]['keyframe_interval'] == 5, captured[-1]
      assert captured[-1]['video'] is True, captured[-1]

      await call('browser.record', {
        'seconds': '999', 'keyframe_interval': '2', 'video': 'false',
      })
      assert captured[-1]['seconds'] == 120, captured[-1]
      assert captured[-1]['keyframe_interval'] == 2, captured[-1]
      assert captured[-1]['video'] is False, captured[-1]

      await call('browser.record', {'seconds': '1'})
      assert captured[-1]['seconds'] == 5, captured[-1]

      assert captured[-1]['action'] == 'record' and captured[-1]['session_id']
    finally:
      user_context.set_current_thread_id('')
      interactive._post_session = old_post
      for name, value in old.items():
        _patch(name, value)
      shutil.rmtree(settings.AGENT_ANALYSIS_DATA_DIR, ignore_errors=True)

  asyncio.run(run())
  print('  [ok] 录制参数钳制（秒数/间隔/video）')


def test_manifest_positioning() -> None:
  from service.query.base.manifest import platform_definition

  old = _patch('BROWSER_EXPLORE_ENABLED', False)
  try:
    assert '交互模式' not in platform_definition('browser')
    assert '截图' in platform_definition('browser')
  finally:
    _patch('BROWSER_EXPLORE_ENABLED', old)

  old = _patch('BROWSER_EXPLORE_ENABLED', True)
  try:
    assert '交互模式' in platform_definition('browser')

    # 配方未校准（BROWSER_ENABLED=false）时不得宣传「预置配方」——那批工具没注册
    o_recipe = _patch('BROWSER_ENABLED', False)
    try:
      defn = platform_definition('browser')
      assert '预置配方' not in defn and '交互模式' in defn, defn
    finally:
      _patch('BROWSER_ENABLED', o_recipe)

    o_recipe = _patch('BROWSER_ENABLED', True)
    try:
      assert '预置配方' in platform_definition('browser')
    finally:
      _patch('BROWSER_ENABLED', o_recipe)
  finally:
    _patch('BROWSER_EXPLORE_ENABLED', old)
  print('  [ok] 平台定位随交互开关切换（不宣传未注册的工具）')


def main() -> None:
  print('browser interactive self-test')
  test_capability_set()
  test_session_id()
  test_system_for()
  test_fill_credentials()
  test_prepare_navigate()
  test_prepare_navigate_login()
  test_shrink_elements()
  test_blocked_stage_hint()
  test_shape()
  test_record_params()
  test_call_guards()
  test_manifest_positioning()
  print('全部通过')


if __name__ == '__main__':
  main()

"""browser 配方/调用器自测（纯 assert，不连网、不启浏览器）。

运行：`python -m service.query.browser.tests.test_recipes`
覆盖：参数合并与校验、能力随 enabled 配方生成、主机白名单、截图发布不覆盖。
"""

import shutil
import tempfile
from pathlib import Path

from config.settings import settings
from service.query.browser.capabilities import build_capabilities
from service.query.browser.client import (
  BrowserError,
  _build_request,
  _check_host,
  _publish_png,
  _shrink_tables,
)
from service.query.browser.recipes import (
  Recipe,
  RecipeError,
  enabled_recipes,
  get_recipe,
  resolve_params,
)


def _patch(name: str, value) -> object:
  old = getattr(settings, name)
  setattr(settings, name, value)
  return old


def test_params() -> None:
  recipe = Recipe(
    id='t', name='t', description='t', system='dcim', path='/p',
    params={'park': '园区', 'level': '等级'},
    param_enums={'level': ('紧急', '重要')},
    defaults={'level': '重要'},
  )
  p = resolve_params(recipe, {'park': '廊坊'})
  assert p == {'level': '重要', 'park': '廊坊'}, p
  assert resolve_params(recipe, {'level': '紧急'})['level'] == '紧急'

  for bad in ({'level': '重大'}, {'nope': 'x'}):
    try:
      resolve_params(recipe, bad)
    except RecipeError:
      pass
    else:
      raise AssertionError(f'应拒绝非法参数: {bad}')
  assert resolve_params(recipe, {'park': ''}) == {'level': '重要'}  # 空串被丢弃
  print('  [ok] 参数合并/枚举校验/未知参数拒绝')


def test_capabilities_follow_enabled() -> None:
  caps = build_capabilities()
  expected = {f'browser.{r.id}' for r in enabled_recipes()}
  assert {c.id for c in caps} == expected, (caps, expected)
  for cap in caps:
    assert cap.category == 'page' and cap.domain == 'capture'
    assert cap.params, f'{cap.id} 没有参数'
  print(f'  [ok] 能力数={len(caps)}（模板未校准时不注册）')


def test_get_recipe() -> None:
  assert get_recipe('mgmt.query_my_info') is None
  assert get_recipe('browser.capture_dcim_alarms') is not None
  assert get_recipe('browser.unknown') is None
  print('  [ok] 配方定位')


def test_host_whitelist() -> None:
  old = _patch('BROWSER_ALLOWED_HOSTS', 'dcim.example.com, mgmt.example.com')
  try:
    _check_host('http://dcim.example.com/alarms', {})
    for bad in ('http://evil.example.com/x', 'http://{park}.example.com/x'):
      try:
        _check_host(bad, {})
      except BrowserError:
        pass
      else:
        raise AssertionError(f'应拒绝: {bad}')
  finally:
    _patch('BROWSER_ALLOWED_HOSTS', old)
  print('  [ok] 主机白名单（含未解析占位符拒绝）')


def test_build_request() -> None:
  recipe = Recipe(
    id='t', name='t', description='t', system='dcim', path='/alarms?hours={hours}',
    params={'hours': '窗口', 'park': '园区'}, defaults={'hours': '24'},
    steps=({'action': 'fill', 'selector': '#kw', 'value': '{park}'},),
    ready={'selector': '#grid', 'state': 'visible'},
  )
  old = _patch('BROWSER_DCIM_URL', 'http://dcim.example.com/')
  # 用例自造域名，必须同时放开白名单（否则会读真实 .env 的 BROWSER_ALLOWED_HOSTS 被拒）
  old_hosts = _patch('BROWSER_ALLOWED_HOSTS', 'dcim.example.com')
  try:
    payload = _build_request(recipe, {'park': '廊坊'})
    assert payload['recipe']['url'] == 'http://dcim.example.com/alarms?hours={hours}', payload
    assert payload['params']['hours'] == '24' and payload['params']['park'] == '廊坊'
    assert payload['recipe']['steps'][0]['value'] == '{park}'
    assert payload['system'] == 'dcim'
  finally:
    _patch('BROWSER_DCIM_URL', old)
    _patch('BROWSER_ALLOWED_HOSTS', old_hosts)
  print('  [ok] 请求构造（占位符留给旁路服务插值）')


def test_publish_no_overwrite() -> None:
  old = _patch('AGENT_ANALYSIS_DATA_DIR', tempfile.mkdtemp(prefix='browser-test-'))
  try:
    data = b'\x89PNG\r\n\x1a\n' + b'0' * 32
    n1 = _publish_png('rz-agent-thread', 'capture_x', data)
    n2 = _publish_png('rz-agent-thread', 'capture_x', data)
    assert n1 != n2, (n1, n2)
    d = Path(settings.AGENT_ANALYSIS_DATA_DIR) / 'rz-agent-thread' / 'browser'
    assert (d / n1).read_bytes() == data and (d / n2).read_bytes() == data
    assert n1.endswith('.png') and n2.endswith('.png')
    assert not any(p.name.startswith('.') for p in d.iterdir()), '临时文件未清理'
  finally:
    shutil.rmtree(settings.AGENT_ANALYSIS_DATA_DIR, ignore_errors=True)
    _patch('AGENT_ANALYSIS_DATA_DIR', old)
  print('  [ok] 截图原子发布且不覆盖同名文件')


def test_shrink_tables() -> None:
  tables = [
    {'name': 'a', 'headers': list(range(20)), 'rows': [[str(i)] * 20 for i in range(300)]},
    {'name': 'b', 'headers': ['x'], 'rows': [['1']], 'row_count': 500, 'truncated': True},
    {'name': 'c', 'headers': [], 'rows': []},  # 第 3 张应被丢弃
  ]
  out = _shrink_tables(tables)
  assert len(out) == 2
  assert len(out[0]['headers']) == 10 and len(out[0]['rows']) == 100
  assert len(out[0]['rows'][0]) == 10 and out[0]['truncated'] is True
  assert out[1]['row_count'] == 500 and out[1]['truncated'] is True
  print('  [ok] 表格裁剪（2 张 / 100 行 / 10 列）')


def main() -> None:
  print('browser recipes self-test')
  test_params()
  test_capabilities_follow_enabled()
  test_get_recipe()
  test_host_whitelist()
  test_build_request()
  test_publish_no_overwrite()
  test_shrink_tables()
  print('全部通过')


if __name__ == '__main__':
  main()

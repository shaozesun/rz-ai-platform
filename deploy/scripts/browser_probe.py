"""探查内网页面结构，为 browser 配方生成选择器（配食谱工具，不暴露给模型）。

用法（二选一）：
  # 走已部署的旁路容器（后端 / 跳板机上执行，推荐）
  python deploy/scripts/browser_probe.py --system dcim --url http://<dcim-ui>/alarm/list \
    --service-url http://browser:8100 --out /tmp/dcim_alarm.json

  # 本机直接用 Playwright 探查（需 pip install playwright && playwright install chromium，
  # 还需项目依赖 fastapi；复用 deploy/browser/app.py 的检查逻辑）
  python deploy/scripts/browser_probe.py --system dcim --url http://<dcim-ui>/alarm/list \
    --local --steps-file /tmp/steps.json --out /tmp/dcim_alarm.json

登录态：
  --local 读 deploy/browser_state/{system}.json（先跑 browser_login.py）；
  旁路模式读容器内 /state/{system}.json。页面需要先设筛选时用 --steps-file 传步骤：

  {"steps": [{"action": "fill", "selector": "#kw", "value": "廊坊"},
             {"action": "click", "selector": "#search"}],
   "ready": {"selector": "#grid", "state": "visible"}}

输出 JSON：页面标题/URL、可交互元素（id/name/role/testid/class）、表格表头与行数。
拿到后按 service/query/browser/recipes.py 的说明补齐 steps/ready/extract，把 enabled 置 True。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SIDECAR_APP = ROOT / 'deploy' / 'browser' / 'app.py'
DEFAULT_STATE_DIR = ROOT / 'deploy' / 'browser_state'
DEFAULT_SERVICE_URL = os.environ.get('BROWSER_SERVICE_URL') or 'http://browser:8100'


def _load_recipe(args) -> dict:
  """组装旁路服务 / 本机检查共用的 recipe：url + steps + ready。"""
  steps: list = []
  ready: dict = {}
  if args.steps_file:
    payload = json.loads(Path(args.steps_file).read_text(encoding='utf-8'))
    if isinstance(payload, list):
      steps = payload
    elif isinstance(payload, dict):
      steps = payload.get('steps') or []
      ready = payload.get('ready') or {}
    else:
      raise SystemExit('--steps-file 必须是步骤数组，或 {steps, ready} 对象')
    if not isinstance(steps, list):
      raise SystemExit('steps 必须是数组')
  if args.ready_selector:
    ready = {
      'selector': args.ready_selector,
      'state': args.ready_state,
      'timeout_ms': args.ready_timeout_ms,
    }
  return {
    'url': args.url,
    'steps': steps,
    'ready': ready,
    'wait_until': 'domcontentloaded',
  }


def _parse_params(items: list[str]) -> dict:
  params: dict[str, str] = {}
  for item in items or []:
    if '=' not in item:
      raise SystemExit(f'--param 需要 KEY=VALUE 形式: {item}')
    key, value = item.split('=', 1)
    if not key.strip():
      raise SystemExit(f'--param 参数名不能为空: {item}')
    params[key.strip()] = value
  return params


def _post_inspect(args, recipe: dict, params: dict) -> dict:
  """向旁路服务 POST /v1/inspect。"""
  url = args.service_url.rstrip('/') + '/v1/inspect'
  body = json.dumps(
    {'system': args.system, 'params': params, 'recipe': recipe},
    ensure_ascii=False,
  ).encode('utf-8')
  headers = {'Content-Type': 'application/json'}
  if args.token:
    headers['X-Browser-Token'] = args.token
  req = urllib.request.Request(url, data=body, headers=headers, method='POST')
  try:
    with urllib.request.urlopen(req, timeout=args.timeout) as resp:
      return json.loads(resp.read().decode('utf-8'))
  except urllib.error.HTTPError as e:
    raw = e.read().decode('utf-8', 'replace')
    try:
      detail = json.loads(raw)
    except ValueError:
      detail = {'ok': False, 'stage': f'http_{e.code}', 'error': raw[:500]}
    detail.setdefault('stage', f'http_{e.code}')
    return detail
  except urllib.error.URLError as e:
    return {'ok': False, 'stage': 'connect', 'error': f'无法连接 {url}: {e.reason}'}
  except TimeoutError:
    return {'ok': False, 'stage': 'timeout', 'error': f'探查超时（{args.timeout}s）'}


def _load_sidecar():
  """加载旁路服务模块以复用 _inspect（本机模式）。"""
  if importlib.util.find_spec('playwright') is None:
    raise SystemExit('本机模式需要 Playwright：pip install playwright && playwright install chromium')
  if importlib.util.find_spec('fastapi') is None:
    raise SystemExit('本机模式需要项目依赖：pip install -r requirements.txt')
  spec = importlib.util.spec_from_file_location('rz_browser_sidecar_probe', SIDECAR_APP)
  if spec is None or spec.loader is None:
    raise SystemExit(f'无法加载旁路服务模块: {SIDECAR_APP}')
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


async def _run_local(args, recipe: dict, params: dict) -> dict:
  """本机启动 Chromium 后复用旁路服务的 _inspect。"""
  from playwright.async_api import async_playwright

  module = _load_sidecar()
  module.STATE_DIR = Path(args.state_dir)
  module.ALLOWED_HOSTS = {
    h.strip().lower() for h in (args.allowed_hosts or '').split(',') if h.strip()
  }
  async with async_playwright() as p:
    browser = await p.chromium.launch(
      headless=not args.headed,
      args=[
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--font-render-hinting=none',
      ],
    )
    module._browser = browser
    try:
      return await asyncio.wait_for(
        module._inspect({'system': args.system, 'params': params, 'recipe': recipe}),
        timeout=args.timeout,
      )
    except asyncio.TimeoutError:
      return {'ok': False, 'stage': 'timeout', 'error': f'探查超时（{args.timeout}s）'}
    except module.RecipeError as e:
      return {'ok': False, 'stage': 'recipe', 'error': str(e)}
    finally:
      module._browser = None
      await browser.close()


def _print_summary(result: dict) -> None:
  if not result.get('ok'):
    print(f'探查失败 [{result.get("stage")}]: {result.get("error")}', file=sys.stderr)
    return
  page = result.get('page') or {}
  elements = result.get('elements') or []
  tables = result.get('tables') or []
  print(f'页面: {page.get("title", "")}  {page.get("url", "")}', file=sys.stderr)
  print(f'可交互元素: {len(elements)}', file=sys.stderr)
  for el in elements[:15]:
    hints = ', '.join(
      f'{k}={el[k]}' for k in ('id', 'name', 'role', 'aria', 'placeholder', 'testid')
      if el.get(k)
    )
    label = (el.get('text') or '')[:30]
    print(f'  <{el.get("tag")}> {label}  {hints}', file=sys.stderr)
  print(f'表格: {len(tables)}', file=sys.stderr)
  for t in tables:
    heads = ' / '.join(t.get('headers') or [])[:120]
    print(f'  #{t.get("index")} id={t.get("id") or "-"} rows={t.get("row_count")} '
          f'headers=[{heads}]', file=sys.stderr)


def main() -> int:
  parser = argparse.ArgumentParser(description='探查内网页面结构，生成 browser 配方选择器')
  parser.add_argument('--system', required=True, help='登录态标识：dcim / mgmt')
  parser.add_argument('--url', required=True, help='目标页面完整地址')
  parser.add_argument('--steps-file', help='筛选步骤 JSON（数组，或 {steps, ready} 对象）')
  parser.add_argument('--ready-selector', help='就绪条件：等待该元素出现')
  parser.add_argument('--ready-state', default='visible', help='就绪状态（默认 visible）')
  parser.add_argument('--ready-timeout-ms', type=int, default=20000)
  parser.add_argument('--param', action='append', default=[],
                      help='配方参数 KEY=VALUE（可重复，供 URL/步骤里的 {占位符} 插值）')
  parser.add_argument('--service-url', default=DEFAULT_SERVICE_URL,
                      help=f'旁路服务地址（默认环境变量 BROWSER_SERVICE_URL 或 {DEFAULT_SERVICE_URL}）')
  parser.add_argument('--token', default=os.environ.get('BROWSER_SERVICE_TOKEN', ''),
                      help='旁路服务 token（默认环境变量 BROWSER_SERVICE_TOKEN）')
  parser.add_argument('--timeout', type=float, default=90, help='整体超时秒数（默认 90）')
  parser.add_argument('--out', help='结果 JSON 输出路径（不给则打印到标准输出）')
  parser.add_argument('--local', action='store_true', help='本机 Playwright 模式（不走旁路服务）')
  parser.add_argument('--state-dir', default=str(DEFAULT_STATE_DIR),
                      help='本机模式的登录态目录（默认 deploy/browser_state）')
  parser.add_argument('--allowed-hosts', default='',
                      help='本机模式的主机白名单（逗号分隔，留空不限制）')
  parser.add_argument('--headed', action='store_true', help='本机模式显示浏览器窗口')
  args = parser.parse_args()

  recipe = _load_recipe(args)
  params = _parse_params(args.param)

  if args.local:
    result = asyncio.run(_run_local(args, recipe, params))
  else:
    result = _post_inspect(args, recipe, params)

  _print_summary(result)
  if args.out:
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'已写入 {out}', file=sys.stderr)
  elif result.get('ok'):
    print(json.dumps(result, ensure_ascii=False, indent=2))

  return 0 if result.get('ok') else 2


if __name__ == '__main__':
  sys.exit(main())

"""手动引导登录，把 storageState 落盘供旁路服务复用。

用于目标系统有**验证码 / SSO / 扫码**、配方里没法自动登录的情况
（纯账号密码登录也可以走配方的 login 步骤，不必用本脚本）。

用法（在能访问目标内网的操作机上执行，需一次性装 Playwright）：
  pip install playwright && playwright install chromium
  python deploy/scripts/browser_login.py --system dcim --url http://<dcim-ui>/
  # 在弹出的浏览器窗口里手动完成登录；回到终端按回车保存
  docker cp deploy/browser_state/dcim.json rz-browser:/state/dcim.json

注意：凭据只会以浏览器 storageState 的形式落盘（Cookie/LocalStorage），
**不含账号密码**；文件权限 600。换人/换账号就重跑一次。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

try:
  from playwright.async_api import async_playwright
except ImportError:
  print('需要 Playwright：pip install playwright && playwright install chromium')
  raise SystemExit(1)


async def _run(args) -> int:
  out = Path(args.state_out)
  out.parent.mkdir(parents=True, exist_ok=True)

  async with async_playwright() as p:
    browser = await p.chromium.launch(headless=False)
    ctx_args = {
      'ignore_https_errors': True,   # 内网自签证书
      'locale': 'zh-CN',
      'timezone_id': 'Asia/Shanghai',
      'viewport': {'width': 1440, 'height': 900},
    }
    if out.is_file() and not args.fresh:
      ctx_args['storage_state'] = str(out)
      print(f'沿用已有登录态：{out}')
    context = await browser.new_context(**ctx_args)
    page = await context.new_page()
    await page.goto(args.url, wait_until='domcontentloaded', timeout=60000)

    print('=' * 60)
    print(f'请在浏览器窗口完成 {args.system} 登录（{args.url}）')
    print(f'登录成功后回到终端按回车，保存到 {out}')
    print('=' * 60)
    await asyncio.get_event_loop().run_in_executor(None, input, '')

    await context.storage_state(path=str(out))
    await browser.close()

  out.chmod(0o600)
  print(f'已保存登录态：{out}')
  print(f'部署到旁路容器：docker cp {out} rz-browser:/state/{args.system}.json')
  return 0


def main() -> int:
  parser = argparse.ArgumentParser(description='手动登录并保存 storageState')
  parser.add_argument('--system', required=True, help='登录态标识：dcim / mgmt')
  parser.add_argument('--url', required=True, help='登录页地址')
  parser.add_argument(
    '--state-out', default='deploy/browser_state/{system}.json',
    help='storageState 输出路径（默认 deploy/browser_state/{system}.json）',
  )
  parser.add_argument('--fresh', action='store_true', help='忽略已有登录态，从零登录')
  args = parser.parse_args()
  args.state_out = args.state_out.format(system=args.system)
  return asyncio.run(_run(args))


if __name__ == '__main__':
  sys.exit(main())

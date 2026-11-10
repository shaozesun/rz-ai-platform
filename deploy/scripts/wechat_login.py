"""个人微信 iLink 扫码接入：扫码拿 bot_token / ilink_bot_id 并落盘。

用法（在项目根目录，且已 `pip install -r requirements.txt`）：

    python deploy/scripts/wechat_login.py

二维码存 `wechat_qrcode.png`，用微信扫码；成功后凭据写入 `WECHAT_STATE_FILE`
（默认 `data/wechat/auth.json`，0600），**重启后端即自动装载**——无需手填 `.env`。
（仍会打印两行 `.env` 供偏好环境变量的部署。）服务端以 `WECHAT_ENABLED=true` 启用。
token 会过期（日志出现 `errcode -14`）时重新跑本脚本即可。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from service.wechat import login, state  # noqa: E402


async def main() -> int:
  try:
    creds = await login.login()
  except Exception as e:
    print(f'登录失败：{e}')
    return 1
  state.save_auth({
    'bot_token': creds['bot_token'],
    'ilink_bot_id': creds.get('ilink_bot_id', ''),
  })
  print('\n登录成功，凭据已写入 WECHAT_STATE_FILE（重启后端即生效）。')
  print('如需改用 .env，复制下面两行：\n')
  print(f'WECHAT_BOT_TOKEN={creds["bot_token"]}')
  print(f'WECHAT_ILINK_BOT_ID={creds["ilink_bot_id"]}')
  print('\n并设置 WECHAT_ENABLED=true，然后重启后端。')
  return 0


if __name__ == '__main__':
  raise SystemExit(asyncio.run(main()))

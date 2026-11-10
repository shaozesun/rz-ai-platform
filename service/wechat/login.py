"""个人微信 iLink 扫码登录：取二维码 + 轮询扫码状态（拿 `bot_token`/`ilink_bot_id`）。

两个公开函数（均为 **GET、公开头、无鉴权**，对齐 DeerFlow 实测）：
- `request_login_qrcode()` → `{qrcode, qrcode_img_content}`（`qrcode_img_content` 是
  二维码 PNG 的 base64，前端拼 `data:image/png;base64,` 即可显示）
- `request_login_status(qrcode, verify_code=None)` → `{status, bot_token?, ilink_bot_id?, ...}`

供命令行扫码脚本（`deploy/scripts/wechat_login.py`）使用。
token 会过期（`errcode -14`），过期需重扫。
"""

import asyncio
import base64
import logging
from typing import Optional

import httpx

from config.settings import settings
from service.wechat import client

logger = logging.getLogger(__name__)


async def _get_json(path: str, params: Optional[dict] = None, *, timeout: float = 10) -> dict:
  base = settings.WECHAT_BASE_URL.rstrip('/')
  async with httpx.AsyncClient() as c:
    resp = await c.get(
      f'{base}{path}', params=params,
      headers=client.public_headers(), timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


async def request_login_qrcode() -> dict:
  """取登录二维码（公开接口）。"""
  return await _get_json('/ilink/bot/get_bot_qrcode', {'bot_type': settings.WECHAT_QR_BOT_TYPE})


async def request_login_status(qrcode: str, *, verify_code: Optional[str] = None,
                               timeout: float = 35) -> dict:
  """查扫码状态（公开接口）。`verify_code` 仅在 need_verifycode 时回填。"""
  params = {'qrcode': qrcode}
  if verify_code:
    params['verify_code'] = verify_code
  return await _get_json('/ilink/bot/get_qrcode_status', params, timeout=timeout)


async def login() -> dict:
  """命令行兜底：取二维码存 PNG → 轮询到 confirmed → 返回凭据。

  此处仅供运维脚本 `deploy/scripts/wechat_login.py`。
  """
  qr = await request_login_qrcode()
  qrcode = str(qr.get('qrcode') or '').strip()
  content = str(qr.get('qrcode_img_content') or '').strip()
  if not qrcode:
    raise RuntimeError(f'取二维码失败: {qr}')

  if content:
    try:
      with open('wechat_qrcode.png', 'wb') as f:
        f.write(base64.b64decode(content))
      print('二维码已保存到 wechat_qrcode.png，请打开图片用微信扫码。')
    except Exception:
      logger.warning('[WeChat] 二维码保存失败（忽略）')

  interval = max(settings.WECHAT_QR_POLL_INTERVAL, 1)
  deadline = asyncio.get_event_loop().time() + settings.WECHAT_QR_TIMEOUT
  while asyncio.get_event_loop().time() < deadline:
    body = await request_login_status(qrcode, timeout=35)
    status = str(body.get('status') or '').strip().lower()
    if status == 'confirmed':
      return {
        'bot_token': body.get('bot_token', ''),
        'ilink_bot_id': body.get('ilink_bot_id', ''),
      }
    if status in ('expired', 'canceled', 'cancelled', 'invalid', 'failed'):
      raise RuntimeError(f'扫码未完成: {status}')
    await asyncio.sleep(interval)
  raise RuntimeError('扫码超时，请重试')

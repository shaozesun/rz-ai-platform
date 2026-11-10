"""个人微信发送：`sendmessage` 纯文本。

iLink **不能编辑已发消息**，也没有打字态接口，所以 Agent 回复只能整段一次性发出
（累计到终稿再发，见 runner.py）。回复须回带入站消息的 `context_token`
（协议不允许冷启动主动推）。
"""

import logging
import secrets
import time

from service.wechat import client

logger = logging.getLogger(__name__)


async def send_text(to_user_id: str, text: str, context_token: str = '') -> None:
  """发一条纯文本消息。"""
  body = {
    'msg': {
      'from_user_id': '',
      'to_user_id': to_user_id,
      'client_id': f'rz-wechat_{int(time.time() * 1000)}_{secrets.token_hex(2)}',
      'message_type': 2,      # 2 = 机器人消息
      'message_state': 2,     # 2 = 完成态
      'context_token': context_token or '',
      'item_list': [{'type': 1, 'text_item': {'text': text}}],
    },
    'base_info': client.base_info(),
  }
  resp = await client.post('/ilink/bot/sendmessage', body)
  ret = resp.get('ret', 0)
  if ret not in (0, None):
    logger.warning('[WeChat] sendmessage 失败 ret=%s errcode=%s errmsg=%s',
                   ret, resp.get('errcode'), resp.get('errmsg', ''))

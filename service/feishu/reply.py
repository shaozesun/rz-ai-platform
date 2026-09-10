"""飞书卡片回复工具：markdown 卡片 + 绑定输入卡片 + in-place patch。

飞书交互卡片原生渲染 markdown；「正在思考」卡片先发、后续用 patch 原地更新，
避免每 chunk 刷屏。所有 lark API 调用为同步阻塞 HTTP，用 asyncio.to_thread 包装。
"""

import asyncio
import json
import logging
from typing import Optional

from service.feishu.client import get_api_client

logger = logging.getLogger(__name__)


def build_markdown_card(text: str) -> str:
  card = {
    'config': {'wide_screen_mode': True, 'update_multi': True},
    'elements': [{'tag': 'markdown', 'content': text}],
  }
  return json.dumps(card, ensure_ascii=False)


def build_bind_card() -> str:
  """未绑定用户引导卡片：提示发 /connect 命令（纯文字，不依赖卡片回传）。"""
  return build_markdown_card(
    '你尚未绑定系统账号。请在 Web 端「认证中心 → 飞书绑定」生成一次性码，'
    '然后发送 `/connect <一次性码>` 完成绑定。',
  )


def _check(response, action: str) -> None:
  if response is not None and not response.success():
    raise RuntimeError(
      f'飞书 {action} 失败: code={response.code}, msg={response.msg}, log_id={response.get_log_id()}',
    )


async def reply_card(message_id: str, text: str) -> Optional[str]:
  """回复一张 markdown 卡片，返回卡片 message_id（供后续 patch）。"""
  client = get_api_client()
  if not client:
    return None
  from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

  content = build_markdown_card(text)
  request = (
    ReplyMessageRequest.builder()
    .message_id(message_id)
    .request_body(
      ReplyMessageRequestBody.builder().msg_type('interactive').content(content).build(),
    )
    .build()
  )
  response = await asyncio.to_thread(client.im.v1.message.reply, request)
  _check(response, 'reply')
  data = getattr(response, 'data', None)
  return getattr(data, 'message_id', None)


async def reply_bind_card(message_id: str) -> None:
  """回复绑定输入卡片（未绑定用户引导）。"""
  client = get_api_client()
  if not client:
    return
  from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody

  content = build_bind_card()
  request = (
    ReplyMessageRequest.builder()
    .message_id(message_id)
    .request_body(
      ReplyMessageRequestBody.builder().msg_type('interactive').content(content).build(),
    )
    .build()
  )
  response = await asyncio.to_thread(client.im.v1.message.reply, request)
  _check(response, 'reply_bind_card')


async def create_card(chat_id: str, text: str) -> None:
  """在目标会话里新发一张卡片（无 message_id 可回复时用）。"""
  client = get_api_client()
  if not client:
    return
  from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

  content = build_markdown_card(text)
  request = (
    CreateMessageRequest.builder()
    .receive_id_type('chat_id')
    .request_body(
      CreateMessageRequestBody.builder().receive_id(chat_id).msg_type('interactive').content(content).build(),
    )
    .build()
  )
  response = await asyncio.to_thread(client.im.v1.message.create, request)
  _check(response, 'create_card')


async def update_card(message_id: str, text: str) -> None:
  """原地更新一张卡片的内容。"""
  client = get_api_client()
  if not client:
    return
  from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

  content = build_markdown_card(text)
  request = (
    PatchMessageRequest.builder()
    .message_id(message_id)
    .request_body(PatchMessageRequestBody.builder().content(content).build())
    .build()
  )
  response = await asyncio.to_thread(client.im.v1.message.patch, request)
  _check(response, 'update_card')

"""飞书通讯录反查：open_id → 真实姓名。

权限：**`contact:contact.base:readonly`**（不是 `contact:user.base:readonly`——这两个名字
极像，配错了接口会因缺权限查不到姓名。2026-09 实测确认）。
"""

import asyncio
import logging

from service.feishu.client import get_api_client

logger = logging.getLogger(__name__)

_BATCH_SIZE = 50  # contact batch 单次上限


async def resolve_names(open_ids) -> dict[str, str]:
  """按 open_id 批量反查通讯录真实姓名，返回 {open_id: name}；失败/未命中跳过。"""
  ids = list(dict.fromkeys([i for i in (open_ids or []) if i]))  # 去重保序
  if not ids:
    return {}
  client = get_api_client()
  if not client:
    return {}
  from lark_oapi.api.contact.v3 import BatchUserRequest

  result: dict[str, str] = {}
  for start in range(0, len(ids), _BATCH_SIZE):
    chunk = ids[start:start + _BATCH_SIZE]
    request = (
      BatchUserRequest.builder()
      .user_id_type('open_id')
      .user_ids(chunk)
      .build()
    )
    response = await asyncio.to_thread(client.contact.v3.user.batch, request)
    if response is None or not response.success():
      logger.warning(
        '[Feishu] 通讯录反查失败 code=%s msg=%s',
        getattr(response, 'code', '?'), getattr(response, 'msg', ''),
      )
      continue
    items = getattr(getattr(response, 'data', None), 'items', None) or []
    for u in items:
      oid = getattr(u, 'open_id', '') or ''
      name = getattr(u, 'name', '') or ''
      if oid and name:
        result[oid] = name
  return result

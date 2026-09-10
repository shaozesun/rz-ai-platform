"""大列表物化：平台列表工具返回 total>20 时，服务端分页物化为数据集。

与 analysis_load 同逻辑的纯服务端分页（无 LLM），供 tools_factory 在工具返回处
确定性触发；返回 records_url，让模型在其后把数据整理成可读 view.csv 并输出卡。
"""

import logging

from config.settings import settings

logger = logging.getLogger(__name__)

_RECORD_KEYS = ('list', 'records', 'rows', 'items', 'data', 'result')
_TOTAL_KEYS = ('total', 'totalCount', 'count', 'pageCounts')


def extract_records(resp) -> list:
  """从平台响应里抽取记录列表（通用探测，同 analysis_load）。"""
  if isinstance(resp, list):
    return resp
  if isinstance(resp, dict):
    for key in _RECORD_KEYS:
      value = resp.get(key)
      if isinstance(value, list) and value:
        return value
    for value in resp.values():
      if isinstance(value, list) and value:
        return value
  return []


def extract_total(resp) -> int | None:
  """尝试取总数（取不到返回 None）。"""
  if isinstance(resp, dict):
    for key in _TOTAL_KEYS:
      try:
        t = int(resp.get(key))
        if t > 0:
          return t
      except (TypeError, ValueError):
        continue
  return None


async def ensure_list_table(
  thread_id: str,
  user_id: str,
  capability_id: str,
  params: dict | None,
  dataset_name: str = 'data',
) -> dict | None:
  """分页物化全量数据为数据集，返回 {dataset_id,rows,columns,records_url}；失败返回 None。"""
  from core.sandbox.datasets import save_dataset
  from service.query.base.platform import get_caller, get_capability

  caller = get_caller(capability_id)
  if get_capability(capability_id) is None or caller is None:
    return None
  page_size = settings.AGENT_ANALYSIS_PAGE_SIZE
  max_rows = settings.AGENT_ANALYSIS_MAX_ROWS

  records: list[dict] = []
  page = 1
  while True:
    if len(records) >= max_rows:
      break
    try:
      resp = await caller(capability_id, {
        **(params or {}),
        'page': str(page),
        'page_size': str(page_size),
      }, on_behalf_of=user_id)
    except Exception:
      logger.warning('大列表物化分页异常 cap=%s page=%d', capability_id, page, exc_info=True)
      break
    pr = extract_records(resp)
    if not pr:
      break
    records.extend(pr)
    total = extract_total(resp)
    if total is not None:
      if len(records) >= total:
        break
    elif len(pr) < page_size:
      break
    page += 1

  if not records:
    return None

  info = save_dataset(thread_id, capability_id, params or {}, records, dataset_name)
  from urllib.parse import quote
  info['records_url'] = (
    f'/api/v1/chat/agent/artifacts/{quote(thread_id, safe="")}/'
    f'{quote(info["dataset_id"], safe="")}/records'
  )
  info['rows'] = len(records)
  return info

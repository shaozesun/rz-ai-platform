"""取数：部门任务列表、任务详情（含设备与图片地址）、图片下载。

接口（实测 2026-11-06，base URL 就是 `MGMT_BASE_URL` = `http://…/center`）：
- `GET /infrared/inspection/task/list/v1/{deptId}/{page}/{pageSize}`
  → `{count, pageCounts, pageSize, infraredInspectionTaskVOS:[…]}`
- `GET /infrared/inspection/task/detail/v1/{taskId}`
  → `{id, inspector, deptId, …, inspectionDeviceTypes:[{name, inspectionDevices:[…]}]}`

`picture` 是 S3 预签名地址（7 天有效），**下载时不带任何鉴权头**——把 mgmt token
发给对象存储既无必要也是泄露面。
"""

import json
import logging
from urllib.parse import quote

import httpx

from config.redis_conn import redis_manager
from config.settings import settings

logger = logging.getLogger(__name__)

PAGE_SIZE = 50
_TASK_CACHE_PREFIX = 'infrared_quality:tasks:'


def _list_path(dept_id: str, page: int, page_size: int) -> str:
  return (f'/infrared/inspection/task/list/v1/{quote(dept_id, safe="")}'
          f'/{page}/{page_size}')


async def _fetch_tasks(dept_id: str) -> list[dict]:
  from service.query.mgmt import http

  rows: list[dict] = []
  page = 1
  while True:
    data = await http.request('GET', _list_path(dept_id, page, PAGE_SIZE))
    if not isinstance(data, dict):
      raise RuntimeError(f'巡检任务列表返回异常：{type(data).__name__}')
    rows.extend(data.get('infraredInspectionTaskVOS') or [])
    pages = int(data.get('pageCounts') or 1)
    if page >= pages:
      break
    page += 1
  return rows


async def list_tasks(dept_id: str, refresh: bool = False) -> list[dict]:
  """部门下全部巡检任务（翻完所有页）。Redis 缓存，`refresh` 绕过。"""
  key = redis_manager.key(f'{_TASK_CACHE_PREFIX}{dept_id}')
  if not refresh:
    try:
      raw = redis_manager.client.get(key)
      if raw:
        return json.loads(raw)
    except Exception as e:  # noqa: BLE001  缓存坏了就当没有
      logger.debug('红外任务列表缓存读取失败: %s', e)
  rows = await _fetch_tasks(dept_id)
  try:
    redis_manager.client.setex(
      key, settings.INFRARED_QUALITY_TASK_CACHE_TTL,
      json.dumps(rows, ensure_ascii=False),
    )
  except Exception as e:  # noqa: BLE001
    logger.debug('红外任务列表缓存写入失败: %s', e)
  return rows


async def task_detail(task_id: str) -> dict:
  from service.query.mgmt import http

  data = await http.request(
    'GET', f'/infrared/inspection/task/detail/v1/{quote(task_id, safe="")}',
  )
  if not isinstance(data, dict):
    raise RuntimeError(f'巡检任务详情返回异常：{type(data).__name__}')
  return data


def flatten_devices(detail: dict) -> list[dict]:
  """跨 `inspectionDeviceTypes` 展平设备，带设备类型名（行里要展示）。"""
  out: list[dict] = []
  for group in detail.get('inspectionDeviceTypes') or []:
    type_name = str(group.get('name') or '')
    for dev in group.get('inspectionDevices') or []:
      if str(dev.get('delStatus') or '0') not in ('', '0', 'None'):
        continue
      out.append({
        'device_id': str(dev.get('deviceId') or ''),
        'name': str(dev.get('name') or ''),
        'num': str(dev.get('num') or ''),
        'code': str(dev.get('code') or ''),
        'type_name': type_name or str(dev.get('infraredDeviceTypeName') or ''),
        'installation_site': str(dev.get('installationSite') or ''),
        'project': str(dev.get('project') or ''),
        'detection_time': dev.get('detectionTime') or 0,
        'upper_limit': str(dev.get('upperLimitValue') or ''),
        'max_temperature': str(dev.get('maxTemperature') or ''),
        'picture': str(dev.get('picture') or ''),
      })
  return out


async def download_image(url: str, dest: str) -> None:
  """预签名地址直连下载（不带鉴权头）；失败抛 `httpx`/`OSError` 异常。"""
  headers = {}
  if settings.MGMT_BASE_URL and url.startswith(settings.MGMT_BASE_URL):
    from service.query.mgmt import http

    token, dept = await http._get_token_and_dept()
    headers = {'token': token, 'Deptid': dept or ''}
  async with httpx.AsyncClient(
    timeout=settings.INFRARED_QUALITY_DOWNLOAD_TIMEOUT, follow_redirects=True,
  ) as client:
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    if not resp.content:
      raise OSError('图片内容为空')
    with open(dest, 'wb') as fh:
      fh.write(resp.content)

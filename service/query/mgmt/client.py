"""综合管理平台统一调用客户端（真实实现，纯分发层）。

设计要点与 dcim/client.py 对称：
- 对上暴露稳定抽象 `call(capability_id, params, on_behalf_of=None)`。
- 本文件不含任何接口相关的硬编码逻辑——具体接口的 method/path/参数映射/取值
  路径全部声明在 endpoints.py 的 EndpointSpec 里，新增/调整接口只改那边。
- `on_behalf_of` 目前未使用（mgmt 侧长期用单一服务账号，暂无按用户区分身份的
  需求，见项目反馈：不强行做身份透传预留设计，等真有需要再加）。
"""

import logging
import re

from service.query.mgmt import http
from service.query.mgmt.endpoints import ENDPOINTS, EndpointSpec

logger = logging.getLogger(__name__)

_PATH_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')


class MgmtError(Exception):
  """综合管理平台调用异常。"""


def _build_request(spec: EndpointSpec, params: dict) -> tuple[str, dict, str]:
  """按 EndpointSpec 把 Capability 参数拆成 (真实 path, 剩余参数字典, header dept_id)。

  path 占位符（如 `{dept_id}`）直接从 params 里取同名字段并从剩余参数中剔除，
  避免路径参数被重复当成 query/body 参数发送。剩余参数再按 param_map 把
  Capability 参数名换成真实接口字段名，未在 param_map 里声明的参数名原样传递。

  第三个返回值是用作 `Deptid` header 的部门 id：优先取 path 占位符里的
  `dept_id`（如 query_device_list），否则取业务筛选参数 `department`（映射为
  query/body 里的 `deptId`，**注意这个值不会从 mapped 里剔除**——deptId 既是
  很多接口的必填查询参数，又要同时作为 Deptid header，两处都要有值）。
  """
  remaining = dict(params)
  path = spec.path
  header_dept_id = ''
  for placeholder in _PATH_PLACEHOLDER_RE.findall(spec.path):
    value = remaining.pop(placeholder, '')
    path = path.replace(f'{{{placeholder}}}', str(value))
    if placeholder == 'dept_id':
      header_dept_id = str(value)

  if not header_dept_id:
    header_dept_id = str(remaining.get('department', '') or remaining.get('dept_id', ''))

  mapped = {spec.param_map.get(k, k): v for k, v in remaining.items()}
  return path, mapped, header_dept_id


def _extract_result(data: dict, result_path: str) -> dict:
  """按 result_path 从响应 data 里摘取字段；空 result_path 原样返回 data。"""
  if not result_path:
    return data
  if not isinstance(data, dict):
    return data
  return {result_path: data.get(result_path)}


async def call(
  capability_id: str,
  params: dict | None = None,
  *,
  on_behalf_of: str | None = None,
) -> dict:
  """执行一次综合管理平台能力调用。

  Args:
    capability_id: 能力 id，如 'mgmt.query_person'。
    params: 调用参数，键对应 Capability.params（snake_case）。
    on_behalf_of: 目前未使用，见模块 docstring。

  Returns:
    结构化 dict 结果。

  Raises:
    MgmtError: capability_id 未注册端点规格，或调用失败。
  """
  params = params or {}
  spec = ENDPOINTS.get(capability_id)
  if spec is None:
    raise MgmtError(f'未知能力 {capability_id}（endpoints.py 未声明其 EndpointSpec）')

  logger.info('mgmt.call id=%s params=%s', capability_id, params)

  path, mapped_params, dept_id = _build_request(spec, params)
  # dept_id 为空时 http.request 会用登录账号默认部门兜底（跨部门查询场景下
  # 调用方传了 department 参数才会有值）

  try:
    if spec.param_location == 'json':
      data = await http.request(spec.method, path, json_body=mapped_params, dept_id=dept_id)
    else:
      data = await http.request(spec.method, path, params=mapped_params, dept_id=dept_id)
  except http.MgmtError as e:
    raise MgmtError(str(e)) from e

  return _extract_result(data, spec.result_path)

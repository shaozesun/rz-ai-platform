"""综合管理平台统一调用客户端（真实实现，纯分发层）。

设计要点与 dcim/client.py 对称：
- 对上暴露稳定抽象 `call(capability_id, params, on_behalf_of=None)`。
- 单接口能力：本文件不含任何接口相关的硬编码逻辑——method/path/参数映射/取值
  路径全部声明在 endpoints.py 的 EndpointSpec 里，新增/调整接口只改那边。
- 组合能力例外：`mgmt.query_pending_ticket`（查人→换 token→调 pen 三步）、
  `mgmt.query_change_ticket` / `mgmt.query_breakdown_ticket` / `mgmt.query_question_ticket`
  （公司维度 POST 变更/故障/问题工单列表）、
  `mgmt.query_breakdown_ticket_detail` / `mgmt.query_question_ticket_detail` /
  `mgmt.query_event_ticket_detail` / `mgmt.query_change_ticket_detail`（按 id 查
  故障/问题/事件/变更工单详情）无 EndpointSpec，在 `call()` 里特判分别走
  `_query_pending_ticket` / `_query_change_ticket` / `_query_breakdown_ticket` /
  `_query_question_ticket` / `_query_breakdown_ticket_detail` /
  `_query_question_ticket_detail` / `_query_event_ticket_detail` /
  `_query_change_ticket_detail`（仅此八处硬编码）。
- `on_behalf_of` 为认证中心身份透传：单接口路径按其 user_id 用该用户自己的
  mgmt token 查询，8 个 pen 组合能力也用该用户身份换发 token/公司部门上下文，
  实现 mgmt/pen 侧数据权限按人生效（user_id 为空才回退服务账号）。
"""

import logging
import re
from datetime import datetime
from urllib.parse import quote

from service.query.mgmt import http
from service.query.mgmt.endpoints import ENDPOINTS, EndpointSpec

logger = logging.getLogger(__name__)

_PATH_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')

_DATETIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d')


def _parse_datetime(value: str) -> datetime | None:
  """把可读日期串解析为 naive datetime（本地时区）；不可解析返回 None。

  综合管理平台时间戳为毫秒 long，且毫秒值对应本地时间零点（实测
  1782835200000 = 2026-07-01 00:00 本地），故用 naive timestamp 换算。
  """
  for fmt in _DATETIME_FORMATS:
    try:
      return datetime.strptime(value, fmt)
    except ValueError:
      continue
  return None


def _time_to_ms(value: str, *, end_of_day: bool = False) -> int | None:
  """日期串 → 毫秒。end_of_day=True 且只给了 date（YYYY-MM-DD）时，按当天 23:59:59.999 解释，
  用于结束日期（避免把“9-08”当成 9-08 00:00 而漏掉当天记录）。"""
  dt = _parse_datetime(value)
  if dt is None:
    return None
  if end_of_day and len(str(value).strip()) <= 10:
    dt = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
  return int(dt.timestamp() * 1000)


def _quarter_range(year: int, quarter: int) -> tuple[str, str]:
  """按年份与季度索引（1-4）返回对齐的 (start_time, end_time) 字符串。

  实测：query_person_training 的 month 是**季度**索引，且 startTime/endTime 必须
  对齐该季度范围（传宽范围与 month 一起会报「获取用户培训信息失败」）。范围与
  平台前端一致：Q1=01-01~04-01 / Q2=04-01~07-01 / Q3=07-01~10-01 / Q4=10-01~次年
  01-01（Q4 跨年）。
  """
  start_month = (quarter - 1) * 3 + 1  # 1,4,7,10
  end_month = start_month + 3          # 4,7,10,13
  end_year = year + 1 if end_month > 12 else year
  end_month = ((end_month - 1) % 12) + 1
  return (
    f'{year}-{start_month:02d}-01 00:00:00',
    f'{end_year}-{end_month:02d}-01 00:00:00',
  )


class MgmtError(Exception):
  """综合管理平台调用异常。"""


def _build_request(spec: EndpointSpec, params: dict) -> tuple[str, dict, str]:
  """按 EndpointSpec 把 Capability 参数拆成 (真实 path, 剩余参数字典, header dept_id)。

  path 占位符（如 `{name}`）直接从 params 里取同名字段并从剩余参数中剔除，
  避免路径参数被重复当成 query/body 参数发送。剩余参数再按 param_map 把
  Capability 参数名换成真实接口字段名，未在 param_map 里声明的参数名原样传递。

  第三个返回值是用作 `Deptid` header 的部门 id：优先取 path 占位符里的
  `dept_id`，否则取 `dept_id` 参数（经 param_map 映射为 query/body 里的
  `deptId`）。注意 `dept_id` 不会从 mapped 里剔除——deptId 既是很多接口的
  必填查询参数，又要同时作为 Deptid header，两处都要有值。当前保留的接口
  路径均无 dept_id 占位符，此项主要依赖 dept_id 参数取值。
  """
  remaining = dict(params)
  path = spec.path
  header_dept_id = ''
  for placeholder in _PATH_PLACEHOLDER_RE.findall(spec.path):
    value = remaining.pop(placeholder, None)
    if value is None:
      # 占位符是接口 camelCase（如 {deptName}），Capability 参数是 snake_case
      # （dept_name）：从 param_map 反查拿到源参数名再取值（如 query_dept_by_name）。
      src_key = next((k for k, v in spec.param_map.items() if v == placeholder), None)
      value = remaining.pop(src_key, '') if src_key else ''
    # 占位符值做 URL 编码：query_person_by_name 的 name 是中文（如「郭春磊」），不编码
    # 会带进路径导致请求非法；alphanumeric 的 dept_id 编码后不变，无副作用。
    path = path.replace(f'{{{placeholder}}}', quote(str(value), safe=''))
    if placeholder == 'dept_id':
      header_dept_id = str(value)

  if not header_dept_id:
    header_dept_id = str(remaining.get('dept_id', ''))

  mapped = {spec.param_map.get(k, k): v for k, v in remaining.items()}
  return path, mapped, header_dept_id


def _extract_result(data: dict, result_path: str, page_meta: tuple[str, ...] = ()) -> dict:
  """按 result_path 从响应 data 里摘取字段；空 result_path 原样返回 data。

  分页接口（如 query_employee_ticket）摘取列表后，page_meta 声明的响应字段
  （count/pageSize/pageCounts）会一并并入返回结果——只摘列表会让 LLM 拿不到
  总条数，无法告知用户「共 N 条、当前展示前 20 条」。字段在 data 里不存在时
  静默忽略。
  """
  if not result_path:
    return data
  if not isinstance(data, dict):
    return data
  result = {result_path: data.get(result_path)}
  for field_name in page_meta:
    if field_name in data:
      result[field_name] = data[field_name]
  return result


async def _query_change_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的变更工单列表（组合能力：公司维度 POST）。

  与待办不同——变更工单是**公司维度**（实测同 body 换不同人 token 结果相同，
  token 只鉴权），故不加 name。默认查当前用户所属公司（kingdeeCompanyId）与部门
  （ownDeptId，user_id 为空回退服务账号）；company_id 传了则按 mgmt 内部 id 映射
  到 pen 的 kingdee 格式（跨公司，按当前账号可见范围）。
  """
  # companyId：pen 只认 kingdee 格式（sA8...，内部格式返回 0），跨公司需映射
  company_id = params.get('company_id')
  if company_id:
    # 接口 companyId 只认 pen kingdeeCompanyId：收到的 company_id 可能是 mgmt 内部
    # companyId / 公司名 / pen kingdeeCompanyId → 都转成 kingdeeCompanyId 再发；
    # 映射表查不到（已是 kingdeeCompanyId 等）则原样透传，避免把内部 id 漏给接口。
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id), auth_user_id=user_id)
    if not kingdee_company:
      kingdee_company = str(company_id)
  else:
    kingdee_company = (
      await http.get_user_pen_company_id(user_id) if user_id
      else await http.get_service_pen_company_id()
    )
    if not kingdee_company:
      raise MgmtError('当前账号 kingdeeCompanyId 未取到，无法查变更工单')

  body = {
    'isPubilc': 0,
    'type': params.get('type', 1),        # 1=全部(默认)/2=我发起的/4=我处理的/5=我验收的/6=我监督的
    'secondType': 1,                       # 用户确认固定传 1（用途未知）
    'companyId': kingdee_company,
    'page': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  # dc：显式传了用它；否则默认当前部门 ownDeptId；跨公司且没传 → 省略（查全公司，
  # 避免把当前公司的部门带到别的公司）
  dc = params.get('dc')
  if dc:
    body['dc'] = dc
  elif not company_id:
    context = (
      await http.get_user_context(user_id) if user_id
      else await http.get_default_context()
    )
    if context.get('own_dept_id'):
      body['dc'] = context['own_dept_id']

  for src, dst in (
    ('ticket_no', 'ticketNo'), ('project_name', 'projectName'), ('key_word', 'keyWord'),
    ('ticket_type', 'ticketType'), ('specialty', 'specialty'), ('modify_type', 'modifyType'),
    ('stage', 'stage'), ('only_time_out', 'onlyTimeOut'),
  ):
    value = params.get(src)
    if value is not None and value != '':
      body[dst] = value
  # start_time/end_time 可读日期串 → 毫秒（pen 时间戳为 ms long）。注意：
  # 列表查询 DTO 只有 17 个字段（size/projectName/ticketType/endTime/keyWord/
  # ticketNo/secondType/startTime/modifyType/onlyTimeOut/companyId/type/specialty/
  # dc/isPubilc/page/stage），**不含 breakdownLevel**——它是记录里的展示字段，
  # 不是查询过滤（实测传入报 Unrecognized field）。
  for src, dst in (('start_time', 'startTime'), ('end_time', 'endTime')):
    value = params.get(src)
    if isinstance(value, str) and value.strip():
      ms = _time_to_ms(value, end_of_day=(dst == 'endTime'))
      if ms is not None:
        body[dst] = ms
  # 只给了开始没给结束（如“9.1 至今”）→ 接口会把时间当全量历史返回，需默认补 end=当前
  if 'startTime' in body and 'endTime' not in body:
    body['endTime'] = int(datetime.now().timestamp() * 1000)

  data = await http.pen_request(
    '/ticket/wiporder/ticketlist/modify', json_body=body, auth_user_id=user_id,
  )
  # 信封 {simpleTicketVOList,total,page,size,approveCount,...}：列表键不在 analysis_load
  # _RECORD_KEYS，显式归一化成 records（保留 total 与各阶段计数）
  if isinstance(data, dict) and 'simpleTicketVOList' in data:
    data['records'] = data.pop('simpleTicketVOList')
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _attach_scope(data, user_id)


async def _query_breakdown_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的故障工单列表（组合能力：公司维度 POST）。

  同 _query_change_ticket——故障工单列表也是公司维度（token 只鉴权不区分人），
  默认查当前用户所属公司/部门（user_id 为空回退服务账号）；company_id 传了则
  跨公司映射。与变更列表的区别仅：端点换 /ticket/wiporder/ticketlist/breakdown，
  筛选项把 specialty/modifyType 换成 breakDownLevel（故障级别）。
  """
  company_id = params.get('company_id')
  if company_id:
    # 接口 companyId 只认 pen kingdeeCompanyId：收到的 company_id 可能是 mgmt 内部
    # companyId / 公司名 / pen kingdeeCompanyId → 都转成 kingdeeCompanyId 再发；
    # 映射表查不到（已是 kingdeeCompanyId 等）则原样透传，避免把内部 id 漏给接口。
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id), auth_user_id=user_id)
    if not kingdee_company:
      kingdee_company = str(company_id)
  else:
    kingdee_company = (
      await http.get_user_pen_company_id(user_id) if user_id
      else await http.get_service_pen_company_id()
    )
    if not kingdee_company:
      raise MgmtError('当前账号 kingdeeCompanyId 未取到，无法查故障工单')

  body = {
    'isPubilc': 0,
    'type': params.get('type', 1),        # 查询范围，与变更列表一致
    'secondType': 1,
    'companyId': kingdee_company,
    'page': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  dc = params.get('dc')
  if dc:
    body['dc'] = dc
  elif not company_id:
    context = (
      await http.get_user_context(user_id) if user_id
      else await http.get_default_context()
    )
    if context.get('own_dept_id'):
      body['dc'] = context['own_dept_id']

  for src, dst in (
    ('ticket_no', 'ticketNo'), ('project_name', 'projectName'), ('key_word', 'keyWord'),
    ('ticket_type', 'ticketType'), ('break_down_level', 'breakDownLevel'),
    ('stage', 'stage'), ('only_time_out', 'onlyTimeOut'),
  ):
    value = params.get(src)
    if value is not None and value != '':
      body[dst] = value
  for src, dst in (('start_time', 'startTime'), ('end_time', 'endTime')):
    value = params.get(src)
    if isinstance(value, str) and value.strip():
      ms = _time_to_ms(value, end_of_day=(dst == 'endTime'))
      if ms is not None:
        body[dst] = ms
  # 只给了开始没给结束（如“9.1 至今”）→ 接口会把时间当全量历史返回，需默认补 end=当前
  if 'startTime' in body and 'endTime' not in body:
    body['endTime'] = int(datetime.now().timestamp() * 1000)

  data = await http.pen_request(
    '/ticket/wiporder/ticketlist/breakdown', json_body=body, auth_user_id=user_id,
  )
  if isinstance(data, dict) and 'simpleTicketVOList' in data:
    data['records'] = data.pop('simpleTicketVOList')
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _attach_scope(data, user_id)


async def _query_question_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的问题工单列表（组合能力：公司维度 POST）。

  同 _query_change_ticket——问题工单列表也是公司维度，默认查当前用户所属公司/部门
  （user_id 为空回退服务账号）；company_id 传了则跨公司映射。与变更列表的区别：
  端点换 /ticket/wiporder/ticketlist/question，筛选项换成 severityLevel（问题级别）
  + specialty（专业），无 ticketType/modifyType/onlyTimeOut。
  """
  company_id = params.get('company_id')
  if company_id:
    # 接口 companyId 只认 pen kingdeeCompanyId：收到的 company_id 可能是 mgmt 内部
    # companyId / 公司名 / pen kingdeeCompanyId → 都转成 kingdeeCompanyId 再发；
    # 映射表查不到（已是 kingdeeCompanyId 等）则原样透传，避免把内部 id 漏给接口。
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id), auth_user_id=user_id)
    if not kingdee_company:
      kingdee_company = str(company_id)
  else:
    kingdee_company = (
      await http.get_user_pen_company_id(user_id) if user_id
      else await http.get_service_pen_company_id()
    )
    if not kingdee_company:
      raise MgmtError('当前账号 kingdeeCompanyId 未取到，无法查问题工单')

  body = {
    'isPubilc': 0,
    'type': params.get('type', 1),        # 查询范围，与变更列表一致
    'secondType': 1,
    'companyId': kingdee_company,
    'page': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  dc = params.get('dc')
  if dc:
    body['dc'] = dc
  elif not company_id:
    context = (
      await http.get_user_context(user_id) if user_id
      else await http.get_default_context()
    )
    if context.get('own_dept_id'):
      body['dc'] = context['own_dept_id']

  for src, dst in (
    ('ticket_no', 'ticketNo'), ('project_name', 'projectName'), ('key_word', 'keyWord'),
    ('severity_level', 'severityLevel'), ('specialty', 'specialty'),
    ('stage', 'stage'),
  ):
    value = params.get(src)
    if value is not None and value != '':
      body[dst] = value
  for src, dst in (('start_time', 'startTime'), ('end_time', 'endTime')):
    value = params.get(src)
    if isinstance(value, str) and value.strip():
      ms = _time_to_ms(value, end_of_day=(dst == 'endTime'))
      if ms is not None:
        body[dst] = ms
  # 只给了开始没给结束（如“9.1 至今”）→ 接口会把时间当全量历史返回，需默认补 end=当前
  if 'startTime' in body and 'endTime' not in body:
    body['endTime'] = int(datetime.now().timestamp() * 1000)

  data = await http.pen_request(
    '/ticket/wiporder/ticketlist/question', json_body=body, auth_user_id=user_id,
  )
  if isinstance(data, dict) and 'simpleTicketVOList' in data:
    data['records'] = data.pop('simpleTicketVOList')
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _attach_scope(data, user_id)


async def _query_threshold_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的阈值工单列表（组合能力：公司维度 POST）。

  同 _query_question_ticket——阈值工单也是公司维度，默认查当前用户所属公司/部门
  （user_id 为空回退服务账号）；company_id 传了则跨公司映射。与问题工单列表的区别：
  端点换 /ticket/wiporder/ticketlist/threshold，无 severityLevel，specialty 专业枚举
  多 8=其他（1=供配电/2=制冷/4=智能化/8=其他）。
  """
  company_id = params.get('company_id')
  if company_id:
    # 接口 companyId 只认 pen kingdeeCompanyId：收到的 company_id 可能是 mgmt 内部
    # companyId / 公司名 / pen kingdeeCompanyId → 都转成 kingdeeCompanyId 再发；
    # 映射表查不到（已是 kingdeeCompanyId 等）则原样透传，避免把内部 id 漏给接口。
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id), auth_user_id=user_id)
    if not kingdee_company:
      kingdee_company = str(company_id)
  else:
    kingdee_company = (
      await http.get_user_pen_company_id(user_id) if user_id
      else await http.get_service_pen_company_id()
    )
    if not kingdee_company:
      raise MgmtError('当前账号 kingdeeCompanyId 未取到，无法查阈值工单')

  body = {
    'type': params.get('type', 1),        # 查询范围，与其它工单列表一致
    'secondType': 1,
    'companyId': kingdee_company,
    'page': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  dc = params.get('dc')
  if dc:
    body['dc'] = dc
  elif not company_id:
    context = (
      await http.get_user_context(user_id) if user_id
      else await http.get_default_context()
    )
    if context.get('own_dept_id'):
      body['dc'] = context['own_dept_id']

  for src, dst in (
    ('ticket_no', 'ticketNo'), ('project_name', 'projectName'), ('key_word', 'keyWord'),
    ('specialty', 'specialty'), ('stage', 'stage'),
  ):
    value = params.get(src)
    if value is not None and value != '':
      body[dst] = value
  for src, dst in (('start_time', 'startTime'), ('end_time', 'endTime')):
    value = params.get(src)
    if isinstance(value, str) and value.strip():
      ms = _time_to_ms(value, end_of_day=(dst == 'endTime'))
      if ms is not None:
        body[dst] = ms
  # 只给了开始没给结束（如“9.1 至今”）→ 接口会把时间当全量历史返回，需默认补 end=当前
  if 'startTime' in body and 'endTime' not in body:
    body['endTime'] = int(datetime.now().timestamp() * 1000)

  data = await http.pen_request(
    '/ticket/wiporder/ticketlist/threshold', json_body=body, auth_user_id=user_id,
  )
  if isinstance(data, dict) and 'simpleTicketVOList' in data:
    data['records'] = data.pop('simpleTicketVOList')
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _attach_scope(data, user_id)


async def _query_supplier_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的供应商服务工单列表（组合能力：公司维度 POST）。

  同 _query_threshold_ticket——供应商服务工单也是公司维度，默认查当前用户所属公司/部门
  （user_id 为空回退服务账号）；company_id 传了则跨公司映射。与阈值工单列表的区别：
  端点换 /ticket/wiporder/ticketlist/supplier，筛选项换成 serviceType（服务类型），
  无 specialty/startTime/endTime。
  """
  company_id = params.get('company_id')
  if company_id:
    # 接口 companyId 只认 pen kingdeeCompanyId：收到的 company_id 可能是 mgmt 内部
    # companyId / 公司名 / pen kingdeeCompanyId → 都转成 kingdeeCompanyId 再发；
    # 映射表查不到（已是 kingdeeCompanyId 等）则原样透传，避免把内部 id 漏给接口。
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id), auth_user_id=user_id)
    if not kingdee_company:
      kingdee_company = str(company_id)
  else:
    kingdee_company = (
      await http.get_user_pen_company_id(user_id) if user_id
      else await http.get_service_pen_company_id()
    )
    if not kingdee_company:
      raise MgmtError('当前账号 kingdeeCompanyId 未取到，无法查供应商服务工单')

  body = {
    'type': params.get('type', 1),        # 查询范围，与其它工单列表一致
    'secondType': 1,
    'companyId': kingdee_company,
    'page': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  dc = params.get('dc')
  if dc:
    body['dc'] = dc
  elif not company_id:
    context = (
      await http.get_user_context(user_id) if user_id
      else await http.get_default_context()
    )
    if context.get('own_dept_id'):
      body['dc'] = context['own_dept_id']

  for src, dst in (
    ('ticket_no', 'ticketNo'), ('project_name', 'projectName'), ('key_word', 'keyWord'),
    ('service_type', 'serviceType'), ('stage', 'stage'),
  ):
    value = params.get(src)
    if value is not None and value != '':
      body[dst] = value

  data = await http.pen_request(
    '/ticket/wiporder/ticketlist/supplier', json_body=body, auth_user_id=user_id,
  )
  if isinstance(data, dict) and 'simpleTicketVOList' in data:
    data['records'] = data.pop('simpleTicketVOList')
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _attach_scope(data, user_id)


async def _query_event_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的事件工单列表（组合能力：公司维度 POST）。

  与问题工单同构（DTO 共用 SelectTicketListDTOEventAndQuestion，支持同样筛选字段）：
  公司/部门维度，默认查当前用户所属公司/部门（user_id 为空回退服务账号）；
  company_id 传了则跨公司映射。端点换 /ticket/wiporder/ticketlist/event；
  筛选项 severityLevel（事件级别）+ specialty（专业）+ onlyTimeOut，与问题列表一致。
  """
  company_id = params.get('company_id')
  if company_id:
    # 接口 companyId 只认 pen kingdeeCompanyId：收到的 company_id 可能是 mgmt 内部
    # companyId / 公司名 / pen kingdeeCompanyId → 都转成 kingdeeCompanyId 再发；
    # 映射表查不到（已是 kingdeeCompanyId 等）则原样透传，避免把内部 id 漏给接口。
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id), auth_user_id=user_id)
    if not kingdee_company:
      kingdee_company = str(company_id)
  else:
    kingdee_company = (
      await http.get_user_pen_company_id(user_id) if user_id
      else await http.get_service_pen_company_id()
    )
    if not kingdee_company:
      raise MgmtError('当前账号 kingdeeCompanyId 未取到，无法查事件工单')

  body = {
    'isPubilc': 0,
    'type': params.get('type', 1),        # 查询范围，与其它工单列表一致
    'secondType': 1,
    'companyId': kingdee_company,
    'page': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  dc = params.get('dc')
  if dc:
    body['dc'] = dc
  elif not company_id:
    context = (
      await http.get_user_context(user_id) if user_id
      else await http.get_default_context()
    )
    if context.get('own_dept_id'):
      body['dc'] = context['own_dept_id']

  for src, dst in (
    ('ticket_no', 'ticketNo'), ('project_name', 'projectName'), ('key_word', 'keyWord'),
    ('severity_level', 'severityLevel'), ('specialty', 'specialty'),
    ('stage', 'stage'), ('only_time_out', 'onlyTimeOut'),
  ):
    value = params.get(src)
    if value is not None and value != '':
      body[dst] = value
  for src, dst in (('start_time', 'startTime'), ('end_time', 'endTime')):
    value = params.get(src)
    if isinstance(value, str) and value.strip():
      ms = _time_to_ms(value, end_of_day=(dst == 'endTime'))
      if ms is not None:
        body[dst] = ms
  # 只给了开始没给结束（如“9.1 至今”）→ 接口会把时间当全量历史返回，需默认补 end=当前
  if 'startTime' in body and 'endTime' not in body:
    body['endTime'] = int(datetime.now().timestamp() * 1000)

  data = await http.pen_request(
    '/ticket/wiporder/ticketlist/event', json_body=body, auth_user_id=user_id,
  )
  if isinstance(data, dict) and 'simpleTicketVOList' in data:
    data['records'] = data.pop('simpleTicketVOList')
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _attach_scope(data, user_id)


async def _query_pen_company_dept(user_id: str | None = None) -> dict:
  """查询当前用户 pen 流程服务可见的公司及部门（组合能力：取 kingdeeUid 换 token 调 pen）。

  返回各公司 {companyId 内部, kingdeeCompanyId, companyName, deptList:[{deptId,deptName}]}，
  用于解析「XX 数据中心/机房」→ 工单列表查询要用的 dc 部门 id 与公司 id（pen 的 dc 即
  mgmt 部门 id，与 query_dept_by_name 返回一致，但本接口按公司分组，能区分跨公司的同名部门）。
  """
  if not user_id:
    raise MgmtError('无法确定当前用户身份（未走 Agent 认证通道）')

  # 静态参考数据缓存：公司/部门结构变化少，TTL 1h
  cache_key = http.ref_cache_key('pen_company_dept', user_id)
  cached = http.cache_get_json(cache_key)
  if cached is not None:
    return cached

  kingdee_uid = await http.get_user_kingdee_uid(user_id)
  if not kingdee_uid:
    raise MgmtError('当前用户 kingdeeUid 未取到，无法查询公司/部门')
  data = await http.pen_request(
    f'/ticket/company/user/{kingdee_uid}', auth_user_id=user_id,
  )
  http.cache_set_json(cache_key, data, 3600)
  return data


async def _list_pen_companies(user_id: str | None = None) -> list[dict]:
  """当前账号可见的 pen 公司列表（[{companyId 内部, companyName, kingdee}]）。

  供「查全部公司工单」逐公司聚合用：一次空 companyId 调用实测返回 0（不可靠），
  必须按公司逐个查。
  """
  data = await http.pen_request(
    '/pen/pending/list/company', kingdee_uid=None, auth_user_id=user_id,
  )
  companies = []
  for c in data or []:
    kid = c.get('kingdee') or ''
    if kid:
      companies.append({
        'companyId': c.get('companyId', ''),
        'companyName': c.get('companyName', ''),
        'kingdee': kid,
      })
  return companies


async def _attach_scope(data: dict, user_id: str | None = None) -> dict:
  """在结果里附上查询范围（具体公司/部门名），供 LLM 回复时说明（避免写『当前登录用户所属公司/部门』占位）。"""
  try:
    profile = await http.get_user_profile(user_id) if user_id else {}
  except Exception:
    profile = {}
  company = profile.get('company_name', '')
  dept = profile.get('own_dept_name', '') or profile.get('dept_name', '')
  if (company or dept) and isinstance(data, dict):
    scope = {}
    if company:
      scope['company'] = company
    if dept:
      scope['dept'] = dept
    data['_query_scope'] = scope
  return data


async def _query_pending_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询流程平台「我的工单」（仅当前账号本人，用本人 token 查自己的工单）。

  kingdee_uid 留空，pen_request 按 auth_user_id 解析用户本人的 kingdeeUid 并换发
  本人 token；不支持按姓名查他人/换他人 token（user_id 为空回退服务账号本人）。

  company_id 不传 = 查全部公司：工具内部拉公司列表，**逐公司调用合并**（一次空
  companyId 调用实测返回 0，不可靠）；每条记录回填 companyName 便于区分。
  """
  base = {
    'isDone': params.get('is_done', 1),   # 1=待办(默认)/2=已办/3=已发起/4=抄送给我
    'deviceType': 1,                       # 实测 URL 固定携带的客户端类型参数
    'current': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  if params.get('type') is not None:       # 工单类型：1=变更/4=问题/5=故障/6=事件/7=阈值（不传返回全部）
    base['type'] = params['type']

  # 单个公司：兼容 mgmt 内部 id（映射 pen kingdee）或 pen 公司列表 id（原样透传）
  if params.get('company_id'):
    query = dict(base)
    try:
      pen_company = await http.get_pen_company_kingdee_id(str(params['company_id']), auth_user_id=user_id)
    except http.MgmtError:
      pen_company = ''
    query['companyId'] = pen_company or str(params['company_id'])
    return await http.pen_request(
      '/pen/pending/list/v1', query, kingdee_uid=None, auth_user_id=user_id,
    )

  # 查全部公司：逐公司调用合并
  companies = await _list_pen_companies(user_id)
  all_records: list = []
  total = 0
  for comp in companies:
    query = {**base, 'companyId': comp['kingdee']}
    data = await http.pen_request(
      '/pen/pending/list/v1', query, kingdee_uid=None, auth_user_id=user_id,
    )
    records = data.get('records') or []
    for r in records:
      r['companyName'] = comp.get('companyName') or r.get('companyName') or ''
    all_records.extend(records)
    total += data.get('total', len(records))
  return {
    'records': all_records,
    'total': total,
    'current': base['current'],
    'size': base['size'],
  }


async def _query_breakdown_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张故障工单的完整详情（组合能力：按待办列表 id 查 pen）。

  详情端点 `GET /ticket/wiporder/query/breakdown/{id}` 实测不按 token 归属人区分
  （自 token 与别人 token 查到同一张单，仅按钮/审计等视图字段不同），仍用当前用户
  身份（auth_user_id），user_id 为空回退服务账号。id 来自 query_pending_ticket
  待办列表返回的 id 字段（用户确认「里面返回的 id 就是工单 id」）。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询故障工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.pen_request(
    f'/ticket/wiporder/query/breakdown/{ticket_id}', auth_user_id=user_id,
  )


async def _query_question_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张问题工单的完整详情（组合能力：按待办列表 id 查 pen）。

  同 _query_breakdown_ticket_detail：详情端点 `/ticket/wiporder/question/{id}` 不按
  token 归属人区分（实测自 token 与别人 token 查到同一张单），用当前用户身份。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询问题工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.pen_request(
    f'/ticket/wiporder/question/{ticket_id}', auth_user_id=user_id,
  )


async def _query_event_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张事件工单的完整详情（组合能力：按待办列表 id 查 pen）。

  同 _query_breakdown_ticket_detail：详情端点 `/ticket/wiporder/event/{id}` 不按
  token 归属人区分（与故障/问题详情同结构同认证），用当前用户身份。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询事件工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.pen_request(
    f'/ticket/wiporder/event/{ticket_id}', auth_user_id=user_id,
  )


async def _query_change_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张变更工单的完整详情（组合能力：按变更工单列表 ticketId 查 pen）。

  实测确认：与 _query_breakdown_ticket_detail 等三个详情接口同构，不按 token
  归属人区分（服务账号本人 token 查到了他人「施光楠」发起的工单完整详情），用
  当前用户身份。id 来自 query_change_ticket 列表返回的 **ticketId** 字段（注意
  不是 `id`——变更工单列表信封字段名与故障/问题/事件的待办列表不同）。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询变更工单详情缺 ticket_id（从 query_change_ticket 列表返回的 ticketId 字段获取）')
  return await http.pen_request(
    f'/ticket/wiporder/query/modify/{ticket_id}', auth_user_id=user_id,
  )


async def _query_supplier_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张供应商服务工单的完整详情（组合能力：按供应商工单列表 ticketId 查 pen）。

  同其它工单详情：详情端点 GET /ticket/wiporder/supplier/{id} 不按 token 归属人区分，
  用当前用户身份。id 来自 query_supplier_ticket 列表返回的 ticketId 字段。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询供应商服务工单详情缺 ticket_id（从 query_supplier_ticket 列表返回的 ticketId 字段获取）')
  return await http.pen_request(
    f'/ticket/wiporder/supplier/{ticket_id}', auth_user_id=user_id,
  )


async def _query_threshold_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张阈值工单的完整详情（组合能力：按阈值工单列表 ticketId 查 pen）。

  同其它工单详情：详情端点 GET /ticket/wiporder/threshold/{id} 不按 token 归属人区分，
  用当前用户身份。id 来自 query_threshold_ticket 列表返回的 ticketId 字段。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询阈值工单详情缺 ticket_id（从 query_threshold_ticket 列表返回的 ticketId 字段获取）')
  return await http.pen_request(
    f'/ticket/wiporder/threshold/{ticket_id}', auth_user_id=user_id,
  )


async def _query_my_info(user_id: str | None = None) -> dict:
  """查询当前登录用户自己的综合管理平台账号信息（组合能力：登录档案）。

  依赖认证中心：user_id 为空（非用户通道）无法确定「我」，抛错提示。
  """
  if not user_id:
    raise MgmtError('无法确定当前用户身份（未走 Agent 认证通道）')
  profile = await http.get_user_profile(user_id)
  if not profile:
    raise MgmtError('当前用户综合管理平台账号信息未取到，请前往【认证中心】确认账号绑定')
  return profile


# 静态参考数据缓存能力（公司/部门列表，结构变化少，TTL 1h）
_STATIC_REF_CACHE = {'mgmt.query_company_list', 'mgmt.query_dept_list'}


async def _exec_single(
  spec, capability_id: str, params: dict, on_behalf_of: str | None = None,
) -> dict:
  """执行单个 EndpointSpec 接口：构建请求 → defaults/value_map/default_params 兜底 → http.request。

  从 call 里抽出，便于对静态参考数据（公司/部门列表）包 Redis 缓存。
  """
  path, mapped_params, dept_id = _build_request(spec, params)
  # 无分页接口（page_style='none'）不接受分页参数（实测 Unrecognized field "page"）
  if spec.page_style == 'none':
    for _k in ('page', 'pageSize', 'page_size', 'pageNum', 'pageCount'):
      mapped_params.pop(_k, None)

  if spec.value_map:
    for field_name, mapping in spec.value_map.items():
      value = mapped_params.get(field_name)
      if value is not None and value in mapping:
        mapped_params[field_name] = mapping[value]

  if spec.defaults:
    context = (
      await http.get_user_context(on_behalf_of)
      if on_behalf_of else await http.get_default_context()
    )
    for field_name, ctx_key in spec.defaults.items():
      if field_name not in mapped_params or not mapped_params[field_name]:
        mapped_params[field_name] = context.get(ctx_key, '')

  if spec.default_params:
    for field_name, value in spec.default_params.items():
      if field_name not in mapped_params or not mapped_params[field_name]:
        mapped_params[field_name] = (
          datetime.now().strftime('%Y') if value == '@current_year' else value
        )

  # query_person_training 参数补全：month 是季度索引，startTime/endTime 对齐该季度
  if capability_id == 'mgmt.query_person_training':
    try:
      _year = int(mapped_params.get('year', ''))
      _month = int(mapped_params.get('month', ''))
    except (TypeError, ValueError):
      _year = _month = 0
    if _year and _month:
      if not mapped_params.get('startTime') or not mapped_params.get('endTime'):
        _st, _et = _quarter_range(_year, _month)
        mapped_params['startTime'] = _st
        mapped_params['endTime'] = _et

  # time_to_ms 转换：接口要求毫秒 long
  if spec.time_to_ms:
    for field_name in spec.time_to_ms:
      value = mapped_params.get(field_name)
      if isinstance(value, str) and value.strip():
        dt = _parse_datetime(value)
        if dt is not None:
          mapped_params[field_name] = int(dt.timestamp() * 1000)

  # page_nested：分页包成嵌套对象
  if spec.page_nested:
    page = mapped_params.pop('page', None) or '1'
    page_size = mapped_params.pop('pageSize', None) or '20'
    mapped_params['page'] = {'page': page, 'pageSize': page_size}

  try:
    if spec.param_location == 'json':
      data = await http.request(
        spec.method, path, json_body=mapped_params, dept_id=dept_id,
        base_url=spec.base_url, user_id=on_behalf_of,
      )
    else:
      data = await http.request(
        spec.method, path, params=mapped_params, dept_id=dept_id,
        base_url=spec.base_url, user_id=on_behalf_of,
      )
  except http.MgmtError as e:
    raise MgmtError(str(e)) from e

  return _extract_result(data, spec.result_path, spec.page_meta)


async def call(
  capability_id: str,
  params: dict | None = None,
  *,
  on_behalf_of: str | None = None,
) -> dict:
  """执行一次综合管理平台能力调用。

  Args:
    capability_id: 能力 id，如 'mgmt.query_person_by_name'。
    params: 调用参数，键对应 Capability.params（snake_case）。
    on_behalf_of: 代表哪个用户发起（rz user_id），单接口与 pen 组合能力都按该用户身份查。

  Returns:
    结构化 dict 结果。

  Raises:
    MgmtError: capability_id 未注册端点规格，或调用失败。
  """
  params = params or {}
  # 组合能力特判：query_my_info / query_pen_company_dept / query_pending_ticket /
  # query_event_ticket / query_change_ticket / query_breakdown_ticket / query_question_ticket /
  # query_breakdown_ticket_detail / query_question_ticket_detail / query_event_ticket_detail /
  # query_change_ticket_detail 无 EndpointSpec（登录档案 / pen 公司部门解析 / 内部查人→换 token
  # →调 pen / 公司维度 POST 各类工单列表 / 按 id 查各类工单详情），在 ENDPOINTS 查找之前分流。
  if capability_id == 'mgmt.query_my_info':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_my_info(on_behalf_of)
  if capability_id == 'mgmt.query_pen_company_dept':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_pen_company_dept(on_behalf_of)
  if capability_id == 'mgmt.query_event_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_event_ticket(params, on_behalf_of)
  if capability_id == 'mgmt.query_pending_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_pending_ticket(params, on_behalf_of)
  if capability_id == 'mgmt.query_change_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_change_ticket(params, on_behalf_of)
  if capability_id == 'mgmt.query_breakdown_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_breakdown_ticket(params, on_behalf_of)
  if capability_id == 'mgmt.query_question_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_question_ticket(params, on_behalf_of)
  if capability_id == 'mgmt.query_threshold_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_threshold_ticket(params, on_behalf_of)
  if capability_id == 'mgmt.query_supplier_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_supplier_ticket(params, on_behalf_of)
  if capability_id == 'mgmt.query_breakdown_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_breakdown_ticket_detail(params, on_behalf_of)
  if capability_id == 'mgmt.query_question_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_question_ticket_detail(params, on_behalf_of)
  if capability_id == 'mgmt.query_event_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_event_ticket_detail(params, on_behalf_of)
  if capability_id == 'mgmt.query_change_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_change_ticket_detail(params, on_behalf_of)
  if capability_id == 'mgmt.query_supplier_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_supplier_ticket_detail(params, on_behalf_of)
  if capability_id == 'mgmt.query_threshold_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_threshold_ticket_detail(params, on_behalf_of)

  spec = ENDPOINTS.get(capability_id)
  if spec is None:
    raise MgmtError(f'未知能力 {capability_id}（endpoints.py 未声明其 EndpointSpec）')

  logger.info('mgmt.call id=%s params=%s', capability_id, params)

  # 静态参考数据缓存：公司/部门列表结构变化少，TTL 1h，避免每次 Agent 查询都调 HTTP
  if capability_id in _STATIC_REF_CACHE:
    scope = on_behalf_of or 'service'
    cache_key = http.ref_cache_key(capability_id, scope, params)
    cached = http.cache_get_json(cache_key)
    if cached is not None:
      logger.info('mgmt.call 命中缓存 id=%s', capability_id)
      return cached
    data = await _exec_single(spec, capability_id, params, on_behalf_of)
    http.cache_set_json(cache_key, data, 3600)
    return data

  return await _exec_single(spec, capability_id, params, on_behalf_of)

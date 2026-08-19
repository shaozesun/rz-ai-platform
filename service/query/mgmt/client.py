"""综合管理平台统一调用客户端（真实实现，纯分发层）。

设计要点与 dcim/client.py 对称：
- 对上暴露稳定抽象 `call(capability_id, params, on_behalf_of=None)`。
- 单接口能力：本文件不含任何接口相关的硬编码逻辑——method/path/参数映射/取值
  路径全部声明在 endpoints.py 的 EndpointSpec 里，新增/调整接口只改那边。
- 组合能力例外：`mgmt.query_pending_ticket`（查人→换 token→调 pen 三步）、
  `mgmt.query_change_ticket`（公司维度 POST 变更工单）、
  `mgmt.query_breakdown_ticket_detail` / `mgmt.query_question_ticket_detail` /
  `mgmt.query_event_ticket_detail`（按 id 查故障/问题/事件工单详情）无 EndpointSpec，
  在 `call()` 里特判分别走 `_query_pending_ticket` / `_query_change_ticket` /
  `_query_breakdown_ticket_detail` / `_query_question_ticket_detail` /
  `_query_event_ticket_detail`（仅此五处硬编码）。
- `on_behalf_of` 目前未使用（mgmt 侧长期用单一服务账号，暂无按用户区分身份的
  需求，见项目反馈：不强行做身份透传预留设计，等真有需要再加）。
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


class MgmtError(Exception):
  """综合管理平台调用异常。"""


def _build_request(spec: EndpointSpec, params: dict) -> tuple[str, dict, str]:
  """按 EndpointSpec 把 Capability 参数拆成 (真实 path, 剩余参数字典, header dept_id)。

  path 占位符（如 `{name}`）直接从 params 里取同名字段并从剩余参数中剔除，
  避免路径参数被重复当成 query/body 参数发送。剩余参数再按 param_map 把
  Capability 参数名换成真实接口字段名，未在 param_map 里声明的参数名原样传递。

  第三个返回值是用作 `Deptid` header 的部门 id：优先取 path 占位符里的
  `dept_id`，否则取业务筛选参数 `department`（映射为 query/body 里的 `deptId`，
  **注意这个值不会从 mapped 里剔除**——deptId 既是很多接口的必填查询参数，
  又要同时作为 Deptid header，两处都要有值）。当前保留的 3 个接口路径均无
  dept_id 占位符，此项仅对未来需要 Deptid 上下文的接口生效。
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
    header_dept_id = str(remaining.get('department', '') or remaining.get('dept_id', ''))

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


async def _query_change_ticket(params: dict) -> dict:
  """查询 pen 流程平台的变更工单列表（组合能力：公司维度 POST）。

  与待办不同——变更工单是**公司维度**（实测同 body 换不同人 token 结果相同，
  token 只鉴权），故不加 name。默认查服务账号所属「润泽科技（kingdeeCompanyId，
  登录缓存）+ 技术保障部（ownDeptId）」；company_id 传了则按 mgmt 内部 id 映射
  到 pen 的 kingdee 格式（跨公司）。
  """
  kingdee_uid = await http.get_service_kingdee_uid()
  if not kingdee_uid:
    raise MgmtError('服务账号本人 kingdeeUid 未取到，无法查变更工单')

  # companyId：pen 只认 kingdee 格式（sA8...，内部格式返回 0），跨公司需映射
  company_id = params.get('company_id')
  if company_id:
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id))
    if not kingdee_company:
      raise MgmtError(f'公司 {company_id} 不在服务账号可见范围，无法映射到 pen 公司 id')
  else:
    kingdee_company = await http.get_service_pen_company_id()
    if not kingdee_company:
      raise MgmtError('服务账号 kingdeeCompanyId 未取到，无法查变更工单')

  body = {
    'isPubilc': 0,
    'type': params.get('type', 1),        # 1=全部(默认)/2=我发起的/4=我处理的/5=我验收的/6=我监督的
    'secondType': 1,                       # 用户确认固定传 1（用途未知）
    'companyId': kingdee_company,
    'page': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  # dc：显式传了用它；否则默认技术保障部 ownDeptId；跨公司且没传 → 省略（查全公司，
  # 避免把润泽科技的部门带到别的公司）
  dc = params.get('dc')
  if dc:
    body['dc'] = dc
  elif not company_id:
    context = await http.get_default_context()
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
      dt = _parse_datetime(value)
      if dt is not None:
        body[dst] = int(dt.timestamp() * 1000)

  data = await http.pen_request(
    '/ticket/wiporder/ticketlist/modify', json_body=body, kingdee_uid=kingdee_uid,
  )
  # 信封 {simpleTicketVOList,total,page,size,approveCount,...}：列表键不在 analysis_load
  # _RECORD_KEYS，显式归一化成 records（保留 total 与各阶段计数）
  if isinstance(data, dict) and 'simpleTicketVOList' in data:
    data['records'] = data.pop('simpleTicketVOList')
  return data


async def _query_pending_ticket(params: dict) -> dict:
  """查询指定人员的流程平台待办工单（组合能力：查人 → 换 token → 调 pen）。

  name 传了先用 query_person_by_name 拿首个匹配人员的 kingdeeUid；不传用服务
  账号本人（登录缓存的 kingdeeUid）。换 token 接口只在内部调用，不透出 LLM
  （能拿任意用户 token = 身份冒充）。
  """
  kingdee_uid = ''
  if params.get('name'):
    persons = await call('mgmt.query_person_by_name', {'name': params['name']})
    persons = persons if isinstance(persons, list) else (persons.get('list') or [])
    for person in persons or []:
      if person.get('kingdeeUid'):
        kingdee_uid = str(person['kingdeeUid'])
        break
    if not kingdee_uid:
      raise MgmtError(
        f"未找到人员「{params['name']}」或其 kingdeeUid（query_person_by_name 返回空）"
      )
  else:
    kingdee_uid = await http.get_service_kingdee_uid()
    if not kingdee_uid:
      raise MgmtError('服务账号本人 kingdeeUid 未取到，无法查待办')

  query = {
    'isDone': params.get('is_done', 1),   # 1=待办(默认) / 2=已办（实测 isDone=2 返回已办）
    'deviceType': 1,                       # 实测 URL 固定携带的客户端类型参数
    'current': params.get('page', 1),
    'size': params.get('page_size', 20),
  }
  if params.get('type') is not None:       # 工单类型，可选（用户确认可不传）
    query['type'] = params['type']
  if params.get('company_id'):             # 公司 id，可选
    query['companyId'] = params['company_id']

  # 返回 pen 信封 {records,total,current,size,pages}，analysis_load 可直接物化
  return await http.pen_request('/pen/pending/list/v1', query, kingdee_uid)


async def _query_breakdown_ticket_detail(params: dict) -> dict:
  """查询单张故障工单的完整详情（组合能力：按待办列表 id 查 pen）。

  详情端点 `GET /ticket/wiporder/query/breakdown/{id}` 实测不按 token 归属人区分
  （自 token 与别人 token 查到同一张单，仅按钮/审计等视图字段不同），故直接用服务
  账号本人 token（pen_request 的 kingdee_uid 留空默认）。id 来自 query_pending_ticket
  待办列表返回的 id 字段（用户确认「里面返回的 id 就是工单 id」）。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询故障工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.pen_request(f'/ticket/wiporder/query/breakdown/{ticket_id}')


async def _query_question_ticket_detail(params: dict) -> dict:
  """查询单张问题工单的完整详情（组合能力：按待办列表 id 查 pen）。

  同 _query_breakdown_ticket_detail：详情端点 `/ticket/wiporder/question/{id}` 不按
  token 归属人区分（实测自 token 与别人 token 查到同一张单），用服务账号本人 token。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询问题工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.pen_request(f'/ticket/wiporder/question/{ticket_id}')


async def _query_event_ticket_detail(params: dict) -> dict:
  """查询单张事件工单的完整详情（组合能力：按待办列表 id 查 pen）。

  同 _query_breakdown_ticket_detail：详情端点 `/ticket/wiporder/event/{id}` 不按
  token 归属人区分（与故障/问题详情同结构同认证），用服务账号本人 token。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询事件工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.pen_request(f'/ticket/wiporder/event/{ticket_id}')


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
    on_behalf_of: 目前未使用，见模块 docstring。

  Returns:
    结构化 dict 结果。

  Raises:
    MgmtError: capability_id 未注册端点规格，或调用失败。
  """
  params = params or {}
  # 组合能力特判：query_pending_ticket / query_change_ticket / query_breakdown_ticket_detail
  # / query_question_ticket_detail / query_event_ticket_detail 无 EndpointSpec（内部查人→
  # 换 token→调 pen / 公司维度 POST / 按 id 查各类工单详情），在 ENDPOINTS 查找之前分流。
  if capability_id == 'mgmt.query_pending_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_pending_ticket(params)
  if capability_id == 'mgmt.query_change_ticket':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_change_ticket(params)
  if capability_id == 'mgmt.query_breakdown_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_breakdown_ticket_detail(params)
  if capability_id == 'mgmt.query_question_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_question_ticket_detail(params)
  if capability_id == 'mgmt.query_event_ticket_detail':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_event_ticket_detail(params)

  spec = ENDPOINTS.get(capability_id)
  if spec is None:
    raise MgmtError(f'未知能力 {capability_id}（endpoints.py 未声明其 EndpointSpec）')

  logger.info('mgmt.call id=%s params=%s', capability_id, params)

  path, mapped_params, dept_id = _build_request(spec, params)
  # 无分页接口（page_style='none'）不接受分页参数。analysis_load 物化全量时会
  # 无条件注入 page/page_size，透传会让平台 DTO 反序列化失败（实测 Unrecognized
  # field "page"）→ 工具异常 → 整轮「回复生成中断」。按声明的 page_style 剥离
  # 分页参数；分页接口的字段名已由 param_map 精确映射，不受影响。
  if spec.page_style == 'none':
    for _k in ('page', 'pageSize', 'page_size', 'pageNum', 'pageCount'):
      mapped_params.pop(_k, None)
  # dept_id 为空时 http.request 会用登录账号默认部门兜底（跨部门查询场景下
  # 调用方传了 department 参数才会有值）

  # value_map 值映射：Capability 给中文值（如 parent_group='供配电'），接口只认
  # code。找不到映射的值原样透传（让接口报错显形，便于 LLM 修正）。
  if spec.value_map:
    for field_name, mapping in spec.value_map.items():
      value = mapped_params.get(field_name)
      if value is not None and value in mapping:
        mapped_params[field_name] = mapping[value]

  # defaults 兜底：records/v2 之类需要部门/公司上下文的 json 接口，LLM 不会传
  # deptId/companyId，用服务账号登录缓存的 own_dept_id/company_id 补上（实测
  # 只传 uid 会报「部门不存在/公司不存在」）。
  if spec.defaults:
    context = await http.get_default_context()
    for field_name, ctx_key in spec.defaults.items():
      if field_name not in mapped_params or not mapped_params[field_name]:
        mapped_params[field_name] = context.get(ctx_key, '')

  # default_params 兜底：分页接口必须显式带 page/pageSize（实测不带报「服务器
  # 错误」），LLM 不一定传，直接填字面量常量（区别于 defaults 从上下文取值）。
  # 特殊值 '@current_year' 是动态占位符：query_equipment_maintenance_plan 的 year
  # 必填（不带报 HTTP 400）且需当年年份，这里替换为当前年份字符串。
  if spec.default_params:
    for field_name, value in spec.default_params.items():
      if field_name not in mapped_params or not mapped_params[field_name]:
        mapped_params[field_name] = (
          datetime.now().strftime('%Y') if value == '@current_year' else value
        )

  # time_to_ms 转换：接口要求毫秒 long（传日期字符串报 400），能力参数给可读日期
  # 串（YYYY-MM-DD[ HH:MM:SS]）时转成毫秒；已是数值或不可解析则原样透传（让接口
  # 报错显形，便于 LLM 修正）。
  if spec.time_to_ms:
    for field_name in spec.time_to_ms:
      value = mapped_params.get(field_name)
      if isinstance(value, str) and value.strip():
        dt = _parse_datetime(value)
        if dt is not None:
          mapped_params[field_name] = int(dt.timestamp() * 1000)

  # page_nested：分页包成 `page:{page,pageSize}` 嵌套对象（default_params 兜底的
  # page/pageSize 一并包入；缺省用 1/20）。
  if spec.page_nested:
    page = mapped_params.pop('page', None) or '1'
    page_size = mapped_params.pop('pageSize', None) or '20'
    mapped_params['page'] = {'page': page, 'pageSize': page_size}

  try:
    if spec.param_location == 'json':
      data = await http.request(
        spec.method, path, json_body=mapped_params, dept_id=dept_id,
        base_url=spec.base_url,
      )
    else:
      data = await http.request(
        spec.method, path, params=mapped_params, dept_id=dept_id,
        base_url=spec.base_url,
      )
  except http.MgmtError as e:
    raise MgmtError(str(e)) from e

  return _extract_result(data, spec.result_path, spec.page_meta)

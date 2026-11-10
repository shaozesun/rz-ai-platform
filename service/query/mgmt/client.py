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
  另有 `mgmt.query_equipment_maintenance_flow`（设备维护操作流程列表）走组合能力：
  该接口把 deptId/page/pageSize 放在 path 里，声明式的 defaults/default_params 在
  `_build_request` 之后才应用、够不到 path 占位符，故路径在
  `_query_equipment_maintenance_flow` 内自行拼接（详见该函数 docstring）。
- `on_behalf_of` 为认证中心身份透传：单接口路径按其 user_id 用该用户自己的
  mgmt token 查询，8 个 pen 组合能力也用该用户身份换发 token/公司部门上下文，
  实现 mgmt/pen 侧数据权限按人生效（user_id 为空才回退服务账号）。
"""

import logging
import re
from datetime import datetime
from urllib.parse import quote

from config.settings import settings
from service.query.mgmt import http
from service.query.mgmt.endpoints import ENDPOINTS, EndpointSpec

logger = logging.getLogger(__name__)

_PATH_PLACEHOLDER_RE = re.compile(r'\{(\w+)\}')

_DATETIME_FORMATS = ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d')


async def _resolve_ticket_dc(params: dict) -> str | None:
  """决定工单列表要带的 dc（部门 id）。

  口径（2026-10-06 调整）：**未指定范围＝查本公司全部部门（不带 dc）**。
  - 显式传 dc → 用它；`dc='ALL'` 或 `all_dept=true` → 不带 dc（与默认一致）；
  - 未指定 → 不带 dc。

  旧口径「未指定 → 本人部门（ownDeptId）」已废弃：工单编号是全局唯一的，按单号查时被限在
  本人部门会直接查空、被答成「不存在」（2026-10-04 实测）。部门维度只在用户**明确要按部门
  列列表**（传 dc）时才收窄。
  """
  dc = params.get('dc')
  if isinstance(dc, str) and dc.strip():
    return None if dc.strip().upper() == 'ALL' else dc.strip()
  return None


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
  # dc：显式传用显式值；未指定＝查本公司全部部门（2026-10-06 口径，见 _resolve_ticket_dc）
  dc = await _resolve_ticket_dc(params)
  if dc:
    body['dc'] = dc

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

  all_companies = bool(params.get('all_companies'))
  if all_companies:
    # 全国模式：不按部门收窄（逐公司拉各自全部部门）
    body.pop('dc', None)

  async def _post(company_kingdee: str) -> dict:
    """按公司取一次（body 复用，只换 companyId）——全国模式逐公司串行调用它。"""
    body['companyId'] = company_kingdee
    d = await http.ticket_request('/wiporder/ticketlist/modify', json_body=body, auth_user_id=user_id)
    if isinstance(d, dict) and 'simpleTicketVOList' in d:
      d['records'] = d.pop('simpleTicketVOList')
    return d

  data = (await _aggregate_companies(user_id, _post) if all_companies
          else await _post(kingdee_company))
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _decorate_ticket_list(
    data, user_id, dc=dc, all_companies=all_companies,
    company_kingdee=kingdee_company)


async def _query_change_statistics(params: dict, user_id: str | None = None) -> dict:
  """查询变更单统计（组合能力：公司维度 POST，返回按部门分组的变更单）。

  端点 `/wiporder/ticketlist/statistics/modify`（2026-09-16 服务账号实测可用），
  入参**只有 {companyId, startTime, endTime}**（毫秒）——与工单列表那套
  type/page/size/dc/secondType 不是同一组（同一 body 打列表接口直接 500 NPE）。

  接口返回 `{baseStatistics:[{dc, dcName, statisticsMetadataVOS:[单子…]}]}`，
  这里**展平成一单一行**（附 `dcName`）再返回，原因：① `analysis_load` 抽记录列表取不到
  嵌套结构（会抽到部门分组对象，进 CSV 变成嵌套列，沙箱里没法用）；② 扁平行才能直接
  groupby 出「各分部变更单数量」。部门名已在组上，故不需要 `_decorate_ticket_list`。
  """
  # companyId：pen 只认 kingdee 格式；跨公司需映射（与列表接口同一套解析）
  company_id = params.get('company_id')
  if company_id:
    kingdee_company = await http.get_pen_company_kingdee_id(str(company_id), auth_user_id=user_id)
    if not kingdee_company:
      kingdee_company = str(company_id)
  else:
    kingdee_company = (
      await http.get_user_pen_company_id(user_id) if user_id
      else await http.get_service_pen_company_id()
    )
    if not kingdee_company:
      raise MgmtError('当前账号 kingdeeCompanyId 未取到，无法查变更单统计')

  body: dict = {'companyId': kingdee_company}
  for src, dst in (('start_time', 'startTime'), ('end_time', 'endTime')):
    value = params.get(src)
    if isinstance(value, str) and value.strip():
      ms = _time_to_ms(value, end_of_day=(dst == 'endTime'))
      if ms is not None:
        body[dst] = ms
  # 只给开始没给结束（如“9.1 至今”）→ 补当前，避免按全量历史返回（与列表接口口径一致）
  if 'startTime' in body and 'endTime' not in body:
    body['endTime'] = int(datetime.now().timestamp() * 1000)

  all_companies = bool(params.get('all_companies'))

  async def _post(company_kingdee: str) -> dict:
    """按公司取一次统计（body 复用，只换 companyId）——全国模式逐公司串行调用。"""
    body['companyId'] = company_kingdee
    d = await http.ticket_request(
      '/wiporder/ticketlist/statistics/modify', json_body=body, auth_user_id=user_id,
    )
    groups = d.get('baseStatistics') if isinstance(d, dict) else None
    if not isinstance(groups, list):
      return d  # 信封与预期不符时原样返回，别把数据吞掉
    records: list[dict] = []
    for group in groups:
      if not isinstance(group, dict):
        continue
      dept_name = group.get('dcName') or group.get('dc') or ''
      for ticket in group.get('statisticsMetadataVOS') or []:
        if isinstance(ticket, dict):
          records.append({**ticket, 'dcName': ticket.get('dcName') or dept_name})
    # 必须带 total：本接口不分页、一次给全量，而 analysis_load 会按 page/page_size 循环拉，
    # 没有 total 且单页条数 ≥ page_size 时会**反复拉同一批**（实测重复到 max_rows：612 条变 10404）。
    # 给了 total，analysis_load 见 len(records) >= total 即停。
    return {'records': records, 'total': len(records), 'departments': len(groups)}

  if all_companies:
    merged = await _aggregate_companies(user_id, _post)
    return {**merged, 'scope': '全国（所有公司/园区）'}
  return await _post(kingdee_company)


async def _query_breakdown_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的故障工单列表（组合能力：公司维度 POST）。

  同 _query_change_ticket——故障工单列表也是公司维度（token 只鉴权不区分人），
  默认查当前用户所属公司/部门（user_id 为空回退服务账号）；company_id 传了则
  跨公司映射。与变更列表的区别仅：端点换 /wiporder/ticketlist/breakdown，
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
  dc = await _resolve_ticket_dc(params)
  if dc:
    body['dc'] = dc

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

  all_companies = bool(params.get('all_companies'))
  if all_companies:
    # 全国模式：不按部门收窄（逐公司拉各自全部部门）
    body.pop('dc', None)

  async def _post(company_kingdee: str) -> dict:
    """按公司取一次（body 复用，只换 companyId）——全国模式逐公司串行调用它。"""
    body['companyId'] = company_kingdee
    d = await http.ticket_request('/wiporder/ticketlist/breakdown', json_body=body, auth_user_id=user_id)
    if isinstance(d, dict) and 'simpleTicketVOList' in d:
      d['records'] = d.pop('simpleTicketVOList')
    return d

  data = (await _aggregate_companies(user_id, _post) if all_companies
          else await _post(kingdee_company))
  _decode_equipment_types(data)
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _decorate_ticket_list(
    data, user_id, dc=dc, all_companies=all_companies,
    company_kingdee=kingdee_company)


async def _query_question_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的问题工单列表（组合能力：公司维度 POST）。

  同 _query_change_ticket——问题工单列表也是公司维度，默认查当前用户所属公司/部门
  （user_id 为空回退服务账号）；company_id 传了则跨公司映射。与变更列表的区别：
  端点换 /wiporder/ticketlist/question，筛选项换成 severityLevel（问题级别）
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
  dc = await _resolve_ticket_dc(params)
  if dc:
    body['dc'] = dc

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

  all_companies = bool(params.get('all_companies'))
  if all_companies:
    # 全国模式：不按部门收窄（逐公司拉各自全部部门）
    body.pop('dc', None)

  async def _post(company_kingdee: str) -> dict:
    """按公司取一次（body 复用，只换 companyId）——全国模式逐公司串行调用它。"""
    body['companyId'] = company_kingdee
    d = await http.ticket_request('/wiporder/ticketlist/question', json_body=body, auth_user_id=user_id)
    if isinstance(d, dict) and 'simpleTicketVOList' in d:
      d['records'] = d.pop('simpleTicketVOList')
    return d

  data = (await _aggregate_companies(user_id, _post) if all_companies
          else await _post(kingdee_company))
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _decorate_ticket_list(
    data, user_id, dc=dc, all_companies=all_companies,
    company_kingdee=kingdee_company)


async def _query_threshold_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的阈值工单列表（组合能力：公司维度 POST）。

  同 _query_question_ticket——阈值工单也是公司维度，默认查当前用户所属公司/部门
  （user_id 为空回退服务账号）；company_id 传了则跨公司映射。与问题工单列表的区别：
  端点换 /wiporder/ticketlist/threshold，无 severityLevel，specialty 专业枚举
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
  dc = await _resolve_ticket_dc(params)
  if dc:
    body['dc'] = dc

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

  all_companies = bool(params.get('all_companies'))
  if all_companies:
    # 全国模式：不按部门收窄（逐公司拉各自全部部门）
    body.pop('dc', None)

  async def _post(company_kingdee: str) -> dict:
    """按公司取一次（body 复用，只换 companyId）——全国模式逐公司串行调用它。"""
    body['companyId'] = company_kingdee
    d = await http.ticket_request('/wiporder/ticketlist/threshold', json_body=body, auth_user_id=user_id)
    if isinstance(d, dict) and 'simpleTicketVOList' in d:
      d['records'] = d.pop('simpleTicketVOList')
    return d

  data = (await _aggregate_companies(user_id, _post) if all_companies
          else await _post(kingdee_company))
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _decorate_ticket_list(
    data, user_id, dc=dc, all_companies=all_companies,
    company_kingdee=kingdee_company)


async def _query_supplier_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的供应商服务工单列表（组合能力：公司维度 POST）。

  同 _query_threshold_ticket——供应商服务工单也是公司维度，默认查当前用户所属公司/部门
  （user_id 为空回退服务账号）；company_id 传了则跨公司映射。与阈值工单列表的区别：
  端点换 /wiporder/ticketlist/supplier，筛选项换成 serviceType（服务类型），
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
  dc = await _resolve_ticket_dc(params)
  if dc:
    body['dc'] = dc

  for src, dst in (
    ('ticket_no', 'ticketNo'), ('project_name', 'projectName'), ('key_word', 'keyWord'),
    ('service_type', 'serviceType'), ('stage', 'stage'),
  ):
    value = params.get(src)
    if value is not None and value != '':
      body[dst] = value

  all_companies = bool(params.get('all_companies'))
  if all_companies:
    # 全国模式：不按部门收窄（逐公司拉各自全部部门）
    body.pop('dc', None)

  async def _post(company_kingdee: str) -> dict:
    """按公司取一次（body 复用，只换 companyId）——全国模式逐公司串行调用它。"""
    body['companyId'] = company_kingdee
    d = await http.ticket_request('/wiporder/ticketlist/supplier', json_body=body, auth_user_id=user_id)
    if isinstance(d, dict) and 'simpleTicketVOList' in d:
      d['records'] = d.pop('simpleTicketVOList')
    return d

  data = (await _aggregate_companies(user_id, _post) if all_companies
          else await _post(kingdee_company))
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _decorate_ticket_list(
    data, user_id, dc=dc, all_companies=all_companies,
    company_kingdee=kingdee_company)


async def _query_event_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询 pen 流程平台的事件工单列表（组合能力：公司维度 POST）。

  与问题工单同构（DTO 共用 SelectTicketListDTOEventAndQuestion，支持同样筛选字段）：
  公司/部门维度，默认查当前用户所属公司/部门（user_id 为空回退服务账号）；
  company_id 传了则跨公司映射。端点换 /wiporder/ticketlist/event；
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
  dc = await _resolve_ticket_dc(params)
  if dc:
    body['dc'] = dc

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

  all_companies = bool(params.get('all_companies'))
  if all_companies:
    # 全国模式：不按部门收窄（逐公司拉各自全部部门）
    body.pop('dc', None)

  async def _post(company_kingdee: str) -> dict:
    """按公司取一次（body 复用，只换 companyId）——全国模式逐公司串行调用它。"""
    body['companyId'] = company_kingdee
    d = await http.ticket_request('/wiporder/ticketlist/event', json_body=body, auth_user_id=user_id)
    if isinstance(d, dict) and 'simpleTicketVOList' in d:
      d['records'] = d.pop('simpleTicketVOList')
    return d

  data = (await _aggregate_companies(user_id, _post) if all_companies
          else await _post(kingdee_company))
  # 附上查询范围（具体公司/部门名），供 LLM 回复时说明
  return await _decorate_ticket_list(
    data, user_id, dc=dc, all_companies=all_companies,
    company_kingdee=kingdee_company)


async def _query_equipment_maintenance_flow(
  params: dict, user_id: str | None = None,
) -> dict:
  """查询设备维护操作流程列表（组合能力：deptId/page/pageSize 都在 path 上）。

  触发组合能力而非 EndpointSpec 的原因：接口把部门与分页写进 **path**
  （`/maintain/flow/v3/{deptId}/{page}/{pageSize}`），而声明式的 defaults/default_params
  是在 `_build_request` **之后**才应用的、够不到 path 占位符——模型不传 page 就会拼出
  `/maintain/flow/v3/23BaepxKklv//` 这种非法路径。故路径在这里自己拼，兜底语义与声明式
  保持一致：dept_id 缺省取当前登录用户所属部门（服务账号回退技术保障部），page/page_size
  缺省 1/20。

  name 是可选模糊查询（如「视频」→ 命中视频安防监控系统相关流程），不传返回该部门全部流程。
  返回信封 {totalPage,totalCount,rows}（与 /maintain/v2 同构），原样返回供模型读总量。
  """
  dept_id = params.get('dept_id')
  if not dept_id:
    context = (
      await http.get_user_context(user_id) if user_id
      else await http.get_default_context()
    )
    dept_id = context.get('own_dept_id') or ''
  if not dept_id:
    raise MgmtError('当前账号所属部门未取到，无法查设备维护操作流程')

  page = params.get('page') or 1
  page_size = params.get('page_size') or 20
  path = f'/maintain/flow/v3/{quote(str(dept_id), safe="")}/{page}/{page_size}'
  query = {'name': params['name']} if params.get('name') else {}

  return await http.request(
    'GET', path, params=query, dept_id=dept_id, user_id=user_id,
  )


async def _query_pen_company_dept(user_id: str | None = None) -> dict:
  """查询当前用户工单服务可见的公司及部门（组合能力：取 kingdeeUid 调 ticket-api）。

  返回各公司 {companyId 内部, kingdeeCompanyId, companyName, deptList:[{deptId,deptName}]}，
  用于解析「XX 数据中心/机房」→ 工单列表查询要用的 dc 部门 id 与公司 id（pen 的 dc 即
  mgmt 部门 id，与 query_dept_by_name 返回一致，但本接口按公司分组，能区分跨公司的同名部门）。
  """
  if not user_id:
    raise MgmtError('无法确定当前用户身份（未走 Agent 认证通道）')

  # 静态参考数据缓存：公司/部门结构变化少，默认 TTL 24h（MGMT_REF_CACHE_TTL 可调）
  cache_key = http.ref_cache_key('pen_company_dept', user_id)
  cached = http.cache_get_json(cache_key)
  if cached is not None:
    return cached

  kingdee_uid = await http.get_user_kingdee_uid(user_id)
  if not kingdee_uid:
    raise MgmtError('当前用户 kingdeeUid 未取到，无法查询公司/部门')
  data = await http.ticket_request(
    f'/company/user/{kingdee_uid}', auth_user_id=user_id,
  )
  http.cache_set_json(cache_key, data, settings.MGMT_REF_CACHE_TTL)
  return data


async def _list_pen_companies(user_id: str | None = None) -> list[dict]:
  """当前账号可见的公司列表（[{companyId 内部, companyName, kingdee, gardenName}]）。

  供「查全部公司工单」逐公司聚合用：一次空 companyId 调用实测返回 0（不可靠），
  必须按公司逐个查。来源＝`http.company_registry`（mgmt.query_company_list，文档内接口，
  命中 `_STATIC_REF_CACHE`）——不再走 pen 未登记的 `/pen/pending/list/company`。
  """
  return [c for c in await http.company_registry(user_id) if c['kingdee']]


async def _attach_scope(
  data: dict, user_id: str | None = None, *, dc: str | None = None,
  all_companies: bool = False, company_kingdee: str = '',
) -> dict:
  """在结果里附上**实际查询范围**（公司 + 部门名），供 LLM 回复时说明。

  部门必须反映**实际请求的范围**（2026-10-06）：默认口径已改为「未指定 dc ＝ 本公司全部部门」，
  旧实现无条件回填 `profile.own_dept_name` 会答成「技术保障部」而实际查的是全公司各部门 ——
  等于在一个地方修好了、又在另一个地方说谎。
  - 传了 dc → 回该 deptId 的部门名（`_query_pen_company_dept` Redis 缓存；取不到回原 id）
  - 没传 dc → 回「全部部门」
  """
  try:
    profile = await http.get_user_profile(user_id) if user_id else {}
  except Exception:
    profile = {}
  company = profile.get('company_name', '')
  n_companies = data.get('companies') if isinstance(data, dict) else None
  if all_companies:
    # 全国口径：数据跨所有公司/园区，不能回填"本人公司"
    company = f'全国（{n_companies or "全部"} 个公司/园区）'
    dept = '所有园区 → 各公司全部部门'
  elif dc:
    dept = (await _dept_name_map(
      user_id, company_kingdee=company_kingdee)).get(str(dc), str(dc))
  else:
    dept = '全部部门'
  if isinstance(data, dict):
    scope = {'dept': dept}
    if company:
      scope['company'] = company
    data['_query_scope'] = scope
  return data


def _collect_dept_names(obj, out: dict) -> None:
  """递归收集 deptId → deptName。pen 公司/部门接口的信封形状不固定，故不写死键名。"""
  if isinstance(obj, dict):
    did, name = obj.get('deptId'), obj.get('deptName')
    if did and name:
      out[str(did)] = str(name)
    for v in obj.values():
      _collect_dept_names(v, out)
  elif isinstance(obj, list):
    for v in obj:
      _collect_dept_names(v, out)


def _decode_equipment_types(node) -> None:
  """就地解码 `breakdownEquipmentType`（故障设备类型）：**按同行的 specialty 查表**。

  值本身没有全局含义（同一个 11 在供配电/制冷/智能化下是三种东西），所以必须成对出现才能翻；
  查不到（专业不在表内 / 码缺失 / 非数字）**保留原值**——宁可显示数字，也不猜错设备。
  递归整棵树：列表行、详情 VO、以及详情里嵌套的「涉及设备」数组都覆盖到。

  为什么在返回前就地解码、而不是教模型码表：三张表合计 83 项，渲染进工具描述每轮都要付
  ~600 token；就地解码后模型和前端表格看到的**都是中文**，零上下文成本（同 `_resolve_dc_names`
  把行内 dc 换成部门名的做法）。
  """
  from service.query.mgmt.ticket_codes import equipment_type_label

  if isinstance(node, dict):
    specialty, etype = node.get('specialty'), node.get('breakdownEquipmentType')
    if specialty is not None and etype is not None:
      label = equipment_type_label(specialty, etype)
      if label:
        node['breakdownEquipmentType'] = label
    for value in node.values():
      _decode_equipment_types(value)
  elif isinstance(node, list):
    for value in node:
      _decode_equipment_types(value)


async def _aggregate_companies(user_id: str | None, post_one) -> dict:
  """「全国」模式：遍历所有可见公司**串行**拉取并合并（pen 同接口有并发限制，禁止并发）。

  post_one(kingdee_company) -> 该公司的信封（已把 simpleTicketVOList 归一成 records）。
  合并后每条记录补 `companyName`（跨园区聚合/对比要靠它），并返回 `companies` 计数。
  """
  companies = await _list_pen_companies(user_id)
  if not companies:
    raise MgmtError('未取到可见的公司列表，无法按全国统计')
  records: list = []
  total = 0
  for comp in companies:
    data = await post_one(comp['kingdee'])
    rows = data.get('records') or []
    for r in rows:
      if isinstance(r, dict) and not r.get('companyName'):
        r['companyName'] = comp.get('companyName') or ''
    records.extend(rows)
    total += data.get('total', len(rows))
  return {'records': records, 'total': total, 'companies': len(companies)}


async def _internal_company_id(user_id: str | None, company_kingdee: str) -> str:
  """pen kingdeeCompanyId → mgmt 内部 companyId（`query_dept_list` 的 companyId 认内部格式）。

  公司范围随账号变化，取 `_list_pen_companies` 现查（园区清单已固定在
  `report_templates.PARKS`，不再与它同源）。
  找不到/取不到一律返回 ''，由调用方决定回退。
  """
  target = str(company_kingdee or '').strip()
  if not target:
    return ''
  try:
    companies = await _list_pen_companies(user_id)
  except Exception:
    return ''
  for comp in companies:
    if target in (comp.get('kingdee'), comp.get('companyId'), comp.get('companyName')):
      return str(comp.get('companyId') or '')
  return ''


async def _dept_name_map(
  user_id: str | None = None, *, all_companies: bool = False,
  company_kingdee: str = '',
) -> dict[str, str]:
  """deptId → deptName 映射；取不到就返回 {}。

  1. **有用户身份** → `_query_pen_company_dept`（按该用户可见范围，能区分跨公司同名部门）
  2. **按本次查询的公司取**（2026-10-22 补）—— 定时任务/服务账号**没有用户身份**，
     此前只回退无参 `mgmt.query_dept_list`，接口兜底成服务账号**默认公司**（润泽科技），
     其它园区（平湖/惠州/佛山/重庆）的部门全解析不到 → 报告里写部门编号
     （实测平湖 `27gINaXGZqF` 本可解析为「平湖A1分部」）。
  3. **仍为空时回退无参 `mgmt.query_dept_list`**（2026-10-12 补，保留原行为）。
  """
  mapping: dict = {}
  if user_id:
    try:
      _collect_dept_names(await _query_pen_company_dept(user_id), mapping)
    except Exception:
      mapping = {}
  if not all_companies:
    internal_id = await _internal_company_id(user_id, company_kingdee)
    if internal_id:
      try:
        _collect_dept_names(
          await call('mgmt.query_dept_list', {'company_id': internal_id},
                     on_behalf_of=user_id), mapping)
      except Exception:
        pass
  if not mapping and not all_companies:
    try:
      _collect_dept_names(await call('mgmt.query_dept_list', {}), mapping)
    except Exception:
      pass
  if all_companies:
    # 跨园区：部门 id 各公司各不相同 → 逐公司取部门列表合并（best-effort，取不到保留 id）
    try:
      for comp in await _list_pen_companies(user_id):
        try:
          part = await call(
            'mgmt.query_dept_list', {'company_id': comp.get('companyId', '')},
            on_behalf_of=user_id,
          )
        except Exception:
          continue
        _collect_dept_names(part, mapping)
    except Exception:
      pass
  return mapping


async def _resolve_dc_names(
  data: dict, user_id: str | None = None, *, all_companies: bool = False,
  company_kingdee: str = '',
) -> None:
  """工单列表行内 dc（部门 id）就地换成部门名（清单要求的展示字段），best-effort。

  部门清单随登录用户/查询公司变化，用 `_dept_name_map`（Redis 缓存）拍平；未命中一律保留
  原 id——宁可显示 id，也不丢字段或让主流程报错。请求侧 dc 仍传部门 id，不受影响。
  未解析的条数**要打日志**：过去静默吞掉，报告写出部门编号很久才被发现。
  """
  records = data.get('records') if isinstance(data, dict) else None
  if not isinstance(records, list) or not any(
    isinstance(r, dict) and r.get('dc') for r in records
  ):
    return
  mapping = await _dept_name_map(
    user_id, all_companies=all_companies, company_kingdee=company_kingdee)
  unresolved: list[str] = []
  for r in records:
    if isinstance(r, dict) and r.get('dc'):
      name = mapping.get(str(r.get('dc')))
      if name:
        r['dc'] = name
      else:
        unresolved.append(str(r.get('dc')))
  if unresolved:
    logger.warning(
      '工单部门名未解析 %d 条（公司=%s，示例 %s）',
      len(unresolved), company_kingdee or '默认',
      sorted(set(unresolved))[:3])


async def _decorate_ticket_list(
  data: dict, user_id: str | None = None, *, dc: str | None = None,
  all_companies: bool = False, company_kingdee: str = '',
) -> dict:
  """工单列表收尾：行内 dc 换部门名 + 附**实际查询范围**（公司/部门名，供 LLM 说明）。

  dc 是本次请求实际用的部门 id（None ＝ 未限部门）——透传给 _attach_scope，让它回填真实的
  部门范围，而不是无条件写「本人部门」。`company_kingdee` 是本次实际查的公司，
  部门名映射要按它取（否则服务账号只会映射默认公司的部门）。
  """
  await _resolve_dc_names(
    data, user_id, all_companies=all_companies, company_kingdee=company_kingdee)
  return await _attach_scope(
    data, user_id, dc=dc, all_companies=all_companies,
    company_kingdee=company_kingdee)


async def _query_pending_ticket(params: dict, user_id: str | None = None) -> dict:
  """查询工单服务「我的工单」（仅当前账号本人，用本人 mgmt token 查自己的工单）。

  按 auth_user_id 用用户本人的 mgmt token 请求（新服务按 token 归属人看数据）；
  不支持按姓名查他人/换他人 token（user_id 为空回退服务账号本人）。

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
    return await http.ticket_request(
      '/pending/list/v1', query, auth_user_id=user_id,
    )

  # 查全部公司：逐公司调用合并
  companies = await _list_pen_companies(user_id)
  all_records: list = []
  total = 0
  for comp in companies:
    query = {**base, 'companyId': comp['kingdee']}
    data = await http.ticket_request(
      '/pending/list/v1', query, auth_user_id=user_id,
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

  详情端点 `GET /wiporder/query/breakdown/{id}` 实测不按 token 归属人区分
  （自 token 与别人 token 查到同一张单，仅按钮/审计等视图字段不同），仍用当前用户
  身份（auth_user_id），user_id 为空回退服务账号。id 来自 query_pending_ticket
  待办列表返回的 id 字段（用户确认「里面返回的 id 就是工单 id」）。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询故障工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  data = await http.ticket_request(
    f'/wiporder/query/breakdown/{ticket_id}', auth_user_id=user_id,
  )
  _decode_equipment_types(data)
  return data


async def _query_question_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张问题工单的完整详情（组合能力：按待办列表 id 查 pen）。

  同 _query_breakdown_ticket_detail：详情端点 `/wiporder/question/{id}` 不按
  token 归属人区分（实测自 token 与别人 token 查到同一张单），用当前用户身份。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询问题工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.ticket_request(
    f'/wiporder/question/{ticket_id}', auth_user_id=user_id,
  )


async def _query_event_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张事件工单的完整详情（组合能力：按待办列表 id 查 pen）。

  同 _query_breakdown_ticket_detail：详情端点 `/wiporder/event/{id}` 不按
  token 归属人区分（与故障/问题详情同结构同认证），用当前用户身份。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询事件工单详情缺 ticket_id（从 query_pending_ticket 待办列表返回的 id 获取）')
  return await http.ticket_request(
    f'/wiporder/event/{ticket_id}', auth_user_id=user_id,
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
  return await http.ticket_request(
    f'/wiporder/query/modify/{ticket_id}', auth_user_id=user_id,
  )


async def _query_supplier_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张供应商服务工单的完整详情（组合能力：按供应商工单列表 ticketId 查 pen）。

  同其它工单详情：详情端点 GET /wiporder/supplier/{id} 不按 token 归属人区分，
  用当前用户身份。id 来自 query_supplier_ticket 列表返回的 ticketId 字段。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询供应商服务工单详情缺 ticket_id（从 query_supplier_ticket 列表返回的 ticketId 字段获取）')
  return await http.ticket_request(
    f'/wiporder/supplier/{ticket_id}', auth_user_id=user_id,
  )


async def _query_threshold_ticket_detail(params: dict, user_id: str | None = None) -> dict:
  """查询单张阈值工单的完整详情（组合能力：按阈值工单列表 ticketId 查 pen）。

  同其它工单详情：详情端点 GET /wiporder/threshold/{id} 不按 token 归属人区分，
  用当前用户身份。id 来自 query_threshold_ticket 列表返回的 ticketId 字段。
  """
  ticket_id = params.get('ticket_id')
  if not ticket_id:
    raise MgmtError('查询阈值工单详情缺 ticket_id（从 query_threshold_ticket 列表返回的 ticketId 字段获取）')
  return await http.ticket_request(
    f'/wiporder/threshold/{ticket_id}', auth_user_id=user_id,
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
  # query_event_ticket / query_change_ticket / query_change_statistics /
  # query_breakdown_ticket / query_question_ticket /
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
  if capability_id == 'mgmt.query_change_statistics':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_change_statistics(params, on_behalf_of)
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
  if capability_id == 'mgmt.query_equipment_maintenance_flow':
    logger.info('mgmt.call id=%s params=%s', capability_id, params)
    return await _query_equipment_maintenance_flow(params, on_behalf_of)

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
    http.cache_set_json(cache_key, data, settings.MGMT_REF_CACHE_TTL)
    return data

  return await _exec_single(spec, capability_id, params, on_behalf_of)

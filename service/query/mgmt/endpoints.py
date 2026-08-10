"""综合管理平台真实接口的声明式映射（method/path/参数位置/分页风格/取值路径）。

背景：文档核实结果（见 ~/Downloads/数据中心综合管理平台接口文档.md）显示 500+
接口的调用形态很不统一——GET/POST 混用、分页字段三套并存（page+pageCount /
page+pageSize(或 size) / pageNum+pageSize）、响应体 data 下挂列表的字段名各异
（rows/records/taskTickets/users…）。因此不能写一个通用 `POST /capabilities/{id}`
转发器，每条能力必须显式声明请求怎么发、响应怎么摘取。

`client.py` 只做「查 EndpointSpec → 组装请求 → 调 http.request → 按 result_path
摘取」，不含任何接口相关的硬编码逻辑，新增/调整接口只改这个文件。

字段名精确性说明：下面 12 条里，有 5 条（dept_tree/device_list/device_fault/
inspection_order/inspection_detail）文档只给出请求参数表，返回示例仅为 `{}`
占位、未列出真实字段结构——这些标 `result_path=''`（原样返回 data，不做摘取），
待联调拿到真实响应后再补 result_path 和字段裁剪，不能凭猜测硬编码字段名。
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EndpointSpec:
  """一条真实接口的调用规格。

  Attributes:
    method: HTTP 方法，GET/POST。
    path: 接口路径，可含 `{xxx}` 占位符（由 Capability 参数同名字段填充后从
      实际请求参数里剔除，不会被当作 query/body 参数重复传递）。
    param_location: 除 path 占位符外，其余参数放在 query 还是 body（json）。
    param_map: Capability 参数名 → 真实接口参数名的映射；未列出的参数名原样
      传递（即 Capability 参数名与接口字段名相同时无需在此声明）。
    page_style: 分页字段命名风格，用于告知调用方/未来扩展，当前仅作文档标记，
      实际字段名已通过 param_map 精确映射，不在此处做转换逻辑。
    result_path: 从响应 `data` 中取列表/详情的字段名；空字符串表示 data 结构
      未在文档中明确，原样返回 data（联调后再补充精确路径）。
  """

  method: str
  path: str
  param_location: str = 'query'
  param_map: dict[str, str] = field(default_factory=dict)
  page_style: str = 'none'
  result_path: str = ''


# 约定：Capability.params 用 snake_case（与项目现有 dcim/mgmt mock 命名习惯一致），
# param_map 必须覆盖**每一个**与真实接口字段名（多为 camelCase）不同的参数，
# 未列出的参数名会被原样传递——遗漏映射会导致请求悄悄发送错误字段名而不报错，
# 下面逐条都对照子任务精确核实的字段表检查过，无遗漏。
ENDPOINTS: dict[str, EndpointSpec] = {
  'mgmt.query_person': EndpointSpec(
    method='GET',
    path='/user/dept/v3/page',
    param_location='query',
    # 真实接口无独立工号参数，只能靠 deptId/name 过滤，employee_no 不映射
    # （见 capabilities.py 该能力的 params 注释，未声明此参数）。
    param_map={'department': 'deptId', 'page_count': 'pageCount'},
    page_style='page_count',  # page + pageCount（非常规命名，已在文档核实）
    result_path='users',
  ),
  'mgmt.query_dept_tree': EndpointSpec(
    method='GET',
    path='/user/dept/tree',
    param_location='query',
    param_map={'dept_id': 'deptId'},
    page_style='none',
    result_path='',  # 文档未给出真实返回结构，原样返回 data，联调后补充
  ),
  'mgmt.query_maintain_order': EndpointSpec(
    method='GET',
    path='/maintain/v1',
    param_location='query',
    param_map={
      'department': 'deptId',
      'device_type_id': 'deviceTypeId',
      'page_size': 'pageSize',
    },
    page_style='page_size',  # page + pageSize
    result_path='rows',
  ),
  'mgmt.query_task_order': EndpointSpec(
    method='POST',
    path='/tcm/task/list',
    param_location='json',
    param_map={
      'department': 'deptId',
      'page_size': 'size',
      'ticket_number': 'taskTicketNo',
      'executor_name': 'executorName',
      'template_name': 'templateName',
    },
    page_style='page_size_alt',  # page + size（非 pageSize，已在文档核实）
    result_path='taskTickets',
  ),
  'mgmt.query_device_list': EndpointSpec(
    method='GET',
    # dept_id 是 path 占位符，直接用 Capability 参数名，不经 param_map。
    path='/device/v1/device/list/deptid/{dept_id}',
    param_location='query',
    param_map={'device_group_parent_id': 'deviceGroupParentId'},
    page_style='none',
    result_path='',  # 文档未给出真实返回结构，原样返回 data，联调后补充
  ),
  'mgmt.query_device_detail': EndpointSpec(
    method='GET',
    path='/device/v1/{device_id}',
    param_location='query',
    param_map={},
    page_style='none',
    result_path='',
  ),
  'mgmt.query_device_fault': EndpointSpec(
    method='GET',
    # dept_id/page/page_size 是路径参数（真实接口把分页做进了 path），
    # 直接用 Capability 参数名，不经 param_map；其余走 query。
    path='/devicefault/v1/list/{dept_id}/{page}/{page_size}',
    param_location='query',
    param_map={'occurrence_place': 'occurrencePlace', 'group_id': 'groupId'},
    page_style='path',  # 分页字段在 path 里，不在 query/body
    result_path='',  # 文档未给出真实返回结构，原样返回 data，联调后补充
  ),
  'mgmt.query_inspection_order': EndpointSpec(
    method='POST',
    path='/inspection/ticket/get/list/v1',
    param_location='json',
    param_map={
      'department': 'deptId',
      'page_size': 'pageSize',
      'ticket_number': 'ticketNumber',
    },
    page_style='page_size',  # page + pageSize
    result_path='',  # 文档未给出真实返回结构，原样返回 data，联调后补充
  ),
  'mgmt.query_inspection_detail': EndpointSpec(
    method='GET',
    path='/inspection/ticket/get/v1/{ticket_id}',
    param_location='query',
    param_map={},
    page_style='none',
    result_path='',
  ),
  'mgmt.query_risk_list': EndpointSpec(
    method='POST',
    path='/risk/queryRiskList',
    param_location='json',
    # 真实接口还支持 riskCode/riskTypeId/时间范围/createdByMe 等筛选，本轮只暴露
    # 高频常用字段给 LLM（见 capabilities.py），避免参数过多增加调用出错概率，
    # 需要更多筛选维度时再补 param_map。
    param_map={
      'department': 'deptId',
      'page_num': 'pageNum',
      'page_size': 'pageSize',
      'risk_name': 'riskName',
      'risk_level': 'riskLevel',
      'discoverer_name': 'discovererName',
      'handler_name': 'handlerName',
    },
    page_style='pagenum_size',  # pageNum + pageSize
    result_path='records',
  ),
  'mgmt.query_risk_detail': EndpointSpec(
    method='GET',
    path='/risk/getRiskDetail',
    param_location='query',
    param_map={'risk_id': 'riskId'},
    page_style='none',
    result_path='baseInfo',
  ),
  'mgmt.query_weekly_report': EndpointSpec(
    method='GET',
    path='/weeklyReport/detail/{report_id}',
    param_location='query',
    param_map={},
    page_style='none',
    result_path='',
  ),
}

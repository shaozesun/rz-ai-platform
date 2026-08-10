"""综合管理平台能力数据（真实接口，Phase 2）。

12 条能力对应 endpoints.py 里声明的真实接口（不再是 mock）。全部只读查询——
mgmt.query_person_duty（值班查询）文档中不存在对应接口，mgmt.create_work_order
（创建工单）是写操作且需要前置查设备/人员 id，均已按用户确认从注册列表移除
（不写入本文件，避免产生「已登记但无法调用」的能力）。

params 字段名统一用 snake_case，与 endpoints.py 的 EndpointSpec.param_map 一一
对应——新增/改名参数时两个文件要同步改。

intent_labels 已顺手填上（person/work_order/device/inspection/risk/report 六类），
让 core/agent/query_planner.py 的多标签编排 Step1+2 快路径对 mgmt 查询生效
（此前 P1-1 修复：词汇表为空时会短路跳过意图识别，填了标签后自动恢复走查表
路由，不再是恒定 fallback 到 tool_search）。
"""

from service.query.base.capability import Capability

CAPABILITIES: tuple[Capability, ...] = (
  Capability(
    id='mgmt.query_person',
    name='查询人员信息',
    description=(
      '查询综合管理平台的人员基本信息，包括姓名、工号、部门、职位、联系方式。'
      '适用于「XX 是哪个部门的」「负责人是谁」「某部门有哪些人」等人员信息查询。'
    ),
    params={
      'name': '姓名关键字，模糊匹配；留空则按其他条件查',
      'department': '部门 id；留空查所有部门（无独立工号参数，工号查询需先按'
                     '部门/姓名缩小范围再人工核对）',
      'page': '页码，默认 1',
      'page_count': '每页条数，默认 10',
    },
    domain='person',
    kind='资源',
    intent_labels=('person',),
  ),
  Capability(
    id='mgmt.query_dept_tree',
    name='查询部门树',
    description=(
      '查询综合管理平台的部门组织结构树。适用于「有哪些部门」「XX 部门下面'
      '还分哪些科室」等组织架构查询，也可作为查人员/设备前先定位部门 id 的'
      '前置步骤。'
    ),
    params={
      'dept_id': '起始部门 id；留空查全部门树',
      'name': '部门名称关键字，模糊匹配；留空不按名称过滤',
    },
    domain='person',
    kind='资源',
    intent_labels=('person',),
  ),
  Capability(
    id='mgmt.query_maintain_order',
    name='查询维护工单',
    description=(
      '查询综合管理平台的设备维护工单列表，包括工单名称、状态、执行人、部门、'
      '创建/完成时间。适用于「有哪些维护工单」「XX 设备的维保记录」等查询。'
    ),
    params={
      'department': '部门 id，必填',
      'device_type_id': '设备类型 id，必填',
      'status': '工单状态（数字编码，具体取值需联调确认），必填',
      'type': '工单类型（数字编码，具体取值需联调确认），必填',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 10',
    },
    domain='work_order',
    kind='资源',
    intent_labels=('work_order',),
  ),
  Capability(
    id='mgmt.query_task_order',
    name='查询任务工单',
    description=(
      '查询综合管理平台的任务工单列表（区别于维护工单，覆盖更广的任务类工单），'
      '包括工单编号、状态、执行人、开始/结束时间。适用于「我有哪些待处理任务」'
      '「XX 负责的工单进度」等查询。'
    ),
    params={
      'department': '部门 id；留空查所有部门',
      'status': '工单状态（数字编码，具体取值需联调确认）；留空查所有状态',
      'ticket_number': '工单编号；留空不按编号过滤',
      'executor_name': '执行人姓名；留空查所有人',
      'template_name': '工单模板名称；留空不按模板过滤',
      'type': '工单类型（数字编码）；留空查所有类型',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 10',
    },
    domain='work_order',
    kind='资源',
    intent_labels=('work_order',),
  ),
  Capability(
    id='mgmt.query_device_list',
    name='查询部门设备列表',
    description=(
      '查询综合管理平台指定部门下的设备台账列表。适用于「XX 部门有哪些设备」'
      '「机房设备清单」等查询。'
    ),
    params={
      'dept_id': '部门 id，必填',
      'device_group_parent_id': '设备分组父 id；留空查该部门全部设备',
    },
    domain='device',
    kind='资源',
    intent_labels=('device',),
  ),
  Capability(
    id='mgmt.query_device_detail',
    name='查询设备详情',
    description=(
      '按设备 id 查询综合管理平台的设备详细信息（型号、厂商、序列号、额定容量等）。'
      '适用于「XX 设备的详细信息」等查询，需先通过部门设备列表拿到设备 id。'
    ),
    params={
      'device_id': '设备 id，必填',
    },
    domain='device',
    kind='资源',
    intent_labels=('device',),
  ),
  Capability(
    id='mgmt.query_device_fault',
    name='查询设备故障记录',
    description=(
      '按部门分页查询综合管理平台的设备故障记录，支持按等级/名称/编号/发生地点/'
      '状态过滤。适用于「XX 机房最近有哪些故障」「设备故障历史」等查询。'
    ),
    params={
      'dept_id': '部门 id，必填',
      'page': '页码，必填',
      'page_size': '每页条数，必填',
      'level': '故障等级；留空查所有等级',
      'name': '故障名称关键字；留空不按名称过滤',
      'number': '故障编号；留空不按编号过滤',
      'occurrence_place': '发生地点关键字；留空不按地点过滤',
      'group_id': '设备分组 id；留空查所有分组',
      'status': '故障状态（数字编码）；留空查所有状态',
    },
    domain='device',
    kind='状态',
    intent_labels=('device',),
  ),
  Capability(
    id='mgmt.query_inspection_order',
    name='查询巡检工单',
    description=(
      '查询综合管理平台的巡检工单列表，包括工单名称、编号、状态、类型。'
      '适用于「有哪些巡检任务」「巡检工单进度」等查询。'
    ),
    params={
      'department': '部门 id；留空查所有部门',
      'name': '工单名称关键字；留空不按名称过滤',
      'status': '工单状态（数字编码）；留空查所有状态',
      'ticket_number': '工单编号；留空不按编号过滤',
      'type': '工单类型（数字编码）；留空查所有类型',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 10',
    },
    domain='inspection',
    kind='资源',
    intent_labels=('inspection',),
  ),
  Capability(
    id='mgmt.query_inspection_detail',
    name='查询巡检报告详情',
    description=(
      '按工单 id 查询综合管理平台的巡检报告详情。适用于「XX 巡检工单的详细'
      '内容」等查询，需先通过巡检工单列表拿到工单 id。'
    ),
    params={
      'ticket_id': '巡检工单 id，必填',
    },
    domain='inspection',
    kind='资源',
    intent_labels=('inspection',),
  ),
  Capability(
    id='mgmt.query_risk_list',
    name='查询风险隐患列表',
    description=(
      '查询综合管理平台的风险隐患列表，包括风险名称、等级、状态、发现人、'
      '处理人。适用于「有哪些安全隐患」「XX 等级的风险有多少」等查询。'
    ),
    params={
      'department': '部门 id；留空查所有部门',
      'risk_name': '风险名称关键字；留空不按名称过滤',
      'risk_level': '风险等级（数字编码）；留空查所有等级',
      'discoverer_name': '发现人姓名；留空不按发现人过滤',
      'handler_name': '处理人姓名；留空不按处理人过滤',
      'status': '风险状态；留空查所有状态',
      'page_num': '页码，默认 1',
      'page_size': '每页条数，默认 10',
    },
    domain='risk',
    kind='资源',
    intent_labels=('risk',),
  ),
  Capability(
    id='mgmt.query_risk_detail',
    name='查询风险隐患详情',
    description=(
      '按风险 id 查询综合管理平台的隐患详情（含处理记录、审批进度、附件）。'
      '适用于「XX 隐患的处理进度」等查询，需先通过风险隐患列表拿到风险 id。'
    ),
    params={
      'risk_id': '风险 id，必填',
    },
    domain='risk',
    kind='资源',
    intent_labels=('risk',),
  ),
  Capability(
    id='mgmt.query_weekly_report',
    name='查询周报详情',
    description=(
      '按周报 id 查询综合管理平台的周报详情（本周工作、下周计划、问题记录）。'
      '适用于「XX 的周报内容」等查询。'
    ),
    params={
      'report_id': '周报 id，必填',
    },
    domain='report',
    kind='资源',
    intent_labels=('report',),
  ),
)

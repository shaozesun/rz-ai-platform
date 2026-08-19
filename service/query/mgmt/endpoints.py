"""综合管理平台真实接口的声明式映射（method/path/参数位置/取值路径）。

33 条 EndpointSpec（与 capabilities.py 的 38 条能力对应：33 条单接口一一映射，
另 5 条 query_pending_ticket / query_change_ticket / query_breakdown_ticket_detail
/ query_question_ticket_detail / query_event_ticket_detail 为组合能力，无
EndpointSpec，走 client.call 特判；30 条经联调核实，另 3 条文档登记待实测）：
- query_person_by_name：GET /user/info/v2/{name}，data 即用户对象数组，原样返回。
- query_person_by_id：POST /user/records/v2，body {uid,deptId,companyId}，
  result_path 取 user；deptId/companyId 走服务账号默认上下文兜底。
- query_employee_ticket：POST /user/records/ticket/v1，body {companyId,page,size,
  ticketType,userId}，result_path 取 ticketVOList。
- query_person_training：POST /user/records/training/v1，data 即 {userInfo,
  studyDurations}，无分页，原样返回。
- query_person_maintenance_plan：POST /user/records/maintenanceplan/v1，分页
  page+pageSize，result_path 取 maintenancePlanList；deptId 必填且须用人员档案的
  ownDeptId（从按姓名查询结果获取），不能用服务账号上下文兜底。
- query_person_inspection：POST /user/records/inspection/v1，分页 page+pageSize，
  result_path 取 inspectionVOList；deptId/companyId 同 maintenance_plan。
- query_inspection_list：POST /inspection/inspection/search/v1，base URL 是根路径
  （无 /center 前缀，走 MGMT_INSPECTION_BASE_URL），分页嵌套 page:{page,pageSize}
  （page_nested），时间戳毫秒 long（time_to_ms 转换），result_path 取 list，分页
  元数据仅 total；type=1 计划巡检 / 0 非计划巡检，archive 固定 true。
- query_inspection_device_type：POST /inspection/device/type/search/v3，base URL 同上
  （根路径；GET 返 405）。body {company,name,floor,deviceTypeId,page,pageSize} 全可选
  （空 body 也返回全部），分页 page+pageSize **平铺**（非嵌套），信封 {pageSize,
  pageCounts,count,deviceTypeVOS}，result_path 取 deviceTypeVOS，page_meta 保留
  count/pageSize/pageCounts；name 模糊、deviceTypeId 精确过滤，company/floor 实测
  不影响结果（设备类型字典 48 种）。
- query_inspection_device_type_v2：POST /inspection/device/type/search/v2，base URL 同上
  （根路径；GET 返 405）。与 v3 同一份设备类型字典，区别是 v2 返回**纯数组**（无分页
  信封），body 全可选，name 模糊/deviceTypeId 精确过滤同样生效；result_path 留空
  （data 即数组）、无 page_meta、无 default_params。
- query_inspection_item：POST /inspection/picture/type/search/v3，base URL 同上
  （根路径；GET 返 405）。body {floor,pictureName,deviceTypeId,company,page,pageSize}
  全字段可选，分页 page+pageSize 平铺，信封 {pageSize,pageCounts,count,pictureTypeVOS}，
  result_path 取 pictureTypeVOS，page_meta 保留 count/pageSize/pageCounts；pictureName
  巡检点位名称模糊、deviceTypeId 设备类型精确过滤（巡检项模板，全量约 54 个点位）。
- query_inspection_ticket（文档登记，待实测）：POST /inspection/ticket/get/list/v1，
  base URL 同上（根路径，走 MGMT_INSPECTION_BASE_URL）。body {deptId,name,page,pageSize,
  status,ticketNumber,type} 全可选，type 0=所有/1=我巡检的/2=我审核的；分页 page+pageSize
  平铺，result_path/page_meta 为猜测值待实测修正。
- query_inspection_ticket_detail（文档登记，待实测）：GET /inspection/ticket/get/detail/v1/{id}，
  base URL 同上。id 在 path 占位符（ticket_id 经 param_map 反查），data 即工单明细 VO
  （basicInfo + abnormalStatList），原样返回。
- query_dept_by_name：GET /dept/v1/dept/{deptName}，data 即部门对象数组，原样返回
  （底层能力，供 agent 按部门名解析 deptId）。path 占位符 {deptName} 与 Capability
  参数 dept_name（snake_case）不同名，由 client._build_request 经 param_map 反查对接。
- query_equipment_maintenance_plan：POST /plan/list/plan/infoV3，body {deptId,year,
  isNew,deviceName?,parentGroupId?}。deptId 走服务账号默认部门兜底（defaults），year
  动态默认当前年（default_params 的 @current_year 占位符），isNew 固定 '1'；
  parentGroupId 中文→code 值映射（value_map：供配电/制冷/智能化）；result_path 取
  detailList，planMain 一并保留（page_meta）；无分页。
- query_equipment_maintenance_status：GET /plan/status/{year}/{companyId}，year/companyId
  在 path 占位符（company_id 经 param_map 反查，year 同名直接填），data 即各分部状态
  数组（status 0/1），无分页，原样返回；两个占位符必填（缺任一 404，defaults 兜底
  不到 path 占位符）。
- query_equipment_work_order：GET /maintain/v2，query {deptId,page,pageSize,year?,week?,
  status?,type?,planWeekId?,parentDeviceTypeId?,maintenanceNum?,maintenanceMainNum?}（注意
  POST 同路径是 addMaintenancePlanV2 写入接口，勿混）。信封 {totalPage,totalCount,rows}
  （非 MyBatis-Plus 的 records/total），分页 page+pageSize，result_path 取 rows，
  page_meta 保留 totalPage/totalCount；deptId 必填（缺 400）走 own_dept_id 兜底，
  其余全可选（maintenanceNum 按子单号 maintenancePlanNum 过滤、maintenanceMainNum 按
  主单号过滤返回同主单全部子单，year 留空查全部年份，status 0=未执行/3=已完成）。
- query_device_info：POST /plan/list/device/info，body {deptId,page,size,typeId}，
  分页 page + size，result_path 取 records，page_meta 保留 total/current/pages/size；
  deptId 必填（agent 未指明部门时询问用户，不默认兜底）。通用设备台账，红外关联
  字段（deviceTypeId/association/upperLimitValue）恒为空。
- query_infrared_device_type：GET /infrared/device/type/list/v1/{deptId}，deptId 在
  path 占位符（param_map 反查填充，无 defaults 兜底），data 即红外设备类型数组
  （id/name/defaultUpperLimitValue，实测 4 种），原样返回，无分页。
- query_infrared_device_list：POST /infrared/device/list/v1，body {infraredDeviceTypeId,
  deptId,page,pageSize}。infraredDeviceTypeId 必填（不带实测「服务器错误」500），
  deptId 走 own_dept_id 兜底。信封 {pageSize,pageCounts,count,infraredDeviceVOList}，
  result_path 取 infraredDeviceVOList，page_meta 保留 count/pageSize/pageCounts。
- query_infrared_detect_device：POST /plan/list/device/info/v2（v1 的 v2，实测 v1/v2
  返回同一批设备、字段集一致，区别仅 v2 填充红外关联字段：deviceTypeId=该设备对应
  红外类型 id、association=是否已纳入检测、upperLimitValue=温度上限，v1 三者恒空）。
  body {deptId,page,size}，分页 page + size，信封同 v1（MyBatis-Plus
  records/total/current/pages/size），result_path 取 records，page_meta 保留分页元数据；
  default_params 兜底 size=10000（红外检测设备清单场景要一次拿全部）。
- query_device_group：GET /deviceGroup/v1/dept/{deptId}，deptId 在 path 占位符
  （param_map 反查填充），data 即设备分组数组（专业类别 + 下挂设备组），原样返回；
  deptId 必填。
- query_device_system_tree：GET /equipment/get/system/tree/{deptId}，deptId 在 path
  占位符（param_map 反查填充），data 即系统类型树数组（一级系统 + 二级子系统，节点
  id 即下钻 pid），无分页，原样返回；deptId 必填（ownDeptId）。
- query_device_logic_type：POST /equipment/get/logic/type（URL 含 "get" 但方法是
  POST），body {pid,deptId,page,pageSize}，分页 page+pageSize，result_path 取
  equipmentLogicTypeVOList，page_meta 保留 count/pageSize/pageCounts；pid 必填，
  deptId 走服务账号默认部门兜底。
- query_device_major：POST /equipment/get/dept（URL 含 "get" 但方法是 POST），deptId
  在 query（param_map 反查），data 即专业类别数组（供配电/制冷/智能化），无分页，
  原样返回；deptId 走服务账号默认部门兜底（defaults，query 参数适用）。
- query_device_type：POST /equipment/list，body {deptId,page,size,name?,parentId?}，
  分页 page + size（信封与 query_device_info 一致：records/total/current/pages/size），
  result_path 取 records，page_meta 保留分页元数据；deptId 走默认部门兜底，name/
  parentId 为可选过滤。
- query_company_list：GET /authority/userrole/v1/current/company，无参数，
  data 即公司对象数组，原样返回（供 agent 取 company_id / 查公司列表）。
- query_user_list：GET /user/dept/v4/page，query {deptId,page,pageCount}，
  result_path 取 users，page_meta 保留 count/pageCounts/pageSize；deptId 必填。
- query_dept_list：GET /authority/userrole/v3/current/dept，query {companyId}，
  companyId 走服务账号默认公司兜底（defaults），data 即部门数组原样返回。
- query_mindmap：GET /file/v1/getAll/{id}，id 在 path 占位符（file_id 经 param_map
  反查填充），data 即思维导图树对象原样返回，无分页。
- query_kb_article：POST /kbArticle/getIsSharedKbArticleList，body
  {page,size,title,tagId}（MyBatis-Plus 信封 records/total/current/pages/size，
  page+size 风格，page_size_alt），title 按标题关键字模糊过滤（实测 title=误操作
  仅返回标题含「误操作」的文章）、tagId 按标签过滤（实测 tagId=3 → 行业案例），
  两者不传返回全部，分页元数据 total/current/pages/size 保留。
- query_kb_standard（文档登记）：POST /kbNationalStandard/getIsSharedStandardList，
  body {page,size,tagId,title}（MyBatis-Plus 信封 current/pages/records/size/total，
  page+size 风格，page_size_alt），title 按标题关键字模糊过滤、tagId 按标签过滤，
  均可选；不传返回全部，分页元数据 total/current/pages/size 保留。

其余接口（部门树/隐患/周报等）已移除。`client.py` 只做「查 EndpointSpec →
组装请求 → 调 http.request → 按 result_path 摘取」，不含任何接口相关的硬编码逻辑，
新增/调整接口只改这个文件。
"""

from dataclasses import dataclass, field

from config.settings import settings


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
    page_style: 分页字段命名风格；'none' 表示无分页——client 据此剥离调用方
      注入的 page/page_size 等分页参数（如 analysis_load 物化全量时会无条件注入，
      透传会让无分页 DTO 反序列化失败）。分页接口的字段名仍由 param_map 精确映射。
    result_path: 从响应 `data` 中取列表/详情的字段名；空字符串表示 data 即
      目标结构（如 query_person_by_name 的 data 就是用户数组），原样返回 data。
    defaults: 接口字段名 → 服务账号默认上下文键（'own_dept_id' / 'company_id'）。
      调用方未传该字段时，client 用登录缓存的服务账号上下文兜底（如 records/v2
      的 deptId 必须用父部门 ownDeptId，LLM 不会知道这些值）。
    page_meta: result_path 摘取后需要**一并保留**的响应字段（分页元数据，如
      count/pageSize/pageCounts）。不声明则只返回 result_path 摘取的结果，分页
      信息会丢失，LLM 无法告诉用户总条数。字段名按真实接口响应核对后声明。
    default_params: **字面量兜底**——字段缺失/为空时填固定值（如分页接口必须带
      page/pageSize，实测不带报「服务器错误」，而 LLM 不一定传）。区别于 defaults：
      defaults 从服务账号登录缓存取上下文（own_dept_id/company_id），这里直接填
      常量字符串。
    base_url: 覆盖 base URL（如巡检服务在根路径 /inspection，无 /center 前缀）；
      空串用 settings.MGMT_BASE_URL。
    page_nested: 分页包成 `page:{page,pageSize}` 嵌套对象（区别于分页字段平铺在
      body 顶层）；default_params 里的 page/pageSize 也会被包进去。
    time_to_ms: 值需转毫秒时间戳的**接口字段名**（如 ('startTime','endTime')）。
      值为可读日期串（YYYY-MM-DD[ HH:MM:SS]）时由 client 转毫秒；已是数值/不可
      解析则原样透传（接口要求 long，日期字符串会报 400）。
    value_map: 字段名 → {能力参数值: 接口值} 的**值映射**（如 parentGroupId 的
      中文类别名 → code）。接口只认 code、agent 只会给中文名时用；client 在
      param_map 映射后应用，值不在映射表时原样透传。
  """

  method: str
  path: str
  param_location: str = 'query'
  param_map: dict[str, str] = field(default_factory=dict)
  page_style: str = 'none'
  result_path: str = ''
  defaults: dict[str, str] = field(default_factory=dict)
  page_meta: tuple[str, ...] = field(default_factory=tuple)
  default_params: dict[str, str] = field(default_factory=dict)
  base_url: str = ''
  page_nested: bool = False
  time_to_ms: tuple[str, ...] = field(default_factory=tuple)
  value_map: dict[str, dict[str, str]] = field(default_factory=dict)


# 约定：Capability.params 用 snake_case（与项目现有 dcim/mgmt mock 命名习惯一致），
# param_map 必须覆盖**每一个**与真实接口字段名（多为 camelCase）不同的参数，
# 未列出的参数名会被原样传递——遗漏映射会导致请求悄悄发送错误字段名而不报错。
ENDPOINTS: dict[str, EndpointSpec] = {
  # query_person_by_name 实测：GET /user/info/v2/{name}，data 为 user 对象数组，模糊匹配；
  # name 是 path 占位符，中文由 client 做 URL 编码。
  'mgmt.query_person_by_name': EndpointSpec(
    method='GET',
    path='/user/info/v2/{name}',
    param_location='query',
    param_map={},
    page_style='none',
    result_path='',  # data 即数组，原样返回
  ),
  # query_person_by_id 实测：POST /user/records/v2，body {uid,deptId,companyId}；
  # deptId 必须传父部门 ownDeptId（只传 uid 报「部门不存在」），companyId 必传
  # （报「公司不存在」）——两者都从登录缓存的服务账号上下文兜底（defaults）。
  'mgmt.query_person_by_id': EndpointSpec(
    method='POST',
    path='/user/records/v2',
    param_location='json',
    param_map={'dept_id': 'deptId', 'company_id': 'companyId'},
    defaults={'deptId': 'own_dept_id', 'companyId': 'company_id'},
    page_style='none',
    result_path='user',
  ),
  # query_employee_ticket：POST /user/records/ticket/v1，body {companyId,page,size,
  # ticketType,userId}，查询员工变更工单（入职/调岗/离职等员工信息变更记录）。
  # companyId 必须用员工本人所属公司（从按姓名查询结果中与 uid 一并获取），
  # 不能用服务账号默认公司兜底——不同员工可能属不同公司；由 agent 先查人员信息再传。
  # 分页 page + size。返回结构已验证 data 含 ticketVOList/pageSize/pageCounts/count，
  # result_path 取 ticketVOList；page_meta 把分页元数据一并保留给 LLM（否则只拿到
  # 一页列表，LLM 无法告知用户总条数，会把当前页当成全部）。
  'mgmt.query_employee_ticket': EndpointSpec(
    method='POST',
    path='/user/records/ticket/v1',
    param_location='json',
    param_map={
      'company_id': 'companyId',
      'ticket_type': 'ticketType',
      'user_id': 'userId',
      'page_size': 'size',
    },
    page_style='page_size_alt',  # page + size
    result_path='ticketVOList',
    page_meta=('count', 'pageSize', 'pageCounts'),
  ),
  # query_person_training 实测：POST /user/records/training/v1，body {uid,year,
  # startTime,endTime,isPass,month,planStatus}。data 即 {userInfo, studyDurations}
  # （培训时长/完成度/每日学习时长），无分页。uid/year/month 必须带（实测缺任一
  # 返回空 data，缺 startTime/endTime 也返回空）；startTime/endTime 给定时宽范围
  # 即可（跨年也按 year+month 过滤），故用 default_params 兜底为宽范围，LLM 只
  # 需传 uid+year+month。
  'mgmt.query_person_training': EndpointSpec(
    method='POST',
    path='/user/records/training/v1',
    param_location='json',
    param_map={
      'start_time': 'startTime',
      'end_time': 'endTime',
      'is_pass': 'isPass',
      'plan_status': 'planStatus',
    },
    page_style='none',
    result_path='',  # data 即 {userInfo, studyDurations}，原样返回
    default_params={
      'startTime': '2020-01-01 00:00:00',
      'endTime': '2030-12-31 23:59:59',
    },
  ),
  # query_person_maintenance_plan 实测：POST /user/records/maintenanceplan/v1，
  # body {uid,deptId,companyId,page,pageSize}。deptId 必填且必须用人员档案的
  # ownDeptId（by-name 返回的 deptId 或服务账号默认 own_dept_id 均报「部门不存在/
  # 服务器错误」，实测）；companyId 用人员所属公司。page/pageSize 必须显式带上
  # （不带报「服务器错误」），LLM 不一定传，用 default_params 兜底 page=1,size=20。
  # 返回 data 含 maintenancePlanList/pageSize/pageCounts/count，取 maintenancePlanList。
  'mgmt.query_person_maintenance_plan': EndpointSpec(
    method='POST',
    path='/user/records/maintenanceplan/v1',
    param_location='json',
    param_map={'dept_id': 'deptId', 'company_id': 'companyId', 'page_size': 'pageSize'},
    page_style='page_pagesize',  # page + pageSize
    result_path='maintenancePlanList',
    page_meta=('count', 'pageSize', 'pageCounts'),
    default_params={'page': '1', 'pageSize': '20'},
  ),
  # query_person_inspection 实测：POST /user/records/inspection/v1，body 与返回
  # 信封同 maintenanceplan，仅列表字段为 inspectionVOList。
  'mgmt.query_person_inspection': EndpointSpec(
    method='POST',
    path='/user/records/inspection/v1',
    param_location='json',
    param_map={'dept_id': 'deptId', 'company_id': 'companyId', 'page_size': 'pageSize'},
    page_style='page_pagesize',  # page + pageSize
    result_path='inspectionVOList',
    page_meta=('count', 'pageSize', 'pageCounts'),
    default_params={'page': '1', 'pageSize': '20'},
  ),
  # query_inspection_list 实测：POST /inspection/inspection/search/v1（base URL 是
  # 根路径 http://192.168.88.18，无 /center 前缀，走 MGMT_INSPECTION_BASE_URL）。
  # body 全字段可选（空 body 返回全部），分页为嵌套 page:{page,pageSize}（字符串
  # 值可用），时间戳必须毫秒 long（日期字符串报 400，用 time_to_ms 转换）。
  # type=1 计划巡检 / 0 非计划巡检；archive 固定 true 只查已归档。响应 {total, list}。
  'mgmt.query_inspection_list': EndpointSpec(
    method='POST',
    path='/inspection/inspection/search/v1',
    base_url=settings.MGMT_INSPECTION_BASE_URL,
    param_location='json',
    param_map={
      'room_type': 'roomType',
      'employee_name': 'employeeName',
      'start_time': 'startTime',
      'end_time': 'endTime',
      'dept_name': 'deptName',
      'page_size': 'pageSize',
    },
    page_style='page_nested',  # page:{page,pageSize}
    page_nested=True,
    time_to_ms=('startTime', 'endTime'),
    result_path='list',
    page_meta=('total',),
    default_params={'page': '1', 'pageSize': '20', 'archive': 'true'},
  ),
  # query_inspection_device_type（实测）：POST /inspection/device/type/search/v3（base URL
  # 是根路径 http://192.168.88.18，无 /center 前缀，走 MGMT_INSPECTION_BASE_URL；GET 返
  # 405）。body {company,name,floor,deviceTypeId,page,pageSize} 全字段可选（空 body 也返回
  # 全部 48 种）。name 模糊、deviceTypeId 精确过滤；company/floor 实测不影响结果（设备
  # 类型字典）。分页 page+pageSize **平铺**（区别于 query_inspection_list 的嵌套
  # page:{page,pageSize}）。信封 {pageSize,pageCounts,count,deviceTypeVOS}，result_path
  # 取 deviceTypeVOS，page_meta 保留 count/pageSize/pageCounts。
  'mgmt.query_inspection_device_type': EndpointSpec(
    method='POST',
    path='/inspection/device/type/search/v3',
    base_url=settings.MGMT_INSPECTION_BASE_URL,
    param_location='json',
    param_map={'device_type_id': 'deviceTypeId', 'page_size': 'pageSize'},
    page_style='page_pagesize',  # page + pageSize 平铺
    result_path='deviceTypeVOS',
    page_meta=('count', 'pageSize', 'pageCounts'),
    default_params={'page': '1', 'pageSize': '20'},
  ),
  # query_inspection_device_type_v2（实测）：POST /inspection/device/type/search/v2，
  # base URL 是根路径（走 MGMT_INSPECTION_BASE_URL；GET 返 405）。与 v3 同一份设备
  # 类型字典，区别是 v2 返回**纯数组**（无分页信封），body 全可选；name 模糊、
  # deviceTypeId 精确过滤同样生效。result_path 留空（data 即数组）、无 page_meta、
  # 无 default_params（无分页概念）。
  'mgmt.query_inspection_device_type_v2': EndpointSpec(
    method='POST',
    path='/inspection/device/type/search/v2',
    base_url=settings.MGMT_INSPECTION_BASE_URL,
    param_location='json',
    param_map={'device_type_id': 'deviceTypeId'},
    page_style='none',
    result_path='',  # data 即纯数组，原样返回
  ),
  # query_inspection_item（实测）：POST /inspection/picture/type/search/v3（base URL
  # 是根路径，走 MGMT_INSPECTION_BASE_URL；GET 返 405）。body {floor,pictureName,
  # deviceTypeId,company,page,pageSize} 全字段可选。pictureName 巡检点位名称模糊、
  # deviceTypeId 设备类型精确过滤。信封 {pageSize,pageCounts,count,pictureTypeVOS}，
  # result_path 取 pictureTypeVOS，page_meta 保留 count/pageSize/pageCounts；分页
  # page+pageSize 平铺。
  'mgmt.query_inspection_item': EndpointSpec(
    method='POST',
    path='/inspection/picture/type/search/v3',
    base_url=settings.MGMT_INSPECTION_BASE_URL,
    param_location='json',
    param_map={'picture_name': 'pictureName', 'device_type_id': 'deviceTypeId', 'page_size': 'pageSize'},
    page_style='page_pagesize',  # page + pageSize 平铺
    result_path='pictureTypeVOS',
    page_meta=('count', 'pageSize', 'pageCounts'),
    default_params={'page': '1', 'pageSize': '20'},
  ),
  # query_dept_by_name 实测：GET /dept/v1/dept/{deptName}，data 为部门对象数组，
  # 模糊匹配；成功码 code=1。path 占位符 {deptName} 与 Capability 参数 dept_name
  # （snake_case）不同名，由 client._build_request 经 param_map 反查填充并做
  # URL 编码。
  'mgmt.query_dept_by_name': EndpointSpec(
    method='GET',
    path='/dept/v1/dept/{deptName}',
    param_location='query',
    param_map={'dept_name': 'deptName'},
    page_style='none',
    result_path='',  # data 即部门数组，原样返回
  ),
  # query_equipment_maintenance_plan 实测：POST /plan/list/plan/infoV3，body
  # {deptId,year,isNew,deviceName?,parentGroupId?}。year 必填（不带报 HTTP 400，
  # 传字符串可用）→ default_params 的 @current_year 占位符由 client 替换为当年；
  # isNew 不传返回空数据 → 固定 '1'。deptId 用服务账号默认部门 own_dept_id 兜底
  # （agent 按用户问题中的部门先用 query_dept_by_name 解析后显式传值覆盖）。
  # parentGroupId 接口只认 code，agent 只会给中文类别名 → value_map 中文→code。
  # 响应 data={planMain, detailList}，result_path 取 detailList，planMain（部门/
  # 年份汇总）经 page_meta 一并保留；无分页。
  'mgmt.query_equipment_maintenance_plan': EndpointSpec(
    method='POST',
    path='/plan/list/plan/infoV3',
    param_location='json',
    param_map={
      'dept_id': 'deptId',
      'device_name': 'deviceName',
      'parent_group': 'parentGroupId',
    },
    value_map={
      'parentGroupId': {
        '供配电': '2AySJZkc31m',
        '制冷': '2AySJZbc31m',
        '智能化': '2AySJZkc33m',
      },
    },
    page_style='none',
    defaults={'deptId': 'own_dept_id'},
    default_params={'isNew': '1', 'year': '@current_year'},
    result_path='detailList',
    page_meta=('planMain',),
  ),
  # query_equipment_maintenance_status（实测）：GET /plan/status/{year}/{companyId}，
  # year/companyId 在 path 占位符（company_id 经 param_map 反查，year 同名直接填），
  # data 即各分部状态数组（status 0/1，含 deptName/deptId/companyId），无分页，原样
  # 返回。两个占位符必填（缺任一实测 404；defaults 只兜底 body/query 参数，够不到
  # path 占位符）。
  'mgmt.query_equipment_maintenance_status': EndpointSpec(
    method='GET',
    path='/plan/status/{year}/{companyId}',
    param_location='query',
    param_map={'company_id': 'companyId'},
    page_style='none',
    result_path='',  # data 即分部状态数组，原样返回
  ),
  # query_equipment_work_order（实测）：GET /maintain/v2，query {deptId,page,pageSize,
  # year?,week?,status?,type?,planWeekId?,parentDeviceTypeId?,maintenanceNum?,
  # maintenanceMainNum?}。注意 POST 同路径是 addMaintenancePlanV2 写入接口，勿混。
  # deptId 必填（缺 400）走 own_dept_id 兜底（query 参数适用，defaults）；page/pageSize
  # 可选（缺省返回全部）→ default_params 兜底 1/20。信封 {totalPage,totalCount,rows}
  # （非 MyBatis-Plus 的 records/total/current/pages/size），分页 page+pageSize，
  # result_path 取 rows，page_meta 保留 totalPage/totalCount。
  # maintenanceNum 按子单号（maintenancePlanNum）过滤、maintenanceMainNum 按主单号过滤
  # （返回该主单下全部子单）；year 留空查全部年份；status 实测 0=未执行/3=已完成。
  'mgmt.query_equipment_work_order': EndpointSpec(
    method='GET',
    path='/maintain/v2',
    param_location='query',
    param_map={
      'dept_id': 'deptId',
      'page_size': 'pageSize',
      'maintenance_num': 'maintenanceNum',
      'maintenance_main_num': 'maintenanceMainNum',
    },
    page_style='page_pagesize',  # page + pageSize
    defaults={'deptId': 'own_dept_id'},
    default_params={'page': '1', 'pageSize': '20'},
    result_path='rows',
    page_meta=('totalPage', 'totalCount'),
  ),
  # query_device_info（接口文档）：POST /plan/list/device/info，body
  # {deptId,page,size,typeId}。分页 page + size（平铺，非嵌套，同
  # query_employee_ticket）。deptId 必填且同时作为 Deptid header（_build_request
  # 自动处理），不兜底默认部门（LLM 未传时客户端不补，agent 需询问用户）。
  # typeId 可选（设备类型 id）。响应 data={current,pages,records,size,total}，
  # result_path 取 records，page_meta 保留分页元数据（total 等）。
  'mgmt.query_device_info': EndpointSpec(
    method='POST',
    path='/plan/list/device/info',
    param_location='json',
    param_map={'dept_id': 'deptId', 'page_size': 'size', 'type_id': 'typeId'},
    page_style='page_size_alt',  # page + size
    result_path='records',
    page_meta=('total', 'current', 'pages', 'size'),
    default_params={'page': '1', 'size': '20'},
  ),
  # query_config_template_list（实测）：POST /tck/cfg/template/list，body
  # {page,size,templateName,type}。配置管理模板列表（专项检查表/固定模板）。
  # 分页 page + size 平铺；templateName 模糊、type 精确过滤，均可选。信封
  # {listVO,page,size,total}，result_path 取 listVO，page_meta 保留 total。
  'mgmt.query_config_template_list': EndpointSpec(
    method='POST',
    path='/tck/cfg/template/list',
    param_location='json',
    param_map={'template_name': 'templateName', 'page_size': 'size'},
    page_style='page_size_alt',  # page + size
    default_params={'page': '1', 'size': '20'},
    result_path='listVO',
    page_meta=('total',),
  ),
  # query_task_list（实测）：POST /tcm/task/list，body {page,size,type,deptId,
  # templateName?,status?,executorName?,taskTicketNo?}。配置模板生成的任务工单列表。
  # type 必填（不带报「服务器错误」；1=全部有数据，2/3/4 空）→ default_params 兜底 '1'；
  # deptId 用服务账号默认部门 own_dept_id 兜底（user 示例即 ownDeptId）。templateName
  # 模糊、status/executorName/taskTicketNo 精确过滤，均可选。信封 {taskTickets,page,size,
  # total}，result_path 取 taskTickets，page_meta 保留 total。
  'mgmt.query_task_list': EndpointSpec(
    method='POST',
    path='/tcm/task/list',
    param_location='json',
    param_map={
      'dept_id': 'deptId',
      'template_name': 'templateName',
      'executor_name': 'executorName',
      'task_ticket_no': 'taskTicketNo',
      'page_size': 'size',
    },
    page_style='page_size_alt',  # page + size
    defaults={'deptId': 'own_dept_id'},
    default_params={'page': '1', 'size': '20', 'type': '1'},
    result_path='taskTickets',
    page_meta=('total',),
  ),
  # query_infrared_device_type（实测）：GET /infrared/device/type/list/v1/{deptId}，
  # deptId 在 path 占位符（param_map 反查填充，同 query_device_group；path 占位符
  # defaults 兜底不到，dept_id 必填）。data 即红外设备类型数组（id/name/
  # defaultUpperLimitValue，实测 4 种：测试 33/嵌入式服务器 65.5/变压器 100/高压柜
  # 25.5），无分页，原样返回。
  'mgmt.query_infrared_device_type': EndpointSpec(
    method='GET',
    path='/infrared/device/type/list/v1/{deptId}',
    param_location='query',
    param_map={'dept_id': 'deptId'},
    page_style='none',
    result_path='',  # data 即类型数组，原样返回
  ),
  # query_infrared_device_list（实测）：POST /infrared/device/list/v1，body
  # {infraredDeviceTypeId,deptId,page,pageSize}。infraredDeviceTypeId 必填（不带实测
  # 「服务器错误」500；传真实类型 id 过滤生效，传 "1" 返回全量）；deptId 走
  # own_dept_id 兜底（body 参数，defaults 适用）。信封 {pageSize,pageCounts,count,
  # infraredDeviceVOList}，result_path 取 infraredDeviceVOList，page_meta 保留
  # count/pageSize/pageCounts；分页 page+pageSize 平铺。
  'mgmt.query_infrared_device_list': EndpointSpec(
    method='POST',
    path='/infrared/device/list/v1',
    param_location='json',
    param_map={
      'infrared_device_type_id': 'infraredDeviceTypeId',
      'dept_id': 'deptId',
      'page_size': 'pageSize',
    },
    page_style='page_pagesize',  # page + pageSize 平铺
    defaults={'deptId': 'own_dept_id'},
    default_params={'page': '1', 'pageSize': '20'},
    result_path='infraredDeviceVOList',
    page_meta=('count', 'pageSize', 'pageCounts'),
  ),
  # query_infrared_detect_device（实测）：POST /plan/list/device/info/v2。与 v1
  # （query_device_info）同参数 body {deptId,page,size}、同 MyBatis-Plus 信封
  # records/total/current/pages/size，实测 v1/v2 返回同一批设备、字段集一致，唯一
  # 区别是 v2 填充红外关联字段（deviceTypeId=该设备对应红外类型 id、association=
  # 是否已纳入检测、upperLimitValue=温度上限），v1 三者恒空。红外温度检测场景用 v2。
  # default_params 兜底 size=10000（检测设备清单要一次拿全部，同用户实测用法）。
  'mgmt.query_infrared_detect_device': EndpointSpec(
    method='POST',
    path='/plan/list/device/info/v2',
    param_location='json',
    param_map={'dept_id': 'deptId', 'page_size': 'size'},
    page_style='page_size_alt',  # page + size
    defaults={'deptId': 'own_dept_id'},
    default_params={'page': '1', 'size': '10000'},
    result_path='records',
    page_meta=('total', 'current', 'pages', 'size'),
  ),
  # query_device_group（实测）：GET /deviceGroup/v1/dept/{deptId}，deptId 在 path
  # 占位符（Capability 参数 dept_id 经 param_map 反查填充，同 query_dept_by_name）。
  # data 即设备分组数组（专业类别 + 下挂设备组），无分页，原样返回。dept_id 必填，
  # agent 未指明部门时询问用户，不默认兜底。
  'mgmt.query_device_group': EndpointSpec(
    method='GET',
    path='/deviceGroup/v1/dept/{deptId}',
    param_location='query',
    param_map={'dept_id': 'deptId'},
    page_style='none',
    result_path='',  # data 即分组数组，原样返回
  ),
  # query_device_system_tree（实测）：GET /equipment/get/system/tree/{deptId}，deptId
  # 在 path 占位符（param_map 反查填充，同 query_device_group）。data 即系统类型树
  # 数组（一级系统 + 二级子系统，节点 {id,name,level,children}），无分页，原样返回；
  # 节点 id 即 query_device_logic_type 下钻要传的 pid。dept_id 必填，用服务账号
  # ownDeptId（技术保障部 23BaepxKklv）。
  'mgmt.query_device_system_tree': EndpointSpec(
    method='GET',
    path='/equipment/get/system/tree/{deptId}',
    param_location='query',
    param_map={'dept_id': 'deptId'},
    page_style='none',
    result_path='',  # data 即系统类型树数组，原样返回
  ),
  # query_device_logic_type（实测）：POST /equipment/get/logic/type（URL 含 "get" 但
  # HTTP 方法是 POST，GET 返 405），body {pid,deptId,page,pageSize}。pid 必填（从系统
  # 类型树节点 id 获取，不带返回空）；deptId 走服务账号默认部门 own_dept_id 兜底
  # （defaults）。分页 page+pageSize，default_params 兜底 1/20。响应 data={count,
  # pageSize,pageCounts,equipmentLogicTypeVOList}，result_path 取
  # equipmentLogicTypeVOList，page_meta 保留分页元数据。
  'mgmt.query_device_logic_type': EndpointSpec(
    method='POST',
    path='/equipment/get/logic/type',
    param_location='json',
    param_map={'dept_id': 'deptId', 'page_size': 'pageSize'},
    page_style='page_pagesize',  # page + pageSize
    defaults={'deptId': 'own_dept_id'},
    default_params={'page': '1', 'pageSize': '20'},
    result_path='equipmentLogicTypeVOList',
    page_meta=('count', 'pageSize', 'pageCounts'),
  ),
  # query_device_major（实测）：POST /equipment/get/dept（URL 含 "get" 但方法是 POST，
  # GET 返 405），deptId 在 query（param_map 反查）。data 即专业类别数组（供配电/制冷/
  # 智能化，id 与 query_equipment_maintenance_plan 的 parentGroupId code 一致），无分页，
  # 原样返回。dept_id 走服务账号默认部门 own_dept_id 兜底（defaults，query 参数适用）。
  'mgmt.query_device_major': EndpointSpec(
    method='POST',
    path='/equipment/get/dept',
    param_location='query',
    param_map={'dept_id': 'deptId'},
    page_style='none',
    defaults={'deptId': 'own_dept_id'},
    result_path='',  # data 即专业类别数组，原样返回
  ),
  # query_device_type（实测）：POST /equipment/list，body {deptId,page,size,name?,parentId?}。
  # 分页 page + size（信封与 query_device_info 一致：records/total/current/pages/size）。
  # dept_id 走 own_dept_id 兜底；name/parent_id 为可选过滤（parent_id 从 query_device_major
  # 的专业 id 获取）。result_path 取 records，page_meta 保留分页元数据。
  'mgmt.query_device_type': EndpointSpec(
    method='POST',
    path='/equipment/list',
    param_location='json',
    param_map={'dept_id': 'deptId', 'page_size': 'size', 'parent_id': 'parentId'},
    page_style='page_size_alt',  # page + size
    defaults={'deptId': 'own_dept_id'},
    default_params={'page': '1', 'size': '20'},
    result_path='records',
    page_meta=('total', 'current', 'pages', 'size'),
  ),
  # query_company_list（实测）：GET /authority/userrole/v1/current/company，无参数，
  # data 即公司对象数组（companyId/companyName/kingdeeCompanyId 等），原样返回。
  # 供 agent 查公司列表/取 company_id（查其他公司数据的前置步骤）。
  'mgmt.query_company_list': EndpointSpec(
    method='GET',
    path='/authority/userrole/v1/current/company',
    param_location='query',
    param_map={},
    page_style='none',
    result_path='',  # data 即公司数组，原样返回
  ),
  # query_user_list（实测）：GET /user/dept/v4/page，query {deptId,page,pageCount}。
  # deptId 必填（部门人员列表，不默认）；pageCount 是该接口的每页条数字段（非 size）。
  # 返回 data={pageSize,pageCounts,count,users}，result_path 取 users，page_meta
  # 保留 count/pageCounts/pageSize。
  'mgmt.query_user_list': EndpointSpec(
    method='GET',
    path='/user/dept/v4/page',
    param_location='query',
    param_map={'dept_id': 'deptId', 'page_size': 'pageCount'},
    page_style='page_size_alt',  # page + pageCount(每页条数)
    result_path='users',
    page_meta=('count', 'pageCounts', 'pageSize'),
    default_params={'page': '1', 'pageCount': '20'},
  ),
  # query_dept_list（实测）：GET /authority/userrole/v3/current/dept，query
  # {companyId}。companyId 走服务账号默认公司兜底（defaults 取登录缓存 company_id，
  # 同 query_person_by_id）；查其他公司时 agent 先用 query_company_list 获取该公司 id
  # 传入。data 即部门对象数组，原样返回，无分页。
  'mgmt.query_dept_list': EndpointSpec(
    method='GET',
    path='/authority/userrole/v3/current/dept',
    param_location='query',
    param_map={'company_id': 'companyId'},
    page_style='none',
    defaults={'companyId': 'company_id'},
    result_path='',  # data 即部门数组，原样返回
  ),
  # query_mindmap（实测）：GET /file/v1/getAll/{id}，id 在 path 占位符（file_id 经
  # param_map 反查填充，同 query_dept_by_name/query_device_group）。data 即思维导图
  # 树对象（根 + 多级 children，type=1 分类节点 / type=0 文件节点），原样返回，无分页。
  # 树很大（约 165KB），进上下文会被 tools_factory 截断，LLM 概述结构即可。
  'mgmt.query_mindmap': EndpointSpec(
    method='GET',
    path='/file/v1/getAll/{id}',
    param_location='query',
    param_map={'file_id': 'id'},
    page_style='none',
    result_path='',
  ),
  # query_kb_article（实测）：POST /kbArticle/getIsSharedKbArticleList（URL 含 get 但
  # 实为 POST，同 /equipment/get/dept 的坑）。body {page,size,title,tagId}，MyBatis-Plus
  # 信封 records/total/current/pages/size（page+size 风格，page_size_alt）。title 在
  # body 里按标题关键字模糊过滤（实测 title=误操作 仅返回标题含「误操作」的文章；
  # title 放 query 参数不过滤），tagId 按标签过滤（实测 tagId=3 → 仅行业案例 9 条，
  # tagName 均为「行业案例」），两者不传返回全部。base_url 默认 MGMT_BASE_URL（/center）。
  # result_path 留空（data 即信封），page_meta 保留分页元数据供 LLM 概述总数。
  'mgmt.query_kb_article': EndpointSpec(
    method='POST',
    path='/kbArticle/getIsSharedKbArticleList',
    param_location='json',
    param_map={'page_size': 'size', 'tag_id': 'tagId'},
    page_style='page_size_alt',  # page + size
    default_params={'page': '1', 'size': '10'},
    result_path='',  # data 即 MyBatis-Plus 信封
    page_meta=('total', 'current', 'pages', 'size'),
  ),
  # query_inspection_ticket（文档登记，待实测）：POST /inspection/ticket/get/list/v1
  # （base URL 是根路径，走 MGMT_INSPECTION_BASE_URL，与 query_inspection_list 同源）。
  # body {deptId,name,page,pageSize,status,ticketNumber,type}，全字段可选；type 0=所有 /
  # 1=我巡检的 / 2=我审核的。分页 page+pageSize 平铺。返回信封文档未给出（data:{} 占位），
  # result_path/page_meta 为按巡检服务惯例的猜测值，联调后按真实返回修正。
  'mgmt.query_inspection_ticket': EndpointSpec(
    method='POST',
    path='/inspection/ticket/get/list/v1',
    base_url=settings.MGMT_INSPECTION_BASE_URL,
    param_location='json',
    param_map={'ticket_number': 'ticketNumber', 'page_size': 'pageSize'},
    page_style='page_pagesize',  # page + pageSize 平铺
    result_path='list',  # 猜测值：待实测确认
    page_meta=('total',),  # 猜测值：待实测确认
    default_params={'page': '1', 'pageSize': '20'},
  ),
  # query_inspection_ticket_detail（文档登记，待实测）：GET /inspection/ticket/get/detail/v1/{id}，
  # base URL 同上（走 MGMT_INSPECTION_BASE_URL）。id 在 path 占位符（ticket_id 经 param_map
  # 反查填充）。返回 Result«InspectionTicketReportV2VO»，data 即完整 VO：basicInfo（orderNo
  # 工单号/inspectorText 巡检人/majorText 专业/巡检起止时间/completedSubTaskCount）+
  # abnormalStatList（异常项/图片/处理措施/转办信息）。data 即目标，原样返回，无分页。
  'mgmt.query_inspection_ticket_detail': EndpointSpec(
    method='GET',
    path='/inspection/ticket/get/detail/v1/{id}',
    base_url=settings.MGMT_INSPECTION_BASE_URL,
    param_location='query',
    param_map={'ticket_id': 'id'},
    page_style='none',
    result_path='',  # data 即 VO
  ),
  # query_kb_standard（文档登记）：POST /kbNationalStandard/getIsSharedStandardList
  # （/center 前缀，走默认 MGMT_BASE_URL，同 query_kb_article）。body {page,size,tagId,
  # title}，MyBatis-Plus 信封 {current,pages,records,size,total}（page+size 风格，
  # page_size_alt）。title 按标题关键字模糊过滤、tagId 按标签过滤，均可选；不传返回
  # 全部。result_path 留空（data 即信封），page_meta 保留分页元数据供 LLM 概述总数。
  'mgmt.query_kb_standard': EndpointSpec(
    method='POST',
    path='/kbNationalStandard/getIsSharedStandardList',
    param_location='json',
    param_map={'page_size': 'size', 'tag_id': 'tagId'},
    page_style='page_size_alt',  # page + size
    default_params={'page': '1', 'size': '10'},
    result_path='',  # data 即 MyBatis-Plus 信封
    page_meta=('total', 'current', 'pages', 'size'),
  ),
}

"""综合管理平台能力数据（真实接口，Phase 2）。

50 条能力（32 条单接口经联调核实可用、且当前需求内；另 3 条接口文档登记待实测；
还有 15 条组合能力 query_my_info / query_pen_company_dept / query_event_ticket /
query_pending_ticket / query_change_ticket / query_breakdown_ticket /
query_question_ticket / query_threshold_ticket / query_supplier_ticket /
query_breakdown_ticket_detail / query_question_ticket_detail / query_event_ticket_detail /
query_change_ticket_detail / query_supplier_ticket_detail / query_threshold_ticket_detail，
均已实测）：
- mgmt.query_my_info（查询我的综合管理平台账号信息，解析当前用户身份）
- mgmt.query_pen_company_dept（查询我的公司及部门，工单服务，用于解析 dc/公司 id）
- mgmt.query_person_by_name（按姓名查人）
- mgmt.query_dept_by_name（按部门名称模糊查询部门）
- mgmt.query_person_by_id（按 uid 查档案）
- mgmt.query_employee_ticket（员工变更工单）
- mgmt.query_person_training（人员培训信息）
- mgmt.query_person_maintenance_plan（人员维护工单）
- mgmt.query_person_inspection（人员机房巡检）
- mgmt.query_inspection_list（巡检列表查询，按专业类别/姓名/时间/巡检类型筛选）
- mgmt.query_inspection_device_type（巡检设备类型列表，设备类型 id + 名称，支持名称/类型 id 过滤）
- mgmt.query_inspection_device_type_v2（巡检设备类型全量，v2 接口不分页一次返回纯数组）
- mgmt.query_inspection_item（巡检项/巡检图片点位模板查询，按设备类型/点位名查检查项清单）
- mgmt.query_infrared_device_type（红外设备类型列表，4 种，带默认温度上限）
- mgmt.query_infrared_device_list（红外设备列表，按红外类型分页查已纳入检测的设备明细）
- mgmt.query_infrared_detect_device（红外检测设备列表，部门全量设备 + 红外关联状态/温度上限）
- mgmt.query_equipment_maintenance_plan（设备维护计划进度列表，对方平台也称「设备维护进度」）
- mgmt.query_equipment_maintenance_status（设备维护计划状态列表，按年份+公司查各分部 status）
- mgmt.query_equipment_work_order（设备维护工单列表，分页，支持主单号/子单号/年份/状态/周期过滤）
- mgmt.query_equipment_maintenance_flow（设备维护操作流程/MOP 列表，分页，按部门 + 流程名模糊查）
- mgmt.query_equipment_maintenance_flow_detail（设备维护操作流程详情，按流程 id 查完整内容）
- mgmt.query_device_info（设备台账/设备信息列表，按部门分页）
- mgmt.query_device_group（部门设备分组树，按部门查询）
- mgmt.query_device_system_tree（设备系统类型树，按部门查询；树节点 id 即下钻 pid）
- mgmt.query_device_logic_type（系统类型下的子系统列表，按父系统 pid 分页，带关联设备数）
- mgmt.query_device_major（设备专业类别，供配电/制冷/智能化）
- mgmt.query_device_type（设备类型列表，分页，支持名称关键字/专业过滤）
- mgmt.query_company_list（公司列表，取 company_id 的前置步骤）
- mgmt.query_user_list（部门人员列表，按部门分页，返回 uid 供后续查人）
- mgmt.query_dept_list（公司下的部门列表，companyId 默认当前登录用户所属公司，服务账号回退）
- mgmt.query_mindmap（运维管理体系/思维导图树，file_id 必填）
- mgmt.query_kb_article（IDC 知识库共享案例文章列表，支持标题关键字/标签过滤）
- mgmt.query_inspection_ticket（巡检工单列表，按工单编号/名称/状态/类型筛选）
- mgmt.query_inspection_ticket_detail（巡检工单明细，按 id 查单张工单详情）
- mgmt.query_inspection_user_list（巡检员列表，按部门分页，姓名/手机号模糊过滤；dept_id 必填且不兜底——未指明须先问用户）
- mgmt.query_kb_standard（标准规范列表，按标题关键字/标签过滤）
- mgmt.query_pending_ticket（查询我的工单，组合能力：查人→换 token→调 pen）
- mgmt.query_event_ticket（查询事件工单列表，组合能力：公司维度 POST，支持按部门/时间筛选）
- mgmt.query_change_ticket（查询变更工单，组合能力：公司维度 POST，默认当前登录用户所属公司，服务账号回退润泽科技）
- mgmt.query_breakdown_ticket（查询故障工单列表，组合能力：公司维度 POST，默认当前登录用户所属公司，服务账号回退润泽科技）
- mgmt.query_question_ticket（查询问题工单列表，组合能力：公司维度 POST，默认当前登录用户所属公司，服务账号回退润泽科技）
- mgmt.query_threshold_ticket（查询阈值工单列表，组合能力：公司维度 POST，默认当前登录用户所属公司，服务账号回退润泽科技，specialty 1/2/4/8）
- mgmt.query_supplier_ticket（查询供应商服务工单列表，组合能力：公司维度 POST，默认当前登录用户所属公司，服务账号回退润泽科技，serviceType 1=例行维护/2=故障检修/3=技术培训/0=其他）
- mgmt.query_supplier_ticket_detail（查询供应商服务工单详情，按供应商工单列表 ticketId 查单张工单）
- mgmt.query_threshold_ticket_detail（查询阈值工单详情，按阈值工单列表 ticketId 查单张工单）
- mgmt.query_breakdown_ticket_detail（查询故障工单详情，按待办列表 id 查单张工单）
- mgmt.query_question_ticket_detail（查询问题工单详情，按待办列表 id 查单张工单）
- mgmt.query_event_ticket_detail（查询事件工单详情，按待办列表 id 查单张工单）
- mgmt.query_change_ticket_detail（查询变更工单详情，按变更工单列表 ticketId 查单张工单，已实测）
- mgmt.query_config_template_list（查询配置管理列表，配置模板/专项检查表）
- mgmt.query_task_list（查询任务列表，配置模板生成的任务工单）
- mgmt.query_review_appointment（查询评审预约列表，变更工单审批前的评审会议预约）
- mgmt.query_review_appointment_detail（查询评审预约详情，按列表 id 查单条预约）

其余接口（部门树/隐患/周报等）已全部移除——既有平台侧 bug、又不在当前
需求范围，保留只会让 agent 选错工具。设备信息接口按需新接入，见 query_device_info。

params 字段名统一用 snake_case，与 endpoints.py 的 EndpointSpec.param_map 一一
对应——新增/改名参数时两个文件要同步改。

category 是 L2 功能分类，意图路由与分类清单的统一词汇——intent_labels 不再单独
维护。六条人员能力 category 均为 'org'，路由无歧义：任何 org 相关查询都会 promote
这 6 个工具，链路（按姓名查人 → uid/ownDeptId/companyId → 变更工单/维护工单/
巡检/培训）对 LLM 全可见。query_dept_by_name 亦为 'org'：部门名解析与人员同属
组织域，和按姓名查人一样是底层前置能力，org 查询会一并 promote。query_inspection_list
为 'inspection'：巡检相关查询 promote 它，与按 uid 查个人机房巡检
（query_person_inspection，org）区分。query_inspection_device_type 亦为 'inspection'：
巡检设备类型相关查询 promote 它；返回巡检设备类型字典（deviceTypeId+name），可按
name/deviceTypeId 过滤，供巡检列表按设备类型筛选。query_inspection_device_type_v2 亦为
'inspection'：与 v3 是同一份设备类型字典，仅 v2 不分页一次返回全部（纯数组）、v3 带
分页与过滤；描述中已注明按需过滤/分页优先用 v3，避免 LLM 两工具重复选择。
query_inspection_item 亦为 'inspection'：巡检项/巡检内容查询 promote 它；按设备类型或
点位名称返回巡检图片点位及其检查项清单（inspectionData），是巡检前获取检查项的前置查询。
query_inspection_ticket / query_inspection_ticket_detail 亦为 'inspection'：巡检工单查询
promote 它们（文档登记待实测）；两者联动——列表按工单编号/名称/状态/类型筛出工单，取
id 后再查单张工单明细。与 query_inspection_list（巡检执行记录）区分：本组查工单台账。
query_equipment_maintenance_plan 为
'maintenance'：设备维护计划/维护进度相关查询 promote 它（对方平台两套叫法是同一接口）；
deptId 优先按用户问题中的部门
经 query_dept_by_name 解析，未指明默认当前登录用户所属部门（ownDeptId，服务账号
回退技术保障部）并告知用户。
query_equipment_maintenance_status 亦为 'maintenance'：维护计划状态概览查询 promote
它，与明细列表（query_equipment_maintenance_plan）联动。
query_equipment_work_order 亦为 'maintenance'：设备维护工单/执行记录查询 promote 它，
与维护计划（query_equipment_maintenance_plan）区分——计划查安排/进度，工单查实际执行
记录；主单号（maintenance_main_num）按主单返回其下全部子单，子单号（maintenance_num）
按子单号返回该计划的历史执行记录。
query_equipment_maintenance_flow / query_equipment_maintenance_flow_detail 亦为
'maintenance'：维护操作流程/MOP（「怎么做」的操作步骤模板）查询 promote 它们，详情
用列表返回的 rows[].id 查；与计划、工单三者的分工——流程查操作步骤模板、计划查
安排与执行进度、工单查实际执行记录。
query_device_info 为 'device'：设备/台账相关查询 promote 它；dept_id 未指明默认
当前登录用户所属部门（ownDeptId，服务账号回退技术保障部），用户指定部门时用
query_dept_by_name 解析（区别于 maintenance 的默认兜底）。
query_infrared_device_type / query_infrared_device_list / query_infrared_detect_device
亦为 'device'：红外温度检测相关查询 promote 它们。三者链路——
query_infrared_device_type 红外设备类型（4 种，带默认温度上限）→
query_infrared_device_list 按类型查已纳入检测的红外设备明细（含安装位置/温度上限）；
query_infrared_detect_device 查部门**全量**设备的红外关联状态（association + 温度上限），
与 query_device_info（通用设备台账，无红外信息）区分——后者用于「设备台账/设备信息」，
前者用于「红外温度检测的设备清单/关联状态」。
query_device_system_tree / query_device_logic_type 为 'device'：设备系统类型相关查询
promote 它们；两者联动——系统类型树节点的 id 即下钻子系统列表要传的 pid，链路
（树 → 按父 pid 查子系统及关联设备数）对 LLM 全可见。
query_device_major / query_device_type 为 'device'：设备类型相关查询 promote 它们；
两者联动——query_device_major 返回的专业类别 id 即 query_device_type 按专业过滤要
传的 parentId，链路（专业大类 → 该专业下设备类型）对 LLM 全可见。
query_pending_ticket 为 'work_order'：我的工单/流程单查询 promote 它；组合能力内部
自动完成查人（按姓名拿 kingdeeUid，不传 name 用服务账号本人）→ 用 mgmt token 调
工单服务三步。工单服务的巡检工单是单独接口 /pending/list/inspection/v1（当前
未接入），与 mgmt 巡检台账（query_inspection_ticket）区分——本工具查的是工单服务
「我的工单」（token 归属人相关），返回工单编号/名称/级别/类型/状态/部门/创建人等；
type 按工单类别过滤：1=变更/4=问题/5=故障/6=事件/7=阈值。
query_change_ticket 亦为 'work_order'：变更工单查询 promote 它；组合能力内部公司维度
POST（默认当前登录用户所属公司/部门，服务账号回退润泽科技+技术保障部，company_id 可选
跨公司），与待办区分——
变更工单是**公司维度**（token 只鉴权，不区分人，实测同 body 不同人结果相同），
所以无 name 参数，返回工单编号/项目名称/专业/状态/发起人/时间等。
query_breakdown_ticket 亦为 'work_order'：故障工单列表查询 promote 它；与
query_change_ticket 同构（公司维度 POST，默认当前登录用户所属公司/部门，服务账号
回退润泽科技+技术保障部，company_id 可选跨公司），区别仅端点
/wiporder/ticketlist/breakdown 与筛选项——变更的
specialty/modifyType 换成 breakDownLevel（故障级别），key_word 支持故障名称/工单号
模糊查询。
query_question_ticket 亦为 'work_order'：问题工单列表查询 promote 它；与
query_change_ticket 同构（公司维度 POST），区别仅端点 /wiporder/ticketlist/question
与筛选项——换成 severityLevel（问题级别）+ specialty（专业），无 ticketType/modifyType/
onlyTimeOut，key_word 支持问题名称/工单号模糊查询。
query_breakdown_ticket_detail 亦为 'work_order'：故障工单详情查询 promote 它；按
ticket_id 查单张故障工单完整详情（描述/受理人/现场图片/审批流/操作记录等），与待办
列表、变更列表区分——本能力是列表之后的单张详情下钻，id 从 query_pending_ticket
待办列表返回的 id 字段获取。
query_question_ticket_detail 亦为 'work_order'：问题工单详情查询 promote 它；按
ticket_id 查单张问题工单完整详情（问题名称/描述/分派与处理人/涉及设备/计划交付时间
等），与故障/事件工单详情区分——三者的详情端点各不相同（query/breakdown、question、
event），id 均从 query_pending_ticket 待办列表返回的 id 获取，按工单类型选对应工具。
query_event_ticket_detail 亦为 'work_order'：事件工单详情查询 promote 它；按
ticket_id 查单张事件工单完整详情（事件名称/描述/原因分析/处理流程/影响范围与时长/
关联工单等），与故障/问题工单详情区分——id 均从 query_pending_ticket 待办列表
返回的 id 获取，按工单类型选对应工具。
query_change_ticket_detail 亦为 'work_order'：变更工单详情查询 promote 它；按
ticket_id 查单张变更工单完整详情，与故障/问题/事件工单详情区分——四者详情端点各不
相同，本能力的 id 从 query_change_ticket（变更工单列表）返回的 **ticketId** 字段
获取（注意不是 id），其余三个从 query_pending_ticket 待办列表的 id 字段获取，按
工单类型选对应工具。实测确认：不按 token 归属人区分，任意有效 token 可查任意人
发起的工单详情，与其余三个详情接口同构。
query_config_template_list 为 'config'：配置管理相关查询 promote 它；配置管理模板
（专项检查表/固定模板）是独立 L2 分类，与巡检（inspection，巡检项模板
query_inspection_item）区分——本工具查配置管理侧的检查表模板，templateName 模糊、
type 精确过滤，均可选。
query_task_list 亦为 'config'：配置任务查询 promote 它；任务由配置模板（专项检查表/
固定模板）生成，与 query_config_template_list（查模板）联动——模板是任务的定义，
本工具查任务的实际执行（taskTicketNo 形如 RZKJ-RW-…，含执行人/起止时间/状态）。
type 必填（默认 1=全部，2/3/4 空），deptId 走服务账号默认部门兜底。
query_review_appointment 为独立分类 'review'：评审预约查询 promote 它；与变更工单
（query_change_ticket，work_order）是业务链路上的前后环节——变更发起后先约评审会议，
评审通过再走变更审批流程，两者字段高度对应（changeName/changeSpecialty/changeType/
level）。单独建分类而非归入 work_order，是为了让 Step1 意图识别时"评审"能直接命中
候选词，不需要先联想到"工单"再间接关联（词汇表规模小，新增分类成本低）。
query_review_appointment_detail 亦为 'review'：评审预约详情查询 promote 它；按列表
返回的 id 查单条预约完整详情，与 query_review_appointment（列表）联动。
deptId/companyId 走服务账号默认部门/公司兜底，兜底时需告知用户默认查询范围；
type/isAll 观察值可用，具体枚举含义待业务方核实。
"""

from service.query.base.capability import Capability
from service.query.mgmt.ticket_codes import PENDING_LEVEL_DOC, render_return_doc

_TICKET_SCOPE_NOTE = (
  '**回复用户时必须先说明本次查询的公司与部门**（未指定范围时就是「本人公司 + 全部部门」，'
  '不是某个具体部门；服务账号未绑定 mgmt 账号时公司回退「润泽科技」）。'
  '**查不到时先说明查的是哪个范围，不要直接断言「不存在」**——单子可能属于其他公司或部门'
  '（公司维度默认只到本人公司，跨公司要用户指明）。用户指定公司/部门时，说明实际查询的'
  '公司名与部门名（公司名从 query_company_list/query_pen_company_dept 返回获取，部门名用 '
  'query_pen_company_dept 解析出的 deptName）。'
)

# 工单列表「部门范围」文案（6 类工单共用，**单一来源**：改口径只改这里）。
# 2026-10-06 口径调整：**未指定范围＝本公司全部部门**。旧口径"未指定→本人部门"会让
# 按工单编号查（单号全局唯一）落空、被答成「不存在」（2026-10-04 实测）。
_TICKET_DC_DEFAULT_NOTE = (
  '**dc 不传 ＝ 查本公司全部部门（不限部门）**（旧口径「未指定→本人部门」已废弃：工单编号'
  '全局唯一，限部门会直接查空）；company_id 可选，跨公司时用 mgmt 公司 id（从 '
  'query_company_list 获取），跨公司时同样是查该公司全部部门。'
)

_TICKET_DC_DOC = (
  '部门 id，可选；**只在用户要按部门列列表、且指明了部门/分部**（含简称如「A1」即 '
  'A1 分部）时传，先用 query_pen_company_dept 解析出 deptId。**不传＝本公司全部部门'
  '（不限部门）**——用户没提范围就是查全公司各部门，不要为了"缩小范围"自作主张填本人部门'
)
_TICKET_ALL_DEPT_DOC = (
  '是否查全公司（不限部门），可选；**不传即为默认（本公司全部部门）**，用户明确要'
  '“全公司/不限部门”时可传 true（与默认一致）；用户指明某部门时改用 dc 传部门 id'
)

CAPABILITIES: tuple[Capability, ...] = (
  Capability(
    id='mgmt.query_my_info',
    name='查询我的综合管理平台账号信息',
    description=(
      '返回当前登录用户自己的综合管理平台账号信息（姓名、工号、职位、所属部门、'
      '上级部门、公司等，来自账号登录档案）。无参数。适用于一切以「我」为视角的'
      '查询：「我在哪个部门」「我所在部门的人员」「我的部门有哪些人」「我的工号/'
      '职位」等——先调用本工具拿到当前用户身份，再据此继续查询，不要反问用户索要'
      '姓名。own_dept_id/own_dept_name 是所属部门（通常如技术保障部），'
      'dept_id/dept_name 是具体部门；查「我所在部门的人员」时用 own_dept_id 作为'
      '部门人员列表的 dept_id。'
    ),
    params={},
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_person_by_name',
    name='按姓名查询人员信息',
    description=(
      '按姓名模糊查询综合管理平台的人员基本信息，返回匹配人员的姓名、工号、'
      '部门、职位、联系方式等。适用于「XX 是哪个部门的」「XX 是谁」「负责人是谁」'
      '等按姓名查人的场景；也是查询人员变更工单前获取 uid 与 company_id 的前置步骤。'
      '返回空数组 [] 时表示未找到匹配该姓名的人员，直接告知用户「未找到姓名为 XX 的人员」，'
      '不要说服务器故障或平台不可用。'
    ),
    params={
      'name': '姓名关键字，必填，模糊匹配（传姓名的部分片段即可匹配对应人员）',
    },
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_dept_by_name',
    name='按部门名称模糊查询部门',
    description=(
      '按部门名称模糊查询综合管理平台的部门信息，返回匹配部门的 id、名称、'
      '短名、父部门等。适用于「XX 是哪个部门」「技术保障部」等按部门名查部门的'
      '场景；也是查询部门相关数据（如设备维护计划需传 deptId）的前置步骤——'
      '按部门名解析出 deptId 后再传给对应工具。'
    ),
    params={
      'dept_name': '部门名称关键字，必填，模糊匹配（如传「技术保障」能匹配「技术保障部」）',
    },
    category='org',
    domain='dept',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_person_by_id',
    name='按 uid 查询人员详情',
    description=(
      '按用户 uid 查询综合管理平台的单个人员完整档案（部门、职位、证件、学历、'
      '在职信息等）。适用于已拿到 uid 后查看「XX 的详细信息」。dept_id/company_id '
      '留空时自动用服务账号默认部门/公司。'
    ),
    params={
      'uid': '用户 uid，必填（从按姓名查询结果中获取）',
      'dept_id': '部门 id；留空默认当前登录用户所属部门',
      'company_id': '公司 id；留空默认当前登录用户所属公司',
    },
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_employee_ticket',
    name='查询员工变更工单',
    description=(
      '按员工 uid 分页查询综合管理平台的员工变更工单记录（入职、调岗、离职等'
      '员工信息变更），返回工单类型、状态、变更时间等。适用于「XX 有哪些变更'
      '记录」「XX 最近一次调岗的工单」等查询。需先通过按姓名查询拿到员工本人'
      '的 uid 与 company_id（必须用员工所属公司，不能用其他公司代替）。'
      'ticket_type 必填（不带则返回空数据），常见取值如 1。'
    ),
    params={
      'user_id': '员工 uid，必填（从按姓名查询结果中获取）',
      'ticket_type': '工单类型（数字编码，如 1），必填；不带 ticketType 查询返回空数据',
      'company_id': '员工所属公司 id，必填（从按姓名查询结果中与 uid 一并获取）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_person_training',
    name='查询人员培训信息',
    description=(
      '按员工 uid 查询综合管理平台某**季度**的培训学习情况，返回培训时长、完成度、'
      '每日学习时长、培训计划列表等。适用于「XX 的培训情况」「XX 某季度培训多长时间」'
      '等查询。需先通过按姓名查询拿到员工 uid。year+month 必填，**month 是季度索引**'
      '（1=1-3月 / 2=4-6月 / 3=7-9月 / 4=10-12月），不是日历月份；start_time/end_time'
      '不用传，系统自动按季度范围补全。查某年全年培训时，依次传 month=1/2/3/4 查询'
      '4 个季度并自行汇总。用户未指明季度时，用当前年份当前季度。'
    ),
    params={
      'uid': '员工 uid，必填（从按姓名查询结果中获取）',
      'year': '年份，必填（如 2026）',
      'month': '季度索引，必填：1=1-3月 / 2=4-6月 / 3=7-9月 / 4=10-12月（不是日历月份）',
      'start_time': '开始时间，可选；不用传，系统按季度自动补全',
      'end_time': '结束时间，可选；不用传，系统按季度自动补全',
      'is_pass': '合格筛选，可选：0全部 / 1合格 / 2不合格',
      'plan_status': '培训计划状态，可选：0全部 / 1进行中 / 2未开始 / 3已完成',
    },
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_person_maintenance_plan',
    name='查询人员维护工单',
    description=(
      '按员工 uid 分页查询综合管理平台的人员维护工单/维保计划记录，返回设备、'
      '计划时间、状态、操作人等。适用于「XX 的维护/维保记录」「XX 负责哪些维护'
      '工单」等查询。dept_id 必填，必须用该员工档案的 ownDeptId（从按姓名查询'
      '结果中获取，不是 deptId）；company_id 必填，用员工所属公司；需先按姓名'
      '查询拿到 uid/ownDeptId/companyId 三个值。'
    ),
    params={
      'uid': '员工 uid，必填（从按姓名查询结果中获取）',
      'dept_id': '员工档案的 ownDeptId，必填（从按姓名查询结果中获取，不是 deptId）',
      'company_id': '员工所属公司 id，必填（从按姓名查询结果中获取）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_person_inspection',
    name='查询人员机房巡检记录',
    description=(
      '按员工 uid 分页查询综合管理平台的人员机房巡检记录，返回巡检时间、时长、'
      '机房、归档状态等。适用于「XX 的巡检记录」「XX 巡检过哪些机房」等查询。'
      'dept_id 必填，必须用该员工档案的 ownDeptId（从按姓名查询结果中获取，不是'
      'deptId）；company_id 必填，用员工所属公司；需先按姓名查询拿到'
      'uid/ownDeptId/companyId 三个值。'
    ),
    params={
      'uid': '员工 uid，必填（从按姓名查询结果中获取）',
      'dept_id': '员工档案的 ownDeptId，必填（从按姓名查询结果中获取，不是 deptId）',
      'company_id': '员工所属公司 id，必填（从按姓名查询结果中获取）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_inspection_list',
    name='查询巡检列表',
    description=(
      '按专业类别、姓名、时间范围、巡检类型等条件查询综合管理平台的巡检列表，返回'
      '每条巡检的人员、部门、专业类别、巡检类型、巡检时间、时长、归档状态等。'
      '适用于「查一下巡检记录」「XX 专业的巡检」「某员工的巡检」「计划/非计划巡检'
      '列表」等。room_type 巡检专业类别取「供配电/制冷/智能化」；type 巡检类型'
      '1=计划巡检 / 0=非计划巡检；employee_name 姓名、start_time/end_time 时间范围'
      '（格式 YYYY-MM-DD）均可选。company/dept_name：优先按用户问题中的公司/部门'
      '传；问题未指明时，默认查当前登录用户所属公司/部门（服务账号回退润泽科技/'
      '技术保障部）并告知默认范围。archive 固定为 true（只查已归档）。区别于 '
      'query_person_inspection'
      '（按员工 uid 查个人机房巡检记录）：本工具按条件筛巡检列表，不要求 uid。'
    ),
    params={
      'room_type': '巡检专业类别：供配电 / 制冷 / 智能化，可选',
      'employee_name': '巡检人员姓名，可选（用户明确指定某人时才传）',
      'start_time': '开始时间（格式 YYYY-MM-DD），可选',
      'end_time': '结束时间（格式 YYYY-MM-DD），可选',
      'type': '巡检类型：1=计划巡检 / 0=非计划巡检，可选，按用户意图传',
      'company': '公司名称，可选；未指明时默认查当前登录用户所属公司',
      'dept_name': '部门/分部名称，可选；未指明时默认查当前登录用户所属部门',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='inspection',
    domain='inspection',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_inspection_device_type',
    name='查询巡检设备类型列表',
    description=(
      '查询综合管理平台巡检设备类型字典，返回每种设备类型的 id 与名称（共 48 种，'
      '覆盖供配电/制冷/智能化/消防等专业，如 01=柴发机组、03=高压柜、05=变压器、'
      'UPS、冷水机组、CCTV 系统等）。支持按类型名称模糊过滤（name，如「高压」→'
      '高压柜、「机」→ 5 种）与设备类型 id 精确过滤（device_type_id，如 01）。'
      '适用于「巡检有哪些设备类型」「设备类型列表」「查 XX 类型巡检对应的设备类型'
      'id」等场景；也是巡检列表（query_inspection_list）按设备类型筛选的前置步骤'
      '——先用本工具按名称查设备类型拿 deviceTypeId 再传入巡检查询。'
    ),
    params={
      'name': '设备类型名称关键字，可选；模糊匹配（如「高压」「机」）',
      'device_type_id': '设备类型 id，可选；精确匹配（如 01=柴发机组，从查询结果获取）',
      'company': '公司名称，可选（如「润泽科技发展有限公司」；实测留空不影响结果）',
      'floor': '专业/楼层过滤，可选（接口保留参数，实测不影响结果，可留空）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='inspection',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_inspection_device_type_v2',
    name='查询巡检设备类型全量（v2 不分页）',
    description=(
      '查询综合管理平台巡检设备类型字典（v2 接口），一次返回全部 48 种设备类型'
      '的 id 与名称（{deviceTypeId, name} 纯数组，无分页信封）。与'
      'query_inspection_device_type（v3 接口）是同一份字典，区别仅在于 v3 带分页'
      '与过滤、v2 不分页一次拿全。适用于「巡检全部设备类型」「设备类型字典全量」'
      '等要一次拿全量的场景；按名称/类型过滤或分页查询请优先用 v3'
      '（query_inspection_device_type），避免两工具重复选择。'
    ),
    params={
      'name': '设备类型名称关键字，可选；模糊匹配（如「高压」「机」）',
      'device_type_id': '设备类型 id，可选；精确匹配（如 01=柴发机组）',
      'company': '公司名称，可选（实测留空不影响结果）',
      'floor': '专业/楼层过滤，可选（接口保留参数，实测不影响结果，可留空）',
    },
    category='inspection',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_inspection_item',
    name='查询巡检项/巡检图片点位模板',
    description=(
      '查询综合管理平台巡检项模板（巡检图片点位），返回每个巡检点位的图片名称'
      '（picture_name）、所属设备类型（device_type_id/device_type_name）及其检查项'
      '清单（inspection_data：每项 name + type，type=1 数值录入如压力/电压/电流，'
      'type=2 选项勾选如运行/待机/停机）。全量约 54 个巡检点位。支持图片/点位名称'
      '模糊过滤（picture_name，如「风墙 一次冷冻泵」「冷却塔」）与设备类型 id 精确'
      '过滤（device_type_id，如 15=一次冷冻泵，从 query_inspection_device_type '
      '获取）。适用于「XX 设备类型巡检要查哪些项」「巡检项模板」「一次冷冻泵的巡检'
      '检查项」「巡检要记录哪些内容」等场景；是巡检前获取检查项清单的前置步骤。'
    ),
    params={
      'picture_name': '巡检项/图片点位名称关键字，可选；模糊匹配（如「风墙 一次冷冻泵」）',
      'device_type_id': '设备类型 id，可选；精确匹配（如 15=一次冷冻泵，从 query_inspection_device_type 获取）',
      'company': '公司名称，可选（实测留空不影响结果）',
      'floor': '专业/楼层过滤，可选（接口保留参数，实测不影响结果，可留空）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='inspection',
    domain='inspection',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_inspection_user_list',
    name='查询巡检员列表',
    description=(
      '按部门分页查询综合管理平台该部门的巡检员，返回每人姓名、手机号（带 86 前缀）、'
      '所属公司/部门（companyName/deptName）、班组（shifts）、负责专业'
      '（roomTypeInfoList[].roomTypeName，含点位数 roomCount 与设备数 deviceCount）、'
      '楼层（floor）、审核权限（auditPermissions）等。适用于「巡检员名单」「XX 部门的'
      '巡检员/巡检人员」「这个班组有谁」「查某人的巡检资格/负责点位」等查询。'
      '**本能力按部门查，dept_id 必填**：用户已说明公司/部门时，先用 query_dept_by_name '
      '按部门名解析出 dept_id 再传入；**用户没说查哪个公司、哪个部门时，必须先问用户，'
      '不要默认用本人所属部门**——要给用户列选项就先调 query_company_list 列出账号可见的'
      '公司（含园区名 gardenName），再用 query_dept_list 列出该公司的部门让用户挑。'
      'name（姓名）、phone（手机号）为模糊过滤：用户给了姓名或手机号就传对应那个，'
      '两个都没给就查该部门全部巡检员。'
      '返回里的 **size 是总条数**、pageSize 是每页条数（两个字段名容易读反，务必按此理解）。'
    ),
    params={
      'dept_id': '部门 id，必填；用户指明部门时先用 query_dept_by_name 解析出 dept_id；用户未指明公司/部门时先问用户，不要默认本人部门（可先用 query_company_list / query_dept_list 列选项）',
      'name': '巡检员姓名，可选；模糊匹配（如「王」）',
      'phone': '巡检员手机号，可选；模糊匹配（11 位或带 86 前缀均可）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='inspection',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_equipment_maintenance_plan',
    name='查询设备维护计划进度列表',
    description=(
      '按部门、年份、专业类别（供配电/制冷/智能化）和/或设备名称，查询综合管理'
      '平台的设备维护计划进度（对方平台也称「设备维护进度」，是同一接口），'
      '返回 planMain 部门/年份汇总（如「技术保障部 '
      '2026 年维护计划」）与 detailList 明细（计划名称、专业类别、频率、维护类型、'
      '排序等），明细每条含 weeks[] 每周执行进度：weekType 周期（▲季度/▲月度等）、'
      'status 执行状态（空=未执行）、percent 完成百分比（0-100）、executor 执行人。'
      '适用于「XX 的设备维护计划」「维护保养计划」「智能化/供配电/制冷类维护计划」'
      '「查某设备的维护计划」「设备维护进度」「维护进度/执行进度」「哪些维护项还没做/已完成」等'
      '查询。'
      '公司/组织（部门）处理规则：用户问题中明确了公司或组织时，优先按用户所指'
      '查询——先用 query_dept_by_name 按部门名解析出 dept_id 传入；未指明时用'
      '默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部），回复须明确'
      '告知默认查询的公司/部门，并提示用户可选择其他公司/组织。parent_group 按问题'
      '中的专业类别传（供配电/'
      '制冷/智能化），不指明则查全部类别；device_name 仅当用户明确提到具体设备'
      '（如「消防自动报警系统」）时才做模糊查询，否则不传；year 必填（默认当前'
      '年份）。数据量大（多条计划×每周）会被截断，回答进度类问题可汇总已见部分'
      '并提示；查某具体设备/类型的进度时传 device_name 缩小范围。区别于 '
      'query_person_maintenance_plan（按员工 uid 查该人员负责的维护工单）。'
    ),
    params={
      'dept_id': '部门 id，可选；用户指明部门时从 query_dept_by_name 解析获取，未指明用当前登录用户所属部门（服务账号回退技术保障部）',
      'device_name': '设备名称，可选；用户明确问某类设备时才传（模糊匹配，如「消防」）',
      'parent_group': '维护专业类别：供配电 / 制冷 / 智能化，可选；不传查全部类别',
      'year': '年份，必填；不传用当前年份（如 2026）',
    },
    category='maintenance',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_equipment_maintenance_status',
    name='查询设备维护计划状态列表',
    description=(
      '按年份与公司查询综合管理平台各分部的设备维护计划状态，返回每条的分部名称、'
      '部门 id、status（0/1）等。适用于「哪些分部今年维护计划做好了」「各分部维护'
      '计划完成情况」「维护计划状态」等查询；也是查看某分部维护计划明细'
      '（query_equipment_maintenance_plan）前的状态概览。year 必填（维护计划年份，'
      '如 2026，通常为当前年份）；company_id 必填（公司 id，润泽科技发展有限公司'
      '为 27WqDiazDVr，可从 query_company_list 获取）。'
    ),
    params={
      'year': '年份，必填（如 2026，通常为当前年份）',
      'company_id': '公司 id，必填（当前登录用户所属公司 id，从 query_company_list 获取）',
    },
    category='maintenance',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_equipment_work_order',
    name='查询设备维护工单列表',
    description=(
      '按部门分页查询综合管理平台的设备维护工单/维护执行记录，返回每条的子单号、'
      '主单号、设备名称/型号/安装位置、计划名称、周期类型（双周/月度/季度）、执行'
      '周次/年份、执行状态、执行人、监督人、计划时间、完成时间等。适用于「设备维护'
      '工单」「维护执行记录」「某台设备的维护历史」「主单/子单查询」等场景。'
      '支持主单号（maintenance_main_num）按主单返回其下全部子单、子单号'
      '（maintenance_num）按子单号返回该计划的历史执行记录，也支持按年份/周次/'
      '状态/周期类型过滤。dept_id 默认当前登录用户所属部门（服务账号回退技术保障部）；year 可选（留空查全部年份）。'
      '区别于 query_equipment_maintenance_plan（查维护计划安排/进度），本能力查'
      '实际维护工单执行记录。'
    ),
    params={
      'dept_id': '部门 id，可选；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv）',
      'year': '年份，可选（如 2026）；留空查全部年份',
      'week': '执行周次，可选（如 32）',
      'status': '执行状态，可选（实测 0=未执行，3=已完成；其余取值可能返回空）',
      'type': '周期类型，可选（数字编码；如 typeName 双周/月度/季度）',
      'maintenance_num': '子单号，可选；按维护计划子单号（maintenancePlanNum）过滤，返回该子单的历史执行记录',
      'maintenance_main_num': '主单号，可选；按主单号（maintenanceMainNum）过滤，返回该主单下全部子单',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='maintenance',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_equipment_maintenance_flow',
    name='查询设备维护操作流程列表',
    description=(
      '按部门分页查询综合管理平台的设备维护操作流程（MOP），返回每条流程的名称、'
      '专业组别（供配电/制冷/智能化）、关联维护计划（devicePlanName）、周期类型'
      '（typeName，如季度/月度）、所属部门，以及嵌套的操作步骤明细 deviceInfo.details[]'
      '（每步的检查项 content、操作说明 operation、执行人/监督人及结果）。适用于'
      '「设备维护操作流程」「维护作业指导/操作步骤」「MOP」「某类设备的维护流程」'
      '（如「视频的维护流程」）等查询。name 为流程名称模糊匹配（如传「视频」命中'
      '视频安防监控系统相关流程），不传返回该部门全部流程。'
      '**本能力按部门查询，dept_id 可切换**：用户指明了部门/分部时，先用 '
      'query_dept_by_name 解析出 dept_id 再传入；**用户没要求就用默认的本人所属部门'
      '（ownDeptId，服务账号回退技术保障部），但必须在回复里说明本次查的是哪个部门，'
      '并补一句"要看其他部门的流程告诉我部门名即可"**——不要让用户以为只有自己部门的数据。'
      '列表每条的 id（rows[].id）可传给 query_equipment_maintenance_flow_detail '
      '查该条流程的完整内容（作业条件/保障措施/仪器/回退方案等）。'
    ),
    params={
      'dept_id': '部门 id，可选、可切换；用户指明部门/分部时先用 query_dept_by_name 解析出 dept_id 传入；不传默认当前登录用户所属部门（服务账号回退技术保障部），此时回复须告知用户查的是哪个部门并可换',
      'name': '流程名称关键字，可选（模糊匹配，如「视频」）；不传返回该部门全部流程',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='maintenance',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_equipment_maintenance_flow_detail',
    name='查询设备维护操作流程详情',
    description=(
      '按流程 id 查询综合管理平台单条设备维护操作流程（MOP）的完整内容，返回基础信息'
      '（流程名、专业组别、关联维护计划、周期类型、所属部门）与作业要素：'
      'deviceInfo.details[] 逐项操作步骤（检查项 content、操作说明 operation、'
      '执行人/监督人及结果），以及作业条件 condition、保障措施 guarantee、'
      '使用仪器 instrument、回退方案 rollbackPlan、流程说明 flow。适用于用户已经指到'
      '某一条流程后问「这条流程具体怎么做/操作步骤是什么/需要什么仪器/有什么注意事项」'
      '等。flow_id 必填，取自 query_equipment_maintenance_flow 列表返回的 rows[].id'
      '（注意不是 deviceInfo.id）。用户只给了流程名而没指定是哪一条时，先用'
      'query_equipment_maintenance_flow 按 name 模糊查到 id 再查详情。'
    ),
    params={
      'flow_id': '流程 id，必填；从 query_equipment_maintenance_flow 列表结果的 rows[].id 获取',
    },
    category='maintenance',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_device_info',
    name='查询设备台账/设备信息',
    description=(
      '按部门分页查询综合管理平台的设备台账/设备信息，返回每台设备的名称、型号、厂商、'
      '设备类型、所属部门、专业组别（供配电/制冷/智能化）、负责人、关联状态、告警上限等。'
      '适用于「查一下 XX 部门的设备」「有哪些设备」「设备台账」「某类设备清单」等查询。'
      'dept_id 必填：用户问题中明确了部门时，先用 query_dept_by_name 按部门名解析出 '
      'dept_id 再传入；用户未指明部门时，默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部）。'
      'type_id 按问题中的设备类型传（可选，不传查该部门全部设备）。区别于 '
      'query_equipment_maintenance_plan（查设备维护计划/维保安排）。'
    ),
    params={
      'dept_id': '部门 id，必填；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部），用户明确部门时先用 query_dept_by_name 解析',
      'type_id': '设备类型 id，可选；用户明确问某类设备时才传',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_infrared_device_type',
    name='查询红外设备类型列表',
    description=(
      '查询综合管理平台红外温度检测的设备类型列表，返回每种红外设备类型的 id、'
      '名称与默认温度上限（default_upper_limit_value）。实测 4 种：测试（上限 33）、'
      '嵌入式服务器（65.5）、变压器（100）、高压柜（25.5）。适用于「红外检测有哪些'
      '设备类型」「XX 类型的默认温度上限」等查询；也是查询红外设备列表'
      '（query_infrared_device_list）按类型过滤拿 infraredDeviceTypeId 的前置步骤。'
      'dept_id 必填：默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部），用户明确'
      '部门时先用 query_dept_by_name 解析。'
    ),
    params={
      'dept_id': '部门 id，必填；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv），用户明确部门时先用 query_dept_by_name 解析',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_infrared_device_list',
    name='查询红外设备列表',
    description=(
      '按红外设备类型分页查询综合管理平台已纳入红外温度检测的设备明细，返回每台的'
      '设备 id、名称、红外设备类型、安装位置（installation_site / project）、温度'
      '上限（upper_limit_value）等。适用于「XX 红外类型下有哪些设备」「红外检测点位'
      '清单」「高压柜红外设备有哪些」等查询。infrared_device_type_id 必填（不带报'
      '服务器错误），从 query_infrared_device_type 获取；dept_id 默认当前登录用户所属部门（服务账号回退技术保障部）。'
      '区别于 query_infrared_detect_device（查部门全量设备及关联状态）——本工具'
      '只查已纳入检测的设备。'
    ),
    params={
      'infrared_device_type_id': '红外设备类型 id，必填（从 query_infrared_device_type 获取，如高压柜 27mEIi1uDpd）',
      'dept_id': '部门 id，可选；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_infrared_detect_device',
    name='查询红外检测设备列表',
    description=(
      '查询综合管理平台红外温度检测的设备清单，返回部门**全量**设备的台账信息'
      '（名称、型号、专业组别、负责人等）及红外关联状态：device_type_id（该设备'
      '对应的红外设备类型 id）、association（是否已纳入红外检测）、'
      'upper_limit_value（温度上限，仅已关联设备有值）。适用于「红外检测覆盖哪些'
      '设备」「哪些设备还没纳入红外检测」「某设备的红外关联状态/温度上限」等查询。'
      '默认一次返回全部（size=10000）。区别于 query_device_info（通用设备台账，'
      '无红外信息）与 query_infrared_device_list（只查已纳入检测的设备明细）——'
      '本工具是红外温度检测的设备范围/关联状态一览。'
    ),
    params={
      'dept_id': '部门 id，可选；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv），用户明确部门时先用 query_dept_by_name 解析',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 10000（一次返回全部，与红外检测设备清单场景匹配）',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_device_group',
    name='查询部门设备分组',
    description=(
      '查询综合管理平台某部门下的设备专业分组与设备组树，返回专业类别（供配电/制冷/'
      '智能化）及其下挂的设备组（如嵌入式服务器、视频监控摄像机、消防主机等）。适用于'
      '「XX 部门有哪些设备专业/分组」「设备怎么分类的」「某专业下有哪些设备组」等查询；'
      '也是按设备分组细看设备信息的前置步骤——分组 typeId 即设备信息记录里的'
      'parentGroupId。dept_id 必填：用户明确部门时先用 query_dept_by_name 按部门名'
      '解析，未明确默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部）。'
    ),
    params={
      'dept_id': '部门 id，必填；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部），用户明确部门时先用 query_dept_by_name 解析',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_device_system_tree',
    name='查询设备系统类型树',
    description=(
      '查询综合管理平台某部门的设备系统类型树，返回一级系统类型（配电/暖通/弱电/消防'
      '系统等）及其下挂的二级子系统，节点含 id、名称、层级。适用于「XX 部门有哪些设备'
      '系统类型」「暖通系统下有哪些子系统」「设备系统树」「设备怎么按系统分类」等查询。'
      'dept_id 必填，默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部）；用户指明'
      '部门时先用 query_dept_by_name 按部门名解析。树节点 id 即查询子系统列表'
      '（query_device_logic_type）时需传的 pid，两者联动。'
    ),
    params={
      'dept_id': '部门 id，必填；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv），用户明确部门时先用 query_dept_by_name 解析',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_device_logic_type',
    name='查询系统类型下的子系统列表',
    description=(
      '按父系统类型 id（pid）分页查询综合管理平台某部门下的具体子系统/逻辑类型列表，'
      '返回子系统名称、父类型、关联设备数 appTypeNumber 等。适用于「XX 系统下有哪些'
      '具体系统」「点击某系统看下级」「各子系统关联多少设备」等查询；也是系统类型树'
      '的点击下钻步骤——pid 从 query_device_system_tree 的树节点 id 获取（如暖通系统'
      '2BHdF5kH4Zq）。dept_id 可选，默认当前登录用户所属部门（服务账号回退技术保障部）。'
    ),
    params={
      'pid': '父系统类型 id，必填（从系统类型树节点获取）',
      'dept_id': '部门 id，可选；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_device_major',
    name='查询设备专业类别',
    description=(
      '查询综合管理平台某部门下的设备专业大类，返回专业类别 id、名称（供配电/制冷/'
      '智能化）。适用于「XX 部门有哪些设备专业」「设备按专业怎么分」「有哪些大类」等'
      '查询；也是按专业过滤设备类型列表（query_device_type）获取 parentId 的前置步骤'
      '——parent_id 即本工具返回的专业类别 id。dept_id 可选，默认当前登录用户所属部门'
      '（服务账号回退技术保障部）。'
    ),
    params={
      'dept_id': '部门 id，可选；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv）',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_device_type',
    name='查询设备类型列表',
    description=(
      '按部门分页查询综合管理平台的设备**类型字典**（有哪些设备类型），返回设备类型 id、'
      '名称、父级专业类别（parentId + deviceMajor）等。适用于「XX 部门有哪些设备类型」'
      '「制冷专业下有哪些设备类型」「设备类型有多少种」等查「类型/分类」的问题。'
      '**注意：本工具只查设备类型字典，不是具体设备实例清单**——用户要查「制冷设备有哪些」'
      '「XX 设备清单/台账」（要具体设备）时，用 query_device_info（设备台账），不要用本工具。'
      'dept_id 可选，默认当前登录用户所属部门（服务账号回退技术保障部）；name 为设备类型'
      '名称关键字（模糊，如「阀门」）；parent_id 为父级专业类别 id（从 query_device_major '
      '获取，如制冷 2AySJZbc31m），可按专业筛选。'
    ),
    params={
      'dept_id': '部门 id，可选；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部 23BaepxKklv）',
      'name': '设备类型名称关键字，可选，模糊匹配（如「阀门」）',
      'parent_id': '父级专业类别 id，可选（从 query_device_major 获取）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='device',
    domain='device',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_pen_company_dept',
    name='查询我的公司及部门（流程服务）',
    description=(
      '返回当前用户工单服务可见的公司列表及各公司下部门，每个公司含 companyId、'
      'kingdeeCompanyId、companyName 与 deptList（deptId+deptName）。适用于解析「XX 数据'
      '中心/机房/分部」对应的**部门 id(dc)** 与**公司 id**——查事件/故障/问题/变更工单'
      '列表时，先用本工具按用户所指的位置（如「廊坊园区A1数据中心」=润泽科技下的 A1分部）'
      '找到该部门的 deptId 和所属公司的 kingdeeCompanyId，再传给对应工单列表工具。'
      '**未指定公司/部门时，默认用当前登录用户所属公司/部门（ownDeptId/ownDeptName、'
      'kingdeeCompanyId/companyName，在本工具返回里可见）；服务账号才回退润泽科技+技术保障部。** '
      '区别于 '
      'query_company_list/query_dept_by_name（mgmt 内部 id）：本工具返回 pen 侧公司/部门'
      '结构且按公司分组，能区分跨公司的同名部门（如多个公司都有 A1分部）。无需参数。'
    ),
    params={},
    category='org',
    domain='company',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_company_list',
    name='查询公司列表',
    description=(
      '查询综合管理平台当前账号可见的公司列表，返回每个公司的 company_id、公司全称、'
      '金蝶公司编码、**gardenName（园区名，如「廊坊园区」「平湖园区」）**等。适用于'
      '「有哪些公司」「XX 公司的全称/ID」「查其他公司的数据」等场景；也是获取 '
      'company_id 的来源——用户要查其他公司的数据时，先在这里拿到该公司的 company_id，'
      '再结合 query_dept_by_name 按部门名解析 dept_id 后传给对应工具。**用户提到园区名'
      '（如「廊坊园区」）时，用本工具按 gardenName 找到对应公司**（如 廊坊园区=润泽科技'
      '发展有限公司），再继续查该公司的部门/人员。无需参数，返回账号可见的全部公司。'
    ),
    params={},
    category='org',
    domain='company',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_user_list',
    name='查询部门人员列表',
    description=(
      '按部门分页查询综合管理平台的部门人员列表，返回每人姓名、uid、手机号、所属部门、'
      '专业（供配电/制冷/智能化）、职务、技术职称、入司时间、工作年限等。适用于「XX '
      '部门有哪些人」「技术保障部人员名单」「某部门一共有多少人」「查某个部门所有人的 '
      'uid」等查询。返回的 uid 可用于后续查询该人员的档案、变更工单、巡检、培训等'
      '（结合 query_person_by_id / query_employee_ticket / query_person_inspection 等）。'
      '**可按专业筛选**：用户问「供配电/制冷/智能化专业的人」时传 profession（供配电/制冷/'
      '智能化/其他）。'
      'dept_id 必填：用户明确部门时先用 query_dept_by_name 按部门名解析，未明确默认'
      '当前登录用户所属部门（ownDeptId，服务账号回退技术保障部）。区别于 '
      'query_person_by_name（按姓名跨部门模糊查人）。'
      '**仅单页明细**：每次调用返回一页（默认 20 条）；需要统计全量分布/汇总/占比'
      '（如「部门人员专业分布」）时，改用 analysis_load 物化全量后再聚合。'
    ),
    params={
      'dept_id': '部门 id，必填；默认当前登录用户所属部门（ownDeptId，服务账号回退技术保障部），用户明确部门时先用 query_dept_by_name 解析',
      'profession': '专业，可选：供配电 / 制冷 / 智能化 / 其他',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='org',
    domain='person',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_dept_list',
    name='查询公司下的部门列表',
    description=(
      '查询综合管理平台某公司下（当前账号可见）的部门列表，返回每个部门的 id、名称、'
      '简称、父部门、状态等。适用于「XX 公司下有哪些部门」「有哪些分部」「某公司的部门'
      '列表」「查部门 id」等查询；也是按部门查人员/设备/维护计划时解析 dept_id 的来源'
      '（查其他公司的数据时先在这里拿部门 id）。company_id 可选，留空默认当前登录用户'
      '所属公司（服务账号回退润泽科技）；查其他公司的部门时，先用 query_company_list 拿到'
      '该公司的 company_id 再传入。区别于 query_dept_by_name（按部门名模糊搜索单个'
      '部门）。'
    ),
    params={
      'company_id': '公司 id，可选；留空默认当前登录用户所属公司，查其他公司时先用 query_company_list 获取',
    },
    category='org',
    domain='dept',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_mindmap',
    name='查询运维管理体系/思维导图',
    description=(
      '查询综合管理平台的文件/思维导图体系树，返回节点 id、标题、类型（1=分类节点，'
      '0=文件节点）及多级子节点。典型场景「数据中心运维管理体系」：根节点为「数据中心'
      '运维管理体系」，下设规划管理、建设管理、运行维护管理等章节，叶子节点为具体制度'
      '文件（如「风险评估与控制管理制度.docx」）。适用于「数据中心运维管理体系里有哪些'
      '内容」「查 XX 制度在哪个章节」「体系怎么分章节」等查询。file_id 必填：数据中心'
      '运维管理体系为 24CiSBO7cxt。树较大时向用户概述层级结构与主要章节即可，用户问'
      '具体制度时再定位对应节点，不要平铺所有节点。节点标题字段是 `topic`——要组装 '
      '<chart type="mind_map"> 时把它映射为 `name`，并按主要章节裁剪到 2~3 级。'
    ),
    params={
      'file_id': '体系/导图文件 id，必填；数据中心运维管理体系为 24CiSBO7cxt',
    },
    category='document',
    domain='document',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_kb_article',
    name='查询 IDC 知识库案例文章',
    description=(
      '查询综合管理平台的共享 IDC 知识库案例文章，返回文章列表（标题、简介、作者、'
      '标签、点赞/收藏数、发布时间等），分页返回，支持按标题关键字模糊过滤、按标签'
      '过滤。适用于「查 XX 案例」「数据中心有哪些运维案例」「某故障/误操作案例怎么'
      '处理、复盘结论是什么」「行业里有哪些相关案例」等查询。行业案例需传 tag_id=3；'
      '不传 tag_id 返回全部（含内部/行业案例）。区别于 query_mindmap（运维管理体系/'
      '思维导图树）——本工具是具体案例文章，命中案例后向用户概述处理经过与结论即可，'
      '不必逐条平铺。'
    ),
    params={
      'title': '文章标题关键字，可选；传则按标题模糊过滤（如「误操作」「变频器」）',
      'tag_id': '文章标签 id，可选；3=行业案例，不传返回全部',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 10',
    },
    category='document',
    domain='document',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_inspection_ticket',
    name='查询巡检工单列表',
    description=(
      '按工单编号、巡检名称、状态、类型等条件查询综合管理平台的巡检工单列表，返回'
      '每条工单的编号、巡检名称、状态等。适用于「查巡检工单」「有哪些待巡检工单」'
      '「某编号的工单」等。type 0=所有 / 1=我巡检的 / 2=我审核的；ticket_number 按'
      '工单编号精确查；page/page_size 分页。区别于 query_inspection_list（巡检执行'
      '记录，按专业/姓名/时间筛选）：本工具查工单台账，取到工单 id 后可进一步调'
      'query_inspection_ticket_detail 看单张工单明细。'
    ),
    params={
      'ticket_number': '巡检工单编号，可选（精确匹配）',
      'name': '巡检名称关键字，可选',
      'status': '巡检状态编码，可选',
      'type': '工单类型：0=所有 / 1=我巡检的 / 2=我审核的，可选',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='inspection',
    domain='inspection',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_inspection_ticket_detail',
    name='查询巡检工单明细',
    description=(
      '按工单 id 查询综合管理平台单张巡检工单的完整明细，返回工单基本信息'
      '（basicInfo：工单号 orderNo、巡检人 inspectorText、专业 majorText、巡检起止'
      '时间、已完成子任务数等）与异常项统计（abnormalStatList：异常点位/图片/处理'
      '措施/转办信息等）。适用于「查 XX 工单的明细/详情/异常项」「这张工单有哪些'
      '异常」等。ticket_id 必填，先调 query_inspection_ticket 拿工单列表取 id。'
    ),
    params={
      'ticket_id': '巡检工单 id，必填（从 query_inspection_ticket 列表结果获取）',
    },
    category='inspection',
    domain='inspection',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_kb_standard',
    name='查询标准规范',
    description=(
      '查询综合管理平台的共享标准规范（国家标准规范）列表，返回每条规范的标题、'
      '作者、标签、创建/更新时间等，分页返回，支持按标题关键字模糊过滤、按标签'
      '过滤。适用于「查 XX 标准规范」「数据中心有哪些国家标准」「XX 标准文档在不在'
      '库里」等查询。区别于 query_kb_article（IDC 知识库案例文章）——本工具查标准'
      '规范文档。命中后向用户概述标题/作者/标签即可，不必逐条平铺。'
    ),
    params={
      'title': '规范标题关键字，可选；传则按标题模糊过滤',
      'tag_id': '规范标签 id，可选；不传返回全部',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 10',
    },
    category='document',
    domain='document',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_pending_ticket',
    name='查询我的工单',
    description=(
      '查询流程平台「我的工单」（仅当前账号本人，用本人 token 查询，不支持查他人），返回'
      '每条工单的编号、名称、级别、类型、状态、部门、创建人等。type 区分工单类别'
      '（1=变更 / 4=问题 / 5=故障 / 6=事件 / 7=阈值，不传查全部类别）；is_done 区分'
      '状态（1=待办 / 2=已办 / 3=已发起 / 4=抄送给我，默认待办）。公司范围：company_id '
      '**不传 = 查全部公司的工单**（回复用户时应说明「未限定公司，返回的是全部公司的'
      '待办」，不要答成「默认公司」——这与变更/故障/问题工单列表默认查当前登录用户所属公司不同）；'
      '传了才按该公司过滤。**注意：本工具是个人「我的工单」口径（仅当前账号本人，'
      '按 is_done 区分 1=待办 / 2=已办 / 3=已发起 / 4=抄送给我，默认待办），不是公司/'
      '部门维度的平台工单列表，不能按部门/时间筛选**；用户查「XX 公司的 XX 工单」「有'
      '哪些 XX 工单」而没说「我的」时，用 query_event_ticket / query_change_ticket / '
      'query_breakdown_ticket / query_question_ticket / query_threshold_ticket / '
      'query_supplier_ticket（公司/部门维度平台列表，支持按部门、时间筛选）。适用于'
      '「我的工单」「我的待办/已办/已发起/抄送」等场景（仅能查本人工单，不支持查他人）。'
      + PENDING_LEVEL_DOC
    ),
    params={
      'is_done': '状态筛选，可选，默认 1=待办：1=待办 / 2=已办 / 3=已发起 / 4=抄送给我',
      'type': '工单类别，可选：1=变更 / 4=问题 / 5=故障 / 6=事件 / 7=阈值；不传查全部类别',
      'company_id': '公司 id，可选。**不传 = 查全部公司的工单**；查某个公司时才传，用 query_company_list / query_pen_company_dept 返回的 pen kingdeeCompanyId（勿传 mgmt 内部 companyId 或公司名）',
      'page': '页码，可选，默认 1',
      'page_size': '每页条数，可选，默认 20',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_change_ticket',
    name='查询变更工单',
    description=(
      '查询 pen 流程平台的变更工单列表，返回工单编号、项目名称、专业、状态、发起人、'
      '时间等。适用于「查变更工单」「有哪些变更单」「XX 公司的变更工单」「XX 部门的'
      '变更工单」「我发起的变更单」等。**与 query_change_statistics 互斥，二选一**：用户要的是'
      '**变更单的统计/汇总**（说「变更统计」「变更数据统计」「变更统计表」「各分部变更单有多少」）时'
      '**用 query_change_statistics**（一次给全公司各部门、不分页）；本工具只用于查**明细列表**'
      '（支持部门/时间/专业/等级筛选，用于出交互表格）。**支持按部门筛选**：dc 参数即部门 id，传了就'
      '按该部门查（不传＝本公司全部部门）——用户问「其他部门」「XX 部门」或部门/分部简称（如 '
      '「A1」即 A1 分部）的变更工单时，先用 query_pen_company_dept 解析出该部门的 '
      'deptId(dc)，**不要把部门/分部名放进 key_word**（key_word 只模糊匹配项目名称/'
      '工单号，匹配不到部门）。'
      + _TICKET_DC_DEFAULT_NOTE
      + _TICKET_SCOPE_NOTE
      + 'type 1=全部/2=我发起的/4=我处理的/5=我验收的/6=我监督的；ticket_type '
      '变更等级 1=低风险/2=中风险/3=高风险；specialty 专业 1=供配电/2=制冷/4=智能化/8=其他。'
      + render_return_doc('change')
    ),
    params={
      'company_id': '公司 id，可选；默认当前登录用户所属公司。指定公司/跨公司时用 query_company_list 或 query_pen_company_dept 返回的 pen kingdeeCompanyId 传入（勿传 mgmt 内部 companyId 或公司名）',
      'all_companies': '是否查**全国（所有公司/园区）**，可选；报告/跨园区统计时传 true —— 服务端会逐公司串行拉取并合并，每条记录带 companyName（按园区聚合用）。不传＝只查上面 company_id 指定的那家公司',
      'type': '查询范围，可选，默认 1=全部 / 2=我发起的 / 4=我处理的 / 5=我验收的 / 6=我监督的',
      'ticket_type': '变更等级，可选：1=低风险 / 2=中风险 / 3=高风险',
      'specialty': '专业，可选：1=供配电 / 2=制冷 / 4=智能化 / 8=其他',
      'ticket_no': '工单编号，可选',
      'project_name': '项目名称，可选（模糊）',
      'key_word': '关键字，可选；只模糊匹配项目名称/工单号，部门/分部名不算，用 dc 筛',
      'modify_type': '变更类型，可选：1=计划型变更 / 2=非计划型变更 / 3=紧急变更',
      'stage': '阶段，可选',
      'dc': _TICKET_DC_DOC,
      'all_dept': _TICKET_ALL_DEPT_DOC,
      'start_time': '开始时间，可选，YYYY-MM-DD',
      'end_time': '结束时间，可选，YYYY-MM-DD（查“X至今/到现在”时传当天；只传 start 缺 end 接口会按全量历史返回，务必成对传）',
      'only_time_out': '只看超时，可选',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_change_statistics',
    name='查询变更单统计',
    description=(
      '查询变更单统计：一次拿到某公司一段时间内的**全部变更单**（不分页，一单一行），含'
      '部门 / 工单号 / 项目名称 / 变更类型 / 创建时间 / 计划开始·结束时间。适用于'
      '「各分部变更单数量」「变更单按部门统计」「这段时间哪个部门变更最多」「变更统计」'
      '「变更单数据统计」这类**按部门/跨部门对比**的统计口径。'
      '**与 query_change_ticket 互斥，二选一**：用户说「**变更统计**」「**变更数据统计**」'
      '「**变更统计表**」「各分部变更单数量」这类**要统计/汇总**的话时，**只用本工具**——'
      '一次调用即返回全公司各部门的变更单；**不要**改用 `query_change_ticket` 拉全量列表再在'
      '沙箱里 `value_counts`（更慢，且可能被分页截断）。反过来，用户要的是**明细/列表/工单内容'
      '本身**（列出、查看、按专业/等级/时间筛具体单子）时**用 `query_change_ticket`**，不要用本工具。'
      '**回复用户时必须先说明本次查询的公司与时间范围**：未指定公司时默认查当前登录用户所属'
      '公司（kingdeeCompanyId/companyName）；服务账号（用户未绑定 mgmt 账号）才回退润泽科技；'
      '用户指定其他公司时，说明实际查询的公司名。'
    ),
    params={
      'company_id': '公司 id，可选；默认当前登录用户所属公司。指定公司/跨公司时用 query_company_list 或 query_pen_company_dept 返回的 pen kingdeeCompanyId 传入（勿传 mgmt 内部 companyId 或公司名）',
      'all_companies': '是否查**全国（所有公司/园区）**，可选；报告/跨园区统计时传 true —— 服务端会逐公司串行拉取并合并，每条记录带 companyName（按园区聚合用）。不传＝只查上面 company_id 指定的那家公司',
      'start_time': '开始时间，可选，YYYY-MM-DD（内部转毫秒）',
      'end_time': '结束时间，可选，YYYY-MM-DD（内部转毫秒；只给 start 时 end 自动补当前）',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_breakdown_ticket',
    name='查询故障工单列表',
    description=(
      '查询 pen 流程平台的故障工单列表，返回工单编号、故障名称、级别、状态、发起人、'
      '时间等。适用于「查故障工单」「有哪些故障单」「XX 公司的故障工单」「XX 部门的'
      '故障工单」等。**支持按部门筛选**：dc 参数即部门 id，传了就按该部门查（不传＝本公司'
      '全部部门）——用户问「其他部门」「XX 部门」或部门/分部简称（如「A1」即 A1 分部）'
      '的故障工单时，先用 query_pen_company_dept 解析出该部门的 deptId 再作为 dc 传入，'
      '**不要把部门/分部名放进 key_word**（key_word 只模糊匹配故障名称/工单号，匹配'
      '不到部门）。'
      + _TICKET_DC_DEFAULT_NOTE
      + _TICKET_SCOPE_NOTE
      + 'key_word 支持故障名称/工单号模糊查询；break_down_level 故障级别'
      '筛选，可选。与 query_change_ticket（变更工单列表）同构，仅端点与故障级别筛选项不同。'
    ),
    params={
      'company_id': '公司 id，可选；默认当前登录用户所属公司。指定公司/跨公司时用 query_company_list 或 query_pen_company_dept 返回的 pen kingdeeCompanyId 传入（勿传 mgmt 内部 companyId 或公司名）',
      'all_companies': '是否查**全国（所有公司/园区）**，可选；报告/跨园区统计时传 true —— 服务端会逐公司串行拉取并合并，每条记录带 companyName（按园区聚合用）。不传＝只查上面 company_id 指定的那家公司',
      'type': '查询范围，可选，默认 1=全部（与变更工单列表一致）',
      'ticket_type': '专业码，可选：1=供配电 / 2=制冷 / 4=智能化',
      'break_down_level': '故障级别，可选',
      'ticket_no': '工单编号，可选',
      'project_name': '项目名称，可选（模糊）',
      'key_word': '关键字，可选（支持故障名称/工单号模糊查询；部门/分部名不算，用 dc 筛）',
      'stage': '阶段（进度），可选：7=故障派发 / 2=故障处理 / 3=故障验收 / 5=故障关闭',
      'dc': _TICKET_DC_DOC,
      'all_dept': _TICKET_ALL_DEPT_DOC,
      'start_time': '开始时间，可选，YYYY-MM-DD',
      'end_time': '结束时间，可选，YYYY-MM-DD（查“X至今/到现在”时传当天；只传 start 缺 end 接口会按全量历史返回，务必成对传）',
      'only_time_out': '只看超时，可选',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_question_ticket',
    name='查询问题工单列表',
    description=(
      '查询 pen 流程平台的问题工单列表，返回工单编号、问题名称、级别、状态、发起人、'
      '时间等。适用于「查问题工单」「有哪些问题单」「XX 公司的问题工单」「XX 部门的'
      '问题工单」等。**支持按部门筛选**（dc 参数，同变更/故障工单列表）：用户问部门/'
      '分部（含简称如「A1」即 A1 分部）的问题工单时，先用 query_pen_company_dept 解析'
      '出 deptId 作为 dc 传入，**不要把部门/分部名放进 key_word**（key_word 只模糊匹配'
      '问题名称/工单号，匹配不到部门）；company_id 可选跨公司；severity_level 问题级别'
      '筛选，'
      + _TICKET_SCOPE_NOTE
      + '可选。与 query_change_ticket（变更工单列表）同构，仅端点与问题级别/专业筛选项'
      '不同（无 ticketType/modifyType/onlyTimeOut）。'
      + render_return_doc('question')
    ),
    params={
      'company_id': '公司 id，可选；默认当前登录用户所属公司。指定公司/跨公司时用 query_company_list 或 query_pen_company_dept 返回的 pen kingdeeCompanyId 传入（勿传 mgmt 内部 companyId 或公司名）',
      'all_companies': '是否查**全国（所有公司/园区）**，可选；报告/跨园区统计时传 true —— 服务端会逐公司串行拉取并合并，每条记录带 companyName（按园区聚合用）。不传＝只查上面 company_id 指定的那家公司',
      'type': '查询范围，可选，默认 1=全部（与变更工单列表一致）',
      'severity_level': '问题级别，可选',
      'specialty': '专业，可选：1=供配电 / 2=制冷 / 4=智能化 / 8=其他',
      'ticket_no': '工单编号，可选',
      'project_name': '项目名称，可选（模糊）',
      'key_word': '关键字，可选（支持问题名称/工单号模糊查询；部门/分部名不算，用 dc 筛）',
      'stage': '阶段，可选',
      'dc': _TICKET_DC_DOC,
      'all_dept': _TICKET_ALL_DEPT_DOC,
      'start_time': '开始时间，可选，YYYY-MM-DD',
      'end_time': '结束时间，可选，YYYY-MM-DD（查“X至今/到现在”时传当天；只传 start 缺 end 接口会按全量历史返回，务必成对传）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_threshold_ticket',
    name='查询阈值工单列表',
    description=(
      '查询 pen 流程平台的阈值工单列表，返回工单编号、阈值项、专业、状态、发起人、'
      '时间等。适用于「查阈值工单」「有哪些阈值单」「XX 公司的阈值工单」「XX 部门的'
      '阈值工单」等。**支持按部门筛选**（dc 参数）：用户问部门/分部（含简称如「A1」'
      '即 A1 分部）的阈值工单时，先用 query_pen_company_dept 解析出 deptId 作为 dc '
      '传入，**不要把部门/分部名放进 key_word**（key_word 只模糊匹配名称/工单号，'
      '匹配不到部门）；company_id 可选跨公司；specialty 专业筛选 1=供配电/2=制冷/'
      '4=智能化/8=其他。与 query_question_ticket 同构（DTO 共用），仅端点与专业枚举'
      '不同。**注意：这是公司/部门维度的平台工单列表，不是当前账号的个人待办/已办/'
      '已发起/抄送**（个人口径用 query_pending_ticket）。**查询结果为空时回复「当前'
      '公司/部门没有符合条件的阈值工单」，严禁说「无待办、已办、已发起或抄送的阈值'
      '工单」**。'
      + _TICKET_SCOPE_NOTE
      + render_return_doc('threshold')
    ),
    params={
      'company_id': '公司 id，可选；默认当前登录用户所属公司。指定公司/跨公司时用 query_company_list 或 query_pen_company_dept 返回的 pen kingdeeCompanyId 传入（勿传 mgmt 内部 companyId 或公司名）',
      'all_companies': '是否查**全国（所有公司/园区）**，可选；报告/跨园区统计时传 true —— 服务端会逐公司串行拉取并合并，每条记录带 companyName（按园区聚合用）。不传＝只查上面 company_id 指定的那家公司',
      'type': '查询范围，可选，默认 1=全部（与其它工单列表一致）',
      'specialty': '专业，可选：1=供配电 / 2=制冷 / 4=智能化 / 8=其他',
      'ticket_no': '工单编号，可选',
      'project_name': '项目名称，可选（模糊）',
      'key_word': '关键字，可选；只模糊匹配名称/工单号，部门/分部名不算，用 dc 筛',
      'stage': '阶段，可选',
      'dc': _TICKET_DC_DOC,
      'all_dept': _TICKET_ALL_DEPT_DOC,
      'start_time': '开始时间，可选，YYYY-MM-DD',
      'end_time': '结束时间，可选，YYYY-MM-DD（查“X至今/到现在”时传当天；只传 start 缺 end 接口会按全量历史返回，务必成对传）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_supplier_ticket',
    name='查询供应商服务工单列表',
    description=(
      '查询 pen 流程平台的供应商服务工单列表，返回工单编号、服务项、状态、发起人、'
      '时间等。适用于「查供应商服务工单」「有哪些供应商工单」「XX 公司的供应商服务工单」'
      '等。**支持按部门筛选**（dc 参数）：用户问部门/分部（含简称如「A1」即 A1 分部）'
      '的供应商服务工单时，先用 query_pen_company_dept 解析出 deptId 作为 dc 传入，'
      '**不要把部门/分部名放进 key_word**（key_word 只模糊匹配服务项目名称/工单号，匹配不到'
      '部门）；company_id 可选跨公司；serviceType 服务类型筛选（1=例行维护/2=故障检修/'
      '3=技术培训/0=其他），可选。与 query_threshold_ticket '
      '同构（DTO 共用），仅端点与筛选项（serviceType 替代 specialty、无时间筛选）不同。'
      + _TICKET_SCOPE_NOTE
      + render_return_doc('supplier')
    ),
    params={
      'company_id': '公司 id，可选；默认当前登录用户所属公司。指定公司/跨公司时用 query_company_list 或 query_pen_company_dept 返回的 pen kingdeeCompanyId 传入（勿传 mgmt 内部 companyId 或公司名）',
      'all_companies': '是否查**全国（所有公司/园区）**，可选；报告/跨园区统计时传 true —— 服务端会逐公司串行拉取并合并，每条记录带 companyName（按园区聚合用）。不传＝只查上面 company_id 指定的那家公司',
      'type': '查询范围，可选，默认 1=全部（与其它工单列表一致）',
      'service_type': '服务类型，可选：1=例行维护 / 2=故障检修 / 3=技术培训 / 0=其他',
      'ticket_no': '工单编号，可选',
      'project_name': '项目名称，可选（模糊）',
      'key_word': '关键字，可选；只模糊匹配服务项目名称/工单号，部门/分部名不算，用 dc 筛',
      'stage': '阶段，可选',
      'dc': _TICKET_DC_DOC,
      'all_dept': _TICKET_ALL_DEPT_DOC,
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_event_ticket',
    name='查询事件工单列表',
    description=(
      '查询 pen 流程平台的事件工单列表，返回工单编号、事件名称、级别、状态、发起人、'
      '时间等。适用于「查事件工单」「事件单」「XX 公司/部门的事件单」「XX 数据中心昨天/'
      '某时间段的事件单」等。**用户问事件工单而没说「我的」时用本工具**（区别于'
      'query_pending_ticket——那是查「我的工单/待办」，走的是当前账号本人的待办，不能按'
      '部门/时间筛选）。**支持按部门筛选**（dc 参数）与按时间筛选（start_time/end_time，'
      'YYYY-MM-DD，内部转毫秒）；company_id 可选跨公司；severity_level 事件级别、specialty '
      '专业、key_word 关键字（事件名称/工单号模糊）均可选。用户指明数据中心/机房/分部'
      '（含简称如「A1」即 A1 分部）时，先调 query_pen_company_dept 解析出该位置的 '
      'deptId(dc) 与公司 id 再传入，**不要把部门/分部名放进 key_word**（key_word 只'
      '模糊匹配事件名称/工单号，匹配不到部门）。'
      ''
      + _TICKET_SCOPE_NOTE
      + '与'
      'query_question_ticket 同构（DTO 共用），仅端点不同。'
      + render_return_doc('event')
    ),
    params={
      'company_id': '公司 id，可选；默认当前登录用户所属公司。指定公司/跨公司时用 query_company_list 或 query_pen_company_dept 返回的 pen kingdeeCompanyId 传入（勿传 mgmt 内部 companyId 或公司名）',
      'all_companies': '是否查**全国（所有公司/园区）**，可选；报告/跨园区统计时传 true —— 服务端会逐公司串行拉取并合并，每条记录带 companyName（按园区聚合用）。不传＝只查上面 company_id 指定的那家公司',
      'dc': _TICKET_DC_DOC,
      'all_dept': _TICKET_ALL_DEPT_DOC,
      'type': '查询范围，可选，默认 1=全部（与其它工单列表一致）',
      'severity_level': '事件级别，可选',
      'specialty': '专业，可选：1=供配电 / 2=制冷 / 4=智能化 / 8=其他',
      'ticket_no': '工单编号，可选',
      'project_name': '项目名称，可选（模糊）',
      'key_word': '关键字，可选（支持事件名称/工单号模糊查询；部门/分部名不算，用 dc 筛）',
      'stage': '阶段，可选',
      'only_time_out': '只看超时，可选',
      'start_time': '开始时间，可选，YYYY-MM-DD',
      'end_time': '结束时间，可选，YYYY-MM-DD（查“X至今/到现在”时传当天；只传 start 缺 end 接口会按全量历史返回，务必成对传）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_breakdown_ticket_detail',
    name='查询故障工单详情',
    description=(
      '按工单 id 查询单张故障工单的完整详情（故障名称、描述、发起人及手机号、受理人、'
      '参与人、发生时间地点、级别、阶段、现场图片、涉及设备、审批流程、操作记录等）。'
      '适用于「查看 XX 工单的详情」「这张故障工单的具体情况」等。id 从 query_pending_ticket'
      '（待办/我的工单列表）返回的 id 字段获取；区别于 query_pending_ticket（待办列表）'
      '与 query_change_ticket（变更工单列表）。'
    ),
    params={
      'ticket_id': '工单 id，必填（从 query_pending_ticket 待办列表返回的 id 获取）',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_question_ticket_detail',
    name='查询问题工单详情',
    description=(
      '按工单 id 查询单张问题工单的完整详情（问题名称、描述、问题来源、分派人/处理人、'
      '发生时间地点、专业、级别、阶段、涉及设备、频率、计划/交付时间、现场图片等）。'
      '适用于「查看 XX 问题工单的详情」「这张问题工单的具体情况」等。id 从 '
      'query_pending_ticket（待办/我的工单列表）返回的 id 字段获取；区别于'
      'query_breakdown_ticket_detail（故障工单）与 query_event_ticket_detail（事件工单）'
      '——三者详情端点不同，按工单类型选对应工具。'
    ),
    params={
      'ticket_id': '工单 id，必填（从 query_pending_ticket 待办列表返回的 id 获取）',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_event_ticket_detail',
    name='查询事件工单详情',
    description=(
      '按工单 id 查询单张事件工单的完整详情（事件名称、事件描述、事件类型、原因分析、'
      '处理人/处理流程、关联工单、影响范围/时长、发生时间地点、级别、阶段、现场图片等）。'
      '适用于「查看 XX 事件工单的详情」「这张事件工单的具体情况」等。id 从 '
      'query_pending_ticket（待办/我的工单列表）返回的 id 字段获取；区别于'
      'query_breakdown_ticket_detail（故障工单）与 query_question_ticket_detail（问题'
      '工单）——三者详情端点不同，按工单类型选对应工具。'
    ),
    params={
      'ticket_id': '工单 id，必填（从 query_pending_ticket 待办列表返回的 id 获取）',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_change_ticket_detail',
    name='查询变更工单详情',
    description=(
      '按工单 id 查询单张变更工单的完整详情（工单编号、项目名称、专业、状态、发起人、'
      '执行人、审批流程、涉及设备、操作记录、原因/方案/风险识别与控制措施等）。适用于'
      '「查看 XX 变更单的详情」「这张变更工单的具体情况」等。id 从 query_change_ticket'
      '（变更工单列表）返回的 ticketId 字段获取（注意不是 id）；与'
      'query_breakdown_ticket_detail/query_question_ticket_detail/'
      'query_event_ticket_detail 区分——本能力查的是变更类工单详情，其余三个查'
      '故障/问题/事件类工单详情，四者详情端点各不相同，按工单类型选对应工具。'
      '实测确认：不按 token 归属人区分，任意有效 token 可查任意人发起的工单详情。'
    ),
    params={
      'ticket_id': '变更工单 id，必填（从 query_change_ticket 列表结果的 ticketId 字段获取）',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_supplier_ticket_detail',
    name='查询供应商服务工单详情',
    description=(
      '按工单 id 查询单张供应商服务工单的完整详情（工单编号、服务类型、服务商、项目、'
      '状态、发起人、服务内容/方案、计划/实际时间、涉及设备、审批流程、操作记录等）。'
      '适用于「查看 XX 供应商服务工单的详情」「这张供应商工单的具体情况」等。id 从 '
      'query_supplier_ticket（供应商服务工单列表）返回的 ticketId 字段获取（注意不是 '
      'id）；与 query_breakdown_ticket_detail/query_question_ticket_detail/'
      'query_event_ticket_detail/query_change_ticket_detail 区分——本能力查的是供应商'
      '服务工单详情，各工单类型详情端点不同，按工单类型选对应工具。'
    ),
    params={
      'ticket_id': '供应商服务工单 id，必填（从 query_supplier_ticket 列表结果的 ticketId 字段获取）',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_threshold_ticket_detail',
    name='查询阈值工单详情',
    description=(
      '按工单 id 查询单张阈值工单的完整详情（工单编号、阈值项、专业、状态、发起人、'
      '阈值内容/判定、计划/实际时间、涉及设备、审批流程、操作记录等）。适用于'
      '「查看 XX 阈值工单的详情」「这张阈值工单的具体情况」等。id 从 '
      'query_threshold_ticket（阈值工单列表）返回的 ticketId 字段获取（注意不是 id）；'
      '与 query_breakdown_ticket_detail/query_question_ticket_detail/'
      'query_event_ticket_detail/query_change_ticket_detail/query_supplier_ticket_detail '
      '区分——本能力查的是阈值工单详情，各工单类型详情端点不同，按工单类型选对应工具。'
    ),
    params={
      'ticket_id': '阈值工单 id，必填（从 query_threshold_ticket 列表结果的 ticketId 字段获取）',
    },
    category='work_order',
    domain='work_order',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_config_template_list',
    name='查询配置管理列表',
    description=(
      '查询综合管理平台的配置管理模板列表（专项检查表/固定模板等），返回每条'
      '模板的 id、编号、类型、名称、级别、状态、创建人、创建/更新时间、关联文件等。适用于'
      '「有哪些检查表模板」「查 XX 专项检查表」「配置管理模板」等。template_name 模糊匹配、'
      'type 精确过滤（如 专项检查 / 固定），均可选。'
    ),
    params={
      'template_name': '模板名称，可选，模糊匹配',
      'type': '模板类型，可选，精确过滤（如 专项检查 / 固定）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='config',
    domain='config',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_task_list',
    name='查询任务列表',
    description=(
      '查询综合管理平台的配置任务列表（由专项检查表/固定模板生成的任务工单），返回每条'
      '任务的 id、名称、编号（RZKJ-RW-…）、关联模板、执行人、起止/创建时间、状态等。适用于'
      '「有哪些任务」「XX 专项检查任务」「查任务执行情况」等。type 默认 1=全部；'
      'template_name 模糊匹配、status/executor_name 精确过滤，均可选。'
    ),
    params={
      'type': '任务类型，可选，默认 1=全部',
      'dept_id': '部门 id，可选，默认当前登录用户所属部门（服务账号回退）',
      'template_name': '模板名称，可选，模糊匹配',
      'status': '状态，可选，精确过滤',
      'executor_name': '执行人姓名，可选，精确过滤',
      'task_ticket_no': '任务工单号，可选，精确过滤',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='config',
    domain='config',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_review_appointment',
    name='查询评审预约列表',
    description=(
      '按类型、部门、公司、时间范围查询综合管理平台的评审预约列表（变更评审会议预约'
      '安排），返回每条预约的编号、变更名称、变更专业、变更类型、级别、申请人、'
      '预约评审时间、改期时间、状态等。适用于「查评审预约」「XX 的变更评审安排在'
      '什么时候」「有哪些待评审的变更」等查询。是变更工单（query_change_ticket）'
      '审批流程中「评审会议预约」这一环节的记录，两者可结合查看变更全流程。'
      'dept_id/company_id 留空时用服务账号默认部门/公司（技术保障部/润泽科技），'
      '此时必须明确告知用户「默认查询的是技术保障部」，并提示可指定其他部门/公司。'
      'start_time/end_time 可选，格式 YYYY-MM-DD，不传查全部时间范围。'
    ),
    params={
      'type': '预约类型编码，可选，含义待核实，默认 0',
      'dept_id': '部门 id，可选；留空默认当前登录用户所属部门（服务账号回退技术保障部），需告知用户默认范围',
      'company_id': '公司 id，可选；留空默认当前登录用户所属公司（服务账号回退润泽科技），需告知用户默认范围',
      'start_time': '开始时间（格式 YYYY-MM-DD），可选，不传查全部时间范围',
      'end_time': '结束时间（格式 YYYY-MM-DD），可选，不传查全部时间范围',
      'is_all': '是否查全部（不分页），可选，含义待核实，默认 false',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 20',
    },
    category='review',
    domain='review',
    kind='资源',
  ),
  Capability(
    id='mgmt.query_review_appointment_detail',
    name='查询评审预约详情',
    description=(
      '按预约 id 查询综合管理平台单条评审预约的完整详情（变更名称、变更专业、变更'
      '类型、级别、申请人、预约评审时间、改期时间、状态等完整字段）。适用于「查看 '
      'XX 评审预约的详情」「这条评审预约的具体安排」等。appointment_id 从 '
      'query_review_appointment（评审预约列表）结果里每条记录的 id 字段获取。'
    ),
    params={
      'appointment_id': '评审预约 id，必填（从 query_review_appointment 列表结果的 id 字段获取）',
    },
    category='review',
    domain='review',
    kind='资源',
  ),
)

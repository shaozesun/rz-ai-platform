# 平台接口能力清单

> 数据源：`service/query/{mgmt,dcim_meta,security}/capabilities.py` 的 `CAPABILITIES`。
> 每条能力即 Agent 可调用的一个查询/执行工具；「分类」为意图路由的统一词汇。
> 每条能力标注其实际 HTTP 接口（method + path），接口定义见 `service/query/mgmt/endpoints.py`（单接口）与 `service/query/mgmt/client.py`（组合能力）。
> 「参数」为该能力支持的可传参数（`capabilities.py` 每条 Capability 的 `params`，参数名用 snake_case，必填/可选及含义见代码描述）。
> **加粗**的参数为**支持用户筛选/指定的查询条件**（如设备名、状态、时间范围、工单类别、部门等）；未加粗的为分页参数（`page`/`page_size`）或服务账号上下文兜底参数（`dept_id`/`company_id` 自动取默认）。

## 平台 Base URL

| 别名 | 实际地址 | 平台 |
|---|---|---|
| `MGMT` | `http://192.168.88.18/center` | 综合管理平台（.env `MGMT_BASE_URL`） |
| `INSPECT` | `http://192.168.88.18` | 巡检服务，根路径（.env `MGMT_INSPECTION_BASE_URL`，无 `/center` 前缀） |
| `TICKET` | `https://ticket-api.ddbes.com` | 工单服务（.env `MGMT_TICKET_BASE_URL`；2026-10-26 对方整体重做后工单接口都迁到这里，认证同 mgmt `token`+`Deptid`） |
| `PEN` | `https://api.ddbes.com` | 老 pen 流程服务（.env `MGMT_PEN_BASE_URL`）；工单接口已迁走，`pen_request()` 保留未删但不再被调用 |

> 工单列表/详情（`TICKET` 接口）为组合能力：直接按当前用户 mgmt token 归属人鉴权（路径去掉老的 `/ticket`、`/pen` 前缀）；公司/部门维度，`dc` 部门 id 与 `query_dept_by_name` 返回一致。迁移记录见 `docs/problems/pen-ticket-api-migration.md`。

## 总览

| 平台 | 能力数 |
|---|---|
| 综合管理平台（mgmt） | 54 |
| 动力环境数据监控（dcim_meta） | 9 |
| 安防平台（security） | 0 |
| **合计** | **63** |

## 动力环境数据监控（dcim_meta）

> **无 HTTP 后端**：与 mgmt/security 不同，本平台不调对方接口，**全部查本地库**。
> 测点**数值**在 TDengine 宽表（一个设备类型一张超级表、一台设备一张子表、测点做列），
> 空间/设备/测点的**定义**在 PostgreSQL。PG 没有连 TDengine 的 FDW，跨库 JOIN 做不到 →
> 只能「PG 定范围 → TDengine 取值 → 应用层合并」两段式（`service/query/dcim_meta/client.py`）。
> 数据由 Kafka 接入（`service/dcim_ingest/`，见 `docs/dcim-kafka-ingest-design.md`）。开关：`DCIM_ENABLED`。
> **时间口径**：dcim_meta 所有返回的时间字段（`ts` / `latest_ts` / `_wstart` / `report_ts` / `alarm_time` / `remove_time` / `updated_at`）
> 已在数据层渲染为**北京时间**（`YYYY-MM-DD HH:MM:SS`，Asia/Shanghai），调用方直接展示，**不要再做时区换算**。
> （`DCIM_BASE_URL` / `DCIM_API_TOKEN` / `DCIM_TIMEOUT` / `DCIM_RETRIEVAL_TOP_K` 是遗留配置项，
> 留给将来接对方 HTTP 接口用。）
>
> 注：原 `service/query/dcim/` 的 6 条能力（`query_alarm`、`query_alarm_history`、`query_asset`、
> `query_asset_count`、`query_capacity`、`query_power_usage`）是 Phase 1 的**门面 mock**，
> `client.py` 里写死假数据、无真实实现，已整体删除。

### 空间（asset）

- 查询空间树（`dcim_meta.query_space_tree`）→ 本地 PostgreSQL（递归子空间）；参数：**cluster**、**keyword**、**idc**

### 设备（device）

- 查询动力环境监控设备台账（`dcim_meta.query_devices`）→ 本地 PostgreSQL；参数：**cluster**、**space_guid**、**device_type**、**keyword**、page、page_size
- 查询测点定义与现状（`dcim_meta.query_points`，`with_latest=true` 时带当前值）→ PG 定义 + TDengine 当前值；参数：**cluster**、**space_guid**、**device_guid**、**keyword**、**with_latest**、page、page_size。**带 `keyword` 且命中不多时（≤30 条）即使 `with_latest=true` 也回 `device_guid`/`device_type`** —— 这条链路下一步就是查时序，缺了它们模型会把测点的 `idc` 当 guid 传（2026-10-22 实测）
- 查询测点数值时序（`dcim_meta.query_series`）→ TDengine 宽表；参数：**cluster**、**device_type**、**device_guids**、**point_ids**、**space_guid**、**start_ms**、**end_ms**、**interval**、**agg**、page、page_size。⚠️ **一行 = 一台设备在某时刻的全部测点**（一台 PDU 就 1,204 列、单行 ≈29KB）→ 看单个/几个测点的曲线**必须带 `point_ids`**（`query_points` 返回的 `point_id`，服务端经 `dcim_td_column` 映射成 TD 列名并只 `SELECT` 这几列）；不给就会整行返回，被 12,000 字符的工具输出上限截成 1 行、画不出趋势。`point_ids` 与 `device_guids` 配对即可，设备类型由服务端按 PG 档案自动定位。**设备 guid 不存在时显式报错**（不会静默返回 0 行）—— 点位的 `idc`/空间编码不是设备 guid
- 跨库一致性巡检（`dcim_meta.check_consistency`）→ PG 定义 × TDengine 实测；参数：**cluster**、**device_type**

### 告警（alarm）

- 超阈值测点清单（`dcim_meta.query_over_threshold`）→ PG 阈值 + TDengine 当前值，应用层比对；参数：**cluster**、**space_guid**、**device_guid**、**keyword**、**level**
- 查询告警事件（`dcim_meta.query_alarms`）→ PG `dcim_alarms`（Kafka `ems-alarm-data` 接入）；参数：**cluster**、**park**、**space_guid**、**device_guid**、**keyword**、**level**、**status**、**start_ms**、**end_ms**、**unresponded_min**、page、page_size（`start_ms`/`end_ms` 接受毫秒 / 秒级 / ISO；**`end_ms` 只传日期＝含当天整天**；返回的 `alarm_time`/`remove_time` 已渲染成北京时间，`status`/`alarm_level`/`alarm_type` 已解码成中文；**`space_name`＝空间全名**（园区+数据中心+模组+楼层+房间）—— 报文只带 `space_guid`，名称由查询时 `LEFT JOIN dcim_spaces`（`(cluster, guid)` 主键 1:1，不放大行数）取回，`keyword` 同时匹配空间名；**`unresponded_min=N` = 只看发生超过 N 分钟、仍未解除（`status<>'3'`）、且 `NOT EXISTS` 任何关联处置工单的告警**（= 还没人响应；人员点响应会自动建单，有单即已响应。「告警响应超时」的标准口径＝N=5 配 `level=重要`＝重要及以上，完整说明见技能 `skill__alarm_response_timeout`）—— 与 `query_alarm_orders` 的 `response_timeout_min`（已响应但慢）是两回事；**每条告警若已建处置工单，自带嵌套 `work_order`** —— 同库 `LEFT JOIN LATERAL … LIMIT 1` 取最新一单，不放大行数与总数）
- 查询告警工单（`dcim_meta.query_alarm_orders`）→ PG `dcim_alarm_orders`（Kafka `ems-alarm-order-rz` 接入，`guid = orderNo`，同一单多次推送 upsert 成一行），同库 LEFT JOIN `dcim_alarms` 带出 `alarm_status`；参数：**cluster**、**park**、**space_guid**、**device_guid**、**event_log_id**、**keyword**、**operator**、**level**、**status**（默认 `all`）、**start_ms**、**end_ms**（按告警发生时间；**`end_ms` 只传日期＝含当天整天**）、**response_timeout_min**、page、page_size。**`park`（园区名，如「廊坊」）展开成该园区全部代号走 `= ANY`** —— 一次查询、`summary` 就是整园的（逐个传代号查回来的 P50/P90 合不出来）；与 `cluster` 二选一（同时传以 `cluster` 为准），未知园区报错而不是退化成「不过滤」。返回 `records` + `total` + **`summary`**（按整个过滤集：处理中/已处理、各档级别、响应时长 avg/P50/P90 分钟）；`response_minutes`/`handle_minutes` 已在服务端算好，时间/码值已中文化。**`response_timeout_min=N` = 以工单为主表筛「响应时刻 − 告警时刻 > N 分钟」的单**（`level=重要` 即「重要 + 紧急」），`summary` 也随之只统计这批；**尚未响应的工单不在其中**（无 `response_time` 或早于告警的脏数据被 `_RESP_OK` 剔除，属「没响应」而非「响应超时」）。**「工单已关、告警仍未解除」（提前关单）**：没有专门参数，取 `status=2` 后按返回的 `alarm_status` 过滤，口径见技能 `skill__alarm_premature_close`

> `query_alarms` 与 `query_over_threshold` **刻意分开、别混**：前者是**对方告警引擎的事件**（触发/刷新/恢复三态、有生命周期），
> 后者是**我方算的「此刻值越没越限」**（无生命周期、无恢复态）。「现在有哪些告警在跑」用前者，「巡检一下有没有超限」用后者。
> `query_alarm_orders` 是第三类：**人工处置记录**（处理中/已处理、响应人、响应时长、处理结果），与告警事件用 `event_log_id` 关联。
> 「告警在不在跑」看 `query_alarms`，**单条告警的处置情况也在 `query_alarms` 返回的嵌套 `work_order` 里**；
> `query_alarm_orders` 用于按人 / 按时段 / 跨告警**批量**看处置情况（带全量 `summary`）。

### 能耗（energy）

- 查询 PUE 与能耗统计（`dcim_meta.query_pue`）→ TDengine 独立超级表 `st_pue`（Kafka `ems-real-data-rz` 里**无 `device_id`、有 `building`** 的那类报文；见 `docs/dcim-pue-storage-query.md`）；参数：**cluster**、**park**、**building**、**space**、**time_type**、**metrics**、**start_ms**、**end_ms**、**latest**、**interval**、**agg**、page、page_size。**一栋楼 × 一个时间口径 × 18 个指标**（`pue` = PUE 本身，其余为各项耗电量）。⚠️ `time_type` 默认 `real`（约 10 秒一条），「小时 / 日 / 周 / 月 / 年」是对方按整点或期末推的统计快照，**必须显式传**（如 `hour`）；**不同口径不能互比或相加**。`latest=true` 只看每个「楼宇 × 口径」最新一行（问当前值用它，不需要时间窗）；**非 latest 必须给时间窗**，时段长要带 `interval` 聚合（实时值一天 8,640 行）。返回 `records` + `total` + **`metric_labels`**（列名→中文），`ts`/`_wstart` 已是北京时间，每行带 `space_name`（空间全名）。**只有上游真推了 PUE 的楼宇才有数据**，查回 0 行 ≠ PUE 为 0

## 综合管理平台（mgmt）

### 组织人事（org）

- 查询我的账号信息（`mgmt.query_my_info`）→ 无独立 HTTP 接口（认证中心登录档案，`client.py _query_my_info`）；无参数
- 查询我的公司及部门（`mgmt.query_pen_company_dept`，工单服务）→ GET `{TICKET}/company/user/{kingdeeUid}`；无参数
- 按姓名查询人员信息（`mgmt.query_person_by_name`）→ GET `{MGMT}/user/info/v2/{name}`；参数：**name**
- 按部门名称模糊查询部门（`mgmt.query_dept_by_name`）→ GET `{MGMT}/dept/v1/dept/{deptName}`；参数：**dept_name**
- 按 uid 查询人员详情（`mgmt.query_person_by_id`）→ POST `{MGMT}/user/records/v2`；参数：**uid**、dept_id、company_id
- 查询员工变更工单（`mgmt.query_employee_ticket`）→ POST `{MGMT}/user/records/ticket/v1`；参数：**user_id**、**ticket_type**、company_id、page、page_size
- 查询人员培训信息（`mgmt.query_person_training`，month 为季度索引 1-4，时间范围自动补全）→ POST `{MGMT}/user/records/training/v1`；参数：**uid**、**year**、**month**、start_time、end_time、**is_pass**、**plan_status**
- 查询人员维护工单（`mgmt.query_person_maintenance_plan`）→ POST `{MGMT}/user/records/maintenanceplan/v1`；参数：**uid**、dept_id、company_id、page、page_size
- 查询人员机房巡检记录（`mgmt.query_person_inspection`）→ POST `{MGMT}/user/records/inspection/v1`；参数：**uid**、dept_id、company_id、page、page_size
- 查询公司列表（`mgmt.query_company_list`）→ GET `{MGMT}/authority/userrole/v1/current/company`；无参数
- 查询部门人员列表（`mgmt.query_user_list`）→ GET `{MGMT}/user/dept/v4/page`；参数：**dept_id**、**profession**（供配电/制冷/智能化/其他）、page、page_size
- 查询公司下的部门列表（`mgmt.query_dept_list`）→ GET `{MGMT}/authority/userrole/v3/current/dept?companyId=`；参数：company_id

### 巡检（inspection，走 INSPECT 根路径）

- 查询巡检列表（`mgmt.query_inspection_list`）→ POST `{INSPECT}/inspection/inspection/search/v1`；参数：**room_type**、**employee_name**、**start_time**、**end_time**、**type**、**company**、**dept_name**、page、page_size
- 查询巡检设备类型列表（`mgmt.query_inspection_device_type`）→ POST `{INSPECT}/inspection/device/type/search/v3`；参数：**name**、**device_type_id**、**company**、**floor**、page、page_size
- 查询巡检设备类型全量（v2 不分页）（`mgmt.query_inspection_device_type_v2`）→ POST `{INSPECT}/inspection/device/type/search/v2`；参数：**name**、**device_type_id**、**company**、**floor**
- 查询巡检项/巡检图片点位模板（`mgmt.query_inspection_item`）→ POST `{INSPECT}/inspection/picture/type/search/v3`；参数：**picture_name**、**device_type_id**、**company**、**floor**、page、page_size
- 查询巡检工单列表（`mgmt.query_inspection_ticket`）→ POST `{INSPECT}/inspection/ticket/get/list/v1`（文档登记，待实测）；参数：**ticket_number**、**name**、**status**、**type**、page、page_size
- 查询巡检工单明细（`mgmt.query_inspection_ticket_detail`）→ GET `{INSPECT}/inspection/ticket/get/detail/v1/{id}`（文档登记，待实测）；参数：**ticket_id**
- 查询巡检员列表（`mgmt.query_inspection_user_list`）→ GET `{MGMT}/user/inspection/v1`；参数：**dept_id**（必填，未指明公司/部门时须先问用户，不默认本人部门）、**name**（姓名模糊）、**phone**（手机号模糊）、page、page_size

### 维护（maintenance）

- 查询设备维护计划进度列表（`mgmt.query_equipment_maintenance_plan`）→ POST `{MGMT}/plan/list/plan/infoV3`；参数：**year**、**device_name**、**parent_group**、dept_id（对方平台也称「设备维护进度」，同一接口；接口必填 year/isNew/deptId，其中 isNew 固定 1，dept_id 缺省取当前登录用户所属部门）
- 查询设备维护计划状态列表（`mgmt.query_equipment_maintenance_status`）→ GET `{MGMT}/plan/status/{year}/{companyId}`；参数：**year**、**company_id**
- 查询设备维护工单列表（`mgmt.query_equipment_work_order`）→ GET `{MGMT}/maintain/v2`；参数：dept_id、**year**、**week**、**status**、**type**、**maintenance_num**、**maintenance_main_num**、page、page_size
- 查询设备维护操作流程列表（`mgmt.query_equipment_maintenance_flow`）→ GET `{MGMT}/maintain/flow/v3/{deptId}/{page}/{pageSize}`（deptId/page/pageSize 在 path）；参数：dept_id、**name**（流程名模糊匹配）、page、page_size
- 查询设备维护操作流程详情（`mgmt.query_equipment_maintenance_flow_detail`）→ GET `{MGMT}/maintain/flow/info/v1/{id}`（id 在 path）；参数：**flow_id**（取自列表结果的 `rows[].id`）

### 设备（device）

- 查询设备台账/设备信息（`mgmt.query_device_info`）→ POST `{MGMT}/plan/list/device/info`；参数：**dept_id**、**type_id**、page、page_size
- 查询红外设备类型列表（`mgmt.query_infrared_device_type`）→ GET `{MGMT}/infrared/device/type/list/v1/{deptId}`；参数：dept_id
- 查询红外设备列表（`mgmt.query_infrared_device_list`）→ POST `{MGMT}/infrared/device/list/v1`；参数：**infrared_device_type_id**、dept_id、page、page_size
- 查询红外检测设备列表（`mgmt.query_infrared_detect_device`）→ POST `{MGMT}/plan/list/device/info/v2`；参数：dept_id、page、page_size
- 查询部门设备分组（`mgmt.query_device_group`）→ GET `{MGMT}/deviceGroup/v1/dept/{deptId}`；参数：**dept_id**
- 查询设备系统类型树（`mgmt.query_device_system_tree`）→ GET `{MGMT}/equipment/get/system/tree/{deptId}`；参数：dept_id
- 查询系统类型下的子系统列表（`mgmt.query_device_logic_type`）→ POST `{MGMT}/equipment/get/logic/type`；参数：**pid**、dept_id、page、page_size
- 查询设备专业类别（`mgmt.query_device_major`）→ POST `{MGMT}/equipment/get/dept`；参数：dept_id
- 查询设备类型列表（`mgmt.query_device_type`）→ POST `{MGMT}/equipment/list`；参数：dept_id、**name**、**parent_id**、page、page_size

### 文档（document）

- 查询运维管理体系/思维导图（`mgmt.query_mindmap`）→ GET `{MGMT}/file/v1/getAll/{id}`；参数：**file_id**
- 查询 IDC 知识库案例文章（`mgmt.query_kb_article`）→ POST `{MGMT}/kbArticle/getIsSharedKbArticleList`；参数：**title**、**tag_id**、page、page_size
- 查询标准规范（`mgmt.query_kb_standard`）→ POST `{MGMT}/kbNationalStandard/getIsSharedStandardList`；参数：**title**、**tag_id**、page、page_size

### 工单（work_order，全走 TICKET 工单服务）

- 查询我的工单（`mgmt.query_pending_ticket`）→ POST `{TICKET}/pending/list/v1`（按 mgmt token 归属人；type 1=变更/4=问题/5=故障/6=事件/7=阈值）；参数：**name**、**is_done**、**type**、company_id、page、page_size
- 查询事件工单列表（`mgmt.query_event_ticket`）→ POST `{TICKET}/wiporder/ticketlist/event`；参数：company_id、**dc**、**type**、**severity_level**、**specialty**、**ticket_no**、**project_name**、**key_word**、**stage**、**only_time_out**、**start_time**、**end_time**、page、page_size
- 查询变更工单（`mgmt.query_change_ticket`）→ POST `{TICKET}/wiporder/ticketlist/modify`；参数：company_id、**type**、**ticket_type**、**ticket_no**、**project_name**、**key_word**、**modify_type**、**stage**、**dc**、**start_time**、**end_time**、**only_time_out**、page、page_size
- 查询变更单统计（`mgmt.query_change_statistics`）→ POST `{TICKET}/wiporder/ticketlist/statistics/modify`（**按部门分组的全量变更单、不分页**，一单一行；与 `query_change_ticket` 互斥二选一）；参数：**company_id**、**all_companies**、**start_time**、**end_time**
- 查询故障工单列表（`mgmt.query_breakdown_ticket`）→ POST `{TICKET}/wiporder/ticketlist/breakdown`；参数：company_id、**type**、**ticket_type**、**break_down_level**、**ticket_no**、**project_name**、**key_word**、**stage**、**dc**、**start_time**、**end_time**、**only_time_out**、page、page_size
- 查询问题工单列表（`mgmt.query_question_ticket`）→ POST `{TICKET}/wiporder/ticketlist/question`；参数：company_id、**type**、**severity_level**、**specialty**、**ticket_no**、**project_name**、**key_word**、**stage**、**dc**、**start_time**、**end_time**、page、page_size
- 查询阈值工单列表（`mgmt.query_threshold_ticket`）→ POST `{TICKET}/wiporder/ticketlist/threshold`（specialty 1=供配电/2=制冷/4=智能化/8=其他）；参数：company_id、**type**、**specialty**、**ticket_no**、**project_name**、**key_word**、**stage**、**dc**、**start_time**、**end_time**、page、page_size
- 查询阈值工单详情（`mgmt.query_threshold_ticket_detail`）→ GET `{TICKET}/wiporder/threshold/{id}`（id 取阈值列表返回的 ticketId）；参数：**ticket_id**
- 查询供应商服务工单列表（`mgmt.query_supplier_ticket`）→ POST `{TICKET}/wiporder/ticketlist/supplier`（serviceType 1=例行维护/2=故障检修/3=技术培训/0=其他）；参数：company_id、**type**、**service_type**、**ticket_no**、**project_name**、**key_word**、**stage**、**dc**、page、page_size
- 查询供应商服务工单详情（`mgmt.query_supplier_ticket_detail`）→ GET `{TICKET}/wiporder/supplier/{id}`（id 取供应商列表返回的 ticketId）；参数：**ticket_id**
- 查询故障工单详情（`mgmt.query_breakdown_ticket_detail`）→ GET `{TICKET}/wiporder/query/breakdown/{id}`；参数：**ticket_id**
- 查询问题工单详情（`mgmt.query_question_ticket_detail`）→ GET `{TICKET}/wiporder/question/{id}`；参数：**ticket_id**
- 查询事件工单详情（`mgmt.query_event_ticket_detail`）→ GET `{TICKET}/wiporder/event/{id}`；参数：**ticket_id**
- 查询变更工单详情（`mgmt.query_change_ticket_detail`）→ GET `{TICKET}/wiporder/query/modify/{id}`；参数：**ticket_id**

### 配置（config）

- 查询配置管理列表（`mgmt.query_config_template_list`）→ POST `{MGMT}/tck/cfg/template/list`；参数：**template_name**、**type**、page、page_size
- 查询任务列表（`mgmt.query_task_list`）→ POST `{MGMT}/tcm/task/list`；参数：**type**、dept_id、**template_name**、**status**、**executor_name**、**task_ticket_no**、page、page_size

### 评审（review）

- 查询评审预约列表（`mgmt.query_review_appointment`）→ POST `{MGMT}/review_appointments/list/v1`；参数：**type**、dept_id、company_id、**start_time**、**end_time**、**is_all**、page、page_size
- 查询评审预约详情（`mgmt.query_review_appointment_detail`）→ GET `{MGMT}/review_appointments/detail/v1/{id}`；参数：**appointment_id**

## 安防平台（security）

_暂无能力_

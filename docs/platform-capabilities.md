# 平台接口能力清单

> 数据源：`service/query/{dcim,mgmt,security}/capabilities.py` 的 `CAPABILITIES`。
> 每条能力即 Agent 可调用的一个查询/执行工具；「分类」为意图路由的统一词汇。

## 总览

| 平台 | 能力数 |
|---|---|
| 数据中心（dcim） | 6 |
| 综合管理平台（mgmt） | 43 |
| 安防平台（security） | 0 |
| **合计** | **49** |

## 数据中心（dcim）

### 告警（alarm）

- 查询活跃告警（`dcim.query_alarm`）
- 查询历史告警（`dcim.query_alarm_history`）

### 资产（asset）

- 查询资产台账（`dcim.query_asset`）
- 统计资产数量（`dcim.query_asset_count`）

### 容量（capacity）

- 查询机房容量（`dcim.query_capacity`）
- 查询能耗电力（`dcim.query_power_usage`）

## 综合管理平台（mgmt）

### 组织人事（org）

- 按姓名查询人员信息（`mgmt.query_person_by_name`）
- 按部门名称模糊查询部门（`mgmt.query_dept_by_name`）
- 按 uid 查询人员详情（`mgmt.query_person_by_id`）
- 查询员工变更工单（`mgmt.query_employee_ticket`）
- 查询人员培训信息（`mgmt.query_person_training`）
- 查询人员维护工单（`mgmt.query_person_maintenance_plan`）
- 查询人员机房巡检记录（`mgmt.query_person_inspection`）
- 查询公司列表（`mgmt.query_company_list`）
- 查询部门人员列表（`mgmt.query_user_list`）
- 查询公司下的部门列表（`mgmt.query_dept_list`）

### 巡检（inspection）

- 查询巡检列表（`mgmt.query_inspection_list`）
- 查询巡检设备类型列表（`mgmt.query_inspection_device_type`）
- 查询巡检设备类型全量（v2 不分页）（`mgmt.query_inspection_device_type_v2`）
- 查询巡检项/巡检图片点位模板（`mgmt.query_inspection_item`）
- 查询巡检工单列表（`mgmt.query_inspection_ticket`）
- 查询巡检工单明细（`mgmt.query_inspection_ticket_detail`）

### 维护（maintenance）

- 查询设备维护计划列表（`mgmt.query_equipment_maintenance_plan`）
- 查询设备维护计划状态列表（`mgmt.query_equipment_maintenance_status`）
- 查询设备维护工单列表（`mgmt.query_equipment_work_order`）

### 设备（device）

- 查询设备台账/设备信息（`mgmt.query_device_info`）
- 查询红外设备类型列表（`mgmt.query_infrared_device_type`）
- 查询红外设备列表（`mgmt.query_infrared_device_list`）
- 查询红外检测设备列表（`mgmt.query_infrared_detect_device`）
- 查询部门设备分组（`mgmt.query_device_group`）
- 查询设备系统类型树（`mgmt.query_device_system_tree`）
- 查询系统类型下的子系统列表（`mgmt.query_device_logic_type`）
- 查询设备专业类别（`mgmt.query_device_major`）
- 查询设备类型列表（`mgmt.query_device_type`）

### 文档（document）

- 查询运维管理体系/思维导图（`mgmt.query_mindmap`）
- 查询 IDC 知识库案例文章（`mgmt.query_kb_article`）
- 查询标准规范（`mgmt.query_kb_standard`）

### 工单（work_order）

- 查询我的工单（`mgmt.query_pending_ticket`）
- 查询变更工单（`mgmt.query_change_ticket`）
- 查询故障工单列表（`mgmt.query_breakdown_ticket`）
- 查询问题工单列表（`mgmt.query_question_ticket`）
- 查询故障工单详情（`mgmt.query_breakdown_ticket_detail`）
- 查询问题工单详情（`mgmt.query_question_ticket_detail`）
- 查询事件工单详情（`mgmt.query_event_ticket_detail`）
- 查询变更工单详情（`mgmt.query_change_ticket_detail`）

### 配置（config）

- 查询配置管理列表（`mgmt.query_config_template_list`）
- 查询任务列表（`mgmt.query_task_list`）

### 评审（review）

- 查询评审预约列表（`mgmt.query_review_appointment`）
- 查询评审预约详情（`mgmt.query_review_appointment_detail`）

## 安防平台（security）

_暂无能力_


"""PEN 工单列表「返回字段」码表（单一来源）。

来源：docs/dcim/工单列表接口字段映射确认清单.md（平台方确认）。
**返回字段专用**——与请求筛选参数（capabilities.py 的 params）不是一套：
- 同名不同义：六类列表的请求 type=查询范围（1 全部/2 我发起的…），「我的工单」的返回
  type=工单大类（1 变更/4 问题/…）；
- status（状态）与 stage（进度）是两套码，不可互推。

本文件是这些码表的**唯一来源**，两处消费：
- `render_return_doc()` → 拼进能力描述给模型看（**只渲染模型无从推断的部分**）；
- `api/v1/agent.py` 的 `_RECORD_DECODE` → 前端可交互表格解码。

刻意不给模型的部分：请求 params 里已写明同码（specialty / 变更等级 / serviceType /
modify_type / 我的工单 type / 故障 stage），或模型自己就能翻（level、breakDownLevel 的
0-4 → N级）——它们仍留在本文件供前端表格用，但不进上下文，避免描述膨胀。
"""

# specialty · 专业（通用，六类工单一致）。请求 params 里已写同码，故不重复渲染给模型。
SPECIALTY = {1: '供配电', 2: '制冷', 4: '智能化', 8: '其他'}

# status · 工单状态。变更 / 问题事件阈值 / 供应商 三套文案不同。
STATUS = {
  'change': {
    0: '无', 1: '通过', 2: '驳回', 3: '审批中', 4: '实施中',
    5: '撤回', 6: '退回', 7: '验收中', 8: '审批通过', 9: '延期中',
  },
  'approval': {
    0: '无', 1: '通过', 2: '驳回', 3: '审批中', 4: '实施中',
    5: '撤回', 6: '退回', 7: '验收中', 8: '审批通过',
  },
  'supplier': {
    0: '草稿', 1: '通过', 2: '驳回', 3: '审批中', 4: '实施中',
    5: '撤回', 6: '退回', 7: '验收中', 8: '审批通过',
  },
}

# stage · 当前进度。各工单类型文案不同（故障的已写在 capabilities.py 的 params 里，不重复渲染）。
# -1：事件/问题、故障、供应商按「驳回期」展示；变更/阈值按平台确认清单原词「驳回」。
# 这里只影响**返回字段解析**（模型描述 + 交互表格解码），请求筛选参数不涉及。
STAGE = {
  'change': {-1: '驳回', 1: '审批期', 2: '实施期', 3: '验收期', 5: '关闭期', 7: '等待期'},
  'breakdown': {-1: '驳回期', 2: '故障处理', 3: '故障验收', 5: '故障关闭', 7: '故障派发'},
  'question_event': {-1: '驳回期', 2: '处理期', 3: '验收期', 5: '关闭期', 7: '派发期'},
  'threshold': {-1: '驳回', 1: '审批期', 2: '处理期', 3: '验收期', 5: '关闭期', 7: '派发期'},
  'supplier': {-1: '驳回期', 0: '无', 3: '验收期', 5: '关闭期', 9: '草稿'},
}

# breakdownEquipmentType · 故障设备类型。**按专业分表**：同一个数字在不同专业下含义完全不同，
# 必须结合**同行的 specialty** 查表（来源：平台方提供的「故障设备类型字段对应映射表」，
# 与 OA `src/constants/fault.js`、移动端同一份，2026-10-08）。
# 特殊项：specialty=4（智能化）下 `11` 在源表里是**空缺**，平台口径按「DDC控制箱」展示 —— 照补；
# 注意供配电表里 `20` 也叫 DDC控制箱，两者不冲突（不同专业各自一套）。
BREAKDOWN_EQUIPMENT_TYPE: dict[int, dict[int, str]] = {
  # 1 = 供配电
  1: {
    0: '高压柜', 1: '变压器', 2: '低压柜', 3: 'UPS蓄电池', 4: 'UpS', 5: '列头柜', 6: 'UPS电池开关柜', 7: '供油系统',
    8: '柴发机组', 9: 'EPS', 10: 'EPS蓄电池', 11: 'ATS箱', 12: '直流屏', 13: '空调配电柜', 14: '照明',
    15: '直流开关电源', 16: '冷机启动柜', 17: '配电线路', 18: '防雷系统', 19: '其他', 20: 'DDC控制箱',
  },
  # 2 = 制冷
  2: {
    0: '冷水机组', 1: '控制柜', 2: '冷却泵', 3: '—次冷冻泵', 4: '二次冷冻泵', 5: '板式换热器', 6: '冷却水补水系统',
    7: '综合水流器', 8: '冷却塔', 9: '冷冻水补水系统', 10: '蓄冷罐', 11: '直膨精密空调', 12: '水冷精密空调', 13: '恒湿机',
    14: '无负压供水系统', 15: '压力表', 16: '温度表', 17: '温度计', 18: '流量计', 19: 'VRV', 20: '加湿补水泵',
    21: '加药装置', 22: '冷却塔控制柜', 23: '水系统管道阀门', 24: '水处理装置', 25: '新风系统', 26: 'CDU', 27: '风墙',
    28: '其他',
  },
  # 4 = 智能化
  4: {
    0: '新风机控制系统', 1: '排烟控制系统', 2: '门禁监控系统', 3: '防火卷帘系统', 4: '门禁控制器', 5: '动力环境监控系统',
    6: '嵌入式服务器', 7: '动环服务器', 8: '交换机、防火墙', 9: 'AC管理器', 10: '视频监控系统', 11: 'DDC控制箱',
    12: '视频电源柜', 13: '硬盘录像机', 14: '视频光端机', 15: '消防控制柜', 16: '启动钢瓶', 17: '手提式灭火器',
    18: '气体灭火盘', 19: '空外消防箱', 20: '消防泵自动检测系统', 21: '消火栓', 22: '高位水箱控制设备',
    23: '消防应急照明和疏数指示', 24: '入侵报警系统', 25: '综合安防管理平台', 26: 'BA服务器', 27: '动环采集箱',
    28: '视频监控系统服务器', 29: '磁盘阵列', 30: '自动喷水灭火系统', 31: '预作用报警阀组', 32: '湿式报警阀组',
    33: '其他',
  },
}


def equipment_type_label(specialty, code) -> str | None:
  """按专业查故障设备类型中文名。

  专业或码任一查不到时返回 None —— 调用方**保留原值**（宁可显示数字，也不丢字段/猜错）。
  """
  try:
    table = BREAKDOWN_EQUIPMENT_TYPE.get(int(specialty))
  except (TypeError, ValueError):
    return None
  if not table:
    return None
  try:
    return table.get(int(code))
  except (TypeError, ValueError):
    return None


# 「我的工单」level：变更/阈值/供应商 → 风险等级（问题/故障/事件 是「数字+级」，无需码表）。
RISK_LEVEL = {1: '低风险', 2: '中风险', 3: '高风险'}

# 工单种类 → (status 表键, stage 表键)
KIND_TABLES = {
  'change': ('change', 'change'),
  'question': ('approval', 'question_event'),
  'event': ('approval', 'question_event'),
  'threshold': ('approval', 'threshold'),
  'supplier': ('supplier', 'supplier'),
}


def _fmt(table: dict) -> str:
  """码表 → 紧凑一行：'-1驳回/1审批期/…'（按键升序）。"""
  return '/'.join(f'{k}{table[k]}' for k in sorted(table))


def as_str_map(table: dict) -> dict:
  """码表 → 键为字符串的副本（CSV 读回的码是字符串，表格解码要按字符串查）。"""
  return {str(k): v for k, v in table.items()}


def render_return_doc(kind: str) -> str:
  """渲染某类工单的「返回字段」说明，供能力描述拼接；未知种类返回空串。

  前缀「非请求参数」是为和同一段描述里的请求筛选枚举撇清——两者同名但可能不同义。
  """
  tables = KIND_TABLES.get(kind)
  if not tables:
    return ''
  status_key, stage_key = tables
  return (
    f'\n返回字段（非请求参数）：status {_fmt(STATUS[status_key])}；'
    f'stage {_fmt(STAGE[stage_key])}。'
  )


# 「我的工单」level 说明：一字段多义，按行内 type 选表（严重级别就是「数字+级」，不铺开写）。
PENDING_LEVEL_DOC = (
  '\n返回字段（非请求参数）：level 为工单等级，含义随行内 type 变——变更(1)/阈值(7)/供应商(8) '
  f'为风险等级（{_fmt(RISK_LEVEL)}）；问题(4)/故障(5)/事件(6) 为严重级别（数字 0-4，展示为 N级）。'
)

# ============ 列名映射：接口原始字段名 → 表格列名（交互表格表头兜底） ============
# 消费方：api/v1/agent.py 的 get_agent_artifact_records —— 前端表格表头只认中文，
# 但 agent 整理 view.csv 时可能漏改个别列（2026-09-18 实测：两次「查变更单」，一次表头
# 是 ticketNo/projectName，一次正常；模型代码把需要映射的列都起了中文名，纯字符串列
# 在 `df_view[[...]]` 选列时被原样带走）。这层是**确定性兜底**：只改「仍逐字等于原始
# 字段名」的列，模型已改好的中文列一律不碰。
#
# 口径 = **收敛版**：不按工单类型加前缀（文档里的「变更机房/故障机房/…」统一成「机房」）。
# 来源：docs/dcim/工单列表接口字段映射确认清单.md 的「字段注释」+ 该文档按约定
# **不收录的直出字段**（编号/名称/人名/时间——表头需要它们，故在此补齐）。
# 键统一小写，匹配时忽略大小写。
FIELD_LABELS: dict[str, str] = {
  # 直出字段（两份清单按约定不收录，本表补齐）
  'ticketno': '工单编号',
  'projectname': '项目名称',
  'correlationticketno': '关联工单',
  'initiator': '发起人',
  'approveusername': '当前审批人',
  'createtime': '创建时间',
  'occurrencetime': '发生时间',
  'scheduledstarttime': '计划开始时间',
  'scheduledendtime': '计划结束时间',
  'executestarttime': '实施开始时间',
  'executeendtime': '实施结束时间',
  'starttime': '开始时间',
  'endtime': '结束时间',
  'duration': '时长',
  'faulthandlingduration': '处理时长',
  # 文档字段注释（收敛掉类型前缀）
  'dc': '机房',
  'specialty': '专业',
  'status': '状态',
  'stage': '阶段',
  'type': '类型',
  'breakdownlevel': '级别',
  'modifytypev2': '变更类型',
  'modifytype': '变更类型',
  'servicetype': '服务类型',
  'breakdownequipmenttype': '设备类型',
  # 刻意不收录：
  # - approvetime：同一列名在变更单里既是「审批时长」又是「验收时长」（取决于接口路径
  #   checkBeginApprove / checkEndApprove），无法唯一命名，宁可不改
  # - 内部 id / 标志位（ticketId、approveProcessId、isRead…）：本就不该出现在整理视图里
  # - **level / ticketType：同名不同义，只按能力登记在下表**（见 FIELD_LABELS_BY_CAPABILITY）
}

# 同名不同义的字段：只在**文档明确登记过**的能力上改名（实测撞车：事件单的 ticketType
# 取值是 6（工单大类），不是变更单的 1/2/3（变更等级）；人员列表的 level 取值是「中级/高级」
# （职级），不是工单等级）。宁可不改，也不要给出一个错误的表头。
FIELD_LABELS_BY_CAPABILITY: dict[str, dict[str, str]] = {
  'mgmt.query_change_ticket': {'tickettype': '变更等级'},
  **{
    cap: {'level': '等级'}
    for cap in (
      'mgmt.query_question_ticket', 'mgmt.query_event_ticket',
      'mgmt.query_threshold_ticket', 'mgmt.query_pending_ticket',
    )
  },
}


def build_column_labels(
  columns: list[str], capability_id: str | None = None,
) -> dict[str, str]:
  """返回「原列名 → 中文列名」重命名表，**只含需要改的列**。

  已是中文的列、未登记的字段（内部 id/标志位等）都不会出现在返回值里 —— 调用方据此
  改表头与记录键，未命中的列原样保留。按能力登记的同名不同义字段优先于全局表。
  """
  per_cap = FIELD_LABELS_BY_CAPABILITY.get(capability_id or '', {})
  out: dict[str, str] = {}
  for col in columns:
    key = str(col).strip().lower()
    label = per_cap.get(key) or FIELD_LABELS.get(key)
    if label and label != col:
      out[col] = label
  return out

"""Mock 运维工具 — 设备台账/维保/值班/工单/通知（外部系统就绪后替换为真实调用）

数据互相关联：告警 mock（mock_tools.py）里的设备 UPS-1A-03 / CRAC-B-07 / PDU-C-12
在设备台账和维保记录中都能查到，值班表的人员可被工单指派和通知引用，
支撑 agent 多工具链式编排演示。

⚠️ 下线说明（P0-3）：query_device_info / query_maintenance_history /
query_duty_roster / create_work_order / query_work_orders /
query_attendance_records 已从 service/agent/agent_service.py 的常驻工具列表移除，
不再注册进 Agent。原因：它们与 mgmt/dcim 平台真实能力功能重叠，而常驻工具对 LLM
始终可见、平台工具走 deferred（需 tool_search 才可见），导致 LLM 永远优先调用
这些 mock、真实平台永不被调用。函数定义本身保留在此文件供测试/参考，不要删除，
也不要重新注册，除非对应平台能力尚不存在且明确要临时兜底。send_notification
未与任何平台能力重叠，仍在常驻工具列表中。
"""

import json
import logging
from datetime import datetime, timedelta

from langchain.tools import tool

logger = logging.getLogger(__name__)

# ── 共享 Mock 数据 ──

_DEVICES = {
  'UPS-1A-03': {
    'device_id': 'UPS-1A-03',
    'name': 'A7栋1层UPS 3号机',
    'type': 'UPS 不间断电源',
    'model': '华为 UPS5000-E-400K',
    'room': 'A701',
    'location': 'A7栋1层配电室 3列2柜',
    'commissioned': '2023-05-18',
    'owner': '王志强（配电组）',
    'last_maintenance': '2025-12-20',
    'maintenance_cycle_months': 6,
    'status': '运行中（有活跃告警）',
  },
  'CRAC-B-07': {
    'device_id': 'CRAC-B-07',
    'name': 'A7栋2层精密空调 7号机',
    'type': '精密空调',
    'model': '维谛 PEX4 P2060',
    'room': 'A702',
    'location': 'A7栋2层机房 空调间B区',
    'commissioned': '2023-08-02',
    'owner': '李海峰（暖通组）',
    'last_maintenance': '2026-05-10',
    'maintenance_cycle_months': 3,
    'status': '运行中（有活跃告警）',
  },
  'PDU-C-12': {
    'device_id': 'PDU-C-12',
    'name': 'B1栋1层PDU 12号',
    'type': 'PDU 电源分配单元',
    'model': '施耐德 iPDU-32A',
    'room': 'B103',
    'location': 'B1栋1层机房 C列12柜',
    'commissioned': '2024-01-15',
    'owner': '王志强（配电组）',
    'last_maintenance': '2026-06-28',
    'maintenance_cycle_months': 6,
    'status': '运行中',
  },
}

_MAINTENANCE_RECORDS = {
  'UPS-1A-03': [
    {
      'date': '2025-12-20',
      'type': '半年度保养',
      'engineer': '王志强',
      'content': '更换直流母线电容 2 只，测试电池组内阻，整体正常',
      'result': '正常',
    },
    {
      'date': '2026-04-11',
      'type': '故障巡检',
      'engineer': '赵磊',
      'content': '巡检发现输入 A 相电压短时波动（192V~205V），持续约 40 分钟后自行恢复，未处理',
      'result': '异常（已恢复，建议关注市电侧）',
    },
    {
      'date': '2026-06-30',
      'type': '例行巡检',
      'engineer': '赵磊',
      'content': '外观检查、风扇滤网清洁，运行参数正常',
      'result': '正常',
    },
  ],
  'CRAC-B-07': [
    {
      'date': '2026-05-10',
      'type': '季度保养',
      'engineer': '李海峰',
      'content': '清洗冷凝器、检查制冷剂压力，压力略低已补充',
      'result': '正常',
    },
    {
      'date': '2026-07-01',
      'type': '例行巡检',
      'engineer': '李海峰',
      'content': '送风温度略高于设定值，已调整风阀开度',
      'result': '关注',
    },
  ],
  'PDU-C-12': [
    {
      'date': '2026-06-28',
      'type': '半年度保养',
      'engineer': '王志强',
      'content': '端子紧固、红外测温，各回路温升正常',
      'result': '正常',
    },
  ],
}

_DUTY_ROSTER = {
  'day': {'name': '张伟', 'role': '值班长（白班 08:00-20:00）', 'phone': '138****2201', 'group': '运行值班组'},
  'night': {'name': '刘洋', 'role': '值班工程师（夜班 20:00-08:00）', 'phone': '139****6675', 'group': '运行值班组'},
  'backup': {'name': '王志强', 'role': '配电专业后备', 'phone': '137****0912', 'group': '配电组'},
}

# 进程内工单存储：预置 2 条历史工单，create_work_order 会往里追加
_WORK_ORDERS: list[dict] = [
  {
    'order_id': 'WO-20260714-001',
    'title': 'A702 精密空调送风温度偏高处理',
    'device_id': 'CRAC-B-07',
    'priority': 'medium',
    'status': 'processing',
    'assignee': '李海峰',
    'created_at': '2026-07-14 15:30:00',
  },
  {
    'order_id': 'WO-20260710-003',
    'title': 'B103 机房月度红外测温巡检',
    'device_id': '',
    'priority': 'low',
    'status': 'completed',
    'assignee': '赵磊',
    'created_at': '2026-07-10 09:00:00',
  },
]

# 员工打卡/门禁记录（考勤系统接口就绪后替换为真实调用）
_ATTENDANCE_RECORDS = {
  '小明': [
    {'date': '2026-07-17', 'check_in': '08:47', 'check_out': '', 'door': 'A7栋大堂闸机', 'status': '在岗'},
    {'date': '2026-07-16', 'check_in': '08:52', 'check_out': '18:34', 'door': 'A7栋大堂闸机', 'status': '正常'},
    {'date': '2026-07-15', 'check_in': '09:12', 'check_out': '18:05', 'door': 'A7栋大堂闸机', 'status': '迟到'},
    {'date': '2026-07-14', 'check_in': '08:40', 'check_out': '19:20', 'door': 'B1栋侧门', 'status': '正常'},
  ],
  '张伟': [
    {'date': '2026-07-17', 'check_in': '07:55', 'check_out': '', 'door': 'A7栋大堂闸机', 'status': '在岗'},
    {'date': '2026-07-16', 'check_in': '07:58', 'check_out': '20:10', 'door': 'A7栋大堂闸机', 'status': '正常'},
  ],
}


def _dump(data) -> str:
  return json.dumps(data, ensure_ascii=False, indent=2)


# ── 工具定义 ──


@tool
def query_device_info(device_id: str) -> str:
  """查询设备台账信息，包括设备型号、位置、投产日期、责任人、上次维保时间。

  通常在拿到告警中的设备编号后调用，用于了解告警设备的基本情况和维保状态。

  Args:
    device_id: 设备编号，如 UPS-1A-03、CRAC-B-07、PDU-C-12
  """
  device = _DEVICES.get(device_id.strip())
  if not device:
    return _dump({'found': False, 'message': f'未找到设备 {device_id}，请确认设备编号'})

  # 计算维保是否超期
  last = datetime.strptime(device['last_maintenance'], '%Y-%m-%d')
  due = last + timedelta(days=device['maintenance_cycle_months'] * 30)
  overdue = datetime.now() > due
  return _dump({
    'found': True,
    **device,
    'next_maintenance_due': due.strftime('%Y-%m-%d'),
    'maintenance_overdue': overdue,
    'maintenance_note': f'维保已超期 {(datetime.now() - due).days} 天，建议尽快安排' if overdue else '维保在有效期内',
  })


@tool
def query_maintenance_history(device_id: str) -> str:
  """查询设备的历史维保和巡检记录，可用于分析设备故障是否有历史征兆。

  Args:
    device_id: 设备编号，如 UPS-1A-03
  """
  records = _MAINTENANCE_RECORDS.get(device_id.strip())
  if not records:
    return _dump({'count': 0, 'records': [], 'message': f'设备 {device_id} 无维保记录'})
  return _dump({'device_id': device_id, 'count': len(records), 'records': records})


@tool
def query_duty_roster(date: str = '') -> str:
  """查询值班表，获取值班人员的姓名、岗位和联系电话。

  需要指派工单或发送通知时，先调用本工具确认值班人。

  Args:
    date: 日期，格式 YYYY-MM-DD，留空表示今天
  """
  day = date.strip() or datetime.now().strftime('%Y-%m-%d')
  return _dump({
    'date': day,
    'shifts': [_DUTY_ROSTER['day'], _DUTY_ROSTER['night']],
    'backup': _DUTY_ROSTER['backup'],
  })


@tool
def create_work_order(title: str, device_id: str = '', priority: str = 'medium', assignee: str = '', description: str = '') -> str:
  """创建运维工单。执行前应先向用户说明将要创建的工单内容。

  Args:
    title: 工单标题，简要描述要处理的问题
    device_id: 关联设备编号（可选），如 UPS-1A-03
    priority: 优先级，可选 critical/high/medium/low
    assignee: 指派处理人姓名（建议先查值班表确认）
    description: 问题详细描述和处理要求（可选）
  """
  today = datetime.now().strftime('%Y%m%d')
  seq = sum(1 for o in _WORK_ORDERS if o['order_id'].startswith(f'WO-{today}')) + 1
  order = {
    'order_id': f'WO-{today}-{seq:03d}',
    'title': title,
    'device_id': device_id,
    'priority': priority,
    'status': 'pending',
    'assignee': assignee or _DUTY_ROSTER['day']['name'],
    'description': description,
    'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
  }
  _WORK_ORDERS.append(order)
  logger.info('mock 工单已创建: %s %s', order['order_id'], title)
  return _dump({'created': True, **order})


@tool
def query_work_orders(status: str = '') -> str:
  """查询运维工单列表。

  Args:
    status: 工单状态过滤，可选 pending/processing/completed，留空则查询全部
  """
  orders = _WORK_ORDERS
  if status.strip():
    orders = [o for o in orders if o['status'] == status.strip()]
  return _dump({'count': len(orders), 'orders': orders})


@tool
def send_notification(recipient: str, content: str) -> str:
  """向指定人员发送通知（短信/IM）。执行前应先向用户说明通知对象和内容。

  Args:
    recipient: 接收人姓名（建议先查值班表确认联系方式）
    content: 通知内容
  """
  logger.info('mock 通知已发送: to=%s content=%s', recipient, content[:80])
  return _dump({
    'sent': True,
    'channel': 'sms',
    'recipient': recipient,
    'content': content,
    'sent_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
  })


@tool
def query_attendance_records(name: str, date: str = '') -> str:
  """查询员工的打卡/门禁记录，包括上下班打卡时间、通行闸机和考勤状态。

  可用于确认员工是否到岗、查询近几天的考勤情况。

  Args:
    name: 员工姓名，如 小明
    date: 日期，格式 YYYY-MM-DD，留空则返回最近几天的全部记录
  """
  records = _ATTENDANCE_RECORDS.get(name.strip())
  if not records:
    return _dump({'found': False, 'message': f'未找到 {name} 的打卡记录，请确认姓名'})

  if date.strip():
    records = [r for r in records if r['date'] == date.strip()]
    if not records:
      return _dump({'found': True, 'name': name, 'count': 0, 'message': f'{name} 在 {date} 无打卡记录'})

  return _dump({'found': True, 'name': name, 'count': len(records), 'records': records})


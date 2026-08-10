"""Mock 工具 — DCIM/门禁等外部 API 就绪后替换为真实调用"""

import json
from datetime import datetime
from langchain.tools import tool

# DCIM API 基地址（Phase 1 使用 mock，将来替换为真实 API）
DCIM_BASE_URL = 'http://dcim.internal/api'


@tool
def query_dcim_alarm(room: str = '', severity: str = '') -> str:
  """查询 DCIM 运维平台的活跃告警。

  Args:
    room: 机房或楼栋编号，如 A701、B103，也可只填栋号如 A7，留空则查询所有机房
    severity: 告警级别，可选 critical/warning/info，留空则查询所有级别
  """
  # Phase 1: Mock 数据，外部 API 就绪后替换为 httpx.get()
  mock_alarms = [
    {
      'id': 'ALM-20260715-001',
      'room': 'A701',
      'device': 'UPS-1A-03',
      'type': '输入电压异常',
      'severity': 'critical',
      'time': '2026-07-15T10:23:00',
      'status': 'active',
      'detail': 'UPS 输入 A 相电压 185V，低于阈值 198V',
    },
    {
      'id': 'ALM-20260715-002',
      'room': 'A702',
      'device': 'CRAC-B-07',
      'type': '送风温度偏高',
      'severity': 'warning',
      'time': '2026-07-15T09:45:00',
      'status': 'active',
      'detail': '送风温度 26.5°C，超过设定值 24°C',
    },
    {
      'id': 'ALM-20260715-003',
      'room': 'B103',
      'device': 'PDU-C-12',
      'type': '负载率告警',
      'severity': 'info',
      'time': '2026-07-15T08:00:00',
      'status': 'active',
      'detail': 'PDU 负载率 78%，接近阈值 80%',
    },
  ]

  alarms = mock_alarms
  if severity:
    alarms = [a for a in alarms if a['severity'] == severity]
  if room:
    # 前缀匹配：填 A7 可命中 A701/A702
    key = room.strip().upper().replace('栋', '')
    alarms = [a for a in alarms if a['room'].startswith(key)]

  if not alarms:
    return json.dumps({'count': 0, 'alarms': [], 'message': '无匹配告警'}, ensure_ascii=False)

  return json.dumps({
    'count': len(alarms),
    'alarms': alarms,
    'query': {'room': room or '全部', 'severity': severity or '全部'},
  }, ensure_ascii=False, indent=2)


@tool
def get_current_time() -> str:
  """获取当前系统时间。"""
  now = datetime.now()
  return json.dumps({
    'datetime': now.strftime('%Y-%m-%d %H:%M:%S'),
    'weekday': ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()],
    'timestamp': int(now.timestamp()),
  }, ensure_ascii=False)

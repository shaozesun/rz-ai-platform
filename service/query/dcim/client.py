"""DCIM 统一调用客户端 — 唯一与外部 DCIM 交互的一层（可插拔）。

设计要点：
- 对上暴露稳定抽象 `call(capability_id, params, on_behalf_of=None)`；registry/retriever/
  门面工具都不感知底层是 HTTP 还是 mock。
- Phase 1 走 mock 实现；DCIM 真实接口就绪后，仅在此文件把 mock 分支换成 httpx 调用
  （参考 service/video/pipeline.py 的 httpx.AsyncClient 模式），上层无需改动。
- `on_behalf_of` 为身份透传占位（阶段二用），mock 阶段仅记录不生效。
- 未来若对方提供 MCP Server，替换本文件为 MCP client 即可。
"""

import logging

from service.query.dcim.capabilities import CAPABILITIES

logger = logging.getLogger(__name__)

# 本平台能力快查表：id -> Capability（仅供 call 校验与日志用）
_LOCAL: dict = {c.id: c for c in CAPABILITIES}


class DcimError(Exception):
  """DCIM 调用异常，交由门面工具/中间件转为友好错误。"""


async def call(
  capability_id: str,
  params: dict | None = None,
  *,
  on_behalf_of: str | None = None,
) -> dict:
  """执行一次 DCIM 能力调用。

  Args:
    capability_id: registry 中的能力 id，如 'dcim.query_alarm'。
    params: 调用参数，键对应 Capability.params。
    on_behalf_of: 代表哪个用户发起（工号/手机号），阶段二透传给 DCIM 做鉴权。

  Returns:
    结构化 dict 结果。

  Raises:
    DcimError: capability_id 未注册或调用失败。
  """
  params = params or {}
  cap = _LOCAL.get(capability_id)
  if cap is None:
    raise DcimError(f'未知能力 {capability_id}')

  logger.info(
    'dcim.call id=%s domain=%s on_behalf_of=%s params=%s',
    capability_id, cap.domain, on_behalf_of or '-', params,
  )

  # Phase 1: mock 分发。真实接口就绪后替换为 httpx 调用。
  handler = _MOCK_HANDLERS.get(capability_id)
  if handler is None:
    raise DcimError(f'能力 {capability_id} 暂无 mock 实现')
  return handler(params)


# ══════════════════ 以下为 Phase 1 mock 数据与处理器 ══════════════════
# 告警数据搬自 tools/mock_tools.py 的 query_dcim_alarm，保持一致。

_MOCK_ALARMS = [
  {
    'id': 'ALM-20260715-001', 'room': 'A701', 'device': 'UPS-1A-03',
    'type': '输入电压异常', 'severity': 'critical', 'time': '2026-07-15T10:23:00',
    'status': 'active', 'detail': 'UPS 输入 A 相电压 185V，低于阈值 198V',
  },
  {
    'id': 'ALM-20260715-002', 'room': 'A702', 'device': 'CRAC-B-07',
    'type': '送风温度偏高', 'severity': 'warning', 'time': '2026-07-15T09:45:00',
    'status': 'active', 'detail': '送风温度 26.5°C，超过设定值 24°C',
  },
  {
    'id': 'ALM-20260715-003', 'room': 'B103', 'device': 'PDU-C-12',
    'type': '负载率告警', 'severity': 'info', 'time': '2026-07-15T08:00:00',
    'status': 'active', 'detail': 'PDU 负载率 78%，接近阈值 80%',
  },
]


def _match_room(room_field: str, query_room: str) -> bool:
  """前缀匹配：填 A7 可命中 A701/A702。"""
  if not query_room:
    return True
  key = query_room.strip().upper().replace('栋', '')
  return room_field.upper().startswith(key)


def _h_query_alarm(params: dict) -> dict:
  room = params.get('room', '')
  severity = params.get('severity', '')
  alarms = [a for a in _MOCK_ALARMS if _match_room(a['room'], room)]
  if severity:
    alarms = [a for a in alarms if a['severity'] == severity]
  return {
    'count': len(alarms),
    'alarms': alarms,
    'query': {'room': room or '全部', 'severity': severity or '全部'},
  }


def _h_query_alarm_history(params: dict) -> dict:
  room = params.get('room', '')
  # mock：历史告警复用活跃告警集，标注为已恢复
  history = [
    {**a, 'status': 'resolved', 'resolved_time': '2026-07-15T12:00:00'}
    for a in _MOCK_ALARMS if _match_room(a['room'], room)
  ]
  return {
    'count': len(history),
    'alarms': history,
    'query': {
      'room': room or '全部',
      'start': params.get('start', ''),
      'end': params.get('end', ''),
    },
  }

# ── 资产域 ──

_MOCK_ASSETS = [
  {
    'device_id': 'UPS-1A-03', 'name': 'A7栋1层UPS 3号机', 'type': 'UPS',
    'model': '华为 UPS5000-E-400K', 'room': 'A701', 'status': '运行中',
    'commissioned': '2023-05-18',
  },
  {
    'device_id': 'CRAC-B-07', 'name': 'A7栋2层精密空调 7号机', 'type': '精密空调',
    'model': '维谛 PEX4 P2060', 'room': 'A702', 'status': '运行中',
    'commissioned': '2023-08-02',
  },
  {
    'device_id': 'PDU-C-12', 'name': 'B1栋1层PDU 12号', 'type': 'PDU',
    'model': '施耐德 iPDU-32A', 'room': 'B103', 'status': '运行中',
    'commissioned': '2024-01-15',
  },
]


def _filter_assets(params: dict) -> list[dict]:
  device_id = params.get('device_id', '').strip()
  room = params.get('room', '')
  dtype = params.get('type', '').strip()
  assets = _MOCK_ASSETS
  if device_id:
    assets = [a for a in assets if a['device_id'] == device_id]
  if room:
    assets = [a for a in assets if _match_room(a['room'], room)]
  if dtype:
    assets = [a for a in assets if dtype in a['type']]
  return assets


def _h_query_asset(params: dict) -> dict:
  assets = _filter_assets(params)
  return {'count': len(assets), 'assets': assets}


def _h_query_asset_count(params: dict) -> dict:
  assets = _filter_assets(params)
  by_type: dict[str, int] = {}
  for a in assets:
    by_type[a['type']] = by_type.get(a['type'], 0) + 1
  return {
    'total': len(assets),
    'by_type': by_type,
    'query': {'room': params.get('room', '') or '全部', 'type': params.get('type', '') or '全部'},
  }

# ── 容量/能耗域 ──

_MOCK_CAPACITY = {
  'A701': {
    'room': 'A701', 'cabinet_total': 40, 'cabinet_used': 34, 'cabinet_free': 6,
    'power_capacity_kw': 200, 'power_used_kw': 156, 'load_rate': 0.78, 'pue': 1.42,
  },
  'A702': {
    'room': 'A702', 'cabinet_total': 40, 'cabinet_used': 28, 'cabinet_free': 12,
    'power_capacity_kw': 200, 'power_used_kw': 120, 'load_rate': 0.60, 'pue': 1.45,
  },
  'B103': {
    'room': 'B103', 'cabinet_total': 60, 'cabinet_used': 51, 'cabinet_free': 9,
    'power_capacity_kw': 300, 'power_used_kw': 240, 'load_rate': 0.80, 'pue': 1.50,
  },
}


def _match_rooms(query_room: str) -> list[dict]:
  if not query_room:
    return list(_MOCK_CAPACITY.values())
  return [v for k, v in _MOCK_CAPACITY.items() if _match_room(k, query_room)]


def _h_query_capacity(params: dict) -> dict:
  rooms = _match_rooms(params.get('room', ''))
  return {'count': len(rooms), 'rooms': rooms}


def _h_query_power_usage(params: dict) -> dict:
  rooms = _match_rooms(params.get('room', ''))
  usage = [
    {'room': r['room'], 'power_used_kw': r['power_used_kw'],
     'load_rate': r['load_rate'], 'pue': r['pue']}
    for r in rooms
  ]
  return {'count': len(usage), 'usage': usage}


# ── 分发表：capability_id -> 处理器 ──
_MOCK_HANDLERS = {
  'dcim.query_alarm': _h_query_alarm,
  'dcim.query_alarm_history': _h_query_alarm_history,
  'dcim.query_asset': _h_query_asset,
  'dcim.query_asset_count': _h_query_asset_count,
  'dcim.query_capacity': _h_query_capacity,
  'dcim.query_power_usage': _h_query_power_usage,
}

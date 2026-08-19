"""DCIM 平台能力数据（纯数据，平台专属）。

只登记「这个平台能查什么」的元数据；检索/工具生成/意图绑定等逻辑全在 base/ 统一层。
新增 DCIM 接口只需在此追加一条 Capability。数据来源：Phase 1 为 mock，DCIM 真实
接口就绪后仅需替换同目录 client.py 的实现，本文件与上层皆无需改动。

说明：description 面向语义检索，需覆盖同义词与使用场景，直接影响 tool_search 命中率。
category 是 L2 功能分类（alarm/asset/capacity），意图路由与分类清单的统一词汇；
intent_labels 留空，绑定表自动回退用 category（见 base/intent_bindings.py）。
"""

from service.query.base.capability import Capability

# ── 能力登记（Phase 1 mock，告警/资产/容量三域共 6 条）──

CAPABILITIES: tuple[Capability, ...] = (
  Capability(
    id='dcim.query_alarm',
    name='查询活跃告警',
    description=(
      '查询 DCIM 运维平台当前的活跃告警/报警/异常事件。'
      '可按机房楼栋、告警级别过滤。'
      '适用于「某机房现在有哪些告警」「有没有严重告警」「XX 设备报警了吗」等问题。'
    ),
    params={
      'room': '机房或楼栋编号，如 A701、B103，也可只填栋号 A7；留空查所有',
      'severity': '告警级别 critical/warning/info；留空查所有级别',
    },
    category='alarm',
    domain='alarm',
    kind='状态',
  ),
  Capability(
    id='dcim.query_alarm_history',
    name='查询历史告警',
    description=(
      '查询 DCIM 平台指定时间范围内的历史告警记录（含已恢复/已处理）。'
      '适用于「过去一周有多少告警」「XX 设备历史告警趋势」等回溯分析问题。'
    ),
    params={
      'room': '机房或楼栋编号；留空查所有',
      'start': '起始时间 ISO8601，如 2026-07-01',
      'end': '结束时间 ISO8601',
    },
    category='alarm',
    domain='alarm',
    kind='状态',
  ),
  Capability(
    id='dcim.query_asset',
    name='查询资产台账',
    description=(
      '查询 DCIM 资产/设备台账信息，包括设备型号、位置、状态、投产日期。'
      '适用于「XX 设备在哪个机房」「某型号设备有哪些」「设备基本信息」等问题。'
    ),
    params={
      'device_id': '设备编号，如 UPS-1A-03；留空按其他条件查',
      'room': '机房或楼栋编号，用于按位置筛选',
      'type': '设备类型，如 UPS/精密空调/PDU',
    },
    category='asset',
    domain='asset',
    kind='资源',
  ),
  Capability(
    id='dcim.query_asset_count',
    name='统计资产数量',
    description=(
      '按机房、类型统计 DCIM 资产/设备数量。'
      '适用于「A7 栋有多少台 UPS」「机房设备总数」等统计类问题。'
    ),
    params={
      'room': '机房或楼栋编号；留空统计全部',
      'type': '设备类型；留空统计所有类型',
    },
    category='asset',
    domain='asset',
    kind='资源',
  ),
  Capability(
    id='dcim.query_capacity',
    name='查询机房容量',
    description=(
      '查询机房的容量使用情况，包括机柜总量/已用/剩余、电力容量、空间余量。'
      '适用于「XX 机房还剩多少容量」「机柜够不够用」「电力负载多少」等问题。'
    ),
    params={
      'room': '机房或楼栋编号，如 A701；留空查所有机房汇总',
    },
    category='capacity',
    domain='capacity',
    kind='资源',
  ),
  Capability(
    id='dcim.query_power_usage',
    name='查询能耗电力',
    description=(
      '查询机房或设备的实时/近期能耗与电力负载数据（功率、负载率、PUE）。'
      '适用于「机房现在用电多少」「负载率高不高」「PUE 是多少」等能耗问题。'
    ),
    params={
      'room': '机房或楼栋编号；留空查所有',
      'device_id': '设备编号，用于查单设备功率',
    },
    category='capacity',
    domain='capacity',
    kind='状态',
  ),
)

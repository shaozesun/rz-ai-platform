"""DCIM 能力声明（纯数据）。

**为什么需要这一层**：空间/设备/测点的**定义**在 PostgreSQL，测点**数值**在 TDengine
（宽表，一个设备类型一张超级表、一台设备一张子表、测点做列）。
而 **PG 没有连 TDengine 的 FDW**（跨库 JOIN 做不到，硬约束）——
所以跨库查询只能「先在一边定出范围，再去另一边取数，最后在应用层合并」。

这层就是把那个「两段式」封装掉，**不让 Agent 自己拼两个库的查询**。

设计依据：`docs/dcim-timeseries-storage-design.md` §8。
"""

import logging
import re

from service.query.base.capability import Capability

logger = logging.getLogger(__name__)


def _cluster_codes() -> tuple[str, ...]:
  """当前全部园区代号（来自 `deploy/config/dcim-clusters.json`，与消费端**同一份** → 单一真相）。

  这些值会进 `Capability.param_enums` → **在 schema 层生成成 enum**，
  所以**不要再往描述里罗列** —— 靠描述列值一定会过期，而且白占 token。

  （2026-10-13 实测的教训：描述里写「园区**名**（如「廊坊B1M2」）」，
  模型就把空间名「廊坊园区」填了进去，连试 6 个园区才蒙对。）
  """
  try:
    from service.dcim_ingest.config import clusters
    return tuple(c.cluster for c in clusters() if c.cluster)
  except Exception as e:      # 文件缺失/格式错/import 失败 → 退化成自由文本
    # ⚠️ 绝不能抛出：本模块是平台启动时必 import 的，挂在这里等于整个查询层起不来。
    #    宁可退化（模型可能填错）也不能让服务起不来。
    logger.warning('读不到园区清单，cluster 将退化为自由文本（模型可能填错）：%s', e)
    return ()


_CLUSTER_CODES = _cluster_codes()
_CLUSTER_ENUM = {'cluster': _CLUSTER_CODES} if _CLUSTER_CODES else {}

# 园区名 = 代号的前导中文段（廊坊B1M1 → 廊坊、惠州A2M1 → 惠州、佛山 → 佛山）。
# 一个园区常有多个代号，只按代号查会让模型一个个试、且 summary 的 P50/P90 合不出来。
_PARK_RE = re.compile(r'^([一-鿿]+)')


def park_of(cluster: str) -> str:
  """代号 → 园区名；认不出就原名返回（与 `incident_stats/rules.park_of` 同一规则）。"""
  m = _PARK_RE.match(cluster or '')
  return m.group(1) if m else (cluster or '')


PARK_NAMES = tuple(dict.fromkeys(park_of(c) for c in _CLUSTER_CODES if c))


def cluster_codes() -> tuple[str, ...]:
  """全部园区代号（供调用层把 `park` 展开成代号列表，不重复读配置文件）。"""
  return _CLUSTER_CODES


# 七个能力共用的 cluster 说明（保持完全一致 —— 模型可能直接调任意一条）
# ⚠️ **这里不再罗列代号** —— 合法值已经在 schema 的 enum 里（见 `_CLUSTER_ENUM`）。
_CLUSTER_DESC = (
  '园区**代号**（如「廊坊B1M2」）。⚠️ **不是空间名** ——「廊坊园区」是空间名，别拿它当 cluster。'
  '**不传 = 跨所有园区查**：返回记录**自带 `cluster` 字段**，一次就看出数据在哪个园区。'
  '⚠️ **别拿代号一个个园区去试** —— 试到一半就停会**漏数据**；要跨园区就干脆不传'
)

# 空间参数：七个能力共用（措辞必须**完全一致**，否则模型对"能传哪一层"的理解会飘）
# ⚠️ 别写「按房间」—— 房间没有子空间，且会让模型**不敢传楼宇/园区**的 guid。
#    真实语义：**传任意层级**，工具内部用递归子空间 CTE 往下带（实测：传楼宇 guid 能带出它下面的房间）。
_SPACE_DESC = (
  '按**空间**查（**园区 / 楼宇 / 房间任意层级都行** —— 自动含其下所有子空间）。'
  'guid **必须**先用 query_space_tree 拿到，别自己编'
)

# 空间树结构说明（2026-11-05）：模型此前把「A1」当成了园区代号，查的是整个廊坊A（37,585 条），
# 而 A1 楼的真实条数是 550。工具说明里只有「园区/楼宇/房间任意层级」，没有层级怎么拼、
# 也没有说 A1 是楼 —— 光给 `space` 参数它仍然不知道「A1」该填哪。
# 只放能力说明、不进系统提示词（常驻 token 更贵；且模型只在调这条能力时才需要这层结构）。
_SPACE_TREE_DESC = (
  '空间树（实测最多 5 层，各楼中间层不完全一致）：'
  '园区（廊坊园区 / 重庆园区）→ 数据中心（A1、A10、A12…）→ 区域'
  '（机房区域 / 制冷区域 / 配电区域，也写作 M1、M2 模组）→ 楼层（三层、M1-3F…）→ '
  '房间（101机房、UPS电池室、冷冻站、强电间）；柴发 / 水泵房 / 集中冷站直接挂在数据中心下面。'
  '⚠️ 园区代号（cluster）形如「廊坊A」「廊坊B1M1」「重庆」—— **A1 不是代号**，'
  'A1 是廊坊A 园区里的一栋楼；问「A1 的告警」要用 space，只传 cluster 会算成整个园区（14 栋楼）。'
  '⚠️ 同一短代号在多个园区都可能有（A1 在廊坊A 与重庆都有），跨园区查要带 cluster / park。'
  '⚠️ 空间名**不保证包含父空间名**（房间叫「廊坊园区A10数据中心M1一层101机房」，不带「机房区域」），'
  '所以别用名字模糊匹配代替按节点查子树。'
)

# 告警两条能力共用：按空间名称查（服务端解析成 guid，再走递归子树）
_SPACE_NAME_DESC = (
  '按**空间名称**查（园区 / 楼宇 / 房间任意层级，自动含其下所有子空间），'
  '比 `keyword` 精确 —— `keyword` 会串到设备名与告警描述。'
  '写法：`A1`（整栋楼）、`A1 101机房`（楼 + 房间，空格分隔 = 同时满足）、`M1一层101机房`；'
  '不带楼宇限定的短名（如 `101机房`）会多义，此时返回候选、要求补全。\n'
  + _SPACE_TREE_DESC
)

# 告警两条能力共用：用户给的是「一个时刻」而不是区间时，
# 必须点查、不得自行扩窗。
# 实测：同一秒只有个位数条，扩成十几分钟就变成四位数 —— 直接答错。
# ⚠️ 规则只写抽象表述，不放具体日期/时刻示例 ——
#    模型会把示例里的值照搬进回答。
_POINT_IN_TIME_RULE = (
  '⚠️ 用户问的是**一个具体时刻**（精确到分或秒，不是区间）时，'
  '`start_ms` 与 `end_ms` 传**同一个值**，平台按闭区间精确匹配该秒；'
  '**不要自行把这个时刻扩成前后一段时间或整天** —— '
  '会把条数放大几个数量级、答非所问。'
  '只有用户明说「前后 / 附近 / 这段时间」时才给区间。'
)

# 告警两条能力共用：按园区整体查（用户嘴里说的「廊坊」「惠州」是园区，不是代号）
_PARK_DESC = (
  '园区**名**（如「廊坊」）—— 展开成该园区下**全部代号**，一次查询、一份汇总。'
  '与 `cluster` 二选一（同时传以 `cluster` 为准）。'
  '⚠️ 要「整个园区」时必须用它：逐个传代号查回来的 summary 合不出 P50/P90'
)
_ALARM_ENUMS = {**_CLUSTER_ENUM, 'park': PARK_NAMES} if PARK_NAMES else dict(_CLUSTER_ENUM)

# PUE 查询：在告警那套枚举上加时间口径（token 与 `pue_catalog.TimeType.token` 同源，
# 中文写法也认，但 enum 里只放英文 token —— 枚举值会进 schema，越少越省 token）
_PUE_ENUMS = {
  **_ALARM_ENUMS,
  'time_type': ('real', 'hour', 'day', 'week', 'month', 'year'),
}

CAPABILITIES: tuple[Capability, ...] = (
  Capability(
    id='dcim_meta.query_space_tree',
    name='空间树查询',
    description=(
      '查询数据中心的**空间层级**：园区 → 楼宇 → 机房/包间。可按空间名称关键词定位，'
      '返回该空间的子节点。要按名称找机房、看某机房属于哪个楼/哪个园区、'
      '或不确定用户说的「A1」「M1」指哪一层时**先用它看清结构**。\n'
      + _SPACE_TREE_DESC
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'keyword': '空间名称关键词（模糊匹配，如「201机房」）；不给则返回该园区的顶层空间',
      'idc': '数据中心标识，可选（注意：实测对方推的 idc 多为空串，通常筛不出东西）',
    },
    category='asset',
    domain='space',
    kind='query',
    param_enums=_CLUSTER_ENUM,
  ),
  Capability(
    id='dcim_meta.query_devices',
    name='设备台账查询',
    description=(
      '查询**动力环境监控设备**台账：名称、所属空间、设备类型、机柜位置。'
      '可按空间（**含其下所有子空间**，如房间下的包间/机柜）、设备类型、名称关键词筛。'
      '要「某机房有哪些设备」「某类设备有多少台」时用它。'
      '⚠️ 这里的设备是**动环监控**范围内的；要按**部门**查全量设备台账（配电/暖通/其他弱电/消防），'
      '用综合管理平台的 `mgmt.query_device_info`。'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'space_guid': _SPACE_DESC,
      'device_type': '设备类型编码（如 00000000-10BC-…；暂无中文名字典）',
      'keyword': '设备名称关键词（模糊匹配，如「冷通道温湿度」）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 200',
    },
    category='device',
    domain='asset',
    kind='query',
    param_enums=_CLUSTER_ENUM,
  ),
  Capability(
    id='dcim_meta.query_points',
    name='测点定义与现状查询',
    description=(
      '查询测点的**定义**：名称、单位、点位类型、状态映射（如 0=正常;1=中断）、'
      '以及 4 档告警阈值（提示/一般/重要/紧急）；`with_latest=true` 时**同时带上每个测点的最新值**。'
      '告警阈值给成 `alarms`：**只列已配置的档位**，每档是 `[下限, 上限]` 两个数'
      '（某一侧没配就是 `null`）；完全没配阈值的测点不带这个字段。'
      '**测点的单位、量程、告警阈值只有这里有**（时序库里只有数值）—— 看到数值想解释它、'
      '或要判断"多少算高"时必须先查这里。\n'
      '**查"某房间/某机房的点位情况"就用它**：传 `space_guid`（**自动含子空间**）+ '
      '`with_latest=true`，一次拿到该空间下所有设备的全部测点及其当前值。\n'
      '⚠️ **时间字段（如 `latest_ts`）已是北京时间（Asia/Shanghai），直接展示，不要再做时区换算。**'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'space_guid': _SPACE_DESC,
      'device_guid': '按单台设备查',
      'keyword': '测点名称或编码关键词（模糊匹配，如「温度」）',
      'with_latest': '是否带每个测点的**最新值**（true/false，默认 false）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 300',
    },
    category='device',
    domain='asset',
    kind='query',
    param_enums=_CLUSTER_ENUM,
  ),
  Capability(
    id='dcim_meta.query_series',
    name='测点数值时序查询',
    description=(
      '查询测点的**数值时间序列**（TDengine 宽表）—— 要某测点/某机房/某设备的'
      '**曲线、趋势、历史值**就用它。**一行 = 一台设备一个时刻，各测点并排成列** —— '
      '所以同一行的各数值天然同刻，可以直接对比、算能耗、画趋势。\n'
      '⚠️ **必须给时间窗**（start/end），否则会全表扫、很慢。\n'
      '⚠️ **时段长就用 `interval` 聚合**：不聚合的话单测点 30 天约 26 万行，会被行数上限截断；'
      '给 `interval`（如 `1h`）后 30 天只剩 720 行/设备。**超过几小时的分析一律带 `interval`。**\n'
      '⚠️ 超级表按**设备类型**分 —— 知道类型就给 `device_type`（**全量分页只在这条路径上**）；'
      '只知道机房就改传 `space_guid`，会自动查出该空间有哪几类设备、逐类取回并合并'
      '（跨类型单次取数有上限，要全量务必给 `device_type`）。\n'
      '⚠️ **一行 = 一台设备在该时刻的全部测点**（宽表，一台 PDU 就上千列）。'
      '看**单个/几个测点**的曲线必须带 `point_ids`（先用 query_points 取 point_id），'
      '否则整行会撑满输出上限、只回得来 1 行，画不出趋势。\n'
      '要测点的单位/阈值先去 query_points；要"当前值一览"用 query_points 的 with_latest；'
      '要"哪些测点超限"直接用 query_over_threshold。\n'
      '⚠️ **返回的时间列（`ts` / `_wstart` / `report_ts`）已是北京时间（Asia/Shanghai），'
      '直接展示，不要再做时区换算。**'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'device_type': '设备类型编码（可先用 query_devices 拿到）。**给了就单类型查、分页准确**',
      'device_guids': '要查的设备 guid，**多个用英文逗号分隔**（留空则查该类型全部设备，量大时慎用）',
      'point_ids': '只看这几个测点：用 query_points 返回的 `point_id`，多个用英文逗号分隔。'
                   '**查某测点的曲线务必带**（宽表一行上千列，不选列会被输出上限截断成 1 行）。'
                   '与 `device_guids` **配对使用即可**，设备类型由服务端按设备档案自动定位；'
                   '只有不带设备时才需要 `device_type`（那表示查该类型全部设备）',
      'space_guid': _SPACE_DESC + '。没给 device_type 时用它收窄范围',
      'start_ms': '起始时间。**直接传日期即可**，如 `2026-10-19` 或 `2026-10-19 00:00:00`'
                  '（北京时间）；也接受毫秒/秒时间戳。**必填**。⚠️ 不要自己换算 epoch，传日期',
      'end_ms': '结束时间，写法同 start_ms。**必填**',
      'interval': '按时间窗**聚合**的窗口宽度，如 `10m` / `1h` / `1d`（**时段长就必须给**；给了就走聚合，返回 `_wstart` 窗口起点 + 各列聚合值）',
      'agg': '聚合函数，配合 interval：avg（默认）/ max / min / sum / first / last / count',
      'page': '页码，默认 1（**聚合路径也支持翻页**；聚合结果是"每设备 × 每窗口一行"，长区间要多翻几页取全）',
      'page_size': '每页行数，默认 2000',
    },
    category='device',
    domain='series',
    kind='query',
    param_enums=_CLUSTER_ENUM,
  ),
  Capability(
    id='dcim_meta.query_over_threshold',
    name='超阈值测点清单',
    description=(
      '列出**当前值已越过告警阈值**的测点：测点、单位、当前值、所属设备、'
      '告警档位（提示 / 一般 / 重要 / 紧急）、越过的阈值与方向（高于上限 / 低于下限）。\n'
      '**「现在有哪些测点在报警」「巡检一下有没有超限」就用它** —— 阈值在元数据、'
      '当前值在时序库，这一层已经跨库比好了，不用自己拉一堆数来对。\n'
      '⚠️ 只比较**定义了阈值的测点**；通断/状态这类非数值测点（有值才对得上阈值）不参与。\n'
      '⚠️ **时间字段（`ts`）已是北京时间（Asia/Shanghai），直接展示，不要再做时区换算。**'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'space_guid': _SPACE_DESC,
      'device_guid': '按单台设备查',
      'keyword': '测点名称关键词（模糊匹配，如「温度」）',
      'level': '只看某一档**及以上**：urgent（紧急）/ important（重要）/ general（一般）/ hint（提示）',
    },
    category='alarm',
    domain='alarm',
    kind='query',
    param_enums=_CLUSTER_ENUM,
  ),
  Capability(
    id='dcim_meta.query_alarms',
    name='告警事件查询',
    description=(
      '查询**对方系统推过来的告警事件**（触发/刷新/恢复三态，同一告警串成一条）。'
      '**「现在有哪些告警在跑」「某房间报过什么告警」「有没有紧急告警」就用它**。\n'
      '⚠️ **别和 `query_over_threshold` 混** —— 那个是**我们自己算的**「此刻值越没越限」'
      '（没有生命周期、没有恢复状态）；这里是**对方告警引擎的事件**，有 触发→刷新→恢复 的完整生命周期。\n'
      '⚠️ 判「在跑的」**只看 `status`**（`<>` 恢复），**不要看解除值是不是 0** —— '
      '未恢复时对方给的解除字段是**哨兵 0**（真实恢复值也可能就是 0）。\n'
      '返回的 `alarm_time` / `remove_time` 已渲染成**北京时间**（`YYYY-MM-DD HH:MM:SS`），'
      '`status` / `alarm_level` / `alarm_type` 已换成中文 —— **直接展示，不要再做时区换算或查码表。**\n'
      '**地点直接用 `space_name`**：它是空间全名（园区 + 数据中心 + 模组 + 楼层 + 房间，'
      '如「廊坊园区A12数据中心M1模组三层M1-3F-强电间2」），**不要拿 `space_guid` 当地点展示**。\n'
      '**每条告警若已建处置工单，会自带嵌套 `work_order`**（`order_no` / `status` / `responder` / '
      '`handler` / `response_time` / `response_minutes` / `process_result` / `star`）；没有就整个字段不出现。'
      '**问单条或某台设备的告警"处理了没"直接看这里，不用再调 `query_alarm_orders`** —— '
      '那个用于按人/按时段/跨告警批量统计处置情况。'
      '⚠️ **「响应超时」有两种，别混**：① **告警响应超时**（发生超 5 分钟 + 仍未解除 + '
      '重要及以上 + 没有关联处置工单）→ 用**本能力**的 `unresponded_min` 配 `level=重要`，'
      '完整口径见技能 `skill__alarm_response_timeout`；② **已响应但慢**'
      '（响应时刻 − 告警时刻 > N 分钟）→ 用 `query_alarm_orders` 的 `response_timeout_min`。'
      '**不能拿 `work_order.response_minutes` 自己筛**（分页只回来一部分，会漏）。\n'
      '⚠️ **问某栋楼 / 某房间的告警用 `space`（如 `A1`），不要用 `keyword`** —— '
      'keyword 同时匹配点位名/设备名/告警描述/空间名，实测比精确范围多一串；'
      '更不要把「A1」当成 `cluster`（那是整个园区）。'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'park': _PARK_DESC,
      'space': _SPACE_NAME_DESC,
      'space_guid': _SPACE_DESC,
      'device_guid': '按**单台设备**查（问"某台设备的告警"时用它，别把整个房间倒出来）',
      'keyword': '关键词（模糊匹配点位名 / 设备名 / 事件描述 / **空间全名**，'
                 '如「温度」「A12」「强电间」）',
      'level': '只看**该档及以上**：紧急 / 重要 / 一般 / 提示（= 对方的 4 / 3 / 2 / 1）',
      'unresponded_min': '只看**发生超过 N 分钟、仍未解除、且没有关联处置工单**的告警'
                         '（= 还没人响应）。「告警响应超时」的标准是 N=5 配 `level=重要`'
                         '（重要及以上），问「哪些重要告警没人管」直接这么传，不要反问。'
                         '⚠️ 判据是**有没有关联该告警的工单**（人员点响应会自动建单）；'
                         '已响应但慢的单用 `query_alarm_orders` 的 `response_timeout_min`',
      'status': 'active（默认，**在跑的**，含触发+刷新）/ recovered（已恢复）/ '
                'all（**全部**：触发+恢复）/ 1 / 2 / 3。'
                '⚠️ 用户要「所有告警 / 含已恢复」时**必须传 `all`** —— '
                '不传 = 只看在跑的，会漏掉已恢复的',
      'start_ms': '起始时间：毫秒时间戳（也接受秒级 / ISO 如 `2026-10-17`），可选。\n'
                  + _POINT_IN_TIME_RULE,
      'end_ms': '结束时间：毫秒时间戳（也接受秒级 / ISO 如 `2026-10-18`），可选。'
                '**只传日期（如 `2026-10-18`）＝含当天整天**\n'
                + _POINT_IN_TIME_RULE,
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 200',
    },
    category='alarm',
    domain='alarm',
    kind='query',
    param_enums=_ALARM_ENUMS,
  ),
  Capability(
    id='dcim_meta.query_alarm_orders',
    name='告警工单查询',
    description=(
      '查询**告警工单**（对方告警的人工处置记录）：「这条告警处理了吗」「谁处理的」'
      '「响应多快」「处理结果是什么」「某段时间处理了多少单」就用它。\n'
      '⚠️ **别和 `query_alarms` 混**：那个回答「告警在不在跑」（对方的告警事件，'
      '有触发/刷新/恢复）；这里是**人工填写的工单**（处理中/已处理、响应人、响应时刻、处理结果）。'
      '⚠️ **单条告警的处置情况不用调这里** —— `query_alarms` 会给每条告警带上嵌套 `work_order`；'
      '这里用于**按人 / 按时段 / 跨告警批量统计**处置情况（带全量 `summary`）。'
      '两者用 `event_log_id` 关联 —— 也可把 `query_alarms` 的 `event_log_id` 传进来精确查。\n'
      '返回里除 `records`/`total` 外还有 `summary`：**按整个过滤集**（不是当前页）统计的'
      '工单总数、处理中/已处理、各档级别、响应时长 avg/P50/P90（分钟）。'
      '「响应多快」直接引用 `response_minutes`，**不要自己拿时间相减**。\n'
      '`response_minutes`（响应时刻 − 告警时刻）与 `handle_minutes`（处理结束 − 开始）'
      '已由服务端算好；所有时间字段已是**北京时间**，`status`/`alarm_level`/'
      '`alarm_status`/`process_result` 已换成中文 —— **直接展示，不要再做换算或查码表。**\n'
      '**问「响应超时的单」传 `response_timeout_min`**（如 5 = 响应比告警晚 5 分钟以上）：'
      '以工单为主表算 `response_time - alarm_time`，`summary` 也随之只统计这批。'
      '配上 `level=重要` 就是「重要 + 紧急」的响应超时单（该参数是**该档及以上**，紧急含在内）。\n'
      '**问「某个园区」用 `park`**（如「廊坊」「惠州」）—— 它会展开成该园区下全部代号，'
      '一次查询、`summary` 就是整园的；逐个传 `cluster` 查回来的分位（P50/P90）合不出来。\n'
      '⚠️ 工单是**人工处置记录**，不是每条告警都有：查不到 ≠ 告警不存在，可能是还没人填单。\n'
      '⚠️ **「还没人响应」的告警这里查不到**（没有工单就没有行）—— 那是 `query_alarms` 的 '
      '`unresponded_min`（发生超过 N 分钟仍无任何工单）；本能力的 `response_timeout_min` '
      '只回答「已响应、但响应慢了」。'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'park': _PARK_DESC,
      'space': _SPACE_NAME_DESC,
      'space_guid': _SPACE_DESC,
      'device_guid': '按**单台设备**查（问"某台设备的告警处理情况"时用它）',
      'event_log_id': '按**告警事件 id** 查对应工单（= `query_alarms` 返回的 `event_log_id`）',
      'keyword': '关键词（模糊匹配空间名 / 设备名 / 测点名 / 告警描述，如「UPS」）',
      'operator': '按人查：响应人 / 处理人 / 实际操作者的姓名关键词（如「郭云龙」）',
      'level': '只看**该档及以上**：紧急 / 重要 / 一般 / 提示（= 对方的 4 / 3 / 2 / 1）',
      'status': 'all（默认，**全部**：处理中 + 已处理）/ 1 处理中 / 2 已处理。'
                '⚠️ 问「哪些还没处理完」传 `1`；不传 = 不过滤',
      'start_ms': '按**告警发生时间**筛起始（直接传日期，如 `2026-10-19`，北京时间），可选。\n'
                  + _POINT_IN_TIME_RULE,
      'end_ms': '按**告警发生时间**筛结束，写法同 start_ms，可选。'
                '**只传日期（如 `2026-10-20`）＝含当天整天**\n'
                + _POINT_IN_TIME_RULE,
      'response_timeout_min': '只看**响应超时**的单：响应时刻 − 告警时刻 > 该分钟数'
                              '（如 `5`）。⚠️ **只算已记录响应时刻的工单** —— 还没响应的'
                              '不在这里（那是「没响应」不是「响应超时」）',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 100',
    },
    category='alarm',
    domain='alarm',
    kind='query',
    param_enums=_ALARM_ENUMS,
  ),
  Capability(
    id='dcim_meta.check_consistency',
    name='跨库一致性巡检',
    description=(
      '核对「时序库里出现过的设备/测点」与「元数据里的定义」是否对得上，'
      '报出**只有数值没有定义**（孤儿）和**只有定义没有数值**的情况。'
      '跨库关联靠的是 guid 拼接约定（`设备guid.测点编码`），对方改命名规则会**静默断掉** —— '
      '怀疑数据对不上、或要定期体检时用它。'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'device_type': '设备类型编码（只核对这一类；不给则用元数据里出现的全部类型）',
    },
    category='device',
    domain='series',
    kind='query',
    param_enums=_CLUSTER_ENUM,
  ),
  Capability(
    id='dcim_meta.query_pue',
    name='PUE 与能耗查询',
    description=(
      '查询 **PUE（电能利用效率）与各项耗电量**的统计值 —— 按园区 / 楼宇 / 时间口径取数。'
      '**「现在 PUE 多少」「某楼这个月每小时的 PUE 趋势」「IT 与制冷耗电量对比」就用它。**\n'
      '⚠️ **只有上游真推了 PUE 的园区 / 楼宇才有数据** —— 查回 0 行不代表 PUE 是 0，'
      '而是该楼没有上报。\n'
      '⚠️ **时间口径要分清**：`time_type` 默认 `real`（实时值，约 10 秒一条）；'
      '「小时 / 日 / 周 / 月 / 年」是对方按整点或期末推送的统计快照，**必须显式传 `time_type`**'
      '（如 `hour`），否则只会拿到实时值。\n'
      '⚠️ **看历史趋势必须给时间窗**（start/end），且时段长（超过几小时）要带 `interval` 聚合；'
      '**`latest=true` 只看每个「楼宇 × 口径」的最新一行**（问当前值就用它），不需要时间窗。\n'
      '18 个指标：`pue`（PUE 本身）、`total_value`（总耗电量）、`it_value`（IT 耗电量）、'
      '`cool_value`（制冷耗电量）、`ac_value`（空调耗电量）、`light_value`（照明耗电量）…；'
      '不传 `metrics` 默认全取。返回里的 `metric_labels` 是「列名 → 中文」对照表。\n'
      '⚠️ **返回的时间列（`ts` / `_wstart`）已是北京时间（Asia/Shanghai），'
      '直接展示，不要再做时区换算。**'
    ),
    params={
      'cluster': _CLUSTER_DESC,
      'park': _PARK_DESC,
      'building': '楼宇的 `idc` 编码（如 `P001_A009`），多个用英文逗号分隔；'
                  '不给则查该范围内的全部楼宇',
      'space': '按**楼宇空间名**定位（如 `A1`，须是楼宇层 —— PUE 是楼宇级统计），'
               '服务端解析成 `space_guid` 后按 tag 过滤；多义或不命中会报错',
      'time_type': '时间口径：real（默认，实时值）/ hour（小时快照）/ day / week / month / year；'
                   '也接受中文（实时/小时/日/周/月/年），多个用英文逗号分隔',
      'metrics': '指标列，多个用英文逗号分隔，默认全部 18 个。可传列名（pue / total_value / '
                 'it_value / cool_value / ac_value / light_value …）或中文名（PUE / 总耗电量 / '
                 'IT耗电量 …）',
      'start_ms': '起始时间。**直接传日期即可**（如 `2026-10-19`，北京时间），'
                  '也接受毫秒/秒时间戳。**非 latest 时必填**',
      'end_ms': '结束时间，写法同 start_ms。**非 latest 时必填**',
      'latest': 'true = 只看每个「楼宇 × 口径」的**最新一行**（问"现在 PUE 多少"用），'
                '不需要时间窗',
      'interval': '按时间窗**聚合**的窗口宽度，如 `10m` / `1h` / `1d`；'
                  '长区间必须给（实时值约 10 秒一条，不聚合会被行数上限截断）',
      'agg': '聚合函数，配合 interval：avg（默认）/ max / min / sum / first / last / count',
      'page': '页码，默认 1',
      'page_size': '每页条数，默认 2000',
    },
    category='energy',
    domain='pue',
    kind='query',
    param_enums=_PUE_ENUMS,
  ),
)

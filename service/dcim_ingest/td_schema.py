"""TDengine 宽表的**命名与映射规则**（纯函数，不碰数据库，便于单测）。

宽表模型：**一个设备类型一张超级表**（列 = 该类型的各个测点），**一台设备一张子表**。
测点特别多的类型（余量撞 TDengine 4,096 列上限）按**测点分组**拆成多张超级表
（`st_<type>` = g0、`st_<type>_g1`…），容量线见 `GROUP_MAX_COLUMNS`。
本模块负责三件事：

  1. 设备类型（+ 测点分组）→ 超级表名、设备 → 子表名
  2. 测点编码 → TDengine 列名（含**冲突去重**）
  3. 测点分级（哪些算「重要」→ 进业务当前态表 + 多档聚合）

**为什么单独成模块**：这些规则一旦变了，**历史数据就对不上了**
（列名变了 = 旧数据在新映射下找不到）。所以规则必须集中、可测，
且**计算结果要落库**（`schema.py` 的 `dcim_td_stable` / `dcim_td_column` /
`dcim_point_level`）—— 本模块只负责「怎么算」，落库由调用方做。

经验依据：行业里「文档编码规范 vs 实际推送编码」不一致极常见（多因固件版本差异），
我们自己也撞到过（文档示例 `1001000` 数字码、实推 `HT_Temp` 字符串）。
所以映射是**数据**，不是代码里的常量。
"""

from __future__ import annotations

import hashlib
import re

# TDengine 表名上限 192；留足余量（子表名还要拼设备 guid）。
_MAX_TABLE = 120
_MAX_COLUMN = 60          # 列名没有硬性 192 限制那么紧，但太长没意义

# 每组的数据列容量线：+3 系统列（ts/report_ts/quality）+6 tag = 4,009，
# 距 TDengine 单表 4,096 列硬上限留 87 列余量。**容量线只在这里定义**，
# 分配（repository）与告警（wide_writer）都用它，别各写一份。
GROUP_MAX_COLUMNS = 4000
# 撞墙前的预警线：分到这么满就打 WARNING，运维能在下一次分配失败之前看到
GROUP_WARN_COLUMNS = 3900

_PREFIX_STABLE = 'st_'    # 超级表前缀：TDengine 表名不能以数字开头
_PREFIX_SUBTABLE = 'd_'   # 子表前缀
_COLUMN_FALLBACK = 'c_unnamed'   # 测点编码为空时（上游数据问题）的占位列名

_UNSAFE = re.compile(r'[^0-9a-zA-Z_]')
_MULTI_UNDERSCORE = re.compile(r'_{2,}')

# TDengine 保留字里我们**已经踩过**的两个（`value`、`cluster` 建表直接语法错）。
# 不追求穷举，只挡住已知的和最显然的 —— 撞上就加 `_v` 后缀。
# 列名**不统一加前缀**（那样虽然省事，但 `value`/`cluster` 就永远撞不上，
# 这段检查会变成死代码）；只在「以数字开头」这种硬性非法时才补前缀。
_RESERVED = {
  'value', 'values', 'cluster', 'timestamp', 'ts', 'tag', 'tags', 'table',
  'database', 'select', 'from', 'where', 'group', 'order', 'by', 'interval',
  'name', 'type', 'level', 'status', 'partition', 'range', 'between', 'and', 'or',
}


def _normalize(raw: str, *, prefix: str, max_len: int) -> str:
  """归一化成 TDengine 可用的标识符：转小写、非字母数字下划线换 `_`、压缩连续下划线。

  归一化后**过长则截断**（截断后可能撞名，由调用方按需去重）。
  """
  s = _UNSAFE.sub('_', str(raw).strip()).lower()
  s = _MULTI_UNDERSCORE.sub('_', s).strip('_')
  return f'{prefix}{s}'[:max_len] if s else ''


def stable_name(device_type: str, point_group: str = 'g0') -> str:
  """设备类型（+ 测点分组）→ 超级表名。

  同一设备类型的**同一测点分组**在**所有园区**共用一张超级表（设备类型编码是对方给的、全局一致）。
  `g0` 沿用原表名 `st_<type>`（存量数据零迁移），`g1` 起是 `st_<type>_g<n>`。
  """
  base = (_normalize(device_type, prefix=_PREFIX_STABLE, max_len=_MAX_TABLE)
          or _PREFIX_STABLE.rstrip('_'))
  if point_group and point_group != 'g0':
    group = _normalize(point_group, prefix='', max_len=6)
    if group:
      return f'{base[:_MAX_TABLE - len(group) - 1]}_{group}'
  return base


def next_group(point_group: str) -> str:
  """g0 → g1 → g2…（分配时「本组装满、换下一组」用；认不出就给 g1）。"""
  match = re.fullmatch(r'g(\d+)', (point_group or '').strip())
  return f'g{int(match.group(1)) + 1}' if match else 'g1'


def _subtable_prefix(cluster_code: str) -> str:
  """子表前缀：`d_<园区代号>_`；代号为空时退回旧前缀 `d_`。"""
  code = _normalize(cluster_code, prefix='', max_len=12)
  return f'{_PREFIX_SUBTABLE}{code}_' if code else _PREFIX_SUBTABLE


def subtable_name(device_guid: str, point_group: str = 'g0', cluster_code: str = '') -> str:
  """设备 guid（+ 点位组 + 园区代号）→ 子表名 `d_<园区代号>_<guid>`。

  **为什么必须带园区**：设备身份只在园区内唯一（PG 主键 `(cluster, guid)`），而子表名
  在整库内唯一、tag 创建后不再更新 —— 两个园区写同名子表会让后写的数据串到先建的
  tag 上。代号由 `park_codes.CLUSTER_CODES` 给出；`cluster_code=''` 只保留旧命名
  （兼容既有自检，生产写入一律传代号）。

  点位组只有「设备特别大、测点要拆成多张子表」时才不是默认的 `g0`。
  """
  base = _normalize(
    device_guid, prefix=_subtable_prefix(cluster_code), max_len=_MAX_TABLE - 8,
  )
  if point_group and point_group != 'g0':
    group = _normalize(point_group, prefix='', max_len=8)
    if group:
      base = f'{base}_{group}'
  return base[:_MAX_TABLE]


def subtable_variant(
  device_guid: str, point_group: str, tag: str, cluster_code: str = '',
) -> str:
  """子表名的**备用形态**：原名 + `_<sha1(tag)[:6]>`（确定性、可复现）。

  用途：设备的 `device_type` 变过之后，原名 `d_<园区代号>_<guid>` 已绑定在**旧超级表**上，
  往新超级表写会报 `Table already exists in other stables`（实测 2026-11-02，约 30 台）。
  换一个带类型指纹的名字就能继续写，旧表/旧数据原样保留、不删不改。

  ⚠️ 读侧不依赖子表名（按 tag 查超级表），所以换名是安全的。
  """
  base = subtable_name(device_guid, point_group, cluster_code)
  suffix = '_' + hashlib.sha1(str(tag).encode('utf-8')).hexdigest()[:6]
  return base[:_MAX_TABLE - len(suffix)] + suffix


def normalize_column(point_code: str) -> str:
  """测点编码 → 列名（**不含冲突去重**；一批列名要一起去重请用 `allocate_columns`）。"""
  col = _normalize(point_code, prefix='', max_len=_MAX_COLUMN - 3)
  if not col:
    return _COLUMN_FALLBACK
  if col[0].isdigit():          # TDengine 标识符不能以数字开头
    col = f'c_{col}'
  if col in _RESERVED:          # `value` / `cluster` 等实测会直接建表失败
    col = f'{col}_v'
  return col[:_MAX_COLUMN]


def allocate_columns(point_codes, *, taken: object = ()) -> dict[str, str]:
  """一批测点编码 → {point_code: 列名}，**保证同一超级表内列名唯一**。

  去重规则：先归一化；撞名（归一化后相同、撞上 TDengine 保留字、或撞上 `taken`）就追加 `_2`、`_3`…

  ⚠️ **`taken` 必须传入该超级表里「已经注册过的列名」** —— 否则新增测点时，
  同一个编码可能被分到跟上次不同的列名，历史数据就对不上了。
  调用方要**按确定的顺序**传 `point_codes`（如 sorted），保证结果可复现。

  **结果必须落库**（`dcim_td_column`），这是映射稳定的唯一保证。
  """
  out: dict[str, str] = {}
  used: set[str] = set(taken)          # type: ignore[arg-type]
  for code in point_codes:
    col = normalize_column(code)
    if col in used:
      i = 2
      while f'{col}_{i}' in used:
        i += 1
      col = f'{col}_{i}'
    used.add(col)
    out[code] = col
  return out


def classify_level(
  *,
  device_name: str = '',
  device_type: str = '',
  point_code: str = '',
  point_name: str = '',
  rules: list[dict] | None = None,
  default: int = 2,
) -> tuple[int, str | None]:
  """按规则表判断测点级别，返回 `(level, matched_by)`。

  `rules` 是 `dcim_level_rule` 的行（按 `priority` 升序传进来即可），
  每条形如 `{'priority': 10, 'match_on': 'device_name', 'pattern': '电表', 'level': 1}`。

  匹配方式是**子串包含**（大小写不敏感）—— 门槛低、可解释，够用；
  要做正则再换 `match_on` 的取值即可，不动调用方。

  **判错的代价是有界的**：分级只影响冗余层（当前态表 / 聚合档位），
  原始明细一律全量落库，所以不会丢数据。
  """
  fields = {
    'device_name': device_name,
    'device_type': device_type,
    'point_code': point_code,
    'point_name': point_name,
  }
  for rule in rules or []:
    target = fields.get(str(rule.get('match_on', '')), '')
    pattern = str(rule.get('pattern') or '')
    if not pattern:
      continue
    if pattern.lower() in str(target).lower():
      level = int(rule.get('level', default))
      return level, f"{rule.get('match_on')}~{pattern}"
  return default, None

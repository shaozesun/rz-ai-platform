"""PUE / 能耗统计报文的字段目录（纯数据，无 IO）。

口径来自对方《PUE_attribute》表（108 个属性 = **18 个指标后缀 × 6 个时间口径**），
报文形态与存储方案见 `docs/dcim/dcim-pue-storage-query.md`。

**编码格式**：`PUE_<时间口径>_<指标后缀>`，如 `PUE_Real_IT_Value` / `PUE_Hour_Total_Value`。
⚠️ 指标后缀本身含下划线（`IT_Value` / `Total_Value` / `NoPower_Value`），**不能按 `_` 切分** ——
必须先把时间口径 token 剥掉、剩下的整体查表（见 `resolve_metric`）。

⚠️ `value` 是 TDengine 保留字（`td_schema._RESERVED`），所以 PUE 指标列叫 **`pue`**，不叫 `value`。
"""

from __future__ import annotations

from dataclasses import dataclass

PUE_PREFIX = 'PUE_'


@dataclass(frozen=True)
class TimeType:
  """时间口径：`Real`(实时) / `Hour`(小时) / `Day`(日) / `Week`(周) / `Month`(月) / `Year`(年)。"""

  token: str      # 编码里的 token（大写，如 'Real'）
  code: int       # 落库代号（`st_pue.time_type` tag）
  label: str      # 中文（页面对模型展示用）


TIME_TYPES: tuple[TimeType, ...] = (
  TimeType('Real', 1, '实时'),
  TimeType('Hour', 2, '小时'),
  TimeType('Day', 3, '日'),
  TimeType('Week', 4, '周'),
  TimeType('Month', 5, '月'),
  TimeType('Year', 6, '年'),
)
TIME_TYPE_BY_CODE = {t.code: t for t in TIME_TYPES}
_TIME_TYPE_BY_TOKEN = {t.token.lower(): t for t in TIME_TYPES}
# 剥前缀时**长 token 优先**：虽然当前 6 个 token 互不为前缀，但换口径时不至于静默错剥
_TIME_TOKENS_LONGEST_FIRST = tuple(sorted(TIME_TYPES, key=lambda t: -len(t.token)))


@dataclass(frozen=True)
class Metric:
  """一个指标后缀 → 落库列。

  `set_type` 是对方属性表的分类号（`Other` 是 0），**只用于对照文档，不参与任何逻辑**。
  """

  suffix: str      # 编码里的后缀（原样大小写，如 'IT_Value'）
  column: str      # TDengine 列名（小写蛇形）
  label: str       # 中文名
  set_type: int


METRICS: tuple[Metric, ...] = (
  Metric('Value', 'pue', 'PUE', 8),
  Metric('Total_Value', 'total_value', '总耗电量', 1),
  Metric('IT_Value', 'it_value', 'IT耗电量', 2),
  Metric('Cool_Value', 'cool_value', '制冷耗电量', 3),
  Metric('AC_Value', 'ac_value', '空调耗电量', 4),
  Metric('Mgr_Value', 'mgr_value', '设备用电损耗电量', 5),
  Metric('Light_Value', 'light_value', '照明系统电量设备耗电量', 6),
  Metric('AF_Value', 'af_value', '附属设施耗电量', 7),
  Metric('UPS_Value', 'ups_value', 'UPS损耗', 9),
  Metric('TF_Value', 'tf_value', '变压器损耗', 10),
  Metric('VRV_Value', 'vrv_value', 'VRV耗电量', 17),
  Metric('XF_Value', 'xf_value', '新风耗电量', 18),
  Metric('ITLoad_Value', 'itload_value', 'IT负载率耗电量', 19),
  Metric('Temp_Value', 'temp_value', '供水温度耗电量', 20),
  Metric('Wet_Value', 'wet_value', '月湿球耗电量', 21),
  Metric('NoPower_Value', 'nopower_value', '非生产用电耗电量', 22),
  Metric('CHM_Value', 'chm_value', '恒湿机耗电量', 23),
  Metric('Other_value', 'other_value', '其他耗电量', 0),
)

# 18 个指标列的固定顺序（建表、SQL 列清单、查询默认列都按它，保证各处一致）
METRIC_COLUMNS: tuple[str, ...] = tuple(m.column for m in METRICS)
COLUMN_LABELS: dict[str, str] = {m.column: m.label for m in METRICS}
# 口语/中文名 → 列（查询层让模型能传「IT耗电量」这种中文名）
COLUMN_ALIASES: dict[str, str] = {
  **{m.label: m.column for m in METRICS},
  **{m.column: m.column for m in METRICS},
  **{m.suffix: m.column for m in METRICS},
}

_METRIC_BY_SUFFIX = {m.suffix.lower(): m for m in METRICS}

# 108 个全名 → 列（供对照/自检/日后按全名查询）
FULL_NAME_TO_COLUMN: dict[str, str] = {
  f'{PUE_PREFIX}{tt.token}_{m.suffix}': m.column
  for tt in TIME_TYPES for m in METRICS
}


def resolve_metric(code: str) -> tuple[TimeType, Metric] | None:
  """`PUE_Real_IT_Value` → `(TimeType('Real'), Metric('IT_Value'))`；认不出返回 `None`。

  **绝不抛异常**：实时链路里一条认不出的编码只该被计数跳过，不能让整批报文失败。
  """
  raw = (code or '').strip()
  if not raw.lower().startswith(PUE_PREFIX.lower()):
    return None
  rest = raw[len(PUE_PREFIX):].lower()
  for tt in _TIME_TOKENS_LONGEST_FIRST:
    prefix = f'{tt.token.lower()}_'
    if not rest.startswith(prefix):
      continue
    metric = _METRIC_BY_SUFFIX.get(rest[len(prefix):])
    return (tt, metric) if metric else None
  return None


def resolve_time_type(value: str) -> TimeType | None:
  """查询参数 `time_type` → `TimeType`；接受 token（real/hour）、中文（实时/小时）与代号（1~6）。"""
  key = str(value or '').strip().lower()
  if not key:
    return None
  if key.isdigit():
    return TIME_TYPE_BY_CODE.get(int(key))
  hit = _TIME_TYPE_BY_TOKEN.get(key)
  if hit is not None:
    return hit
  return next((t for t in TIME_TYPES if t.label == key), None)


def resolve_column(value: str) -> str | None:
  """查询参数 `metrics` 的单个值 → 列名；接受列名 / 中文名 / 后缀。"""
  key = str(value or '').strip()
  if not key:
    return None
  if key in COLUMN_ALIASES:
    return COLUMN_ALIASES[key]
  return COLUMN_ALIASES.get(key.lower())


def time_type_label(code) -> str:
  """代号 → 中文；认不出原样返回（查询结果里 tag 可能是别的值）。"""
  try:
    tt = TIME_TYPE_BY_CODE.get(int(code))
  except (TypeError, ValueError):
    tt = None
  return tt.label if tt else str(code)

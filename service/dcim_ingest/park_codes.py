"""园区代号常量 —— **进入 TDengine 子表名，有数据后永不可改**。

设备身份只在**园区内**唯一（PG 主键 `(cluster, guid)`），而 TDengine 子表名在整库内
唯一、tag 一旦创建就固定。两个园区写同名子表时，后写的数据会静默落进先建的那张表、
带着别的园区的 `cluster_name`。所以子表名必须是 `d_<园区代号>_<guid>`。

**新增集群必须在这里登记**（`CLUSTER_CODES`），否则 `config.clusters()` 直接报错 ——
故意不兜底：静默生成代号会让故障表现成「数据看起来在、查询却看不到」。
"""

LANGFANG = 'LF-'
LANGFANGBM1 = 'LFBM1-'
LANGFANGBM2 = 'LFBM2-'
LANGFANGBM3 = 'LFBM3-'
PINGHUA = 'PHA-'
PINGHUBM1M2 = 'PHBM1M2-'
PINGHUBM3M4 = 'PHBM3M4-'
FOSHAN = 'FS-'
HUIZHOUM1 = 'HZMF-'
HUIZHOUM2 = 'HZMS-'
HUIZHOUA3M1 = 'HZATMF-'
HUIZHOUA3M2 = 'HZATMS-'
CHONGQING = 'CQ-'

# Kafka 集群名 → 园区代号。
# 惠州四个按业务要求直接用原始代号（A2M1/A2M2/A3M1/A3M2）。
# `HUIZHOUM1`/`HUIZHOUM2`/`HUIZHOUA3M1`/`HUIZHOUA3M2` 先保留常量，当前不映射集群。
CLUSTER_CODES: dict[str, str] = {
  '廊坊A': LANGFANG,
  '廊坊B1M1': LANGFANGBM1,
  '廊坊B1M2': LANGFANGBM2,
  '廊坊BM3': LANGFANGBM3,
  '平湖A': PINGHUA,
  '平湖B': PINGHUBM1M2,
  '平湖BM3M4': PINGHUBM3M4,
  '佛山': FOSHAN,
  '惠州A2M1': 'A2M1',
  '惠州A2M2': 'A2M2',
  '惠州A3M1': 'A3M1',
  '惠州A3M2': 'A3M2',
  '重庆': CHONGQING,
}


def code_for(cluster: str) -> str:
  """Kafka 集群名 → 园区代号；未登记直接抛 `KeyError`（不做兜底）。"""
  try:
    return CLUSTER_CODES[cluster]
  except KeyError:
    raise KeyError(
      f'园区代号未登记：{cluster} —— 在 service/dcim_ingest/park_codes.py 的 CLUSTER_CODES 里补上'
    ) from None

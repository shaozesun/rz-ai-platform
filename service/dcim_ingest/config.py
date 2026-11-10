"""worker 配置：集群清单读取 + confluent-kafka 客户端配置。

对方 DCIM 是**每个园区一个独立 Kafka 集群**（廊坊A/廊坊B/平湖/佛山/惠州/重庆…），
彼此 bootstrap 不同，所以「集群」是一等概念：消费线程 = 集群 × topic。
清单放在 `settings.DCIM_CLUSTERS_FILE` 指向的 JSON（随仓库评审），SASL 凭证走 .env。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from service.dcim_ingest import park_codes

logger = logging.getLogger(__name__)

# 仓库根：本文件在 <root>/service/dcim_ingest/config.py
_REPO_ROOT = Path(__file__).resolve().parents[2]


class ClusterConfigError(Exception):
  """集群清单缺失或格式不合法。"""


@dataclass(frozen=True)
class ClusterSpec:
  """一个园区集群。

  Attributes:
    cluster: 园区名（如 '廊坊A'），落库到各表的 cluster 列。
    bootstrap: confluent-kafka 的 bootstrap.servers；**同一集群多 broker 用逗号分隔**。
    broker: 对方的 broker 名（如 'dcim-broker-rztohj'），仅作参考，不参与连接。
    code: 园区代号（如 'LF-'），**进入 TDengine 子表名，有数据后不可变更**。
    concurrency: 该园区的实时消费线程数；**0 = 沿用全局 `DCIM_REALTIME_CONCURRENCY`**。
      只给大流量园区单独调（廊坊A 16 个分区 → 16 条线程，一区一条）；
      全局调大会给闲置园区也铺一遍线程和 PG 连接。
  """

  cluster: str
  bootstrap: str
  broker: str = ''
  code: str = ''
  concurrency: int = 0


def _clusters_path() -> Path:
  p = Path(settings.DCIM_CLUSTERS_FILE)
  return p if p.is_absolute() else _REPO_ROOT / p


def clusters() -> list[ClusterSpec]:
  """读集群清单并校验（园区名不重复、bootstrap 非空）。"""
  path = _clusters_path()
  if not path.exists():
    raise ClusterConfigError(f'集群清单不存在：{path}')
  try:
    raw = json.loads(path.read_text(encoding='utf-8'))
  except json.JSONDecodeError as e:
    raise ClusterConfigError(f'集群清单不是合法 JSON（{path}）：{e}') from e
  if not isinstance(raw, list) or not raw:
    raise ClusterConfigError(f'集群清单必须是非空数组：{path}')

  out: list[ClusterSpec] = []
  seen: set[str] = set()
  seen_codes: dict[str, str] = {}
  for i, item in enumerate(raw):
    if not isinstance(item, dict):
      raise ClusterConfigError(f'集群清单第 {i + 1} 项不是对象')
    name = str(item.get('cluster', '')).strip()
    bootstrap = str(item.get('bootstrap', '')).strip()
    if not name or not bootstrap:
      raise ClusterConfigError(f'集群清单第 {i + 1} 项缺少 cluster 或 bootstrap')
    if name in seen:
      raise ClusterConfigError(f'集群清单里园区名重复：{name}')
    seen.add(name)
    try:
      code = park_codes.code_for(name)
    except KeyError:
      raise ClusterConfigError(
        f'集群清单第 {i + 1} 项（{name}）没有登记园区代号 —— '
        '在 service/dcim_ingest/park_codes.py 的 CLUSTER_CODES 里补上'
      ) from None
    if code in seen_codes:
      raise ClusterConfigError(
        f'园区代号重复：{name} 与 {seen_codes[code]} 都是 {code}'
      )
    seen_codes[code] = name
    try:
      concurrency = int(item.get('concurrency', 0) or 0)
    except (TypeError, ValueError):
      raise ClusterConfigError(
        f'集群清单第 {i + 1} 项（{name}）的 concurrency 不是整数'
      ) from None
    if concurrency < 0:
      raise ClusterConfigError(
        f'集群清单第 {i + 1} 项（{name}）的 concurrency 不能为负'
      )
    out.append(ClusterSpec(
      cluster=name, bootstrap=bootstrap,
      broker=str(item.get('broker', '')), code=code,
      concurrency=concurrency,
    ))
  return out


def realtime_workers(cluster: ClusterSpec) -> int:
  """该园区实际起多少条实时消费线程：园区覆盖优先，0 = 全局默认（至少 1 条）。"""
  return max(1, cluster.concurrency or int(settings.DCIM_REALTIME_CONCURRENCY))


def consumer_config(
  cluster: ClusterSpec,
  *,
  group_id: str | None = None,
  auto_commit: bool = False,
) -> dict[str, Any]:
  """单个集群的消费者配置。

  - 提交方式：默认**手动**（写库成功后再提交，保证 at-least-once）。
    实时数据**也用手动**（2026-11-02 改）：原先自动提交 + 写侧吞异常 = 写失败的采样永久丢失；
    现在失败整批退避重试，重放按 `(子表, ts)` 覆盖、幂等。`auto_commit=True` 只留给
    「本来就允许丢」的临时调试场景
  - 消费组：默认 `KAFKA_GROUP_ID`（配置类）。**实时数据必须用独立的组**：
    同组内不同 topic 的成员会一起 rebalance，配置类线程重启会把实时消费也带停
  - 起始位置：earliest（历史里有首次全量推送，不能漏）
  - 认证：KAFKA_SECURITY_PROTOCOL 以 SASL 开头时带 SASL/PLAIN 凭据
    （broker 与 topic 由 DCIM 提供，账号口令由对方下发，配置见 deploy/config/.env.production）
  """
  conf: dict[str, Any] = {
    'bootstrap.servers': cluster.bootstrap,
    'group.id': group_id or settings.KAFKA_GROUP_ID,
    'enable.auto.commit': auto_commit,
    'auto.offset.reset': 'earliest',
    'enable.partition.eof': False,
  }
  if settings.KAFKA_SECURITY_PROTOCOL.upper().startswith('SASL'):
    conf.update({
      'security.protocol': settings.KAFKA_SECURITY_PROTOCOL,
      'sasl.mechanism': settings.KAFKA_SASL_MECHANISM,
      'sasl.username': settings.KAFKA_SASL_USERNAME,
      'sasl.password': settings.KAFKA_SASL_PASSWORD,
    })
  return conf


def topics() -> list[tuple[str, str]]:
  """[(kind, topic)]：kind 决定解析器与落库表（见 models.PARSERS/TABLES）。"""
  return [
    ('space', settings.KAFKA_TOPIC_SPACE),
    ('device', settings.KAFKA_TOPIC_DEVICE),
    ('point', settings.KAFKA_TOPIC_POINT),
  ]


def shard_slice(items: list, shard: int, shards: int) -> list:
  """把工作线程清单按**扁平序号轮询**分给各进程（纯函数，便于自检）。

  为什么是轮询而不是按园区切：廊坊A 一个园区就占了绝大部分流量，按园区切会让它的
  实时线程整体落进同一个进程 —— 那个进程仍是单 GIL，扩进程等于没扩。轮询能保证
  「同一园区的多条线程分散到尽可能多的进程」（`shards <= 1` 时原样返回）。
  """
  if shards <= 1:
    return list(items)
  return [item for i, item in enumerate(items) if i % shards == shard]

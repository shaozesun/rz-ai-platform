"""DCIM 配置数据的 Kafka 消费者（每个 topic 一个线程）。

语义与取舍（详见 docs/dcim-kafka-ingest-design.md 第三节）：
- 同一消费组 rz-dcim-ingest，`enable.auto.commit=False`：**写库成功后才提交 offset**（at-least-once）
- 幂等：当前态按 guid upsert；重复消费不产生重复行
- 单条解析失败 → 写 dcim_dlq 并继续，不阻塞批次
- 写库失败 → 保留批次退避重试（不提交 offset），超次数后退出线程并报错
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from confluent_kafka import Consumer, KafkaException

from config.settings import settings
from service.dcim_ingest.models import (
  PARSERS, PueSample, SkippedRecord, is_pue_message, parse_device_samples,
  parse_pue_samples, pue_stats, ts_stats,
)
from service.dcim_ingest.repository import DcimRepository

logger = logging.getLogger(__name__)

_LAG_LOG_INTERVAL = 30.0     # 秒：周期性打印消费 lag
_DB_MAX_RETRY = 5            # 写库失败的最大重试次数（之后退出线程）
_RECONNECT_MAX_WAIT = 60.0   # 秒：断线重连的退避上限（2^N 递增，封顶）

# 心跳文件：供容器 healthcheck 判断"进程是否还在正常轮转"。
# 与 deploy/docker-compose.dcim-ingest.yaml 的 healthcheck 约定同一路径。
_HEARTBEAT_FILE = Path('/tmp/dcim-ingest.alive')


def _beat() -> None:
  """刷新心跳（33 条线程任一在轮转都会刷）。失败无妨，纯观测用途。"""
  try:
    _HEARTBEAT_FILE.touch()
  except OSError:
    pass


class TopicConsumer(threading.Thread):
  """消费某个集群的单个 topic → 解析 → 批量落库（当前态 + 历史）→ 提交 offset。

  「集群 × topic」是线程的粒度：13 个园区 × 6 个 topic（配置 3 / 实时 / 告警 / 告警工单）
  = 78 条线程（每个 topic 有各自开关，实际数量随配置变化）。
  """

  def __init__(
    self,
    *,
    cluster: str,
    kind: str,
    topic: str,
    consumer_config: dict[str, Any],
    dsn: str,
    batch_size: int,
    stop_event: threading.Event,
    index: int | None = None,
    cluster_code: str = '',
  ) -> None:
    # index：同园区同 topic 起多条线程时用来区分（实时消费并行化）。
    # 默认 None → 线程名与日志标签**与原来逐字相同**，配置类/告警类不受影响。
    suffix = '' if index is None else f'-{index}'
    super().__init__(name=f'dcim-{cluster}-{kind}{suffix}', daemon=True)
    self.cluster = cluster
    # 园区代号：进入 TDengine 子表名（`d_<代号>_<guid>`），配置类线程不用、默认空串
    self.cluster_code = cluster_code
    self.kind = kind
    self.index = index
    self.topic = topic
    self._consumer_config = consumer_config
    self._dsn = dsn
    self._batch_size = batch_size
    # 注意：不能命名为 self._stop —— 会覆盖 threading.Thread._stop() 内部方法，
    # 退出 join 时抛 TypeError: 'Event' object is not callable
    self._stop_event = stop_event
    self.total = 0          # 已落库条数（含 upsert/delete）
    self.dlq = 0            # 进 DLQ 的条数
    self.skipped = 0        # 主动丢弃的条数（见 SkippedRecord；**不进 DLQ**）

  @property
  def label(self) -> str:
    """日志标签：园区 + 实体类型（实时并行后带 `#序号`，其余线程不变）。"""
    if self.index is None:
      return f'{self.cluster}/{self.kind}'
    return f'{self.cluster}/{self.kind}#{self.index}'

  # ── 三个可覆盖的口子：配置类与实时数据的差异都收在这里 ──
  def _make_store(self):
    """落库对象。默认 PostgreSQL（配置类）；实时数据由子类换成 TDengine。"""
    return DcimRepository(self._dsn)

  def _ingest(self, raw: dict, meta: dict[str, Any], buf: list) -> None:
    """把一条报文解析进缓冲。默认：一消息一实体（配置类）。"""
    buf.append((PARSERS[self.kind](raw), meta))

  def _record_dlq(self, store, meta: dict[str, Any], raw_text: str, e: Exception) -> None:
    """解析失败的落点。默认写 PG 的 dcim_dlq；实时数据只记日志（它的库没有 DLQ 表）。"""
    store.record_dlq(
      cluster=self.cluster,
      topic=meta['topic'], partition=meta['partition'], offset=meta['offset'],
      error=f'{type(e).__name__}: {e}',
      raw=raw_text,
    )
    logger.warning('[%s] 解析失败已入 DLQ offset=%s: %s', self.label, meta['offset'], e)

  def _progress_extra(self) -> str:
    """lag 日志行尾的附加信息（子类可覆盖，如实时数据的空包计数）。"""
    # 「主动丢弃」必须在日志里看得见 —— 否则丢数据是静默的，出问题无从追溯
    return f' 跳过={self.skipped}' if self.skipped else ''

  # ── 主循环：断线自动重连，线程不会因一次异常永久退出 ──
  def run(self) -> None:
    """消费主循环：连不上或中途断开都**退避重连**，直到收到停止信号。

    为什么必须重连：现在有 33 条线程（11 园区 × 3 topic）跑在 11 个远端集群上，链路中间
    还有单点中转。任何一次抖动若让线程永久退出，进程仍然活着，只表现为「某个园区的数据
    不再更新」，**只能翻日志才发现**。

    offset 语义不受影响：`enable.auto.commit=False`，写库成功才提交 —— 重连后从已提交位置
    继续；当前态按 (cluster, guid) upsert 幂等，历史表重复追加属预期。
    """
    attempt = 0
    while not self._stop_event.is_set():
      repo = self._make_store()          # 配置类 → PG；实时 → TDengine（见 RealtimeConsumer）
      consumer = Consumer(self._consumer_config)
      try:
        repo.connect()
        consumer.subscribe([self.topic])
        if attempt:
          logger.info('[%s] 已重连成功（此前连续失败 %d 次）', self.label, attempt)
        attempt = 0          # 连上就重置退避
        logger.info('[%s] 开始消费 topic=%s', self.label, self.topic)
        self._loop(repo, consumer)
      except Exception as e:  # noqa: BLE001 —— 任何异常都要重连，不能打死线程
        attempt += 1
        # 首次失败打完整堆栈便于定位；持续失败只留一行，避免 33 条线程刷屏
        logger.warning(
          '[%s] 消费中断（第 %d 次）：%s: %s',
          self.label, attempt, type(e).__name__, e, exc_info=(attempt == 1),
        )
      finally:
        try:
          consumer.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不该掩盖上面的原因
          logger.debug('[%s] consumer.close 异常', self.label, exc_info=True)
        repo.close()

      if self._stop_event.is_set():
        break
      if attempt:
        wait = min(2.0 ** attempt, _RECONNECT_MAX_WAIT)
        logger.warning('[%s] 连续失败 %d 次，%.0f 秒后重连', self.label, attempt, wait)
        # 用 wait 而非 sleep：收到停止信号能立刻退出，不卡在退避里
        if self._stop_event.wait(wait):
          break
    logger.info('[%s] 已退出（累计落库 %d 条，DLQ %d 条）', self.label, self.total, self.dlq)

  def _loop(self, repo: DcimRepository, consumer: Consumer) -> None:
    buf: list[tuple[Any, dict[str, Any]]] = []
    last_lag_log = time.time()

    while not self._stop_event.is_set():
      msgs = consumer.consume(num_messages=self._batch_size, timeout=1.0)
      for m in msgs:
        if m.error():
          logger.warning('[%s] 消费错误: %s', self.label, m.error())
          continue
        # cluster 是「消息来自哪个集群」，只有消费端知道，解析层不涉及
        meta = {
          'cluster': self.cluster,
          'topic': m.topic(), 'partition': m.partition(), 'offset': m.offset(),
        }
        try:
          raw: dict = json.loads(m.value())
          self._ingest(raw, meta, buf)
        except SkippedRecord as e:
          # 已知无用的噪声（如 guid 前缀被截断的空壳空间）：**只计数、不入 DLQ** ——
          # 配置类 topic 是周期性推送，入 DLQ 会把表刷爆。计数会显示在 lag 日志的
          # 「跳过=N」里，保证「主动丢数据」有迹可查。
          self.skipped += 1
          logger.debug('[%s] 跳过 offset=%s: %s', self.label, meta['offset'], e)
        except Exception as e:  # 解析失败 → DLQ，不阻塞后续
          self.dlq += 1
          self._record_dlq(repo, meta, _safe_text(m.value()), e)

      if buf:
        self._flush(repo, consumer, buf)

      if time.time() - last_lag_log >= _LAG_LOG_INTERVAL:
        last_lag_log = time.time()
        logger.info(
          '[%s] lag=%s 累计=%d DLQ=%d%s',
          self.label, _lag(consumer), self.total, self.dlq, self._progress_extra(),
        )
        _beat()   # 心跳：容器 healthcheck 据此判断线程是否还在轮转

  def _flush(self, repo: DcimRepository, consumer: Consumer, buf: list) -> None:
    """写库成功后提交 offset；失败则退避重试（不提交）。"""
    t0 = time.time()
    for attempt in range(1, _DB_MAX_RETRY + 1):
      try:
        repo.apply_batch(self.kind, buf)
        break
      except Exception as e:
        logger.warning('[%s] 写库失败(%d/%d): %s', self.label, attempt, _DB_MAX_RETRY, e)
        if attempt >= _DB_MAX_RETRY:
          raise
        time.sleep(2 * attempt)
    consumer.commit(asynchronous=False)
    self.total += len(buf)
    logger.info(
      '[%s] 落库 %d 条 用时 %.2fs（累计 %d）',
      self.label, len(buf), time.time() - t0, self.total,
    )
    buf.clear()


def _lag(consumer: Consumer) -> int | str:
  """当前消费组在本 topic 上的 lag 合计（取不到返回 '-'）。"""
  try:
    total = 0
    for tp in consumer.assignment():
      _low, high = consumer.get_watermark_offsets(tp, timeout=1.0, cached=False)
      pos = consumer.position([tp])[0].offset
      if pos >= 0 and high >= 0:
        total += max(0, high - pos)
    return total
  except (KafkaException, RuntimeError):
    return '-'


def _safe_text(value: bytes | None) -> str:
  if value is None:
    return ''
  return value.decode('utf-8', 'replace')


class _RealtimeStore:
  """实时线程的落库门面：设备宽表（`WideWriter`）+ PUE 统计（`PueWriter`）。

  **为什么合成一个对象**：`TopicConsumer._loop` 只有一条 `buf`、一个 `_flush`。两种报文
  混在同一个 topic 里，若各用各的缓冲，「一批只有 PUE」就永远凑不出 `if buf:` 的触发点；
  offset 提交语义也必须由一处统一控制 —— **两者都写成功才提交**。

  **共用同一对 PG / TDengine 连接**（同一个 `RealtimeConsumer` 线程内串行使用）：每批 PUE
  通常只有一两行、写的是同一套库，各开一组连接会让每条实时线程的 PG 连接数翻倍。
  因此 `connect`/`close` 只经由 `WideWriter` —— `PueWriter` 自带的同名方法在这个组合里不调，
  否则会把连接重开一遍（泄漏旧连接）或提前关掉。
  """

  def __init__(self, wide: Any, pue: Any) -> None:
    self._wide = wide
    self._pue = pue

  def connect(self) -> None:
    self._wide.connect()

  def close(self) -> None:
    self._wide.close()

  def write_devices(self, samples: list) -> None:
    """写设备宽表（`WideWriter.write`）。"""
    self._wide.write(samples)

  def write_pue(self, samples: list) -> int:
    """写 PUE 超级表，返回实际写入的行数。"""
    return self._pue.write(samples)

  @property
  def pue_statements(self) -> int:
    return self._pue.statements

  @property
  def unknown_buildings(self) -> int:
    return self._pue.unknown_buildings

  def __getattr__(self, name: str):
    """`unknown_devices` / `statements` / `merge_failures` 等既有计数器透给 `WideWriter`
    —— `_progress_extra` 里那几行 `getattr(store, …)` 不用改。"""
    if name.startswith('_'):
      raise AttributeError(name)
    return getattr(self._wide, name)


class RealtimeConsumer(TopicConsumer):
  """实时数据消费者（`ems-real-data-rz` → TDengine **宽表**）。

  和配置类**语义相反**，四处不同：

  1. **落库目标是 TDengine**（点位数值时序），不是 PG
  2. **解析是「一消息 = 一台设备的一组测点值」**，在宽表里就是**一行**（同一行的各列天然同刻）
  3. **写入要查 PG 设备档案**：报文里只有 `device_id`，而宽表按 `device_type` 选超级表 ——
     所以 `dsn` 现在是**必填**的（走 `WideWriter` 协调 PG 与 TDengine）
  4. **写库成功后才手动提交 offset**（与配置类一致）。原先这里自动提交，而 `WideWriter`
     又把异常全吞掉 ⇒ 写失败的数据**永久丢失**且无重试。现在失败会抛出、整批退避重试，
     offset 不提交，重连后重放（按 `(子表, ts)` 覆盖，幂等）。
  """

  # 空包计数：`points` 为空数组的报文（对方设备这一刻没有有效测点，实测约 4%）。
  # 照常跳过，但要能在 lag 日志里看见 —— 否则「收到很多包却落库 0 条」无从判断。
  empty_packets = 0
  # PUE 报文解析出 0 行（未知编码 / 坏时间戳全被跳过）：与设备空包分开计
  pue_empty = 0

  def _make_store(self):
    from service.dcim_ingest.realtime import RealtimeRepository
    from service.dcim_ingest.repository import DcimRepository
    from service.dcim_ingest.wide_writer import WideWriter
    from service.dcim_ingest.pue import PueWriter
    sinks = []
    if settings.DCIM_ARCHIVE_ENABLED:
      # 归档 sink：全量 Parquet → MinIO（兼作热层备份），见 archive.py
      from service.dcim_ingest.archive import ArchiveSink
      sinks.append(ArchiveSink())
    # PG / TDengine 连接在这一层各开一组，两个 writer 共用（见 `_RealtimeStore`）
    pg, td = DcimRepository(self._dsn), RealtimeRepository()
    return _RealtimeStore(
      WideWriter(
        pg, td, cluster=self.cluster, cluster_code=self.cluster_code,
        extra_sinks=sinks,
      ),
      PueWriter(
        pg, td, cluster=self.cluster, cluster_code=self.cluster_code,
      ),
    )

  def _ingest(self, raw: dict[str, Any], meta: dict[str, Any], buf: list) -> None:
    # 两种报文混在同一个 topic：「无 device_id + 有 building」的是 PUE / 能耗统计报文。
    # **两者塞进同一个 `buf`**：解析成空的那批不会让 `_flush` 空转，
    # 分流在 `_flush` 里按样本类型做。
    if is_pue_message(raw):
      samples = parse_pue_samples(raw)
      if not samples:
        self.pue_empty += 1
      buf.extend(samples)
      return
    samples = parse_device_samples(raw)
    if not samples:
      self.empty_packets += 1
    buf.extend(samples)

  def _progress_extra(self) -> str:
    store = getattr(self, '_store_for_log', None)
    unknown = getattr(store, 'unknown_devices', 0)
    failed = getattr(store, 'failed_devices', 0)
    statements = getattr(store, 'statements', 0)
    merged = getattr(store, 'merge_failures', 0)
    remapped = getattr(store, 'subtable_remaps', 0)
    unknown_buildings = getattr(store, 'unknown_buildings', 0)
    pue_statements = getattr(store, 'pue_statements', 0)
    extra = f' 空包={self.empty_packets}'
    ts = ts_stats()
    if ts.get('bad'):
      extra += f" 坏时间戳={ts['bad']}"
    if ts.get('seconds'):
      extra += f" 秒级时间={ts['seconds']}"
    if unknown:
      extra += f' 档案缺失={unknown}'
    if failed:
      extra += f' 写失败设备={failed}'
    if statements:
      extra += f' 语句={statements}'
    if merged:
      extra += f' 合并失败={merged}'
    if remapped:
      extra += f' 子表换名={remapped}'
    pue = pue_stats()
    if self.pue_empty:
      extra += f' PUE空包={self.pue_empty}'
    if pue.get('unknown_code'):
      extra += f" PUE未知编码={pue['unknown_code']}"
    if pue.get('bad_ts'):
      extra += f" PUE坏时间戳={pue['bad_ts']}"
    if pue.get('ts_fallback'):
      extra += f" PUE时间戳回退={pue['ts_fallback']}"
    if pue.get('no_building'):
      extra += f" PUE楼宇缺失={pue['no_building']}"
    if unknown_buildings:
      extra += f' PUE未知楼宇={unknown_buildings}'
    if pue_statements:
      extra += f' PUE语句={pue_statements}'
    return extra

  def _record_dlq(self, store, meta: dict[str, Any], raw_text: str, e: Exception) -> None:
    # 实时数据的库（TDengine）没有 DLQ 表；整包坏了只记日志，不落库
    logger.warning('[%s] 整包解析失败已跳过 offset=%s: %s', self.label, meta['offset'], e)

  def _flush(self, store, consumer: Consumer, buf: list) -> None:
    """写 TDengine（宽表 + PUE）；**两者都成功才提交 offset**，任一失败整批退避重试（不提交）。"""
    t0 = time.time()
    self._store_for_log = store        # 供 lag 日志读「档案缺失/语句/换名」计数
    devices = [s for s in buf if not isinstance(s, PueSample)]
    pues = [s for s in buf if isinstance(s, PueSample)]
    pue_rows = 0
    for attempt in range(1, _DB_MAX_RETRY + 1):
      try:
        if devices:
          store.write_devices(devices)
        # PUE 只在有样本时才建表/下发；空列表直接跳过，不浪费一次 ensure_schema
        pue_rows = store.write_pue(pues) if pues else 0
        break
      except Exception as e:
        logger.warning('[%s] 写 TDengine 失败(%d/%d): %s', self.label, attempt, _DB_MAX_RETRY, e)
        if attempt >= _DB_MAX_RETRY:
          raise
        time.sleep(2 * attempt)
    consumer.commit(asynchronous=False)
    self.total += len(buf)
    if pues:
      logger.info(
        '[%s] 落库 %d 行（设备×时刻）+ PUE %d 行 用时 %.2fs（累计 %d）',
        self.label, len(devices), pue_rows, time.time() - t0, self.total,
      )
    else:
      logger.info(
        '[%s] 落库 %d 行（设备×时刻）用时 %.2fs（累计 %d）',
        self.label, len(devices), time.time() - t0, self.total,
      )
    buf.clear()

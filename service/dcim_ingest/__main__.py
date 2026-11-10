"""DCIM Kafka 接入 worker 入口。

    python -m service.dcim_ingest

独立进程运行（与 web 同镜像、不同 command）；只通过 PostgreSQL 与 web 交互。
启动前请确认：
1. `deploy/config/.env.production` 打开 `DCIM_KAFKA_ENABLED=true`，并配好 `DCIM_PG_DSN`
   与 `KAFKA_*`（对方 broker 的地址/协议/SASL 账号）；
2. 各 topic（配置 ×3、实时、告警、告警工单）已由 DCIM 建好且我方账号有读权限
   （本进程**不建 topic**；用 `deploy/scripts/dcim_topic_status.py` 核对）；
3. 元数据库表由本进程启动时幂等自动创建，无需手工执行 `deploy/sql/dcim_schema.sql`。

**多进程（`DCIM_INGEST_PROCESSES`，默认 1 = 旧行为）**：实时接入是**单进程 GIL 饱和**
（实测约 6.5k 行/s ≈ 1 核，`top -H` 几个 Python 线程合计 ≈100%；加线程/加下发并发都不涨）。
>1 时 fork 出 N 个子进程，工作线程按「扁平序号 % N」均分 —— **每个进程一个 GIL**。
线程总数与 PG 连接数**不变**，只增加内存。
"""

from __future__ import annotations

import logging
import multiprocessing
import signal
import threading
import time

from config.settings import settings
from service.dcim_ingest.config import (
  ClusterConfigError, clusters, consumer_config, realtime_workers, shard_slice, topics,
)
from service.dcim_ingest.consumers import RealtimeConsumer, TopicConsumer

logger = logging.getLogger(__name__)


def _setup_logging(processes: int = 1) -> None:
  """多进程时在日志里带上 pid —— 否则各子进程的线程名（含 `#序号`）会撞在一起。"""
  fmt = '%(asctime)s | %(levelname)-5s | '
  if processes > 1:
    fmt += '%(process)d | '
  fmt += '%(threadName)s | %(name)s | %(message)s'
  logging.basicConfig(level=logging.INFO, format=fmt)


def _build_workers(
  cluster_list, stop: threading.Event, *, shard: int = 0, shards: int = 1,
) -> list[threading.Thread]:
  """按配置构造全部工作线程，再按扁平序号轮询取出**本进程**那一份。"""
  confs = {c.cluster: consumer_config(c) for c in cluster_list}
  workers: list[threading.Thread] = [
    TopicConsumer(
      cluster=c.cluster,
      kind=kind,
      topic=topic,
      consumer_config=confs[c.cluster],
      dsn=settings.DCIM_PG_DSN,
      batch_size=settings.KAFKA_BATCH_SIZE,
      stop_event=stop,
    )
    for c in cluster_list
    for kind, topic in topics()
  ]

  # 实时数据（阶段二）：每个园区加 N 条线程 → TDengine。
  # 用**独立消费组**（不与配置类互相 rebalance），**写库成功才手动提交**（见 RealtimeConsumer）。
  # N 条线程同属一个消费组 → Kafka 按分区自动分摊（16 分区 / N 条）。
  # N 默认取全局 `DCIM_REALTIME_CONCURRENCY`，集群清单里可用 `concurrency` 按园区覆盖
  # （廊坊A 16 个分区 → 16 条；只给大流量园区加，别全局铺）。
  if settings.DCIM_REALTIME_ENABLED:
    real_confs = {
      c.cluster: consumer_config(
        c, group_id=settings.KAFKA_REAL_DATA_GROUP_ID,   # 手动提交（默认）
      )
      for c in cluster_list
    }
    workers += [
      RealtimeConsumer(
        cluster=c.cluster,
        kind='real',
        topic=settings.KAFKA_TOPIC_REAL_DATA,
        consumer_config=real_confs[c.cluster],
        # 数值落 TDengine，但**要查 PG 的设备档案**才能按 device_type 选超级表
        # （实时报文里没有 device_type）—— 所以这里必须给 PG DSN
        dsn=settings.DCIM_PG_DSN,
        batch_size=settings.KAFKA_BATCH_SIZE,
        stop_event=stop,
        index=i,
        cluster_code=c.code,   # 子表名 `d_<园区代号>_<guid>`，见 park_codes.py
      )
      for c in cluster_list
      for i in range(realtime_workers(c))
    ]

  # 告警事件：每个园区再加一条线程 → PG（**不进 TDengine** —— 它是事件、不是时序，
  # 且 point_guid/device_guid 就是 PG 里的 guid，同库可直接 JOIN）。
  # 用普通 TopicConsumer（落 PG 的路径和配置类完全一样：upsert 当前态 + 追历史），
  # 但**独立消费组**（理由同实时数据：和配置类同组会被它的重启 rebalance 带停）。
  if settings.DCIM_ALARM_ENABLED:
    alarm_confs = {
      c.cluster: consumer_config(c, group_id=settings.KAFKA_ALARM_GROUP_ID)
      for c in cluster_list
    }
    workers += [
      TopicConsumer(
        cluster=c.cluster,
        kind='alarm',
        topic=settings.KAFKA_TOPIC_ALARM,
        consumer_config=alarm_confs[c.cluster],
        dsn=settings.DCIM_PG_DSN,
        batch_size=settings.KAFKA_BATCH_SIZE,
        stop_event=stop,
      )
      for c in cluster_list
    ]

  # 告警工单：人工处理告警时推送（同一 orderNo 多次推送 → upsert 成最新状态）。
  # 同样用独立消费组，理由同上。
  if settings.DCIM_ALARM_ORDER_ENABLED:
    order_confs = {
      c.cluster: consumer_config(c, group_id=settings.KAFKA_ALARM_ORDER_GROUP_ID)
      for c in cluster_list
    }
    workers += [
      TopicConsumer(
        cluster=c.cluster,
        kind='alarm_order',
        topic=settings.KAFKA_TOPIC_ALARM_ORDER,
        consumer_config=order_confs[c.cluster],
        dsn=settings.DCIM_PG_DSN,
        batch_size=settings.KAFKA_BATCH_SIZE,
        stop_event=stop,
      )
      for c in cluster_list
    ]

  picked = shard_slice(workers, shard, shards)
  logger.info(
    '启动 DCIM 接入 worker（分片 %d/%d）：集群 %d 个 %s；配置类 topic %s（group=%s，手动提交）'
    ' + 实时 topic %s（enabled=%s，group=%s，手动提交，线程数 %s）'
    ' + 告警 topic %s（enabled=%s，group=%s，手动提交）'
    ' + 告警工单 topic %s（enabled=%s，group=%s，手动提交）；本进程 %d/%d 条线程，batch=%d',
    shard + 1, shards,
    len(cluster_list), [c.cluster for c in cluster_list],
    [t for _, t in topics()], settings.KAFKA_GROUP_ID,
    settings.KAFKA_TOPIC_REAL_DATA, settings.DCIM_REALTIME_ENABLED,
    settings.KAFKA_REAL_DATA_GROUP_ID,
    {c.cluster: realtime_workers(c) for c in cluster_list},
    settings.KAFKA_TOPIC_ALARM, settings.DCIM_ALARM_ENABLED,
    settings.KAFKA_ALARM_GROUP_ID,
    settings.KAFKA_TOPIC_ALARM_ORDER, settings.DCIM_ALARM_ORDER_ENABLED,
    settings.KAFKA_ALARM_ORDER_GROUP_ID,
    len(picked), len(workers), settings.KAFKA_BATCH_SIZE,
  )
  return picked


def _serve(shard: int = 0, shards: int = 1) -> int:
  """在**当前进程**里起分到的工作线程，直到收到 SIGINT/SIGTERM。"""
  stop = threading.Event()

  def _shutdown(signum, _frame):  # noqa: ANN001 - signal handler
    logger.info('收到信号 %s，准备退出…', signum)
    stop.set()

  signal.signal(signal.SIGINT, _shutdown)
  signal.signal(signal.SIGTERM, _shutdown)

  # 集群 × topic 双层展开：每个园区各起一组配置类消费者（11 园区 × 3 topic）
  try:
    cluster_list = clusters()
  except ClusterConfigError as e:
    logger.error('集群清单不可用，无法继续：%s', e)
    return 1

  workers = _build_workers(cluster_list, stop, shard=shard, shards=shards)
  for w in workers:
    w.start()

  try:
    while not stop.is_set():
      time.sleep(1)
  finally:
    for w in workers:
      w.join(timeout=10)
    logger.info('worker 已停止')
  return 0


def _child_main(shard: int, shards: int) -> None:
  """子进程入口（spawn 模式下必须模块级、可 pickle）。退出码经 SystemExit 传给父进程。"""
  _setup_logging(shards)
  try:
    code = _serve(shard, shards)
  except Exception:                          # noqa: BLE001 —— 子进程任何异常都要变成非零退出码
    logger.exception('子进程 %d 异常退出', shard)
    code = 1
  raise SystemExit(code)


def _run_processes(processes: int, cluster_list) -> int:
  """起 N 个子进程并看护；任一异常退出就停掉其余进程并返回非零（交给容器重启）。"""
  ctx = multiprocessing.get_context()   # Linux=fork（省内存、秒起）；macOS=spawn
  if settings.DCIM_REALTIME_ENABLED:
    min_threads = min(realtime_workers(c) for c in cluster_list)
    if min_threads < processes:
      logger.warning(
        '最小园区实时线程数 %d < 进程数 %d：部分进程分不到实时线程，'
        '建议把该园区的 concurrency 或全局 DCIM_REALTIME_CONCURRENCY 调到 ≥ 进程数',
        min_threads, processes,
      )

  children = []
  for i in range(processes):
    p = ctx.Process(target=_child_main, args=(i, processes), name=f'dcim-ingest-{i}')
    p.start()
    children.append(p)
    logger.info('子进程 %d/%d 已启动 pid=%s', i + 1, processes, p.pid)

  stopping = threading.Event()

  def _shutdown(signum, _frame):  # noqa: ANN001 - signal handler
    logger.info('收到信号 %s，通知 %d 个子进程退出…', signum, len(children))
    stopping.set()

  signal.signal(signal.SIGINT, _shutdown)
  signal.signal(signal.SIGTERM, _shutdown)

  code = 0
  try:
    while not stopping.is_set():
      dead = [p for p in children if not p.is_alive()]
      if dead:
        # 子进程只在收到停止信号时才应退出；此时循环已经先因 stopping 结束。
        logger.error(
          '子进程异常退出：%s，停止其余子进程（容器将整体重启）',
          [(p.pid, p.exitcode) for p in dead],
        )
        code = 1
        break
      time.sleep(1)
  finally:
    for p in children:
      if p.is_alive():
        p.terminate()          # SIGTERM → 子进程自己优雅停线程
    for p in children:
      p.join(timeout=20)
    for p in children:
      if p.is_alive():
        logger.warning('子进程 %s 未在 20s 内退出，强制结束', p.pid)
        p.kill()
        p.join(timeout=5)
  return code


def run() -> int:
  """启动各 topic 的消费者，直到收到 SIGINT/SIGTERM。"""
  processes = max(1, int(settings.DCIM_INGEST_PROCESSES))
  _setup_logging(processes)

  if not settings.DCIM_KAFKA_ENABLED:
    logger.error('DCIM_KAFKA_ENABLED=false，未启用；请在 .env 打开后重试')
    return 1
  if not settings.DCIM_PG_DSN:
    logger.error('DCIM_PG_DSN 未配置（示例：postgresql://user:pwd@localhost:5432/dcim）')
    return 1

  # 自动建表（幂等）：免去手工执行 deploy/sql/dcim_schema.sql 的步骤
  try:
    from service.dcim_ingest.schema import ensure_schema
    ensure_schema(settings.DCIM_PG_DSN)
    logger.info('元数据表结构已就绪（幂等建表完成）')
  except Exception as e:
    logger.error('建表失败，无法继续（检查 DCIM_PG_DSN 与库权限）: %s', e)
    return 1

  # 先校验集群清单与 TDengine 库，再决定是否 fork —— 免得 N 个子进程各自报同一个错
  try:
    cluster_list = clusters()
  except ClusterConfigError as e:
    logger.error('集群清单不可用，无法继续：%s', e)
    return 1

  if settings.DCIM_REALTIME_ENABLED:
    # 建库要在**起线程/子进程之前做一次**：并发 CREATE 会互相撞
    # （TDengine 报 Conflict transaction not completed）
    try:
      from service.dcim_ingest.realtime import ensure_realtime_schema
      ensure_realtime_schema()
      # 设备宽表的超级表**不在这里建**：宽模型下按设备类型分表（st_<device_type>），
      # 随数据到达按需建（见 RealtimeRepository.ensure_stable）；
      # `st_pue` 是固定一张，在 ensure_realtime_schema 里一并建好（避免多进程并发建表）
      logger.info('TDengine 库已就绪（%s；设备超级表按类型按需创建，st_pue 已建）', settings.TAOS_DB)
    except Exception as e:
      logger.error('TDengine 建库建表失败，实时数据将无法落库：%s', e)
      return 1

  if processes == 1:
    return _serve(0, 1)
  return _run_processes(processes, cluster_list)


if __name__ == '__main__':
  raise SystemExit(run())

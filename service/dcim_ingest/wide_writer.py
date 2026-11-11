"""把实时数据写进 TDengine 宽表 —— 把 PG 的建模信息与 TDengine 的写入缝在一起。

分工：

  - `repository.DcimRepository`     → **PG**：设备档案、列映射注册表
  - `realtime.RealtimeRepository`   → **TDengine**：建库建表、执行 SQL
  - 本模块                          → 协调：查档案 → 定超级表与列 → 确保表结构 → 拼 INSERT 写

**为什么单独一层**：实时报文里**没有 `device_type`**（只有 `device_id`/`idc`/`points`），
而宽表要按设备类型选超级表 —— 这个「查设备档案」的动作必须有地方做。

**设备档案没到时**：不写那台设备、计数、**不阻塞**其它设备。配置类是全量先推、实时后推，
正常情况下档案已经在库里；个别缺失也能靠 topic 保留期重放补上。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from config.settings import settings
from service.dcim_ingest.models import DeviceSample
from service.dcim_ingest.realtime import (
  RealtimeRepository, build_insert_body, build_multi_device_insert, tag_values,
)
from service.dcim_ingest.repository import DcimRepository
from service.dcim_ingest.td_schema import stable_name, subtable_name, subtable_variant

logger = logging.getLogger(__name__)

# 设备档案"查不到"的负缓存 TTL：到期重查 —— 档案可能"先缺后补"（配置类 topic 晚于实时数据到达）
_MISS_TTL = 300.0

# 一条 INSERT 里最多塞几台设备。一条语句 = 一次 HTTP 往返，所以要合；
# 但也不能无限大（语句长度、失败重试成本）—— 与旧窄表写入器同样是 200。
_DEVICES_PER_INSERT = 200

# 「设备级」错误：重试多少次都不会成功，只能跳过/换名 —— 与瞬时故障（超时、5xx）分开处理。
# 判据是**子串**：TDengine REST 把错误塞在 `desc` 里，写法各异（大小写、前缀），
# 匹配全文比解析结构稳。新增已知的"这设备就是写不进去"的错时加在这里。
_DEVICE_ERROR_MARKERS = (
  'already exists in other stables',
  'invalid table name',
  'too many columns',
  'invalid tag',
  # 时间戳早于库保留期（KEEP）：合并写时**整条语句**被拒，必须退到逐台才能定位到坏设备。
  # 解析层已做校验，这里是兜底 —— 绕过解析的写入（如归档回放）也会遇到。
  'timestamp data out of range',
)


# 逐台回退后判定「系统性故障」所需的最小批大小（见 `_exec_chunk`）。
_SKIP_RATIO_MIN_DEVICES = 5


def _is_device_error(exc: BaseException) -> bool:
  """这个异常是「该设备本身写不进去」还是「暂时写不进去」。"""
  text = str(exc).lower()
  return any(marker in text for marker in _DEVICE_ERROR_MARKERS)


# 一条 INSERT 语句的**字符数上限**（比台数更硬的约束）。
# TDengine 默认 `maxSQLLength` = 1MB，超了整条语句报 `537 SQL statement too long`：
# 合写失败 → 退回逐台重试，而且 taosAdapter 会把**整条语句**（实测 2.66MB）写进错误日志
# → 每秒一条 = 数 GB/小时的日志，曾把 30G 系统盘写满（2026-10-16 事故，见 docs）。
# ⚠️ 光按台数分片不够：`_DEVICES_PER_INSERT=200` 是按**窄表**（一行几列）定的，
# 换宽表后一行可达数百列，几台就能拼到 MB 级。留 ~10% 余量，别贴着 1MB。
_MAX_INSERT_CHARS = 900_000


def merge_values(
  state: dict[str, tuple[float, int]] | None, *, ts: int, values: dict, max_gap_ms: int,
) -> tuple[dict, dict[str, tuple[float, int]]]:
  """把本次采样与「每点最近一次**真实采样**」合并，返回 `(本行要写的值, 新状态)`。

  `state` 是 `point_code -> (值, 该值真实被采到的 ts)` —— **只记真实采样**，不记带出来的值。
  （纯函数，便于离线断言。）

  为什么需要（对方的推送是两种机制：10 秒定期 + **采集到达即推**）：
  即时包**只带刚采到的测点** —— 实测 18.6 万行里 `ht_temp` 缺 15%、`ht_hum` 缺 10%。
  如果照原样写，宽表就会是**稀疏**的：同一行的几个测点常常不齐，
  以后做「能耗 = 电压 × 电流」这类**同行多列联合计算**会算不出来。

  两条规则：

  1. **同 `ts`** → 合并。实测同一 `(设备, 时间戳)` 会被推两次且内容不同（3.4%）；
     不合并的话，后到的部分包会**把先到的完整行整行覆盖成 NULL**（TDengine 按 `(子表, ts)`
     后写覆盖，行是整行不可变的）—— 那是**数据丢失**。
  2. **不同 `ts`、且该点的真实采样距今 ≤ `max_gap_ms`** → **零阶保持**（把这个值带过来）。
     这在物理上是**对的**：一个 10 秒采样的温度传感器，两次采样之间它的值就是保持上一个。

  ⚠️ **带出来的值不刷新 `last_seen_ts`**：只按真实采样时刻算存活期。旧实现拿「上一条已合并的
  整行」当 prev，带出来的值被当成新的起点，于是只要设备持续推包、哪怕某测点再也不采，
  它也会被**永远**带下去（60s 窗口形同失效，行越写越宽）。

  ⚠️ **为什么要 `max_gap_ms` 限长**：设备离线 1 小时若无脑补齐，会补成一条**直线** →
  **把故障掩盖掉**。"这段时间没数据"是**应该被看见**的信息，所以超长间隔**保留空洞**。

  ⚠️ 只对**测点列**做（调用方传进来的 `values`）；`ts`/`report_ts`/`quality` 是"这一行的属性"，
  不是传感器读数，**跨行带过来是错的**，所以不在这里处理。
  """
  state = state or {}
  # 1) 零阶保持：只带「距最近一次真实采样不超过 max_gap_ms」的点
  merged: dict = {
    code: value for code, (value, seen_ts) in state.items()
    if 0 <= ts - seen_ts <= max_gap_ms
  }
  # 2) 本行真实采样的点覆盖；**本次为空的测点不覆盖**（否则又变回"用 NULL 冲掉已有值"）
  for code, value in values.items():
    if value is not None:
      merged[code] = value

  new_state = dict(state)
  for code, value in values.items():
    if value is None:
      continue
    last = new_state.get(code)
    # 时间倒流的迟到包不能把 last_seen 往回拨，否则会让更早的值提前过期
    if last is None or ts >= last[1]:
      new_state[code] = (value, ts)
  return merged, new_state


def _group_columns(mapping: dict[str, tuple[str, str]]) -> dict[str, list[str]]:
  """`{code: (group, col)}` → `{group: [col…]}`（按组归拢列名，建表用）。"""
  out: dict[str, list[str]] = {}
  for group, col in mapping.values():
    out.setdefault(group, []).append(col)
  return out


def _chunk_blocks(blocks: list) -> list[list]:
  """把设备片段切成若干条 INSERT：**以字符数为主、台数为辅**（纯函数，便于单测）。

  合并的收益是省 HTTP 往返（一条语句 = 一次往返），所以**尽量合**；但合并语句超过
  TDengine 的 `maxSQLLength` 会整条失败（`537`），所以必须**在发出去之前**就切好 ——
  靠"失败再重试"兜底代价太大：taosAdapter 会把整条超长语句写进错误日志（见 `_MAX_INSERT_CHARS`）。
  """
  chunks: list[list] = []
  cur: list = []
  cur_len = 0
  for blk in blocks:
    n = len(blk[3])
    # 先到者为准：字符数上限 or 台数上限（台数上限对**窄表**仍有意义：小片段也不能拼太多）
    if cur and (cur_len + n > _MAX_INSERT_CHARS or len(cur) >= _DEVICES_PER_INSERT):
      chunks.append(cur)
      cur, cur_len = [], 0
    cur.append(blk)
    cur_len += n + 1                     # 片段之间有一个空格（见 build_multi_device_insert）
  if cur:
    chunks.append(cur)
  return chunks


class WideWriter:
  """每个实时消费线程各持一个实例（内含各自的 PG / TDengine 连接与缓存）。

  **线程模型（别改成两边都碰）**：`write()` 分两段 ——

  1. **准备（调用线程、串行）**：查档案/分配列（**PG 事务，不能跨线程**）、零阶保持状态更新、
     DESCRIBE 列序、拼好 INSERT 字符串与分片；
  2. **下发（线程池、并行）**：只把已拼好的字符串发 HTTP，不做任何状态读写。

  所以 `_devices`/`_last`/`_code_to_col`/`_sub_override` 只在准备阶段访问，worker 只读入参。
  计数由 worker **返回增量**、调用线程累加，不加锁。`httpx.Client` 本身线程安全。
  """

  def __init__(
    self, pg: DcimRepository, td: RealtimeRepository, *, cluster: str,
    cluster_code: str = '', extra_sinks=None, workers: int | None = None,
  ) -> None:
    self._pg = pg
    self._td = td
    self._cluster = cluster
    # 园区代号：子表名 `d_<代号>_<guid>` 的前缀（见 park_codes.py）。
    # 空串只保留旧命名，生产由 `__main__` 从 ClusterSpec 传入。
    self._cluster_code = cluster_code
    # 附加 sink（如归档层）：拿到**同一份解析结果**（设备类型/列序/tag），
    # 这样 PG 只查一次、两个 sink 的数据天然一致 —— 对应设计里的「可插拔 sink」
    self._extra_sinks = list(extra_sinks or [])
    self._devices: dict[str, dict] = {}          # device_guid -> 设备档案（查到的，永久缓存）
    # 负缓存**必须带 TTL**：设备档案可能"先缺后补"（配置类 topic 晚于实时数据到达）。
    # 早期实现用"查过就不再查"的集合，结果那台设备被永久跳过 —— 与
    # "档案缺失可靠 topic 保留期重放补上"的承诺矛盾。所以只记"上次确认缺失的时间"，过期重查。
    self._missing_at: dict[str, float] = {}      # device_guid -> 上次确认缺失的时间
    # device_type -> {point_code: (point_group, td_column)}
    self._code_to_col: dict[str, dict[str, tuple[str, str]]] = {}
    # 每台设备「每个测点最近一次**真实采样**」—— 用来做同 ts 合并 / 跨 ts 零阶保持（见 `merge_values`）。
    # **只记真实采样**（带出来的值不入缓存），所以不会随时间累积成整行。
    # 内存占用 = 设备数 × 测点数 × 一个小元组（1 万台 × 几十点也就几 MB）。
    # 线程安全：`WideWriter` 每个实时消费线程各持一个，而 Kafka 消息 **key = 设备 guid**
    # → 同一台设备必然落在同一分区、被同一线程处理 → 不会被两个线程同时写，**不需要加锁**。
    self._last: dict[str, dict[str, tuple[float, int]]] = {}
    #   device_guid -> {point_code: (值, 该值真实被采到的 ts)}
    # 零阶保持的**最长间隔**（见 `merge_values`）：超过它就不补，如实留空 —— 否则设备离线
    # 一小时会被补成一条直线、把故障掩盖掉。正常推送间隔 10 秒 → 这里给 6 倍余量。
    self._carry_max_ms = max(0, int(settings.DCIM_CARRY_MAX_SECONDS)) * 1000
    self.unknown_devices = 0                     # 设备档案缺失的采样数（观测用）
    self.failed_devices = 0                      # 单设备写入失败次数（观测用）
    self.statements = 0                          # 已下发的 INSERT 语句数（观测用）
    self.merge_failures = 0                      # 合并语句失败转逐台的次数（观测用）
    self.subtable_remaps = 0                     # 子表换名的设备数（观测用）

    # 并发下发：实测串行时客户端只有 ~1 条在途、服务端平均并发 2.38，而 taosd 一半 CPU 空转。
    # `workers` 仅供自检注入；生产读 `DCIM_WRITE_CONCURRENCY`。
    self._write_workers = max(
      1, int(workers if workers is not None else settings.DCIM_WRITE_CONCURRENCY),
    )
    self._pool: ThreadPoolExecutor | None = None
    # (device_guid, point_group, device_type) -> 换过名的子表名。
    # 设备改过 device_type 后，原 `d_<guid>` 仍绑在旧超级表上（同名子表不能再挂到新表），
    # 换一个带类型指纹的名字就能继续写；旧表/旧数据原样保留。只在准备阶段读、worker 只在
    # 「第一次撞冲突」后写一次（dict 赋值在 CPython 里是原子的）。
    self._sub_override: dict[tuple[str, str, str], str] = {}

  def _ensure_pool(self) -> ThreadPoolExecutor:
    if self._pool is None:
      self._pool = ThreadPoolExecutor(
        max_workers=self._write_workers,
        thread_name_prefix=f'dcim-wr-{self._cluster}',
      )
    return self._pool

  # ── 连接（消费线程只认 connect / close / write 三个方法） ──
  def connect(self) -> None:
    self._pg.connect()
    self._td.connect()
    for sink in self._extra_sinks:
      sink.connect()

  def close(self) -> None:
    # ⚠️ 线程池必须**在关连接之前**停：反过来会让在途请求撞上已关闭的 TD/PG。
    if self._pool is not None:
      self._pool.shutdown(wait=True)
      self._pool = None
    # 先关附加 sink（归档要把缓冲刷完），再关主库
    for sink in reversed(self._extra_sinks):
      try:
        sink.close()
      except Exception:                          # noqa: BLE001 —— 一个 sink 失败不影响其它
        logger.warning('[%s] 附加 sink 关闭失败', self._cluster, exc_info=True)
    self._td.close()
    self._pg.close()

  # ── 设备档案 ──
  def _fill_devices(self, guids: set[str]) -> None:
    """批量预热设备档案缓存。查到的永久缓存；**查不到的按 TTL 重试**（可能先缺后补）。"""
    now = time.time()
    miss = [
      g for g in guids
      if g not in self._devices and now - self._missing_at.get(g, 0.0) >= _MISS_TTL
    ]
    if not miss:
      return
    # ⚠️ 必须带 cluster：guid 只在园区内唯一，不带过滤时重复 guid 的档案是随机命中的
    found = self._pg.devices_meta(miss, cluster=self._cluster)
    self._devices.update(found)
    for g in miss:
      if g in found:
        self._missing_at.pop(g, None)
      else:
        self._missing_at[g] = now
    if len(found) < len(miss):
      logger.info(
        '[%s] 设备档案暂缺 %d 台（配置类 topic 未到？%.0fs 后会重查，可靠保留期重放补）',
        self._cluster, len(miss) - len(found), _MISS_TTL,
      )

  # ── 列映射（持久化在 PG，只对**新**编码分配，避免列名漂移） ──
  def _ensure_columns(
    self, device_type: str, codes: set[str],
  ) -> dict[str, tuple[str, str]]:
    """拿到该 device_type 的**全量**映射 `{point_code: (point_group, td_column)}`，缺的补上。

    「读现有映射 → 分配列名/分组 → 落库」在 `allocate_columns_locked` 里**一个事务完成**
    （带按 device_type 的建议锁）—— 并发加列不会再撞 `UNIQUE(device_type, td_column)`。

    ⚠️ 顺序是 **分配（未提交）→ 逐组建 TDengine 表 → 提交**：若先把映射提交进 PG，
    ALTER 失败就会留下「PG 里已注册、TDengine 里没有」的列 —— 之后每次重放都带着它去
    ALTER、永远失败（整园区消费者卡死）。分配结果是确定性的，回滚后重放会分到同样的列。
    """
    known = self._code_to_col.get(device_type)
    if known is not None and codes <= known.keys():
      return known                       # 本批没有新编码 → 用缓存，不碰 PG

    before = len(known or {})
    known = self._pg.allocate_columns_locked(device_type, sorted(codes), commit=False)
    try:
      for point_group, cols in _group_columns(known).items():
        # ⚠️ 传**该组全量已知列**：ensure_stable 的建表分支要用它建全；
        #    只传新增列会在「TDengine 重建但 PG 保留」时漏建旧列 → 静默丢数据
        self._td.ensure_stable(
          stable_name(device_type, point_group), {col: 'DOUBLE' for col in cols},
        )
    except Exception:
      self._pg.rollback()                # 不留下 TDengine 不认的映射
      raise
    self._pg.commit()
    self._code_to_col[device_type] = known
    if len(known) > before:
      logger.info('[%s] 设备类型 %s 列映射 +%d（共 %d 列，%d 组）',
                  self._cluster, device_type, len(known) - before, len(known),
                  len({g for g, _col in known.values()}))
    return known

  # ── 写入 ──
  def write(self, samples: list[DeviceSample]) -> int:
    """写一批采样，返回**实际写入的行数**（每行 = 一台设备一个时刻）。

    准备阶段全部串行，下发阶段全部并行。**任何非设备级异常都会从本方法抛出** ——
    上层（`RealtimeConsumer._flush`）靠这个做整批退避重试；吞掉就等于静默丢数据。
    """
    if not samples:
      return 0
    self._fill_devices({s.device_guid for s in samples})

    by_type: dict[str, list[DeviceSample]] = {}
    for s in samples:
      meta = self._devices.get(s.device_guid)
      if not meta or not meta.get('device_type'):
        self.unknown_devices += 1
        continue                      # 档案缺失：跳过，可重放
      by_type.setdefault(str(meta['device_type']), []).append(s)
    if not by_type:
      return 0

    # ① 准备（串行）：结果只是字符串，线程池里不再碰 PG / 状态
    prepared = {dt: self._prepare_type(dt, batch) for dt, batch in by_type.items()}

    # ② 下发（并行）：**所有** chunk 一次性提交，避免串行等待
    pool = self._ensure_pool()
    jobs = [
      (dt, point_group, chunk, pool.submit(self._exec_chunk, dt, point_group, chunk))
      for dt, (_by_device, _groups, chunks, _states) in prepared.items()
      for point_group, chunk in chunks
    ]

    done: dict[str, set[tuple[str, int]]] = {dt: set() for dt in by_type}
    first_error: BaseException | None = None
    for device_type, _point_group, _chunk, future in jobs:
      try:
        rows, failed, statements, merge_failed, remapped = future.result()
      except Exception as e:                        # noqa: BLE001 —— 收全再抛，不让其它 chunk 白跑
        first_error = first_error or e
        continue
      done[device_type].update(rows)
      self.failed_devices += failed
      self.statements += statements
      self.merge_failures += merge_failed
      self.subtable_remaps += remapped

    # 有异常就整批交给上层重试：**不归档**，否则重试成功后会重复归档同一批行
    if first_error is not None:
      raise first_error

    # 全批写成功后才推进零阶保持状态。原先在准备阶段就写回 `_last`，一旦整批失败重试，
    # 重放会拿「已推进到本批最新 ts 的状态」去回填历史行 —— 把未来值带进过去。
    # 窗口拉长后一批里同一设备可能有多行，这个问题的影响面会随之变大。
    for _by_device, _groups, _chunks, states in prepared.values():
      self._last.update(states)

    for device_type, (_by_device, groups, chunks, _states) in prepared.items():
      if self._extra_sinks and done[device_type]:
        self._archive(device_type, _by_device, groups, done[device_type])
    return sum(len(d) for d in done.values())

  def _prepare_type(
    self, device_type: str, batch: list[DeviceSample],
  ) -> tuple[dict, dict, list[tuple[str, list]], dict[str, dict]]:
    """准备阶段（**调用线程**）：查映射、零阶保持、拼 SQL、分片。

    返回 `(by_device, groups, chunks, new_states)` —— 状态由调用方在全批成功后统一写回
    `self._last`（失败不推进，重试与首次调用逐字一致）。
    """
    code_to_group = self._ensure_columns(
      device_type, {c for s in batch for c in s.values},
    )

    by_device: dict[str, list[DeviceSample]] = {}
    for s in batch:
      by_device.setdefault(s.device_guid, []).append(s)

    # ★ 同 ts 合并 / 跨 ts 零阶保持（见 `merge_values` 的两条规则）。
    #   **按 ts 升序**处理，这样"每点最近一次真实采样"能顺着链带下去（批内一条设备可能有多个时刻）。
    #   状态只记真实采样 → 处理完留回缓存供**下一批**继续，不会被带出来的值撑大。
    new_states: dict[str, dict] = {}
    for guid, samples in by_device.items():
      state = self._last.get(guid) or {}
      out: list[DeviceSample] = []
      for s in sorted(samples, key=lambda x: x.ts):
        vals, state = merge_values(state, ts=s.ts, values=s.values,
                                   max_gap_ms=self._carry_max_ms)
        out.append(replace(s, values=vals))
      by_device[guid] = out
      new_states[guid] = state

    # 测点按组拆表：**每组一张超级表、每台设备每组各写一行**（组内只有该组的列）。
    # 各组行数因此一致，读侧才能按 (时间, 设备) 把多组行合成一行。
    groups: dict[str, dict[str, str]] = {}          # point_group -> {code: col}
    for code, (point_group, col) in code_to_group.items():
      groups.setdefault(point_group, {})[code] = col

    chunks: list[tuple[str, list]] = []
    for point_group in sorted(groups):
      g_stable = stable_name(device_type, point_group)
      # ⚠️ 列序以 TDengine 的 DESCRIBE 为准（USING…TAGS 语法下 VALUES 只能按建表列序对应）
      columns = self._td.stable_columns(g_stable)
      code_to_col = groups[point_group]
      col_to_code = {col: code for code, col in code_to_col.items()}
      # 按「设备类型 × 组」预计算一次列序号：`_used_columns` 反查后按它排序，
      # 免去每台设备逐列扫描 4,000+ 列（实测约 26µs/行，是 GIL 饱和的热点之一）
      col_index = {col: i for i, col in enumerate(columns)}

      # 先给每台设备拼好 `<子表> USING ... TAGS(...) VALUES(...)` 片段，再**合并成尽量少的语句**。
      # ⚠️ 一条语句 = 一次 HTTP 往返：逐设备各发一条的话往返数 = 设备数，库一旦不在本机
      # （跨机房 RTT 10~30ms）就会按设备数放大。合并后往返数与批大小无关。
      # 片段是 `(guid, tags, samples, body, subtable)` —— subtable 留着，换名重试时要替换。
      blocks: list[tuple[str, dict, list[DeviceSample], str, str]] = []
      for guid, samples in by_device.items():
        meta = self._devices.get(guid) or {}
        space_guid = str(meta.get('space_guid') or '')
        tags = {
          'cluster_name': self._cluster,
          'device_guid': guid,
          'point_group': point_group,
          'space_guid': space_guid,
          'device_type': device_type,
          'idc': samples[0].idc,
        }
        subtable = (self._sub_override.get((guid, point_group, device_type))
                    or subtable_name(guid, point_group, self._cluster_code))
        body = build_insert_body(
          stable=g_stable,
          subtable=subtable,
          tag_values=tag_values(
            cluster=self._cluster, device_guid=guid,
            space_guid=space_guid, device_type=device_type, idc=tags['idc'],
            point_group=point_group,
          ),
          columns=columns, col_to_code=col_to_code, samples=samples,
          code_to_col=code_to_col, col_index=col_index,
        )
        blocks.append((guid, tags, samples, body, subtable))

      for chunk in _chunk_blocks(blocks):
        chunks.append((point_group, chunk))
    return by_device, groups, chunks, new_states

  def _exec_chunk(self, device_type: str, point_group: str, chunk: list) -> tuple:
    """下发阶段（**工作线程**）：发一条合并 INSERT，失败按「设备级 / 瞬时」分派。

    返回 `(done, failed, statements, merge_failed, remapped)` 增量，由调用线程累加。
    """
    done: set[tuple[str, int]] = set()
    failed = 0
    statements = 1
    merge_failed = 0
    remapped = 0
    try:
      self._td.exec_sql(build_multi_device_insert([b[3] for b in chunk]))
      ok = chunk
    except Exception as e:                          # noqa: BLE001
      # ⚠️ 瞬时故障（超时/5xx/连接）必须**原样抛出**：让 `_flush` 的整批退避重试生效。
      #    旧写法在这里一律吞掉 + 自动提交 offset = 失败的数据永久丢失。
      if not _is_device_error(e):
        raise
      # ⚠️ **逐设备隔离**：单台设备失败（如子表绑在旧超级表）不能把同批其它设备一起拖失败。
      # 合并写省往返，代价是"一台坏则整条失败" —— 所以失败后退回**逐台重试**，
      # 两边的性质都保住：正常路径 1 次往返，异常路径仍只丢坏的那台。
      merge_failed = 1
      logger.warning('[%s] 设备类型 %s 组 %s 合并写入失败（%d 台），改为逐台重试: %s',
                     self._cluster, device_type, point_group, len(chunk), e)
      ok = []
      device_errors: list[BaseException] = []
      for guid, _tags, samples, body, subtable in chunk:
        statements += 1
        try:
          self._td.exec_sql('INSERT INTO ' + body)
          ok.append((guid, _tags, samples, body, subtable))
          continue
        except Exception as e2:                     # noqa: BLE001
          if not _is_device_error(e2):
            raise                                   # 瞬时故障：整批重试，别当设备故障跳过
          error = e2

        # 子表名冲突（设备改过 device_type）：换一个带类型指纹的名字重试一次
        if 'already exists in other stables' in str(error).lower():
          alt = subtable_variant(guid, point_group, device_type, self._cluster_code)
          swapped = body.replace(f'{subtable} USING ', f'{alt} USING ', 1)
          if swapped != body:
            statements += 1
            try:
              self._td.exec_sql('INSERT INTO ' + swapped)
              key = (guid, point_group, device_type)
              if self._sub_override.get(key) != alt:
                self._sub_override[key] = alt
                remapped += 1
              logger.info('[%s] 设备 %s 子表换名为 %s（原 %s 已绑在旧超级表），后续直接用新名',
                          self._cluster, guid, alt, subtable)
              ok.append((guid, _tags, samples, swapped, alt))
              continue
            except Exception as e3:                 # noqa: BLE001
              if not _is_device_error(e3):
                raise
              error = e3

        failed += 1
        device_errors.append(error)
        logger.warning('[%s] 设备 %s 写入失败（跳过该设备，不影响同批其它）: %s',
                       self._cluster, guid, error)

      # 半数以上设备都失败 → 更像系统性故障（表结构/权限/库状态），不是个别坏设备：
      # 原样抛出交给上层整批重试，别把半批数据静默丢掉。
      # **小批不判**（<5 台）：末批常只有一两台，一台坏就是 100%，会把「个别坏设备」
      # 误判成系统性故障 → 整批重试 → 又退回卡死重放的老路。
      if len(chunk) >= _SKIP_RATIO_MIN_DEVICES and failed * 2 > len(chunk):
        raise device_errors[0]

    for guid, _tags, samples, _body, _subtable in ok:
      done.update((guid, s.ts) for s in samples)
    return done, failed, statements, merge_failed, remapped

  def _archive(
    self, device_type: str, by_device: dict[str, list[DeviceSample]],
    groups: dict[str, dict[str, str]], done: set[tuple[str, int]],
  ) -> None:
    """归档 sink：每台设备**只调一次**，传**全组列并集**、tag `point_group='g0'`。

    归档是「一类型一表」的导出，组在文件里没有业务含义；且 bucket 的列集在建桶时固定，
    逐组各传一份不同的列会互相打架。失败只记日志，不影响 TDengine 落库。
    """
    first = sorted(groups)[0]
    columns = list(self._td.stable_columns(stable_name(device_type, first)))
    col_to_code: dict[str, str] = {}
    for point_group in sorted(groups):
      for code, col in groups[point_group].items():
        col_to_code[col] = code
        if col not in columns:              # 系统列已在 columns 里，这里只补其它组的测点列
          columns.append(col)

    for guid, samples in by_device.items():
      ok = [s for s in samples if (guid, s.ts) in done]
      if not ok:
        continue
      meta = self._devices.get(guid) or {}
      tags = {
        'cluster_name': self._cluster,
        'device_guid': guid,
        'point_group': 'g0',
        'space_guid': str(meta.get('space_guid') or ''),
        'device_type': device_type,
        'idc': ok[0].idc,
      }
      for sink in self._extra_sinks:
        try:
          sink.write_rows(
            cluster=self._cluster, device_type=device_type, columns=columns,
            col_to_code=col_to_code, tags=tags, samples=ok,
          )
        except Exception:                      # noqa: BLE001 —— 归档失败不能拖垮实时写入
          logger.warning('[%s] 附加 sink 写入失败（不影响 TDengine 落库）',
                         self._cluster, exc_info=True)

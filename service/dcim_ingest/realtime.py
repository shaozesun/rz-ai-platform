"""DCIM 实时数据落 TDengine —— **宽表**（一个设备类型一张超级表、一台设备一张子表、测点做列）。

职责边界（和 repository.py 的分工）：
  - `repository.py` → **PostgreSQL**：空间/设备/点位的**定义**、变更历史、告警阈值
  - 本模块        → **TDengine**：点位的**数值**时间序列

为什么走 REST 而不是 native 协议：
  TDengine 的 native 客户端（`taospy` 的 `taos` 子模块）依赖 C 库 `libtaos`，要在镜像里额外装客户端；
  而 taosAdapter 的 REST 接口用现成的 `httpx` 就能打，**不加依赖、不用重建镜像**。
  代价是 HTTP 开销 —— 当前量级完全无所谓；真到百万测点再评估切 native（REST 上限约 50 万行/秒）。

**宽表而不是窄表**（一测点一行）：
  窄表里「同一时刻的不同测点」是**不同的行**，要对齐就得按 `(设备, ts)` 做 **k 路 self-join**
  或拉到应用层 pivot —— 测点越多路数越多（实测 799 万行的窄表上一次 self-join 取 3 行，均耗 0.55s；
  TDengine 3.3 **能做** self-join，见下方更正）。宽表把「同一设备同一时刻的各测点」放在**同一行**，
  对齐是**结构性的**，天然成立；行数还少一个量级（现网 36 台设备 × 3 测点：799 万行 → 约 285 万行，
  **测点越多的设备类型倍数越大**），写入也更省（对方本来就按设备组包推送）。
  设计依据见 docs/dcim-timeseries-storage-design.md §4。

⚠️ **更正（2026-10-07 实测）**：本文档与设计文档此前写的「TDengine 不支持多表 JOIN」**不成立** ——
在 3.3.0.0 上实测 `dcim_realtime` 自连接（`a.ts=b.ts AND a.device_guid=b.device_guid`）可正常返回。
所以选宽表的理由**不是"窄表没法对齐"，而是对齐的成本与行数**。

写入幂等：TDengine 的**子表 + 时间戳**天然去重 —— 同一 `(子表, ts)` 重复写入是覆盖，
所以"10 秒周期 + 变化"两种推送即使有重叠也不会产生重复行。
"""

from __future__ import annotations

import base64
import logging
import math
import re
import time
from typing import Any

import httpx

from config.settings import settings
from service.dcim_ingest.models import DeviceSample

logger = logging.getLogger(__name__)

# `DESCRIBE` 列序的缓存时长（见 `stable_columns`）：长驻进程里要能感知"超级表加列"
_COLUMNS_TTL = 300.0

# 列序缓存**进程级共享**（所有 RealtimeRepository 实例同一份）：
# 实时消费并行后，线程 A 的 `ensure_stable` 加列只能清掉自己的旧缓存；
# 若各线程各存一份，线程 B 最长 300s 内仍按旧列数拼 VALUES → 与表列数不符、INSERT 直接失败。
_COLUMNS_CACHE: dict[str, tuple[list[str], float]] = {}   # stable -> (列序, 缓存时刻)

# 系统列（不是测点）：建表时就固定，映射表里不会出现它们
COL_TS = 'ts'
COL_REPORT_TS = 'report_ts'
COL_QUALITY = 'quality'
SYSTEM_COLUMNS = (COL_TS, COL_REPORT_TS, COL_QUALITY)

# 超级表的 tag（顺序即 TAGS(...) 的取值顺序，全库统一）
# ⚠️ **tag 要一次设计到位**：实测有「50 万子表批量改 tag 把 taosd CPU 打满且不回落」的事故。
# 这些维度是稳定属性（官方规则：稳定不变的做 tag，连续变化的值做列）。
# `space_guid` / `device_type` 冗余进来是**为跨库查询铺路** —— TDengine 不能 JOIN PG，
# 有了它们，「某机房/某类型设备的曲线」可以直接打在 TDengine 上。
TAG_COLUMNS: tuple[tuple[str, str], ...] = (
  ('cluster_name', 'BINARY(32)'),   # 园区（来自消息所属集群，不是报文字段）
  ('device_guid', 'BINARY(64)'),
  ('point_group', 'BINARY(16)'),    # 默认 'g0'；设备测点特别多、拆多张子表时用
  ('space_guid', 'BINARY(64)'),
  ('device_type', 'BINARY(48)'),
  ('idc', 'BINARY(64)'),
)


def tag_values(
  *, cluster: str, device_guid: str, space_guid: str, device_type: str,
  idc: str = '', point_group: str = 'g0',
) -> list[str]:
  """按 `TAG_COLUMNS` 的顺序产出 tag 值（顺序错了会 tag 串位）。"""
  return [cluster, device_guid, point_group, space_guid, device_type, idc]

# 「不可能重名」的列名兜底会走 td_schema.normalize_column，这里只做 SQL 字面量转义
def _q(s: str) -> str:
  """SQL 单引号字符串转义。"""
  return "'" + str(s).replace('\\', '\\\\').replace("'", "\\'") + "'"


def _num(v: Any) -> str:
  """浮点 → SQL 字面量；NaN/Inf/缺失一律写成 NULL（TDengine 不接受它们）。"""
  if v is None:
    return 'NULL'
  try:
    f = float(v)
  except (TypeError, ValueError):
    return 'NULL'
  return 'NULL' if not math.isfinite(f) else repr(f)


def _used_columns(
  columns: list[str], col_to_code: dict[str, str], samples: list[DeviceSample],
  *,
  code_to_col: dict[str, str] | None = None,
  col_index: dict[str, int] | None = None,
) -> list[str]:
  """本设备本批**真正要写**的列（按 `columns` 的 DESCRIBE 顺序，纯函数）。

  为什么只写用到的列：超级表动辄 4,000+ 列（00AG 4,068、00WG 4,043），而一台设备
  只上报自己那几十个测点 —— 实测抽 16 列做 `count(col)`，15 列在 10 分钟窗口里为 0。
  旧写法给每个未采样列都写 `'NULL'`，单行 SQL ≈30KB，是吞吐瓶颈的主因。

  TDengine 的列清单写法（`INSERT INTO tb USING stb TAGS(...) (c1,c2) VALUES(...)`，
  ≥2.0.20.5）**未列出的列自动填 NULL** —— 与旧写法逐字等价，读侧无需改动。
  `ts` 必须在内；`quality` 恒为 NULL，永不进清单。

  ⚠️ 实现是「按本批测点**反查列** + 按列序号排序」，**不是逐列扫描超级表**：
  逐列扫描 4,071 列实测约 26µs/行（cProfile，单进程 GIL 饱和的主因之一）；反查后只碰
  本设备上报的几十个测点，输出顺序仍严格等于 DESCRIBE 顺序。`code_to_col` / `col_index`
  由调用方按「设备类型 × 组」预计算一次即可（不传则就地计算，语义完全相同）。
  """
  if code_to_col is None:
    code_to_col = {code: col for col, code in col_to_code.items()}
  if col_index is None:
    col_index = {col: i for i, col in enumerate(columns)}

  codes = {code for s in samples for code, v in s.values.items() if v is not None}
  idxs: set[int] = set()
  ts_i = col_index.get(COL_TS)
  if ts_i is not None:               # 理论恒真；保留兜底，与旧写法逐字等价
    idxs.add(ts_i)
  if any(s.report_ts is not None for s in samples):
    report_i = col_index.get(COL_REPORT_TS)
    if report_i is not None:
      idxs.add(report_i)
  for code in codes:
    col = code_to_col.get(code)
    if not col or col in SYSTEM_COLUMNS:
      continue
    i = col_index.get(col)
    if i is not None:                # 列不在 DESCRIBE 里（跨进程缓存落后）→ 与旧写法一样跳过
      idxs.add(i)

  picked = [columns[i] for i in sorted(idxs)]
  if not picked:                     # 理论不可达（samples 非空必有 ts），兜底不拼空列名表
    picked = [COL_TS]
  if picked == [COL_TS]:
    # 只有时间戳的行交给 TDengine 是否可写没把握；补一个占位列写 NULL，
    # 与旧写法「本组无测点也写一整行 NULL」等价（拆组时各组行数要对齐）。
    pad = next((c for c in columns if c not in SYSTEM_COLUMNS), None)
    if pad:
      picked.append(pad)
  return picked


def build_wide_insert(
  *,
  stable: str,
  subtable: str,
  tag_values: list[str],
  columns: list[str],
  col_to_code: dict[str, str],
  samples: list[DeviceSample],
) -> str:
  """一台设备一条 INSERT（多行 VALUES），纯函数便于单测。

  ⚠️ **`columns` 必须是超级表的真实列顺序**（来自 `DESCRIBE`，权威）——
  写出的列清单按它过滤，保证输出确定；PG 里存的是 `point_code ↔ td_column` 映射，
  **顺序以 TDengine 为准**，避免两处漂移。

  `col_to_code` 是列名 → 测点编码；系统列（ts/report_ts/quality）不在其中，单独取值。
  """
  return 'INSERT INTO ' + build_insert_body(
    stable=stable, subtable=subtable, tag_values=tag_values,
    columns=columns, col_to_code=col_to_code, samples=samples,
  )


def build_insert_body(
  *,
  stable: str,
  subtable: str,
  tag_values: list[str],
  columns: list[str],
  col_to_code: dict[str, str],
  samples: list[DeviceSample],
  code_to_col: dict[str, str] | None = None,
  col_index: dict[str, int] | None = None,
) -> str:
  """一台设备的 **`<子表> USING <超级表> TAGS(...) (列清单) VALUES(...)` 片段**
  （不含前导 `INSERT INTO`）。

  拆出来是为了**多设备合并成一条语句**（见 `build_multi_device_insert`）——
  `INSERT INTO` 后面可以直接跟多个这样的片段；**每个片段可各带自己的列清单**
  （官方支持「一条语句写多张表、每张表可指定自己的列」，已核实文档）。

  列清单由 `_used_columns` 按本批真实出现的测点算出：设备只报几十个测点，
  不再给超级表的 4,000+ 列各写一个 `'NULL'`。未列出的列 TDengine 自动填 NULL。

  `code_to_col` / `col_index` 是「设备类型 × 组」维度的预计算缓存（见 `_used_columns`），
  热点路径上由 `WideWriter._prepare_type` 传入；不传则就地计算，输出逐字相同。
  """
  tags = ','.join(_q(t) for t in tag_values)
  cols = _used_columns(
    columns, col_to_code, samples, code_to_col=code_to_col, col_index=col_index,
  )
  # 预展开成 (列名, 测点编码)：否则每行每列都要再查一次 `col_to_code`
  spec = [(col, col_to_code.get(col)) for col in cols]
  rows = []
  for s in samples:
    cells = []
    for col, code in spec:
      if col == COL_TS:
        cells.append(str(int(s.ts)))
      elif col == COL_REPORT_TS:
        cells.append('NULL' if s.report_ts is None else str(int(s.report_ts)))
      else:
        cells.append(_num(s.values.get(code)) if code else 'NULL')
    rows.append('(' + ','.join(cells) + ')')
  # ⚠️ `VALUES` 关键字照写不省 —— 与**生产上正在跑的**窄表写入器同形
  # （`{子表} USING {超级表} TAGS(...) VALUES (...)`），语法风险为零；别自作聪明省略。
  return (
    f'{subtable} USING {stable} TAGS({tags}) '
    f'({",".join(cols)}) VALUES ' + ' '.join(rows)
  )


def build_multi_device_insert(bodies: list[str]) -> str:
  """把**多台设备的片段**拼成一条 INSERT（纯函数，便于单测）。

  ⚠️ **语法要点（实测踩过）**：不能写成 `INSERT INTO t1 ... INTO t2 ...`
  （报 `syntax error ... (table_name is expected)`）；正确形式是**一个 `INSERT INTO`
  后面跟多个 `<子表> USING ... TAGS(...) VALUES(...)` 片段**。

  **为什么必须合并**：TDengine 走 REST 时**一条语句 = 一次 HTTP 往返**。
  逐设备各发一条的话，往返次数 = 本批设备数 —— 同机房（实测 RTT 0.45~0.68ms）无所谓，
  但库一旦挪到另一台机器/跨机房（RTT 10~30ms）就会**按设备数放大**，
  写入从毫秒级变成百毫秒级、甚至追不上推送。合并成一条后，往返次数与**批大小**无关。

  ✅ **语法是生产验证过的**：调用方（`WideWriter._exec_chunk`）一次只处理**一个 device_type**，
  所以各片段用的是**同一张超级表** —— 这与窄表写入器在生产上跑了很久的形态**完全同形**，
  不是新写法（那边 `parts[i:i+200]` 同样是 200 个片段拼一条语句）。
  """
  if not bodies:
    raise ValueError('合并 INSERT 需要至少一个设备片段')
  return 'INSERT INTO ' + ' '.join(bodies)


# 允许的时间戳精度（**白名单**：建库参数，写错了不可逆，不能由配置随意塞值）
_PRECISIONS = ('ms', 'us', 'ns')


def build_create_database() -> str:
  """建库 DDL（**纯函数**，便于离线断言"参数一个都没漏"——不必连库）。

  ⚠️ **只有 `VGROUPS` 建后不可改**，所以它必须一次定对；其余几个都能 `ALTER DATABASE`。
  这里把 **8 个参数全部显式写出**：`COMP` / `BUFFER` / `WAL_LEVEL` / `PRECISION` 本可以
  吃 TDengine 默认值，但那套默认值会随版本变 —— 写死才可复现。（它们此前只存在于设计文档
  的示例 DDL 里，从没落到代码，实测只是"恰好等于默认值"。）

  两条自证约束：
    - `KEEP ≥ 3 × DURATION`（TDengine 硬要求）
    - `KEEP / DURATION` = 单 vnode 的时间切片文件数，官方建议 **10~100** → `1d`/`90d` = 90 ✓
  """
  precision = str(settings.TAOS_PRECISION).strip().lower()
  if precision not in _PRECISIONS:
    raise ValueError(f'TAOS_PRECISION 只能是 {"/".join(_PRECISIONS)}，收到 {precision!r}')
  return (
    f'CREATE DATABASE IF NOT EXISTS {settings.TAOS_DB}'
    f' VGROUPS {int(settings.TAOS_VGROUPS)}'
    f' DURATION {settings.TAOS_DURATION}'
    f' KEEP {int(settings.TAOS_KEEP_DAYS)}'
    f" CACHEMODEL '{settings.TAOS_CACHEMODEL}'"
    f' COMP {int(settings.TAOS_COMP)}'
    f' BUFFER {int(settings.TAOS_BUFFER)}'
    f' WAL_LEVEL {int(settings.TAOS_WAL_LEVEL)}'
    f" PRECISION '{precision}'"
  )


class RealtimeError(Exception):
  """TDengine 调用异常，交由消费线程重连。"""


class RealtimeRepository:
  """TDengine 读写（每个消费线程各持一个实例）。

  只负责「连 TDengine + 建库建表 + 执行 SQL」，**不碰 PG**：
  设备类型/列映射来自 PG，由上层 `wide_writer` 协调后传进来。
  """

  def __init__(self) -> None:
    self._host = f'http://{settings.TAOS_HOST}:6041'
    auth = base64.b64encode(
      f'{settings.TAOS_USER}:{settings.TAOS_PASSWORD}'.encode()
    ).decode()
    self._headers = {'Authorization': f'Basic {auth}'}
    self._client: httpx.Client | None = None
    self._columns_cache = _COLUMNS_CACHE      # 进程级共享（见模块顶部说明）

  # ── 连接 ──
  def connect(self) -> None:
    """只建立 HTTP 客户端（建库建表见 `ensure_database` / `ensure_stable`）。"""
    self._client = httpx.Client(timeout=30.0)

  def close(self) -> None:
    if self._client is not None:
      try:
        self._client.close()
      finally:
        self._client = None
    # 列序缓存是进程级共享的，**不能在这里清** —— 一个线程退出会连带清掉其它线程的缓存

  # ── SQL 执行 ──
  def _exec(self, sql: str, *, db: str | None = None) -> Any:
    """执行一条 SQL。db=None 表示不带库名（用于 CREATE DATABASE）。"""
    if self._client is None:
      raise RealtimeError('未连接（先调 connect()）')
    url = f'{self._host}/rest/sql' + (f'/{db}' if db else '')
    try:
      resp = self._client.post(url, headers=self._headers, content=sql.encode('utf-8'))
    except httpx.HTTPError as e:
      raise RealtimeError(f'TDengine 请求失败: {e}') from e
    if resp.status_code != 200:
      raise RealtimeError(f'TDengine HTTP {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    if data.get('code') not in (0, None):
      raise RealtimeError(f'TDengine 报错: {data.get("desc") or data}')
    return data

  # ── 建库（幂等；⚠️ `VGROUPS` 建后不可改，其余几个可 ALTER，见 settings.TAOS_*） ──
  def database_exists(self) -> bool:
    """库是否已存在（`SHOW DATABASES` 一行）—— 用来识别"参数不会生效"这种情况。"""
    data = self._exec('SHOW DATABASES', db=None)
    return any(str(row[0]) == settings.TAOS_DB for row in data.get('data') or [])

  def ensure_database(self) -> None:
    """建库（幂等）。

    ⚠️ **库已存在时，`CREATE DATABASE IF NOT EXISTS` 会静默忽略全部参数** ——
    于是"配置写着 KEEP 90、库里其实还是老值"会一直存在，而日志还在报 90（误导）。
    所以先探一次：已存在就告警，让人知道要 `ALTER DATABASE`（`VGROUPS` 改不了，只能重建库）。
    实测现网库正是这种情况：`CACHEMODEL 'none'` + `KEEP 525600m` + `VGROUPS 2`（旧代码的默认值）。
    """
    existed = False
    try:
      existed = self.database_exists()
    except RealtimeError as e:            # 探测失败不该拦住建库本身
      logger.warning('TDengine 库列表探测失败（不影响建库）：%s', e)

    self._exec(build_create_database(), db=None)

    if existed:
      logger.warning(
        'TDengine 库 %s **已存在** → 本次建库参数（vgroups=%s duration=%s keep=%sd cachemodel=%s）'
        '**没有生效**，库里仍是建库时的那套。要调整请 `ALTER DATABASE %s ...`'
        '（`VGROUPS` 建后不可改，只能重建库）',
        settings.TAOS_DB, settings.TAOS_VGROUPS, settings.TAOS_DURATION,
        settings.TAOS_KEEP_DAYS, settings.TAOS_CACHEMODEL, settings.TAOS_DB,
      )
    else:
      logger.info(
        'TDengine 库已建（%s，vgroups=%s duration=%s keep=%sd cachemodel=%s comp=%s buffer=%sMB）',
        settings.TAOS_DB, settings.TAOS_VGROUPS, settings.TAOS_DURATION,
        settings.TAOS_KEEP_DAYS, settings.TAOS_CACHEMODEL,
        settings.TAOS_COMP, settings.TAOS_BUFFER,
      )

  # ── 超级表（按设备类型；列会随新测点增长） ──
  def stable_columns(self, stable: str) -> list[str]:
    """超级表的**真实列顺序**（去掉 tag），带缓存 —— 顺序是 VALUES 拼装的唯一依据。

    ⚠️ 缓存带 **TTL**（`_COLUMNS_TTL`）：长驻进程（Web 侧）里新测点会让超级表**加列**，
    缓存不过期的话「取最新值 / 聚合取数」会一直漏掉新列，直到进程重启。
    摄入侧不受影响 —— `ensure_stable` 加完列会主动清缓存。
    """
    hit = self._columns_cache.get(stable)
    if hit is not None and (time.monotonic() - hit[1]) < _COLUMNS_TTL:
      return hit[0]
    data = self._exec(f'DESCRIBE {stable}', db=settings.TAOS_DB)
    # DESCRIBE 返回 [field, type, length, note, ...]；note='TAG' 的行是 tag，要排除
    cols = [row[0] for row in data.get('data') or [] if (row[3] or '') != 'TAG']
    self._columns_cache[stable] = (cols, time.monotonic())
    return cols

  def ensure_stable(
    self, stable: str, columns: dict[str, str],
    tags: tuple[tuple[str, str], ...] = TAG_COLUMNS,
  ) -> None:
    """建/补超级表。**调用方必须传「该 device_type 的全量已知列」**，不是只传新增列。

    三步、**不靠异常判断"表存不存在"**：

      1. `CREATE STABLE IF NOT EXISTS`（表已在则无副作用）
      2. `DESCRIBE` → 实际列序（权威，VALUES 按它拼）
      3. 差集 `ALTER STABLE ADD COLUMN`（表在但缺列时补齐）

    ⚠️ **为什么不能"DESCRIBE 报错就当表不存在"**：DESCRIBE 失败可能是瞬时故障；
    若因此走"建表"分支且只传了新增列，那些已注册的旧列就**永远不会被补建**，
    写入时列不存在 → **静默丢值、不报错**（实测自审发现的致命路径）。

    `ALTER STABLE ADD COLUMN` 官方是**在线、毫秒级、级联所有子表、历史填 NULL** 的元数据操作，
    所以"边收边加列"是安全的。

    `tags` 默认取设备宽表的 `TAG_COLUMNS`（**那是全库契约，别改**）；`st_pue` 这类独立超级表
    传自己的一份（见 `pue.PUE_TAGS`），不改默认值就逐字沿用原有行为。
    """
    cols = f'{COL_TS} TIMESTAMP'
    for name, typ in columns.items():
      cols += f', {name} {typ}'
    cols += f', {COL_REPORT_TS} TIMESTAMP, {COL_QUALITY} TINYINT'
    tag_defs = ', '.join(f'{n} {t}' for n, t in tags)
    self._exec(
      f'CREATE STABLE IF NOT EXISTS {stable} ({cols}) TAGS ({tag_defs})',
      db=settings.TAOS_DB,
    )
    self._columns_cache.pop(stable, None)     # 建表可能改了结构 → 缓存作废

    existing = set(self.stable_columns(stable))          # 现查，别用旧缓存
    missing = [(n, t) for n, t in columns.items() if n not in existing]
    if not missing:
      return
    for name, typ in missing:
      self._exec(f'ALTER STABLE {stable} ADD COLUMN {name} {typ}', db=settings.TAOS_DB)
    self._columns_cache.pop(stable, None)
    logger.info('TDengine 超级表加列：%s ← %s', stable, [n for n, _ in missing])

  # ── 写入 ──
  def exec_sql(self, sql: str) -> None:
    """执行一条写语句（SQL 由 `build_wide_insert` 生成）。"""
    self._exec(sql, db=settings.TAOS_DB)

  def query(self, sql: str) -> dict:
    """执行查询并转成 `{columns, records, total}` —— 这个形状能被 `analysis_load` 物化。"""
    data = self._exec(sql, db=settings.TAOS_DB)
    columns = [c[0] for c in data.get('column_meta') or []]
    rows = data.get('data') or []
    return {
      'columns': columns,
      'records': [dict(zip(columns, r)) for r in rows],
      'total': int(data.get('rows') or len(rows)),
    }

  def last_rows(
    self, stable: str, *, cluster: str | None = None, device_guids: list[str] | None = None,
    space_guid: str | None = None,
  ) -> dict[tuple[str, str], dict]:
    """每台设备的**最新一行** → `{(cluster_name, device_guid): {列名: 值}}`。

    键必须是**设备对**：guid 只在园区内唯一，跨园区查询时两个园区可能有同一个 guid
    （PG 主键 `(cluster, guid)`）。单园区时 cluster_name 是常量，行为与只按 guid 等价。

    列序取自 `DESCRIBE`（权威）。宽表下一行就含该设备所有测点的最新值。
    """
    columns = self.stable_columns(stable)
    sql = build_last_row_query(
      stable=stable, columns=columns, cluster=cluster, device_guids=device_guids,
      space_guid=space_guid,
    )
    out: dict[tuple[str, str], dict] = {}
    for rec in self.query(sql)['records']:
      guid = str(rec.get('device_guid') or '')
      if guid:
        key = (str(rec.get('cluster_name') or ''), guid)
        # 去掉 `last_row(x)` 的包装，还原成列名 → 值
        out[key] = {
          k[len('last_row('):-1]: v for k, v in rec.items()
          if k.startswith('last_row(') and k.endswith(')')
        }
    return out

  def agg_series(
    self, stable: str, *, interval: str, agg: str = 'avg',
    start_ms: int | None = None, end_ms: int | None = None,
    cluster: str | None = None, device_guids: list[str] | None = None,
    space_guid: str | None = None, limit: int = 5000, offset: int = 0,
    columns: list[str] | None = None,
  ) -> dict:
    """按时间窗**聚合**取数（查询时降采样）—— 长区间分析走这条，别拉原始值。

    结果集是「每设备 × 每窗口一行」，所以**要翻页**（`limit`/`offset`）。

    `columns` 只给**要看的那几个测点列**（缺省 = 超级表全部数据列）。宽表一台设备
    动辄上千列，整行返回会把工具输出上限（12,000 字符）直接撑满 —— 查单个测点曲线
    必须只选它的列（2026-10-22 实测：整行 1,204 列、约 29KB，6 行窗口只回得来 1 行）。
    """
    sql = build_series_agg_query(
      stable=stable, columns=columns or self.stable_columns(stable), interval=interval,
      agg=agg, start_ms=start_ms, end_ms=end_ms, cluster=cluster,
      device_guids=device_guids, space_guid=space_guid, limit=limit, offset=offset,
    )
    return self.query(sql)

  def count(self, stable: str = '') -> int:
    """超级表总行数（联调核对用）；stable 为空则统计 `dcim_realtime`（旧的窄表）。"""
    target = stable or 'dcim_realtime'
    data = self._exec(f'SELECT COUNT(*) FROM {target}', db=settings.TAOS_DB)
    try:
      return int(data['data'][0][0])
    except (KeyError, IndexError, TypeError):
      return -1


def sql_literal(s: str) -> str:
  """字符串字面量（**拼进 TDengine SQL 的字符串值都要走它**）。

  公开导出：能力层（如 `dcim_meta.check_consistency`）也要用它 ——
  两处口径不一致就是注入漏洞（自己踩过）。
  """
  return _q(s)


def _series_filters(
  *,
  start_ms: int | None,
  end_ms: int | None,
  cluster: str | None = None,
  device_guids: list[str] | None = None,
  space_guid: str | None = None,
) -> str:
  """时序查询的 WHERE 子句。三条查询（原始值 / 计数 / 聚合）**共用**它 ——
  条件不一致的话，`total` 与数据就对不上，上层翻页会漏或重复。

  `space_guid` 走的是**宽表的 tag**（`TAG_COLUMNS` 里就冗余了它）——
  所以「按机房查曲线」可以直接打在 TDengine 上，不必先去 PG 把该空间的设备 guid 全拼出来。

  ⚠️ **时间窗是硬约束**：百万测点下单点位 365 天 ≈ 315 万行，不限定就是全表扫。
  **不替调用方补默认值** —— 免得把"忘了加时间窗"掩盖成"查得慢"。
  报错文案要**能指导模型怎么补**：这条错误会冒到 `analysis_load` 里直接给模型看。
  """
  if not start_ms or not end_ms:
    raise ValueError(
      '时序查询必须带时间窗：请提供 start_ms / end_ms（毫秒时间戳）；'
      '范围大时先缩小设备范围或时间段（单次拉取有行数上限）'
    )
  sql = f'ts >= {int(start_ms)} AND ts <= {int(end_ms)}'
  if cluster:
    sql += f' AND cluster_name = {sql_literal(cluster)}'
  if space_guid:
    sql += f' AND space_guid = {sql_literal(space_guid)}'
  if device_guids:
    sql += ' AND device_guid IN (' + ','.join(sql_literal(g) for g in device_guids) + ')'
  return sql


_COLUMN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _column_name(column) -> str:
  """列名白名单校验。

  `columns` 可能来自 PG 的 `dcim_td_column`（点选列）或调用方，而它们会拼进 SQL。
  列名只允许 `[A-Za-z_][A-Za-z0-9_]*` —— 与聚合函数/时间窗白名单同一套思路。
  """
  name = str(column)
  if not _COLUMN_RE.match(name):
    raise ValueError(f'非法列名 {name!r}')
  return name


def _safe_columns(columns: list[str]) -> str:
  """校验后拼成 SELECT 列表。"""
  return ', '.join(_column_name(c) for c in columns)


def build_series_query(
  *,
  stable: str,
  start_ms: int | None = None,
  end_ms: int | None = None,
  cluster: str | None = None,
  device_guids: list[str] | None = None,
  space_guid: str | None = None,
  limit: int = 5000,
  offset: int = 0,
  columns: list[str] | None = None,
) -> str:
  """拼一条**带时间窗**的时序查询（纯函数，便于单测）。

  ⚠️ 排序必须是 `ts, device_guid`：**同一个 `ts` 可能有多行**（不同设备），
  只按 `ts` 排序时 LIMIT/OFFSET 翻页会**漏行 / 重复行**（排序列有重复值就会漂移）。
  **跨园区**（`cluster` 为空）还要加 `cluster_name`：guid 只在园区内唯一，
  `(ts, device_guid)` 不足以唯一确定一行（两个园区的同名设备会互相压行/漂页）。

  `columns` 给了就只选这几列（**必须含 `ts`/`device_guid`**，否则回来认不出时刻/设备）——
  宽表整行上千列，单行就能撑满工具输出上限。跨园区时会自动补上 `cluster_name`。
  """
  where = _series_filters(
    start_ms=start_ms, end_ms=end_ms, cluster=cluster, device_guids=device_guids,
    space_guid=space_guid,
  )
  cross_park = not cluster
  cols = None
  if columns:
    cols = (['cluster_name'] if cross_park else []) + list(columns)
  select = _safe_columns(cols) if cols else '*'
  order = 'ts, cluster_name, device_guid' if cross_park else 'ts, device_guid'
  return (f'SELECT {select} FROM {stable} WHERE {where}'
          f' ORDER BY {order} LIMIT {int(limit)} OFFSET {int(offset)}')


def build_last_row_query(
  *,
  stable: str,
  columns: list[str],
  cluster: str | None = None,
  device_guids: list[str] | None = None,
  space_guid: str | None = None,
) -> str:
  """取**每台设备的最新一行**（宽表下 = 该设备各测点的最新值）。纯函数，便于单测。

  ⚠️ 三个实测要点：

  1. **必须分组** —— 超级表上单用 `LAST_ROW()` 只返回**整体一行**，不分组拿不到"每台设备一行"。
  2. 按 **tag（`device_guid`）**分组而不是 `tbname` ——
     `subtable_name()` 会小写化、把 `-` 换成 `_`，**是有损的、反推不回原始 guid**；
     而按 tag 分组返回的就是原始 guid（实测：`CHNLFB01-00HJ-…` 大小写与 `-` 都在）。
  3. **必须连 `cluster_name` 一起分组** —— guid 只在园区内唯一，
     跨园区查询时两个园区可能有同一个 guid；只按 guid 分组会把两台设备压成一行。
     单园区时 `cluster_name` 是常量，结果与只按 guid 分组一致。

  `columns` 用 `RealtimeRepository.stable_columns()`（DESCRIBE）拿到的**真实列序**。
  """
  if not columns:
    raise ValueError('取最新值需要列清单（先用 stable_columns 拿 DESCRIBE 的列）')
  exprs = ', '.join(f'last_row({c})' for c in columns)
  where = []
  if cluster:
    where.append(f'cluster_name = {sql_literal(cluster)}')
  if space_guid:
    where.append(f'space_guid = {sql_literal(space_guid)}')
  if device_guids:
    where.append('device_guid IN (' + ','.join(sql_literal(g) for g in device_guids) + ')')
  sql = f'SELECT cluster_name, device_guid, {exprs} FROM {stable}'
  if where:
    sql += ' WHERE ' + ' AND '.join(where)
  return f'{sql} GROUP BY cluster_name, device_guid'


# 允许的聚合函数（**白名单**：它会拼进 SQL，不能由调用方任意指定）
_AGG_FUNCS = ('avg', 'max', 'min', 'sum', 'first', 'last', 'count')
# 允许的窗口写法：数字 + 时间单位（m/h/d），如 10m / 1h / 1d。同样是为了拼 SQL 的安全。
# **带捕获组**：`interval_to_ms` 要用（同一份正则，避免"校验用一套、换算用另一套"漂移）
_INTERVAL_RE = re.compile(r'^(\d+)([mhd])$')
# 窗口单位 → 毫秒
_INTERVAL_UNIT_MS = {'m': 60_000, 'h': 3_600_000, 'd': 86_400_000}


def interval_to_ms(interval: str) -> int:
  """`10m` / `1h` / `1d` → 毫秒。

  给「按窗口数推算结果行数」用：知道区间和窗口宽度，就能算出会产出多少个窗口
  （见 `dcim_meta.client._agg_total` —— 聚合结果的行数 = 设备数 × 窗口数）。
  """
  m = _INTERVAL_RE.match(str(interval or '').strip())
  if not m:
    raise ValueError(f'时间窗写法不对：{interval!r}（形如 10m / 1h / 1d）')
  return int(m.group(1)) * _INTERVAL_UNIT_MS[m.group(2)]


def build_series_agg_query(
  *,
  stable: str,
  columns: list[str],
  interval: str,
  agg: str = 'avg',
  start_ms: int | None = None,
  end_ms: int | None = None,
  cluster: str | None = None,
  device_guids: list[str] | None = None,
  space_guid: str | None = None,
  limit: int = 5000,
  offset: int = 0,
) -> str:
  """按时间窗**聚合**取数（查询时降采样）—— 治「长区间原始值拉不动」这个根本问题。

  为什么必须有它：单测点 30 天 × 10 秒 ≈ **26 万行**，远超 `analysis_load` 的 10000 行上限，
  原始值路径根本没法做长区间分析。按 `INTERVAL(1h)` 聚合后，30 天只剩 **720 行/设备**。

  ⚠️ 三个实测要点：
  1. **必须 `PARTITION BY device_guid`** —— 宽表下同一时间窗里多台设备各有各的行，
     不按设备分组会把不同设备的值混在一起聚合（实测语法可行，且分组键是 tag，可直接选出来）。
     **跨园区**（`cluster` 为空）还要带上 `cluster_name`：guid 只在园区内唯一，
     只按 guid 分区会把两个园区的同名设备**混算**。
  2. 返回的时间列是 **`_wstart`**（窗口起点），不是 `ts`。
  3. **排序必须 `_wstart, device_guid`，且带 `OFFSET`** —— 结果集是「**每设备 × 每窗口一行**」，
     所以**必须能翻页**（否则设备一多/区间一长就被 `LIMIT` 静默截断）。
     而 `_wstart` **不是唯一键**：实测多台设备在同一个窗口里的 `_wstart` 完全相同，
     单靠它排序 + OFFSET 翻页会**漂行**（同一行在第 1、2 页各出现一次）—— 加 `device_guid` 做 tiebreaker。
     跨园区时 tiebreaker 再加 `cluster_name`（否则同名设备仍会压行）。
  """
  if agg not in _AGG_FUNCS:
    raise ValueError(f'不支持的聚合函数 {agg!r}（可选：{"/".join(_AGG_FUNCS)}）')
  if not _INTERVAL_RE.match(str(interval)):
    raise ValueError(f'时间窗写法不对：{interval!r}（形如 10m / 1h / 1d）')
  data_cols = [c for c in columns if c not in SYSTEM_COLUMNS]
  if not data_cols:
    raise ValueError('聚合取数需要数据列（先用 stable_columns 拿 DESCRIBE 的列）')

  exprs = ', '.join(f'{agg}({_column_name(c)})' for c in data_cols)
  where = _series_filters(
    start_ms=start_ms, end_ms=end_ms, cluster=cluster, device_guids=device_guids,
    space_guid=space_guid,
  )
  cross_park = not cluster
  keys = 'cluster_name, device_guid' if cross_park else 'device_guid'
  select_keys = f'_wstart, {keys}, count(*) AS n'
  return (f'SELECT {select_keys}, {exprs} FROM {stable}'
          f' WHERE {where} PARTITION BY {keys} INTERVAL({interval})'
          f' ORDER BY _wstart, {keys} LIMIT {int(limit)} OFFSET {int(offset)}')


def build_series_count(
  *,
  stable: str,
  start_ms: int | None = None,
  end_ms: int | None = None,
  cluster: str | None = None,
  device_guids: list[str] | None = None,
  space_guid: str | None = None,
) -> str:
  """与 `build_series_query` **同一套 WHERE** 的总行数 —— 上层据此判断翻页结束。"""
  where = _series_filters(
    start_ms=start_ms, end_ms=end_ms, cluster=cluster, device_guids=device_guids,
    space_guid=space_guid,
  )
  return f'SELECT count(*) AS n FROM {stable} WHERE {where}'


def ensure_realtime_schema() -> None:
  """建库 + `st_pue`（幂等）。**在起消费线程之前调一次**，不要每条线程各调一次 ——
  并发 `CREATE DATABASE` / `CREATE STABLE` 会让 TDengine 报
  `Conflict transaction not completed`（实测踩过）。

  设备宽表的超级表**不在这里建**：它按设备类型分，是随数据到达按需建的（见 `ensure_stable`）。
  `st_pue` 是**固定一张**，提前建好就不会有多进程并发建表的机会。
  """
  # 延迟导入：`pue` 依赖本模块的 `_q`/`_num`/`_AGG_FUNCS`，顶层互引会成环
  from service.dcim_ingest.pue import ensure_pue_schema

  repo = RealtimeRepository()
  repo.connect()
  try:
    repo.ensure_database()
    ensure_pue_schema(repo)
  finally:
    repo.close()

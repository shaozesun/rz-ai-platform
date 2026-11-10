# DCIM 时序数据地基设计

> 状态：**设计稿（待评审）** · 2026-10-01
> 范围：DCIM 实时数据（测点数值）的存储架构 —— 数据模型、分层、部署、接入与查询。
> 配套：接入侧见 `dcim-kafka-ingest-design.md`、部署见 `dcim-kafka-deploy-manual.md`、
> 向对方确认见 `dcim-kafka-待确认清单.md`。
> 本文里**标 ⚠️ 的是「推断/待验证」，标 ✓ 的是「已实测或有官方出处」** —— 请勿把 ⚠️ 当结论用。

---

## 一、背景与目标

| 项 | 现状 / 目标 |
|---|---|
| 数据来源 | DCIM 通过 Kafka 推送，**每个园区一个独立集群**，共 **13 个园区** |
| 规模 | **百万级测点**，**两种推送机制**（实测确认，2026-10-08 问过对方）——见下表。行数**不能按固定 10 秒推** |
| 现在 | 只有廊坊B1M2 在试推（108 测点 / 36 设备），本机 Docker 里的 TDengine 只是**联调验证用** |
| 要支撑 | 监控看板 · 告警 · **跨园区对比（PUE 等）** · **节能推演 / 仿真 / 计算** |
| 部署 | 独立数据库机（**PVE 上的 VM**，实机规格 + 虚拟化参数见 **§6.1**），tar 包 + systemd，**社区版**（不买企业版） |

### 实时数据的两种推送机制（2026-10-08 问清，**此前本文档误称「变化推送」**）

| | 机制 | 特征 |
|---|---|---|
| ① | **定期推送** | 每 **10 秒**，一般带齐该设备的全部测点 |
| ② | **即时推送** | **底层采集一上来就立刻推一条** —— 与"值有没有变化"**无关**（此前误写成"值变化才推"） |

⚠️ **机制②的包不一定含全部测点**（刚采到哪个就推哪个）→ 这是**结构性**的，不是 bug、也不是可配置项。

实测（廊坊B1M2，一台设备 11.5 小时）：10 秒间隔占 **54%**、其余 1~9 秒占 **46%**，
**总行数比纯 10 秒周期多约 33%**；照原样写的话 `ht_temp` 会有 **13~15% 的行**是空。

**三个直接推论**：

1. **容量估算不能按"固定 10 秒"推** —— 行数取决于**底层采集速率**，比 10 秒周期更多
2. **推送密度不均匀 → 原始值里有大量重复值**（实测一分钟 8 行、同一个值出现 7 次）。
   所以 **`avg()` 是按推送次数加权、不是按时间加权** —— 算"平均温度/日均能耗"必须先按时间
   重采样（或走 `INTERVAL` 聚合每窗口取一个值），**不能直接对原始行求平均**
   （⚠️ 这条**不受**下面的写入侧补齐影响：补齐只填列，不改变"推得多密"）
3. **同一 `(设备, 时间戳)` 会被推两次、内容不同**（实测 10 分钟 88 个时间戳里 3 个）。
   我方按 `(子表, ts)` 后写覆盖、且缺的列写 NULL → 后到的部分包会把完整行覆盖成 NULL。
   详见 `docs/dcim-kafka-待确认清单.md` 第 18 项

### 写入侧的应对：同 ts 合并 + 限长零阶保持（已实现）

`service/dcim_ingest/wide_writer.py::merge_values`。**两条规则**：

1. **同 `ts`** → 合并（治上面第 3 条：部分包不再冲掉完整行）
2. **不同 `ts`、间隔 ≤ `DCIM_CARRY_MAX_SECONDS`（默认 60 秒）** → **零阶保持**：把上一个值带过来。
   物理上是**对的** —— 10 秒采样的传感器，两次采样之间它的值就是保持上一个。
   → 于是**正常写入的行都是完整快照**（不再稀疏），做「能耗 = 电压 × 电流」这类
   **同行多列联合计算**不用先对齐

⚠️ **限长是必须的**：设备离线一小时若无脑补齐会补成一条**直线**、**掩盖故障**。
超过 60 秒**留空** —— "这段时间没数据"是**应该被看见**的信息。
（正常推送间隔 10 秒 → 60 是 6 倍余量：正常稀疏都补上、真断档留白。）

⚠️ **SQL 只带本批用到的列**（2026-11-02）：`build_insert_body` 按实际出现的测点生成列清单，
未列出的列由 TDengine 填 NULL。这与"逐列写 `'NULL'`"在语义上等价（上面第 3 条所描述
的覆盖行为不变），但超级表有 4,000+ 列时单行 SQL 从 ~30KB 降到百字节级 —— 实测设备只上报
自己那几十个测点（抽 16 列 `count(col)`，15 列为 0），全列写是实时消费吞吐的主因。

⚠️ **历史行不回填**：实现之前写进库的洞留着（那段时间的真实情况就是那样）；回填是昂贵且不可逆的批量操作。

**为什么现在就要设计**：`vgroups` / `maxTablesPerVnode` 这类参数**建库后不可改**，
数据模型一旦定错，写入端要全部重写。真数据进来之前是唯一低成本的窗口。

### 写入侧的应对：合并 INSERT **按语句大小分片**（2026-10-16 修）

`service/dcim_ingest/wide_writer.py::_chunk_blocks`。为什么必须有：

- REST 下**一条语句 = 一次 HTTP 往返**，所以把一批设备**拼进同一条 `INSERT`**（`build_multi_device_insert`）。
- 但 TDengine 默认 `maxSQLLength` = **1MB**，超了整条报 `537 SQL statement too long`：
  合写失败 → 退回逐台重试（数据不丢），**而 taosAdapter 会把整条语句写进错误日志**。
- 实测：`st_...00ag` 这张超表 **883 列**，一次 29 台 × 每台约 6 行 ≈ **2.66MB** →
  每秒一条 2.66MB 错误日志 ≈ **4~6GB/小时**，把 30G 系统盘写满（TDengine 转而报
  `code:299 Out of disk space`，平台侧 `dcim_meta/client.py` 吞掉异常 → **所有测点值为空**）。

分片依据**从「设备台数」改为「语句字符数」**（`_MAX_INSERT_CHARS=900000`，留 ~10% 余量），
台数上限（`_DEVICES_PER_INSERT=200`）保留作窄表的次级约束。⚠️ `200` 是按**窄表**（一行几列）
定的，宽表一行几百列时它能拼出十几 MB —— **只按台数分片是不够的**。

### 写入侧的应对：下发并行 + 失败分类 + 子表换名（2026-11-02）

**为什么**（生产只读实测，廊坊A）：稀疏列写入后约 **2,816 行/s**，仍追不上入流。`perf_apps`
显示全系统写入 109 req/s、21.8 ms/req ⇒ 服务端同时在途仅 **2.38 条**；同期 taosd 只用
3.9/16 核、55% CPU 空闲、盘 40~52 MB/s。**瓶颈在客户端串行下发**，不在库。

1. **准备 / 下发两段**（`WideWriter.write`）：准备（`_prepare_type`，调用线程）查档案、
   `allocate_columns_locked`（**PG 事务，不能跨线程**）、零阶保持、`stable_columns`、拼 SQL
   与分片；下发（`_exec_chunk`，`ThreadPoolExecutor`，`DCIM_WRITE_CONCURRENCY` 默认 4）
   只发已拼好的字符串。所有 chunk 一次提交、逐个 `future.result()` 收齐后才归档。
2. **失败分类**：非设备级错误（超时 / HTTP 5xx / 连接失败）**原样抛出** → `_flush` 的
   5 次退避重试启动；设备级错误（`_DEVICE_ERROR_MARKERS`：`already exists in other stables`、
   `Invalid table name`、`Too many columns`、`invalid tag`）才允许逐台重试后跳过那一台。
   旧实现一律吞异常 + 自动提交 offset ⇒ **失败即永久丢**，重试代码是死代码。
3. **子表换名**：设备的 `device_type` 变过之后，`d_<园区代号>_<guid>` 已绑在旧超级表上，写新表报
   `Table already exists in other stables`（30 分钟 229 次、约 30 台）。用
   `td_schema.subtable_variant()`（原名 + `sha1(device_type)[:6]`）换名重试，成功后记入
   `_sub_override`，后续批次直接用新名。旧子表/旧数据不删不改 —— 读侧按 tag 查超级表，
   不依赖子表名。
4. **实时消费改手动提交**：`KAFKA_REAL_DATA` 不再 `auto_commit=True`，`_flush` 在
   `store.write()` 成功后 `commit(asynchronous=False)`。代价是每批一次同步提交（~1~5 ms）；
   失败不提交、重连重放，按 `(子表, ts)` 覆盖幂等。
5. **线程边界**：`_devices`/`_last`/`_code_to_col`/`_sub_override` 只在准备阶段访问，worker
   只读入参；计数由 worker 返回增量、调用线程累加（不加锁）。`close()` 必须**先**
   `pool.shutdown(wait=True)` 再关 TD/PG。

自检：`python -m service.dcim_ingest.tests.test_parallel_write`（并发上限、串并行等价、
设备隔离、瞬时错误必抛、子表换名缓存）。

---

## 二、设计原则

1. **统一接入** —— 一个入口解析、补全、路由；接入层不做业务。
2. **按用途分层** —— 不同用途不同存储。**不要把一切塞进一张大表，也不要拆成海量小表。**
3. **按时间分区** —— 时序数据按时间切片，查询靠分区裁剪，过期靠切片删除。
4. **按 hash 分片** —— 让写入与存储横向摊开。
5. **热冷分离** —— 近期高频查询与全量归档走不同介质。
6. **元数据与时序分离** —— 定义（空间/设备/测点/阈值）在 PostgreSQL，数值在 TDengine。
7. **先做能确定的，把不确定的留成扩展点** —— 不拿未验证的假设当地基。

---

## 三、架构总览

```
┌ ⓪ 统一接入 ────────────────────────────────────────────────────────┐
│ DCIM Kafka（只读，对方 broker，园区各一个）                          │
│   → Ingestion Service（解析 / 补 cluster·空间·设备类型 / 统一 schema）│
│   → Stream Router · Processor（可插拔 sink）                        │
└───────────────────────────────────────────────────────────────────┘
        │
        ├─① 最新状态层
        │   ①a 全量「最近一条」  TDengine CACHEMODEL='last_row' + LAST_ROW()
        │   ①b 业务权威当前态    PostgreSQL 独立表，每测点 1 行，持续 upsert
        │                        （**只放重要测点**，量级万级）
        │
        ├─② 原始明细层         TDengine，宽表（一设备一子表、测点做列）
        │                       DURATION=1d（时间分区）· KEEP=90d
        │
        ├─③ 聚合层             TDengine 流计算从②派生，档位互为倍数
        │                       重要测点 1min+1h；其余只 1h
        │
        ├─④ 画像层             ⏳ 后续阶段·暂不实现
        │                      设备/系统能效画像、特征向量 → Parquet(离线)+PG/Redis(在线)
        │
        ├─⑤ 仿真结果层         ⏳ 后续阶段·暂不实现
        │                      what-if 推演产物，必须可版本追溯 → PG + 文件
        │
        └─⑥ 归档层             全量历史，**不走高频查询**
                               MinIO + Parquet（dt 分区）+ DuckDB 直读
```

各层一览：

| 层 | 用途 | 存储 | 保留 | 关键点 |
|---|---|---|---|---|
| ①a | 快速读"最近一条" | TDengine `last_row` 缓存 | 随② | 免扫盘；随②写入自动维护，**无需额外写** |
| ①b | 业务权威当前态（可 JOIN 元数据） | **PostgreSQL** | 永久 | **只放重要测点**（PG 上限约 5k~10k upsert/秒 ✓） |
| ② | 近期明细查询、原始回放、故障定位 | **TDengine**（宽表） | 90 天 | 时间分区 + 自动过期；最重的写入 |
| ③ | 趋势图、能耗/PUE 分析、报表 | **TDengine**（流计算派生） | 1min 短 / 1h 长 | 档位互为倍数；**按测点价值分级** |
| ④ | 能效画像、ML 特征 | Parquet(离线) + PG/Redis(在线) | 视用途 | **后续阶段 · 暂不实现**（表结构取决于以后实际做哪些分析） |
| ⑤ | what-if 推演结果 | PG(元信息) + 文件(结果集) | 永久 | **后续阶段 · 暂不实现**（记录参数/时间/操作人，要求可复现） |
| ⑥ | 合规留存、全量重算的源 | **MinIO + Parquet** | 永久 | DuckDB 直读；社区版无内置冷热分层 → 自建。**归档全量（不区分是否过期），重复的那份兼作热层备份** |

---

## 四、数据模型（核心）

### 4.1 结论：**宽表** —— 一设备一子表、测点做列

TDengine 的两层模型：

| 层 | 对应什么 | 说明 |
|---|---|---|
| **超级表（STable）** | **一种设备类型**（如所有智能电表 = `meter_table`） | 定义有哪些列、哪些 tag |
| **子表（Subtable）** | **一台物理设备** | 从超级表继承结构，**自带自己的 tag 值** |

> ⚠️ **硬约束**：子表的 tag 值在**创建时固定**，所以「一张子表 = 一组固定 tag 值 = 一台具体设备」。
> 一个设备类型有 N 台设备，就需要 N 张子表 —— **无法让多台设备共用一张子表**。

**子表名必须带园区代号（2026-11-03）**：设备 guid **只在园区内唯一**（PG `dcim_devices`
主键是 `(cluster, guid)`，实测 652 个 guid 跨园区重复），而子表名是**全库唯一**。若两个园区
写同一个 `d_<guid>`，后写的数据会因 tag 创建即固定而**静默落进先建的那张子表**、带**别的园区
的 `cluster_name`** —— 按园区查询永远看不到它，本园区查询则混入别人的值。故子表名统一为
`d_<园区代号>_<guid>`（代号见 `service/dcim_ingest/park_codes.py`，归一化后小写：`LF-` →
`d_lf_`），`devices_meta` 查询也一律带 `cluster`。

一行 = **某台设备在某一时刻的一组测点值**（测点做列）：
天然时间对齐、写入最省（对方本来就按设备组包推送）、压缩好。

**读侧的园区身份（2026-11-03）**：写侧换了子表名，读侧也必须按 `(cluster, guid)` 认设备 ——
`cluster` 不传 = 跨所有园区（能力描述承诺"记录自带 `cluster`"），此时所有读路径：

- 最新值 `last_rows`：`GROUP BY cluster_name, device_guid`，返回键 `(cluster_name, guid)`；
- 原始序列：显式列时补 `cluster_name`，`ORDER BY ts, cluster_name, device_guid`（分页 tiebreaker 唯一）；
- 聚合序列：`PARTITION BY cluster_name, device_guid`，`ORDER BY _wstart, cluster_name, device_guid`；
- 拆组合并键 `(ts, cluster_name, device_guid)`、跨类型合并排序同三键；
- 设备类型反查按行遍历（同名 guid 可能属不同设备类型 → 两张超级表都要查）；
- `total` / `n_devices` 数**不同 `(cluster, guid)` 对**，不能用 `len(guids)`（否则下界 → `analysis_load` 提前停、静默丢数据）；
- 点位/阈值记录补 `cluster`；一致性巡检跨园区比较设备对、差异项写 `cluster/guid`。

**单园区（传了 `cluster`）SQL 与输出逐字不变** —— 新分支只在 `cluster` 为空时生效。



### 4.2 设备特别大时：按「设备实例 + 点位组」再拆

一台设备测点特别多（如大型机组下几百个传感器）时，在 tag 里再加一个
**`point_group` 维度**：同一台设备产生多张子表，每张装一组测点做列。

```
子表 = (设备实例, 点位组)      tag: cluster_name, device_guid, point_group, space_guid, device_type, idc
```

### 4.2.1 容量与拆组（2026-10-26 实测 + 实现）

⚠️ **之前写的「列数控制在 ≤50」是官方建议值，生产上没有做到，也做不到**：

- **硬上限 4,096 列/表，3.3.0.0 上改不了**：`SHOW CREATE DATABASE dcim` 里没有 `MAX_COLUMNS`
  （3.3.0 还没这个库级参数，`taos.cfg` 也没有）→ 只能靠升级 TDengine 抬高，且抬到 8,192 也只是把墙推远
  （单行宽度已近 32KB）。**决定不升库、改拆表。**
- 实测 96 张超级表里最大 `st_00000000_00ag_…` = **4,071 列**（4,068 数据列 + 3 系统列）/ `00wg` 4,043，
  余量只剩 19~25 列 → 再来一个新测点，`ALTER STABLE ADD COLUMN` 直接抛错，
  该设备类型整批入库失败，Kafka 偏移反复重放**永久卡死**。

**规则（`td_schema.py` 是唯一来源）**：

| 项 | 值 |
|---|---|
| 容量线 `GROUP_MAX_COLUMNS` | **4,000 数据列/组**（+3 系统列 +6 tag = 4,009，距 4,096 留 87） |
| 预警线 `GROUP_WARN_COLUMNS` | **3,900**（分配后打 WARNING，撞墙前可见） |
| 超级表名 | `g0` 沿用 `st_<type>`（存量零迁移），`g1` 起 `st_<type>_g<n>` |
| 子表名 | `d_<园区代号>_<guid>`（代号见 `park_codes.py`，归一化后小写），非 g0 再加 `_g<n>` 后缀 |
| 分配 | **惰性溢出**：存量编码永不换组；新编码按 g0→g1… 顺序填（`sorted` 保证可复现） |
| 落库 | `dcim_td_column.point_group` + `dcim_td_stable (device_type, point_group)` |
| 提交顺序 | 先 ALTER 建 TDengine 表、成功后才提交 PG 映射（失败 rollback，重放分到同样的名字） |

**写侧代价**：每次写入按「存在的组」各写一整行（组内无值的列照旧零阶保持），
各组行数因此一致。**读侧必须合并**：`client._series_for_type` 逐组取数后用
`_merge_rows_by_key` 按 `(ts, cluster_name, device_guid)` 并成一列 × 一行（列取并集 ——
`UNIQUE (device_type, td_column)` 保证列名跨组也不重名），`total = max(各组 total)`（不是求和）。
合并键必须带 `cluster_name`：guid 只在园区内唯一，跨园区同名设备是两台，按 `(ts, guid)`
合并会把它们的列并进同一行（详见 §4.1「读侧的园区身份」）。

**幽灵映射与对账工具（2026-10-26 生产实测）**：拆组上线当天 `廊坊A/real` 消费者卡在
`TDengine 报错: Too many columns` —— `00WG` 在 PG 里登记 **6,775** 列（全在 g0），
TDengine 实际只有 **4,043** 个业务列，**2,732 个「幽灵映射」**是旧提交顺序（先提交 PG → 再 ALTER）
在历史 ALTER 失败（根盘写满）时留下的：这些测点从未写进 TDengine，但每次 `ensure_stable` 都要
发 `CREATE STABLE IF NOT EXISTS (6,778 列)`，TDengine 即使表已存在也校验列数 → 永久重试。
修法：`deploy/scripts/dcim_group_reconcile.py`（默认 dry-run）**只把「PG 有、TDengine 没有」的列**
按真实空位挪到 `g1…`，有数据的列一律不动；写完不重启，写侧下一次 `ensure_stable` 自动建 g1。
实测搬迁后 g0 = 4,043（存量不搬，故意超容量线）、g1 = 2,732，消费者 1 秒内恢复。
复盘见 `docs/problems/dcim-phantom-columns-consumer-stall.md`。

### 4.3 schema 示例

⚠️ 下面列名与分组**是示意**，真实列要由「设备类型 → 测点集合」字典生成（见 4.6）。

```sql
CREATE DATABASE dcim_raw
  VGROUPS   32            -- 建后不可改；= VM vCPU × 2（⚠️「核数」指【实分给 VM 的 vCPU】，非宿主总核数）
  DURATION  1d            -- 时间分区：每 1 天一个数据文件切片
  KEEP      90            -- 90 天后最老切片自动删除（文件数≈90，落在建议的 10~100）
  BUFFER    256           -- 每 vnode 写缓冲(MB)，⚠️ 32 vgroups × 256MB = 8GB 内存（定值规则见 §6.3）
  CACHEMODEL 'last_row'   -- ①a 靠它：每子表最后一行驻留内存
  WAL_LEVEL 1
  PRECISION 'ms'
  COMP      2;

-- 一种设备类型一张超级表
CREATE STABLE meter_table (
  ts        TIMESTAMP,
  voltage   DOUBLE,        -- ← 来自 point_code
  current   DOUBLE,
  power     DOUBLE,
  energy    DOUBLE,
  quality   TINYINT        -- 质量位 0正常/1坏值/2补值（可选）
) TAGS (
  cluster_name BINARY(32), -- 园区（来自消息所属集群，不是报文字段）
  device_guid  BINARY(64),
  point_group  BINARY(16), -- 默认 'g0'；大设备按点位组拆时用
  space_guid   BINARY(64), -- 所属空间（接入时补齐）
  device_type  BINARY(48), -- 设备类型（接入时补齐）
  idc          BINARY(64)
);

-- 子表：一台设备（或 设备+点位组）一张，靠 INSERT 自动建
-- 带**列清单**：只写本批真实采样的列，未列出的列 TDengine 自动填 NULL
-- （超级表动辄 4,000+ 列，逐列写 'NULL' 会让单行 SQL ~30KB，是吞吐瓶颈）
INSERT INTO d_<园区代号>_<device_guid> USING meter_table TAGS(...) (ts, power, energy) VALUES (...), (...);
```

**为什么 tag 里要塞 `space_guid` / `device_type`**：⚠️ 因为 **TDengine 不能 JOIN PG**。
有了这两个冗余 tag，"某机房所有设备的功率曲线"可以**直接打在 TDengine 上**，
不必先去 PG 捞一堆 guid —— **这是跨库查询的地基**（tag 共 6 个，官方建议 ≤10，且都必须是稳定属性）。

### 4.4 列名映射（point_code → 列名）必须持久化

测点编码是字符串（`HT_Temp`），不能裸当列名（可能含非法字符、超长、冲突）。需要：

- **归一化规则**（大小写、非法字符替换、长度截断）
- **冲突去重**（归一化后重名 → 加后缀）
- **映射落库**（在 PG，与 `dcim_points` 关联）—— **否则进程重启后列名对不上**

### 4.5 schema 自适应（实现关键点）

真数据会不断带来新东西，写入端必须能自动应对：

| 情况 | 动作 |
|---|---|
| 见到新 `device_type` | `CREATE STABLE`（列 = 该类型已知测点集合） |
| 已有超级表出现新 `point_code` | `ALTER STABLE ADD COLUMN`（官方：在线、毫秒级、级联所有子表、历史填 NULL） |
| 某类型测点集合**不稳定**或**列数超限** | **回退窄表**（该类型单独建窄表超级表） |

### 4.6 宽表前提：已用**我们自己的真实数据**验证 ✅

**前提**：同类型设备的测点集合固定且一致 —— 这是宽表能否成立的唯一前提。

**验证（2026-10-01，廊坊B1M2 真实 topic 落库数据）**：

```
每台设备的测点数（按设备类型）
device_type 10BC-… | 22 台设备 | min=3 max=3 distinct_shapes=1   ← 每台恰好 3 个
device_type 20BC-… | 14 台设备 | min=3 max=3 distinct_shapes=1   ← 每台恰好 3 个

同类型内测点集合签名（string_agg 排序后分组）
device_type 10BC-… | HT_Hum,HT_Status_Comm,HT_Temp | 22 台      ← 只有 1 组签名
device_type 20BC-… | HT_Hum,HT_Status_Comm,HT_Temp | 14 台      ← 只有 1 组签名
```

→ **同类型内每台设备的测点集合 100% 一致，无选配、无异构。前提成立。**

**样本局限（如实写明，别当已完全证实）**：仅 **1 个园区 / 2 个设备类型**，
且都是**温湿度类**（每台 3 个测点）。真正的 **电表 · UPS · PDU · 冷机**等大设备类型**尚未进来** ——
那些类型的测点数会多得多，选配的可能性也大得多。
**权威确认靠向 DCIM 索取「设备类型 → 测点集合」字典**（已加入 `dcim-kafka-待确认清单.md`）。

**兜底设计（风险不作为推翻方案的理由，但必须有退路）**

| 情况 | 动作 |
|---|---|
| 某类型出现**新测点** | `ALTER STABLE ADD COLUMN`（官方：在线、毫秒级、级联子表、历史填 NULL） |
| 某类型**确实选配/异构** | **只对该类型**回退窄表（多超级表共存）；或用虚拟表拼宽视图（见 4.7） |
| 列数超限 | 拆「设备实例 + 点位组」（见 4.2）；硬上限 4096 含 tag，**官方建议 ≤50** |
| 某类型列大量常 NULL | 官方存储引擎对稀疏宽表自动走 KV 行编码（省 30~50% 存储，但随机列访问慢 2~3 倍）——**这时就该回退窄表** |

**另一条独立的设计约束（与宽表无关，但同样是实测教训）**：
行业里「**文档编码规范 vs 实际推送编码**」不一致**极常见，主因是固件版本差异**
（我们自己也撞到：文档示例是 `1001000` 数字码，实推是 `HT_Temp` 字符串）。
→ 所以**列名/表名的映射必须落在元数据层（PG），不要硬编码进代码**。

### 4.7 查询层增强：虚拟表（可选，**不是**存储模型的替代）

TDengine **虚拟表 VTable**（v3.3.6.0+）是一张**不存数据、只读**的逻辑表：
查询时从多张真实表读列、**按时间戳对齐**，相同时间戳合并成一行，缺失列填 NULL（可 padding 插值）。
官方对它的用途描述原文：

> "存储层按采集点分布，业务层按设备统一呈现"

**用在哪（两种，都不改存储模型）**：

1. **跨设备对齐** —— 把不同设备的同类指标对齐到同一时间轴（对比分析、PUE 计算直接可用）
2. **异构设备兜底** —— 万一某类型测点集合不一致，那些设备仍进窄表，用虚拟表拼出宽视图

**为什么不做成主存储方案**：我们的数据已证明同类型测点集合一致（4.6），
宽表本身就是最省事的形态；虚拟表是**查询时动态计算**，有对齐与 NULL 处理开销，只该按需用在它擅长的地方。

⚠️ **待实测**：**社区版是否包含虚拟表** —— 官方 3.3.6.0 发布说明同时列了社区版/企业版新增功能，
但检索结果**未单独确认虚拟表的版本归属**。上线前用 `CREATE VTABLE` 实测一次即可确认。

---

## 五、分片与分区

| 维度 | 做法 | 依据 |
|---|---|---|
| **hash 分片** | TDengine **自动**按**子表名**一致性哈希分到 vgroup；**不能按 device_guid/point_guid 指定** | ✓ 官方架构 |
| 影响分布 | 只能靠**子表命名**（如 `d_<园区代号>_<device_guid>`，园区代号是固定前缀、guid 本身随机 → 天然分散）+ `minTablesPerVnode` / `tableIncStepPerVnode` | ✓ |
| **时间分区** | `DURATION` 就是时间分区（落盘按此切片）；`KEEP` 超期**自动删最老切片** | ✓ 官方建库参数 |
| 文件数控制 | ≈ `KEEP/DURATION`，官方建议 **10~100** → `1d/90d` = 90 ✓ | ✓ |
| 归档分区 | Parquet 目录 `dt=YYYY-MM-DD/`（可加 `bucket=hash%N`），查询靠分区裁剪 | ✓ 业界通行 |

**vgroups 怎么定**：子表数 = 设备数 ×（点位组数）。两条经验值**互相印证**：

| 口径 | 依据 |
|---|---|
| **每核服务 1~2 个 vnode**（`vnode = vgroups × 副本数`；单副本时 1 核 : 1~2 vgroup） | ✓ **官方** Capacity Planning Guide |
| **单 vgroup 约 10 万~50 万子表** | 调研（子表上限讨论）→ 百万测点约需 **10~50 个 vgroup** |
| 每 vnode 2~10 万张表 | 官方社区经验值 |

> ⚠️ **2026-10-09 更正**：此表此前写的是「每 **1~2 核**一个 vgroup」——**方向写反了**。
> 官方口径是「**一个核服务 1~2 个 vnode**」。官方例：**100 vgroups × 3 副本 = 300 vnode
> → 150~300 核**（=300 ÷ 1~2）。按错的写法 16 核只给 8~16 个 vgroup，**少算了一半到四分之三**。

**⚠️ 这个「核数」指「实分给这台 VM 的核数」，不是宿主的总核数**

2026-10-09 实际踩到：目标宿主是 **24 核的 Xeon Gold 5418Y**，但它**同时跑着其他 VM**，
数据库 VM 只分到 **16 vCPU**。按宿主 24 核推 → 24~48；按实分的 16 推 → 16~32。**两者差一倍。**

> **共享（虚拟化）机器上，`VGROUPS` 必须按「分给这台 VM 的 vCPU 数」算。**
> 按宿主总核数算 = 假设自己独占，而 vgroup 数一旦建库就改不了（见下）。

本机取值：**VM 16 vCPU × 2（官方上限 1 核 : 2 vnode）= `VGROUPS 32`**。
最终值仍**必须用压测确认**，并以落地版本 `SHOW CREATE DATABASE` 核对默认值。

⚠️ **两个不可逆的坑**

1. **`VGROUPS` 建库时定死、事后极难改** → 第一次建库就要定对（配合 §6.3）。
2. **大规模批量改 tag 会把 taosd CPU 打满且不回落**（有 50 万子表批量改 tag 的真实事故记录）
   → **tag 设计要一次到位**，别指望以后批量调整。这也是为什么 §4.3 把
   `cluster_name`/`space_guid`/`device_type` 这些维度**在建表时就放进 tag**。

**建库参数的「可改性」速查**（决定哪些必须一次定对、哪些可以先给保守值再调）：

| 参数 | 事后可改？ | 怎么改 |
|---|---|---|
| **`VGROUPS`** | ❌ **不可改**（不可减少；视版本或可增加） | 只能建库前定对 |
| `DURATION` / `KEEP` | ✅ | `ALTER DATABASE dcim KEEP 365d`（须 `KEEP ≥ 3×DURATION`） |
| `BUFFER` | ✅ | `ALTER DATABASE dcim BUFFER <MB>`（改大改小都行） |
| `CACHEMODEL` | ✅ | `ALTER DATABASE dcim CACHEMODEL 'last_row'` |
| `COMP` / `WAL_LEVEL` / `PRECISION` | ✅ | `ALTER DATABASE ...` |

→ **推论**：`VGROUPS` 是唯一不能「先保守、后调大」的，所以它按**近期实分到的核数**定；
   与内存绑定的 `BUFFER` 可以先给小值，等 VM 内存定了再 `ALTER` 调（规则见 §6.3）。

---

## 六、部署（第二台机器）

### 6.1 目标机（实机规格 + 虚拟化参数）

> ⚠️ **这台机器上的 CPU / 内存 / 磁盘与其他 VM 共享** —— 规格要按**分给本 VM 的部分**看，
> 不是宿主总量（`VGROUPS` 的换算尤其受影响，见 §5）。

| 项 | 实机（宿主） | 给本 VM | 说明 |
|---|---|---|---|
| CPU | Xeon Gold **5418Y**（24 核 / 48 线程） | **16 vCPU** | ⚠️ 宿主 **2/3 给了本 VM，其他 VM 只剩 8 核** → 上线前必须与实际跑在其他 VM 上的负载核对 |
| 内存 | **~251 GiB**（8×32GB 已插满，`free -g` 实测） | **64 GB** | 决定 `BUFFER`（§6.3）；以后关机可加，**不用返工** |
| 数据盘 | 8×20TB HDD（`WUH722020CLE604`） | **8 块整盘 PCI 直通** | 见下「存储怎么接的」 |
| 系统盘 | — | **64 GiB** | |

**存储怎么接的**（2026-10-12 实测确认，**与最初设想不同**）：

```
MegaRAID 卡（Broadcom/LSI SAS3916）以【PCI 直通】给 VM `td`，且卡处于【JBOD/直通模式】
  → VM 里 lsblk 直接看到 8 块裸盘 sdb..sdi（看到的是硬盘型号，不是卡的虚拟盘）
  → PVE 宿主看不到这些盘（卡与盘都归 VM）
存储 = 【在 VM 内】mdadm 软件 RAID6 + ext4 —— 宿主完全不参与
```

⚠️ **采购单写「RAIDZ2 阵列」，实际实现是 RAID6** —— 两者**功能等价**（都双校验、扛 2 块盘坏），
但**实现不同**：RAIDZ2 = **ZFS 软件**；本方案 = **Linux `mdadm` 软件 RAID6**。
**别拿着"RAIDZ2"去找 ZFS** —— 这台机器上**没有 ZFS**。

**为什么不用「硬件 RAID6」（让卡做）**：
① 配硬件 RAID 必须搞到 `storcli2`（Broadcom 注册 / 华为支持账号），而**进不去 BIOS、也没有 iBMC** → 拿不到工具就走不下去；
② 卡的优势是 4GB 写回缓存应付**高并发随机写**，我们是**顺序追加**（~0.26 MB/s）用不上；
③ **硬件 RAID 的阵列绑卡**（卡坏了要找同型号卡才能导入），**mdadm 的阵列绑 Linux**（换机也能 `--assemble`）。

**虚拟化参数**（PVE 建 VM 时）：

| 项 | 值 | 为什么 |
|---|---|---|
| Machine / BIOS | `q35` / **OVMF (UEFI)** + EFI Disk | 现代默认 |
| SCSI 控制器 | **VirtIO SCSI single** | 每盘独立 IO 线程 |
| 系统盘 Cache | **None** | `writeback` 掉电丢数据，DB 不能要 |
| CPU Type | **host** | 单节点不迁移，无兼容顾虑，拿满 CPU 特性 |
| Ballooning | **关** | 内存被动态抽走 = 延迟抖动，DB 最怕 |
| 数据盘 | **8 块整盘 PCI 直通**（**不是**虚拟盘） | 见上 |

⚠️ **不要超配**：给 VM 的 vCPU ≤ 宿主物理核数，否则延迟抖动 —— DB 最怕。
⚠️ **直通的两个代价**：① PVE **无法快照/备份这 8 块盘**（不归宿主）；
② VM 必须随宿主自启（`Options → Start at boot = Yes`），否则宿主重启后这些盘**悬空没人管**。

- **系统装 Ubuntu 22.04 Server**（在 TDengine `3.3.0.0` 的支持矩阵内；**24.04 不在**）
- **实机已就绪（2026-10-13）**：主机名 **`td`** · 静态 IP **`10.100.17.101/24`**（网关 `10.100.17.1`）
  · 16 vCPU / 64GB / 系统盘 64 GiB · 时区 `Asia/Shanghai` · `qemu-guest-agent` 已装
- **数据盘已就绪**：`mdadm` RAID6 **`/dev/md0` = 109.14 TiB（120 TB）**，`ext4`（`-m 1`）挂 **`/var/lib/taos`**；
  `mdadm.conf` + `initramfs` + `fstab(UUID)` **均已持久化**（重启不会散架）
- **TDengine 已装**：`3.3.0.0`（与现网容器**同版本**），`taosd` + `taosadapter` 已起，
  `dcim` 库已按 §6.3 建好；**不启用** taosKeeper / taosExplorer

> ⚠️ **fqdn 用了 IP `10.100.17.101`，不是主机名 `td`** —— 因为 Ubuntu 默认 `/etc/hosts` 把
> `td` 指向 **`127.0.1.1`**（回环），拿它当 fqdn 会让 taosd **绑到回环、别的机器永远连不上**
> （TDengine 部署的经典坑）。**fqdn 建库后不可改**（数据目录与它绑定），所以第一次就定对。

### 6.2 版本 —— 装 **3.3.x**，与现网对齐

- **没有 TDengine 4.x**，最新是 **3.4.x**
- ⚠️ **新机不升 3.4，装 `3.3.x`**：现网容器（`deploy/docker-compose.yaml` 的 `tdengine` 服务）
  跑的是 **`tdengine/tdengine:3.3.0.0`**；而 **3.4.0.0 起禁止改配置文件调参**（要改用 `ALTER`），
  两套版本的参数注入方式不一样，混着上必踩坑 → **升级单独排期，别和数据迁移混在一起做**
- 3.4 那些"升级坑"（不支持滚动升级、客户端要一起升）对我们影响小：**worker 走 REST(httpx)，不依赖客户端库**

### 6.3 建库参数（第一次建库前定死）

> ⚠️ **2026-10-07 更正**：本节此前笼统写"`VGROUPS`/`DURATION`/`KEEP` 都不可改"，**不准确**。
> 按官方 DDL 参考复核后：**只有 `VGROUPS` 建后不可改**（至少不可减少），
> `DURATION` / `KEEP` / `CACHEMODEL` 都支持 `ALTER DATABASE`（`KEEP` 须 ≥ 3×`DURATION`）。

| 项 | 取值 | 事后可改？ |
|---|---|---|
| `VGROUPS` | **32**（= VM 16 vCPU × 2，官方上限 1 核 : 2 vnode，算法见 §5） | ❌ **建后不可改**（不可减少，视版本或可增加）→ 建库前必须定对 |
| `maxTablesPerVnode` | 默认 100 万；只要 vgroups 给够就**不用改** | ❌ 系统级、创建后不能动态改 |
| `DURATION` / `KEEP` | `1d` / `90d` | ✅ `ALTER DATABASE ... KEEP 365d` |
| `CACHEMODEL` | `last_row` | ✅ `ALTER DATABASE ... CACHEMODEL 'last_row'` |
| `COMP` / `BUFFER` / `WAL_LEVEL` / `PRECISION` | `2` / **`128`** / `1` / `'ms'` | ✅ 可改；**显式写死只为换版本不漂**。`BUFFER` 的定值规则见下 |
| `fqdn` | 固定域名 / 固定 hostname | ❌ dnode 绑定 `fqdn:port`，**数据目录不能跨 fqdn 拷贝** ✓ |

**定值依据（生产库实测 108 测点 / 815 万行 / 7.4 天）**：**10,140 行/测点/天**（≈10 秒周期）·
压缩后 **2.25 字节/行**（原始 24 字节/行 → **压缩比 ≈10:1**，列级 `delta-i`/`delta-d` + `lz4`/`medium`，
库级 `COMP 2`）→ **22.8 KB/测点/天**。据此 90 天：10 万测点≈205GB、100 万测点≈2.0TB。
**WAL 是另一笔账**：不受 `KEEP` 约束，稳态 ≈ `写入速率 × 回收间隔`（实测 966MB，是数据本体的 53 倍）。

**`BUFFER` 怎么定**：**先算上限（防吃光内存），再按实际负载往下取**。

```
上限：BUFFER(MB) × VGROUPS ≲ VM 内存的 25%
本例：VM 64GB → 上限 512（32 × 512MB = 16GB）
实取：256（TDengine 默认）—— 我们的负载远小于「内存受限」的场景
```

| VM 内存 | 上限 `BUFFER` | 实际占用（32 vgroups） |
|---|---|---|
| 16 GB | 128 | 4 GB |
| 32 GB | 256 | 8 GB |
| **64 GB（本机）** | 512 | 16 GB |

⚠️ **上限不是目标**：我们稳态写入只有 **~0.26 MB/s（压缩后）**，`BUFFER` 加大也用不上。
取 **256（= 8GB，VM 内存的 12.5%）** 已远超需求，**剩下 56GB 留给 page cache**（TDengine 读性能靠它）。
`BUFFER` **可 `ALTER`** —— 真发现写入吞吐不够再往上调（`VGROUPS` 则不可改，见 §5）。

### 6.4 社区版缺失项 → 自建替代

| 企业版专属 | 社区版替代 |
|---|---|
| 多级存储（SSD→HDD→S3 自动迁移） | 明细库短 `KEEP` + **归档 sink 写 Parquet 到 MinIO** |
| RSMA 自动降采样并删原始 | **流计算**派生聚合表 + `KEEP` 过期原始 |
| TLS 加密 | 端口只放内网 + 防火墙白名单（**社区版 REST 默认明文**） |

### 6.5 装机清单

本地盘（禁 NFS/网络盘）· **NTP 时钟同步** ·
`taosd.service` 显式 `LimitNOFILE=1048576`（`limits.conf` 对 systemd **不生效** ✓）·
**防复发配置照搬**：`logKeepDays 3`、`numOfLogLines 1000000`、
`telemetryReporting 0`、`crashReporting 0` —— 不配就是 2026-09-30 那次事故重演
（udfd 死循环 → fd 耗尽 → taosd 死 → 单文件日志 45 GB）·
**不启用** taosExplorer / taosKeeper · 端口只放内网。

> ⚠️ **`udf 0` 在 3.3.0.0 的 `taos.cfg` 里没有这个配置项**（只有 `udfDebugFlag`）。
> 现网容器是靠环境变量 `TAOS_UDF=0` 注入的；**裸机版没有对应开关** —— 这点待核实
> （若 UDF 默认开启，udfd 进程仍存在，需靠 `logKeepDays` 防止日志失控）。

**本次实机的存储配置（在 VM 内，非宿主）**：

```bash
# 8 块整盘直通进 VM 后，用 mdadm 组 RAID6（不碰 RAID 卡）
mdadm --create /dev/md0 --level=6 --raid-devices=8 --bitmap=internal /dev/sd{b..i}
mkfs.ext4 -m 1 -L dcim-data /dev/md0     # -m 1：保留块 5%→1%，109TB 上省下 4TB+
# 挂 /var/lib/taos，并持久化（mdadm.conf + initramfs + fstab 用 UUID）
```

- ⚠️ **`--bitmap=internal` 必须加**：没有它，断电/重启后要全盘重扫 20TB×8；有了只重扫变化区域
- ⚠️ **RAID6 首次初始化要跑很久**（8×20TB，实机约 **20~30 小时**，期间阵列可用但偏慢）；
  初始化期间建 `VGROUPS 32` 的库会很慢（每个 vnode 的 `fsync` 被拖到几十秒）→
  **可先 `echo idle > /sys/block/md0/md/sync_action` 暂停初始化，建完库再 `echo resync` 恢复**
- ⚠️ **`mkfs`/挂载/`mdadm --create` 都是不可逆的** —— 执行前确认盘上没有要留的数据
- ❌ **不用 `--assume-clean`**：新阵列的校验位会按"全零"假定，而盘上还留着旧数据 → 阵列不自洽

---

## 七、接入与扇出

```
DCIM Kafka(只读) → Ingestion Service → Stream Router/Processor → sinks
                                        ├ TDengine 宽表（批量多表 INSERT）
                                        ├ PostgreSQL 业务当前态表（只重要测点，批量 upsert）
                                        ├ 归档 sink（按天攒 Parquet → MinIO）
                                        └ ①a last_row 随宽表写入自动维护，无需单独 sink
                     ③聚合由 TDengine 流计算从②派生（不经路由）
```

**实现方式**

- **先走「应用内路由」**：Python 写消费服务，**扇出目标做成可插拔 sink**。
  理由：团队是 Python 栈；Kafka 消费者组**天然支持按分区水平扩展**（多副本 = 更多消费者）；
  sink 接口定死之后，将来换流处理框架不影响下游。
- **「流处理框架」（Flink / Spark Structured Streaming / Kafka Streams）留作演进** ——
  当压测证明 Python 在 10 万行/秒下扛不住、或需要复杂窗口/状态计算时再上。
  ⚠️ **这是必须压测验证的假设**，不要先假定 Python 行或不行。
- **建议加一条内部总线**（自己的 Kafka/Redpanda，保留 **24~48h**）：解耦 + 短故障回放
  （各层互不拖累、归档慢不阻塞实时）。
  **长期"改逻辑重算"不依赖它 —— 靠⑥归档层（Parquet 全量）重放**，所以总线保留期可以很短。

**语义**：配置类 at-least-once（写成功才提交）；实时值可丢（auto-commit），
但**进入 sink 后要幂等**（TDengine 靠 `(子表, ts)` 覆盖天然幂等 ✓）。

### 7.1 ⚠️ 两条必须写下来的边界

**① 中转/园区侧丢的数据，我方找不回**

我们不拥有对方的园区侧：Kafka broker、topic、以及园区到中转机的链路**都在对方手里**。
所以——

- **"断网续传"是对方的责任**，不是我们的
- 己方内部总线**只能缓冲已送达中转链路的数据**；从没送达的，谁也找不回
- 日后再出现"数据缺失"，**先分清是"没推过来"还是"我们没收到"**，别默认判成我方故障

**② 单机资源竞争（⚠️ 现状与目标要分清）**

- **现状**：TDengine + Ingestion Service + 平台本体（backend / postgres / Mongo / Milvus / Langfuse…）
  **全挤在平台服务器这一台**上（`docker-compose.yaml` 与 `docker-compose.dcim-ingest.yaml` 叠加）——
  这正是要拆的理由。
- **目标**：TDengine 独立到 **§6.1 那台 PVE VM**；**Ingestion Service 仍留在平台侧**。

如果将来还打算把 Ingestion / MinIO / 归档任务也搬到数据库机（**目前没有这个计划**），
那台 VM 的内存与磁盘就得**提前分账**：

- ⚠️ **内部总线（Kafka）是演进项、不是现状** —— 现在 broker 与三个 topic 都由 **DCIM 提供、我方只消费**
  （见 `docker-compose.dcim-ingest.yaml` 顶部）。将来若要自建，先评估是否**真需要完整 Kafka**
  （可换轻量缓冲，省一半资源）—— 见 §9 实施顺序
- 建议在压测阶段就把资源占用打出来（§9 第 3 步）

---

## 八、跨库查询

PG（定义/拓扑）与 TDengine（数值）**不能做数据库层 JOIN**（PG 没有 TD 的 FDW，跨库 JOIN 做不到）。
所以一律**两段式**：

> ⚠️ **更正（2026-10-07 实测）**：本节此前还写了「TDengine 3.x 不支持多表 JOIN」——**这条不成立**。
> 在本机 TDengine **3.3.0.0** 上实测窄表 `dcim_realtime`（799 万行）自连接
> （`a.ts=b.ts AND a.device_guid=b.device_guid`）可正常返回，取 3 行耗时 0.55s。
> 该论断原本是**未验证的假设**，不构成选宽表的理由。**宽表的真正收益是对齐成本与行数**（§4）：
> 窄表要按 `(设备, ts)` 做 **k 路 self-join**（测点越多路数越多），宽表对齐是**结构性的**；
> 且现网 36 台设备 × 3 测点下 799 万行 → 约 285 万行，测点越多的类型倍数越大。

```
① PG：按空间/设备/中文名筛 → 得到目标 guid（或直接靠 tag 过滤，见 4.3）
② TDengine：按 guid / tag + 时间窗取数（tag 有索引）
③ 应用层（pandas）合并 / 对齐
```

**封装在 `service/query/dcim_meta/`**，不让 Agent 自己拼两个库的查询。能力清单：

| id | 说明 |
|---|---|
| `dcim_meta.query_space_tree` | 空间树（递归 CTE） |
| `dcim_meta.query_devices` | 按空间/类型/名称查设备 |
| `dcim_meta.query_points` | 按设备/名称查测点定义（单位、状态映射、4 档告警阈值） |
| `dcim_meta.query_series` | 按设备/测点查时序：**强制时间窗**，长区间自动降采样 |
| `dcim_meta.build_aligned_table` | 生成"设备 × 时刻 × 指标"对齐宽表 → 物化进沙箱（**推演入口**） |
| `dcim_meta.check_consistency` | guid 一致性巡检（TD ↔ PG），**常驻**：关联是拼接约定，对方改命名规则会静默断掉 |

**硬约束**（写进能力描述）：必须带时间窗（单测点 365 天 × 10 秒 ≈ 315 万行）；
长区间必须降采样；返回结构含 `list/records/rows/items/data/result` 之一 + `total`，
才能被 `analysis_load` 物化进沙箱。

---

## 九、实施顺序

1. **先拿数字**（DCIM）：设备数 / 测点数 / **设备类型数与「类型 → 测点集合」字典** /
   点位编码规范（数字码 vs 字符串）/ 每设备测点数分布 —— 决定宽表能否成立、`VGROUPS` 取多少
2. 第二台装机 + 建库（规格见 §6.1，参数按 §6.3 定死；**装 3.3.x 不升 3.4**，见 §6.2）
3. **压测**（`deploy/scripts/dcim_mock_producer.py` 放大到百万测点 × 10 秒）：
   写入行/秒、**vnode/表分布是否均匀**、磁盘增长、**Ingestion Service 的 Python 吞吐**
4. 接入改造：Ingestion Service + 可插拔 sink + 列名映射表 + 重要测点分级表（PG）
5. 第二台就绪 + 压测过 → **再请 DCIM 推全量**（顺序不能反）
6. 改 `TAOS_HOST` → 本机删 `tdengine` 服务 + 清卷

---

## 十、验证

```bash
# 建库参数（建后立刻查，别等灌数据）
# ⚠️ 用 SHOW CREATE DATABASE —— 3.3.0.0 上 `select ... from information_schema.ins_databases`
#    会报 `syntax error`（实测，2026-10-13）
curl -u root:$PW .../rest/sql -d 'show create database dcim'
# 期望 DDL 里含：VGROUPS 32 / DURATION 1440m / KEEP 129600m / BUFFER 256 /
#               CACHEMODEL 'last_row' / COMP 2 / WAL_LEVEL 1 / PRECISION 'ms'
# 超级表按设备类型建出来了吗、列对不对
curl -u root:$PW .../rest/sql/dcim_raw -d 'show stables'
curl -u root:$PW .../rest/sql/dcim_raw -d 'describe meter_table'
# 一行多列是否生效（同一时刻多测点在同一行）
curl -u root:$PW .../rest/sql/dcim_raw -d 'select * from d_<园区代号>_<guid> limit 5'
# 最新值快读（不扫历史）
curl -u root:$PW .../rest/sql/dcim_raw -d 'select last_row(ts) from meter_table'
# vnode / 表分布是否均匀（压测后）
curl -u root:$PW .../rest/sql -d 'show vgroups'
# 业务当前态表（只含重要测点）
psql -c 'select count(*) from dcim_point_latest'      # ≈ 重要测点数，不是百万
# 归档层可查
duckdb -c "select count(*) from 'archive/dt=2026-10-01/*.parquet'"
```

---

## 十一、待确认（向 DCIM）

> 完整清单见 `dcim-kafka-待确认清单.md`（P1 第 10/11 条即下面 1/2 条）。

| # | 事项 | 影响 |
|---|---|---|
| 1 | **「设备类型 → 测点集合」字典**（含**选配测点**标注） | 有字典才能**预先建好宽表的列**；没有就只能边收边 `ALTER ADD COLUMN` |
| 2 | **点位 `id` 编码规范**：`1001000` 数字标准码 还是 `HT_Temp` 字符串？是否会变 | 决定**列名映射**规则与稳定性 |
| 3 | 真实设备数 / 测点数 / 设备类型数 / 每设备测点数分布 | 决定 `VGROUPS`、是否要拆点位组 |
| 4 | 同类型设备的测点集合是否**完全一致**（选配测点的比例） | 稀疏程度、是否要对某类型回退窄表 |
| 5 | `idc` 推空串、`device_name` 未推（另见 `dcim-kafka-待确认清单.md`） | 字段可用性 |

---

## 附：调研依据摘要（关键事实与出处）

| 事实 | 出处 |
|---|---|
| 官方建模首选取「一设备一张表、测点做列」；测点集合可变时用测点窄表 | [Data Modeling Practice Guide](https://tdengine.com/data-modeling-practice-guide-super-table-creation/) |
| 不同设备类型测点集合不同 → 按设备类型建多个超级表 | 同上 |
| 子表按**表名**一致性哈希分片，不能指定分片键 | [技术内幕·架构](https://gitcode.net/taosdata/tdengine/-/blob/20baf373a082378e475852fa5733ee055674ad31/docs/zh/21-tdinternal/01-arch.md) |
| `DURATION` = 时间分区；`KEEP` 超期自动删切片 | [Database 参数](https://docs.taosdata.com/3.4.0/reference/taos-sql/database/) |
| `CACHEMODEL='last_row'` + `LAST_ROW()` 免扫盘读最新值 | [读缓存](https://docs.tdengine.com/tdengine-sql/data-query/cache-query/) |
| 列上限 4096（含 tag），**建议 ≤50**；`ALTER STABLE ADD COLUMN` 在线毫秒级 | [Limit](https://docs.tdengine.com/3.2/taos-sql/limit/) |
| 多级存储 / 对象存储 / RSMA = **企业版专属** | [版本功能对比](https://www.taosdata.com/feature-comparison) |
| 新版流计算自 v3.3.7.0 起 | [流式计算](https://docs.taosdata.com/3.4.1/reference/taos-sql/stream/) |
| PG 批量 upsert 上限约 5k~10k 点/秒 | [ThingsBoard DB 架构](https://docs.sensef.ru/docs/reference/architecture/database/) |
| 业界分层三段式（最新值独立 + 明细时序 + 冷层对象存储） | Lindorm / ThingsBoard / Timestream / 华为 IoTDA |
| 归档用对象存储 + Parquet + 列式引擎直读 | [DuckDB/Parquet](https://duckdb.org/duckdb-docs.pdf) |
| 接入扇出推荐「Kafka → 各层各自消费」 | Kappa 事件流架构 |
| **虚拟表 VTable**（3.3.6.0+）：存储按采集点分布、业务按设备统一呈现 | [虚拟表机制](https://tdengine.com/virtual-table-mechanism-in-tsdb/) · [发布说明](https://www.taosdata.com/time-series-databases/28840.html) |
| **不支持跨库查询** → 多园区用「单库 + site tag」 | [多租户权限](https://tdengine.com/multi-tenant-tsdb-permission-management/) |
| vgroup 建议**每核服务 1~2 个 vnode**（`vnode = vgroups × 副本`；官方例 100 vgroup × 3 副本 → 150~300 核）；单 vgroup 约 10 万~50 万子表 | [Capacity Planning Guide](https://tdengine.com/deployment-capacity-planning-guide/) · [子表上限](https://ask.taosdata.com/t/topic/1791) |
| 官方容量规划指南的按规模参考配置：**50 万~100 万元素 → 32 核 / 64 GB**；CPU 使用率建议 **≤50%** | 同上 |
| ⚠️ **ZFS 不在官方支持矩阵**：官方存储建议**只提 `ext4`/`xfs`**，多次检索**零提及 ZFS** → 未验证路径 | 同上 |
| ⚠️ 官方（IDMP 容量规划）**"生产环境建议使用 SSD"**；机械盘只说"寿命长 / 成本低" | [IDMP Planning](https://idmpdocs.taosdata.com/operation/planning/) |
| ⚠️ **批量改 tag 会把 taosd CPU 打满不回落**（50 万子表事故） | [tag 事故帖](https://ask.taosdata.com/t/tag-taosd-cpu/1016) |
| 行业点位值多为**窄表**：Zabbix(`itemid+clock+value`) / Prometheus(metric+labels) / 华为 NetEco / 施耐德 EcoStruxure IT | [Zabbix 表结构](https://www.cnblogs.com/kyle-7Qc/p/20521465) · [Prometheus 数据模型](https://prometheus.io/docs/concepts/data_model/) |
| 设备模板普遍**支持可选测点**（BACnet object-list 各不同、Modbus 含 Reserved 寄存器） | [OpenDCIM 表结构](https://deepwiki.com/opendcim/openDCIM/2.1-database-schema-and-installation) · [Bosch 测点模板](https://learn.bosch-nexeed.com/master_data_management/device_types/manage_or_create_a_new_measuring_point_for_a_device_type.html) |
| 「文档码 vs 实推码」不一致**极常见**，主因固件版本差异 | [阿里云产线采集踩坑](https://developer.aliyun.com/article/1761958) |

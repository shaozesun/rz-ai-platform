# DCIM 实时接入单进程 GIL 饱和：多进程分片 + 热点优化（2026-11-02）

## 结论

实时接入跑不快**不是** TDengine、也不是下发并发不够，而是**单进程 GIL 饱和**：一个进程里
所有消费线程共享一把解释器锁，CPU 只用满 1 核，加线程、加 `DCIM_WRITE_CONCURRENCY` 都不涨。
修法是**按进程拆 GIL**（`DCIM_INGEST_PROCESSES`，默认 1 = 旧行为），同时把每行都要跑的
逐列扫描热点改成反查 + 预计算。

- 单进程实测：**廊坊A 生产 5,690 msg/s，实际只消费 875 msg/s**，实时 lag 涨到 **14.99 亿条**
- 容器 CPU **≈1.07 核**、cgroup **无 quota**、宿主机 **12 核约 90% 空闲**
- `top -H`：3 条 Python 线程合计 ≈100%（单核打满），是 GIL 的典型形态
- 修复后预期：**约 40k~47k 行/s**（6 倍进程 × 热点约 15%），积压约 **1.6 天**消化完

## 证据

### 1. 只有 3 个园区有数据

| 园区 | 分区数 | 生产速率 |
|---|---|---|
| 廊坊A | 16 | **5,690 msg/s** |
| 廊坊B1M1 | 1 | 236 msg/s |
| 廊坊B1M2 | 9 | 395 msg/s |
| 其余 8 个园区 | — | 0 |

因此「扩吞吐」的实际含义只有一个：把**廊坊A** 的消费能力提上去。按园区切进程会失败
（廊坊A 的线程全落进同一个进程，那个进程仍是单 GIL）。

### 2. 单进程已经打满，且不是 I/O 等待

- `docker stats`：容器 CPU ≈1.07 核；cgroup 未设 quota；宿主机 12 核、约 90% 空闲
  → 不是资源不够，是**用不起来**
- `top -H`：几条消费线程合计 ≈100%，其余线程在等锁
- 提高 `DCIM_REALTIME_CONCURRENCY` 或 `DCIM_WRITE_CONCURRENCY` 均无提升 → 排除「并发不足」

### 3. cProfile：每行 175µs，全是解释器内的纯 Python

| 热点 | 每行耗时 |
|---|---|
| `build_insert_body` | 48µs |
| `_used_columns`（逐列扫描 4,000+ 列） | 26µs |
| `_num` | 18µs |
| `merge_values`（零阶保持） | ≈28µs |

单进程 6.6k 行/s ≈ 1/175µs，与容器 CPU 1.07 核对得上 —— **耗时就是 GIL 里的 Python 代码**。

## 落地

### A. 多进程分片（`DCIM_INGEST_PROCESSES`）

- `service/dcim_ingest/config.py` 的 `shard_slice(items, shard, shards)`：按**扁平序号轮询**
  （`i % shards == shard`）。轮询而不是按园区切，是为了让廊坊A 的多条实时线程散到尽量多的
  进程上（纯函数，便于自检）
- `service/dcim_ingest/__main__.py`：`run()` 在 fork 前校验配置、幂等建 PG/TDengine 库结构
  （并发 CREATE 会互相撞 `Conflict transaction not completed`），然后 `_run_processes()` 起
  N 个子进程；父进程只做看护：
  - 任一子进程**非预期退出** → 停掉其余进程、返回非零 → 容器 `restart: unless-stopped`
  - 收到 SIGTERM → 转发 `terminate()`，20s 未退再 `kill()`
  - 线程数总数与 PG 连接总数**不变**，只是分摊到各进程（每条消费线程各持一条 PG 连接）
- 心跳文件 `/tmp/dcim-ingest.alive` 由所有进程的消费线程共刷，healthcheck 语义不变
- `multiprocessing.get_context()`：Linux（生产镜像 `python:3.11-slim`）= fork（COW 省内存、
  秒起）；macOS = spawn。fork 时**还没有任何线程**，且建库用的连接已关闭
- 子表新增列导致的进程间 `_COLUMNS_CACHE` 落后**会自愈**：任何本进程没见过的新编码都会走
  `allocate_columns_locked` → `ensure_stable()` 逐组重新 DESCRIBE，把本进程缓存刷新
  （`codes <= known.keys()` 的短路只在「本批没有新编码」时生效）

### B. 热点优化（`realtime._used_columns` / `build_insert_body`）

- 旧实现每台设备**逐列扫描**超级表全部列（00AG 已 4,071 列）判断「这列本批有没有采样」，
  实测 **≈26µs/行**。新实现改为「按本批测点**反查列** + 按列序号排序」，只碰本设备上报的
  几十个测点，输出顺序**仍严格等于 DESCRIBE 顺序**
- `code_to_col` / `col_index` 由 `WideWriter._prepare_type` 按「设备类型 × 组」预计算一次；
  `build_insert_body` 再把列清单预展开成 `(列名, 测点编码)`，免去每行每列的 `col_to_code.get`
- **语义逐字不变**：`service/dcim_ingest/tests/test_shard_columns.py` 保留了旧实现原文，
  用 400 组随机用例（打乱 DESCRIBE 顺序、未映射编码、None 值、可选 `report_ts`、0~4 条采样）
  断言新旧输出**完全相同**

### C. 配置

```dotenv
# deploy/config/.env.production
DCIM_REALTIME_CONCURRENCY=8   # 必须 ≥ 进程数，否则有进程分不到实时线程（启动日志会告警）
DCIM_INGEST_PROCESSES=6
```

资源：常驻内存约 **6~7G**（容器上限 10g）、CPU 约 **6 核**（宿主机 12 核）。

## 自检

```bash
python -m service.dcim_ingest.tests.test_shard_columns   # 分片轮询 + 稀疏列与旧实现逐字一致
python -m service.dcim_ingest.tests.test_sparse_insert   # 只写真实采样的列
python -m service.dcim_ingest.tests.test_parallel_write  # 并行下发 / 失败分类 / 子表换名
python -m service.dcim_ingest.tests.test_group_split     # 测点拆组
```

部署后（只读）核对：

1. `docker stats rz-dcim-ingest`：CPU 应涨到 **5~6 核**，内存 6~7G 以内
2. `docker exec rz-dcim-ingest ps -eo pid,pcpu,comm --sort=-pcpu | head`：应有 **6 个
   python 进程**各占约 1 核（旧形态是 1 个进程 + 几条线程合计 1 核）
3. `docker logs --since 10m rz-dcim-ingest | grep '分片'`：6 行（每进程一行），
   廊坊A 的实时线程不集中在同一个 pid
4. `docker logs ... | grep 'lag='`：廊坊A 的 lag 应**停止增长并开始下降**
5. `docker logs ... | grep -c '写 TDengine 失败'` → 0
6. TDengine 侧行速（`perf_apps` 增量）：从 6.6k 行/s 目标 **≥40k 行/s**

## 边界

- **不提高单进程能力上限**：每个进程仍是单 GIL、约 6.6k 行/s；要再提速只能继续加进程
  （受宿主机核数与内存约束）
- **lag 消化是逐步的**：14.99 亿条按约 40k 行/s 需要约 1.6 天；期间新数据仍在进来，
  真实收敛时间会略长
- **手动提交、at-least-once 不变**：写库成功才提交，进程被杀后按 `(子表, ts)` 覆盖重放；
  子进程异常退出 → 整容器重启 → 重放，不会丢
- **配置类/告警类线程也一起被分片**（它们闲置为主），不影响语义；配置 topic 每小时全量
  重推，删掉的定义会自己回来
- 进程数 > 廊坊A 的实时线程数时，多余进程只跑配置类线程 —— 所以要求
  `DCIM_REALTIME_CONCURRENCY >= DCIM_INGEST_PROCESSES`

## 第二阶段：批次放大是反效果（2026-11-03 实测，已回退）

多进程上线后廊坊A 从 875 → 约 1,800 msg/s（落库约 5,050 行/s），但 lag 仍以 **+4,064 msg/s**
增长。诊断指向写侧：客户端在途上限 = 8 条实时线程 × `DCIM_WRITE_CONCURRENCY`(4) = **32**，
与 `perf_apps.current_req` 恒为 32 吻合；每条 INSERT 仅约 45 行却横跨 11~32 个 vgroup
（`sub_num` p50=11 / p90=27）。据此把 `KAFKA_BATCH_SIZE` 500 → 2000，想让每子表每批多写几行、
把固定开销摊薄。**结果完全相反**：

| 指标 | batch=500 | batch=2000 |
|---|---|---|
| 语句数（廊坊A 8 线程合计） | ≈108/s | ≈44/s（降到 1/4，符合预期） |
| 行/语句 | ≈45 | ≈96~136（符合预期） |
| taosd 单请求耗时 | ≈128ms | **≈1.2s** |
| 客户端单批耗时 | p50 1.69s / max 12.7s | **41~118s** |
| `写 TDengine 失败` | 0 | **104 次/10 分钟**（`TDengine 请求失败: timed out`，httpx `timeout=30s`） |
| 廊坊A 落库行速 | ≈5,049 行/s | ≈4,199 行/s（**回落**） |
| lag 增速 | +4,064 msg/s | +3,788 msg/s（无改善） |
| 容器内存 | 5.1 GiB | 6.8 GiB |

**结论：taosd 的单请求耗时随语句规模超线性增长，加大批次是「放大」而不是「摊薄」。**
切分逻辑本身没问题（语句数确实降到 1/4），问题在服务端 —— 4,096 列宽表、136 行/语句、
跨 32 个 vgroup 的写入，处理时间涨了约 10 倍。已回退到 500。

**顺带确认的两件事**（都指向同一个方向）：

- 客户端此时 `docker stats` 只有 **2.5% CPU**、`ps` 各进程 14~22%（生命周期均值）——
  完全在阻塞等待 HTTP 响应，本地已无瓶颈，`DCIM_WRITE_CONCURRENCY` 再调大只会把更多
  请求压到已经拥塞的服务端。
- 对 TDengine REST 的**只读查询**也开始变慢：`select server_version()` 0.12~0.37s，
  `performance_schema.perf_apps` 单次 **3~30s**（此前毫秒级）→ taosAdapter/taosd 侧排队严重。

**下一步必须先拿到 taosd 宿主机指标**（`iostat -xm 2 5` / `top -b -n1` / `df -h` / `free -m`），
才能区分是磁盘 fsync（`walFsyncPeriod=3000ms`、单 dnode 串行 WAL）还是 CPU/内存。
在此之前不要再调大批次或并发——两个方向都只会加重拥塞。

## 第三阶段：真正的瓶颈是 RAID6 月度校验（2026-11-03）

批次回滚后仍然慢，上机取 taosd 宿主指标才发现：**TDengine 虚拟机 `/var/lib/taos` 所在的
8 盘 RAID6 正在跑月度 `check`**，`sync_speed_max` 被加速到 200 MB/s，8 块盘每块读约
183 MB/s、忙碌 83~90%，WAL fsync 排在流式校验读之后。限速到 50 MB/s 后 `写 TDengine 失败`
从 104 次/10 分钟归零、落库耗时回到 1~3s、行速升到 8,830 行/s；但磁盘降到 40% 忙后
**taosd CPU 成为新瓶颈（16 核占 9~13 核）**，lag 仍以约 +4,584 msg/s 增长。

结论：**「客户端加并发/加批次」在这个状态下都是错的方向**。完整证据、排障顺序与边界见
`docs/problems/dcim-tdengine-raid-check-io-starvation.md`。

## 来源

- 实测数据来自 2026-11-02 生产容器（服务器 118.126.142.38）的 `docker stats`、`top -H`、
  `cProfile` 与 worker 日志；TDengine 侧走 REST `perf_apps`
- 关联文档：`docs/dcim-kafka-ingest-design.md`（链路与 topic）、
  `docs/dcim-timeseries-storage-design.md` §4.2.1（拆组与 4,096 列上限）

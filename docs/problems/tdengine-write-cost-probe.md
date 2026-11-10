# TDengine 宽表写入成本对照实验（2026-11-04）

## 结论

1. **每行写入成本不随列数线性增长，但在宽表区间（≥1,000 列）明显超线性。**
   30 列 → 4.3 µs/行；400 列 → 7.4 µs/行（列数 ×13，成本 ×1.7）；4,000 列 → 124.3 µs/行（列数 ×133，成本 ×29）。
2. **主要放大器是 `CACHEMODEL 'last_row'`，不是压缩。**
   同一张 4,000 列超级表，关掉 last_row 缓存后每行 CPU 从 124.3 µs 降到 22.0 µs（**降 82%**，吞吐 8,650 → 28,021 行/s）；
   而 `COMP 2 → COMP 0` 几乎无变化（124.3 → 126.1 µs）。压缩调参不是出路。
3. 因此后续优化应优先考虑「去掉/替换 last_row 缓存」，而不是调压缩、调 vgroup。
4. **已在生产验证**：`dcim` 关缓存后写入吞吐 7,718 → 15k~26k 行/s（CPU 575% → ~265%），读侧无退化（见下节）。

## 实验方法

- 位置：生产 TDengine 3.3.0.0（10.100.17.101，16 核），独立探针库 `rz_probe` / `rz_probe2` / `rz_probe3`，各 `VGROUPS 1`，`MAXROWS 4096`、`DURATION 1440m`、`KEEP 7d`（`KEEP` 必须 ≥ 3×DURATION，原计划的 `KEEP 1d` 被 TDengine 拒绝）。
- 变量只保留「列数 / 压缩 / 缓存」三项，其余一致：`ts` + `v1..vN` DOUBLE + `TAGS(device_guid, cluster_name)`。
- 负载：4 线程 × 50 行/语句，每行只写 30 个非空列（模拟设备只报几十个测点），每个变体固定压 60 秒。
- 指标：`/proc/<taosd_pid>/stat` 的 `utime+stime` 差值 → **CPU 秒/行**；吞吐行/s 为参考。
- 开工前先停 `rz-dcim-ingest`（否则探针 CPU 无法归因，测到的是排队时间），测完已恢复。
- 空载基线：taosd 0.3% of one core。

## 结果

| 变体 | 行数 | 行/s | CPU·s / 60s | µs/行 |
|---|---:|---:|---:|---:|
| 30 列 · COMP2 · last_row | 1,896,650 | 31,610 | 8.17 | 4.3 |
| 400 列 · COMP2 · last_row | 1,719,100 | 28,651 | 12.78 | 7.4 |
| 4,000 列 · COMP2 · last_row | 519,150 | 8,650 | 64.55 | **124.3** |
| 4,000 列 · **COMP0** · last_row | 533,050 | 8,882 | 67.20 | 126.1 |
| 4,000 列 · COMP2 · **无缓存** | 1,681,400 | 28,021 | 36.96 | **22.0** |

宽/窄每行成本比 = **28.9×**。

## 生产基数

- `dcim` 当前：`VGROUPS 32`、**`CACHEMODEL 'none'`（2026-11-04 04:30 起，原为 `last_row`）**、`COMP 2`、`MAXROWS 4096`、118 张超级表 / 157,718 张子表。
- 超级表列数分布：3 张 ≥3,000 列（4,071 / 4,046 / 3,278）、7 张 1,000~3,000、2 张 500~1,000、15 张 200~500、91 张 <200（中位 23 列）。
- 生产实测：约 8,300~9,100 行/s 时 taosd ≈10/16 核（≈1.1~1.2 ms/行），比探针宽表（0.124 ms/行）高约一个数量级。

## 差异与不确定项

探针只用了每库 1 个 vgroup、每个变体 4 个子表；生产是 32 个 vgroup、15 万多张子表。生产每行成本高一个数量级，最可能的解释是 **last_row 缓存在海量子表上的内存拷贝/分配开销**（4,000 列 × 8B ≈ 32KB/子表），但这一点没有直接测量，属推断。

## 生产实跑：`ALTER DATABASE dcim CACHEMODEL 'none'`（2026-11-04 04:30~04:45）

在探针结论的基础上，对生产库 `dcim` 做了一次可逆的在线试跑（不重启 taosd，`information_schema.ins_databases` 立即变为 `cachemodel='none'`；`SHOW CREATE DATABASE` 会显示旧值，不要拿它判断）。

| 时点 | 写入行/s | taosd CPU | µs/行（含排队） |
|---|---:|---:|---:|
| 改前（last_row） | 7,718 | 575% of one core | ≈745 |
| 改后 +1min | 21,732 | 264% | ≈84 |
| 改后 +3min | 25,696 | 255% | ≈72 |
| 改后 +6min | 18,832 | — | — |
| 改后 +12min（读测试期间） | 14,770 | 266% | ≈130 |

**写入侧**：吞吐约 2~3×，CPU 约 −54%。与探针的 −82% 同级（生产仍有 Kafka/PG/合并等固定开销）。

**读侧**（同一时段，REST 直连 + 真实后端路径）：

| 查询 | 缓存 ON（此前） | 缓存 OFF（实测） |
|---|---:|---:|
| 1 设备 1 列 `last_row` + GROUP BY | 0.03s | 0.03s |
| 5 设备 1 列 `last_row` + GROUP BY | 0.02s | 0.02s |
| 1 设备全 4,071 列 `last_row` + GROUP BY | 7.54s | 5.99s |
| `_query_points(device, with_latest)`（61 点，4,071 列宽表） | 未测 | 6.93s |
| `_query_points(keyword='温度', with_latest)`（取 300 点、跨多设备类型） | 未测 | 13.44s |
| `_query_over_threshold(device)` | 未测 | 0.10s |
| `_query_over_threshold(keyword='温度')`（扫描 5,000 点） | 未测 | 19.42s |

**结论：读侧没有出现退化**（宽表 `last_row` 反而略快，因为省掉了维护缓存的开销；其余为原有量级）。`with_latest` 在 4,000 列宽表上本来就要 7 秒级，与本改动无关。

**没解决的部分**：`dcim-ingest` 现在约 6k 行/s 消费 廊坊A，而该园区生产约 7k 行/s；7 个实时分区的 lag 各 ≈1.87 亿条、仍以每分区 ~+4k/30s 缓慢增长。瓶颈已经从 taosd CPU 转到消费端（当前 `DCIM_INGEST_PROCESSES=6`、`DCIM_REALTIME_CONCURRENCY=8`），不再是宽表写入。

## 建议的下一步

1. ~~生产试跑 `CACHEMODEL 'none'`~~ 已完成，见上节。
2. **保持 `CACHEMODEL 'none'`**（除非出现未覆盖的读路径退化）。若将来要恢复：`ALTER DATABASE dcim CACHEMODEL 'last_row'`。
3. 若要继续提吞吐，方向是消费端与库存结构，不是 TDengine 参数：
   - `DCIM_INGEST_PROCESSES` / `DCIM_REALTIME_CONCURRENCY` 继续加（受 PG 连接数与 CPU 约束，每进程约 1 核）；
   - 宽表拆分（`00AG` 4,071 列按测点分组）已具备，可评估是否把其余 ≥1,000 列的超级表也拆组；
   - 若读侧将来在意 `last_row` 的秒级延迟，可改客户端 `last(col)` + `CACHEMODEL 'last_value'`，需另行验证。

## 复现

探针脚本与执行步骤见本次会话记录；关键点：先停 ingest worker、建 3 个 `VGROUPS 1` 的探针库、5 个变体各 60s、读 `/proc/<pid>/stat` 算 CPU，最后 DROP 三个探针库并重启 worker。

> 注意：`supportVnodes` 默认 32，而生产 `dcim` 已占满 32/32，建探针库会报 `Vnodes exhausted`。本次临时把 `supportVnodes` 调到 40 并重启 taosd，实验后已改回（`/etc/taos/taos.cfg` 第 49 行恢复为注释，`/etc/taos/taos.cfg.bak.supportVnodes` 为备份）。

# DCIM 写库变慢的真正原因：TDengine 虚拟机 RAID6 月度 check 打满磁盘 I/O（2026-11-03）

## 结论

实时接入写入慢、`写 TDengine 失败: timed out`、只读查询从毫秒级恶化到 3~30s，**不是**
Kafka、不是批次大小、不是跨 vgroup 扇出、也不是客户端并发不够，而是 TDengine 虚拟机
`/var/lib/taos` 所在的 **8 盘 RAID6 正在跑月度 `check`（一致性扫描）**：mdcheck 把
`sync_speed_max` 一路加速到上限 200 MB/s，8 块盘每块读 ~183 MB/s、忙碌 83~90%，taosd 的
WAL fsync 排在流式校验读之后，单条 INSERT 卡 70 秒。

把校验限速到 50 MB/s 后：`写 TDengine 失败` 归零、落库耗时从 41~118s（最长 231s）回到
1~3s、`select server_version()` 从 0.1~18.3s 跳变回到 7~40ms、写入行速从 5,049 升到
**8,830 行/s**。但 lag 仍在以约 +4,584 msg/s 增长 —— 新瓶颈换成了 **taosd 的 CPU**
（16 核里占 9~13 核）。

## 证据

### 1. md 阵列正在做 check，且没有速率限制

```
$ cat /proc/mdstat
md0 : active raid6 sdi[7] sdh[6] sdg[5] sdf[4] sde[3] sdd[2] sdc[1] sdb[0]
      117190158336 blocks super 1.2 level 6, 512k chunk, algorithm 2 [8/8] [UUUUUUUU]
      [====>................]  check = 22.4% (4384857752/19531693056) finish=4924.5min speed=51262K/sec
```

- `/var/lib/taos` 落在 `/dev/md0`（8 × 18.2T HDD，RAID6，109T）
- 限速前 `sync_speed_max = 200000`（KB/s，系统默认），实际 **200,318 K/sec**
- `mdcheck_start.timer` 的 `OnCalendar=Sun *-*-1..7 01:00:00` —— **每月第一个周日 01:00**
  触发；`mdcheck_continue.timer` 负责续跑。随磁盘空闲自动加速（不是固定速率）

### 2. 磁盘被打满，而 TDengine 的真实写只有 14 MB/s

限速前 `/proc/diskstats` 两次快照求差：

| 设备 | 读 | 写 | 忙碌 |
|---|---|---|---|
| sdb~sdi（每块） | **~183 MB/s** | ~3 MB/s | **814~900 ms/s（83~90%）** |
| md0 | 327 MB/s | **14 MB/s** | ≈100% |

`mdstat` 的 `mismatch_cnt` 为 0（阵列健康），所以这些读**全部是校验扫描**，不产生任何
有效写入。

### 3. 服务端与客户端同时被拖垮

| 指标 | 限速前 | 限速后 |
|---|---|---|
| `select server_version()` | 0.1s ↔ 18.3s 跳变 | **7~40ms**（首调 6.5s） |
| `performance_schema.perf_apps` 单查 | 3~30s | 亚秒 |
| 客户端 `写 TDengine 失败` | **104 次/10 分钟**（`timed out`，httpx 30s） | **0** |
| 客户端 `消费中断` / rebalance | 有（`_ASSIGNMENT_LOST`） | **0 / 0** |
| 落库耗时（500 条/批） | p50 1.69s / max 12.7s，batch=2000 时 41~118s、最长 231s | 限速初期 **0.9~2.6s**；持续泄流下均值 **4.3s**、10 分钟内 11% 批次 >10s（**无 30s 超时**） |
| 写入行速（`perf_apps` 60s 增量） | 4,199~5,049 行/s | **8,830 行/s** |

因果链：`check` 读占满磁盘队列 → taosd WAL fsync 等待 70s → 客户端 30s 超时 → 批次重试
→ 单次 flush 拖到 231s → 超过 librdkafka 默认 `max.poll.interval.ms=300000` →
`_ASSIGNMENT_LOST` → 消费组 rebalance → 越滚越慢。

### 4. 已排除的其他方向（都做过实测）

| 假设 | 反证 |
|---|---|
| 客户端 GIL / 进程数不够 | 限速前客户端 `docker stats` 只有 2.5% CPU，全在等 HTTP |
| 批次太小、固定开销摊不薄 | 500→2000 后语句数确实降到 1/4、行/语句 45→96~136，但 taosd 单请求 128ms→1.2s，落库反而回落（见 `dcim-ingest-gil-multiprocess.md` 第二阶段） |
| 跨 vgroup 扇出太大 | 同样内容跨 1 个与跨 28 个 vgroup 的语句耗时**完全相同** |
| CPU / 内存不足 | 限速前 CPU 72~84% 空闲、内存 63G 只用 18G；taosd 自身 CPU **0.55%**（全在等 I/O） |
| 磁盘空间 / compaction | 空间占用 1%、`ins_compacts` 为空 |
| vgroup 不健康 | 32 个 vgroup 全为 leader，cache load 正常 |

## 处置

```bash
# 只读确认
cat /proc/mdstat
cat /sys/block/md0/md/sync_speed_max
cat /sys/block/md0/md/sync_speed

# 限速（需要 root；本次由用户批准后执行）
echo 50000 > /sys/block/md0/md/sync_speed_max
```

执行后 `sync_speed_max` 从 `200000 (system)` 变为 `50000 (local)`，实际速度稳定
50~54 MB/s：

| 指标 | 限速前 | 限速后 |
|---|---|---|
| 每块盘读 / 忙碌 | ~183 MB/s / 83~90% | **~32 MB/s / 39~46%** |
| md0 写 | 14 MB/s | 119 MB/s（积压泄流） |

`check` 剩余时间从「未受限」变为 `finish≈4924min`（约 3.4 天）。

## 边界与提醒

- **`check` 只比对不修复**：它重算 P/Q 校验并与盘上值比较，只累计 `mismatch_cnt`；
  与 `repair`（回写修正）、`resync`（重建）、`recover`（替换盘后重建）不是一回事
- **限速不持久**：`/sys/block/md0/md/sync_speed_max` 重启即失效，下次月检会重新加速到
  200 MB/s。**注意这台机器不能写 `CHECKSPEED_MAX`** —— 它用的是 mdadm 上游 systemd 单元
  （`mdcheck_start.service` → `/usr/share/mdadm/mdcheck`），那个脚本里没有任何限速逻辑，
  Debian 的 `/etc/default/mdadm`（`checkarray` 用）也不参与。持久化做法见文末
  「2026-11-05 限速持久化」
- **本轮没有中止校验**：`check` 是预防性扫描，中止不会丢数据，但本月进度作废、下月重头；
  是否跳过由业务方决定
- **限速只解决了 I/O 争抢，没解决吞吐上限**：磁盘降到 40% 忙后，`top` 显示 taosd
  **906%~1307%**（16 核占 9~13 核，93% us / 1.6% id），最热的是 `rocksdb`（93%）与
  `vnode-c/w`（66~93%）。此时 **加大 `DCIM_WRITE_CONCURRENCY` 或批次只会加重 CPU 争抢**
- **lag 仍在增长**：廊坊A 8 条线程 lag 合计 1,497,795,694 → 1,499,221,414（311 秒，
  **+4,584 msg/s**）。要真正追上生产速率，下一步必须从 taosd CPU 侧入手
  （`numOfCommitThreads`、vgroup 数、子表碎片、窄表/拆表策略），而不是客户端参数

## 2026-11-04 第二轮：瓶颈转到 taosd 提交线程池（4 → 8 → 12）

限速后 lag 仍 +4.6k msg/s，继续查服务端。此时磁盘 `%wa` 仅 5.6%、`select server_version()`
0.01~0.21s，**不是 I/O**；taosd 362% 里 **4 条 `vnode-commit` 各 99.9% CPU**、整机 75% CPU
空闲 —— 32 个 vgroup 共用默认 4 条提交线程，写请求在服务端排队，批耗时尾部拉长到 9.3s。

`numOfCommitThreads` **不支持动态修改**：

```
alter dnode 1 'numOfCommitThreads 8'
→ {"code":281,"desc":"Invalid config option"}
```

只能改 `/etc/taos/taos.cfg`（该文件原本没有这一行，追加即可）+ `systemctl restart taosd`。
改前已备份为 `/etc/taos/taos.cfg.bak.<时间戳>`，回滚即把备份拷回再重启。

| 指标 | 4 线程（原） | 8 线程 | 12 线程 |
|---|---|---|---|
| 落库行速 | 18,592 行/s | **32,099** | 31,733~34,246 |
| 写批耗时均值 | 1.33s | **0.50s** | 0.36~0.51s |
| >5s 批次 | 1.2%（max 9.3s） | **0%**（max 4.0s） | **0%**（max 4.1s） |
| 写失败 / 10min | 40 | **0** | **0** |
| 廊坊A lag | +1.7k msg/s | **−2.05k msg/s** | −1.72k~−1.96k msg/s |
| taosd CPU | 362% | 625% | 281% |

**4 → 8 有明显收益（+70%，lag 由正转负）；8 → 12 没有额外收益**，12 条里只有 2 条活跃
（187%），说明提交线程已不是瓶颈。保留 12 无害（只是多几条空闲线程），但 **8 就够**，
下次维护窗口可回退到 8 以减少无谓并发。

### 2026-11-05 现状

- **月检仍在跑，并没有结束**（09:15 看到 `sync_action=idle` 是**分片间隙**，不是跑完）：
  `mdcheck_start.service` 每次只干 6 小时（`MDADM_CHECK_DURATION=6 hours`），
  剩下的由 `mdcheck_continue.timer` 每小时续跑；进度存 `/var/lib/mdcheck/MD_UUID_<uuid>`。
  09:41:51 continue 单元再次启动，当前 `sync_completed = 12,675,335,472 / 39,063,386,112`
  （**32.4%**）、`sync_speed ≈ 52 MB/s`（限速生效）。**判断是否跑完看 checkpoint 文件是否还在**，
  别看某一时刻的 `sync_action`
- 客户端：8 进程 4.92/12 核、34,246 行/s、批 0.36s、0 失败 —— **既没打满 GIL 也没打满网络**
- 积压 14.63 亿条，按 −1,963 msg/s ≈ **8.6 天**才追平；Kafka 保留期若短于这个时间，
  尾部数据会被 broker 删掉（需向 DCIM 侧确认 topic `retention.ms`）
- 下一步要提速只能往客户端/链路找等待点（每进程仅 0.61 核，说明在等 HTTP 往返，
  不是 CPU 饱和）；**不要再给同一园区加线程**（GIL，已实测反向），可评估加进程或
  提高在途并发，但要先定位等待点
- 限速已持久化（见下节），重启后仍为 50 MB/s

### 2026-11-05 限速持久化（已执行）

上游 `mdcheck` 脚本不读任何限速配置，所以在两个单元上加 systemd drop-in，**检查启动前**
写一次 sysfs：

```ini
# /etc/systemd/system/mdcheck_start.service.d/speed-limit.conf
# /etc/systemd/system/mdcheck_continue.service.d/speed-limit.conf   ← 两份内容相同
[Service]
ExecStartPre=/bin/sh -c 'for f in /sys/block/md*/md/sync_speed_max; do [ -w "$f" ] && echo 50000 > "$f"; done'
```

```bash
sudo systemctl daemon-reload      # 不需要重启/触发检查
```

验证：`sync_speed_max` 在 **check 空闲时也可写**（实测写 40000 回读 40000，再恢复 50000），
且 09:41:51 `mdcheck_continue.service` 的 `ExecStartPre` 已实际执行（`status=0`）随后续跑检查。
回滚：删掉这两个 `.d/speed-limit.conf` 再 `daemon-reload`。

## 排障顺序（下次直接照做）

1. `cat /proc/mdstat` —— 先确认有没有 `check` / `resync` 在跑（**最容易被忽略的一项**）
2. `top -H -b -n1 -p $(pgrep -x taosd)` —— 看 taosd 是 CPU 饱和还是被 I/O 阻塞
   （被阻塞时 taosd CPU 极低，如 0.55%，这是磁盘争抢的典型形态）；若热线程是
   `vnode-c+`（`vnode-commit`）且整机仍有大量空闲核 → 查 `numOfCommitThreads`
3. `/proc/diskstats` 两次快照求差 —— 区分「校验读」与「真实写」
4. `perf_apps` 的 `insert_row` 增量 —— 确认真实落库行速
5. 客户端日志 `写 TDengine 失败` / `消费中断` / `_ASSIGNMENT_LOST` 计数

## 来源

- 实测数据来自 2026-11-03 生产环境：跳板机 `118.126.142.38` → TDengine 虚拟机
  `10.100.17.101`（16 核 / 63G 内存 / 8 盘 RAID6）的 `/proc/mdstat`、
  `/proc/diskstats`、`top -H`、REST `perf_apps` 与 `rz-dcim-ingest` 容器日志
- 相关文档：`docs/problems/dcim-ingest-gil-multiprocess.md`（第一/二阶段：GIL 多进程、
  批次放大反效果）、`docs/dcim-kafka-ingest-design.md`（链路与 topic）

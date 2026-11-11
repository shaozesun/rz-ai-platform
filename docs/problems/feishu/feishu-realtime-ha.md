# 飞书实时入库要不要 7×24 / 高可用（2026-10-25）

> 范围：飞书园区群消息 → `dcim_incident_reports` 的实时入库链路（`service/scheduled_task/incident_stats/realtime.py`）。
> 起因：把「300s 轮询拉取」改成「WS 推送实时入库」后，值班场景默认它必须一直在线。用户问：
> 是不是要 7×24 保持、项目要不要做高可用、重启发版期间的消息怎么办。
> 状态：三项 P0 已落地（本文所列均为当前实现），L1/L2 只写方案不实施。

---

## 一、结论先说

**不需要「进程永不重启」，需要的是「任何空窗都能补齐」。**

判断依据只有一条：**飞书群消息本身是可以在事后拉回来的**（`im.v1.message.list`）。
既然数据源不是一次性的，就没必要为了「一秒都不停」去上双机热备、蓝绿、跨机房；
要做的是把「停多久」和「补得回来吗」两件事分开管：

| 问题 | 现状答案 |
|------|---------|
| 停机期间消息会丢吗 | **不会**（只要 < 72h，补偿循环按水位补；更早的用 CLI 手工回填） |
| 停机期间用户查不到吗 | **会**，最多到下一次补偿（默认 30 分钟，可调小） |
| 发版要做特殊操作吗 | 不需要，正常重启；WS 由选主自动接管，缺口由补偿补 |
| 需要双机/蓝绿吗 | **不需要**（P2）。没有降低任何 RPO，只是把「可见延迟」压短 |

一句话：**RPO ≈ 0（有 72h 回捞窗口），RTO = WS 重连（秒级）+ 补偿一轮（默认 ≤30 分钟）。**
嫌那 30 分钟长，就把 `FEISHU_GROUP_INGEST_BACKFILL_MINUTES` 调到 5，不需要改代码。

---

## 二、飞书事件语义（决定了所有设计）

1. **WS 推送只在连接期间有效，断开期间的事件不会在重连后补投。**
   飞书没有「离线事件队列」。重连后你收到的只是「从现在开始」的事件，
   停机那段没有任何回放机制 —— 所以**补偿只能靠 `message.list` 反向拉**。
2. **连接内是 at-least-once**：ack 超时/网络抖动会重推同一条。
   所以入库必须**幂等**（`UNIQUE(chat_id, message_id)` + upsert，已具备）。
3. **`message.list` 能拉机器人入群之后的历史消息**，不依赖那个敏感权限
   （`im:message.group_msg` 只影响「WS 能不能推非 @ 消息」）。
   单次拉取上限 5000 条（`feishu._MAX_MESSAGES`），够回填一个活跃群一个月。
4. **同一个飞书应用不能多环境同时开**：选主锁在各环境的 Redis 里互不可见，
   两端都连上会双连接，事件随机分派 → 消息落到没有身份数据的那一端。

推论：**WS 是「快」，`message.list` 是「对」。** 两条路共用 `build_report_row`，
行的字段逐字段同形，所以补回来的和推回来的完全一样。

---

## 三、改造前的三个缺口（以及修法）

### 缺口 1：补偿是固定滑窗，停机超过 2N 的消息永久丢失

原实现每 `N` 分钟跑一次 `sync_range(now - 2N, now, backfill=True)`。
问题在于：**实时写入会把该群的 `max(create_time)` 推到「此刻」**，
而固定窗口只看「最近 2N」，停机 2 小时（N=30）之后，中间那段空洞永远不在任何一轮
的窗口里 —— 不报错、不告警，就是没有。

**修法（`sync.sync_gaps` + Redis 水位）**：

```
每群起点 = max( min(Redis 水位, 库内 max(create_time)) - 60s ,  now - cap )
```

- 取 **min** 而不是 max：实时写入把库内最新时间顶到「此刻」时，min 仍指向真正的缺口起点。
- 水位键 `incident:backfill:ts:<chat_id>`（TTL 30d）**只在「拉取 + 写入都成功」后推进**，
  失败不推进 → 下一轮自然重试，不会跳过错过的窗口。
- 两个锚点都没有（新群 / Redis 刚清空）→ 直接追满 `cap`。
- `cap` = `FEISHU_GROUP_INGEST_MAX_BACKFILL_HOURS`（默认 72）× 3600。
  更早的窗口用 CLI `--sync` 手工回填。

### 缺口 2：失去 leader 时不断开 WS → 双连接，事件随机分派

`_leader_loop` 原来只在**成为** leader 时建连。若进程假死超过 `LARK_LEADER_TTL`（30s），
锁被别人抢走，**旧 WS 还挂着** → 同一应用两条连接，飞书把事件随机发给其中一条，
对话可能落到「已经不是 leader」的 worker。

**修法（`client._stop_ws`）**：检测到失锁且线程仍活 → 主动断开。
SDK 没有公开 `stop()`，做法是 `_auto_reconnect=False` → `await _disconnect()` →
停它自建的 loop → `join(8s)`；`_ws_stop` 标志同时抑制「loop 停止导致
`run_until_complete` 抛 RuntimeError」的日志噪音。
`shutdown()` 改为「先断 WS、再释放锁」，做到优雅交接（新 leader 最多 15s 内接上）。

### 缺口 3：`/health` 不暴露飞书状态 → 静默停摆

Redis 不可用时选不到主，**一条 WS 都不建**，而 `/health` 只说 mongodb/redis —— 
进程活着、端口通、容器不重启，实际上没人收消息。

**修法（`client.health_snapshot()`）**：`checks.feishu` 返回

| 字段 | 含义 | 判据 |
|------|------|------|
| `leader_present` | 集群里有没有 leader（Redis `lark:leader` 键） | **false 持续 > 2 分钟 = 故障** |
| `leader_here` | 本 worker 是否 leader | 仅展示（4 个 worker 里 3 个本就 false） |
| `ws_thread_alive` | 本进程 WS 线程是否活着 | 断线重连期间仍为 true，属正常 |
| `last_event_at` / `last_event_age_s` | 全集群最后一次收到 WS 事件 | 群不活跃时会偏大，需结合业务判断 |

**刻意不并入 `ok`**：Docker `HEALTHCHECK` 打的就是这个端点，
并入会让正常的 follower 被判死 → 容器反复重启。

---

## 四、发版 / 重启 SOP

正常发版**不需要额外步骤**，但要按顺序确认：

1. **确认没有第二个环境连着同一个飞书应用**（本地开发永远 `LARK_ENABLED=false`）。
2. 正常重启（`docker compose up -d` 会停旧容器再起新的）。
   `shutdown()` 会先断 WS 再放锁；新容器启动后 leader 在一个 TTL/2（15s）内抢到锁并建连。
3. 起来后看三件事：
   - `curl -s localhost/api/v1/health | jq .checks.feishu` → `leader_present=true`
   - 日志里有 `[Incident] 回补 park=… 起点 MM-DD HH:MM 拉取 N 条（新增 M）`
   - 群里发一条速报，10 秒内 `dcim_incident_reports` 出现该行
4. 停机时间较长（> 补偿间隔）时，不想等下一轮可把
   `FEISHU_GROUP_INGEST_BACKFILL_MINUTES` 临时调到 5，或直接跑
   `python -m service.scheduled_task.incident_stats.cli --sync`。

**注意**：重启窗口内的 WS 事件确实会丢（推不进来），但它们在飞书侧还在，
下一轮补偿按水位补回来 —— 这正是「不追求不间断、追求可补齐」的落地形态。

---

## 五、可用性分级

| 级别 | 形态 | RPO | RTO（数据可见） | 建议 |
|------|------|-----|----------------|------|
| **L0（当前）** | 单机 + 4 worker 选主 + 幂等 upsert + 水位补偿 + `/health` | ≈0（72h 回捞） | WS 秒级；缺口 ≤ 补偿间隔 | 够用 |
| **L1** | L0 + 外部监控（`leader_present=false` >2min 报警、`last_event_age_s` 异常报警）+ Redis/PG 托管或主从 + 产物目录独立卷 | ≈0 | ≤ 5 分钟 | **推荐下一步** |
| **L2** | 多节点 + 蓝绿/滚动发布 + 对象存储 | ≈0 | 秒级 | **不做**：不改善 RPO，只压短可见延迟 |

L1 的三件事都比 L2 便宜，且各自防的是一类真故障：
监控防「静默停摆」，Redis/PG 高可用防「存储单点」，独立卷防「重建容器丢产物」。

---

## 六、真正的单点（按影响排序）

1. **Redis** —— 选主锁 + 水位 + 全平台缓存。挂了就没有 WS。
   兜底：补偿循环在选主失败时 **fail-open** 继续跑；水位读不到 → 退化为满 `cap` 重拉，
   所以数据仍会进来，只是不再实时。恢复后自动重建 WS。
2. **PostgreSQL（`dcim_incident_reports`）** —— 真正的数据落点。挂了写入失败，
   水位不推进 → 恢复后一轮补完。**这是全链路唯一的数据单点。**
3. **飞书应用凭据 / WS 出网** —— 凭据失效或网络不通时 `leader_present=false`，
   `/health` 可见，但只能人工介入（重发凭据/查网络）。
4. **产物本地盘（`AGENT_ANALYSIS_DATA_DIR`）** —— 不影响入库，但报告/看板/视频产物都在本地盘；
   容器重建且没挂卷就是永久丢失。**部署时必须确认已挂 volume。**
5. **飞书接口限流** —— `message.list` 50 条/页、单次 5000 条上限；
   补偿循环是低频任务，正常不会触顶，但一次补满 72h 的重群会明显变慢。

---

## 七、验证方法

```bash
# 静态检查（工具/服务多是懒加载，这类错只有真调用才炸）
python -m pyflakes service/feishu service/scheduled_task/incident_stats | grep "undefined name"

# 事件统计自检（含实时行形状 test_realtime_row_shape）
python -m service.scheduled_task.incident_stats.tests.test_incident
```

生产验证：

1. 连续 `curl /health` 多次，`feishu.leader_present` 恒为 true（不随 worker 抖动）。
2. 重启 backend → 15–45 秒内 `leader_present` 恢复 true。
3. 发版演练：记下某群 `max(create_time)` → 停 10 分钟 → 起 → 核对空窗时段有新行，
   且日志出现按水位计算的回补起点（不是「最近 60 分钟」这种固定滑窗）。
4. 同一个 `message_id` 重复推送 → 仍只有 1 行（`UNIQUE(chat_id, message_id)`）。
5. 断网/停 Redis 时：`leader_present=false` 可在 `/health` 看到，补偿仍能 fail-open 入库。

---

## 八、不做的事

- **不追求进程 7×24 不重启**，也不为此上蓝绿/双机 —— 数据源可回捞，重启是可接受的。
- **不开启 `INCIDENT_STATS_ENABLED`**（它会顺带建 61 个预设报告任务）；实时入库的开关
  是独立的 `FEISHU_GROUP_INGEST_ENABLED`。
- **不放宽 `LARK_LEADER_TTL=30`** —— 缩短 leader 崩溃后的接管时间靠的是选主循环（TTL/2），
  放宽 TTL 只会让假死判定变慢。
- **不把 `feishu` 并入 `/health.ok`**（follower 会被判死）。
- **不让 WS 当唯一数据源**（它不可重放）；`message.list` 永远是兜底真相。
- **不做「群里所有消息都进对话 Agent」**（成本/串味/刷屏，见 `feishu-group-realtime-ingest.md`）。

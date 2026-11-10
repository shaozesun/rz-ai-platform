# DCIM Kafka 接入设计(阶段一:空间/设备/点位配置数据)

> 范围:接收前三类**配置**数据 —— `ems-config-space-rz`(空间)、`ems-config-device-rz`(设备)、`ems-config-point-rz`(点位)。
> 实时监控 `ems-real-data`、时序存储与能耗/PUE 分析不在本阶段(阶段二再设计)。
> 依据:`DCIM Kafka 接口文档(接入) V1.0`。
>
> **阶段二已落地(2026-11)**:`ems-real-data-rz` 里**混着两种报文** —— 带 `device_id` 的设备测点
> 进按 `device_type` 分的宽表(`dcim-timeseries-storage-design.md`);**无 `device_id`、有 `building`**
> 的是 **PUE / 能耗统计**报文,进独立超级表 `st_pue`(`dcim-pue-storage-query.md`)。
> 判据、存储模型、查询能力与已知边界都写在后一篇。
>
> **⚠️ 变更(2026-09)**:原方案为「我方自建 Kafka 集群,topic 由 `kafka-init` 自动创建,DCIM 往我们集群推」。
> 现已变更为 **broker 与三个 topic 均由 DCIM 提供,我方只消费**;且我方服务器不能直连对方 broker 的 IP,
> 链路中间经**一台虚拟机转发**。下文第九 / 十 / 十一 / 十二 / 十三节已按新口径改写;
> 自建方案保留在 `deploy/kafka-prod/`(已停用,见该目录 README)。

## 一、目标与产出

**目标**:把 DCIM 采集层的"空间树 / 设备台账 / 点位字典"落成我们自己的**永久元数据底座**(PostgreSQL),**追随 DCIM 推送做变更同步**,并保留变更历史;供平台与 Agent 查询,为阶段二(实时数据 → 时序库 → 能耗分析)打地基。

**产出**:
1. 一个独立消费 worker(`dcim-ingest`,同仓库同镜像、独立进程);
2. PostgreSQL 中的三张**当前态表** + 三张**变更历史表**;
3. 面向平台的元数据查询能力(空间树、设备清单、点位字典含单位/状态映射、历史回溯);
4. Redis 字典缓存(热数据,写库后失效);
5. ~~由我们搭建的 Kafka 集群与 topic~~ → **broker 与 topic 由 DCIM 提供,我方只消费**(见第九节)。

> 关键价值:`ems-config-point-rz` 的 `point_type / unit / status_map` 是**把阶段二原始数值翻译成业务语义**的唯一依据(`1001000` → 「第15节电压 / V」、`1=开门;0=关门`),必须先落库。

## 二、总体架构

```
DCIM(生产者) ──▶ DCIM 自建 Kafka(SASL_PLAINTEXT/PLAIN, LZ4)
  ems-config-space-rz  (首次全量 + 变更增量)
  ems-config-device-rz (首次全量 + 变更增量)
  ems-config-point-rz  (首次全量 + 变更增量)
                              │
                              │  经中转虚拟机(端口/路由转发;
                              │  我方不能直连对方 broker IP)
                              ▼
                 ┌──────────────────────────────┐
                 │ dcim-ingest worker(独立进程) │
                 │ 3 个消费者 · 批量 · 幂等 upsert│
                 │ 同事务写:当前态 + 历史        │
                 └───────────────┬──────────────┘
                                 ▼
                 ┌──────────────────────────────┐
                 │ PostgreSQL  database: dcim   │
                 │ dcim_spaces / _devices / _points        (当前态)
                 │ dcim_*_history                           (变更历史)
                 └───────────────┬──────────────┘
                                 ▼
                    Redis 字典缓存(写库后失效)
                                 ▼
                     平台能力层 / Agent 工具(只查本地库)
```

## 三、消费者通用设计

| 项 | 设计 |
|---|---|
| 语言/库 | Python(`confluent-kafka` 或 `aiokafka`) |
| 认证 | 按对方 broker 配置(`SASL_PLAINTEXT` 或 `SASL_SSL` + `PLAIN`),账号密码由 DCIM 下发、走配置 |
| 消费组 | `rz-dcim-ingest`;并发上限 = 对方 topic 的分区数;横向扩缩靠加 worker 进程 |
| 起始位置 | 首次 `auto.offset.reset=earliest`(历史含首次全量,不能漏) |
| 提交方式 | `enable.auto.commit=false`,**同事务写库成功后**再提交 offset(at-least-once) |
| 幂等 | 按主键 `guid` upsert;重复消费不产生脏数据 |
| 批量 | 500~1000 条/批或 1s 一刷 |
| 乱序 | 父子/设备颠倒到达不阻塞:先按原值落 `parent_guid`,查询时关联;**待确认项见第十三节**。同一实体内的顺序依赖 Kafka 分区顺序 → 推送必须以**实体 guid 作消息 key**(见 9.3) |
| 失败 | 单条解析失败 → 死信(本地 DLQ 表或 DLQ topic);DB 异常 → 退避重试 + 告警 |
| 监控 | 消费 lag、批次耗时、失败条数(沿用现有日志规范) |

**变更同步原则**:以 **DCIM 推送为准**——新增/修改/删除都由推送驱动,我们不做推测性修改;所有变更**同时写入历史表**(见第六节)。

## 四、三类数据的处理

### 1. 空间 `ems-config-space-rz`
字段:`guid / tag / path / name / idc / node_type(=1) / space_type / parent_id`
- upsert 到 `dcim_spaces`,主键 `guid`;
- `parent_id` 为空即根节点;层级(数据中心→楼宇→房间)通过 `parent_guid` 链表达;
- `idc` 作为机房维度过滤键。
- **⚠️ guid 校验(2026-10-12 加)**:对方会对**同一空间推两份** —— 完整的 `RZYWBXSJ-P006-…`
  和**前缀被截断**的 `-P006-…`(见待确认清单 #2)。残缺那份是**查不到任何设备的空壳**,
  且 `-`(0x2D) < `R`(0x52) 会让它在 `ORDER BY path, name, guid` 里排到前面,
  模型拿它去查设备得 0 条 →「问机房有哪些设备」全空。
  **规则:首段为空(以 `-`/`.` 开头)的 guid 一律丢弃** —— 在 `models.py`
  `_reject_truncated_guid()` 里,抛 `SkippedRecord`:**只计数、不入 DLQ**
  (配置类是周期性推送,入 DLQ 会把表刷爆),计数显示在 lag 日志的「跳过=N」。
  设备/点位同样加了这道校验(实测 0 条命中,统一防御)。

### 2. 设备 `ems-config-device-rz`
字段:`guid / path / name / idc / node_type(=2) / device_type / device_name / parent_id(所属空间) / extend_position`
- upsert 到 `dcim_devices`;`space_guid = parent_id` → 关联 `dcim_spaces.guid`;
- `device_type` 为规范编码(如 `1110101`),**`device_name` 是该编码对应的类型名称**(如「精密空调」)——
  注意别和设备名称 `name` 混(9/29 版新增字段);
- `extend_position`(如 `01-13.A1`)保留为物理位置描述。

### 3. 点位 `ems-config-point-rz`
字段:`id / guid(设备guid.id) / parent_id(设备 guid) / name / idc / node_type(=3) / point_type / unit / status_map`
+ **4 个告警阈值** `hint_alarm / general_alarm / important_alarm / urgent_alarm`(9/29 版新增)
- upsert 到 `dcim_points`;规则:**`point.guid = device_guid + '.' + point_id`**;
- `point_type`(1-AI/2-DI/3-通讯状态/4-AO/5-DO)、`unit`、`status_map`(如 `1=开门;0=关门`)解析为 `status_map jsonb`,同时保留原文;
- **告警阈值**是嵌套对象(`EmsAlarmThreshold`:`maxUse/minUse/maxAlarm/minAlarm/maxEvent/minEvent`),
  整体存 `jsonb`,**不展开成 24 列**;阈值由 DCIM 侧配置,是后续"按阈值分析告警"的依据;
- **⚠️ 读阈值的正确姿势(实测语义,`dcim_meta.query_over_threshold` 依赖它)**:
  **`maxUse`/`minUse` 才是「该侧是否配置」的开关**,`maxAlarm`/`minAlarm` 是界限值。
  未配置的档位是 `maxUse:0,minUse:0,maxAlarm:0,minAlarm:0`;而通断类测点 `HT_Status_Comm`
  是 `maxUse:1,maxAlarm:0` —— **这个 0 是真上限**(0=正常,超过就告警)。
  所以「`maxAlarm≠0` 才算配置」和「0 就是上限」**两种读法都错**:前者漏掉通断点,
  后者把每个未配置的测点都报成"高于上限 0"(实测 36 个温湿度点全被误报)。
  生产数据 432 条档位记录**零反例**。`maxEvent`/`minEvent` 是**告警编码**(实测 `HJ00200004`),
  不是文案,不参与比较。落地在 `client.py::_breach` + `repository.py::_alarm_enabled`;
  ⚠️ 待对方确认的正是这条(见 `docs/dcim-kafka-待确认清单.md` 第 17 项);
- `device_guid = parent_id` → 支持"设备下有哪些点位""点位属于哪个机房"。

### 4. 告警事件 `ems-alarm-data-rz`

对方告警引擎的事件流(触发 1 / 刷新 2 / 恢复 3),同一 `event_log_id` 三态推送。
- upsert 到 `dcim_alarms`(主键 `(cluster, guid)`,`guid` 存 `event_log_id`)+ 追 `dcim_alarms_history`;
- **判「是否已恢复」只能看 `status=3`**,不能看 `remove_time/remove_value != 0`(未恢复时是哨兵 0);
- 实测报文键是 `NormalRange`(大写),同时兼容小写 `normal_range`;
- **独立消费组** `rz-dcim-alarm`:与配置类同组会被配置线程的重启 rebalance 带停。

### 5. 告警工单 `ems-alarm-order-rz`

告警被**人工响应/处理**时推送,同一 `orderNo` 多次推送(1 处理中 → 2 已处理,字段逐步填全)。
- upsert 到 `dcim_alarm_orders`(主键 `(cluster, guid)`,`guid` 存 `orderNo`)+ 追 `dcim_alarm_orders_history`;
- `event_log_id` 关联 `dcim_alarms.guid`,同库可直接 JOIN;
- **实推报文比文档 §10 全**:除文档字段外还带 `addrId/addrName`(空间)、`mgrObjId/mgrObjName`(设备)、
  `mgrObjAttributeId/Name`(测点)、`level/content/value/alarmTime` —— 已映射为与 `dcim_alarms`
  同名的 `space_guid/device_guid/point_guid/alarm_level/...` 列,其余留 `payload`;
- **时间戳是秒级**(实测 `responseTime` 形如 `1792479166`);解析层按量级兼容毫秒,`0` 视为未填落 NULL;
- **独立消费组** `rz-dcim-alarm-order`;开关 `DCIM_ALARM_ORDER_ENABLED`。
- **查询入口**:`dcim_meta.query_alarm_orders`(参数见 `docs/platform-capabilities.md`),
  返回工单列表 + `summary`(处理中/已处理、各档级别、响应时长 avg/P50/P90),`event_log_id` 可与
  `query_alarms` 关联 —— 「告警在不在跑」查事件,「处理了吗/响应多快」查工单。
- **告警侧已内联工单**:`query_alarms` 每行用 `LEFT JOIN LATERAL … LIMIT 1`(按 `updated_at DESC`)
  带出**最新一单**收成嵌套 `work_order`,同一事件多次建单也不会放大行数/总数;
  **单条告警的处置情况不必再调 `query_alarm_orders`**。

> 2026-10-20 实测:廊坊A 的 `ems-alarm-order-rz` 已建但为空,base `ems-alarm-order` 持续有数据。
> 按既定约定**只订 `-rz` 那份**(base 是对方内部 topic);对方接线后自动开始落库,无需重启。

## 五、PostgreSQL 存储(永久保存)

同实例新建 database(或 schema)`dcim`,与 agent checkpoint 库隔离。

**当前态表(最新状态)**

| 表 | 字段要点 |
|---|---|
| `dcim_spaces` | `guid PK`、`tag`、`path`、`name`、`idc`、`node_type`、`space_type`、`parent_guid`、`payload jsonb`(原始报文)、`updated_at`、`is_deleted bool`、`deleted_at` |
| `dcim_devices` | `guid PK`、`name`、`path`、`idc`、`device_type`、`extend_position`、`space_guid`(FK→dcim_spaces)、`payload jsonb`、`updated_at`、`is_deleted`、`deleted_at` |
| `dcim_points` | `guid PK`、`point_id`、`name`、`idc`、`point_type`、`unit`、`status_map_raw`、`status_map jsonb`、`device_guid`(FK→dcim_devices)、`payload jsonb`、`updated_at`、`is_deleted`、`deleted_at` |

索引:`parent_guid`、`space_guid`、`device_guid`、`idc`。

写入(批量 upsert):
```sql
INSERT INTO dcim_devices (guid, name, ..., updated_at)
VALUES (...), (...)
ON CONFLICT (guid) DO UPDATE SET name = EXCLUDED.name, ..., updated_at = EXCLUDED.updated_at;
```

空间树查询(递归 CTE):
```sql
WITH RECURSIVE t AS (
  SELECT * FROM dcim_spaces WHERE guid = $1
  UNION ALL
  SELECT s.* FROM dcim_spaces s JOIN t ON s.parent_guid = t.guid
) SELECT * FROM t;
```

## 六、变更历史(追加表)

> **2026-10-21 更新:配置类历史表已移除。** 原设计给了 `dcim_spaces_history` /
> `dcim_devices_history` / `dcim_points_history` 三张表,假设"元数据变更很少,append-only
> 存储量小"。实际上游把配置推送做成了**每小时全量重推**——每次都是新消息、新 offset,
> payload 与业务键完全不变。于是这三张表记的不是"变更"而是"快照":
> `dcim_points_history` 实测 4,303,647 行,**77.2% 与同 guid 上一条逐字节相同**,
> 单栋楼约 7 GB/天;而全仓库**没有任何读取方**(`device_at()` 定义了但无人调用)。
> 三张表已删除,`models.TABLES` 里对应项的历史表名为 `None`。
> **告警类(`dcim_alarms_history` / `dcim_alarm_orders_history`)保留** ——
> 一条告警一行、增长极慢,"何时触发/何时被谁处理"是真会被追问的追溯需求。

保留下来的历史表结构:

| 字段 | 说明 |
|---|---|
| `id bigserial PK` | 自增 |
| `guid` | 关联主键(索引) |
| `op` | 变更类型:`upsert` / `delete`(删除语义待确认,见第十三节) |
| `payload jsonb` | 该次推送的**原始报文快照** |
| `valid_from` / `ingested_at` | 生效时间 / 入库时间 |
| `kafka_topic` / `partition` / `offset` | 溯源(可定位到具体消息) |

- 与当前态表**同一事务**写入,保证两者一致;
- `UNIQUE (cluster, kafka_topic, partition, "offset")` 只保证**同一条消息**重放时不重复写,
  挡不住"换个 offset 推相同内容"——这是上面移除配置类历史表的直接原因;
- 索引:`(guid, ingested_at)`。

## 七、服务形态:同仓库 + 同镜像 + 独立 worker

| | 说明 |
|---|---|
| 代码 | 同仓库新增 `service/dcim_ingest/{consumers,repository,models,config}.py`,入口 `python -m service.dcim_ingest` |
| 镜像 | **复用同一镜像**,`docker-compose` 中以不同 command 起两个 service:`web`(uvicorn main:app)与 `dcim-ingest`(python -m service.dcim_ingest) |
| 共享 | `.env`/settings、日志规范、PG/Redis 连接封装、元数据仓储代码 |
| 交互 | 两进程**只通过 PG 交互**,不共享内存 |
| 收益 | web 发版不打断消费;消费百万点位不挤占 API/SSE;按分区独立扩缩;故障域隔离;阶段二接实时数据可复用同一框架 |
| 定位 | 属"**模块化单体 + Worker 角色**",不是微服务(同仓库、同镜像、共享 DB、同发布节奏) |

## 八、对外能力(供平台/Agent)

1. **空间查询**:按 `idc` 列空间树 / 按名称定位空间 / 查某空间下设备与点位数;
2. **设备查询**:按空间、名称、`device_type` 查设备台账;
3. **点位字典**:按设备查点位(含 `unit`、`status_map` 解码后的中文含义)、按点位 `guid` 查定义;
4. **历史回溯**:查询某设备/点位/空间在指定时间的状态(读 `_history`);
5. 一律查**本地 PG + Redis**,不实时打 DCIM、不连 Kafka。

## 九、Kafka 环境与联调

**broker、topic 与 SASL 账号均由 DCIM 提供**,我方**只消费、不建 topic**。
接入参数（地址/端口/协议/advertised 类型/节点数/topic 权限）需向对方索取，清单见
`docs/dcim-kafka-deploy-manual.md` 第二节。

### 9.1 开发/联调环境（本地离线测试,保留）
- `deploy/docker-compose.kafka-dev.yaml` + `deploy/kafka-dev/` 提供单节点 dev broker,配 mock 生产者脚本
  (`deploy/scripts/dcim_mock_producer.py`),用于**不依赖对方**的端到端验收(全量/增量/乱序/重复/软删场景);
- `deploy/scripts/dcim_verify_pipeline.py` 不碰 Kafka,直接回归 PG 解析与写库逻辑。

### 9.2 生产环境
- **我方不部署 broker**。原自建方案(单节点 KRaft + 自动建 topic)保留在 `deploy/kafka-prod/`,已停用;
- 消费端只需能出网访问中转虚拟机;资源需求从"16 核/32 GB/500 GB"降到"1 核 / 512 MB"量级;
- topic 分区数、保留期由对方决定 —— 配置类数据建议与对方约定 **≥30 天**,便于重放/补数。

### 9.3 需与 DCIM 确认的推送约定
推送规范(JSON、**必须开启 LZ4**、按设备组包、单条 ≤1MB)仍然适用;

**并要求消息 key = 实体 guid(设备/空间/点位所属设备)**：同一实体的新增/更新/删除必须落**同一分区**才能保证顺序。
实测踩坑:mock 未设 key 时,同一实体的"删除"与"新增"被分到不同分区,消费乱序 → 删除先执行、随后新增又把记录 upsert 回来(表现为软删不生效)。

### 9.4 网络与安全
- 链路:我方服务器 → 中转虚拟机 → 对方 broker,**不直连公网**;
- `SASL_PLAINTEXT` 为**明文协议**:报文与口令在链路上是明文,只应走内网/专线/隧道;
  若链路必须跨公网,应改 `SASL_SSL`;
- 口令由对方下发;不要沿用接口文档里的示例弱口令。

### 9.5 中转方式（按对方 advertised 类型二选一）
- advertised 报 **IP** → 虚拟机开 `ip_forward` + SNAT,我方加一条 `ip route add <brokerIP>/32 via <VM_IP>`;
  对消费端完全透明,多节点每节点一条。**推荐**。
- advertised 报 **域名** → 虚拟机做 DNAT,容器加 `extra_hosts` 把域名指到虚拟机;
  同一 `VM_IP`+端口无法区分多个 broker,多节点需给虚拟机配多个 IP。
- 两边规则**都要持久化**(iptables-persistent / netplan / systemd),否则重启失效。

> 原理提醒:**bootstrap 只用于"问路"**。客户端第一次连上后,broker 返回的节点表里是 `advertised.listeners`,
> 之后所有连接只走那些地址。所以只转发 bootstrap 端口是不够的 —— 这也是原自建方案里 healthcheck
> 那次误判(`localhost:9094` 连得上、却被指向解析不了的 advertised 域名)的同一根因。

## 十、配置项(拟)

```
DCIM_KAFKA_ENABLED=true
KAFKA_BOOTSTRAP_SERVERS=<对方地址>:<端口>          # 由 DCIM 提供；若 advertised 用域名需配 extra_hosts
KAFKA_SECURITY_PROTOCOL=SASL_PLAINTEXT            # 或 SASL_SSL，按对方实际配置
KAFKA_SASL_MECHANISM=PLAIN
KAFKA_SASL_USERNAME=<对方账号>
KAFKA_SASL_PASSWORD=***
KAFKA_GROUP_ID=rz-dcim-ingest
KAFKA_TOPIC_SPACE=ems-config-space-rz
KAFKA_TOPIC_DEVICE=ems-config-device-rz
KAFKA_TOPIC_POINT=ems-config-point-rz
KAFKA_BATCH_SIZE=500

DCIM_REALTIME_ENABLED=true
KAFKA_TOPIC_REAL_DATA=ems-real-data-rz     # 混两种报文：设备测点 + PUE/能耗统计（见 dcim-pue-storage-query.md）
KAFKA_REAL_DATA_GROUP_ID=rz-dcim-realdata

DCIM_ALARM_ENABLED=true
KAFKA_TOPIC_ALARM=ems-alarm-data-rz
KAFKA_ALARM_GROUP_ID=rz-dcim-alarm

DCIM_ALARM_ORDER_ENABLED=true
KAFKA_TOPIC_ALARM_ORDER=ems-alarm-order-rz
KAFKA_ALARM_ORDER_GROUP_ID=rz-dcim-alarm-order

DCIM_PG_DSN=postgresql://user:***@host:5432/dcim
```

## 十一、部署与容量

- `dcim-ingest` 作为独立 service 加入 `deploy/docker-compose.dcim-ingest.yaml`(同镜像不同 command),起步 ~1 核 / 512 MB;
- Kafka 由 DCIM 托管,**我方不占资源**;只需一条到中转虚拟机的网络通路;
- PG:元数据量级小(空间/设备十万内,点位百万级),百万点位 + 历史表按 GB 级估算,预留 50~100 GB 足够;
- 仅配置数据时流量很小;阶段二实时数据需要另建**时序库**(ClickHouse / TimescaleDB)与降采样。

## 十二、落地步骤

1. 向 DCIM 索取接入参数(地址/端口/advertised 类型/节点数/协议/账号/topic 权限)→ 打通中转链路;
2. 实现 `dcim-ingest`(三消费者 + 同事务写库 + 历史表 + offset/DLQ + 指标);
3. 用 mock 生产者联调:全量/增量/乱序/重复场景(dev broker 或指向对方 broker);
4. 落库校验:抽样比对 DCIM 侧空间/设备/点位数量与层级关系;
5. 建 Redis 缓存与元数据查询能力,接入平台(Agent 可用);
6. 正式环境联调 DCIM 推送;完成后进入阶段二(实时数据 → 时序库)。

## 十三、风险与待确认

| 风险/问题 | 说明 |
|---|---|
| **删除语义** | 待向 DCIM 确认:增量里是否有显式删除事件?若没有,需"定期全量对账"标记消失记录(确认后启用,防误删)。当前先按软删处理 |
| 首次全量已过期 | 配置 topic 保留 30 天(远宽于文档默认 24h),但若长期未接入仍需请 DCIM 重推全量;阶段二实时 topic 仅 24h,消费端必须先就绪 |
| `guid` 稳定性 | 设备/点位重建后 `guid` 是否复用,影响历史关联正确性 |
| `idc` 取值 | 取值集合与现有机房命名如何对应 |
| `device_type` 编码 | 编码含义字典由谁提供(是否需要 DCIM 给映射表) |
| 存储增长 | 历史表 append-only;需评估点位高频变更场景,必要时对历史表分区/归档 |
| broker 归属 | 已明确:broker 与 topic 由 DCIM 提供,我方只消费;topic 分区数/保留期由对方决定 |
| 中转链路 | 我方不能直连对方 broker IP,依赖中间虚拟机转发;规则需持久化,且虚拟机是**单点** |
| 依赖对方 TTL | 重放/补数窗口受对方 topic 保留期约束;配置类数据建议书面约定 ≥30 天 |

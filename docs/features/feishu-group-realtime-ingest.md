# 飞书群消息实时入库（WS 推送）

2026-10-22。把「飞书园区群的通报消息」从**300s 轮询拉取**改为 **WS 推送实时入库**。

## 为什么

原来的主路径是拉：`sync.sync_range()` → `im.v1.message.list`，由后台循环每
`INCIDENT_SYNC_INTERVAL_SECONDS=300` 跑一轮。两个问题：

1. **延迟 0–5 分钟**：值守场景里「刚发的速报」查不到，用户以为没入库。
2. **那个循环被 `INCIDENT_STATS_ENABLED` 门控**，而开这个开关还会顺带创建 61 个
   预设报告任务（我们并不想开）。

飞书 WS 长连接（`service/feishu/client.py`，订阅 `im.message.receive_v1`）**早就在跑**，
但 `channel._on_message` 把「非 p2p 且未 @ 机器人」的群消息全部丢弃，且只通向对话 Agent。

## 关键取舍

| 取舍 | 选择 | 理由 |
|------|------|------|
| 群消息进不进对话 | **不进，只入库** | 成本 / 群上下文串味 / 未绑定用户刷屏 |
| 开关 | **独立** `FEISHU_GROUP_INGEST_ENABLED` | 不能牵连 `INCIDENT_STATS_ENABLED` 的事件统计表预设任务 |
| 范围 | chat_id **白名单，为空即不启用**（fail-closed） | 群消息读权限是敏感权限，不提供「默认全收」 |
| 轮询 | 保留，降级为**低频补偿** | WS 断线/进程重启期间的窗口必须能补回来 |
| 匹配 | 只有速报/首报触发（2s 去抖） | `other` 也入库（统计口径不变），但没必要为闲聊跑匹配 |

## 链路

```
飞书 WS 事件（WS 线程）
  → channel._on_message 开头旁路：should_ingest(chat_id) ？→ realtime.on_message_event
      （之后原对话逻辑照旧执行，两条路并存、互不影响）
  → submit_async 回主 loop
  → text_from_content(message_type, content)      # WS 给的是 JSON 字符串
  → build_report_row(...)                          # 与拉取路径共用，行逐字段同形
  → cluster = chat_id → 园区（INCIDENT_FEISHU_PARK_CHATS 取反，进程内缓存 5 分钟）
  → asyncio.to_thread(source.save_reports, [row])  # 幂等 upsert
  → sos/first：2s 去抖后 match_pending(100)
```

补偿循环：每 `FEISHU_GROUP_INGEST_BACKFILL_MINUTES`（默认 30）跑一次 `sync_gaps`，
选主 key `incident:backfill`。起点逐群算，**取两个锚点的最小值**：

```
since = max(min(Redis 水位, 库内 max(create_time)) - 60s,  now - MAX_BACKFILL_HOURS)
```

Redis 水位（`incident:backfill:ts:<chat_id>`）只在「拉取+写入成功」后推进，
所以停机两小时重启后仍从停机前的点补起。`FEISHU_GROUP_INGEST_MAX_BACKFILL_HOURS`
（默认 72）是单次回追上限，再早的窗口用 `cli --sync` 手工回填。

## 撤回处理（当作没发过）

库里一行 = 一条群消息，统计表每次从库实时算，所以**撤回 = 物理删行**（`source.delete_report`），
已导出的 xlsx 不受影响。两个入口都要堵，缺一不可：

- **推**：事件订阅 `im.message.recalled_v1` → `channel._on_recall` → `realtime.on_recall_event`
  （白名单 `should_ingest` 才处理）→ `source.delete_report(chat_id, message_id)`。
- **拉**：`message.list` 仍会返回已撤回的消息，但 `deleted=true`、`body.content` 固定是
  `This message was recalled`。`feishu.is_recalled()` **先于 `extract_text`** 判断，撤回的
  message_id 单独返回（`fetch_messages` → `(rows, recalled_ids)`），`sync_range`/`sync_gaps`
  拿去删行、**绝不入库**。漏了这一步会把库里原文 upsert 成占位符、`kind` 掉成 `other`，
  而且只要还在回补窗口内（每 30 分钟一轮）每轮再覆盖一次。

语义与边界：

- 删速报 → 通报照常成行；删通报 → 剩下的速报自己成行（`build._group_reports` 里没有 `first`
  的组也成行）。这就是「当没发过」。
- 匹配结果（`event_log_id`）随行消失，不做补救；该组按剩余报文重算。
- **边界**：机器人离线期间发生的撤回、且早于 `FEISHU_GROUP_INGEST_MAX_BACKFILL_HOURS`（默认 72h）
  的消息清不掉 —— 回补窗口的固有上限。
- 运维前提：飞书开放平台 → 事件订阅新增 `im.message.recalled_v1`（长连接也要勾）→ 发布版本 →
  重启后端。没加订阅时只有拉取侧兜底。

## 卡片通报：进展是「每条新消息」（2026-11-10 实测订正）

现场有一种**卡片式事件通报**：标题 `【RZ-CQ-A1数据中心事件通报】`，字段把冒号写在括号内
（`【事件概述：】 : 值`），之后每次进展是一条**新卡片消息**「进展同步1/2/3…」。
真实廊坊群（2026-11-09）是**换行拆开的裸标签**：块 1 带 `通报时间` / `进展描述`，
**块 2 起没有标签**，只有「时间 + 描述」；事件结束时追加 `事件结束` 段
（`填写人/处理人/处理时间/处理结果/事件原因`）。

> **2026-11-10 订正**：2026-11-05 第一版按「同一条消息里原地追加」实现（定时
> `message.get` 回捞）。生产复核否定该前提 —— 同一事件（发生 16:11:22）的 7 条卡片是
> **7 个不同 `message_id`**，正文逐条累积；最早那条至今仍只有 1 条进展。即**每次进展
> 就是一条新消息**，由 WS 实时入库（`ingested_at − create_time` = 1~2 秒），库里就是
> 完整正文。

- **收到的形式**：`message_type='interactive'`，`content` 是卡片 JSON；两路都用
  `feishu._walk_texts` 递归抽 `text`/`content`/`title` → 纯文本后再 `classify`。
- 每条含「通报时间」的卡片都会被判 `first`，靠**匹配到同一告警**（`event_log_id`）在
  `build._group_reports` 里合并成一行；匹配不到告警时按 `_SOS_FIRST_MERGE_SECONDS` 锚定合并。
- **查询/出表直接读库**：`build.progress_blocks()` 按 `进展同步N` 的编号去重（同号取后出现者），
  多条累积卡片自然聚合成完整时间线，**不依赖任何回捞**。
- **定点回捞 `sync.refresh_cards` 默认关闭**（`INCIDENT_CARD_REFRESH_MINUTES=0`）：它只对
  已知 `message_id` 重新 `message.get`，发现不了漏收消息；实测 78/78 张卡片进展数与库里
  相同，唯一效果是把干净正文（205 字）覆盖成 lark_md 版本（425 字，带
  `【**发生时间**】<font color='grey'>…`）。代码保留，仅当某园区确实存在原地编辑卡片时
  把间隔改非 0 开启。
- **补偿仍走 `sync_gaps`**（每 30 分钟 `message.list`）：2026-11-10 日志每轮报
  `230027 need scope: im:message.group_msg`；同容器探测 `chat.list` 成功、`message.list` 被拒
  → 权限已勾选但未在发布版本中生效，需重新发布版本 / 管理员审批。未生效期间只有 WS，
  **停机漏收的消息补不回来**。手工核对：
  `python -m service.scheduled_task.incident_stats.cli --refresh-cards --hours 72`。

**「事件进展」列按追加块解析**（`build._latest_progress()`）：优先级 ① `事件结束` 段的
`处理结果`（**最终进展**，如「现场已处理,告警已解除」）→ ② **编号最大**的 `进展同步N`
块（同号取后出现者）→ ③ 旧格式 `【进展描述】` 取最后一条非空。块内**裸标签 /【…】/
无标签**三种写法都认，块首的 `通报时间`/`进展描述` 标签与时间戳会先剥掉。
原地字段 `进展同步`（**不带数字**）**不进这一列**（那是首版的处置状态）。
一条进展都取不到才回退工单/告警状态（旧行为逐字不变）；工单状态始终在「处理结果」列。

**卡片标记要先清掉**（`build._plain_text()`，2026-11-09）：`_walk_texts` 抽的是 lark_md
原文，库里存的是 `【**处理结果**】 <font color='grey'>现场已处理,告警已解除</font>`
这种带 `**` / `<font>` 的文本 —— 不清理会让值原样进报表（实测「事件进展」列显示
`**】 <font color='grey'>…</font>`：括号分支没匹配上，退到裸标签分支把 `**】` 吃进了值）。
`_plain_text` 只删 `**` 与 `<font>/<at>/<text_tag>/<emoji>` 标签，换行与标点原样保留；
`_latest_progress()`、`_report_field()`、以及名称取不到时的「速报内容/首报内容」都先过它。

**进展全记录（2026-11-09）**：`build.progress_blocks()` 把同一段正文按 `进展同步N`
切成 `[{'no','time','desc'}]`（同号取后出现者、按编号升序；没有编号块时才回退旧格式
`【进展描述】`），`build.final_section()` 取最后一个 `事件结束` 段的
处理结果/事件原因/填写人/处理人/处理时间。统计表仍只取最新一条（`_latest_progress` 复用
`progress_blocks`，行为不变）；对话侧由 `service/scheduled_task/incident_stats/detail.py`
按事件聚合（复用 `_group_reports` + `_compose`，口径与出表一致）并提供 `search()` 关键词
匹配，工具 `tools/incident_tools.py` 的 `incident_progress` 交付：**自然日窗口**（不是班次）、
多候选只列候选表让用户确认（**不猜**）、唯一命中时服务端渲染「序号/通报时间/进展描述」
与「事件结束」结论，正文仍是卡片原始报文。

**仲裁输入截掉追加块**：`arbitrate._strip_progress()` 只从 `进展同步N`（**带数字**）或
`【进展描述】` 处截断（前面 `－－－－` 分隔线一并吃掉），`【事件来源】/【涉及客户】/
【影响范围】/【冗余配置】/【系统状态】` 与原地 `【进展同步】` 都保留 ——「影响范围」是
仲裁依据。表格展示仍用完整正文。

**已知上限**：查询路径不调飞书，`message.get` 的 72h 保留期不再影响出表/进展查询；
真正要补的是 `sync_gaps`（`message.list`，受 `im:message.group_msg` 权限约束），超过
`FEISHU_GROUP_INGEST_MAX_BACKFILL_HOURS`（默认 72h）的停机漏收补不回来。

⚠️ 每条进展因含「通报时间」都会被判 `first`，合并成一行**依赖匹配到同一告警**
（`event_log_id`）；若某条通报匹配不到告警且与速报间隔超过 `_SOS_FIRST_MERGE_SECONDS`，
可能与同事件拆成两行 —— 这是该形态的已知边界（当前廊坊现场未出现）。

## 通报↔告警匹配顺序（2026-11-05）

1. **时间精确同秒**（触发态时间）是唯一必需信号；告警值只在报文给了【告警值】且同秒
   多条时用于收窄。
2. **兄弟证据**：同 `chat_id` + 同 `event_time` 的另一条通报（速报 / 首报）已配上唯一
   `guid`，且该 `guid` 就在本条并列组里 → 直接继承，`match_detail` 带 `+sibling`，零模型。
   兄弟指向多个 `guid`（同秒两起不同事件）或 guid 不在并列组里时**不继承**。
3. 仍分不出 → 整组交一次模型仲裁（`match_detail` 带 `+llm`）；判不出留空，不配优于配错。

**不设每轮仲裁次数上限，也不设并列候选条数上限**（2026-11-05 删掉这两处自加的成本控制：
前者让 49 条积压按 30 分钟一轮要 8.5 小时才消化，后者让并列超过 6 条的通报永远配不上）。

## 踩过的坑

- **不能按固定滑窗补**：实时写入会把该群的 `max(create_time)` 推到「此刻」，
  `[now-2N, now]` 的窗口永远覆盖不到停机那段空洞（2026-10-25 前就是这么写的，
  停机超过 2N 的消息会永久丢失）。必须按「上次成功补到的水位」补。
- **WS 事件的消息类型字段是 `message_type`**，拉取侧才是 `msg_type`（`list` 返回的
  对象）。写错不会报错，只是全被当成非文本丢掉。
- **推送路径的空名会盖掉真名**：WS 只给 `open_id`（通讯录缺权限/离职时为 `''`），
  `ON CONFLICT DO UPDATE SET sender = EXCLUDED.sender` 会把回补时拉到的真名冲成空。
  改为 `COALESCE(NULLIF(EXCLUDED.sender, ''), dcim_incident_reports.sender)`。
  ⚠️ 右侧**必须带表名**限定，否则 PG 报 `column reference "sender" is ambiguous`
  （SET 目标列同名，已在 PG 上实跑确认）。
- **建表**：`INCIDENT_STATS_ENABLED` 关着时没人建 `dcim_incident_reports`，所以
  `realtime.start()` 自己 `ensure_reports_schema()` 一次（失败只 warn，不阻塞启动）。
- **eWatch 报文有两种形态，「时间：」不能按行首锚定**（2026-10-29 修，旧写法已推翻）：
  现场既有全多行（每个字段独占一行），也有**全挤在一行**的（`对象:… 机房:… 时间：… 监控项：…`）。
  原 `(?m)^\s*时间\s*:` 只认行首 → 单行形态永远取不到发生时间 → `build._occurred_at`
  退回消息 `create_time`，表里「发生时间」与「速报时间」一模一样（实测差 67 分钟，可能算错班次）。
  现改为「标签前否定环视 + 截到下一个字段标签」：`(?<!恢复)(?<!发生)(?<!告警)(?<!产生)时间\s*[:：]\s*(.+?)(?=对象|机房|时间|监控项|告警值|描述|级别|来源\s*[:：]|\n|$)`。
  **截断是必须的**：描述里常写「预计 2026年10月30日 完成」，不截断会把它当成发生时间。
- **模型兜底，但不许乱填**（`incident_stats/extract.py`）：确定性解析拿不到发生时间/告警值时，
  让模型从原文抽（`INCIDENT_EXTRACT_LLM_*` 三个配置，默认开、每轮最多 20 次、缓存 30 天）。
  模型给的值仍要过 `feishu.value_to_epoch` 与区间校验（消息时间 −7 天 ~ +5 分钟），
  不合格直接丢弃。**模型也抽不到时「日期」照常用速报当天、「发生时间」留空**——
  不再用消息时间充当发生时间（用户口径：识别不出来的不要乱填写）。
- **候选告警必须按「触发态时间」查，不能查当前态 `alarm_time`**（2026-11-05 修）：
  `dcim_alarms` 每条刷新(2)/恢复(3)推送都会重写 `alarm_time`/`alarm_value`
  （`parse_alarm` 全写 + `apply_batch` 整行 upsert），只有 `dcim_alarms_history` 里
  第一条 `status='1'` 的 `payload->>'timestamp'` 不变。按当前态查时，只要告警在通报到达前
  收到过一次刷新/恢复，候选就查不到、通报留 `event_log_id IS NULL`，与速报**裂成两行**。
  现 `source.alarm_candidates` 走历史表（部分表达式索引 `idx_dcim_alarms_history_trigger_ts`），
  无历史行的老告警仍回退当前态（`UNION ALL ... NOT EXISTS`，逐行等价）。
- **展示侧也必须用触发态时间、地点用空间全名**（2026-11-05 修）：`source._ALARM_SQL`
  增列 `trigger_time`（同一个历史表 `status='1'` 条件），`build._occurred_at` 优先取它，
  当前态 `alarm_time` 只作老告警兜底 —— 否则「匹配按触发时刻、展示却是恢复时刻」，
  `build_rows` 按发生时间过滤窗口时还会把行归到错误班次。「地点」直接取告警
  `space_guid` 对应的 `dcim_spaces.name` 全名（如 `廊坊园区A1数据中心M4模组一层101机房`，
  与 `query_alarms.space_name` 同源），取不到才降级到楼宇 `rules.place` → 园区；
  不要再爬到 `space_type='1'` 祖先（那会丢掉房间/楼层/模组）。
- **同一告警补发多条通报 → 覆盖，不新增行**：分组只认 `event_log_id`（**绝不按时间配对**，
  同秒也可能是不同告警）；`速报时间`/`首报时间`/违规判定取**最早**那条，正文/名称/类型/地点
  按 `create_time` **从新到旧取第一个非空值**（后发覆盖；只写进展、不重复【事件概述】的补充
  通报不会把名称冲空），`事件进展` 扫**全组**正文：`事件结束` 的处理结果优先，
  否则取编号最大的 `进展同步N`（旧格式 `【进展描述】` 兜底）。
- **Python 相邻字符串拼接不带分隔**（2026-11-05 事故）：`' UNION ALL'` + `'SELECT …'`
  拼成 `UNION ALLSELECT`，PG 报 `syntax error at or near "ALLSELECT"`。判别特征三件套：
  该报错 + 未匹配行 `llm_tried_at` **全空** + 日志里没有 `[Incident] 通报匹配：待匹配 N，
  本轮成功 M`。`match_one` 第一条就抛错，而 `_match_pending` 当时没有逐条兜底 → **整轮中断**，
  21 条有兄弟的首报永远等不到仲裁。现补 `test_incident._assert_sql_tokens`（每个 `SELECT`
  前必须是空白，抓 `)SELECT` / `ALLSELECT` 这类粘连）并给单条匹配加 try/except（一条坏数据
  不再拖垮整批）。

## 配置

```bash
LARK_ENABLED=true                                   # 前置：WS 长连接是唯一入口
FEISHU_GROUP_INGEST_ENABLED=true
FEISHU_GROUP_INGEST_CHATS=oc_xxx,oc_yyy             # 为空 = 不启用
FEISHU_GROUP_INGEST_BACKFILL_MINUTES=30             # 补偿循环间隔
FEISHU_GROUP_INGEST_MAX_BACKFILL_HOURS=72           # 单次最多往回补多久
INCIDENT_FEISHU_PARK_CHATS={"廊坊":"oc_xxx"}        # 园区映射：cluster 与补偿循环都要它
```

**这两个 chat_id 清单必须一致**（`FEISHU_GROUP_INGEST_CHATS` 缺 → 收不到实时推送、只能靠
30 分钟回补；`INCIDENT_FEISHU_PARK_CHATS` 缺 → 能入库但 `cluster` 为空、配不上告警也进不了表）。
启动时会双向对账并各打一条 warning（`realtime.start()`），只告警不阻断。

## 新增园区上线步骤

1. **机器人进群**（飞书群里添加应用），确认已开通 `im:message.group_msg` 并发布版本。
2. **两处配置同时登记**：`INCIDENT_FEISHU_PARK_CHATS` 里加 `"平湖": "<chat_id 或群名>"`，
   `FEISHU_GROUP_INGEST_CHATS` 里加同一个 chat_id。值可以写群名（精确优先、再子串匹配），
   但两处最终必须指向同一批 chat_id。
3. **`--dump` 验一遍真实报文**（规则都是按真实报文调的，别跳）：
   ```bash
   python -m service.scheduled_task.incident_stats.cli --dump 平湖园区动环通报群 \
     --start 2026-11-01 --end 2026-11-06 --out /tmp/raw.json
   ```
   看 `kind` 是否分出 sos/first、发生时间/告警值能不能解析出来。
4. **重启后端** → 进程内园区映射缓存 5 分钟内生效，补偿循环开始按新群推进水位。
5. **替换占位群时先删配置、再清数据**：把旧占位 chat_id 从上面两处**移除** → **重启后端** →
   再执行清理（dry-run 看行数与时间范围，确认后加 `--yes`）：
   ```bash
   python -m service.scheduled_task.incident_stats.cli --purge-chat oc_旧占位群
   python -m service.scheduled_task.incident_stats.cli --purge-chat oc_旧占位群 --yes
   ```
   **顺序不能反**：它还在映射/白名单里时 `sync_gaps` 会按 72h 窗口把数据重新拉回来。
   真删（`--yes`）时一并清掉 Redis 回补水印 `incident:backfill:ts:{chat_id}`；历史 xlsx/run 记录不动。
6. 以后新增园区只改配置，**不再新增预设**（定时任务只有一条 `incident__all__shift`，
   已配置的园区自动并入同一张表）。

## 健康指标

`GET /api/v1/health` 在 `LARK_ENABLED=true` 时多一段 `checks.feishu`：
`leader_present`（Redis 有无 `lark:leader` 键，集群级）、`leader_here`（本 worker 是否
leader）、`ws_thread_alive`、`last_event_at` / `last_event_age_s`（Redis
`lark:last_event_at`，7 天 TTL）。

**它不并入 `ok`**：4 个 worker 里只有 1 个是 leader，并入会让 Docker `HEALTHCHECK`
把正常的 follower 判死。监控按 `leader_present=false` 持续 > 2 分钟报警。

飞书侧运维动作：申请 `im:message.group_msg`（获取群组中所有消息，**敏感权限**，需管理员
审核）并**发布版本**。没有它时，只有 @ 机器人的消息会推送过来（WS 一直有，但飞书不推
非 @ 的群消息）。若要收其他机器人的消息，另加 `im:message.group_msg.include_bot:read`。

**权限未开通时，实际新鲜度 = 补偿间隔**：`message.list` 拉取不依赖那个敏感权限，补偿
循环照样能拿到群消息，所以环境里若看到「推送迟迟不来」，把 `FEISHU_GROUP_INGEST_BACKFILL_MINUTES`
调小（如 5，对齐原来的 300s 轮询）即可，不用改代码。

生产环境 2026-10-22 已开通该权限，因此保持默认 **30**：实时推送是主路径（秒级），
补偿循环只负责断线/进程重启的兜底（按水位补，最远追 72 小时）。

语义提醒：这个权限意味着应用能读**机器人所在所有群**的消息 —— 所以白名单与日志留存
（每条入库都打 `chat_id`/`message_id`/`kind`）要能自证。

## 验证

```bash
python -m pyflakes service/feishu service/scheduled_task/incident_stats
python -m service.scheduled_task.incident_stats.tests.test_incident
```

生产测试群：

1. 发一条速报 → 10 秒内 `dcim_incident_reports` 出现该行（`kind='sos'`）
2. 发一句闲聊 → 落 `kind='other'`，不参与匹配
3. 同一条消息重复推送 → 仍只有 1 行（`UNIQUE(chat_id, message_id)`）
4. 重启后端 → 断线窗口内的消息被补偿循环补回，且 `sender` 不被空值覆盖

## 不做

- 群里所有消息都进对话 Agent
- 告警消息自动回复工作流（后续另议；形态＝人工发的速报/通报 + 纯数据模板，不调模型）
- 主动往群推送、企业微信
- **不开启** `INCIDENT_STATS_ENABLED`（那会建事件统计表的定时预设）

# 运营监控中心告警不合规检查（班次 Excel + 独立页面）

> 2026-10-27 上线。本轮**只上线页面**：检查项卡片 + 产出目录 + 无数据时的空状态。
> 定时执行代码已就绪但**处于关闭状态**（`COMPLIANCE_ENABLED=false`），需要时改配置并重启即可。

## 定位

值守按班次记录告警工单（早 09:00 查前一晚班、晚 18:00 查白班）。本功能把四项告警处置口径
变成**班次产出**：每班次一份 xlsx《运营监控中心告警不合规检查》，一个检查项一个 sheet，
另有「总览」sheet（检查项清单 + 园区 × 检查项矩阵）。

与「分析报告」「事件统计表」同属**确定性流水线**：不调 LLM、不进 agent 会话，纯 SQL +
openpyxl。同一窗口两次跑结果一致。

## 四项检查的口径

`alarm_unclosed_order` / `alarm_premature_close` / `alarm_repeat` 与对应技能逐字对齐；
「告警响应超时」是**已响应但慢**（对齐 `query_alarm_orders(response_timeout_min=N)`），
**不是**技能 `alarm_response_timeout`（那是「还没有工单 = 没人响应」，另一回事）。

| 检查项 | 判定条件 | 时间归属 | 级别 |
|---|---|---|---|
| 告警响应超时 | 有处置工单，且 **工单 `response_time` − 告警 `alarm_time` > N 分钟**（默认 5） | **`response_time`** 在窗口内 | 重要及以上 |
| 告警未关单 | 工单 `status = 1`（处理中） | **`response_time`** 在窗口内 | 不限 |
| 告警未解除关单 | 工单 `status = 2`（已关单）＋ 关联告警 `status ∈ ('1','2')` | **`end_time`（关单时间）** 在窗口内 | 不限 |
| 重复告警 | 同「自然日 × 园区 × 空间 × 测点 × 告警内容」≥ N 次（默认 3） | `alarm_time` 在窗口内 | 重要及以上 |

「重复告警」sheet 的列**对齐值守台账**《数据中心处置不合规统计表》（参考件
`副本数据中心处置不合规统计表（第43周).xlsx` 的「43周重复告警」）：`序号 / 大楼名称 / 告警时间 /
等级 / 次数 / 地址名称 / 设备名称 / 监控项名称 / 告警内容 / 处理过程 / 处理结果 / 问题是否解决 /
解决办法 / 填报人`，后接补充列 `首次时间 / 跨度(分钟)`。其中：

- `大楼名称` 取最近 `space_type='1'` 祖先（「廊坊园区A1数据中心」）→ 去前缀后缀 → `LF-A1`；
- `告警时间` 取组内**末次**发生时间（`首次时间` 另有列）；
- `处理过程` / `处理结果` 取组内最后一次告警关联的工单（`process` / `process_result` 解码，
  无工单按告警状态降级为「已解除 / 未处理」）；
- `问题是否解决` / `解决办法` / `填报人` **留空**，由值守人工填写（参考表同样为空）。

每张 sheet 的 **第 1 行是标题**（如「告警响应超时统计 · 2026-10-27 夜班（…）」）、第 2 行是
表头、第 3 行起数据。接口按「**第一个非空单元格 ≥ 2 的行**」识别表头（`_sheet_rows`），
所以老产物（表头在第 1 行）与新产物都能读。标题只占 A1 一格 —— 写成多列会被误判成表头。

要点：

- 全部**全平台**（不传 cluster，行内带园区列），一个产物里用 sheet 区分。
- `alarm_time` / `response_time` / `start_time` / `end_time` 都是**秒级 epoch**；时间一律
  服务端按 `Asia/Shanghai` 渲染成字符串，表格里不出现裸时间戳。
- 「告警响应超时」只出**有工单且响应慢**的单；完全没有关联工单的告警（「还没人响应」）
  **不进这张表** —— 那是技能 `alarm_response_timeout` / `query_alarms(unresponded_min=…)` 的活。
- 「告警未解除关单」用 **JOIN** 而不是 LEFT JOIN：库内查不到告警行的单**不计入**
  （无从证明"未解除"）。
- 「重复告警」按**自然日**分组：夜班跨零点 → 同一设备可能出现两行日期，与技能「同一天」口径一致。
- 轮次内自洽：`run_all()` 把同一个 `now_s` 传给四项检查，「已持续 / 未关单时长」不会因
  检查先后差几秒而互相矛盾。

阈值均可配（`config/settings.py`）：`COMPLIANCE_ALARM_TIMEOUT_MINUTES`、
`COMPLIANCE_REPEAT_MIN_COUNT`。检查项的**口径说明文案**由这两个配置拼出（同源，改配置后
页面与表头不会说谎）。

## 班次窗口算法

白班 08:30–17:30，晚班 17:30–次日 08:30（`checks.DAY_START/DAY_END`）。

| 触发时刻 | 取哪个班次 | 期号 |
|---|---|---|
| 09:00（cron） | 夜班：昨日 17:30 ~ 今日 08:30 | `2026-10-27-n` |
| 18:00（cron） | 当前白班：08:30 ~ 17:30 | `2026-10-27-d` |
| 其它时刻手动触发 | **最近一个已结束的班次**（未结束的班次不查） | 同上规则 |

窗口由**平台算好**，不交给模型、不做人工推算。`period_key/period_label` 落库供页面按期
展示（如「2026-10-27 夜班（10-26 17:30 ~ 10-27 08:30）」）。

## 产物与页面

- 产物：`{AGENT_ANALYSIS_DATA_DIR}/{thread_id}/{dataset_id}/compliance_YYYYMMDD_{night|day}.xlsx`。
  **文件名纯 ASCII**（`resolve_artifact_path` 不解码 URL，中文名会静默 404）；下载时另拼中文名。
- 全 0 行**不是失败**：照出空表（表头完整），摘要写「本班次未发现不合规项」——页面上能看出
  这一班确实查过了。
- 页面：左侧「AI 能力 → 不合规检查」（`/compliance`，权限 `ai:agent`）。
  - **顶部大卡是「数据中心事件统计表」的最近一期**（2026-10-27 加，与不合规检查同属值守视角；
    位置就是原来「最近一期大卡」那一格，筛选条之下、期卡网格之上）：
    班次（白班 / 夜班，图标与色调同期卡）+ 日期 + 窗口 + **总条数**（一期一份合并表，不按
    园区拆开展示；有任务失败时挂一个「N 个任务失败」徽章），右上「下载本期全部」逐个下
    （浏览器对连续下载有节流，间隔 300ms）。数据来自
    `GET /scheduled-tasks/incidents`（一期一条 all run；历史期仍是各园区一条，选择规则见下）；
    **没有产出时保留这张卡**，用一段说明文案代替数字。卡片与不合规检查的数据互不依赖，
    任一方接口失败都不会让另一块空白。
  - **大卡可点开「整期合并」明细看板（2026-11-04，2026-11-06 多园区合并后简化）**：这张表是
    **当期所有园区的一张表，不按园区切换**。现在**一期一条 all run、一份合并 xlsx**（后端把
    多园区拼成一张只是历史期的兼容路径）：后端取该期**最新一条**运行（重跑覆盖旧产出，避免同批
    行出现两次），若一份产物里就有多个 Sheet 也按同名 Sheet 拼接。明细按 `(发生时间, 事件名称)`
    升序、`序号` 在整期范围内重排 1..N，**列与 xlsx 完全一致（22 列），不插园区列**（园区已在
    「地点」里）；`事件汇总` sheet 丢掉各园区自带的「总计」行、按各列重算唯一一行总计。
    前端看板（`IncidentBoard`）顶部是返回目录 + 期号 + 窗口 + 下载 Excel + 失败徽章，下面是
    Sheet 切换（`{N}周` / `事件汇总` + 行数）与 `RecordsTable`。宽表用 `RecordsTable` 的
    `adaptive` 模式：**按非空内容自适应列宽**（长文本列最高 480px、短列收窄），单元格
    **多行展示不截断**、行高随内容增长，不需要手动拖列宽（其他表格仍走原紧凑模式）。
    可点条件＝该期至少有一个成功产物；「下载本期全部」按钮 `stopPropagation`。
    **产物文件丢失/过期不是错误**（2026-11-04）：`/sheets[/{name}]` 仍返回 200，带
    `available: false` 与 `reason`，看板显示空状态（不再报 404，也不再出现红色错误）；
    404 只留给「sheet 名在产物里不存在」这类真实错误。
    **防重复计数**：同一期可能并存旧 per-park run 与新的 all run，列表与明细共用
    `api/v1/scheduled_tasks.py` 的 `_pick_incident_runs` —— 有 success 的 all run 就只取它；
    没有则回退 per-park（历史期行为不变）；只有 failed 的 all run 也显示（带 `error` 与失败
    徽标）。**两种 run 绝不累加**（同一批行会被算两遍）。
  - **事件统计表按班次出、全部园区合并（2026-11-06）**：预设只有一条 `incident__all__shift`
    （cron `{0-4} 9,18 * * *`，每班结束即出表；文件名 `incident_{YYYYMMDD}_{d|n}_all.xlsx`），
    期号/窗口直接复用 `compliance.checks.shift_window()`，与不合规检查的班次卡**逐字对得上**。
    **不再出周报**（用户口径），旧的 `incident__{园区}__weekly` / `__shift` 预设自动停用
    （不删除，历史产物照常可看）。`/incidents` 的 `period=shift|weekly` 参数保留，只为看历史期。
    **按期按 `triggered_at` 倒序**而不是 `period_key`：班次期号 `-n` / `-d` 同一天里 `-n`
    字典序在后却属于「昨天傍晚开始的夜班」，周报的 `W` 又大于数字结尾，期号排序会乱。
  - **不合规检查本身是班次卡片墙**（2026-10-27 重做）：筛选条（日期区间日历 + 班次类型，下拉对齐分析报告页）→
    **期卡网格**（每期一张卡：班次类型、期号、总命中、四项迷你条、生成时间/耗时、单独下载）。
    总命中 0 的期左侧标绿；失败/无产物的期走虚线边框，不画条。
  - **日期区间筛选（2026-10-29）**：原来的「全部时间 / 近 1 个月 / 近 3 个月 / 近 1 年」预设换成了
    双月区间日历（`web/src/components/date-range-filter.tsx`，base-ui Popover + react-day-picker）。
    区间按**期号日期**过滤：`GET /scheduled-tasks/compliance` 的 `start/end`（`YYYY-MM-DD`，含起止两天）
    在 `repository._runs_query` 里转成 `period_key` 的 `>= start` 且 `< end+1 天`（字典序即日期序），
    列表与 `status_counts` 共用同一条件，保证「共 N 期」与 ✓✗ 计数同源。`days` 参数保留未删。
  - 卡片上的数字来自运行记录的 `check_counts`（生成时落库）；老记录没有该字段时显示 `—`，
    仍可点开看板。**不要**在列表接口里逐个打开 xlsx 算行数。
  - **详情是整屏看板**：四张指标卡（命中数 + 口径 + 较上一同类班次的增减，点击切明细）+
    本期构成堆叠条 + 近 10 期总命中柱图 + 检查项分段控件 + 明细表格。
    明细复用 `RecordsTable`（搜索/排序/翻页免费），预览与下载是同一份文件。
  - 检查项**完全由后端注册表驱动**（`GET /scheduled-tasks/compliance/spec`）：前端只按 `key`
    维护图标/色调映射，未知 key 走兜底图标。没有产物时展示四张口径卡（同样来自注册表）。
- 端点（均在 `/scheduled-tasks` 前缀下、声明在 `/{task_id}` 之前，权限 `ai:agent`）：
  `/compliance/spec`、`/compliance`、`/compliance/runs/{run_id}/sheets`、
  `/compliance/runs/{run_id}/sheets/{key}`、`/compliance/runs/{run_id}/artifacts/{filename}`；
  事件统计表大卡用 `/incidents`（`period=shift|weekly` 默认 `shift`，最近 N 期的 run；
  新期一条 all run，历史期仍是各园区一条）、
  `/incidents/periods/{period_key}/sheets`（整期 Sheet 清单 + 合并后行数）、
  `/incidents/periods/{period_key}/sheets/{name}`（**不套 `{ok,data}`**，直接给 `RecordsTable`
  消费）与 `/incidents/runs/{run_id}/artifacts/{filename}`（下载）。
- 事件统计表的**事件条数**从 `final_reply` 里的「共 N 条已通报事件」解析（空表写「本期无通报
  事件」→ 0），不额外落库字段；取不到时前端显示 `—`。窗口从同一段文本里解析，班次带时分。

## 开关与预设

- `COMPLIANCE_ENABLED=false`（默认）：`ensure_compliance_presets()` 直接返回空，不建任务、
  不跑 cron；页面只有空状态，**没有「立即生成」按钮**。
- 改成 `true` 并重启：启动时幂等建系统预设 `compliance__alarm_shift`（`created_by='system'`、
  共享只读、cron `0 9,18 * * *`）。`runner.run_task` 按 `template_id` 前缀 `compliance__`
  路由到 `compliance.workflow.run_compliance_task`。
- 三族 system 任务的 stale 清理互不误停：`report_templates` 的 `exclude_prefixes` 在
  `compliance_presets.enabled()` 为真时排除 `compliance__`（关闭时不排除 → 正好被统一停用）。
- 预设失败通知发给运维（`repo.alert_user_ids()`），同一天的失败按批次聚合成一条。
- 事件统计表另由 `INCIDENT_STATS_ENABLED` 控制（默认关）：开启后只建一条
  `incident__all__shift`，旧的 per-park 预设自动停用；关闭时也会把库里仍在启用的旧预设停掉
  （只停不删，历史期产物照常可看）。

## 上线前核对口径

```bash
# 纯逻辑自检（不连库）：班次窗口/格式化/行变换/列与行键一致/空表结构
python -m service.scheduled_task.compliance.tests.test_compliance

# 真实数据出表（需 DCIM_PG_DSN；只读 DB，不写 Mongo）
python -m service.scheduled_task.compliance.cli --shift night --out /tmp/compliance.xlsx
python -m service.scheduled_task.compliance.cli --shift day --date 2026-10-27 --out /tmp/d.xlsx
```

## 以后加检查项（如周维度）

1. `checks.CHECKS` 加一条（`key/name/cadence/description/sheet/title`，`title` 是 sheet 第 1 行）；
2. `checks.COLUMNS[key]` 声明列（顺序即展示顺序；测试会校验行键与列一致）；
3. 写查询函数（返回**已是中文列名**的行）并注册进 `checks._RUNNERS`。

xlsx（新 sheet + 总览行 + 矩阵列）、API（`/spec`、sheet 列表与详情）、前端（卡片、tab）
全部自动跟上 —— **前端不写死任何检查项名字**。周维度如果窗口不同，再在 `shift_window`
旁边加一个窗口函数、在 workflow 里按 `cadence` 选择即可。

## 边界

- 不给 `dcim_meta` 能力层加过滤参数（技能/对话工具一行不动）；取数自己写 SQL。
- 一个班次一个产物、全平台一份，行内带园区列；不做园区分级产物。
- 供应商服务工单仍走 agent 报告路径，与本功能无关。

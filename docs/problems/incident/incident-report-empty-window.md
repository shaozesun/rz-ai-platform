# 事件统计表「查了没有」：不是没收消息，是事件落在别的班次

> 2026-10-26 | 现象 → 证据 → 根因 → 口径 → 修法

## 一、现象

用户在飞书园区群刚发了两条通报（速报 + 通报），随即问「查一下事件统计表」，
模型回答本期没有事件。用户据此怀疑机器人收不到飞书消息。

## 二、证据（生产，2026-10-26）

| 项 | 证据 |
|----|------|
| 实时入库正常 | `09:56:00 [Incident] 实时入库 … kind=sos`、`09:56:19 … kind=first`，均匹配到告警 |
| WS 正常 | `/api/v1/health` → `checks.feishu = {leader_present: true, last_event_at: "2026-10-26 09:56:19"}` |
| 取数到 2 条 | `09:57:41 sync:226 [Incident] park=廊坊 拉取 2 条（新增 0）` |
| 出表 0 行 | `09:57:41 build:201 [Incident] 事件统计：通报 2 条 → 事件 0 行` |
| 通报本身 | `dcim_incident_reports` id=98/99，`chat_id=oc_451f…`，`event_log_id=6ade173cdae3c900015a6755` |
| 告警本身 | `dcim_alarms` 该 guid：`水泵停机报警`、`M2-3#一次泵 / 变频器运行状态`、**`alarm_time=2026-10-25 22:50:36`** |
| 通报消息时间 | `create_time`：09:55:59 / 09:56:19（10-26 上午） |

即：**消息收到了，事件发生在昨夜 22:50**，今早才补发速报/通报。

## 三、根因

1. `build.build_rows` 按 **`occurred`（发生时间）** 归属统计窗口（`build.py:188`）；
   默认窗口是**当前班次** = 10-26 白班 08:00–18:00，22:50 的事件属昨夜班 → 被过滤。
2. 0 行时工具只回一句「该窗口没有已通报事件（或机器人尚未进群，进群前的历史消息拉不到）」，
   模型据此答「没有」——用户完全看不出「消息收到了、只是算在上一班」。

**不是缺陷的部分**：接收、匹配、入库、班次计算均正常。查 `start=2026-10-25`
（`load_reports` 有 ±1 天 pad）能正常出这 1 行。

## 四、口径决定（用户 2026-10-26）

- **保持按事件发生时间归属**：昨夜发生、今早补录的事件**不进今天白班**（台账口径不变，
  不改成按通报时间、也不做「两窗口并集」）。
- **工具必须把真实情况说清楚**，回答要说明本次查询窗口与归属口径。

## 五、修法

| 文件 | 改动 |
|------|------|
| `service/scheduled_task/incident_stats/build.py` | 新增 `outside_events(start_s, end_s, cluster_prefix, chat_ids, limit=5)`：取 `create_time` 在窗口内的通报，复用 `_group_reports` / `_occurred_at` 求 `occurred`，返回**发生时间在窗口外**的补录事件 `{reports, items[{occurred,cluster,name}], truncated}` |
| `tools/incident_tools.py` | `rows=0` 时调 `_outside_hints()` → `_empty_note()`，写出「窗口内收到 N 条通报，事件发生时间在 <时间>（<班次>），请查 `start=<日期>`」；结果固定带 `window_basis`；`_shift_at()` 复用 `_current_shift`，班次规则仍只有一份 |
| `skills/incident_report/SKILL.md` | 0 行必须转述 `note`、禁止猜「机器人没收到」；回答开头写 `window` + `window_basis`；口径补「按发生时间归属」 |
| `tests/test_incident.py` | 新增 `test_outside_events` / `test_empty_note` |

自检：`python -m service.scheduled_task.incident_stats.tests.test_incident`。

## 六、边界

- 探测只看「**消息时间**在本窗口」的通报（正是补录场景）；若补录晚于窗口结束超过窗口长度
  （如半夜补录昨天的），该次查询看不到——但那时窗口内通常已有其它事件，不会误报「没有」。
- `build_rows` 取通报带 ±1 天 pad，所以按发生日期查询时，次日补录的通报能正常回来。
- 定时预设（`INCIDENT_STATS_ENABLED`）仍按班次窗口出表，口径不变；本次仅改对话入口的解释文案。
- 修复需重建后端镜像才生效。

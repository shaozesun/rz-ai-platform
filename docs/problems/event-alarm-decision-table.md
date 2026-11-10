# 事件工单 ↔ DCIM 告警：从「结论快照缓存」改为逐次发生的判定表（2026-11-09）

> 现象：全国事件报告把一批廊坊工单判成「非廊坊 / 未覆盖」，廊坊园区报告随后**原样命中
> 同一批结论**，一条告警都没关联；另一批期次又因为缓存里的负结论永久不再重判，
> 直到有人手工 bump 版本才恢复。对话里想查「这张单关联到哪条告警」只能重跑报告逻辑。
> 结论：不是匹配算法的问题，**是持久化的粒度和键设计错了**。改为「一行 = 一次发生」
> 的判定表（键含口径），报告与对话共用，并按状态机决定何时重查。

---

## 一、旧设计的问题（`alarm_cache.py` / 集合 `event_alarm_links`）

旧缓存把「匹配结论」按 `_id = sha1(代表单号, 类目名)` 冻结，粗粒度且无维度：

| 问题 | 后果 |
|---|---|
| 键里没有**口径**（园区 / 全国） | 全国报告判的「非廊坊」被廊坊园区报告命中（W45 事故），园区报告一条都关联不到 |
| 粒度是**类目**不是**发生** | 同类的第二次发生命中第一次的结论；新增发生只有 `force_refresh` 才会重算 |
| 只存**展示快照**，不存告警身份 | 无法按 `(cluster, event_log_id)` 追溯/刷新，展示值只能一直用旧快照 |
| 负结论**永久缓存** | DCIM 数据晚到（2026-10-29 才有数据）时，先跑的报告把「无告警」写死，规则一变只能手改 `CACHE_VERSION` 全量失效 |
| 对话里查不到 | 用户问「这张单关联到哪条告警」，只能重新跑一遍报告链路 |

## 二、新设计：判定表（`report/alarm_decision.py`）

Mongo 集合 **`event_alarm_link_decisions`**，**一行 = 一次发生**。

```python
_id = sha1(f'{ticket_no}\x1f{occurrence_ts}\x1f{scope}')
{
  'ticket_no', 'occurrence_ts', 'scope',          # scope = 园区 slug 或 'nationwide'
  'name', 'dc', 'rule_version', 'coverage_fingerprint',
  'status',                                       # matched / not_covered / no_alarm / ambiguous
  'match',                                        # point / time / llm / ''
  'alarm': {'cluster': '廊坊A', 'event_log_id': '…'},   # 告警身份（唯一可追溯的键）
  'point_sig': ['廊坊A', '<device_guid>', '<point_guid>'],
  'alarm_ts', 'alarm_snapshot',                   # 触发时刻 + 当时展示字段（兜底用）
  'reason', 'source', 'decided_at', 'recheck_after',
}
```

- **口径进键**：`normalize_scope()` 把园区名/短名归一成 slug（`廊坊园区` → `廊坊`），
  空/`全国`/`nationwide` → `nationwide`。同单同时间、不同口径**互不串用**。
- **缺关键字段不产键**：没有工单号或发生时间（`None`/`0`/非数字）→ `decision_id()` 返回
  `None`，不落表（这类发生下次还能重新判定）。
- **只存「结论 + 告警身份」**：告警值/四档阈值仍**实时**从 PG 取（判定表不冻结展示值）。

### 状态机与复用策略

| status | 何时写 | 何时复用 |
|---|---|---|
| `matched` | 时间唯一 / 点位签名 / 模型仲裁成功 | 永久复用（告警身份已确定） |
| `not_covered` | 明确非廊坊园区，或发生时间早于 `2026-10-29` | 复用；`coverage_fingerprint` 变了才重判 |
| `no_alarm` | ±120s 内无候选 | `recheck_after` **之前**每次重查，之后永久复用 |
| `ambiguous` | 多候选、模型判不出（非调用异常） | 复用；显式 `refresh` 才重判 |
| `unknown` | 公司档案不可用 / 仲裁调用异常 | **不落表**，下次重试 |
| `no_time` | 缺发生时间 | **不落表**，下次重试 |

读取时校验 `rule_version` 与 `coverage_fingerprint`（`廊坊A|廊坊B1M1|廊坊B1M2@2026-10-29`），
任一不一致视为未命中 —— 规则或覆盖范围变了，旧结论自动失效，**不再需要手工清缓存**。

`no_alarm` 的重查时刻 = `max(decided_at + 30min, occurrence_ts + 24h)`：事件刚发生时 DCIM
数据可能还没落库，先给 24 小时窗口反复重查；超过窗口仍无候选则永久复用。
（老事件已过 24 小时 → 取 `decided_at + 30min` 兜底。）

**强制刷新**：报告侧 `SCHEDULED_TASK_REPORT_ALARM_REFRESH=true`、对话侧 `refresh=true`，
跳过读取、重算后按**同一 `_id`** 覆盖写回。

**失败隔离**：读写 Mongo 失败一律 fail-open（读按未命中、写只告警），**绝不让报告失败**。

### 为什么用 Mongo 而不是 PG

判定对象是 **mgmt 工单**，不是 DCIM 域数据；报告链路本来就有 `mongodb_manager`，
失败只降级；无需建表/迁移，且 `bulk_write` 天然按 `_id` 幂等覆盖。
告警值/阈值这类 DCIM 域数据仍留在 PG，**判定表只存身份**。

## 三、报告侧改造

1. **候选按触发态时间查**（`repository.find_alarms_by_trigger_time`）：
   `dcim_alarms_history` 里每个 `(cluster, guid)` 第一条 `status='1'` 的
   `payload->>'timestamp'`（已有表达式索引 `idx_dcim_alarms_history_trigger_ts`），
   无历史行的老告警才回退当前态 `alarm_time`。
   原因：`apply_batch` 对每条推送整行 upsert，当前态 `alarm_time` 会被刷新(2)/恢复(3)
   覆盖；用 `alarm_time ±120s` 会漏掉「晚到通报」对应的告警。
2. **逐次发生读写**（`alarm_link.link_repeat_alarms`）：先按类目把所有发生的键批量
   `store.load`；命中的直接出结论；未命中的才查候选/仲裁；**全部判定一次 `store.save`**。
   园区档案（`_company_parks()`）只在确有未命中且需要它时才查（mgmt 抖动不影响已有结论）。
3. **展示实时补齐**：判定表里的告警身份去重后 `find_alarms_by_ids` +
   `find_alarm_trigger_values` + `find_point_thresholds`；行被软删时回退 `alarm_snapshot`。
4. **代表单点位签名复用**：代表单先搜并学 `(cluster, device_guid, point_guid)`，
   其余发生候选里有同签名 → 确定性选中、`match='point'`、不调模型。签名来自判定表或本次命中。

报告仍只渲染「重复类目」（`stats['repeat']`），但**判定表覆盖全部事件单**。

## 四、对话工具（`tools/event_alarm_tools.py`）

`event_alarm_link(ticket_no, start, end, park, refresh, link_missing)`，`DCIM_ENABLED`
时注册、常驻可见；技能 `skills/event_alarm_link/SKILL.md` 约束模型「先调工具、不得自己复算」。

流程：

1. `ticket_no` 与日期区间至少给一个；`park` 留空 = 全国口径（**A1/A18 是楼宇不是园区**）。
2. 先查判定表（按票号精确 / 按发生时间区间）。
   - 全部命中且 `refresh=false` → **不碰 mgmt、不碰 PG、不调模型**。
   - `link_missing=false` → 缺失的发生只标「未判定」，不补算。
3. 缺失且 `link_missing=true`：调 `mgmt.query_event_ticket` 拉列表（`all_companies=true`；
   区间成对传），逐条取详情，串行调 `alarm_link.link_occurrence(..., source='chat')`，
   最后批量写回。单条失败只标该行（`fetch_error`），不整单失败。
4. 交付由服务端渲染（同 `incident_tools`）：
   - 固定 **16 列**：日期 / 发生时间 / 工单编号 / 事件名称 / 部门 / 关联结论 / 匹配方式 /
     告警时间 / 等级 / 设备 / 测点 / 告警内容 / 告警值 / 四档阈值 / 越限档 / 说明；
   - `rows>0` 一律落数据集并给 `/records` 交互表格；**≤20 条**另给服务端
     `table_markdown`，**>20 条**只给文件卡；`rows=0` 给一句原因、不产文件；
   - 模型只负责原样复制 `table_markdown` / `file_tag` 并补结论，**不得重排、增删列或改写判定**。

关联结论文案：

| status | 文案 |
|---|---|
| `matched` | 已关联 |
| `not_covered` | 未覆盖（非廊坊或早于 2026-10-29） |
| `no_alarm` | ±2 分钟内无告警 |
| `ambiguous` | 多候选待判定 |
| `unknown` | 暂无法判定 |
| `no_time` | 缺发生时间 |

## 五、边界与已知盲区

- **只能启发式匹配**：事件单没有设备/点位/guid 字段，「已关联」靠发生时间 ±120 秒、
  机房码收敛、点位签名与模型仲裁；**不配优于配错**。
- **DCIM 告警只覆盖廊坊**（`廊坊A` / `廊坊B1M1` / `廊坊B1M2`）且自 **2026-10-29** 起才有数据；
  更早/其他园区一律「未覆盖」——**未关联 ≠ 没有告警**。
- **区间补判是串行 mgmt 调用**（同账号每接口 1 在途），大区间会慢；工具如实说明，不自行截断。
- **旧集合 `event_alarm_links`（`alarm_cache.py`）不读、不删、不迁移**：历史数据留档，
  不再基于它加任何逻辑。
- `no_alarm` 在事件发生 24 小时内可能被重查（等数据落库）是刻意设计。

## 六、验证

```bash
python -m service.scheduled_task.report.tests.test_alarm_decision   # 9 项
python -m service.scheduled_task.report.tests.test_alarm_link       # 35 项
python -m service.scheduled_task.report.tests.test_event_alarm_tool # 10 项
python -m service.scheduled_task.report.tests.test_report_spec      # 版式/章节回归
python -m pyflakes service/scheduled_task/report tools/event_alarm_tools.py 2>&1 | grep "undefined name"
```

匹配口径、阈值语义与生产实测坑见 [`../event-report-alarm-link.md`](../event-report-alarm-link.md)。

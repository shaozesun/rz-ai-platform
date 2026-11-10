# DCIM PUE / 能耗统计：入库与查询

> 2026-11-10 落地。范围：上游 `ems-real-data-rz` 里**没有 `device_id` 的 PUE / 能耗统计报文**
> 从 Kafka 接入 → TDengine 独立超级表 `st_pue` → Agent 查询能力 `dcim_meta.query_pue`。
> 配套：测点数值的宽表设计见 `dcim-timeseries-storage-design.md`，接入框架见
> `dcim-kafka-ingest-design.md`，技能正文见 `skills/pue/SKILL.md`。

---

## 一、报文形态（实测，别再猜）

同一个 topic `ems-real-data-rz` 里**混着两种报文**：

| 形态 | 判据 | 去向 |
|---|---|---|
| 设备测点 | `device_id` 非空 | 按 `device_type` 进宽表 `st_<type>`（见时序设计） |
| **PUE / 能耗统计** | **无 `device_id`、有 `building`、`points` 是 list** | 本文：`st_pue` |

PUE 报文样本：

```json
{"guid":"00000000-0PUE-0000-0000-000000000009","status":1,
 "building":"P001_A009","buliding":true,
 "points":[{"guid":"PUE_Real_IT_Value","value":21015.0,"timestamp":1794274440,
            "standardId":"PUE_Real_IT_Value","standardTypeId":"…","mgr_obj_id":""}]}
```

- **编码** = `standardId`（缺则 `guid`），格式 `PUE_<时间口径>_<指标>_Value`。
- **`timestamp` 是秒**（10 位）；`_normalize_ts` 会把秒归一成毫秒。
- **`building`** = 空间 `idc` 编码（`P001_A009`）；`buliding`（对方拼写如此）是布尔标志，忽略。
- 报文级字段 `guid` / `status` 存进 tag `pue_guid`；`points[]` 里除 `timestamp` 外没有别的时间。
- 一条报文可**混装多个时间口径**（整点时 Real 与 Hour 同秒到达）。

### 时间口径与指标（108 个属性 = 18 × 6）

| `time_type` | token | 含义 | 数据形态 |
|---|---|---|---|
| 1 | `Real` | 实时值 | 约 10 秒一条，瞬时快照 |
| 2 | `Hour` | 小时值 | 对方**整点**推的统计快照（一条含 18 个指标） |
| 3/4/5/6 | `Day` / `Week` / `Month` / `Year` | 日/周/月/年统计 | 期末推送（文档有、现网未必推） |

18 个指标列（`pue_catalog.METRICS`）：`pue`（PUE 本身）、`total_value`、`it_value`、`cool_value`、
`ac_value`、`mgr_value`、`light_value`、`af_value`、`ups_value`、`tf_value`、`vrv_value`、
`xf_value`、`itload_value`、`temp_value`、`wet_value`、`nopower_value`、`chm_value`、`other_value`。

解析规则是「**先剥时间口径、再按后缀查表**」（后缀自身含下划线，不能按 `_` 简单切）；
`resolve_metric('PUE_Real_IT_Value')` 大小写不敏感，认不出的编码**计数后跳过、绝不抛**。

## 二、存储模型

### 为什么独立超级表 `st_pue`

- PUE 报文**没有 `device_id`**、没有 `device_type`，硬塞进按类型分的设备宽表得凭空造一个类型；
- 18 个指标是**固定集合**，与设备测点「随类型增长」的列集合是两种模型，共用会让 `dcim_td_column`
  的映射语义失真。

### 表结构

```
st_pue: ts TIMESTAMP, pue/total_value/…/other_value DOUBLE,
        report_ts TIMESTAMP, quality TINYINT        （后两列由 ensure_stable 补齐）
TAGS:   cluster_name BINARY(32) | building BINARY(64) | time_type TINYINT
        | space_guid BINARY(64) | pue_guid BINARY(64)
```

- **`TAG_COLUMNS` 不动**：设备宽表的 tag 顺序是全库契约，`st_pue` 用自己的一份 `PUE_TAGS`。
- **指标列叫 `pue` 不叫 `value`**：`value` 是 TDengine 保留字（`td_schema._RESERVED`）。
- 一次写入是**稀疏列**写法：只列本片段真有值的指标列（`ts` 恒在），未列出的列由 TDengine 填 NULL。

### 为什么子表按「楼宇 × 时间口径」分

`(子表, ts)` 在 TDengine 是**整行覆盖**，而 Real（约 10 秒一条）与 Hour（整点快照）
**会在整点同一秒到达** —— 落在同一张子表就会互相覆盖。故：

- 子表：`pue_subtable(cluster_code, building, time_type)` →
  `d_<园区代号>_pue_<楼宇>_<口径>`，如 `d_lf_pue_p001_a009_1`；
- 园区代号进表名沿用既有约束（子表名全库唯一、tag 创建即固定，
  **两园区写同名子表会静默串园区**）；代号登记在 `service/dcim_ingest/park_codes.py`。
- 一条报文若混装多个口径，`build_pue_insert` 会拆成**多个 `<子表> USING st_pue …` 片段**，
  拼在一条 `INSERT INTO` 里。

### 写入链路

- `RealtimeConsumer._ingest` **先判 PUE**（`is_pue_message`）再走设备解析，样本进同一个缓冲
  `buf`（`isinstance` 在 `_flush` 里分流 —— 否则「一批只有 PUE」永远凑不出 flush 触发点）。
- `_RealtimeStore` 把 `WideWriter` + `PueWriter` 合成一个门面，两者**共用同一对 PG/TD 连接**；
  `_flush` 里**两者都写成功才 `commit`**，任一失败整批退避重试、不提交（按 `(子表, ts)` 覆盖幂等）。
- 消费线程第一次写入前 `ensure_pue_schema()` 建表（进程内锁防并发 `CREATE STABLE` 撞
  `Conflict transaction not completed`）；`ensure_realtime_schema()` 在 fork 之前也会调一次。
- `PueWriter` 不用设备档案选表、不做零阶保持、不开线程池：每批通常只有一栋楼的一两行；
  异常一律抛出交给 `_flush` 重试。`building → space_guid` 走 PG `find_spaces(idc=…)`，
  **命中永久缓存、未命中缓存 300 秒**（配置类 topic 可能晚于实时数据到达，永久缓存空值会永远缺 `space_guid`）。

### 时间戳处理

| 情况 | 处理 |
|---|---|
| 10 位秒 | `_normalize_ts` 归一成毫秒 |
| 0 / 负数 / 未来（> 现在 + 1 天） | **丢弃**，计 `bad_ts` |
| 过旧（超出 TDengine KEEP 90 天） | **回退到「到达时刻」**并计 `ts_fallback` —— 年/月统计的期初时间戳若原样写，整条 INSERT 会被拒 |
| `building` 为空 | 返回 `[]`，计 `no_building` |
| 认不出的编码 | 计数后跳过，计 `unknown_code` |

计数在 `models.pue_stats()`（读后清零），消费进度日志里打
`PUE=… PUE未知编码/…/PUE时间戳回退/…/PUE未知楼宇`。

## 三、查询能力 `dcim_meta.query_pue`

分类 `energy`、域 `pue`，处理器在 `service/query/dcim_meta/client.py::_query_pue`，
SQL 构造函数在 `service/dcim_ingest/pue.py`（显式列清单、不用 `*`；字符串一律 `sql_literal`）。

### 三种取数形态

1. **当前值**：`latest=true`（**不需要时间窗**）→ `build_pue_last_row`，每个「楼宇 × 口径」最新一行。
2. **历史趋势**：给 `start_ms` / `end_ms`（**直接传日期**，北京时间）+
   `interval`（如 `1h` / `1d`）与 `agg`（默认 `avg`；`sum` / `last` 看累计量）。

   实时值 10 秒一条，一天不聚合是 8,640 行，会被分页/工具输出上限截断。
3. **原始序列**（不带 `interval`）：`build_pue_series` 分页 + `build_pue_count` 给真实 `total`。
   **非 latest 且无时间窗直接报错**（与 `query_series` 同一口径）。

### 参数

`cluster` / `park`（园区名展开成代号列表）、`building`（idc 编码，逗号分隔）、
`space`（楼宇空间名，服务端解析成 `space_guid`；0 命中或多义报错）、
`time_type`（默认 `real`，多个逗号分隔，中文也认）、`metrics`（列名或中文名，默认 18 列全取）、
`start_ms` / `end_ms`、`latest`、`interval` / `agg`、`page` / `page_size`（默认 2000）。

### 返回

- `records` + `total` + `metric_labels`（列 → 中文，18 条，免得模型猜 `mgr_value` 是什么）。
- `ts` / `_wstart` **已渲染成北京时间**；`time_type` 已换成中文标签；每行补 `cluster` / `park` /
  `space_name`（空间全名，查不到留空，不影响数值）。
- **聚合路径的 `total` 不是精确值**：分组数不是已知量，满页时给 `offset + limit`
  （让 `analysis_load` 知道还有下一页），不满页给 `offset + len(rows)` ——
  **绝不能给小下界**，否则 `analysis_load` 会提前停、静默丢数据。

## 四、路由

- `core/agent/orchestrator.py`：`_CATEGORY_KEYWORDS` 与 `_QUERY_TOOLS` 都登记
  `PUE / pue / 能耗 / 耗电量 / 用电量` → 直达 `dcim_meta.query_pue`（PUE 是 dcim_meta 专有说法，
  mgmt 没有能耗数据，不会抢别的平台工具）。
- ⚠️ **有意不登记裸词「用电」**：它会与「容量 / 机柜」等叠加成 2 类，
  破坏「恰好 1 类才走快路径」→ 该直连的查询落到 planner 白付一次 LLM。
- `service/query/base/manifest.py`：`energy` → 能耗、`pue` → PUE，平台定位补「含 PUE / 能耗统计」。

## 五、已知边界（别当成故障报给用户）

- **只覆盖上游真推了 PUE 的园区 / 楼宇**。查回 0 行 = 该楼没上报，**不是 PUE 为 0**、也不是平台故障。
- 实时值受 TDengine **KEEP 90 天**限制，更早的实时数据查不到；年/月期初时间戳超出保留期时
  按「到达时刻」落库（会看到整点附近的时间）。
- **不同口径的数字不能互相比较或相加**：小时值是整点快照、实时值是瞬时值、月值是期末累计，
  量纲不同。要做「月度趋势」就统一取 `time_type=month`。
- `pue` 是无量纲比值（通常 1.1~1.6），耗电量类指标是 kWh 量级 —— 回答时先说清是哪一个。

## 六、自检

```bash
# 纯 assert、不连库：108 个属性解析 / 报文解析 / 时间戳回退 / 混装口径拆子表 /
# 稀疏列 / tag 顺序 / 筛选片段与转义（9 项）
python -m service.dcim_ingest.tests.test_pue

# 容器内跑（连库 + embedding）：能力已注册、路由直达、query_pue 工具已生成
python deploy/scripts/dcim_query_check.py
```

上线后只读核对：

```sql
-- 有数据、且按口径分开
select building, time_type, count(*) from st_pue group by building, time_type;
-- 最新一行含 18 列（Real 应有 PUE 与各分项）
select last_row(*) from st_pue where time_type = 1 group by cluster_name, building;
```

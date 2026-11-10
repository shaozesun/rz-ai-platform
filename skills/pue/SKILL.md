---
name: pue
title: PUE 与能耗查询
description: 查 PUE 与能耗统计（按园区/楼宇/时间口径）。用户问「现在 PUE 多少 / 某楼 PUE 趋势 / IT 与制冷耗电量对比 / 这个月耗电量」时用 dcim_meta 的 PUE 查询能力，不要反问口径。
category: energy
domain: pue
---

# PUE 与能耗查询

上游 `ems-real-data-rz` 里除设备测点外，还有一类**没有 `device_id`** 的 PUE / 能耗统计报文：
一栋楼 × 一个时间口径 × 18 个指标。平台按 `(楼宇, 时间口径)` 落 TDengine 超级表 `st_pue`，
查询入口是平台能力 **`dcim_meta.query_pue`**（工具名 `dcim_meta__query_pue`）。

> ⚠️ **只有上游真推了 PUE 的园区 / 楼宇才有数据**。查回 0 行 = 该楼没上报，
> **不是 PUE 为 0**，也不是平台故障。

## 一、先分清「时间口径」（最容易错的一步）

| `time_type` | 含义 | 数据形态 |
|---|---|---|
| `real`（默认） | 实时值 | 约 **10 秒一条**，是瞬时快照 |
| `hour` | 小时值 | 对方**整点**推的统计快照（一条含 18 个指标） |
| `day` / `week` / `month` / `year` | 日/周/月/年统计 | 期末推送（文档有、现网未必推） |

**「现在 PUE 多少」「目前 PUE 水平」→ `real` + `latest=true`。**
**「每小时 / 每天 / 每月 PUE」→ 必须显式传 `time_type=hour`（等）**，不传只会拿到实时值。
**不要把不同口径的数字放在一起比或相加** —— 小时值是整点快照、实时值是瞬时值、月值是期末累计，
量纲不同；用户要做「月度趋势」就统一取 `time_type=month`（若无数据则说明该口径未上报）。

## 二、三种取数形态

1. **当前值**：`latest=true`（**不需要时间窗**）。返回每个「楼宇 × 口径」的最新一行。
2. **历史趋势**：给 `start_ms` / `end_ms`（**直接传日期**，如 `2026-11-01`，北京时间），
   并且**时段长就带 `interval`**（如 `1h` / `1d`）—— 实时值 10 秒一条，一天不聚合是 8,640 行，
   会被行数上限截断。聚合时 `agg` 默认 `avg`，耗电量看累计量可用 `sum` / `last`。
3. **跨楼对比**：`building` 多个用英文逗号分隔；楼宇编码是空间 `idc`（如 `P001_A009`），
   用户通常只知道「A9」这类名字 → 优先用 `space`（楼宇空间名，服务端解析；多义会报错让补园区）。

## 三、常用问法与参数

| 用户这么问 | 怎么调 |
|---|---|
| 现在 A9 的 PUE 多少 | `space=A9`（或 `building=P001_A009`）+ `latest=true` |
| 某楼本月每小时 PUE 趋势 | `time_type=hour` + `start_ms`/`end_ms`=本月首末日 + 不需要 `interval` |
| 昨天 IT 与制冷耗电量对比 | `metrics=it_value,cool_value` + `interval=1h` + 日期窗 |
| 各楼今天总耗电量 | `metrics=total_value` + `time_type=day`（若该口径无数据则用 `hour` + `agg=sum`） |
| 上个月 PUE 月度值 | `time_type=month` + 覆盖上个月的时间窗 |

参数要点：

- `cluster` / `park`：园区。**A1 是楼宇不是园区**，别把 `A1` 传成 `cluster`（那会查成整个园区）。
- `metrics`：列名（`pue` / `total_value` / `it_value` / `cool_value` / `ac_value` / `light_value` …）
  或中文名（`PUE` / `总耗电量` / `IT耗电量` …）；不传 = 全部 18 个。
- 返回里的 `metric_labels` 是「列名 → 中文」，**写结论时用中文名**，别把 `mgr_value` 原样抛给用户。
- 返回的 `ts` / `_wstart` **已是北京时间**，直接展示，不要再做时区换算。

## 四、18 个指标

`pue`（PUE 本身）、`total_value`（总耗电量）、`it_value`（IT 耗电量）、`cool_value`（制冷耗电量）、
`ac_value`（空调耗电量）、`mgr_value`（设备用电损耗）、`light_value`（照明耗电量）、
`af_value`（附属设施耗电量）、`ups_value`（UPS 损耗）、`tf_value`（变压器损耗）、
`vrv_value`（VRV）、`xf_value`（新风）、`itload_value`（IT 负载率）、`temp_value`（供水温度）、
`wet_value`（湿球）、`nopower_value`（非生产用电）、`chm_value`（恒湿机）、`other_value`（其他）。

**口径提醒**：`pue` 是无量纲比值（通常 1.1~1.6），耗电量类指标是 kWh 量级；
回答时先说清「这是 PUE 比值」还是「耗电量」。

## 五、已知边界（别当成故障报给用户）

- 只覆盖**真推了 PUE 的楼宇**；未上报的楼查回 0 行。
- 实时值保留期受 TDengine KEEP 限制（**90 天**），更早的实时数据查不到；
  年 / 月这类期初时间戳若超出保留期，平台按「到达时刻」落库（会看到整点附近的时间）。
- 同一条报文里可能混装实时与小时点，平台按 `(楼宇, 口径, 时刻)` 分表，不会互相覆盖。

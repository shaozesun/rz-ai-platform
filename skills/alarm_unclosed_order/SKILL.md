---
name: alarm_unclosed_order
title: 告警未关单
description: 告警未关单：本班已响应、仍未关单的告警工单。用户问「还有哪些单没关 / 交班遗留几单 / 处理中的工单」时按本技能直接查，不要反问口径。
when_to_use: 用户问「还有哪些单没关 / 交班遗留几单 / 处理中的工单」时使用；例：「这个班还有几单告警工单没关？」
category: alarm
domain: alarm
---

# 告警未关单

口径：查**告警工单**（`dcim_alarm_orders`），`status = 处理中`，且 `response_time`
（响应时刻）落在统计窗口内。**「未关单」≠「告警未解除」**：告警是否恢复只看返回的
`alarm_status`（触发 / 刷新 / 恢复），不作为筛选条件。

## 一、窗口

- 默认**当前班次**：白班 08:30–17:30、晚班 17:30–次日 08:30（00:00–08:30 仍算前一晚班）。
  先调 `get_current_time`，再按这个规则算起止，**不要凭印象推算日期**。
- 用户明确给了区间才用别的窗口（「昨天白班」「10-20 到 10-22」）。
- 用户要「全部未关单快照 / 现在一共几张没关」时不加窗口，回答里写明是全量、
  跨所有班次。

## 二、查询

窗口内：

1. 物化全部处理中工单（未关单量小，不加时间过滤）：

```
analysis_load('dcim_meta.query_alarm_orders', {
  'status': '1',        # 处理中，必传；不传会连已处理的一起返回
  'park': '廊坊',        # 可选；不给 = 全平台
})
```

2. 一次 `analysis_exec` 按**响应时间**过滤窗口（返回的 `response_time` 已是北京时间
   `YYYY-MM-DD HH:MM:SS` 字符串，直接字符串比较）：

```python
start, end = '2026-10-27 08:30:00', '2026-10-27 17:30:00'   # 本班，左闭右开
if 'response_time' in df.columns:
    ts = df['response_time'].fillna('')
    result = df[(ts >= start) & (ts < end)].copy()
else:
    result = df.iloc[0:0].copy()
result = result[['cluster', 'space_name', 'device_name', 'point_name', 'alarm_level',
                 'alarm_time', 'response_time', 'responder', 'process', 'order_no']]
result.to_csv('view.csv', index=False)
```

⚠️ **不要把窗口传给工具的 `start_ms`/`end_ms`** —— 那两个参数筛的是**告警发生时间**，
不是响应时间，会算错（昨夜告警、今晨响应的单会漏掉）。

全量快照（不加窗口）：直接调 `dcim_meta__query_alarm_orders({'status': '1'[, 'park']})`，
条数引用返回的 `summary.total`（`records` 只是当页）；超过 20 条再走 `analysis_load`
+ 一次 `analysis_exec` 整理视图。

## 三、交付

- **0 行就是没有**，直接说「本班没有未关单」，不要换参数反复重拉。
- ≤20 行：Markdown 表格（工单号 / 园区 / 空间 `space_name` / 设备 / 测点 / 级别 /
  告警时间 / 响应时间 / 响应人）。
- >20 行：原样输出本次 `analysis_exec` 返回的 `<file url=.../records>`，再补一句条数。
- 只有用户明确要 Excel / 导出时才产文件。

## 四、口径

- 「未关单」= 工单仍是**处理中**，不代表告警没恢复（两者可能矛盾）。
- `response_time` 为空 = 有工单但没记录响应时刻（脏数据 / 手工建单），**不计入窗口**，
  单独提示有几条即可。
- 没有工单的告警不在本技能（那是「还没人响应」，见 `skill__alarm_response_timeout`）；
  工单已关但告警仍未解除是「提前关单」，见 `skill__alarm_premature_close`。
- 查不到工单 ≠ 没有告警：工单是人工填写的处置记录。

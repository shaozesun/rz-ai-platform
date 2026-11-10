---
name: alarm_repeat
title: 重复告警
description: 重复告警：同一天同一测点同一告警内容出现 3 次以上。用户问「重复告警 / 哪些测点反复报警 / 同一告警一天好几次」时按本技能直接查。
category: alarm
domain: alarm
---

# 重复告警

口径：**同一天 + 同一测点 + 同一告警内容**为一组，**出现 ≥3 次**计入；
**只统计重要/紧急**（`level='重要'` 是「该档及以上」，紧急自动包含）。

## 一、取数

1. 物化全量：

```
analysis_load('dcim_meta.query_alarms', {
  'level': '重要',          # 重要及以上（含紧急）
  'status': 'all',          # 必须传，否则默认只看在跑的
  'park': '廊坊',           # 可选；不给 = 全平台
  'start_ms': '2026-10-21', # 裸日期，= 含当天整天
  'end_ms': '2026-10-21',
})
```

2. **返回 `error`（数据量超过单次分析上限）时**：数据量过大，缩小时间窗或加园区 /
   级别等筛选条件后重拉。工具会拒绝物化，**不存在「拿部分数据先算」这个选项**。

3. 一次 `analysis_exec` 算完（不要每个维度各跑一次）：

```python
df['日期'] = df['alarm_time'].str[:10]          # 已是北京时间字符串
df['测点键'] = df['point_guid'].fillna(df['point_name'])
g = (df.groupby(['日期', '测点键', 'content'], dropna=False)
       .agg(次数=('event_log_id', 'count'),
            园区=('cluster', 'first'),
            空间=('space_name', 'first'),        # 全名：园区+数据中心+模组+楼层+房间
            设备=('device_name', 'first'),
            测点=('point_name', 'first'),
            级别=('alarm_level', lambda s: '、'.join(sorted(set(s)))),
            首次=('alarm_time', 'min'),
            末次=('alarm_time', 'max'))
       .reset_index())
hot = g[g['次数'] >= 3].sort_values(['日期', '次数'], ascending=[True, False])
result = hot.drop(columns=['测点键'])          # 表格给明细
```

4. 可另算「日期 × 重复组数 / 总次数」看趋势（折线或柱状），Top 测点用横向条形图。
   出图前调 `skill__chart` 取字段规范；**任何图上都不加文字/数字标签**。

## 二、交付

- **0 组就是没有**，直接说「没有重复告警」，不要换参数反复重拉。
  `analysis_load` 对空结果返回「该查询未返回数据」——这就是 0 条。
- ≤20 组：Markdown 表格；>20 组：整理成可读视图后输出 `<file url=.../records>`。
  只有用户明确要 Excel / 导出时才产文件。

## 三、口径

- 一行 = **一次告警事件**（`event_log_id`）；同一事件的「刷新」只更新状态、不新增行，
  不会重复计数。
- 同一测点的**不同告警内容**分开算（如温度过高与通信中断是两类）。
- `analysis_load` 返回的 `alarm_level` 已是中文、`alarm_time` 已是北京时间字符串，
  **不要再做换算或查码表**。
- 地点用 `space_name`（空间全名，如「廊坊园区A1数据中心M3模组二层201机房」），
  **不要用 `space_guid` 当地点展示**。

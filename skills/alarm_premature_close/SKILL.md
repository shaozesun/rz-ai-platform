---
name: alarm_premature_close
title: 告警提前关单
description: 告警提前关单：工单已关但告警仍未解除。核对关联工单已处理（关闭）、而告警事件仍处于未恢复状态的事件。用户问「告警没解除就关单了 / 哪些单关了告警还在 / 提前关单」时按本技能直接查，不要反问口径。
when_to_use: 用户问「告警没解除怎么就关单了 / 哪些单关了告警还在 / 提前关单」时使用；例：「查一下昨天提前关单的告警。」
category: alarm
domain: alarm
---

# 告警提前关单

口径：**关联的处置工单已经是「已处理」（关单），但告警事件当前仍未解除（状态 ≠ 恢复）**。
这是处置闭环的漏项：单关掉了、告警还在跑。

## 一、为什么不能直接调工具

`query_alarm_orders` 的过滤参数**没有**「告警是否解除」这一维 —— `status` 只筛工单状态，
返回每行里的 `alarm_status` 才是关联告警事件的**当前状态**（触发 / 刷新 / 恢复）。
直接调工具只回 20 条当页、`total` 又是全部已关单数，数不出矛盾单有几条，
所以必须：**先取「已处理」工单全量 → 再按 `alarm_status` 过滤**。

## 二、查询（两步）

1. 物化「已关单」全量：

```
analysis_load('dcim_meta.query_alarm_orders', {
  'status': '2',        # 已处理 = 已关单
  'park': '廊坊',        # 可选；不给 = 全平台
})
```

- 返回 `error`（数据量超过单次分析上限）时，缩小范围后重拉：加 `park`，
  或加 `start_ms`/`end_ms`（**按告警发生时间**，裸日期，`end_ms` 含当天整天）。
  **不存在「拿部分数据先算」这个选项**。

2. 一次 `analysis_exec` 过滤 + 整理：

```python
hot = df[df['alarm_status'].isin(['触发', '刷新'])].copy()
result = hot[['cluster', 'space_name', 'device_name', 'point_name', 'alarm_level',
              'alarm_time', 'end_time', 'handler', 'process_result', 'order_no']]
result.to_csv('view.csv', index=False)
```

- `alarm_status` 为空＝库内查不到对应告警行，**不计入**（无从证明「未解除」）。
- `end_time` 就是关单（处理结束）时间，**不要自己拿时间相减或换算**。
- 不加时间窗就是「全部矛盾单」；陈旧未恢复的告警会带来噪音，回答里说明是否加了窗口。

## 三、交付

- **0 行就是没有**，直接说「当前没有提前关单」，不要换参数反复重拉。
- ≤20 行：用 Markdown 表格（园区 / 空间 `space_name` / 设备 / 测点 / 级别 /
  告警时间 / 关单时间 / 处理人 / 处理结果），**不要输出 `<file>` 标签**。
- >20 行：原样输出第 2 步返回的 `<file url=.../records>`（交互表格，可搜索/排序/翻页），
  再补一两句结论（条数、园区分布）。
- 只有用户明确要 Excel / 导出时才产文件。

## 四、口径

- 「已关单」＝工单 `status=已处理`，**不是**告警的 `process_result`
  （那只是处置结果码：确认告警 / 已恢复正常 —— 关单的单也可能写着「确认告警」）。
- 「未解除」只看告警 `status`（≠恢复），**不看解除值是否为 0**（未恢复时对方给哨兵 0）。
- 反向的另一类矛盾（**告警已恢复、单还没关**）不属本技能，那归「交班未关单」：
  `query_alarm_orders({'status': '1'})` 后看 `alarm_status` 是否为「恢复」，
  见 `skill__alarm_unclosed_order`。

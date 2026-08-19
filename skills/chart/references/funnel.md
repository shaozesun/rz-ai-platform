---
name: 生成漏斗图
description: 过程各阶段的转化/衰减，如工单流转、告警处理链路。data 每行含 `category` 与 `value`，须按流程先后顺序排列。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# funnel — 漏斗图

## 功能概述
展示多阶段转化或流失情况，常用于销售管道、用户旅程等逐步筛选过程。

## 输入字段
### 必填
- `data`: array<object>，需按流程顺序排列，每条包含 `category`（string）与 `value`（number）。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义各阶段颜色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
阶段顺序需按实际流程排列；若数值为百分比应统一基准并在标题或备注中说明口径；避免阶段过多导致阅读困难（建议 ≤6）。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
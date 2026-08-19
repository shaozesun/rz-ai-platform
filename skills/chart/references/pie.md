---
name: 生成饼图
description: 整体与部分的占比构成，可做环图（donut），适合市场份额、预算构成。data 每行含 `category` 与 `value`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# pie — 饼/环图

## 功能概述
展示整体与部分的占比，可通过内径形成环图，适用于市场份额、预算构成、用户群划分等。

## 输入字段
### 必填
- `data`: array<object>，每条记录包含 `category`（string）与 `value`（number）。

### 可选
- `innerRadius`: number，范围 [0, 1]，默认 `0`，设为 `0.6` 等值可生成环图。
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义配色列表。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
类别数量建议 ≤6，若更多可聚合为“其它”；确保数值单位统一（百分比或绝对值），必要时在标题中说明基数。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
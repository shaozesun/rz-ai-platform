---
name: 生成矩形树图
description: 层级结构与占比，适合目录/资源占用分层。data 每行含 `name` 与 `value`，可嵌套 `children` 递归。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# treemap — 矩形树图

## 功能概述
以嵌套矩形展示层级结构及各节点权重，适合资产占比、市场份额、目录容量等。

## 输入字段
### 必填
- `data`: array<object>，节点数组，每条含 `name`（string）与 `value`（number），可递归嵌套 `children`。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义配色列表。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
确保每个节点 `value` ≥0，并与子节点之和一致；树层级不宜过深，可按需要提前聚合；为提升可读性可在节点名中加上数值单位。 

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
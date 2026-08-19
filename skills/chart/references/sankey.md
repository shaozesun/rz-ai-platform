---
name: 生成桑基图
description: 流量与流向、转化路径，适合跨节点流转分析。data 每行含 `source`、`target` 与 `value`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# sankey — 桑基图

## 功能概述
展示资源、能量或用户流在不同节点之间的流向与数量，适合预算分配、流量路径、能耗分布等。

## 输入字段
### 必填
- `data`: array<object>，每条记录包含 `source`（string）、`target`（string）与 `value`（number）。

### 可选
- `nodeAlign`: string，默认 `center`，可选 `left`/`right`/`justify`/`center`。
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义节点配色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
节点名称保持唯一，避免过多交叉；如存在环路需先打平为阶段流向；可按阈值过滤小流量以聚焦重点。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
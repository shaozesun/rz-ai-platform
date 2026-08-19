---
name: 生成流程图
description: 流程/步骤/决策分支，适合处置流程、审批链路。data 为 `{nodes:[{name}], edges:[{source,target}]}`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# flow_diagram — 流程图

## 功能概述
以节点和连线展示业务流程、审批链或算法步骤，支持开始/判断/操作等多种节点类型。

## 输入字段
### 必填
- `data`: object，必填，包含节点与连线定义。
- `data.nodes`: array<object>，至少 1 条，节点需提供唯一 `name`。
- `data.edges`: array<object>，至少 1 条，包含 `source` 与 `target`（string），可选 `name` 作为连线文本。

### 可选
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。

## 使用建议
先罗列节点 `name` 并保持唯一，再建立连线；若需要描述条件，可在 `edges.name` 中填写；流程应保持单向或明确分支避免交叉。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
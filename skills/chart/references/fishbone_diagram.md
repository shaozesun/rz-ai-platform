---
name: 生成鱼骨图
description: 问题根因因果分析，适合故障原因梳理。data 为对象 `{name, children[]}` 递归（或 `{nodes, edges}`）。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# fishbone_diagram — 鱼骨图

## 功能概述
用于根因分析，将中心问题放在主干，左右分支展示不同类别的原因及其细化节点，常见于质量管理、流程优化。

## 输入字段
### 必填
- `data`: object，必填，至少提供根节点 `name`，可通过 `children`（array<object>）递归拓展，最大建议 3 层。

### 可选
- `style.texture`: string，默认 `default`，可选 `default`/`rough` 以切换线条风格。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。

## 使用建议
主干节点描述问题陈述；一级分支命名原因类别（人、机、料、法等）；叶子节点写具体现象，保持短语式表达。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
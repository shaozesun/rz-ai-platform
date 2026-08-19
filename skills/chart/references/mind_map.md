---
name: 生成思维导图
description: 主题发散/头脑风暴，适合梳理要点。data 为对象 `{name, children[]}` 递归，建议深度 ≤3。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# mind_map — 思维导图

## 功能概述
围绕中心主题展开 2~3 级分支，帮助组织想法、计划或知识结构，常用于头脑风暴、方案规划。

## 输入字段
### 必填
- `data`: object，必填，节点至少含 `name`，可通过 `children`（array<object>）递归扩展，建议深度 ≤3。

### 可选
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。

## 使用建议
中心节点写主题，一级分支代表主要维度（目标、资源、风险等），叶子节点使用短语；如分支较多，可先分拆多张导图。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
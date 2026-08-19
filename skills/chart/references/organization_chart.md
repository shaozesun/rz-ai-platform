---
name: 生成组织架构图
description: 组织/团队层级结构。data 为对象 `{name, description?, children[]}` 递归，建议深度 ≤3。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# organization_chart — 组织架构图

## 功能概述
展示公司、团队或项目的层级关系，并可在节点上描述角色职责。

## 输入字段
### 必填
- `data`: object，必填，节点至少含 `name`（string），可选 `description`（string），子节点通过 `children`（array<object>）嵌套，最大深度建议为 3。

### 可选
- `orient`: string，默认 `vertical`，可选 `horizontal`/`vertical`。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。

## 使用建议
节点名称使用岗位/角色，`description` 简要说明职责或人数；若组织较大可拆分多个子图或按部门分批展示。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
---
name: 生成雷达图
description: 多维指标对比，适合能力/属性画像。data 每行含 `name` 与 `value`，多系列时加 `group`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# radar — 雷达图

## 功能概述
在多维坐标系上比较单个对象或多对象的能力维度，常用于评测、产品对比、绩效画像。

## 输入字段
### 必填
- `data`: array<object>，每条记录包含 `name`（string）与 `value`（number），可选 `group`（string）。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.lineWidth`: number，设置雷达线宽。
- `style.palette`: string[]，定义系列颜色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
维度数量控制在 4~8 之间；不同对象通过 `group` 区分并保证同一维度都给出数值；如量纲不同需先归一化。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
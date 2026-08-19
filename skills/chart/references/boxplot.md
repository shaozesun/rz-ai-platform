---
name: 生成箱线图
description: 统计分布/离群值，适合多组数据的中位数与四分位对比。data 每行含 `category` 与 `value`，多组比较时加 `group`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# boxplot — 箱型图

## 功能概述
展示各类别数据的分布范围（最值、四分位、异常值），用于质量监控、实验结果或群体分布比较。

## 输入字段
### 必填
- `data`: array<object>，每条记录包含 `category`（string）与 `value`（number），可选 `group`（string）用于多组比较。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义配色列表。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。
- `axisXTitle`: string，默认空字符串。
- `axisYTitle`: string，默认空字符串。

## 使用建议
单个类别至少提供 5 个样本以保证统计意义；如需展示多批次，可通过 `group` 或拆分多次调用。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
---
name: 生成韦恩图
description: 集合重叠关系，适合交集分析。data 每行含 `sets`（string[]）与 `value`，可选 `label`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# venn — 维恩图

## 功能概述
展示多个集合之间的交集、并集与差异，适用于市场细分、特性覆盖、用户重叠分析。

## 输入字段
### 必填
- `data`: array<object>，每条记录包含 `value`（number）与 `sets`（string[]），可选 `label`（string）。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义配色列表。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
集合数量建议 ≤4；若缺少精确权重可根据大致占比填写；集合命名保持简洁明确（如“移动端用户”）。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
---
name: 生成散点图
description: 两变量相关性/分布，适合温湿度关联等。data 每行含 `x` 与 `y`（number），分组时加 `group`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# scatter — 散点图

## 功能概述
展示两个连续变量之间的关系，可通过颜色/形状区分不同分组，适合相关性分析、聚类探索。

## 输入字段
### 必填
- `data`: array<object>，每条记录包含 `x`（number）与 `y`（number），可选 `group`（string）。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，指定系列配色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。
- `axisXTitle`: string，默认空字符串。
- `axisYTitle`: string，默认空字符串。

## 使用建议
在上传前可对不同量纲进行标准化；若数据量很大可先抽样；使用 `group` 区分不同类别或聚类结果以便阅读。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
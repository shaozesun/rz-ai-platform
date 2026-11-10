---
name: 生成矩形树图
description: 层级结构与占比，适合目录/资源占用分层。data 为**单个根节点对象**（不是数组），根下用 `children` 递归，叶子节点含 `name` 与 `value`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# treemap — 矩形树图

## 功能概述
以嵌套矩形展示层级结构及各节点权重，适合资产占比、市场份额、目录容量等。

## 输入字段
### 必填
- `data`: **单个根节点对象**（object，不是数组），如
  `{"name":"合计","children":[{"name":"制冷","value":1772,"children":[{"name":"三级","value":700}]}]}`。
  只有**叶子节点**需要带 `value`（number）；父节点不用给 value（给了也会被忽略，按叶子求和）。
  **顶层不要直接给节点数组**——必须包一层根节点（前端只把顶层当单根树处理）。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义配色列表。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
叶子节点 `value` ≥0；父节点的面积由叶子自动求和，不用自己算；树层级不宜过深，可按需要提前聚合。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
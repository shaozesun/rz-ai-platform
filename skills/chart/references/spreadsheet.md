---
name: 生成电子表格
description: 结构化表格/数据透视表，适合明细数据展示。data 为数组，每个对象一行；可选 `columns` 控制列序。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# spreadsheet — 电子表格/数据透视表

## 功能概述
生成电子表格或数据透视表，用于展示结构化的表格数据。当提供 `rows` 或 `values` 字段时，渲染为数据透视表（交叉表）；否则渲染为常规表格。适合展示结构化数据、跨类别比较值以及创建数据汇总。

## 输入字段
### 必填
- `data`: array<object>，表格数据数组，每个对象代表一行。键是列名，值可以是字符串、数字、null 或 undefined。例如：`[{ name: 'John', age: 30 }, { name: 'Jane', age: 25 }]`。

### 可选
- `rows`: array<string>，数据透视表的行标题字段。当提供 `rows` 或 `values` 时，电子表格将渲染为数据透视表。
- `columns`: array<string>，列标题字段，用于指定列的顺序。对于常规表格，这决定列的顺序；对于数据透视表，用于列分组。
- `values`: array<string>，数据透视表的值字段。当提供 `rows` 或 `values` 时，电子表格将渲染为数据透视表。
- `theme`: string，默认 `default`，可选 `default`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。

## 使用建议
- 对于常规表格，只需提供 `data` 和可选的 `columns` 来控制列的顺序。
- 对于数据透视表（交叉表），提供 `rows` 用于行分组，`columns` 用于列分组，`values` 用于聚合的值字段。
- 确保数据中的字段名与 `rows`、`columns`、`values` 中指定的字段名一致。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
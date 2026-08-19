---
name: 生成直方图
description: 连续数值的频数分布，适合时长、容量等数值分布分析。data 每行含一个数值字段，`xField` 指定该字段名。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# histogram — 直方图

## 功能概述
通过分箱显示连续数值的频数或概率分布，便于识别偏态、离群与集中区间。

## 输入字段
### 必填
- `data`: array<object>，每行含一个数值字段，用于构建频数分布。
- 标签属性 `xField` 指定该数值字段名（如 `xField="value"`）。

### 可选
- `binNumber`: number，自定义分箱数量，未设置则自动估算。
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义柱体颜色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。
- `axisXTitle`: string，默认空字符串。
- `axisYTitle`: string，默认空字符串。

## 使用建议
清理空值/异常后再传入；样本量建议 ≥30；根据业务意义调整 `binNumber` 以兼顾细节与整体趋势。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
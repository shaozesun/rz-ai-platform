---
name: 生成双轴图
description: 两个不同量纲指标同画布对比，如温度 vs 湿度、营收 vs 利润。data 每行含 X 轴 刻度字段与两个指标字段；`yField` 用英文逗号分隔两个指标字段，前者柱状、后者折线。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# dual_axes — 双轴图

## 功能概述
在同一画布上叠加柱状与折线（或两条不同量纲曲线），用于同时展示趋势与对比，如营收 vs 利润、温度 vs 降雨。

## 输入字段
### 必填
- `data`: array<object>，每行含 X 轴刻度字段（如 `year`）与两个不同量纲的指标字段（如 `revenue`、`profit`）。
- 标签属性 `xField` 指定 X 轴刻度字段；`yField` 用英文逗号分隔两个指标字段（如 `yField="revenue,profit"`），前者渲染为柱状、后者为折线。

### 可选
- `style.backgroundColor`: string，自定义背景色。
- `style.palette`: string[]，配置多系列配色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。
- `axisXTitle`: string，默认空字符串。

## 使用建议
仅在确有不同量纲或图例对比需求时使用；保持系列数量 ≤2 以免阅读复杂；若两曲线差值巨大可使用次坐标轴进行缩放。 

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
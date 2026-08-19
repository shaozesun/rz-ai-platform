---
name: 生成面积图
description: 累计趋势/量随时间的累积变化，适合流量、库存累积等。data 每行含 `time` 与 `value`，堆叠多系列时加 `group`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# area — 面积图

## 功能概述
展示连续自变量（常为时间）下的数值趋势，可启用堆叠观察不同分组的累计贡献，适合 KPI、能源、产出等时间序列场景。

## 输入字段
### 必填
- `data`: 数组，元素包含 `time`（string）与 `value`（number），堆叠时需补充 `group`（string），至少 1 条记录。

### 可选
- `stack`: boolean，默认 `false`，开启堆叠需确保每条数据都含 `group` 字段。
- `style.backgroundColor`: string，设置图表背景色（如 `#fff`）。
- `style.lineWidth`: number，自定义面积边界的线宽。
- `style.palette`: string[]，传入调色板数组用于系列着色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough` 以控制手绘质感。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`，控制图表宽度。
- `height`: number，默认 `400`，控制图表高度。
- `title`: string，默认空字符串，用于设置图表标题。
- `axisXTitle`: string，默认空字符串，用于设置 X 轴标题。
- `axisYTitle`: string，默认空字符串，用于设置 Y 轴标题。

## 使用建议
保证 `time` 字段格式统一（如 `YYYY-MM`）；堆叠模式下各组数据需覆盖相同的时间点，可先做缺失补值。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
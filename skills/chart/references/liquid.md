---
name: 生成水波图
description: 单一百分比/进度，如达成率、容量占用率。data 每行含 `percent`（0~1 数值），前端取首行渲染。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# liquid — 水波图

## 功能概述
以液面高度展示单一百分比或进度，视觉动效强，适合达成率、资源占用等指标。

## 输入字段
### 必填
- `data`: array<object>，每行含 `percent`（number，取值范围 [0,1]，表示当前百分比或进度）；前端取首行 `percent` 渲染。

### 可选
- `shape`: string，默认 `circle`，可选 `circle`/`rect`/`pin`/`triangle`。
- `style.backgroundColor`: string，自定义背景色。
- `style.color`: string，自定义水波颜色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
确保百分比经过归一化；单图仅支持一个进度，如需多指标请并排生成多个水波图；标题可写“目标完成率 85%”。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
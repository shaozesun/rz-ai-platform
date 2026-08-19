---
name: 生成词云图
description: 文本词频可视化，适合告警关键词、日志热点统计。data 每行含 `text` 与 `value`。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# word_cloud — 词云图

## 功能概述
根据词频或权重调节文字大小与位置，用于快速提炼文本主题、情绪或关键词热点。

## 输入字段
### 必填
- `data`: array<object>，每条记录包含 `text`（string）与 `value`（number）。

### 可选
- `style.backgroundColor`: string，设置背景色。
- `style.palette`: string[]，定义词云配色。
- `style.texture`: string，默认 `default`，可选 `default`/`rough`。
- `theme`: string，默认 `default`，可选 `default`/`academy`/`dark`。
- `width`: number，默认 `600`。
- `height`: number，默认 `400`。
- `title`: string，默认空字符串。

## 使用建议
生成前去除停用词并合并同义词；统一大小写避免重复；如需突出情绪可按正负值映射配色。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
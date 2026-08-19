---
name: 生成点标地图
description: 中国境内 POI 点位分布。data 为 POI 名称数组。前端暂不支持地图渲染，会降级为表格列出数据。调用本工具返回完整数据字段规范（必填/可选），按规范组装
  `<chart>` 标签输出。
---
# pin_map — 点标地图（中国）

## 功能概述
在中国地图上以标记展示多个 POI 位置，可配合弹窗显示图片或说明，适用于门店分布、资产布点等。

## 输入字段
### 必填
- `title`: string，必填且≤16 字，概述点位集合。
- `data`: string[]，必填，包含中国境内的 POI 名称列表。

### 可选
- `markerPopup.type`: string，固定为 `image`。
- `markerPopup.width`: number，默认 `40`，图片宽度。
- `markerPopup.height`: number，默认 `40`，图片高度。
- `markerPopup.borderRadius`: number，默认 `8`，图片圆角。
- `width`: number，默认 `1600`。
- `height`: number，默认 `1000`。

## 使用建议
POI 名称需包含足够的地理限定（城市+地标）；根据业务可在名称中附带属性，如“上海徐汇门店 A”；地图依赖高德数据，仅支持中国。

## 渲染说明
- 前端暂不支持地图渲染，会降级为表格列出数据。

## 返回结果
- 按 `<chart>` 标签输出，数据由前端本地渲染（不出内网）；data 字段约定见上文「必填/可选」，type 取本文件标题对应值。
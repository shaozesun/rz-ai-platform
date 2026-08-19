---
name: chart
title: 图表
description: 数据可视化图表技能。根据数据特征选择合适的图表类型，输出 <chart> 标签（前端本地渲染，不出内网）。覆盖趋势/对比/占比/分布/流向/层级/统计分布/进度等图表。调用本技能返回选型引导与各图表字段规范（必填/可选），按规范组装 <chart> 标签输出。
category: chart
domain: chart
---

# 图表可视化

把结构化数据可视化为图表，输出 `<chart>` 标签，数据由前端本地渲染（不出内网）。
表格给明细、图表给趋势/对比，两者可并存。

## 选型引导

按数据特征选择图表类型：

- **时间趋势**：line 折线图 / area 面积图（累计）/ dual_axes 双轴图（两个不同量纲）
- **分类对比**：column 柱状图 / bar 条形图（分类名较长或多分类时用横向）
- **占比构成**：pie 饼图（可 donut 环图）/ treemap 矩形树图（层级占比）
- **数值分布**：histogram 直方图 / boxplot 箱线图 / violin 小提琴图
- **多维对比**：radar 雷达图
- **相关性**：scatter 散点图
- **过程转化**：funnel 漏斗图 / flow_diagram 流程图 / sankey 桑基图
- **集合重叠**：venn 韦恩图
- **文本词频**：word_cloud 词云图
- **进度百分比**：liquid 水波图
- **层级结构**：mind_map 思维导图 / organization_chart 组织架构图 / network_graph 网络图 / fishbone_diagram 鱼骨图（问题根因）
- **明细表格**：spreadsheet 电子表格
- **地图**（前端暂不支持渲染，降级表格）：district_map 行政区 / pin_map 点标 / path_map 路径

## 使用流程

1. 先用 tool_search 按意图检索图表子能力（如「画个趋势图」→ `skill.chart.line`）。
2. 调用对应子能力 `skill.chart.<type>` 获取该图完整数据字段规范（必填/可选）。
3. 按规范组装 `<chart type="<type>" xField="..." yField="...">JSON 数据</chart>` 输出。

## 约束

- data 必须忠于工具返回结果，禁止编造或改写数值
- 数据点少于 3 个时不强行出图
- 一次回答最多输出一个 `<chart>` 块，紧跟相关分析段落
- 数据属性支持 type/xField/yField/groupField/donut/stacked/title 等；样式属性可选 theme/height/width/palette/lineWidth/backgroundColor（见下方「样式属性」）；其余属性不生效，忽略
- 常用图（line/column/pie 等）可直接输出；结构图与特殊图（sankey/venn/liquid 等）不确定 data 字段时先检索子能力拿规范再组装

## 样式属性

样式一律用**平铺属性**写在 `<chart>` 标签上（本地渲染，不出内网）：

| 属性 | 说明 | 取值示例 |
|------|------|----------|
| `theme` | 主题。思维导图/组织架构/鱼骨/流向/网络等结构图仅 `light`/`dark`；其余图 `classic`/`classicDark`/`light`/`dark`/`academy` | `theme="academy"` |
| `height` | 图高（px），默认 320 | `height="400"` |
| `width` | 图宽（px），默认自适应容器 | `width="600"` |
| `palette` | 系列配色，逗号分隔颜色串（仅普通图） | `palette="#1a3a5c,#6f86b8"` |
| `lineWidth` | 折线/面积图线宽 | `lineWidth="2"` |
| `backgroundColor` | 图表卡片背景色 | `backgroundColor="#f5f7fa"` |

参考文档里的 `style.*` 字段（如 `style.lineWidth`/`style.palette`/`style.texture`）本地不直接支持，改为对应平铺属性：`style.lineWidth`→`lineWidth="2"`、`style.palette`→`palette="..."`、`style.backgroundColor`→`backgroundColor="..."`；`texture`/`axisXTitle`/`axisYTitle` 等不支持，忽略。

思维导图（mind_map）额外支持 `type`（默认 `boxed` 卡片分支配色；可传 `linear` 换下划线样式）与 `direction`（`left`/`right`/`alternate` 分支方向）。

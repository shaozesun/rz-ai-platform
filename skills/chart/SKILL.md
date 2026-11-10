---
name: chart
title: 图表
description: 数据可视化图表技能。根据数据特征选择合适的图表类型，输出 <chart> 标签（前端本地渲染，不出内网）。覆盖趋势/对比/占比/分布/流向/层级/统计分布/进度等图表。调用本技能返回选型引导与各图表字段规范（必填/可选），按规范组装 <chart> 标签输出。数据怎么聚合（统计/分布/占比/趋势/交叉的算法与 pandas 写法）见 skill.analysis。
category: chart
domain: chart
---

# 图表可视化

把结构化数据可视化为图表，输出 `<chart>` 标签，数据由前端本地渲染（不出内网）。
表格给明细、图表给趋势/对比，两者可并存。

本技能只管**怎么画**（选哪种图型、字段怎么摆）；**怎么算**（聚合形状、pandas 写法、交互表格
整理）见 `skill.analysis`。

## 选型引导

**先定图型，再按图型备数据**——数据按什么形状算出来，决定了能出什么图（聚合见 `skill.analysis`）。
选定图型后按该图型需要的字段形状准备数据（如要画耗时分布先在沙箱 `pd.cut` 分箱，要画「专业 × 级别」
先 `groupby(['专业','级别']).size()` 得每组一行三列）。

按数据特征选择图表类型：

- **时间趋势**：line 折线图 / area 面积图（累计）/ dual_axes 双轴图（两个不同量纲）
- **分类对比**：column 柱状图 / bar 条形图（分类名较长或多分类时用横向）
- **占比构成**：pie 饼图（**默认实心，优先用饼图而不是环形图**；只有用户点名要「环图/环形图」时才加 `donut="true"`）；treemap 矩形树图**仅用于真正的层级/嵌套数据**（组织架构、目录容量、分类下还有子分类）
- **两个分类维度**：堆叠柱状图（column + `groupField` + `stacked="true"`）或分组柱状图；**交叉表不是层级，不要用 treemap**
- **数值分布**：histogram 直方图 / boxplot 箱线图 / violin 小提琴图
- **多维对比**：radar 雷达图
- **相关性**：scatter 散点图
- **过程转化**：funnel 漏斗图 / flow_diagram 流程图 / sankey 桑基图
- **集合重叠**：venn 韦恩图
- **文本词频**：word_cloud 词云图
- **进度百分比**：liquid 水波图
- **层级结构**：mind_map 思维导图 / organization_chart 组织架构图 / network_graph 网络图 / fishbone_diagram 鱼骨图（问题根因）
- **明细表格**：spreadsheet 电子表格
- **地图**：pin_map 点描地图（中国底图 + 经纬度打点，点大小随数值；园区坐标表见 `references/pin_map.md`）；district_map / path_map 暂不支持渲染，降级表格

## 使用流程

1. 先用 tool_search 按意图检索图表子能力（如「画个趋势图」→ `skill.chart.line`）。
2. 调用对应子能力 `skill.chart.<type>` 获取该图完整数据字段规范（必填/可选）。
3. 按规范组装 `<chart type="<type>" group="<result 的键名>" xField="..." yField="..."/>` 输出 ——
   **只写属性，不要写数据**：数值由平台按 `group` 从 `analysis_exec` 的 `chart_data` 里注入（逐字节准确）。
   ⚠️ 正文里写 `data="[...]"` 会被引号截断、图直接崩。

## 约束

- 数值必须忠于工具返回结果，禁止编造或改写 —— 且**不要在正文里重写数据**，只写 `group` 引用
- 数据点少于 3 个时不强行出图
- 简单/单维度数据出 1 张图，紧跟相关分析段落；复杂多维度数据每个维度一张（选取 2-3 个最有信息量的维度，不要堆砌）；多张图的数据**各自来自一次 `analysis_exec`**，不要把同一份数据硬改成多张图
- 数据属性支持 type/xField/yField/groupField/donut/stacked/title 等；样式属性可选 theme/height/width/palette/lineWidth/backgroundColor（见下方「样式属性」）；其余属性不生效，忽略
- 常用图（line/column/pie 等）可直接输出；结构图与特殊图（sankey/venn/liquid 等）不确定 data 字段时先检索子能力拿规范再组装

## 结构图字段规范

结构图（mind_map / organization_chart / fishbone_diagram / flow_diagram / network_graph）的 data 是 JSON **对象**（不是数组）。以思维导图为例，`type="mind_map"`，data 为 `{"name":...,"children":[...]}` 递归对象：`name` 为节点标题，`children` 为子节点数组（建议深度 ≤3）：

```html
<chart type="mind_map" title="数据中心运维管理体系">
{"name":"数据中心运维管理体系","children":[{"name":"规划管理","children":[{"name":"××制度"}]},{"name":"建设管理"}]}
</chart>
```

- 默认按分支着色（`boxed` 卡片样式，每个一级分支一色）；可传 `direction`（`left`/`right`/`alternate`）调整分支方向，或用 `type="linear"` 换下划线样式。
- 查询「数据中心运维管理体系」（`mgmt.query_mindmap`）时，把返回节点的 **`topic` 字段映射为 `name`**，按主要章节裁剪到 2~3 级（树很大，不要平铺全部叶子）。
- 其余结构图（organization_chart / fishbone_diagram / flow_diagram / network_graph）字段形态各不相同，先调 `skill.chart.<type>` 取规范再组装。

## 多维度分析建议

时间趋势用 line/area；分组/对比用 column/bar（**两个分类维度用堆叠**）；构成占比用 pie（默认实心饼图，不做环形图）；流向/转化用 sankey/funnel；层级用 mind_map/organization_chart；根因用 fishbone_diagram。维度选 2-3 个最有信息量的，不要罗列过多，**也不要清一色只用柱/饼**。

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

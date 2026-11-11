---
name: html_dashboard
title: HTML 看板
description: 生成自包含 HTML 看板/仪表盘/驾驶舱/大屏（echarts 图表，数据内联，前端右侧面板本地预览）。当用户明确要求「看板 / 驾驶舱 / 大屏 / HTML 报表 / 可视化面板 / dashboard」时调用本技能，返回模板与规范，按 analysis_write_file 流程产出 <file name=*.html> 产物。
when_to_use: 用户明确要「看板 / 驾驶舱 / 大屏 / HTML 报表 / 可视化面板 / dashboard」时使用；例：「把这份数据做成运维驾驶舱大屏。」
category: dashboard
domain: dashboard
---

# HTML 看板（自包含文件）

生成一个**自包含 HTML 看板文件**（数据内联、echarts 图表），前端右侧 artifact 面板本地渲染成「迷你看板」。echarts 由前端打包注入，数据不出内网。

## 使用流程

1. 物化数据：`analysis_load(capability_id, params)` → 拿到 `dataset_id`（全量数据落沙箱，原始记录不进上下文）
2. （可选）若用户提供了参考图片（先用 `list_uploaded_files` 确认有图片），调 `analyze_uploaded_image` 提取其配色/布局/图表风格，按该风格填充模板
3. 调用 `skill.html_dashboard.dashboard_template` 获取完整模板与字段规范
4. `analysis_write_file(dataset_id, 'dashboard.html', <模板填充后的自包含 HTML>)` 写入
5. `analysis_exec(dataset_id, file='dashboard.html')` 执行 → 返回 `artifacts`（每条含 url 与 name）
6. 对每个 artifact 原样输出一行 `<file url="..." name="..."/>`（禁止改写 url/name），并写一句「已在右侧看板面板渲染，可下载」

## 硬性规范

- 数据全部内联进页面 `<script id="data" type="application/json">...</script>`，**禁止任何外部 src**（CDN 脚本/图片/字体都不行，内网或离线会渲染空白）
- echarts 全局由前端注入，页面脚本直接 `echarts.init(dom)`，**禁止 `<script src=外部CDN>`**
- 必须带 CSP meta：`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">`
- 大屏科技风：深色渐变底 + 发光角标/KPI + 图表栅格（grid），禁 emoji；装饰全用 CSS，不引图片/字体
- **固定视口大屏铺满 + 主次层次**：`body` 为 `height:100vh` 的 flex 纵向布局，标题/KPI 固定，内容区 `flex:1` 填满剩余高度、**不出现滚动条**；内容区用 **12 栅格**（`repeat(12,1fr)`）+ `.span-8`/`.span-4` 等跨度类，**主图只加宽、所有卡等高、每行跨度之和正好 12 不留空白**；图表容器 `.chart` 用 `flex:1` 自适应高度，**不要写固定像素高度**
- 脚本用 `var charts=[]` 收集所有 `echarts.init` 实例，末尾 `window.addEventListener('resize', ...)` 逐个 `c.resize()`，让图表随窗口缩放
- echarts **一律不配 label**（柱顶数值/饼图文字/折线数据点都不加），数字可读性靠 tooltip/图例/坐标轴/网格
- 图表/渐变配色**复用模板颜色常量**（`BLUE`/`CYAN`/`COLORS`），不要手写裸 hex 字符串——手写易漏引号导致 JS 语法错误、整段脚本不执行
- **图文结合**：`.panel` 里除 echarts 图外，用 `.rank-list`（TOP 排名榜：序号+名称+数值，前三名序号高亮）或 `.feed-list`（滚动流水：时间+文本+状态徽标）展示排名/明细/事件，大屏不该只有图
- 所有数值必须来自物化数据集，禁止编造、估算或幻觉

## 何时用

用户**明确**要「看板 / 驾驶舱 / 大屏 / HTML 报表 / 可视化面板 / dashboard」才走本流程；仅统计展示 / 趋势 / 对比仍用 `<chart>` + 表格呈现，不产看板文件。

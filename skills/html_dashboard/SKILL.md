---
name: html_dashboard
title: HTML 看板
description: 生成自包含 HTML 看板/仪表盘/驾驶舱/大屏（echarts 图表，数据内联，前端右侧面板本地预览）。当用户明确要求「看板 / 驾驶舱 / 大屏 / HTML 报表 / 可视化面板 / dashboard」时调用本技能，返回模板与规范，按 analysis_write_file 流程产出 <file name=*.html> 产物。
category: dashboard
domain: dashboard
---

# HTML 看板（自包含文件）

生成一个**自包含 HTML 看板文件**（数据内联、echarts 图表），前端右侧 artifact 面板本地渲染成「迷你看板」。echarts 由前端打包注入，数据不出内网。

## 使用流程

1. 物化数据：`analysis_load(capability_id, params)` → 拿到 `dataset_id`（全量数据落沙箱，原始记录不进上下文）
2. 调用 `skill.html_dashboard.dashboard_template` 获取完整模板与字段规范
3. `analysis_write_file(dataset_id, 'dashboard.html', <模板填充后的自包含 HTML>)` 写入
4. `analysis_exec(dataset_id, file='dashboard.html')` 执行 → 返回 `artifacts`（每条含 url 与 name）
5. 对每个 artifact 原样输出一行 `<file url="..." name="..."/>`（禁止改写 url/name），并写一句「已在右侧看板面板渲染，可下载」

## 硬性规范

- 数据全部内联进页面 `<script id="data" type="application/json">...</script>`，**禁止任何外部 src**（CDN 脚本/图片/字体都不行，内网或离线会渲染空白）
- echarts 全局由前端注入，页面脚本直接 `echarts.init(dom)`，**禁止 `<script src=外部CDN>`**
- 必须带 CSP meta：`<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">`
- 大屏科技风：深色渐变底 + 发光角标/KPI + 图表栅格（grid），禁 emoji；装饰全用 CSS，不引图片/字体
- echarts **一律不配 label**（柱顶数值/饼图文字/折线数据点都不加），数字可读性靠 tooltip/图例/坐标轴/网格
- 每个图表容器必须**显式 height**（如 260px，仪表盘 240px），否则 echarts 渲染空白
- 所有数值必须来自物化数据集，禁止编造、估算或幻觉

## 何时用

用户**明确**要「看板 / 驾驶舱 / 大屏 / HTML 报表 / 可视化面板 / dashboard」才走本流程；仅统计展示 / 趋势 / 对比仍用 `<chart>` + 表格呈现，不产看板文件。

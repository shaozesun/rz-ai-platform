---
name: dashboard_template
description: HTML 看板自包含模板（固定视口大屏，主次层次 + 图文结合）。复制下方 ```html 块内完整内容到 analysis_write_file，替换 <script id="data"> 内联 JSON 与标题，按示例 echarts 配置填充图表。
---

# dashboard 模板（自包含 HTML，固定视口大屏，主次层次 + 图文结合）

**用法**：把下面 ```html 块里的完整内容复制给 `analysis_write_file(dataset_id, 'dashboard.html', <内容>)`。只需改三处——① `h1`/`.subtitle` 标题；② `<script id="data">` 里的内联 JSON（换成物化数据，保持结构）；③ 各 `setOption` 的 series 数据引用与字段名（若改了 JSON 字段名）。KPI 数量、图表数量、列表数量按需增删。

**布局（主次层次，不定死）**：固定视口大屏（`body` 为 `height:100vh` 的 flex 纵向布局，标题/KPI 固定，内容区 `flex:1` 填满剩余高度），**不出现滚动条**。内容区是 **12 栅格**：主图用 `.span-8`/`.span-6` **只加宽、不加高**占 C 位，次要图表用 `.span-4`/`.span-3` 小块，**所有卡等高，每行跨度之和正好 12、不留空白**。每个 `.panel` 是 flex 纵向卡片（`h2` 固定 + 内容 `flex:1` 自适应），**不要给 `.chart` 写固定像素高度**。

**图文结合**：`.panel` 里除 echarts 图（`.chart`）外，可放文字列表——`.rank-list`（TOP 排名榜，序号+名称+数值）或 `.feed-list`（滚动流水，时间+文本+状态徽标）。大屏不该只有图，关键明细/排名用列表呈现。

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>设备运行看板</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; }
  body {
    font-family: -apple-system, "PingFang SC", "Microsoft YaHei", "Helvetica Neue", Arial, sans-serif;
    color: #dbe6f6;
    /* 固定视口大屏：整屏铺满、无滚动条 */
    height: 100vh;
    display: flex;
    flex-direction: column;
    padding: 20px 24px;
    overflow: hidden;
    background:
      repeating-linear-gradient(0deg, rgba(77,166,255,.045) 0 1px, transparent 1px 42px),
      repeating-linear-gradient(90deg, rgba(77,166,255,.045) 0 1px, transparent 1px 42px),
      radial-gradient(1200px 700px at 18% -8%, rgba(38,90,160,.38), transparent 60%),
      linear-gradient(180deg, #0d1b33 0%, #0a1428 55%, #071024 100%);
  }
  /* 标题栏：左侧标题 + 实时脉冲点 + 右侧渐变分隔线 */
  .title-bar { flex: none; display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
  .title-bar h1 { font-size: 20px; font-weight: 600; color: #f2f7ff; letter-spacing: .5px; }
  .title-bar::after { content: ""; flex: 1; height: 1px;
    background: linear-gradient(90deg, rgba(77,166,255,.6), rgba(77,166,255,0)); }
  .live { display: inline-flex; align-items: center; gap: 6px; color: #8a96ab; font-size: 12px; white-space: nowrap; }
  .live::before { content: ""; width: 8px; height: 8px; border-radius: 50%;
    background: #4dbf8b; box-shadow: 0 0 8px rgba(77,191,139,.8);
    animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
  .subtitle { flex: none; color: #8a96ab; font-size: 13px; margin-bottom: 14px; }
  /* KPI 指标卡：左侧彩色竖条 + 发光数字 + 涨跌色 */
  .kpis { flex: none; display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 14px; margin-bottom: 14px; }
  .kpi { position: relative; overflow: hidden; padding: 14px 18px 12px 18px;
    background: linear-gradient(180deg, rgba(22,34,56,.92), rgba(13,22,38,.94));
    border: 1px solid rgba(77,166,255,.18); border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0,0,0,.35), inset 0 0 18px rgba(77,166,255,.05); }
  .kpi::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px;
    background: linear-gradient(180deg, #4da6ff, #37e5ff); }
  .kpi .label { color: #8a96ab; font-size: 12px; }
  .kpi .value { margin-top: 4px; font-size: 28px; font-weight: 600; color: #4da6ff;
    text-shadow: 0 0 14px rgba(77,166,255,.6); font-variant-numeric: tabular-nums; }
  .kpi .delta { font-size: 12px; margin-top: 2px; }
  .delta.up { color: #4dbf8b; }
  .delta.down { color: #e05656; }
  /* 内容区：12 栅格，主次靠 span 分配；dense 自动填缝，无空洞 */
  .charts { flex: 1; min-height: 0; display: grid; gap: 14px;
    grid-template-columns: repeat(12, 1fr);
    grid-auto-rows: 1fr; grid-auto-flow: dense; }
  /* 跨度工具类：主图加宽、次要图等宽，按数据重要性分配；所有卡等高，每行凑满 12 */
  .span-3 { grid-column: span 3; }
  .span-4 { grid-column: span 4; }
  .span-6 { grid-column: span 6; }
  .span-8 { grid-column: span 8; }
  .span-12 { grid-column: span 12; }
  /* 图表卡片：flex 纵向，h2 固定 + 内容自适应；发光角标 + 悬停提亮 */
  .panel { position: relative; overflow: hidden; min-height: 0;
    display: flex; flex-direction: column; padding: 12px 16px;
    background: linear-gradient(180deg, rgba(22,34,56,.92), rgba(13,22,38,.94));
    border: 1px solid rgba(77,166,255,.16); border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0,0,0,.35), inset 0 0 18px rgba(77,166,255,.04);
    transition: border-color .2s; }
  .panel:hover { border-color: rgba(77,166,255,.4); }
  .panel::before, .panel::after { content: ""; position: absolute; width: 12px; height: 12px; pointer-events: none; }
  .panel::before { top: 0; left: 0; border-top: 2px solid #4da6ff; border-left: 2px solid #4da6ff;
    border-top-left-radius: 10px; filter: drop-shadow(0 0 6px rgba(77,166,255,.7)); }
  .panel::after { bottom: 0; right: 0; border-bottom: 2px solid #37e5ff; border-right: 2px solid #37e5ff;
    border-bottom-right-radius: 10px; filter: drop-shadow(0 0 6px rgba(55,229,255,.7)); }
  .panel h2 { flex: none; font-size: 14px; font-weight: 500; color: #c3cde0;
    margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
  .panel h2::before { content: ""; width: 3px; height: 14px; flex: none;
    background: linear-gradient(180deg, #4da6ff, #37e5ff); border-radius: 2px; }
  /* 图表容器：flex:1 自适应高度（不写固定像素），由父级 grid 撑满 */
  .chart { flex: 1; min-height: 0; width: 100%; }
  /* TOP 排名榜：序号 + 名称 + 数值，前三名序号高亮 */
  .rank-list { flex: 1; min-height: 0; overflow-y: auto; display: flex;
    flex-direction: column; gap: 8px; }
  .rank-item { display: flex; align-items: center; gap: 10px; padding: 8px 10px;
    background: rgba(77,166,255,.05); border-radius: 8px; }
  .rank-no { width: 18px; height: 18px; flex: none; border-radius: 4px;
    display: flex; align-items: center; justify-content: center;
    font-size: 11px; font-weight: 600; background: rgba(77,166,255,.15); color: #4da6ff; }
  .rank-item:nth-child(1) .rank-no { background: #e05656; color: #fff; }
  .rank-item:nth-child(2) .rank-no { background: #f5a623; color: #fff; }
  .rank-item:nth-child(3) .rank-no { background: #4dbf8b; color: #fff; }
  .rank-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; color: #dbe6f6; font-size: 13px; }
  .rank-val { flex: none; color: #4da6ff; font-size: 14px; font-weight: 600;
    font-variant-numeric: tabular-nums; }
  /* 滚动流水列表：时间 + 文本 + 状态徽标 */
  .feed-list { flex: 1; min-height: 0; overflow-y: auto; display: flex;
    flex-direction: column; gap: 8px; }
  .feed-item { display: flex; align-items: baseline; gap: 10px; padding: 6px 10px;
    border-left: 2px solid rgba(77,166,255,.4);
    background: rgba(77,166,255,.04); border-radius: 0 6px 6px 0; }
  .feed-time { flex: none; color: #8a96ab; font-size: 12px;
    font-variant-numeric: tabular-nums; }
  .feed-text { flex: 1; min-width: 0; color: #dbe6f6; font-size: 13px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .feed-badge { flex: none; font-size: 11px; padding: 1px 6px; border-radius: 4px; }
  .feed-badge.crit { background: rgba(224,86,86,.2); color: #e05656; }
  .feed-badge.warn { background: rgba(245,166,35,.2); color: #f5a623; }
  .feed-badge.info { background: rgba(77,191,139,.2); color: #4dbf8b; }
  /* 列表滚动条：深色细条 */
  .rank-list::-webkit-scrollbar, .feed-list::-webkit-scrollbar { width: 4px; }
  .rank-list::-webkit-scrollbar-thumb, .feed-list::-webkit-scrollbar-thumb {
    background: rgba(77,166,255,.35); border-radius: 2px; }
</style>
</head>
<body>
  <div class="title-bar">
    <h1>××机房设备运行看板</h1>
    <span class="live">实时</span>
  </div>
  <p class="subtitle">数据截至 ×× 年 ×× 月 ×× 日 · 来源：运维平台物化数据</p>

  <div class="kpis">
    <div class="kpi"><div class="label">设备总数</div><div class="value" id="kpi-total">-</div><div class="delta up" id="kpi-total-delta"></div></div>
    <div class="kpi"><div class="label">运行正常</div><div class="value" id="kpi-ok">-</div><div class="delta up" id="kpi-ok-delta"></div></div>
    <div class="kpi"><div class="label">告警数</div><div class="value" id="kpi-alarm">-</div><div class="delta down" id="kpi-alarm-delta"></div></div>
    <div class="kpi"><div class="label">平均负载</div><div class="value" id="kpi-load">-</div></div>
  </div>

  <div class="charts">
    <div class="panel span-8"><h2>温度趋势</h2><div class="chart" id="chart-trend"></div></div>
    <div class="panel span-4"><h2>设备类型占比</h2><div class="chart" id="chart-comp"></div></div>
    <div class="panel span-4"><h2>容量对比</h2><div class="chart" id="chart-bar"></div></div>
    <div class="panel span-4"><h2>设备健康度</h2><div class="chart" id="chart-radar"></div></div>
    <div class="panel span-4"><h2>设备告警 Top5</h2><div class="rank-list" id="rank-alarm"></div></div>
    <div class="panel span-12"><h2>实时告警流水</h2><div class="feed-list" id="feed-alarm"></div></div>
  </div>

  <!-- 数据内联在 JSON 里，禁止任何外部 src；渲染脚本读这里填充 KPI / 图表 / 列表 -->
  <script id="data" type="application/json">
  {
    "kpi": { "total": 0, "ok": 0, "alarm": 0, "load": "0%" },
    "kpi_delta": { "total": "+3.2%", "ok": "98.2%", "alarm": "-5" },
    "trend": { "x": ["01-01", "01-02"], "temp": [20, 21] },
    "comp": [ { "name": "配电", "value": 1 }, { "name": "暖通", "value": 1 } ],
    "bar": { "x": ["A", "B"], "y": [10, 20] },
    "radar": {
      "indicator": [ { "name": "温度", "max": 100 }, { "name": "湿度", "max": 100 }, { "name": "负载", "max": 100 } ],
      "series": [ { "name": "机房A", "values": [80, 70, 60] } ]
    },
    "alarm_top": [ { "name": "机房A", "value": 12 }, { "name": "机房B", "value": 8 }, { "name": "机房C", "value": 5 } ],
    "alarm_feed": [
      { "time": "10:32", "text": "机房A 温度超限", "tag": "紧急", "level": "crit" },
      { "time": "10:29", "text": "门禁闸机离线", "tag": "告警", "level": "warn" },
      { "time": "10:21", "text": "消防设备异常", "tag": "提示", "level": "info" }
    ]
  }
  </script>

  <script>
    (function () {
      // 数据来自上面 #data 内联 JSON（必须来自物化数据集，禁止编造）
      var data = JSON.parse(document.getElementById('data').textContent);

      document.getElementById('kpi-total').textContent = data.kpi.total;
      document.getElementById('kpi-ok').textContent = data.kpi.ok;
      document.getElementById('kpi-alarm').textContent = data.kpi.alarm;
      document.getElementById('kpi-load').textContent = data.kpi.load;
      // delta 可选：有值就填，没有就留空
      if (data.kpi_delta) {
        if (data.kpi_delta.total) document.getElementById('kpi-total-delta').textContent = data.kpi_delta.total;
        if (data.kpi_delta.ok) document.getElementById('kpi-ok-delta').textContent = data.kpi_delta.ok;
        if (data.kpi_delta.alarm) document.getElementById('kpi-alarm-delta').textContent = data.kpi_delta.alarm;
      }

      var AXIS = { color: '#8a96ab' };
      var SPLIT = { lineStyle: { color: 'rgba(42,54,79,.8)' } };
      var BLUE = '#4da6ff';
      var CYAN = '#37e5ff';
      var COLORS = ['#4da6ff', '#37e5ff', '#4dbf8b', '#f5a623', '#e05656', '#9b59b6'];

      var charts = [];
      function init(id, option) {
        // echarts 全局由前端注入，禁止引入外部脚本
        var c = echarts.init(document.getElementById(id));
        c.setOption(option);
        charts.push(c);
        return c;
      }

      // TOP 排名榜渲染：序号 + 名称 + 数值（列表是 panel 的一种内容，与 .chart 并列）
      function renderRank(elId, items) {
        document.getElementById(elId).innerHTML = items.map(function (d, i) {
          return '<div class="rank-item"><span class="rank-no">' + (i + 1) + '</span>' +
            '<span class="rank-name">' + d.name + '</span>' +
            '<span class="rank-val">' + d.value + '</span></div>';
        }).join('');
      }
      // 滚动流水渲染：时间 + 文本 + 徽标（level 取 crit/warn/info 三档配色）
      function renderFeed(elId, items) {
        document.getElementById(elId).innerHTML = items.map(function (d) {
          return '<div class="feed-item"><span class="feed-time">' + d.time + '</span>' +
            '<span class="feed-text">' + d.text + '</span>' +
            '<span class="feed-badge ' + d.level + '">' + d.tag + '</span></div>';
        }).join('');
      }

      // 折线 + 渐变面积（图表一律不配 label，可读性靠图例/tooltip/坐标轴）
      init('chart-trend', {
        backgroundColor: 'transparent',
        tooltip: { trigger: 'axis' },
        grid: { left: 44, right: 16, top: 28, bottom: 28 },
        xAxis: { type: 'category', data: data.trend.x, axisLabel: AXIS,
                 axisLine: { lineStyle: { color: '#2a364f' } } },
        yAxis: { type: 'value', axisLabel: AXIS, splitLine: SPLIT },
        series: [{
          name: '温度(℃)', type: 'line', smooth: true, data: data.trend.temp,
          lineStyle: { width: 2, color: BLUE, shadowBlur: 8, shadowColor: 'rgba(77,166,255,.5)' },
          itemStyle: { color: BLUE },
          areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: 'rgba(77,166,255,.32)' }, { offset: 1, color: 'rgba(77,166,255,0)' }] } }
        }]
      });

      // 环形占比：无 label，图例在底部
      init('chart-comp', {
        backgroundColor: 'transparent',
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        legend: { bottom: 0, textStyle: { color: '#8a96ab' }, itemWidth: 12, itemHeight: 12 },
        series: [{ type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'], data: data.comp,
                   itemStyle: { borderColor: '#0d1b33', borderWidth: 2 }, color: COLORS }]
      });

      // 渐变柱
      init('chart-bar', {
        backgroundColor: 'transparent',
        tooltip: { trigger: 'axis' },
        grid: { left: 44, right: 16, top: 28, bottom: 28 },
        xAxis: { type: 'category', data: data.bar.x, axisLabel: AXIS,
                 axisLine: { lineStyle: { color: '#2a364f' } } },
        yAxis: { type: 'value', axisLabel: AXIS, splitLine: SPLIT },
        series: [{ type: 'bar', data: data.bar.y, barMaxWidth: 42,
                   itemStyle: { borderRadius: [4, 4, 0, 0],
                     color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
                       colorStops: [{ offset: 0, color: CYAN }, { offset: 1, color: BLUE }] } } }]
      });

      // 雷达（指标名即坐标轴，非数据标签）
      init('chart-radar', {
        backgroundColor: 'transparent',
        tooltip: {},
        legend: { bottom: 0, textStyle: { color: '#8a96ab' }, itemWidth: 12, itemHeight: 12 },
        radar: { indicator: data.radar.indicator.map(function (d) { return { name: d.name, max: d.max }; }),
                 radius: '62%', axisName: { color: '#8a96ab' },
                 splitArea: { areaStyle: { color: ['rgba(77,166,255,.04)', 'rgba(77,166,255,.08)'] } },
                 axisLine: { lineStyle: { color: 'rgba(42,54,79,.7)' } },
                 splitLine: { lineStyle: { color: 'rgba(42,54,79,.7)' } } },
        series: [{ type: 'radar', data: data.radar.series.map(function (s) {
          return { name: s.name, value: s.values,
                   areaStyle: { color: 'rgba(77,166,255,.28)' },
                   lineStyle: { color: BLUE }, itemStyle: { color: BLUE } };
        }) }]
      });

      // 列表渲染（非 echarts，纯 DOM）
      renderRank('rank-alarm', data.alarm_top);
      renderFeed('feed-alarm', data.alarm_feed);

      // 窗口尺寸变化时重绘，图表随视口缩放铺满
      window.addEventListener('resize', function () {
        charts.forEach(function (c) { c.resize(); });
      });
    })();
  </script>
</body>
</html>
```

## 扩展提示

- **主次层次**：内容区是 12 栅格，主图用 `.span-8`/`.span-6` **只加宽、不加高**（高度与其他卡一致），次要图表用 `.span-4`/`.span-3` 等宽。**每行 span 之和必须正好 12**（8+4、4+4+4、6+6、6+3+3、12），不留空列；**不要用跨行，所有卡等高**
- **图文结合**：除 `.chart` 外，`.panel` 里可放 `.rank-list`（TOP 排名榜，序号+名称+数值，前三名序号高亮）或 `.feed-list`（滚动流水，时间+文本+状态徽标 `crit`/`warn`/`info`）；关键排名/明细/事件用列表呈现，大屏不该只有图
- **多 KPI / 多图表 / 多列表**：按 `.kpi` / `.panel` + `.chart` / `.rank-list` / `.feed-list` 结构复制，id 保持唯一，JSON 里加对应字段，跨度和数量按需增减
- **更换图表类型**：`series[0].type` 改 `bar` / `area`（line + `areaStyle`）/ `scatter` / `radar` / `gauge` 等，echarts 已内置全部常用类型；仪表盘 `.chart` 用 240px 高的 `.gauge`（本例未用）
- **告警/状态类数据**：可用 `itemStyle.color` 条件着色（如超阈值 `#e05656`、正常 `#4dbf8b`）；KPI 涨跌用 `.delta.up` / `.delta.down`
- **不要**在 echarts 里配 `label`（柱顶数值/饼图文字/折线数据点都不加）——数字可读性靠 tooltip/图例/坐标轴
- **不要**给 `.chart` 写固定像素高度——铺满靠 `body` 的 `height:100vh` + `.charts` `flex:1` + `.chart` `flex:1` 三级联动
- **不要**引入外部 CDN、外链图片或字体，数据与样式必须自包含，否则内网/离线渲染空白
- **颜色复用常量**：图表/渐变配色一律复用模板里已定义的 `BLUE` / `CYAN` / `COLORS` 等常量，不要手写裸 hex 字符串（如 `'#37e5ff'`）——手写易漏引号导致 JS 语法错误、整段脚本不执行、看板渲染空白

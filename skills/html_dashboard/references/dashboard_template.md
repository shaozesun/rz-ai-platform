---
name: dashboard_template
description: HTML 看板自包含模板（大屏科技风）。复制下方 ```html 块内完整内容到 analysis_write_file，替换 <script id="data"> 内联 JSON 与标题，按示例 echarts 配置填充图表。
---

# dashboard 模板（自包含 HTML，大屏科技风）

**用法**：把下面 ```html 块里的完整内容复制给 `analysis_write_file(dataset_id, 'dashboard.html', <内容>)`。只需改三处——① `h1`/`.subtitle` 标题；② `<script id="data">` 里的内联 JSON（换成物化数据，保持结构）；③ 各 `setOption` 的 series 数据引用与字段名（若改了 JSON 字段名）。KPI 数量、图表数量可按需增删，但每个图表容器都要有显式 height（`.chart` 260px、`.chart.gauge` 240px）。

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
    padding: 24px;
    background:
      repeating-linear-gradient(0deg, rgba(77,166,255,.045) 0 1px, transparent 1px 42px),
      repeating-linear-gradient(90deg, rgba(77,166,255,.045) 0 1px, transparent 1px 42px),
      radial-gradient(1200px 700px at 18% -8%, rgba(38,90,160,.38), transparent 60%),
      linear-gradient(180deg, #0d1b33 0%, #0a1428 55%, #071024 100%);
  }
  /* 标题栏：左侧标题 + 实时脉冲点 + 右侧渐变分隔线 */
  .title-bar { display: flex; align-items: center; gap: 12px; margin-bottom: 6px; }
  .title-bar h1 { font-size: 20px; font-weight: 600; color: #f2f7ff; letter-spacing: .5px; }
  .title-bar::after { content: ""; flex: 1; height: 1px;
    background: linear-gradient(90deg, rgba(77,166,255,.6), rgba(77,166,255,0)); }
  .live { display: inline-flex; align-items: center; gap: 6px; color: #8a96ab; font-size: 12px; white-space: nowrap; }
  .live::before { content: ""; width: 8px; height: 8px; border-radius: 50%;
    background: #4dbf8b; box-shadow: 0 0 8px rgba(77,191,139,.8);
    animation: pulse 1.6s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
  .subtitle { color: #8a96ab; font-size: 13px; margin-bottom: 20px; }
  /* KPI 指标卡：左侧彩色竖条 + 发光数字 + 涨跌色 */
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 20px; }
  .kpi { position: relative; overflow: hidden; padding: 16px 18px 14px 18px;
    background: linear-gradient(180deg, rgba(22,34,56,.92), rgba(13,22,38,.94));
    border: 1px solid rgba(77,166,255,.18); border-radius: 10px;
    box-shadow: 0 8px 24px rgba(0,0,0,.35), inset 0 0 18px rgba(77,166,255,.05); }
  .kpi::before { content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 2px;
    background: linear-gradient(180deg, #4da6ff, #37e5ff); }
  .kpi .label { color: #8a96ab; font-size: 12px; }
  .kpi .value { margin-top: 4px; font-size: 30px; font-weight: 600; color: #4da6ff;
    text-shadow: 0 0 14px rgba(77,166,255,.6); font-variant-numeric: tabular-nums; }
  .kpi .delta { font-size: 12px; margin-top: 2px; }
  .delta.up { color: #4dbf8b; }
  .delta.down { color: #e05656; }
  /* 图表卡片：渐变底 + 发光角标 + 悬停提亮 */
  .charts { display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; }
  .panel { position: relative; overflow: hidden; padding: 14px 16px;
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
  .panel h2 { font-size: 14px; font-weight: 500; color: #c3cde0; margin-bottom: 10px; }
  .chart { width: 100%; height: 260px; }
  .chart.gauge { height: 240px; }
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
    <div class="panel"><h2>温度趋势</h2><div class="chart" id="chart-trend"></div></div>
    <div class="panel"><h2>设备类型占比</h2><div class="chart" id="chart-comp"></div></div>
    <div class="panel"><h2>容量对比</h2><div class="chart" id="chart-bar"></div></div>
    <div class="panel"><h2>平均负载</h2><div class="chart gauge" id="chart-gauge"></div></div>
    <div class="panel"><h2>设备健康度</h2><div class="chart" id="chart-radar"></div></div>
  </div>

  <!-- 数据内联在 JSON 里，禁止任何外部 src；渲染脚本读这里填充 KPI 与图表 -->
  <script id="data" type="application/json">
  {
    "kpi": { "total": 0, "ok": 0, "alarm": 0, "load": "0%" },
    "kpi_delta": { "total": "+3.2%", "ok": "98.2%", "alarm": "-5" },
    "trend": { "x": ["01-01", "01-02"], "temp": [20, 21] },
    "comp": [ { "name": "配电", "value": 1 }, { "name": "暖通", "value": 1 } ],
    "bar": { "x": ["A", "B"], "y": [10, 20] },
    "gauge": { "value": 0, "max": 100 },
    "radar": {
      "indicator": [ { "name": "温度", "max": 100 }, { "name": "湿度", "max": 100 }, { "name": "负载", "max": 100 } ],
      "series": [ { "name": "机房A", "values": [80, 70, 60] } ]
    }
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

      function init(id) {
        // echarts 全局由前端注入，禁止引入外部脚本
        return echarts.init(document.getElementById(id));
      }

      // 折线 + 渐变面积（图表一律不配 label，可读性靠图例/tooltip/坐标轴）
      init('chart-trend').setOption({
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
      init('chart-comp').setOption({
        backgroundColor: 'transparent',
        tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
        legend: { bottom: 0, textStyle: { color: '#8a96ab' }, itemWidth: 12, itemHeight: 12 },
        series: [{ type: 'pie', radius: ['42%', '68%'], center: ['50%', '44%'], data: data.comp,
                   itemStyle: { borderColor: '#0d1b33', borderWidth: 2 } }]
      });

      // 渐变柱
      init('chart-bar').setOption({
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

      // 仪表盘（detail 数字是仪表盘自身语义）
      init('chart-gauge').setOption({
        backgroundColor: 'transparent',
        series: [{
          type: 'gauge', min: 0, max: data.gauge.max, data: [{ value: data.gauge.value }],
          progress: { show: true, width: 12, itemStyle: { color: CYAN } },
          axisLine: { lineStyle: { width: 12, color: [[1, 'rgba(42,54,79,.6)']] } },
          axisTick: { show: false }, splitLine: { show: false }, axisLabel: { show: false },
          pointer: { show: false }, anchor: { show: false },
          detail: { fontSize: 26, fontWeight: 600, color: BLUE, offsetCenter: [0, '55%'], formatter: '{value}' }
        }]
      });

      // 雷达（指标名即坐标轴，非数据标签）
      init('chart-radar').setOption({
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
    })();
  </script>
</body>
</html>
```

## 扩展提示

- **多 KPI / 多图表**：按 `.kpi` / `.panel` + `.chart` 结构复制，id 保持唯一，JSON 里加对应字段
- **更换图表类型**：`series[0].type` 改 `bar` / `area`（line + `areaStyle`）/ `scatter` / `radar` / `gauge` 等，echarts 已内置全部常用类型；仪表盘用 `.chart.gauge`（240px）
- **告警/状态类数据**：可用 `itemStyle.color` 条件着色（如超阈值 `#e05656`、正常 `#4dbf8b`）；KPI 涨跌用 `.delta.up` / `.delta.down`
- **不要**在 echarts 里配 `label`（柱顶数值/饼图文字/折线数据点都不加）——数字可读性靠 tooltip/图例/坐标轴
- **不要**引入外部 CDN、外链图片或字体，数据与样式必须自包含，否则内网/离线渲染空白

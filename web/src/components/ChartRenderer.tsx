import { useEffect, memo, useMemo, useRef, useState, type ReactNode } from 'react';
import { Download, Maximize2, Minimize2 } from 'lucide-react';
import {
  Area,
  Bar,
  Box,
  Column,
  DualAxes,
  Funnel,
  Histogram,
  Line,
  Liquid,
  Pie,
  Radar,
  Sankey,
  Scatter,
  Treemap,
  Venn,
  Violin,
  WordCloud,
} from '@ant-design/plots';
import {
  Fishbone,
  FlowDirectionGraph,
  MindMap,
  NetworkGraph,
  OrganizationChart,
} from '@ant-design/graphs';
import type { GraphData as G6GraphData, TreeData as G6TreeData } from '@antv/g6';
import MapChart from './MapChart';

/**
 * Agent 回复中 <chart> 标签的本地渲染出口。
 *
 * 数据来自 LLM 输出的图表 spec（type + 标签属性 + JSON 正文），全部在前端本地用
 * @ant-design/plots / @ant-design/graphs 渲染，数据不出内网（对比 DeerFlow 的云端
 * 渲染方案，见 docs 设计记录）。
 *
 * 支持 26 种类型：plots 常用图 17 种 + graphs 结构图 5 种 + 电子表格 1 种 +
 * 地图 3 种（降级为表格）。结构图 data 为 JSON 对象，其余为 JSON 数组。
 */
export interface ChartSpec {
  type: string;
  attrs: Record<string, string>;
  data: string;
}

const HEIGHT = 320;

// 现代多彩默认色板：LLM 未传 palette 时兜底给多系列/饼图分类用
// （单系列默认色按用户要求保持 G2 出厂蓝，不覆盖）。
const DEFAULT_PALETTE = [
  '#6366F1', '#22C55E', '#F59E0B', '#EF4444', '#8B5CF6',
  '#06B6D4', '#EC4899', '#84CC16', '#F97316', '#14B8A6',
];

// 条形图单系列专属色：荧光蓝（降亮度），区分 G2 默认浅蓝与饼图色板
const BAR_COLOR = '#06B6D4';

// 图型别名：模型常把「属性/俗称」当图型写。环形图（donut/ring）在 @ant-design/plots 里
// 不是独立图型，而是 Pie 设 innerRadius；统一归一到已支持的 type，避免整张图报「不支持」。
const TYPE_ALIASES: Record<string, string> = {
  donut: 'pie',
  ring: 'pie',
};

// 主题白名单：plots 走 G2 v5 主题，graphs 走 G6 v5 主题（G6 无 academy，仅 light/dark）
const PLOT_THEMES = ['classic', 'classicDark', 'light', 'dark', 'academy'];
const GRAPH_THEMES = ['light', 'dark'];
const GRAPH_TYPES = new Set(['mind_map', 'organization_chart', 'fishbone_diagram', 'flow_diagram', 'network_graph']);

function parseJson(raw: string): unknown {
  if (!raw || !raw.trim()) return null;
  try {
    return JSON.parse(raw.trim());
  } catch {
    return null;
  }
}

function asRows(value: unknown): Record<string, unknown>[] | null {
  return Array.isArray(value) ? (value as Record<string, unknown>[]) : null;
}

// ---------- 特殊图数据归一（模型的 data 字段名一律中文，渲染器不能写死英文列名） ----------

function isNum(v: unknown): v is number {
  return typeof v === 'number' && Number.isFinite(v);
}

/** 节点名称：优先 name/label，否则取第一个非空字符串字段（中文键也能取到） */
function nodeName(o: Record<string, unknown>): string {
  const direct = o.name ?? o.label;
  if (typeof direct === 'string' && direct) return direct;
  const key = Object.keys(o).find((k) => typeof o[k] === 'string' && o[k]);
  return key ? String(o[key]) : '';
}

/** 节点数值：优先 value，否则取第一个数值字段 */
function nodeValue(o: Record<string, unknown>): number {
  if (isNum(o.value)) return o.value;
  const key = Object.keys(o).find((k) => isNum(o[k]));
  return key ? Number(o[key]) : 0;
}

/**
 * treemap 数据归一成**单根树**。
 *
 * G2 的 treeDataTransform 对数组走 d3 `stratify()`，只能建单根树：顶层给
 * `[{name,value,children}, ...]` 会抛 `multiple roots`，异常落在 render 的
 * promise 里没人接，整张图静默空白。这里把顶层数组套一层根节点，并把节点键
 * 统一成 `name`/`value`（G2 只认这两个，中文键取不到）。
 */
function toTreemapTree(raw: unknown): Record<string, unknown> | null {
  // 只有叶子带 value：父节点的 value 被 ignoreParentValue 忽略，且根节点带 value 会被
  // G2 的 data 归一逻辑误判成 `{value: 数据}` 信封（plots 的 dataTransform 也按这个判）
  const build = (v: unknown): Record<string, unknown> | null => {
    if (!v || typeof v !== 'object' || Array.isArray(v)) return null;
    const o = v as Record<string, unknown>;
    const out: Record<string, unknown> = { name: nodeName(o) };
    const kids = Array.isArray(o.children)
      ? o.children.map(build).filter((n): n is Record<string, unknown> => n !== null)
      : [];
    if (kids.length) out.children = kids;
    else out.value = nodeValue(o);
    return out;
  };
  if (Array.isArray(raw)) {
    const kids = raw.map(build).filter((n): n is Record<string, unknown> => n !== null);
    return kids.length ? { name: '合计', children: kids } : null;
  }
  return build(raw);
}

// ---------- 结构图数据转换（LLM 用 name/children、nodes/edges，graphs 用 id/label） ----------

function toGraphData(raw: unknown): G6GraphData | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const obj = raw as Record<string, unknown>;
  if (!Array.isArray(obj.nodes) && !Array.isArray(obj.edges)) return null;
  const nodes = (Array.isArray(obj.nodes) ? obj.nodes : []).map((n) => {
    const node = n as Record<string, unknown>;
    const label = String(node.name ?? node.id ?? '');
    return { id: label, data: { label } };
  });
  const edges = (Array.isArray(obj.edges) ? obj.edges : []).map((e) => {
    const edge = e as Record<string, unknown>;
    return { source: String(edge.source ?? ''), target: String(edge.target ?? '') };
  });
  return { nodes, edges };
}

function toTreeData(raw: unknown): G6TreeData | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const node = raw as Record<string, unknown>;
  const label = String(node.name ?? node.topic ?? node.id ?? '');
  const result: G6TreeData = { id: label, data: { label } };
  if (Array.isArray(node.children)) {
    const children = node.children.map(toTreeData).filter((c): c is G6TreeData => c !== null);
    if (children.length) result.children = children;
  }
  return result;
}

// 树形 {name, children} 拍平为 {nodes, edges}（组织架构图用 dagre 布局，只收图数据）
function treeToGraph(raw: unknown): G6GraphData | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const nodes: G6GraphData['nodes'] = [];
  const edges: G6GraphData['edges'] = [];
  const walk = (node: Record<string, unknown>, parent: string | null) => {
    const label = String(node.name ?? node.id ?? '');
    if (!nodes.some((n) => n.id === label)) {
      nodes.push({ id: label, data: { label } });
    }
    if (parent) edges.push({ source: parent, target: label });
    const children = node.children;
    if (Array.isArray(children)) {
      for (const child of children) {
        if (child && typeof child === 'object') {
          walk(child as Record<string, unknown>, label);
        }
      }
    }
  };
  walk(raw as Record<string, unknown>, null);
  return { nodes, edges };
}

// ---------- 表格降级（spreadsheet / 地图类） ----------

function DataTable({
  rows,
  columns,
}: {
  rows: Record<string, unknown>[];
  columns?: string[];
}) {
  const cols = columns?.length
    ? columns
    : rows.length
      ? Object.keys(rows[0])
      : [];
  return (
    <div className="max-h-80 overflow-auto rounded-md border border-border">
      <table className="w-full border-collapse text-xs">
        <thead>
          <tr className="bg-card">
            {cols.map((c) => (
              <th key={c} className="border-b border-border p-1.5 text-left font-medium">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i} className={i % 2 ? 'bg-card/40' : ''}>
              {cols.map((c) => (
                <td key={c} className="border-b border-border/60 p-1.5">
                  {String(r[c] ?? '')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------- 主体 ----------

function ChartRenderer({ spec }: { spec: ChartSpec }) {
  const { type: rawType = '', attrs, data } = spec;
  // 图型别名归一：donut/ring 是环形图的俗称，本质是 pie（下面用 innerRadius 出环）——
  // 不归一会直接落到「不支持的图表类型」兜底。其余属性原样透传，type 的用法保持不变。
  const isDonut = rawType === 'donut' || rawType === 'ring';
  const type = TYPE_ALIASES[rawType.toLowerCase()] ?? rawType;
  const parsed = useMemo(() => parseJson(data), [data]);
  const rows = useMemo(() => asRows(parsed), [parsed]);

  // 字段推断兜底：LLM 组装 <chart> 时可能漏写 xField/yField（实测 pie 因此渲染空白）。
  // x = 首个非数值字段（类别/颜色轴），y = 首个数值字段（值/角度轴）；attrs 显式指定优先。
  const inferred = useMemo(() => {
    const first = rows?.[0];
    if (!first) return { x: '', y: '' };
    const keys = Object.keys(first);
    const y = keys.find((k) => typeof first[k] === 'number') ?? '';
    const x = keys.find((k) => typeof first[k] !== 'number') ?? keys[0] ?? '';
    return { x, y };
  }, [rows]);

  let xField = attrs.xField || inferred.x;
  let yField = attrs.yField || inferred.y;
  const seriesField = attrs.groupField || attrs.colorField || '';
  const stacked = attrs.stacked === 'true';

  // 字段方向归一：xField 必须是类别/时间列、yField 必须是数值列。模型按 ECharts 直觉常把
  // 数值写进 xField，bar 尤其多（Bar 自带 coordinate.transpose，规范本就是 x=类别），
  // 写反后类别会落到横轴、数值轴拿到文本被强转成 0。只在「x 是数值列、y 不是数值列」这种
  // 明确反向时交换；x/y 都为数值（如年份 vs 数量）不误伤。
  const firstRow = rows?.[0];
  if (
    firstRow
    && typeof firstRow[xField] === 'number'
    && firstRow[yField] !== undefined
    && typeof firstRow[yField] !== 'number'
  ) {
    [xField, yField] = [yField, xField];
  }

  // 非数值列 / 数值列，供特殊图（sankey/word_cloud）推断字段：sankey 的源与目标取
  // 前两个非数值列、权重取第一个数值列；词云取第一个非数值列 + 第一个数值列。
  const textKeys = useMemo(
    () => (rows?.[0] ? Object.keys(rows[0]).filter((k) => typeof rows[0][k] !== 'number') : []),
    [rows],
  );
  const numKeys = useMemo(
    () => (rows?.[0] ? Object.keys(rows[0]).filter((k) => typeof rows[0][k] === 'number') : []),
    [rows],
  );

  // 样式解析：LLM 属性（theme/height/width/palette/lineWidth/backgroundColor）+ 全屏覆盖高度
  const [isFullscreen, setIsFullscreen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onFsChange = () => setIsFullscreen(document.fullscreenElement === containerRef.current);
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  }, []);
  // 跟随应用明暗模式：深色（dark/black，黑色模式同时带 .dark）下图表默认用深色主题，
  // 避免深色文字在深色背景上看不清；切主题时 MutationObserver 触发重渲染
  const [isDark, setIsDark] = useState(() => document.documentElement.classList.contains('dark'));
  useEffect(() => {
    const el = document.documentElement;
    const check = () => setIsDark(el.classList.contains('dark'));
    check();
    const mo = new MutationObserver(check);
    mo.observe(el, { attributes: true, attributeFilter: ['class'] });
    return () => mo.disconnect();
  }, []);
  // 懒加载：图表进入视口附近才真正 mount G2 图。历史会话里图表多时（单条最多 6 图），
  // 全部立即 mount 会阻塞主线程数秒；滚动到哪张图哪张才渲染，首屏秒开。
  const [inView, setInView] = useState(false);
  // 容器宽度（px）。G2 的 autoFit **只监听 window resize**（见下方 effect 注释），容器宽度变化
  // 不会触发重算，所以这里自己量，用宽度档位换 key 强制重挂载。
  const [boxW, setBoxW] = useState(0);
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof IntersectionObserver === 'undefined') {
      setInView(true); // 不支持 IntersectionObserver 的环境直接渲染，保底
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            setInView(true);
            io.disconnect();
          }
        }
      },
      { rootMargin: '200px 0px', threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, []);
  // 容器宽度监听：G2 的 autoFit 只绑 window.resize（node_modules/@antv/g2/lib/api/runtime.js:392），
  // **@ant-design/plots 与 @antv/g2 里没有任何 ResizeObserver** —— 容器宽度变了（网格按图表数量
  // 重排、右侧面板开合、进全屏）canvas 仍是挂载时的旧宽度；卡片又是 overflow-hidden，右侧就被
  // 裁掉、饼图看着不居中（实测反馈）。这里自己量容器宽度，跨档位就换 key 让 G2 按新尺寸重初始化。
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof ResizeObserver === 'undefined') return;
    let raf = 0;
    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width ?? 0;
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => setBoxW(Math.round(w)));
    });
    ro.observe(el);
    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
    };
  }, []);

  const isGraph = GRAPH_TYPES.has(type);
  const theme = (attrs.theme || '').toLowerCase();
  const llmTheme = (isGraph ? GRAPH_THEMES : PLOT_THEMES).includes(theme) ? theme : '';
  // LLM 未指定主题时，按应用明暗模式默认：深色用 classicDark/dark，浅色用 classic/light
  const defaultTheme = isDark
    ? (isGraph ? 'dark' : 'classicDark')
    : (isGraph ? 'light' : 'classic');
  const validTheme = llmTheme || defaultTheme;
  const parsedHeight = Number(attrs.height);
  const baseHeight = attrs.height && Number.isFinite(parsedHeight) ? parsedHeight : HEIGHT;
  const height = isFullscreen ? Math.max(window.innerHeight - 120, 240) : baseHeight;
  const width = attrs.width ? Number(attrs.width) : undefined;
  const palette = (attrs.palette ?? '').split(',').map((s) => s.trim()).filter(Boolean);
  const colorRange = palette.length ? palette : DEFAULT_PALETTE;
  const lineWidthNum = Number(attrs.lineWidth);
  const lineWidth = attrs.lineWidth && Number.isFinite(lineWidthNum) ? lineWidthNum : 2;
  const bgColor = attrs.backgroundColor || '';
  // 全屏时用不透明底色：半透明 bg-card/60 叠在全屏黑底上会成灰色
  const cardBg = isFullscreen ? bgColor || 'var(--background)' : bgColor;

  // 单系列默认色保持 G2 出厂蓝（用户选择，不覆盖）；仅多系列/饼图套用多彩分类色板
  const base = {
    height,
    autoFit: !width,
    ...(width ? { width } : {}),
    theme: validTheme,
  };
  // 多系列（分组/堆叠）：seriesField 只映射 G2 的 encode.series（用于堆叠/并排分组），
  // **不上色**；颜色由 encode.color 决定，必须同时给 colorField，否则各系列全是同一个颜色。
  const grouped = seriesField ? { seriesField, colorField: seriesField } : {};

  let chart: ReactNode = null;

  // ---- plots 常用图（data 为对象数组） ----
  if (rows) {
    const plotBase = {
      ...base,
      data: rows,
      // 分类色板：LLM 指定 palette 用它，否则用默认现代多彩
      scale: { color: { range: colorRange } },
    };
    switch (type) {
      case 'line':
        chart = (
          <Line {...plotBase} xField={xField} yField={yField} {...grouped}
            {...(lineWidth ? { style: { lineWidth } } : {})} />
        );
        break;
      case 'area':
        chart = (
          <Area {...plotBase} xField={xField} yField={yField} {...grouped}
            {...(lineWidth ? { style: { lineWidth } } : {})} />
        );
        break;
      case 'column':
        chart = (
          <Column
            {...plotBase}
            xField={xField}
            yField={yField}
            {...grouped}
            {...(stacked ? { stack: true } : {})}
          />
        );
        break;
      case 'bar':
        chart = (
          <Bar
            {...plotBase}
            xField={xField}
            yField={yField}
            {...grouped}
            {...(seriesField ? {} : { style: { fill: BAR_COLOR } })}
            {...(stacked ? { stack: true } : {})}
          />
        );
        break;
      case 'pie':
        chart = (
          <Pie
            {...plotBase}
            angleField={yField}
            colorField={xField}
            innerRadius={attrs.donut === 'true' || isDonut ? 0.6 : 0}
          />
        );
        break;
      case 'radar':
        chart = <Radar {...plotBase} xField={xField} yField={yField} {...grouped} />;
        break;
      case 'scatter':
        chart = (
          <Scatter {...plotBase} xField={xField} yField={yField} shapeField={seriesField || undefined} />
        );
        break;
      case 'dual_axes':
        chart = <DualAxes {...plotBase} xField={xField} yField={yField?.split(',')} />;
        break;
      case 'funnel':
        chart = <Funnel {...plotBase} xField={xField} yField={yField} />;
        break;
      case 'histogram': {
        // binWidth/binNumber 为必填类型字段；binNumber 固定 8，binWidth 由数据范围推导
        const numeric = rows
          .map((r) => Number(r[xField]))
          .filter((n) => Number.isFinite(n));
        const span = numeric.length ? Math.max(...numeric) - Math.min(...numeric) : 0;
        const binWidth = span > 0 ? span / 8 : 1;
        chart = <Histogram {...plotBase} binField={xField} binWidth={binWidth} binNumber={8} />;
        break;
      }
      case 'box':
      case 'boxplot':
        chart = <Box {...plotBase} xField={xField} yField={yField} {...grouped} />;
        break;
      case 'violin':
        chart = <Violin {...plotBase} xField={xField} yField={yField} {...grouped} />;
        break;
      case 'treemap': {
        // 颜色走 G2 默认（按顶层分支 d.path[1] 取色），不再按 name 逐叶上色
        const tree = toTreemapTree(parsed);
        chart = tree ? <Treemap {...plotBase} data={tree} valueField="value" /> : null;
        break;
      }
      case 'sankey': {
        // plots v2 无 sourceField/targetField/weightField 映射，直接给 G2 的 encode
        const source = attrs.sourceField || textKeys[0] || '';
        const target = attrs.targetField || textKeys[1] || '';
        const weight = attrs.weightField || numKeys[0] || '';
        chart = source && target && weight
          ? <Sankey {...plotBase} encode={{ source, target, value: weight }} />
          : null;
        break;
      }
      case 'word_cloud': {
        const text = attrs.textField || textKeys[0] || '';
        const weight = attrs.valueField || numKeys[0] || '';
        chart = text && weight
          ? <WordCloud {...plotBase} textField={text} valueField={weight} />
          : null;
        break;
      }
      case 'venn':
        chart = (
          <Venn
            {...plotBase}
            setsField={attrs.setsField || 'sets'}
            valueField={attrs.valueField || 'value'}
          />
        );
        break;
      case 'liquid': {
        const percent = Number(rows[0]?.[attrs.percentField || 'percent'] ?? 0);
        chart = <Liquid {...base} percent={Number.isFinite(percent) ? percent : 0} />;
        break;
      }
      case 'spreadsheet':
        chart = <DataTable rows={rows} columns={attrs.columns?.split(',')} />;
        break;
      case 'pin_map':
        // 点描地图：中国底图 + 园区点位（echarts + 本地 GeoJSON，懒加载）
        chart = rows.length
          ? <MapChart rows={rows} height={height} isDark={isDark} />
          : null;
        break;
      case 'district_map':
      case 'path_map':
        chart = (
          <div>
            <p className="mb-1.5 text-xs text-muted-foreground">
              该地图类型暂不支持渲染，以下为数据表格。
            </p>
            <DataTable rows={rows} columns={attrs.columns?.split(',')} />
          </div>
        );
        break;
      default:
        chart = null;
    }
  }

  // ---- graphs 结构图（data 为 JSON 对象；autoFit 类型特殊，只传 height） ----
  if (!chart) {
    const graphBase = {
      height,
      ...(width ? { width } : {}),
      theme: validTheme,
    };
    switch (type) {
      case 'mind_map': {
        const tree = toTreeData(parsed);
        chart = tree ? (
          <MindMap
            {...graphBase}
            data={tree}
            // 默认 boxed：官方分支配色（assign-color-by-branch 每分支一色）；可传 type="linear" 换下划线样式
            type={attrs.type === 'linear' ? 'linear' : 'boxed'}
            {...(attrs.direction === 'left' || attrs.direction === 'right' || attrs.direction === 'alternate' ? { direction: attrs.direction } : {})}
          />
        ) : null;
        break;
      }
      case 'organization_chart': {
        const graph = treeToGraph(parsed);
        chart = graph ? <OrganizationChart {...graphBase} data={graph} /> : null;
        break;
      }
      case 'fishbone_diagram': {
        const tree = toTreeData(parsed);
        chart = tree ? <Fishbone {...graphBase} data={tree} /> : null;
        break;
      }
      case 'flow_diagram': {
        const graph = toGraphData(parsed);
        chart = graph ? <FlowDirectionGraph {...graphBase} data={graph} /> : null;
        break;
      }
      case 'network_graph': {
        const graph = toGraphData(parsed);
        chart = graph ? <NetworkGraph {...graphBase} data={graph} /> : null;
        break;
      }
      default:
        chart = null;
    }
  }

  if (!chart) {
    return (
      <div className="my-2 rounded-lg border border-border bg-card/60 p-3 text-xs text-muted-foreground">
        不支持的图表类型或数据格式错误：{type}
      </div>
    );
  }

  const toggleFullscreen = () => {
    const el = containerRef.current;
    if (!el) return;
    if (document.fullscreenElement) void document.exitFullscreen();
    else void el.requestFullscreen();
  };

  const downloadChart = () => {
    const canvas = containerRef.current?.querySelector('canvas');
    if (!canvas) return;
    const url = canvas.toDataURL('image/png');
    const a = document.createElement('a');
    a.href = url;
    a.download = `${attrs.title || type || 'chart'}.png`;
    a.click();
  };

  return (
    <div
      ref={containerRef}
      className={
        isFullscreen
          // 全屏：撑满视口、去掉卡片边框圆角，图表区域由下方显式高度撑开
          ? 'flex h-full w-full flex-col overflow-hidden rounded-none border-0 bg-background p-4'
          // min-w-0：防卡片头部的长标题把网格列撑破（grid/flex 的 min-width:auto 坑）
          // max-w-[720px]：单图不被拉满（组里只有一张图时很常见），纵向间距交给外层网格的 gap
          : 'min-w-0 max-w-[720px] overflow-hidden rounded-lg border border-border bg-card/60 p-3'
      }
      style={cardBg ? { backgroundColor: cardBg } : undefined}
    >
      <div className="mb-2 flex items-center justify-between gap-2">
        {attrs.title ? <p className="text-sm font-medium">{attrs.title}</p> : <span />}
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={toggleFullscreen}
            aria-label={isFullscreen ? '退出全屏' : '全屏'}
            title={isFullscreen ? '退出全屏' : '全屏'}
            className="rounded p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground"
          >
            {isFullscreen ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
          </button>
          <button
            type="button"
            onClick={downloadChart}
            aria-label="下载图片"
            data-tip="下载图片"
            className="rounded p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground"
          >
            <Download className="size-4" />
          </button>
        </div>
      </div>
      {inView ? (
        // key 里带上"容器宽度档位"（16px 一档，避免抖动/拖动时每像素都重挂载）：宽度变化时
        // 强制重挂载，让 G2 按当前容器尺寸重新初始化 —— 否则 canvas 停在旧尺寸，被 overflow-hidden
        // 的卡片裁掉右侧（柱图最后一列被挡、饼图不居中）。全屏切换同理（瞬时尺寸变化监听不到）。
        <div
          key={`${isFullscreen ? 'chart-fs' : 'chart-norm'}-${Math.round(boxW / 16)}`}
          style={isFullscreen ? { height } : undefined}
        >
          {chart}
        </div>
      ) : (
        // 未进入视口前渲染同高度占位，避免布局跳动；滚动到附近才真正 mount G2 图
        <div
          style={{ height }}
          className="flex items-center justify-center text-xs text-muted-foreground"
        >
          图表加载中…
        </div>
      )}
    </div>
  );
}

export default memo(ChartRenderer);

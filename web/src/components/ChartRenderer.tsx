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
  const { type, attrs, data } = spec;
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

  const xField = attrs.xField || inferred.x;
  const yField = attrs.yField || inferred.y;
  const seriesField = attrs.groupField || attrs.colorField || '';
  const stacked = attrs.stacked === 'true';

  // 样式解析：LLM 属性（theme/height/width/palette/lineWidth/backgroundColor）+ 全屏覆盖高度
  const [isFullscreen, setIsFullscreen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onFsChange = () => setIsFullscreen(document.fullscreenElement === containerRef.current);
    document.addEventListener('fullscreenchange', onFsChange);
    return () => document.removeEventListener('fullscreenchange', onFsChange);
  }, []);
  const isGraph = GRAPH_TYPES.has(type);
  const theme = (attrs.theme || '').toLowerCase();
  const validTheme = (isGraph ? GRAPH_THEMES : PLOT_THEMES).includes(theme) ? theme : '';
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
  const cardBg = isFullscreen
    ? bgColor || (validTheme === 'dark' || validTheme === 'classicDark' ? '#141414' : '#ffffff')
    : bgColor;

  // 单系列默认色保持 G2 出厂蓝（用户选择，不覆盖）；仅多系列/饼图套用多彩分类色板
  const base = {
    height,
    autoFit: !width,
    ...(width ? { width } : {}),
    ...(validTheme ? { theme: validTheme } : {}),
  };
  const grouped = seriesField ? { seriesField } : {};

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
            innerRadius={attrs.donut === 'true' ? 0.6 : 0}
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
        chart = <Box {...plotBase} xField={xField} yField={yField} {...grouped} />;
        break;
      case 'violin':
        chart = <Violin {...plotBase} xField={xField} yField={yField} {...grouped} />;
        break;
      case 'treemap':
        chart = (
          <Treemap
            {...plotBase}
            dataField={attrs.valueField || 'value'}
            colorField={attrs.nameField || 'name'}
          />
        );
        break;
      case 'sankey':
        chart = (
          <Sankey
            {...plotBase}
            sourceField={attrs.sourceField || 'source'}
            targetField={attrs.targetField || 'target'}
            weightField={attrs.weightField || 'value'}
          />
        );
        break;
      case 'word_cloud':
        chart = (
          <WordCloud
            {...plotBase}
            wordField={attrs.wordField || 'word'}
            weightField={attrs.weightField || 'value'}
          />
        );
        break;
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
      case 'district_map':
      case 'pin_map':
      case 'path_map':
        chart = (
          <div>
            <p className="mb-1.5 text-xs text-muted-foreground">
              地图渲染暂不支持，以下为数据表格。
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
      ...(validTheme ? { theme: validTheme } : {}),
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
      className="my-2 overflow-hidden rounded-lg border border-border bg-card/60 p-3"
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
            title="下载图片"
            className="rounded p-1 text-muted-foreground hover:bg-muted/60 hover:text-foreground"
          >
            <Download className="size-4" />
          </button>
        </div>
      </div>
      {chart}
    </div>
  );
}

export default memo(ChartRenderer);

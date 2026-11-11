import { useEffect, useRef, useState } from 'react';

/**
 * 点描地图（pin_map）：中国底图 + 园区点位，全部本地渲染（数据不出内网）。
 *
 * 底图用 echarts 的 geo + 本地 GeoJSON（`public/china-geo.json`，省级边界，GCJ-02 坐标系），
 * 点用 geo 坐标系下的 scatter，点大小随数量缩放。名称/数值只在 tooltip 里显示——按项目约定
 * 图表上不加文字/数字标签。
 *
 * echarts 与 GeoJSON 都走懒加载：只有真的出现地图时才拉取，不胀主包。
 */

type Row = Record<string, unknown>;
type EChartsCore = typeof import('echarts/core');

// 字段约定见 skills/chart/references/pin_map.md；中英文别名都收
const NAME_KEYS = ['园区', 'name', '名称', '站点'];
const LNG_KEYS = ['经度', 'lng', 'lon', 'longitude'];
const LAT_KEYS = ['纬度', 'lat', 'latitude'];
const VALUE_KEYS = ['数量', '数值', 'value', 'count'];

const POINT_COLOR = '#6366F1';

function pick(row: Row, keys: string[]): unknown {
  for (const k of keys) {
    const v = row[k];
    if (v !== undefined && v !== null && v !== '') return v;
  }
  return undefined;
}

/** 取数值：优先约定字段名，否则取第一个非经纬度的数值列 */
function pickValue(row: Row, lngKey: string, latKey: string): number | null {
  const v = pick(row, VALUE_KEYS);
  if (typeof v === 'number') return v;
  const k = Object.keys(row).find(
    (key) => typeof row[key] === 'number' && key !== lngKey && key !== latKey,
  );
  return k ? Number(row[k]) : null;
}

interface Point {
  name: string;
  value: [number, number, number];
}

function toPoints(rows: Row[]): Point[] {
  const out: Point[] = [];
  for (const row of rows) {
    const nameRaw = pick(row, NAME_KEYS);
    const lngKey = LNG_KEYS.find((k) => row[k] !== undefined) ?? '';
    const latKey = LAT_KEYS.find((k) => row[k] !== undefined) ?? '';
    const lngRaw = row[lngKey];
    const latRaw = row[latKey];
    // 空串会被 Number() 转成 0，必须先在原始值上挡掉，否则会打出 [0,0] 这种错点
    if (lngRaw === undefined || latRaw === undefined || lngRaw === '' || latRaw === '') continue;
    const lng = Number(lngRaw);
    const lat = Number(latRaw);
    if (!Number.isFinite(lng) || !Number.isFinite(lat)) continue;
    out.push({
      name: String(nameRaw ?? `${lng},${lat}`),
      value: [lng, lat, pickValue(row, lngKey, latKey) ?? 1],
    });
  }
  return out;
}

function buildOption(points: Point[], isDark: boolean) {
  const max = Math.max(1, ...points.map((p) => p.value[2]));
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (p: { name: string; value: [number, number, number] }) =>
        `${p.name}<br/>数量：${p.value[2]}`,
    },
    geo: {
      map: 'china',
      roam: true,
      zoom: 1.2,
      label: { show: false },
      itemStyle: {
        areaColor: isDark ? '#1f2937' : '#f3f4f6',
        borderColor: isDark ? '#4b5563' : '#d1d5db',
      },
      emphasis: {
        label: { show: false },
        itemStyle: { areaColor: isDark ? '#374151' : '#e5e7eb' },
      },
    },
    series: [
      {
        type: 'scatter',
        coordinateSystem: 'geo',
        data: points,
        symbolSize: (v: number[]) => 9 + 20 * (Number(v[2]) / max),
        itemStyle: { color: POINT_COLOR, opacity: 0.9 },
      },
    ],
  };
}

export default function MapChart({
  rows,
  height,
  isDark,
}: {
  rows: Row[];
  height: number;
  isDark: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const chartRef = useRef<ReturnType<EChartsCore['init']> | null>(null);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState('');

  // 懒加载 echarts 子集 + 本地 GeoJSON，初始化一次
  useEffect(() => {
    let disposed = false;
    (async () => {
      try {
        const [core, charts, components, renderers, geo] = await Promise.all([
          import('echarts/core'),
          import('echarts/charts'),
          import('echarts/components'),
          import('echarts/renderers'),
          fetch('/china-geo.json').then((r) => r.json()),
        ]);
        if (disposed || !ref.current) return;
        core.use([
          charts.ScatterChart,
          components.GeoComponent,
          components.TooltipComponent,
          renderers.CanvasRenderer,
        ]);
        core.registerMap('china', geo as Parameters<typeof core.registerMap>[1]);
        chartRef.current = core.init(ref.current, undefined, { renderer: 'canvas' });
        setReady(true);
      } catch {
        if (!disposed) setError('地图加载失败');
      }
    })();
    return () => {
      disposed = true;
      chartRef.current?.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!ready || !chartRef.current) return;
    chartRef.current.setOption(buildOption(toPoints(rows), isDark), true);
  }, [ready, rows, isDark]);

  // 容器尺寸变化（含全屏切换）时重算画布
  useEffect(() => {
    const el = ref.current;
    if (!el || !ready || typeof ResizeObserver === 'undefined') return;
    const ro = new ResizeObserver(() => chartRef.current?.resize());
    ro.observe(el);
    return () => ro.disconnect();
  }, [ready]);

  if (error) {
    return (
      <div className="flex items-center justify-center text-xs text-muted-foreground" style={{ height }}>
        {error}
      </div>
    );
  }

  return <div ref={ref} style={{ height }} className="w-full" />;
}

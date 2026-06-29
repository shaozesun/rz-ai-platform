import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  PieChart,
  Pie,
  Cell,
} from 'recharts';

export interface TrendItem {
  day: string;
  date: string;
  对话: number;
  视频: number;
  隐患识别: number;
  消防配置: number;
}

interface DistItem {
  name: string;
  value: number;
  color: string;
}

const SERIES = [
  { key: '对话', color: 'var(--chart-1)' },
  { key: '视频', color: 'var(--chart-2)' },
  { key: '隐患识别', color: 'var(--chart-3)' },
  { key: '消防配置', color: 'var(--chart-4)' },
] as const;

function TooltipBox({ active, payload, label }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-lg border border-border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="mb-1 font-medium text-popover-foreground">{label}</p>
      {payload.map((p: any) => (
        <p key={p.name} className="flex items-center gap-2 text-muted-foreground">
          <span className="size-2 rounded-full" style={{ background: p.color }} />
          {p.name}：<span className="font-medium text-popover-foreground">{p.value.toLocaleString()}</span>
        </p>
      ))}
    </div>
  );
}

export function UsageAreaChart({ data }: { data: TrendItem[] }) {
  if (!data || data.length === 0) {
    return (
      <div className="flex h-[280px] items-center justify-center text-sm text-muted-foreground">
        暂无数据
      </div>
    );
  }
  return (
    <ResponsiveContainer width="100%" height={280}>
      <AreaChart data={data} margin={{ top: 10, right: 8, left: -16, bottom: 0 }}>
        <defs>
          {SERIES.map((s, i) => (
            <linearGradient key={s.key} id={`tg${i}`} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={s.color} stopOpacity={0.35} />
              <stop offset="100%" stopColor={s.color} stopOpacity={0} />
            </linearGradient>
          ))}
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
        <XAxis
          dataKey="day"
          stroke="var(--muted-foreground)"
          fontSize={12}
          tickLine={false}
          axisLine={false}
        />
        <YAxis stroke="var(--muted-foreground)" fontSize={12} tickLine={false} axisLine={false} />
        <Tooltip content={<TooltipBox />} />
        {SERIES.map((s, i) => (
          <Area
            key={s.key}
            type="monotone"
            dataKey={s.key}
            stroke={s.color}
            strokeWidth={2}
            fill={`url(#tg${i})`}
          />
        ))}
      </AreaChart>
    </ResponsiveContainer>
  );
}

export function DistributionChart({ data }: { data: DistItem[] }) {
  const filtered = data.filter((d) => d.value > 0);
  const total = filtered.reduce((sum, d) => sum + d.value, 0);
  if (total === 0) {
    return (
      <div className="flex h-[180px] items-center justify-center text-sm text-muted-foreground">
        暂无数据
      </div>
    );
  }
  return (
    <div className="flex flex-col items-center gap-4">
      <ResponsiveContainer width="100%" height={180}>
        <PieChart>
          <Pie
            data={filtered}
            dataKey="value"
            nameKey="name"
            innerRadius={55}
            outerRadius={80}
            paddingAngle={2}
            strokeWidth={0}
          >
            {filtered.map((d) => (
              <Cell key={d.name} fill={d.color} />
            ))}
          </Pie>
          <Tooltip content={<TooltipBox />} />
        </PieChart>
      </ResponsiveContainer>
      <ul className="flex w-full flex-col gap-2">
        {data.map((d) => (
          <li key={d.name} className="flex items-center gap-2 text-sm">
            <span className="size-2.5 rounded-full" style={{ background: d.color }} />
            <span className="flex-1 text-muted-foreground">{d.name}</span>
            <span className="font-medium">{total > 0 ? Math.round((d.value / total) * 100) : 0}%</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

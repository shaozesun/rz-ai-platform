import { useEffect, useRef, useState } from 'react';
import {
  ArrowLeft, ArrowUpRight, BarChart3, CheckCircle2, ClipboardList, Clock,
  Download, FileSpreadsheet, Loader2, LockKeyhole, Moon, RefreshCw, Repeat2,
  ShieldCheck, Sun, Timer, XCircle, type LucideIcon,
} from 'lucide-react';
import type { DateRange } from 'react-day-picker';
import { PlatformShell } from '@/components/platform-shell';
import { DocUpdatePanel } from '@/components/doc-update-panel';
import { InfraredQualityPanel } from '@/components/infrared-quality-panel';
import { RecordsTable } from '@/components/RecordsTable';
import { FullScreenOverlay } from '@/components/full-screen-overlay';
import { FilterSelect, type Opt } from '@/components/filter-select';
import { DateRangeFilter } from '@/components/date-range-filter';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  complianceSheetUrl, fetchComplianceArtifactBlob, fetchIncidentArtifactBlob,
  getComplianceRunSheets, getComplianceSpec, getIncidentPeriodSheets, incidentPeriodSheetUrl,
  listComplianceRuns, listIncidentPeriods,
  type ComplianceCheck, type ComplianceRun, type ComplianceRunSheets,
  type ComplianceSpec, type ComplianceSummaryItem,
  type IncidentParkRun, type IncidentPeriod, type IncidentPeriodSheets,
} from '@/api/scheduledTasks';

const RUN_LABEL: Record<string, string> = {
  pending: '排队中', running: '生成中', success: '已完成', failed: '失败',
};

const CADENCE_OPTS: Opt[] = [
  { value: '', label: '全部班次' }, { value: 'night', label: '夜班' },
  { value: 'day', label: '白班' },
];

const PAGE_SIZE = 20;

/** 本地日期 → `YYYY-MM-DD`。**不能用 `toISOString()`**：那是 UTC，东八区会平移到前一天。 */
function dayStr(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** 检查项视觉：名字/口径仍来自后端注册表，这里只按 key 给图标与色调，未知 key 走兜底。 */
interface CheckVisual { Icon: LucideIcon; chip: string; bar: string }

const CHECK_VISUALS: Record<string, CheckVisual> = {
  response_timeout: {
    Icon: Timer, chip: 'bg-warning/12 text-warning', bar: 'bg-warning',
  },
  unclosed_order: {
    Icon: ClipboardList, chip: 'bg-primary/10 text-primary', bar: 'bg-primary',
  },
  premature_close: {
    Icon: LockKeyhole, chip: 'bg-destructive/10 text-destructive', bar: 'bg-destructive',
  },
  repeat_alarm: {
    Icon: Repeat2, chip: 'bg-chart-2/15 text-chart-2', bar: 'bg-chart-2',
  },
};
const FALLBACK_VISUAL: CheckVisual = {
  Icon: ShieldCheck, chip: 'bg-muted text-muted-foreground', bar: 'bg-muted-foreground',
};
const visualOf = (key: string) => CHECK_VISUALS[key] ?? FALLBACK_VISUAL;

function fmtShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 期号 `2026-10-27-n` / `-d` → 班次类型、日期、窗口（从 period_label 的括号里取）。
 *
 * 不合规检查的 run 与事件统计表的期（`IncidentPeriod`）都只用到这三个字段，
 * 所以吃 duck type：两处必须用**同一套**判定，否则大卡与期卡会说两个班次。
 */
type ShiftLike = { period_key: string; period_label: string; triggered_at?: string };

function shiftMeta(run: ShiftLike) {
  const suffix = run.period_key.slice(-1);
  const kind = suffix === 'n' ? 'night' : suffix === 'd' ? 'day' : '';
  const m = run.period_label.match(/（(.+?)\s*~\s*(.+?)）/);
  return {
    kind,
    label: kind === 'night' ? '夜班' : kind === 'day' ? '白班' : '',
    day: run.period_key.slice(0, 10)
      || (run.triggered_at ? fmtShort(run.triggered_at).slice(0, 5) : ''),
    window: m ? `${m[1]} → ${m[2]}` : '',
  };
}

function totalOf(run: ComplianceRun): number | null {
  if (!run.check_counts) return null;
  return Object.values(run.check_counts).reduce((n, v) => n + (Number(v) || 0), 0);
}

function hasArtifact(run: ComplianceRun): boolean {
  return run.files.some((f) => f.endsWith('.xlsx'));
}

/** 触发下载：端点要 Bearer 头，`<a download>` 带不了，先 fetch 成 Blob。 */
async function saveWorkbook(run: ComplianceRun): Promise<void> {
  const name = run.files.find((f) => f.endsWith('.xlsx')) || run.files[0];
  if (!name) return;
  const blob = await fetchComplianceArtifactBlob(run.run_id, name);
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `${run.period_label || '合规检查'}.xlsx`;
  a.click();
  URL.revokeObjectURL(url);
}

function StatusBadge({ run }: { run: ComplianceRun }) {
  if (run.status === 'failed') {
    return <Badge variant="destructive"><XCircle className="size-3" />失败</Badge>;
  }
  if (run.status === 'pending' || run.status === 'running') {
    return <Badge variant="outline">{RUN_LABEL[run.status]}</Badge>;
  }
  if (!hasArtifact(run)) return <Badge variant="warning">无产物</Badge>;
  return <Badge variant="success"><CheckCircle2 className="size-3" />已完成</Badge>;
}

/** 四项命中的迷你条（卡片内）。计数缺失（失败/老记录）时不画。 */
function MiniBars({
  check, counts, max,
}: {
  check: ComplianceCheck;
  counts: Record<string, number> | undefined;
  max: number;
}) {
  const value = counts?.[check.key];
  const { Icon, bar } = visualOf(check.key);
  const pct = value ? Math.max(6, (value / max) * 100) : 0;
  return (
    <div className="flex items-center gap-2 text-xs">
      <Icon className="size-3.5 shrink-0 text-muted-foreground" />
      <span className="w-24 shrink-0 truncate text-muted-foreground">{check.name}</span>
      <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
        <span className={cn('block h-full rounded-full transition-all', bar)} style={{ width: `${pct}%` }} />
      </span>
      <span className={cn(
        'w-6 shrink-0 text-right tabular-nums',
        value ? 'text-foreground' : 'text-muted-foreground/60',
      )}>{value ?? '—'}</span>
    </div>
  );
}

/** 一期卡片。点击进看板；右下角单独下载。 */
function ShiftCard({
  run, checks, onOpen,
}: {
  run: ComplianceRun;
  checks: ComplianceCheck[];
  onOpen: (run: ComplianceRun) => void;
}) {
  const meta = shiftMeta(run);
  const counts = run.check_counts;
  const total = totalOf(run);
  const usable = total !== null;
  const max = counts ? Math.max(1, ...checks.map((c) => counts[c.key] ?? 0)) : 1;
  const clean = run.status === 'success' && total === 0;
  const ShiftIcon = meta.kind === 'night' ? Moon : Sun;

  return (
    <div
      role="button"
      tabIndex={0}
      onClick={() => onOpen(run)}
      onKeyDown={(e) => { if (e.key === 'Enter') onOpen(run); }}
      title={run.status === 'failed' ? (run.error || '生成失败') : `${run.period_label}｜${run.teaser}`}
      className={cn(
        'group flex cursor-pointer flex-col gap-2.5 rounded-xl border bg-card p-3.5 text-left shadow-sm transition-all hover:shadow-md',
        clean ? 'border-l-2 border-l-success' : 'border-border',
        'hover:border-primary/40',
        !hasArtifact(run) && !clean && 'border-dashed',
      )}
    >
      <div className="flex items-center gap-2">
        <span className={cn(
          'flex size-7 shrink-0 items-center justify-center rounded-lg',
          meta.kind === 'night' ? 'bg-primary/10 text-primary' : 'bg-warning/15 text-warning',
        )}>
          <ShiftIcon className="size-3.5" />
        </span>
        <span className="text-sm font-semibold">{meta.label}</span>
        <span className="text-xs tabular-nums text-muted-foreground">{meta.day}</span>
        <span className="ml-auto"><StatusBadge run={run} /></span>
      </div>

      {meta.window && (
        <p className="text-[11px] tabular-nums text-muted-foreground/80">{meta.window}</p>
      )}

      <div className="flex items-baseline gap-1.5">
        <span className={cn(
          'text-2xl font-semibold tracking-tight tabular-nums',
          clean ? 'text-success' : usable ? 'text-foreground' : 'text-muted-foreground/60',
        )}>{usable ? total : '—'}</span>
        <span className="text-xs text-muted-foreground">
          {usable ? (total === 0 ? '本班次未发现不合规项' : '条不合规') : '无命中数据'}
        </span>
      </div>

      {usable && checks.length > 0 && (
        <div className="space-y-1.5">
          {checks.map((c) => (
            <MiniBars key={c.key} check={c} counts={counts} max={max} />
          ))}
        </div>
      )}

      {!usable && run.error && (
        <p className="line-clamp-2 text-xs text-destructive/80">{run.error}</p>
      )}

      <div className="mt-auto flex items-center gap-2 border-t border-border pt-2 text-[11px] text-muted-foreground">
        <span className="tabular-nums">{fmtShort(run.triggered_at)}</span>
        {run.duration_ms > 0 && <span className="tabular-nums">· {(run.duration_ms / 1000).toFixed(0)}s</span>}
        <span className="ml-auto flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
          看板 <ArrowUpRight className="size-3.5" />
        </span>
        <Button
          size="xs"
          variant="ghost"
          className="-mr-1"
          disabled={!hasArtifact(run)}
          onClick={(e) => { e.stopPropagation(); void saveWorkbook(run); }}
          title="下载 Excel"
        >
          <Download className="size-3.5" />
        </Button>
      </div>
    </div>
  );
}

/** 事件统计表 xlsx：端点要 Bearer 头，先 fetch 成 Blob 再触发下载。 */
async function saveIncidentFiles(items: IncidentParkRun[]): Promise<void> {
  for (const item of items) {
    // 现在一期一份合并表（park === 'all'），历史期才是「一园区一份」才带园区名
    const suffix = item.park === 'all' ? '' : `_${item.park_label}`;
    for (const name of item.files) {
      const blob = await fetchIncidentArtifactBlob(item.run_id, name);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `数据中心事件统计表${suffix}${name.slice(name.lastIndexOf('.'))}`;
      a.click();
      URL.revokeObjectURL(url);
      // 浏览器对连续下载有节流：多园区「下载全部」时留出间隔，否则后面的会被吞掉
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
  }
}

/**
 * 顶部大卡：数据中心事件统计表（飞书园区群通报）最近一期。
 *
 * 它不是「不合规检查」的产出，而是同一值守视角的另一张表（全部园区 × 班次）。放在这里是
 * 因为值守每班先看这两张：事件统计表看「这一班通报了什么」，不合规检查看「班次里有哪些
 * 处置口径没做到」。没有产出时也保留这张卡，用说明文案代替数字（否则页面顶部会空一块）。
 */
function IncidentHero({
  period, onOpen,
}: {
  period: IncidentPeriod | null;
  onOpen: (period: IncidentPeriod) => void;
}) {
  const [busy, setBusy] = useState(false);

  if (!period) {
    return (
      <div className="rounded-2xl border border-dashed border-border bg-card/60 p-4 sm:p-5">
        <div className="flex items-center gap-2">
          <span className="flex size-8 items-center justify-center rounded-lg bg-chart-3/15 text-chart-3">
            <BarChart3 className="size-4" />
          </span>
          <span className="text-base font-semibold">数据中心事件统计表</span>
          <Badge variant="outline">尚无产出</Badge>
        </div>
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          以飞书园区群通报（速报 + 首报）为入口，关联告警与处置工单补全原因、进展与处理时长，
          全部园区合并成一张班次表（白班 / 夜班）。定时产出开启后每班结束（09:00 夜班 /
          18:00 白班）自动生成。
        </p>
      </div>
    );
  }

  // 班次类型与期卡同源（period_key 后缀），图标/色调也照期卡来，两处说的是同一班次
  const meta = shiftMeta(period);
  const Icon = meta.kind === 'night' ? Moon : meta.kind === 'day' ? Sun : BarChart3;
  const chip = meta.kind === 'night'
    ? 'bg-primary/10 text-primary'
    : meta.kind === 'day' ? 'bg-warning/15 text-warning' : 'bg-chart-3/15 text-chart-3';
  const title = `数据中心事件统计表${meta.label ? ` · ${meta.label}` : ''}`;
  const windowText = meta.window || period.window;

  const downloadable = period.parks.filter((p) => p.status === 'success' && p.files.length > 0);
  // 没有成功产物时后端合并接口会 404，卡片也就不该可点
  const canOpen = downloadable.length > 0;

  const downloadAll = async () => {
    if (!downloadable.length || busy) return;
    setBusy(true);
    try {
      await saveIncidentFiles(downloadable);
    } finally {
      setBusy(false);
    }
  };

  const open = () => onOpen(period);

  return (
    <div
      role={canOpen ? 'button' : undefined}
      tabIndex={canOpen ? 0 : undefined}
      onClick={canOpen ? open : undefined}
      onKeyDown={canOpen ? (e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
      } : undefined}
      className={cn(
        'rounded-2xl border border-border bg-card p-4 shadow-sm sm:p-5',
        canOpen && 'group cursor-pointer transition-all hover:border-primary/40 hover:shadow-md',
      )}
    >
      <div className="flex flex-col gap-5 xl:flex-row xl:items-center">
        <div className="min-w-0 flex-1 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn('flex size-8 items-center justify-center rounded-lg', chip)}>
              <Icon className="size-4" />
            </span>
            <span className="text-base font-semibold">{title}</span>
            <span className="text-sm tabular-nums text-muted-foreground">{meta.day}</span>
            {period.failed > 0 && <Badge variant="destructive">{period.failed} 个任务失败</Badge>}
          </div>
          {windowText && (
            <p className="text-xs tabular-nums text-muted-foreground/80">{windowText}</p>
          )}
          <p className="text-xs leading-relaxed text-muted-foreground">
            以园区群速报 / 首报为入口，关联告警与处置工单补全原因、进展与处理时长
          </p>
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
            <span className="text-4xl font-semibold tracking-tight tabular-nums">
              {period.total_count}
            </span>
            <span className="text-sm text-muted-foreground">条已通报事件</span>
          </div>
        </div>

        <div className="flex shrink-0 gap-2 xl:flex-col">
          {canOpen && (
            <span className="hidden items-center justify-end gap-0.5 text-[11px] text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100 xl:flex">
              查看明细 <ArrowUpRight className="size-3.5" />
            </span>
          )}
          <Button size="sm" disabled={!downloadable.length || busy}
            onClick={(e) => { e.stopPropagation(); void downloadAll(); }}>
            {busy ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
            下载本期全部
          </Button>
        </div>
      </div>
    </div>
  );
}

/** 本期构成：四项占比堆叠条 + 图例。全 0 时换成绿色结论。 */
function Composition({ summary }: { summary: ComplianceSummaryItem[] }) {
  const total = summary.reduce((n, s) => n + s.rows, 0);
  if (!total) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-1 py-6">
        <CheckCircle2 className="size-7 text-success/70" />
        <p className="text-sm text-muted-foreground">本班次未发现不合规项</p>
      </div>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-muted">
        {summary.filter((s) => s.rows > 0).map((s) => (
          <span
            key={s.key}
            className={visualOf(s.key).bar}
            style={{ width: `${(s.rows / total) * 100}%` }}
            title={`${s.name} ${s.rows} 条`}
          />
        ))}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
        {summary.map((s) => (
          <div key={s.key} className="flex items-center gap-1.5 text-[11px]">
            <span className={cn('size-2 shrink-0 rounded-full', visualOf(s.key).bar)} />
            <span className="truncate text-muted-foreground">{s.name}</span>
            <span className="ml-auto tabular-nums">{s.rows}</span>
            <span className="w-9 shrink-0 text-right tabular-nums text-muted-foreground/70">
              {Math.round((s.rows / total) * 100)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 近 N 期总命中数（数据来自已加载的列表，不足 2 期不画）。 */
function TrendBars({ history, runId }: { history: ComplianceRun[]; runId: string }) {
  const pts = [...history]
    .reverse()
    .filter((r) => r.check_counts)
    .slice(-10)
    .map((r) => ({ run: r, value: totalOf(r) ?? 0 }));
  if (pts.length < 2) {
    return <p className="py-6 text-center text-xs text-muted-foreground">期数不足，暂无法对比</p>;
  }
  const max = Math.max(1, ...pts.map((p) => p.value));
  return (
    <div className="flex h-28 items-end gap-1.5 pt-2">
      {pts.map(({ run, value }) => {
        const cur = run.run_id === runId;
        return (
          <div key={run.run_id}
            className="group flex min-w-0 flex-1 flex-col items-center justify-end gap-1"
            title={`${run.period_label || run.period_key}｜${value} 条`}>
            <span className={cn(
              'text-[10px] tabular-nums',
              cur ? 'font-semibold text-foreground' : 'text-muted-foreground/60',
            )}>{value}</span>
            <span
              className={cn(
                'w-full rounded-t transition-colors',
                cur ? 'bg-primary' : 'bg-primary/25 group-hover:bg-primary/45',
              )}
              style={{ height: `${Math.max(3, (value / max) * 64)}px` }}
            />
            <span className={cn(
              'truncate text-[9px] tabular-nums',
              cur ? 'text-foreground' : 'text-muted-foreground/60',
            )}>{run.period_key.slice(5, 10)}</span>
          </div>
        );
      })}
    </div>
  );
}

/** 较上一同类班次的增减（列表是最新在前，取早于本期的第一条）。 */
function prevCount(history: ComplianceRun[], run: ComplianceRun, key: string): number | undefined {
  const suffix = run.period_key.slice(-1);
  const prev = history.find((r) => r.period_key < run.period_key
    && r.period_key.endsWith(`-${suffix}`) && r.check_counts);
  return prev?.check_counts?.[key];
}

/** 看板详情：整屏（对齐分析报告），指标卡带 + 构成 + 趋势 + 明细。 */
function RunBoard({
  run, data, history, loading, error, activeKey, onActive, onClose,
}: {
  run: ComplianceRun;
  data: ComplianceRunSheets | null;
  history: ComplianceRun[];
  loading: boolean;
  error: string;
  activeKey: string;
  onActive: (key: string) => void;
  onClose: () => void;
}) {
  const active = data?.summary.find((s) => s.key === activeKey);
  const meta = shiftMeta(run);

  return (
    <FullScreenOverlay>
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5">
        <Button size="sm" variant="ghost" onClick={onClose}>
          <ArrowLeft className="size-4" /> 返回目录
        </Button>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {run.period_label || run.period_key}
          {meta.window && <span className="ml-2 text-xs text-muted-foreground">{meta.window}</span>}
        </span>
        <Button size="sm" variant="outline" disabled={!hasArtifact(run)}
          onClick={() => void saveWorkbook(run)}>
          <Download className="size-4" /> 下载 Excel
        </Button>
      </div>

      {loading ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2">
          <XCircle className="size-8 text-destructive/70" />
          <p className="text-sm text-destructive">{error}</p>
        </div>
      ) : !data ? null : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto max-w-6xl space-y-3 p-3 sm:p-4">
            {/* 指标卡带：命中数 + 口径 + 较上一班增减，点击切明细 */}
            <div className="grid grid-cols-2 gap-2 xl:grid-cols-4">
              {data.summary.map((s) => {
                const { Icon, chip } = visualOf(s.key);
                const prev = prevCount(history, run, s.key);
                const delta = prev === undefined ? null : s.rows - prev;
                const selected = s.key === activeKey;
                return (
                  <button
                    key={s.key}
                    type="button"
                    onClick={() => onActive(s.key)}
                    className={cn(
                      'rounded-xl border bg-card p-3 text-left transition-colors',
                      selected
                        ? 'border-primary/50 ring-1 ring-primary/20'
                        : 'border-border hover:border-primary/30',
                    )}
                  >
                    <div className="flex items-center gap-2">
                      <span className={cn('flex size-7 shrink-0 items-center justify-center rounded-lg', chip)}>
                        <Icon className="size-3.5" />
                      </span>
                      <span className="truncate text-xs text-muted-foreground">{s.name}</span>
                    </div>
                    <div className="mt-2 flex items-end gap-2">
                      <span className={cn(
                        'text-2xl font-semibold leading-none tracking-tight tabular-nums',
                        s.rows === 0 && 'text-success',
                      )}>{s.rows}</span>
                      <span className="pb-0.5 text-[11px] text-muted-foreground">条</span>
                      {delta !== null && (
                        <span
                          title="较上一同类班次"
                          className={cn(
                            'ml-auto pb-0.5 text-[11px] tabular-nums',
                            delta > 0 ? 'text-destructive'
                              : delta < 0 ? 'text-success' : 'text-muted-foreground/70',
                          )}
                        >
                          {delta === 0 ? '持平' : `${delta > 0 ? '↑' : '↓'}${Math.abs(delta)}`}
                        </span>
                      )}
                    </div>
                    <p className="mt-1.5 line-clamp-2 text-[11px] leading-relaxed text-muted-foreground/80">
                      {s.description}
                    </p>
                  </button>
                );
              })}
            </div>

            <div className="grid gap-3 lg:grid-cols-2">
              <Card>
                <CardContent className="p-4">
                  <p className="mb-3 text-xs font-medium text-muted-foreground">本期构成</p>
                  <Composition summary={data.summary} />
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-4">
                  <p className="mb-1 text-xs font-medium text-muted-foreground">近 10 期总命中数</p>
                  <TrendBars history={history} runId={run.run_id} />
                </CardContent>
              </Card>
            </div>

            <div className="flex flex-wrap items-center gap-1 pt-1">
              {data.summary.map((s) => {
                const { Icon } = visualOf(s.key);
                return (
                  <button
                    key={s.key}
                    type="button"
                    onClick={() => onActive(s.key)}
                    className={cn(
                      'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors',
                      s.key === activeKey
                        ? 'border-primary/40 bg-primary/5 text-foreground'
                        : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
                    )}
                  >
                    <Icon className="size-3.5" />
                    {s.name}
                    <span className="tabular-nums opacity-70">{s.rows}</span>
                  </button>
                );
              })}
            </div>

            <Card className="overflow-hidden">
              <div className="h-[52vh] min-h-[320px]">
                {active && active.rows === 0 ? (
                  <div className="flex h-full flex-col items-center justify-center gap-1.5">
                    <CheckCircle2 className="size-7 text-success/70" />
                    <p className="text-sm text-muted-foreground">本班次未发现该项不合规</p>
                    <p className="max-w-lg px-6 text-center text-xs text-muted-foreground/80">
                      {active.description}
                    </p>
                  </div>
                ) : (
                  <RecordsTable
                    key={activeKey}
                    url={complianceSheetUrl(run.run_id, activeKey)}
                    name={active?.name ?? activeKey}
                  />
                )}
              </div>
            </Card>
          </div>
        </div>
      )}
    </FullScreenOverlay>
  );
}

/**
 * 事件统计表整期看板：当期所有园区的行合并成一张表（**不按园区切换** —— 这张表就是
 * 这一班/这一周全园区的事件清单），列与生成的 xlsx 完全一致。
 */
function IncidentBoard({ period, onClose }: { period: IncidentPeriod; onClose: () => void }) {
  const [data, setData] = useState<IncidentPeriodSheets | null>(null);
  const [active, setActive] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError('');
    getIncidentPeriodSheets(period.period_key)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setActive((cur) => cur || d.sheets[0]?.name || '');
      })
      .catch((e) => { if (alive) setError((e as Error).message); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [period.period_key]);

  const downloadable = period.parks.filter((p) => p.status === 'success' && p.files.length > 0);
  const meta = shiftMeta(period);
  const windowText = meta.window || period.window;

  const downloadAll = async () => {
    if (!downloadable.length || busy) return;
    setBusy(true);
    try {
      await saveIncidentFiles(downloadable);
    } finally {
      setBusy(false);
    }
  };

  return (
    <FullScreenOverlay>
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5">
        <Button size="sm" variant="ghost" onClick={onClose}>
          <ArrowLeft className="size-4" /> 返回目录
        </Button>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {`数据中心事件统计表 · ${period.period_label || period.period_key}`}
          {windowText && <span className="ml-2 text-xs text-muted-foreground">{windowText}</span>}
        </span>
        {period.failed > 0 && <Badge variant="destructive">{period.failed} 个任务失败</Badge>}
        <Button size="sm" variant="outline"
          disabled={!downloadable.length || busy || data?.available === false}
          onClick={() => void downloadAll()}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : <Download className="size-4" />}
          下载 Excel
        </Button>
      </div>

      {loading ? (
        <div className="flex flex-1 items-center justify-center">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2">
          <XCircle className="size-8 text-destructive/70" />
          <p className="text-sm text-destructive">{error}</p>
        </div>
      ) : !data ? null : data.sheets.length === 0 ? (
        <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center">
          <FileSpreadsheet className="size-8 text-muted-foreground/50" />
          <p className="max-w-md text-sm text-muted-foreground">
            {data.reason || '该期暂无可预览的明细'}
          </p>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 flex-col gap-3 p-3 sm:p-4">
          <div className="flex flex-wrap items-center gap-1">
            {data.sheets.map((s) => (
              <button
                key={s.key}
                type="button"
                onClick={() => setActive(s.name)}
                className={cn(
                  'flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-sm transition-colors',
                  s.name === active
                    ? 'border-primary/40 bg-primary/5 text-foreground'
                    : 'border-border text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
              >
                {s.name}
                <span className="tabular-nums opacity-70">{s.rows}</span>
              </button>
            ))}
          </div>

          <Card className="min-h-0 flex-1 overflow-hidden">
            {active && (
              <RecordsTable
                key={`${period.period_key}:${active}`}
                url={incidentPeriodSheetUrl(period.period_key, active)}
                name={active}
                adaptive
              />
            )}
          </Card>
        </div>
      )}
    </FullScreenOverlay>
  );
}

/** 无产出时展示四个检查项的口径（注册表驱动，各有自己的图标与色条）。 */
function CheckSpecs({ checks }: { checks: ComplianceCheck[] }) {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {checks.map((c) => {
        const { Icon, chip } = visualOf(c.key);
        return (
          <div key={c.key} className="rounded-xl border border-border bg-card p-3.5">
            <div className="flex items-center gap-2">
              <span className={cn('flex size-8 shrink-0 items-center justify-center rounded-lg', chip)}>
                <Icon className="size-4" />
              </span>
              <span className="truncate text-sm font-semibold">{c.name}</span>
              <Badge variant="outline" className="ml-auto shrink-0">{c.cadence}</Badge>
            </div>
            <p className="mt-2 text-xs leading-relaxed text-muted-foreground">{c.description}</p>
          </div>
        );
      })}
    </div>
  );
}

export default function CompliancePage() {
  const [spec, setSpec] = useState<ComplianceSpec | null>(null);
  const [runs, setRuns] = useState<ComplianceRun[]>([]);
  const [incident, setIncident] = useState<IncidentPeriod | null>(null);
  const [incidentDetail, setIncidentDetail] = useState<IncidentPeriod | null>(null);
  const [total, setTotal] = useState(0);
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const [page, setPage] = useState(1);
  const [range, setRange] = useState<DateRange | undefined>();
  const [cadence, setCadence] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<{
    run: ComplianceRun; data: ComplianceRunSheets | null; loading: boolean;
    error: string; activeKey: string;
  } | null>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const checks = spec?.checks ?? [];
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const load = async (pageNo: number) => {
    setLoading(true);
    setError('');
    try {
      const data = await listComplianceRuns({
        offset: (pageNo - 1) * PAGE_SIZE,
        limit: PAGE_SIZE,
        cadence,
        start: range?.from ? dayStr(range.from) : '',
        end: range?.to ? dayStr(range.to) : '',
      });
      setRuns(data.runs);
      setTotal(data.total);
      setStatusCounts(data.statusCounts);
      setPage(pageNo);
    } catch {
      setError('加载失败，请刷新重试');
    } finally {
      setLoading(false);
    }
  };

  // 事件统计表与本页产物互不依赖：任一失败都不能让另一块变成空白
  const loadIncident = async () => {
    try {
      const periods = await listIncidentPeriods(1);
      setIncident(periods[0] ?? null);
    } catch {
      setIncident(null);
    }
  };

  useEffect(() => {
    // 检查项注册表与产物清单互不依赖：注册表失败不能让页面变成空白
    getComplianceSpec().then(setSpec).catch(() => setSpec(null));
    void loadIncident();
  }, []);

  useEffect(() => { void load(1); }, [range, cadence]);

  const openRun = async (run: ComplianceRun, key = '') => {
    setDetail({ run, data: null, loading: true, error: '', activeKey: key });
    try {
      const data = await getComplianceRunSheets(run.run_id);
      setDetail((cur) => (cur ? {
        ...cur, data, loading: false,
        activeKey: key || data.sheets[0]?.key || '',
      } : cur));
    } catch (e) {
      setDetail((cur) => (cur ? { ...cur, loading: false, error: (e as Error).message } : cur));
    }
  };

  const goPage = (p: number) => {
    const target = Math.min(Math.max(1, p), totalPages);
    if (target === page || loading) return;
    void load(target);
    listRef.current?.scrollIntoView({ block: 'start' });
  };

  if (detail) {
    return (
      <RunBoard
        run={detail.run}
        data={detail.data}
        history={runs}
        loading={detail.loading}
        error={detail.error}
        activeKey={detail.activeKey}
        onActive={(key) => setDetail((cur) => (cur ? { ...cur, activeKey: key } : cur))}
        onClose={() => setDetail(null)}
      />
    );
  }

  if (incidentDetail) {
    return (
      <IncidentBoard
        period={incidentDetail}
        onClose={() => setIncidentDetail(null)}
      />
    );
  }

  const hasCounts = Object.keys(statusCounts).length > 0;
  const succeeded = hasCounts ? (statusCounts.success ?? 0)
    : runs.filter((r) => r.status === 'success').length;
  const failed = hasCounts ? (statusCounts.failed ?? 0)
    : runs.filter((r) => r.status === 'failed').length;

  return (
    <PlatformShell
      title="合规检查"
      description="按班次核对告警处置不合规与园区事件统计，按月度跟踪平台资料更新，按需检测红外巡检图片质量"
      actions={(
        <Button size="sm" variant="ghost"
          onClick={() => { void load(page); void loadIncident(); }}>
          <RefreshCw className="size-4" /> 刷新
        </Button>
      )}
    >
      <div className="space-y-4" ref={listRef}>
        <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card p-2 shadow-sm">
          <DateRangeFilter value={range} onChange={setRange} />
          <FilterSelect
            icon={Clock} ariaLabel="按班次筛选"
            value={cadence} options={CADENCE_OPTS}
            onChange={(v) => setCadence(String(v))}
          />
          <span className="ml-auto px-1 text-xs tabular-nums text-muted-foreground">
            共 {total} 期
          </span>
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}

        {loading ? (
          <div className="flex justify-center py-20">
            <Loader2 className="size-6 animate-spin text-muted-foreground" />
          </div>
        ) : runs.length === 0 ? (
          <div className="space-y-4">
            <DocUpdatePanel />
            <IncidentHero period={incident} onOpen={setIncidentDetail} />
            <InfraredQualityPanel />
            <Card>
              <CardContent className="flex flex-col items-center gap-2 py-14">
                <FileSpreadsheet className="size-8 text-muted-foreground/50" />
                <p className="text-sm text-muted-foreground">
                  {range || cadence ? '该筛选条件下没有产出' : '还没有产出'}
                </p>
                <p className="text-xs text-muted-foreground/80">
                  {range || cadence
                    ? '换个日期区间或班次看看'
                    : '本功能尚未开启定时产出，开启后每个班次（09:00 / 18:00）自动生成'}
                </p>
              </CardContent>
            </Card>
            {checks.length > 0 && <CheckSpecs checks={checks} />}
          </div>
        ) : (
          <div className="space-y-4">
            <DocUpdatePanel />
            <IncidentHero period={incident} onOpen={setIncidentDetail} />
            <InfraredQualityPanel />

            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
              {runs.map((r) => (
                <ShiftCard key={r.run_id} run={r} checks={checks} onOpen={(x) => void openRun(x)} />
              ))}
            </div>

            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
              <div className="flex items-center gap-3 text-xs text-muted-foreground">
                <span>共 <b className="text-foreground tabular-nums">{total}</b> 期产出</span>
                <span className="flex items-center gap-2">
                  <span className="flex items-center gap-1">
                    <CheckCircle2 className="size-3.5 text-success" />{succeeded}
                  </span>
                  {failed > 0 && (
                    <span className="flex items-center gap-1">
                      <XCircle className="size-3.5 text-destructive" />{failed}
                    </span>
                  )}
                </span>
              </div>
              {totalPages > 1 && (
                <div className="flex items-center gap-1">
                  <Button size="xs" variant="outline" disabled={page <= 1 || loading}
                    onClick={() => goPage(page - 1)}>上一页</Button>
                  <span className="px-2 text-xs tabular-nums text-muted-foreground">
                    {page} / {totalPages}
                  </span>
                  <Button size="xs" variant="outline" disabled={page >= totalPages || loading}
                    onClick={() => goPage(page + 1)}>下一页</Button>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </PlatformShell>
  );
}

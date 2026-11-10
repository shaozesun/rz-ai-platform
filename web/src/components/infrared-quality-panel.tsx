import { useCallback, useEffect, useState } from 'react';
import { ArrowUpRight, Loader2, RefreshCw, ScanEye } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { InfraredQualityBoard } from '@/components/infrared-quality-board';
import {
  getInfraredQualityTasks,
  type InfraredQualityCounts,
  type InfraredQualityOverview,
} from '@/api/scheduledTasks';

/** 「已检测/未检测」以任务文档为准；轮询只在有租约时进行，后端释放即停。 */
const POLL_INTERVAL = 3000;

const COUNT_ORDER: { key: keyof InfraredQualityCounts; label: string; cls: string }[] = [
  { key: '合格', label: '合格', cls: 'text-success' },
  { key: '待复核', label: '待复核', cls: 'text-warning' },
  { key: '不合格', label: '不合格', cls: 'text-destructive' },
  { key: '检测失败', label: '检测失败', cls: 'text-muted-foreground' },
];

function CountChips({ counts }: { counts: InfraredQualityCounts }) {
  return (
    <>
      {COUNT_ORDER.map(({ key, label, cls }) => (
        <span key={key} className={cn('inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px]', cls)}>
          <span className="size-1.5 rounded-full bg-current opacity-70" />
          {label}
          <b className="tabular-nums">{counts[key] ?? 0}</b>
        </span>
      ))}
    </>
  );
}

export function InfraredQualityPanel() {
  const [data, setData] = useState<InfraredQualityOverview | null>(null);
  const [deptId, setDeptId] = useState('');
  const [open, setOpen] = useState(false);
  const [notice, setNotice] = useState('');

  const load = useCallback(async (refresh = false) => {
    try {
      const next = await getInfraredQualityTasks(deptId, refresh);
      setData(next);
      if (!deptId && next.dept?.dept_id) setDeptId(next.dept.dept_id);
    } catch {
      // 面板是附加信息，取不到不打断合规页
    }
  }, [deptId]);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    if (!data?.running) return;
    const timer = window.setInterval(() => { void load(); }, POLL_INTERVAL);
    return () => window.clearInterval(timer);
  }, [data?.running, load]);

  if (!data?.enabled) return null;

  const deptLabel = data.dept
    ? [data.dept.company_name, data.dept.dept_name].filter(Boolean).join(' · ')
    : '';
  const pending = data.tasks.filter((t) => !t.checked_at && t.status !== 1).length;
  const running = data.running;
  const progress = data.progress;
  const pct = progress && progress.total > 0
    ? Math.round((progress.done / progress.total) * 100) : 0;
  const latest = data.latest_run;

  return (
    <>
      <div
        role="button"
        tabIndex={0}
        onClick={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        className="group cursor-pointer rounded-2xl border border-border bg-card p-4 shadow-sm transition-all hover:border-primary/40 hover:shadow-md sm:p-5"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex size-8 items-center justify-center rounded-lg bg-chart-1/15 text-chart-1">
            <ScanEye className="size-4" />
          </span>
          <span className="text-base font-semibold">红外巡检图片质量</span>
          <Badge variant="outline">按任务检测</Badge>
          <span className="text-xs tabular-nums text-muted-foreground">
            {data.tasks.length} 个任务 · 待检测 {pending}
          </span>
          <div className="ml-auto flex items-center gap-1">
            <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); setOpen(true); }}>
              查看全部 <ArrowUpRight className="size-4" />
            </Button>
            <Button size="sm" variant="outline" disabled={running}
              onClick={(e) => { e.stopPropagation(); void load(true); }}>
              <RefreshCw className="size-4" /> 刷新任务
            </Button>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          {deptLabel && (
            <span className="text-xs text-muted-foreground">{deptLabel}</span>
          )}
          {latest ? (
            <>
              <span className="text-xs text-muted-foreground">
                {latest.status === 'failed' ? '最近一次检测失败' : '最近一次检测'}
              </span>
              <CountChips counts={latest.counts} />
            </>
          ) : (
            <span className="text-xs text-muted-foreground">尚未检测过任何任务</span>
          )}
        </div>

        {latest?.error && !running && (
          <p className="mt-2 text-[11px] leading-relaxed text-destructive/80">{latest.error}</p>
        )}

        {running ? (
          <p className="mt-2 flex items-center gap-1 text-[11px] text-muted-foreground">
            <Loader2 className="size-3.5 animate-spin" />
            {progress && progress.total > 0
              ? `${progress.phase === 'predict' ? '正在推理' : '正在取任务详情'} · ${pct}%`
              : '正在启动检测'}
            {progress?.current ? ` · ${progress.current}` : ''}
          </p>
        ) : (
          <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground/80">
            选择巡检任务后逐张检查红外图片清晰度：sharp 为合格、borderline 待复核、
            defocused 为不合格，其余（uncertain 等）一律按检测失败。已检测的任务默认不再勾选。
          </p>
        )}

        {notice && <p className="mt-2 text-xs text-muted-foreground">{notice}</p>}
      </div>

      {open && (
        <InfraredQualityBoard
          data={data}
          deptId={deptId}
          onDeptChange={setDeptId}
          running={running}
          onClose={() => setOpen(false)}
          onRefresh={() => void load(true)}
          onNotice={setNotice}
        />
      )}
    </>
  );
}

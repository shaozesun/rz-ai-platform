import { useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  ArrowLeft, ChevronLeft, ChevronRight, ImageOff, Loader2, ScanEye,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { FilterSelect } from '@/components/filter-select';
import { RecordsTable } from '@/components/RecordsTable';
import { FullScreenOverlay } from '@/components/full-screen-overlay';
import { cn } from '@/lib/utils';
import {
  infraredQualityRecordsUrl, triggerInfraredQualityCheck,
  type InfraredQualityCounts, type InfraredQualityOverview, type InfraredQualityProgress,
  type InfraredQualityTask,
} from '@/api/scheduledTasks';

const pad = (n: number) => String(n).padStart(2, '0');

const TASK_PAGE_SIZE = 20;

/** 结果表里按图片渲染的列（检测时本地留存的图，走平台端点；老记录回退原始预签名地址）。 */
const IMAGE_COLUMNS = ['图片链接'];

function fmtMs(ms: number): string {
  if (!ms) return '—';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
    + `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtIso(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return fmtMs(d.getTime());
}

type Filter = 'all' | 'unchecked' | 'checked';

const FILTER_LABEL: Record<Filter, string> = {
  all: '全部', unchecked: '未检测', checked: '已检测',
};

/** 四档判定：顺序、文案、色点只在这里定义，结果列与 KPI 分布卡共用。 */
const COUNT_ORDER = [
  { key: '合格', label: '合格', dot: 'bg-success', text: 'text-success' },
  { key: '待复核', label: '待复核', dot: 'bg-warning', text: 'text-warning' },
  { key: '不合格', label: '不合格', dot: 'bg-destructive', text: 'text-destructive' },
  { key: '检测失败', label: '失败', dot: 'bg-muted-foreground/50', text: 'text-muted-foreground' },
] as const satisfies readonly {
  key: keyof InfraredQualityCounts;
  label: string;
  dot: string;
  text: string;
}[];

function sumCounts(counts: InfraredQualityCounts): number {
  return COUNT_ORDER.reduce((n, item) => n + (counts[item.key] ?? 0), 0);
}

/** 一行四档计数：色点 + 数值，避免 2×2 小网格把行高撑乱。 */
function ResultCell({ task }: { task: InfraredQualityTask }) {
  if (!task.checked_at) {
    return <span className="text-xs text-muted-foreground">未检测</span>;
  }
  if (task.result_status === 'no_image') {
    return <Badge variant="outline" className="text-xs">无巡检图片</Badge>;
  }
  const c = task.counts;
  return (
    <span className="flex flex-col gap-1">
      <span className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs tabular-nums">
        {COUNT_ORDER.map(({ key, label, dot, text }) => (
          <span key={key} className={cn('inline-flex items-center gap-1', text)}>
            <span className={cn('size-1.5 rounded-full', dot)} />
            {label} {c[key] ?? 0}
          </span>
        ))}
      </span>
      {task.result_status === 'failed' && (
        <Badge variant="destructive" className="w-fit text-xs">检测失败</Badge>
      )}
      {task.result_status === 'partial' && (
        <Badge variant="outline" className="w-fit text-xs">部分中断</Badge>
      )}
    </span>
  );
}

/** KPI 卡：标签 + 大数字 + 脚注；分布卡用 children 自定义下半部分。 */
function StatTile({
  label, value, hint, accent, children,
}: {
  label: string; value?: string | number; hint?: ReactNode; accent?: string; children?: ReactNode;
}) {
  return (
    <div className="flex min-h-[96px] flex-col rounded-xl border border-border/70 bg-card px-4 py-3">
      <p className="text-xs text-muted-foreground">{label}</p>
      {value !== undefined && (
        <p className={cn(
          'mt-1.5 text-xl font-semibold leading-none tracking-tight tabular-nums',
          accent,
        )}>{value}</p>
      )}
      {hint && <p className="mt-1.5 text-xs leading-relaxed text-muted-foreground/80">{hint}</p>}
      {children}
    </div>
  );
}

/** 最近一次检测的四档构成：2px 堆叠条 + 2×2 图例。 */
function ResultDistribution({ counts }: { counts: InfraredQualityCounts }) {
  const total = sumCounts(counts);
  return (
    <div className="mt-auto space-y-2 pt-2">
      <div className="flex h-1.5 overflow-hidden rounded-full bg-muted">
        {total > 0 && COUNT_ORDER.map(({ key, dot }) => {
          const value = counts[key] ?? 0;
          if (!value) return null;
          return (
            <span
              key={key}
              className={cn('h-full', dot)}
              style={{ width: `${(value / total) * 100}%` }}
            />
          );
        })}
      </div>
      <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
        {COUNT_ORDER.map(({ key, label, dot, text }) => (
          <span key={key} className="flex items-center gap-1">
            <span className={cn('size-1.5 shrink-0 rounded-full', dot)} />
            <span className="truncate text-muted-foreground">{label}</span>
            <span className={cn('ml-auto tabular-nums', text)}>{counts[key] ?? 0}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/** 详情页头部的一项元信息：标签弱化、值用正文色。 */
function Meta({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1 whitespace-nowrap">
      <span className="text-muted-foreground/70">{label}</span>
      <span className="text-foreground">{value}</span>
    </span>
  );
}

function RunProgress({ progress }: { progress: InfraredQualityProgress | null }) {
  if (!progress) return null;
  const total = progress.total;
  const done = Math.min(progress.done, total || progress.done);
  const pct = total > 0 ? Math.round((done / total) * 100) : 0;
  const phaseText = progress.phase === 'predict'
    ? '正在推理检测'
    : progress.phase === 'detail' ? '正在读取巡检任务' : '检测中';
  return (
    <div className="shrink-0 rounded-xl border border-warning/30 bg-warning/10 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <Loader2 className="size-4 animate-spin text-warning" />
        <span>{phaseText}</span>
        {progress.current && (
          <span className="truncate text-muted-foreground">{progress.current}</span>
        )}
        <span className="ml-auto tabular-nums text-muted-foreground">
          {total > 0 ? `${pct}%（${done}/${total}）` : '准备中'}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-warning/20">
        <div
          className="h-full rounded-full bg-warning transition-[width]"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

/** 全屏看板：勾任务 → 检测 → 看明细。任务列表与明细不同屏，点任务行进入明细、可返回。 */
export function InfraredQualityBoard({
  data, deptId, onDeptChange, running, onClose, onRefresh, onNotice,
}: {
  data: InfraredQualityOverview;
  deptId: string;
  onDeptChange: (id: string) => void;
  running: boolean;
  onClose: () => void;
  onRefresh: () => void;
  onNotice: (text: string) => void;
}) {
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [filter, setFilter] = useState<Filter>('all');
  const [refreshPred, setRefreshPred] = useState(false);
  const [starting, setStarting] = useState(false);
  const [page, setPage] = useState(1);
  // 空 = 任务列表；点任务行或「本次检测明细」进入详情，同一时间只显示一份
  const [view, setView] = useState<{ runId?: string; taskId?: string }>({});
  useEffect(() => { setSelected(new Set()); }, [deptId]);
  useEffect(() => { setPage(1); }, [deptId, filter]);

  const latestRunId = data.latest_run?.run_id ?? '';

  const deptOptions = data.departments.map((d) => ({
    value: d.dept_id,
    label: [d.company_name, d.dept_name].filter(Boolean).join(' · '),
  }));

  const tasks = useMemo(() => {
    return data.tasks.filter((t) => {
      if (filter === 'unchecked' && t.checked_at) return false;
      if (filter === 'checked' && !t.checked_at) return false;
      return true;
    });
  }, [data.tasks, filter]);

  const totalPages = Math.max(1, Math.ceil(tasks.length / TASK_PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);
  const pageTasks = tasks.slice(
    (currentPage - 1) * TASK_PAGE_SIZE,
    currentPage * TASK_PAGE_SIZE,
  );

  const unchecked = data.tasks.filter((t) => !t.checked_at && t.status !== 1);
  const checkedCount = data.tasks.filter((t) => t.checked_at).length;
  const canStart = Boolean(deptId) && selected.size > 0 && !running && !starting;

  const viewTask = view.taskId ? data.tasks.find((t) => t.task_id === view.taskId) : undefined;
  const viewRun = view.runId && data.latest_run?.run_id === view.runId ? data.latest_run : null;

  const toggle = (taskId: string) => {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  const selectUnchecked = () => {
    setSelected(new Set(unchecked.map((t) => t.task_id)));
  };

  const start = async () => {
    if (!canStart) return;
    setStarting(true);
    onNotice('');
    try {
      const res = await triggerInfraredQualityCheck({
        dept_id: deptId, task_ids: [...selected], refresh: refreshPred,
      });
      if (!res.started) {
        onNotice(res.reason || '已有一次检测在跑，请稍候');
      }
    } catch {
      onNotice('启动检测失败，请稍后重试');
    } finally {
      setStarting(false);
      onRefresh();
    }
  };

  const recordUrl = view.runId
    ? infraredQualityRecordsUrl({ runId: view.runId })
    : view.taskId
      ? infraredQualityRecordsUrl({ taskId: view.taskId })
      : '';
  // 列表与明细二选一：有 view 就只显示明细，不再同屏并排
  const inDetail = Boolean(view.runId || view.taskId);
  const latest = data.latest_run;

  return (
    <FullScreenOverlay>
      {/* 页头：返回与标题同一行，说明单独一行，只有两行 */}
      <header className="shrink-0 border-b border-border bg-card px-4 py-3 sm:px-6">
        <div className="flex items-center gap-2">
          <Button
            size="icon-sm"
            variant="ghost"
            className="-ml-1.5"
            onClick={onClose}
            aria-label="返回目录"
          >
            <ArrowLeft className="size-4" />
          </Button>
          <h1 className="text-lg font-semibold leading-tight">红外巡检图片质量</h1>
          {running && (
            <Badge variant="warning">
              <Loader2 className="size-3 animate-spin" />检测中
            </Badge>
          )}
        </div>
        <p className="mt-0.5 pl-8 text-xs text-muted-foreground">
          sharp 为合格 · borderline 待复核 · defocused 为不合格 · 其余（uncertain 等）一律按检测失败
        </p>
      </header>

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden px-4 py-4 sm:px-6">
        {inDetail ? (
          <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-card">
            <div className="flex min-h-12 shrink-0 flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-4 py-2">
              <Button size="sm" variant="ghost" className="-ml-2" onClick={() => setView({})}>
                <ArrowLeft className="size-4" /> 任务列表
              </Button>
              <span className="h-4 w-px bg-border" />
              <span className="text-sm font-semibold">
                {view.runId ? '本次检测明细' : '任务明细'}
              </span>
              <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                {viewTask && (
                  <>
                    <Meta label="巡检人" value={viewTask.inspector || '—'} />
                    <Meta label="巡检时间" value={fmtMs(viewTask.end_time || viewTask.start_time)} />
                    <Meta label="设备" value={`${viewTask.device_count} 台`} />
                    {viewTask.photo_count != null && (
                      <Meta label="有图片" value={`${viewTask.photo_count} 台`} />
                    )}
                    {viewTask.checked_at && (
                      <Meta label="检测于" value={fmtIso(viewTask.checked_at)} />
                    )}
                  </>
                )}
                {viewRun && (
                  <>
                    <Meta label="检测于" value={fmtIso(viewRun.created_at)} />
                    <Meta label="用时" value={`${Math.round(viewRun.elapsed)}s`} />
                  </>
                )}
              </span>
              <span className="ml-auto whitespace-nowrap text-xs text-muted-foreground/70">
                支持搜索 / 排序 / 翻页
              </span>
            </div>
            <div className="min-h-0 flex-1">
              <RecordsTable
                key={recordUrl}
                url={recordUrl}
                name="红外图片质量检测"
                imageColumns={IMAGE_COLUMNS}
              />
            </div>
          </section>
        ) : (
          <>
            {/* KPI 卡带：四个等宽口径，最近一次用分布卡替代原来的长统计行 */}
            <div className="grid shrink-0 grid-cols-2 gap-3 lg:grid-cols-4">
              <StatTile
                label="巡检任务"
                value={data.tasks.length}
                hint="当前公司与部门范围内"
              />
              <StatTile
                label="未检测"
                value={unchecked.length}
                accent={unchecked.length > 0 ? 'text-warning' : undefined}
                hint="进行中的任务不可勾选"
              />
              <StatTile
                label="已检测"
                value={checkedCount}
                hint={data.tasks.length > 0
                  ? `占 ${Math.round((checkedCount / data.tasks.length) * 100)}%`
                  : '—'}
              />
              <StatTile
                label="最近一次检测"
                hint={latest
                  ? `${fmtIso(latest.created_at)} · 用时 ${Math.round(latest.elapsed)}s`
                  : '尚未检测'}
              >
                {latest ? <ResultDistribution counts={latest.counts} /> : null}
              </StatTile>
            </div>

            {running && <RunProgress progress={data.progress} />}

            <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-xl border border-border bg-card">
              {/* 工具条：控件统一 h-8，左筛右操作，主操作只有「检测选中」一个 */}
              <div className="flex min-h-12 shrink-0 flex-wrap items-center gap-2 border-b border-border px-4 py-2">
                {deptOptions.length > 0 && (
                  <FilterSelect
                    ariaLabel="按部门筛选"
                    value={deptId}
                    options={deptOptions}
                    onChange={(v) => onDeptChange(String(v))}
                    className="h-8"
                  />
                )}
                <div className="inline-flex h-8 shrink-0 items-center rounded-lg border border-border
                  bg-muted/40 p-0.5">
                  {(['all', 'unchecked', 'checked'] as Filter[]).map((key) => (
                    <button
                      key={key}
                      type="button"
                      onClick={() => setFilter(key)}
                      className={cn(
                        'h-7 rounded-md px-3 text-sm transition-colors',
                        filter === key
                          ? 'bg-card text-foreground shadow-sm'
                          : 'text-muted-foreground hover:text-foreground',
                      )}
                    >
                      {FILTER_LABEL[key]}
                    </button>
                  ))}
                </div>
                <div className="ml-auto flex flex-wrap items-center gap-2">
                  <label className="flex h-8 cursor-pointer items-center gap-1.5 text-xs
                    text-muted-foreground">
                    <input
                      type="checkbox"
                      className="size-4 accent-primary"
                      checked={refreshPred}
                      onChange={(e) => setRefreshPred(e.target.checked)}
                    />
                    <span title="忽略结果缓存，耗时更长">重新推理</span>
                  </label>
                  <Button size="sm" variant="ghost" disabled={running} onClick={selectUnchecked}>
                    全选未检测（{unchecked.length}）
                  </Button>
                  {latestRunId && (
                    <Button size="sm" variant="outline" onClick={() => setView({ runId: latestRunId })}>
                      本次检测明细
                    </Button>
                  )}
                  <Button size="sm" disabled={!canStart} onClick={() => void start()}>
                    {starting
                      ? <Loader2 className="size-3.5 animate-spin" />
                      : <ScanEye className="size-3.5" />}
                    检测选中（{selected.size}）
                  </Button>
                </div>
              </div>

              <div className="min-h-0 flex-1 overflow-auto">
                <table className="w-full border-collapse text-left text-sm">
                  <thead className="sticky top-0 z-10 bg-muted">
                    <tr className="border-b border-border">
                      <th className="w-12 px-4 py-3" />
                      <th className="px-4 py-3 text-xs font-medium text-muted-foreground">巡检人</th>
                      <th className="whitespace-nowrap px-4 py-3 text-xs font-medium text-muted-foreground">
                        巡检时间
                      </th>
                      <th className="px-4 py-3 text-xs font-medium text-muted-foreground">设备数</th>
                      <th className="px-4 py-3 text-xs font-medium text-muted-foreground">检测结果</th>
                      <th className="whitespace-nowrap px-4 py-3 text-xs font-medium text-muted-foreground">
                        检测时间
                      </th>
                      <th className="w-10 px-2 py-3" />
                    </tr>
                  </thead>
                  <tbody>
                    {pageTasks.map((t) => {
                      const inProgress = t.status === 1;
                      const inspectAt = t.end_time || t.start_time;
                      return (
                        <tr
                          key={t.task_id}
                          onClick={() => setView({ taskId: t.task_id })}
                          className="h-12 cursor-pointer border-b border-border/60 last:border-0
                            transition-colors hover:bg-muted/40"
                        >
                          <td className="px-4 py-3" onClick={(e) => e.stopPropagation()}>
                            <input
                              type="checkbox"
                              className="size-4 accent-primary"
                              disabled={inProgress}
                              checked={selected.has(t.task_id)}
                              onChange={() => toggle(t.task_id)}
                              aria-label={`选择任务 ${t.inspector || t.task_id}`}
                            />
                          </td>
                          <td className="whitespace-nowrap px-4 py-3">
                            <span className="flex items-center gap-1.5">
                              {t.inspector || '—'}
                              {inProgress && <Badge variant="outline" className="text-xs">进行中</Badge>}
                            </span>
                          </td>
                          <td className="whitespace-nowrap px-4 py-3 text-xs tabular-nums text-muted-foreground">
                            {fmtMs(inspectAt)}
                          </td>
                          <td className="px-4 py-3 text-sm tabular-nums">{t.device_count}</td>
                          <td className="px-4 py-3"><ResultCell task={t} /></td>
                          <td className="whitespace-nowrap px-4 py-3 text-xs tabular-nums text-muted-foreground">
                            {fmtIso(t.checked_at)}
                          </td>
                          <td className="px-2 py-3 text-muted-foreground/40">
                            <ChevronRight className="size-4" />
                          </td>
                        </tr>
                      );
                    })}
                    {tasks.length === 0 && (
                      <tr>
                        <td colSpan={7} className="px-4 py-16 text-center text-sm text-muted-foreground">
                          <span className="inline-flex flex-col items-center gap-2">
                            <ImageOff className="size-6 opacity-60" />
                            该范围内没有巡检任务
                          </span>
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>

              <div className="flex h-12 shrink-0 items-center gap-2 border-t border-border px-4
                text-sm text-muted-foreground">
                <button
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={currentPage <= 1}
                  aria-label="上一页"
                  className="rounded-md p-1 transition-colors hover:bg-muted hover:text-foreground
                    disabled:opacity-40"
                >
                  <ChevronLeft className="size-4" />
                </button>
                <span className="tabular-nums">{currentPage} / {totalPages}</span>
                <button
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={currentPage >= totalPages}
                  aria-label="下一页"
                  className="rounded-md p-1 transition-colors hover:bg-muted hover:text-foreground
                    disabled:opacity-40"
                >
                  <ChevronRight className="size-4" />
                </button>
                <span className="ml-auto text-xs tabular-nums text-muted-foreground/70">
                  共 {tasks.length} 个任务 · 每页 {TASK_PAGE_SIZE} 条 · 已选 {selected.size}
                </span>
              </div>
            </section>
          </>
        )}
      </div>
    </FullScreenOverlay>
  );
}

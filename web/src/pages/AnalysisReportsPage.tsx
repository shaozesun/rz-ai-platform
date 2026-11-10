import { useEffect, useRef, useState } from 'react';
import {
  Loader2, ArrowLeft, RefreshCw, ChevronDown, Clock,
  FileText, Layers, CalendarRange, CheckCircle2, XCircle,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { FilterSelect, type Opt } from '@/components/filter-select';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import {
  listReportPresets, fetchPresetArtifactBlob,
  type ReportPreset, type PresetRun,
} from '@/api/scheduledTasks';

const RUN_LABEL: Record<string, string> = {
  pending: '排队中', running: '生成中', success: '已完成', failed: '失败',
};

const TICKET_OPTS: Opt[] = [
  { value: '', label: '全部类型' }, { value: 'change', label: '变更工单' },
  { value: 'breakdown', label: '故障工单' }, { value: 'question', label: '问题工单' },
  { value: 'event', label: '事件工单' }, { value: 'threshold', label: '阈值工单' },
];
// 空间与园区合并成一个下拉（两者本是同一维度的两级，后端也是两个参数各自过滤）。
// 值的编码：'' 全部空间 | 'nationwide' 全国 | 'by_park' 各园区（聚合） | 'park:<slug>' 具体园区
const PARK_PREFIX = 'park:';
const PERIOD_OPTS: Opt[] = [
  { value: '', label: '全部周期' }, { value: 'weekly', label: '每周' }, { value: 'monthly', label: '每月' },
];
const DAYS_OPTS: Opt[] = [
  { value: 0, label: '全部时间' }, { value: 30, label: '近 1 个月' },
  { value: 90, label: '近 3 个月' }, { value: 365, label: '近 1 年' },
];

const PAGE_SIZE = 20;

/** 执行清单按工单类型分组（组内是「全国 / 各园区 × 周/月」）。 */
function groupPresets(presets: ReportPreset[]): [string, ReportPreset[]][] {
  const groups = new Map<string, ReportPreset[]>();
  for (const p of presets) {
    groups.set(p.ticket_name, [...(groups.get(p.ticket_name) ?? []), p]);
  }
  for (const [, items] of groups) {
    items.sort((a, b) =>
      (a.space === b.space ? 0 : a.space === 'nationwide' ? -1 : 1)
      || a.space_label.localeCompare(b.space_label, 'zh')
      || (a.period === b.period ? 0 : a.period === 'weekly' ? -1 : 1));
  }
  return [...groups.entries()];
}

/** 行内用的紧凑时间：`10-15 08:56`（期号已写进行标题，这里只给时刻）。 */
function fmtShort(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN', { hour12: false });
}

/** 行标题里的期号：`2026 第 41 周` / `2026 年 9 月`。
 *  `period_label` 在老数据的兜底路径上可能是「每周/每月」（频率不是期号），要跳过；
 *  再退回解析 `period_key`，最后才退回频率。 */
function periodIssue(r: PresetRun): string {
  const FREQ = ['每周', '每月'];
  if (r.period_label && !FREQ.includes(r.period_label)) return r.period_label;
  const key = r.period_key || '';
  const week = key.match(/^(\d{4})-W(\d{1,2})$/);
  if (week) return `${week[1]} 第 ${Number(week[2])} 周`;
  const month = key.match(/^(\d{4})-(\d{1,2})$/);
  if (month) return `${month[1]} 年 ${Number(month[2])} 月`;
  return r.period_type_label || '';
}

export default function AnalysisReportsPage() {
  const [presets, setPresets] = useState<ReportPreset[]>([]);
  const [runs, setRuns] = useState<PresetRun[]>([]);
  const [total, setTotal] = useState(0);
  // 成功/失败用**后端全量口径**（原先前端数"已加载那一页"，与 total 自相矛盾）
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  // 分页：一次只保留当前页（后端 offset/limit）。原先是「加载更多」无限追加 ——
  // 页脚越滚越长、翻不回去，改成常规翻页。
  const [page, setPage] = useState(1);
  const [error, setError] = useState('');
  const [showPresets, setShowPresets] = useState(false);
  const [filter, setFilter] = useState({ ticket_type: '', space: '', period: '', park: '', days: 0 });
  // 发现到的园区（slug, 园区名）；只在没有筛选时重建，否则选中一个园区后选项会自己变少
  const [parks, setParks] = useState<[string, string][]>([]);
  const [detail, setDetail] = useState<{
    run: PresetRun; blobUrl: string; loading: boolean; error: string;
  } | null>(null);
  const blobRef = useRef<string>('');
  // 翻页后把列表滚回顶部（列表卡是内部滚动区，页脚固定不动）
  const scrollRef = useRef<HTMLDivElement>(null);

  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  /** 拉第 p 页（1-based）。分页只保留当前页，不再向后追加。 */
  const load = async (pageNo: number) => {
    setLoading(true);
    setError('');
    try {
      const data = await listReportPresets({
        ...filter, offset: (pageNo - 1) * PAGE_SIZE, limit: PAGE_SIZE,
      });
      // 供应商服务工单**暂不出报告**（后端预设已停用）：老进程仍返回时在这里兜一层
      const presets = data.presets.filter((p) => p.ticket_type !== 'supplier');
      setPresets(presets);
      if (!filter.park && !filter.ticket_type && !filter.space && !filter.period) {
        const seen = new Map<string, string>();
        for (const p of presets) if (p.park !== 'nationwide') seen.set(p.park, p.park_label);
        // 按园区中文名排序，保证下拉顺序稳定（不随接口返回顺序抖）
        setParks(Array.from(seen.entries()).sort((a, b) => a[1].localeCompare(b[1], 'zh')));
      }
      setTotal(data.total);
      setStatusCounts(data.statusCounts);
      setRuns(data.runs);
      setPage(pageNo);
    } catch {
      setError('加载失败，请刷新重试');
    } finally {
      setLoading(false);
    }
  };

  // 筛选变化回到第 1 页（页码不重置会出现「第 3 页只剩 2 条」这类越界空页）
  useEffect(() => { void load(1); }, [filter]);

  /** 翻页：页码钳在有效区间内；正在加载时不响应。 */
  const goPage = (p: number) => {
    const target = Math.min(Math.max(1, p), totalPages);
    if (target === page || loading) return;
    void load(target);
    scrollRef.current?.scrollTo({ top: 0 });
  };

  const releaseBlob = () => {
    if (blobRef.current) {
      URL.revokeObjectURL(blobRef.current);
      blobRef.current = '';
    }
  };

  /** 打开某一期：拉产物 Blob → 内嵌预览（iframe 带不了 Bearer 头，必须先在 JS 里取）。 */
  const openRun = async (r: PresetRun) => {
    releaseBlob();
    const first = r.artifact_urls?.[0];
    setDetail({
      run: r, blobUrl: '', loading: !!first,
      error: r.status === 'failed' ? (r.error || '生成失败') : '',
    });
    if (!first) return;
    try {
      const seg = first.split('/').pop() || '';
      const blob = await fetchPresetArtifactBlob(r.run_id, seg);
      const url = URL.createObjectURL(blob);
      blobRef.current = url;
      setDetail((cur) => (cur ? { ...cur, blobUrl: url, loading: false } : cur));
    } catch (e) {
      setDetail((cur) => (cur ? { ...cur, loading: false, error: (e as Error).message } : cur));
    }
  };

  const closeRun = () => {
    releaseBlob();
    setDetail(null);
  };

  // ---------------- 详情：内嵌 PDF（无对话/提示词/工具过程） ----------------
  if (detail) {
    return (
      // 整窗查看器：详情**不套 PlatformShell**（用户要求"点进去不做左侧导航栏，只显示查看器"），
      // 改为覆盖整个浏览器窗口的浮层（同 FileCard / ArtifactPanel 的全屏预览写法）。
      // 刻意不显示报告标题与下载按钮，只留「返回目录」这一条出入口。
      <div className="fixed inset-0 z-[60] flex flex-col bg-background">
        <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5">
          <Button size="sm" variant="ghost" onClick={closeRun}>
            <ArrowLeft className="size-4" /> 返回目录
          </Button>
        </div>
        {/* 不再套圆角卡片：预览本身就是整屏内容，加边框+圆角反而显得"框在中间" */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden bg-card">
          {detail.loading ? (
            <div className="flex flex-1 items-center justify-center">
              <Loader2 className="size-6 animate-spin text-muted-foreground" />
            </div>
          ) : detail.error ? (
            <div className="flex flex-1 flex-col items-center justify-center gap-2">
              <XCircle className="size-8 text-destructive/70" />
              <p className="text-sm text-destructive">{detail.error}</p>
            </div>
          ) : detail.blobUrl ? (
            <iframe title="报告预览" src={detail.blobUrl}
              className="h-full w-full flex-1 border-0 bg-white" />
          ) : (
            <p className="p-6 text-sm text-muted-foreground">这一期没有可预览的产物</p>
          )}
        </div>
      </div>
    );
  }

  // 「空间」下拉 = 全部空间 / 全国 / 各园区（聚合）/ 各具体园区 —— 一个 value 覆盖原来的
  // space + park 两个参数（选具体园区时两个都写：space='by_park' 让筛选状态自解释）。
  const spaceValue = filter.park ? `${PARK_PREFIX}${filter.park}` : filter.space;
  const spaceOptions: Opt[] = [
    { value: '', label: '全部空间' },
    { value: 'nationwide', label: '全国' },
    { value: 'by_park', label: '各园区' },
    ...parks.map(([slug, label]) => ({ value: `${PARK_PREFIX}${slug}`, label })),
  ];
  const onSpaceChange = (v: string | number) => {
    const val = String(v);
    setFilter(val.startsWith(PARK_PREFIX)
      ? { ...filter, space: 'by_park', park: val.slice(PARK_PREFIX.length) }
      : { ...filter, space: val, park: '' });
  };

  // ---------------- 目录：页头 + 筛选 + 平铺产出列表 ----------------
  // 后端没返回 status_counts（滚动重启期间跑着旧代码）→ 退回数"已加载那一页"，
  // 否则会显示 ✓0 ✗0，比旧口径更糟。与 total 的兜底同思路。
  const hasCounts = Object.keys(statusCounts).length > 0;
  const succeeded = hasCounts ? (statusCounts.success ?? 0)
    : runs.filter((r) => r.status === 'success').length;
  const failed = hasCounts ? (statusCounts.failed ?? 0)
    : runs.filter((r) => r.status === 'failed').length;

  return (
    <PlatformShell
      title="分析报告"
      description="周期自动产出深度分析报告"
      actions={(
        <>
          {/* 刷新重载**当前页**（不是跳回第 1 页）：刷新的是"我正在看的东西" */}
          <Button size="sm" variant="ghost" onClick={() => void load(page)}>
            <RefreshCw className="size-4" /> 刷新
          </Button>
          <Button size="sm" variant="outline" onClick={() => setShowPresets((v) => !v)}>
            <Layers className="size-4" /> 执行清单（{presets.length}）
            <ChevronDown className={cn('size-3.5 transition-transform', showPresets && 'rotate-180')} />
          </Button>
        </>
      )}
    >
      {/* 页内定高：筛选条与页脚固定，只有列表卡内部滚动（整页不再滚到底才看得见分页） */}
      <div className="flex min-h-0 flex-1 flex-col gap-3">
        {showPresets && (
          // 61 条预设 × 3 列展开约 1200px，定高后必须自己在卡内滚，否则会把列表挤没
          <Card className="max-h-[40vh] shrink-0 overflow-y-auto">
            <CardContent className="space-y-3 p-4">
              {groupPresets(presets).map(([ticketName, items]) => (
                <div key={ticketName}>
                  <p className="mb-2 text-sm font-semibold">{ticketName}</p>
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                    {items.map((p) => (
                      <div key={p.key}
                        className="flex items-start gap-2 rounded-lg border border-border bg-secondary/30 p-3"
                        title={p.name}
                      >
                        <div className={cn(
                          'mt-1 size-2 shrink-0 rounded-full',
                          p.enabled && p.task_id ? 'bg-success' : 'bg-muted-foreground/40',
                        )} />
                        <div className="min-w-0 space-y-0.5">
                          <p className="truncate text-sm font-medium">
                            {p.space_label} · {p.period_label}
                          </p>
                          <p className="flex items-center gap-1 text-xs text-muted-foreground">
                            <Clock className="size-3 shrink-0" /> {p.frequency}
                          </p>
                          {p.next_run_at ? (
                            <p className="truncate text-xs text-muted-foreground/80">
                              下次 {fmtTime(p.next_run_at)}
                            </p>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </CardContent>
          </Card>
        )}

        {/* 筛选：只放筛选器；产出统计挪到列表页脚与翻页同一行（对齐「用户管理」） */}
        <div className="flex shrink-0 flex-wrap items-center gap-2 rounded-xl border border-border bg-card p-2 shadow-sm">
          <FilterSelect icon={FileText} ariaLabel="按工单类型筛选" value={filter.ticket_type}
            options={TICKET_OPTS} onChange={(v) => setFilter({ ...filter, ticket_type: String(v) })} />
          <FilterSelect icon={Layers} ariaLabel="按空间筛选" value={spaceValue}
            options={spaceOptions} onChange={onSpaceChange} />
          <FilterSelect icon={CalendarRange} ariaLabel="按周期筛选" value={filter.period}
            options={PERIOD_OPTS} onChange={(v) => setFilter({ ...filter, period: String(v) })} />
          <FilterSelect icon={Clock} ariaLabel="按时间范围筛选" value={filter.days}
            options={DAYS_OPTS} onChange={(v) => setFilter({ ...filter, days: Number(v) })} />
        </div>

        {error && <p className="text-sm text-destructive">{error}</p>}

        {loading ? (
          <div className="flex flex-1 items-center justify-center">
            <Loader2 className="size-6 animate-spin text-muted-foreground" />
          </div>
        ) : runs.length === 0 ? (
          <Card className="shrink-0">
            <CardContent className="flex flex-col items-center gap-2 py-16">
              <FileText className="size-8 text-muted-foreground/50" />
              <p className="text-sm text-muted-foreground">还没有产出</p>
              <p className="text-xs text-muted-foreground/80">到点自动生成，完成后右上角铃铛会提醒</p>
            </CardContent>
          </Card>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col gap-3">
            {/* 一条到底平铺：不再按「期」分组折叠。期号写进每行的标题里
                （`变更工单 · 全国 · 2026 第 41 周`），周报月报靠标题自然区分。 */}
            <Card className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-none">
              {/* 行在卡内滚动：页脚（统计/分页）留在卡外，始终看得见 */}
              <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto">
                <CardContent className="divide-y divide-border p-0">
                  {runs.map((r) => {
                    const hasArtifact = Boolean(r.artifact_urls?.length);
                    const title = [r.ticket_name || r.preset_name || '分析报告',
                      r.space_label, periodIssue(r)].filter(Boolean).join(' · ');
                    return (
                      <button
                        type="button"
                        key={r.run_id}
                        disabled={!hasArtifact}
                        onClick={() => void openRun(r)}
                        title={r.status === 'failed' ? (r.error || '生成失败') : `${title}｜${r.teaser}`}
                        className={cn(
                          'flex w-full items-center gap-3 px-3 py-1.5 text-left transition-colors',
                          hasArtifact ? 'hover:bg-muted/50' : 'cursor-default opacity-70',
                        )}
                      >
                        {/* 行首统一用「文档」图标：图标只说明**这是一份报告文件**，
                            类型信息交给标题文字（`事件工单 · 廊坊园区 · 2026 第 41 周`）。
                            只有失败行改用淡红底做线索。 */}
                        <span className={cn(
                          'flex size-8 shrink-0 items-center justify-center rounded-lg',
                          r.status === 'failed'
                            ? 'bg-destructive/10 text-destructive'
                            : 'bg-muted text-muted-foreground',
                        )}>
                          <FileText className="size-4" />
                        </span>
                        {/* 固定宽的标题列 + 摘要占满中间：标题很短时靠"摘要撑满"而不是留白，
                            跨行也对齐成列，读起来是索引而不是散行 */}
                        <span className="w-64 shrink-0 truncate text-sm">{title}</span>
                        <span className={cn(
                          'min-w-0 flex-1 truncate text-xs',
                          r.status === 'failed'
                            ? 'text-destructive/80' : 'text-muted-foreground',
                        )}>
                          {r.status === 'failed'
                            ? (r.error || '生成失败')
                            : (r.teaser || '—')}
                        </span>
                        <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                          {fmtShort(r.triggered_at)}
                        </span>
                        <span className={cn(
                          'w-20 shrink-0 text-right text-xs tabular-nums',
                          r.status === 'failed' ? 'text-destructive' : 'text-muted-foreground/80',
                        )}>
                          {[r.duration_ms ? `${(r.duration_ms / 1000).toFixed(0)}s` : '',
                            hasArtifact ? RUN_LABEL[r.status] : '无产物']
                            .filter(Boolean).join(' · ')}
                        </span>
                      </button>
                    );
                  })}
                </CardContent>
              </div>
            </Card>
            {/* 页脚：产出统计在左、翻页在右（对齐「用户管理」的分页条） */}
            <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-t border-border pt-3">
              <div className="flex items-center gap-3 text-xs text-muted-foreground">
                {/* total 是**产出份数**（后端 count_documents），不是期数 —— 一期之内可能有多份 */}
                <span>共 <b className="text-foreground tabular-nums">{total}</b> 份产出</span>
                <span className="flex items-center gap-2">
                  <span className="flex items-center gap-1"><CheckCircle2 className="size-3.5 text-success" />{succeeded}</span>
                  {failed > 0 && <span className="flex items-center gap-1"><XCircle className="size-3.5 text-destructive" />{failed}</span>}
                </span>
              </div>
              {totalPages > 1 && (
                <div className="flex items-center gap-1">
                  <Button size="xs" variant="outline" disabled={page <= 1 || loading}
                    onClick={() => goPage(page - 1)}>上一页</Button>
                  <span className="px-2 text-xs tabular-nums text-muted-foreground">{page} / {totalPages}</span>
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

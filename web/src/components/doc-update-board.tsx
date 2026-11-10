import { useMemo, useState } from 'react';
import {
  ArrowLeft, ChevronDown, ChevronRight, FileCheck2, Loader2,
  RefreshCw,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { FilterSelect } from '@/components/filter-select';
import { FullScreenOverlay } from '@/components/full-screen-overlay';
import { cn } from '@/lib/utils';
import {
  STATUS_ORDER, STATUS_VISUALS, countStatus, deptKey, deptLabel, fmtTime,
} from '@/components/doc-update-status';
import type {
  DocUpdateItem, DocUpdateOverview, DocUpdateStatus,
} from '@/api/scheduledTasks';

const MODE_TEXT: Record<string, string> = {
  cross: '与上一期比', baseline: '首期基线',
};

function StatusChip({ status, label }: { status: DocUpdateStatus; label: string }) {
  const { chip, dot } = STATUS_VISUALS[status];
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px]', chip)}>
      <span className={cn('size-1.5 rounded-full', dot)} />
      {label}
    </span>
  );
}

/** 本期构成：四段堆叠条 + 图例（基线期没有可比构成，换成一句说明）。 */
function Composition({ counts, labels, baseline, itemCount, unit = '项' }: {
  counts: Partial<Record<DocUpdateStatus, number>>;
  labels: Partial<Record<DocUpdateStatus, string>>;
  baseline: boolean;
  itemCount: number;
  unit?: string;
}) {
  const parts = STATUS_ORDER.map((k) => ({
    key: k, label: labels[k] ?? k, rows: counts[k] ?? 0,
  }));
  const total = parts.reduce((n, p) => n + p.rows, 0);
  if (!total) {
    return (
      <p className="py-8 text-center text-xs text-muted-foreground">
        {baseline
          ? `首期基线：已记录 ${itemCount} ${unit}，下期起比对是否更新`
          : '本期没有可比的更新结果'}
      </p>
    );
  }
  return (
    <div className="space-y-3">
      <div className="flex h-2.5 overflow-hidden rounded-full bg-muted">
        {parts.filter((p) => p.rows > 0).map((p) => (
          <span
            key={p.key}
            className={STATUS_VISUALS[p.key].bar}
            style={{ width: `${(p.rows / total) * 100}%` }}
            title={`${p.label} ${p.rows} 项`}
          />
        ))}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1.5">
        {parts.map((p) => (
          <div key={p.key} className="flex items-center gap-1.5 text-[11px]">
            <span className={cn('size-2 shrink-0 rounded-full', STATUS_VISUALS[p.key].bar)} />
            <span className="truncate text-muted-foreground">{p.label}</span>
            <span className="ml-auto tabular-nums">{p.rows}</span>
            <span className="w-9 shrink-0 text-right tabular-nums text-muted-foreground/70">
              {Math.round((p.rows / total) * 100)}%
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

/** 一行清单项；点开列该项匹配到的文件（状态 + 待复核说明）。 */
function ItemRow({ item, labels, open, onToggle, showDept }: {
  item: DocUpdateItem;
  labels: Partial<Record<DocUpdateStatus, string>>;
  open: boolean;
  onToggle: () => void;
  showDept: boolean;
}) {
  const age = item.changed_days_ago == null ? '—' : `${item.changed_days_ago} 天前`;
  return (
    <>
      <tr
        onClick={onToggle}
        className="cursor-pointer border-b border-border/60 hover:bg-muted/40 last:border-0"
      >
        {showDept && (
          <td className="whitespace-nowrap px-3 py-1.5 text-xs">{deptLabel(item)}</td>
        )}
        <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
          {item.number}
        </td>
        <td className="px-3 py-1.5 text-xs">{item.name}</td>
        <td className="px-3 py-1.5">
          <StatusChip status={item.status} label={item.status_label} />
        </td>
        <td className="px-3 py-1.5 text-xs tabular-nums text-muted-foreground">{item.file_count}</td>
        <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
          {age}
        </td>
        <td className="max-w-64 truncate px-3 py-1.5 text-xs text-muted-foreground/80"
          title={item.error || undefined}>{item.error || ''}</td>
        <td className="w-6 px-2 py-1.5 text-muted-foreground/60">
          {item.files.length > 0 && (open
            ? <ChevronDown className="size-3.5" />
            : <ChevronRight className="size-3.5" />)}
        </td>
      </tr>
      {open && item.files.length > 0 && (
        <tr className="border-b border-border/60 bg-muted/25">
          <td colSpan={showDept ? 8 : 7} className="px-3 py-2">
            <ul className="space-y-1">
              {item.files.map((f, i) => (
                <li key={`${f.file_name}-${i}`} className="flex flex-wrap items-center gap-2 text-[11px]">
                  <StatusChip status={f.status} label={labels[f.status] ?? f.status} />
                  <span className="text-foreground/80">{f.file_name || '（无文件名）'}</span>
                  {f.warn && <span className="text-warning">{f.warn}</span>}
                  {f.error && <span className="text-destructive/80">{f.error}</span>}
                </li>
              ))}
            </ul>
          </td>
        </tr>
      )}
    </>
  );
}

/** 文档更新检查看板：整屏（对齐合规检查的 RunBoard），点清单项可展开其文件。 */
export function DocUpdateBoard({
  data, running, notice, onCheck, onClose,
  dept, onDeptChange, period, onPeriodChange,
}: {
  data: DocUpdateOverview;
  running: boolean;
  notice: string;
  onCheck: () => void;
  onClose: () => void;
  dept: string;
  onDeptChange: (value: string) => void;
  period: string;
  onPeriodChange: (value: string) => void;
}) {
  const [filter, setFilter] = useState<DocUpdateStatus | ''>('');
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const verdict = data.verdict;
  const items = useMemo(() => verdict?.items ?? [], [verdict]);
  const labels = verdict?.labels ?? {};
  const mode = verdict?.mode ?? 'baseline';
  const baseline = verdict?.baseline ?? true;

  // 部门列是常驻配置，但口径要按快照本身判断（旧单树快照没有部门维度）
  const deptColumns = data.dept_columns ?? [];
  const periods = data.periods ?? [];
  const deptOptions = deptColumns.map((col) => ({
    value: deptKey(col), label: deptLabel(col),
  }));
  const periodOptions = [
    { value: '', label: '最新' },
    ...periods.map((p) => ({ value: p.period_key, label: p.period_label || p.period_key })),
  ];
  const catalogSize = data.catalog?.length ?? 25;
  const isDept = verdict?.scope === 'dept';
  const unit = isDept ? '格' : '项';
  const deptFilter = isDept ? dept : '';
  const showDept = isDept && !deptFilter;
  const companyCount = new Set(deptColumns.map((c) => c.company_id || c.company_name || '')).size;
  const scopeTitle = companyCount > 1
    ? `${companyCount} 家公司 · ${deptColumns.length} 个部门` : `${deptColumns.length} 个部门`;
  const selectedDept = deptColumns.find((c) => deptKey(c) === deptFilter);
  const deptTitle = selectedDept ? deptLabel(selectedDept) : deptFilter;

  // 只显示所选部门的 25 项；dept 为空（旧口径/未选）时原样铺开
  const deptItems = deptFilter
    ? items.filter((i) => deptKey(i) === deptFilter)
    : items;
  const rows = filter ? deptItems.filter((i) => i.status === filter) : deptItems;
  const counts = deptFilter ? countStatus(deptItems) : (verdict?.counts ?? {});
  const total = deptFilter ? deptItems.length : (verdict?.total ?? items.length);
  const changed = deptFilter
    ? (verdict?.changes?.changed ?? []).filter((c) => deptKey(c) === deptFilter)
    : (verdict?.changes?.changed ?? []);
  const warnings = deptFilter
    ? (verdict?.warnings ?? []).filter((w) => deptKey(w) === deptFilter)
    : (verdict?.warnings ?? []);

  const hints: Record<DocUpdateStatus, string> = isDept
    ? {
        updated: '本期有文件，或哈希与上期不同',
        stale: '最新一期早于应到期，或哈希与上期相同',
        missing: '该部门下未查询到相关文件',
        failed: '取不到字节或类型校验不过',
        baseline: '原地更新型尚无上期快照',
      }
    : {
        updated: '哈希与上一期不同（新增文件也算）',
        stale: '哈希与上一期相同',
        missing: '未查询到相关文件',
        failed: '取不到字节或类型校验不过',
        baseline: '',
      };

  const toggle = (key: string) => {
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  return (
    <FullScreenOverlay>
      <div className="flex shrink-0 items-center gap-2 border-b border-border px-3 py-1.5">
        <Button size="sm" variant="ghost" onClick={onClose}>
          <ArrowLeft className="size-4" /> 返回目录
        </Button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-6xl space-y-3 p-3 sm:p-4">
          {/* 标题与快照信息放内容区：顶栏窄条只留「返回目录」 */}
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-base font-semibold">文档更新检查</span>
            <Badge variant="outline">
              {isDept ? `${scopeTitle} × ${catalogSize} 项` : MODE_TEXT[mode] ?? mode}
            </Badge>
            <span className="text-xs tabular-nums text-muted-foreground">
              {verdict?.period_label || '—'}
            </span>
            <span className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              <span className="tabular-nums">快照 {fmtTime(data.latest?.taken_at)}</span>
              {data.latest && (
                <span className="tabular-nums">
                  {data.latest.file_count} 个文件 · {data.latest.elapsed}s
                </span>
              )}
            </span>
            <Button size="sm" variant="outline" className="ml-auto"
              disabled={running} onClick={onCheck}>
              {running ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
              {running ? '检查中' : '立即检查'}
            </Button>
          </div>

          {baseline && (
            <p className="rounded-lg border border-chart-2/30 bg-chart-2/10 px-3 py-2 text-xs leading-relaxed text-foreground/80">
              {isDept
                ? <>首轮：按期提交型（有月份文件夹）已直接判定；原地更新型没有上期快照，记为「基线」，
                  <b>下期起</b>比对是否更新。</>
                : <>首期只建立基线，<b>下期起</b>才比对是否更新；本次每项都记「基线」，不代表「未更新」。</>}
            </p>
          )}

          {/* 指标卡带：点一下只留该类清单项 */}
          <div className="grid grid-cols-2 gap-2 xl:grid-cols-5">
            <button
              type="button"
              onClick={() => setFilter('')}
              className={cn(
                'rounded-xl border bg-card p-3 text-left transition-colors',
                filter === ''
                  ? 'border-primary/50 ring-1 ring-primary/20'
                  : 'border-border hover:border-primary/30',
              )}
            >
              <div className="flex items-center gap-2">
                <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-chart-2/15 text-chart-2">
                  <FileCheck2 className="size-3.5" />
                </span>
                <span className="truncate text-xs text-muted-foreground">全部</span>
              </div>
              <div className="mt-2 flex items-end gap-2">
                <span className="text-2xl font-semibold leading-none tracking-tight tabular-nums">{total}</span>
                <span className="pb-0.5 text-[11px] text-muted-foreground">{unit}</span>
              </div>
              <p className="mt-1.5 line-clamp-2 text-[11px] leading-relaxed text-muted-foreground/80">
                {isDept ? `${scopeTitle} × ${catalogSize} 项` : '只判定 25 条月度项'}
              </p>
            </button>

            {STATUS_ORDER.map((key) => {
              const { Icon, chip } = STATUS_VISUALS[key];
              const selected = filter === key;
              return (
                <button
                  key={key}
                  type="button"
                  onClick={() => setFilter(selected ? '' : key)}
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
                    <span className="truncate text-xs text-muted-foreground">{labels[key] ?? key}</span>
                  </div>
                  <div className="mt-2 flex items-end gap-2">
                    <span className={cn(
                      'text-2xl font-semibold leading-none tracking-tight tabular-nums',
                      (counts[key] ?? 0) === 0 && 'text-muted-foreground/50',
                    )}>{counts[key] ?? 0}</span>
                    <span className="pb-0.5 text-[11px] text-muted-foreground">{unit}</span>
                  </div>
                  <p className="mt-1.5 line-clamp-2 text-[11px] leading-relaxed text-muted-foreground/80">
                    {hints[key]}
                  </p>
                </button>
              );
            })}
          </div>

          <div className="rounded-xl border border-border bg-card p-4">
            <p className="mb-3 text-xs font-medium text-muted-foreground">本期构成</p>
            <Composition counts={counts} labels={labels} baseline={baseline}
              itemCount={total} unit={unit} />
          </div>

          <div className="rounded-xl border border-border bg-card">
            <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
              <p className="text-xs font-medium text-muted-foreground">
                {filter ? `筛选：${labels[filter] ?? filter}` : '清单项明细'}
              </p>
              <span className="text-[11px] tabular-nums text-muted-foreground/70">
                {deptFilter
                  ? `${deptTitle} · ${rows.length} 项${filter ? `（本部门共 ${total}）` : ''}`
                  : `${rows.length} 项${filter ? `（全部 ${total}）` : ''}`}
              </span>
              <div className="ml-auto flex flex-wrap items-center gap-2">
                {filter && (
                  <button type="button" onClick={() => setFilter('')}
                    className="text-[11px] text-primary hover:underline">
                    清除筛选
                  </button>
                )}
                {deptColumns.length > 0 && (
                  <span className="flex items-center gap-1 text-xs text-muted-foreground">
                    部门
                    <FilterSelect ariaLabel="按部门筛选" value={dept}
                      options={deptOptions} onChange={(v) => onDeptChange(String(v))} />
                  </span>
                )}
                {periods.length > 1 && (
                  <span className="flex items-center gap-1 text-xs text-muted-foreground">
                    期间
                    <FilterSelect ariaLabel="按期间筛选" value={period}
                      options={periodOptions} onChange={(v) => onPeriodChange(String(v))} />
                  </span>
                )}
              </div>
            </div>
            {rows.length === 0 ? (
              <p className="px-3 py-10 text-center text-xs text-muted-foreground">
                {items.length === 0 ? '尚无快照，点「立即检查」建立首期基线' : '该范围内没有清单项'}
              </p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-left">
                  <thead>
                    <tr className="border-b border-border text-[11px] text-muted-foreground">
                      {showDept && <th className="px-3 py-1.5 font-medium">部门</th>}
                      <th className="px-3 py-1.5 font-medium">编号</th>
                      <th className="px-3 py-1.5 font-medium">文档</th>
                      <th className="px-3 py-1.5 font-medium">状态</th>
                      <th className="px-3 py-1.5 font-medium">文件</th>
                      <th className="px-3 py-1.5 font-medium">上次变更</th>
                      <th className="px-3 py-1.5 font-medium">说明</th>
                      <th className="w-6 px-2 py-1.5" />
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((item) => {
                      const key = `${deptKey(item)}-${item.seq ?? item.number}`;
                      return (
                        <ItemRow
                          key={key}
                          item={item}
                          labels={labels}
                          showDept={showDept}
                          open={expanded.has(key)}
                          onToggle={() => toggle(key)}
                        />
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          {changed.length > 0 && (
            <div className="rounded-xl border border-border bg-card">
              <p className="border-b border-border px-3 py-2 text-xs font-medium text-muted-foreground">
                本期变更文件（{changed.length}）
              </p>
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-border text-[11px] text-muted-foreground">
                    {showDept && <th className="px-3 py-1.5 font-medium">部门</th>}
                    <th className="px-3 py-1.5 font-medium">编号</th>
                    <th className="px-3 py-1.5 font-medium">文档</th>
                    <th className="px-3 py-1.5 font-medium">文件名</th>
                    <th className="px-3 py-1.5 font-medium">变更时间</th>
                  </tr>
                </thead>
                <tbody>
                  {changed.map((c, i) => (
                    <tr key={`${deptKey(c)}-${c.number}-${c.file_name}-${i}`} className="border-b border-border/60 last:border-0">
                      {showDept && (
                        <td className="whitespace-nowrap px-3 py-1.5 text-xs">{deptLabel(c)}</td>
                      )}
                      <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
                        {c.number}
                      </td>
                      <td className="px-3 py-1.5 text-xs">{c.name}</td>
                      <td className="px-3 py-1.5 text-xs text-muted-foreground">{c.file_name}</td>
                      <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
                        {fmtTime(c.changed_at)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {warnings.length > 0 && (
            <div className="rounded-xl border border-warning/30 bg-card">
              <p className="border-b border-border px-3 py-2 text-xs font-medium text-warning">
                待复核（{warnings.length}）：档案类型或大小与实际字节不符
              </p>
              <table className="w-full border-collapse text-left">
                <thead>
                  <tr className="border-b border-border text-[11px] text-muted-foreground">
                    {showDept && <th className="px-3 py-1.5 font-medium">部门</th>}
                    <th className="px-3 py-1.5 font-medium">编号</th>
                    <th className="px-3 py-1.5 font-medium">文档</th>
                    <th className="px-3 py-1.5 font-medium">文件名</th>
                    <th className="px-3 py-1.5 font-medium">说明</th>
                  </tr>
                </thead>
                <tbody>
                  {warnings.map((w, i) => (
                    <tr key={`${deptKey(w)}-${w.number}-${w.file_name}-${i}`} className="border-b border-border/60 last:border-0">
                      {showDept && (
                        <td className="whitespace-nowrap px-3 py-1.5 text-xs">{deptLabel(w)}</td>
                      )}
                      <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
                        {w.number}
                      </td>
                      <td className="px-3 py-1.5 text-xs">{w.name}</td>
                      <td className="px-3 py-1.5 text-xs text-muted-foreground">{w.file_name}</td>
                      <td className="px-3 py-1.5 text-xs text-warning">{w.warn}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="space-y-1.5 pb-4 text-[11px] leading-relaxed text-muted-foreground/80">
            {isDept ? (
              <>
                <p>「未找到」「下载失败」不等于「未更新」：前者是该部门下未查询到相关文件，
                  后者是取不到字节或类型校验不过，需人工复核。</p>
                <p>判据按目录形态自动分两种：有月份文件夹的项只看本期应到月份里有没有文件（不下载）；
                  没有月份文件夹的项用文件内容哈希（sha256）与上一期快照比。</p>
                <p>是否更新<b>只与上一期（上月）快照比</b>：同月内手动检查多次，结论都不会变。</p>
              </>
            ) : (
              <>
                <p>「未找到」「下载失败」不等于「未更新」：前者是未查询到相关文件，
                  后者是取不到字节或类型校验不过，需人工复核。</p>
                <p>判定用文件内容哈希（sha256），不用接口的修改时间字段；同一次检查只下载文件算哈希，不落盘。</p>
                <p>是否更新<b>只与上一期（上月）快照比</b>：同月内手动检查多次，结论都不会变。</p>
              </>
            )}
          </div>

          {notice && <p className="pb-4 text-xs text-muted-foreground">{notice}</p>}
        </div>
      </div>
    </FullScreenOverlay>
  );
}

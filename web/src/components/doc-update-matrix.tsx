import { Fragment } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils';
import { STATUS_VISUALS } from '@/components/doc-update-status';
import type { DocUpdateItem, DocUpdateStatus } from '@/api/scheduledTasks';

/** 矩阵行：清单项展示信息（看板与合规页卡共用，只约束渲染需要的字段）。 */
export interface CatalogRow {
  seq: number;
  number: string;
  name: string;
}

/** 矩阵列：部门。`dept_id` 可能为空（注册表尚未解析），所以键回退到名称。 */
export interface MatrixColumn {
  dept_id: string;
  dept_name: string;
  error?: string;
}

/** 列/格的稳定键：优先 `dept_id`，没解析出来就用部门名。 */
export function deptKey(col: { dept_id?: string; dept_name: string }): string {
  return col.dept_id || col.dept_name;
}

const NUM_COL = 'w-[72px] min-w-[72px]';
const NAME_COL = 'w-[168px] min-w-[168px] left-[72px]';

function StatusChip({ status, label }: { status: DocUpdateStatus; label: string }) {
  const { chip, dot } = STATUS_VISUALS[status];
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px]', chip)}>
      <span className={cn('size-1.5 rounded-full', dot)} />
      {label}
    </span>
  );
}

function MatrixCell({ item, labels, dimmed, focused, onOpen }: {
  item: DocUpdateItem | undefined;
  labels: Partial<Record<DocUpdateStatus, string>>;
  dimmed: boolean;
  focused: boolean;
  onOpen: () => void;
}) {
  if (!item) {
    return <td className="p-1 text-center text-[11px] text-muted-foreground/50">—</td>;
  }
  const { chip, dot } = STATUS_VISUALS[item.status];
  const title = [
    item.status_label,
    item.layout === 'period' && item.latest_period ? `最新 ${item.latest_period}` : '',
    item.error,
  ].filter(Boolean).join(' · ');
  return (
    <td className="p-1 align-middle">
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onOpen(); }}
        title={title || undefined}
        className={cn(
          'flex w-full items-center justify-center gap-1 rounded px-1 py-0.5 text-[10px] transition-opacity',
          chip, dimmed && 'opacity-25', focused && 'ring-1 ring-primary/60',
        )}
      >
        <span className={cn('size-1.5 shrink-0 rounded-full', dot)} />
        {labels[item.status] ?? item.status_label}
      </button>
    </td>
  );
}

function DetailBlock({ row, columns, cellMap, labels, focus, onClear }: {
  row: CatalogRow;
  columns: MatrixColumn[];
  cellMap: Map<string, DocUpdateItem>;
  labels: Partial<Record<DocUpdateStatus, string>>;
  focus: { seq: number; deptId: string } | null;
  onClear: () => void;
}) {
  const focused = focus?.seq === row.seq;
  const target = focused
    ? columns.filter((c) => deptKey(c) === focus?.deptId)
    : columns;
  return (
    <div className="space-y-2">
      {focused && (
        <button type="button" onClick={onClear}
          className="text-[11px] text-primary hover:underline">显示全部部门</button>
      )}
      {target.map((dept) => {
        const item = cellMap.get(`${deptKey(dept)}-${row.seq}`);
        if (!item) return null;
        if (!focused && item.files.length === 0 && !item.error) return null;
        return (
          <div key={deptKey(dept)}>
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              <span className="font-medium">{dept.dept_name}</span>
              <StatusChip status={item.status} label={item.status_label} />
              {item.layout === 'period' && (
                <span className="text-muted-foreground">
                  按期提交 · 应到 {item.expected_period || '—'} · 最新 {item.latest_period || '—'}
                </span>
              )}
              {item.error && <span className="text-destructive/80">{item.error}</span>}
            </div>
            {item.actual_dirs && item.actual_dirs.length > 0 && (
              <div className="mt-0.5 break-all pl-3 text-[11px] text-muted-foreground">
                实际目录：{item.actual_dirs.join('、')}
              </div>
            )}
            {item.files.length > 0 && (
              <ul className="mt-1 space-y-0.5 pl-3">
                {item.files.map((f, i) => (
                  <li key={`${f.file_name}-${i}`} className="flex flex-wrap items-center gap-2 text-[11px]">
                    <StatusChip status={f.status} label={labels[f.status] ?? f.status} />
                    <span className="text-foreground/80">{f.file_name || '（无文件名）'}</span>
                    {f.period && <span className="text-muted-foreground">期号 {f.period}</span>}
                    {f.warn && <span className="text-warning">{f.warn}</span>}
                    {f.error && <span className="text-destructive/80">{f.error}</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

function columnStat(cells: Map<string, DocUpdateItem>, catalog: CatalogRow[], key: string) {
  let updated = 0;
  let total = 0;
  for (const row of catalog) {
    const item = cells.get(`${key}-${row.seq}`);
    if (!item) continue;
    total += 1;
    if (item.status === 'updated') updated += 1;
  }
  return { updated, total };
}

/**
 * 部门 × 清单项矩阵：行 = 25 条项（编号/文档两列 sticky），列 = 部门（表头 sticky）。
 *
 * - 默认（看板）：行可展开看每个部门命中的文件/实际目录，点格聚焦到该部门；
 * - `compact`（合规页卡）：不展开、不画明细，点格只回调 `onCell`，由外层决定去哪看明细。
 */
export function DeptMatrix({
  catalog, columns, cells, labels, filter = '', expanded, focus,
  onToggleRow, onCell, onClearFocus, compact = false, className,
}: {
  catalog: CatalogRow[];
  columns: MatrixColumn[];
  cells: Map<string, DocUpdateItem>;
  labels: Partial<Record<DocUpdateStatus, string>>;
  filter?: DocUpdateStatus | '';
  expanded?: Set<number>;
  focus?: { seq: number; deptId: string } | null;
  onToggleRow?: (seq: number) => void;
  onCell?: (seq: number, deptId: string) => void;
  onClearFocus?: () => void;
  compact?: boolean;
  className?: string;
}) {
  return (
    <div className={cn(compact ? 'overflow-auto' : 'overflow-x-auto', className)}>
      <table className="w-max border-collapse text-left">
        <thead>
          <tr className="border-b border-border text-[11px] text-muted-foreground">
            <th className={cn(NUM_COL, 'sticky left-0 z-20 bg-card px-2 py-1.5 font-medium')}>编号</th>
            <th className={cn(NAME_COL, 'sticky z-20 bg-card px-2 py-1.5 font-medium')}>文档</th>
            {columns.map((dept) => {
              const key = deptKey(dept);
              const stat = columnStat(cells, catalog, key);
              return (
                <th key={key}
                  className="sticky top-0 z-10 min-w-[88px] bg-card px-1 py-1.5 text-center font-medium"
                  title={dept.error || `${stat.updated} 格已更新 / 共 ${stat.total} 格`}>
                  {dept.dept_name}
                  {dept.error && <span className="ml-0.5 text-destructive">!</span>}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {catalog.map((row) => {
            const open = !compact && !!expanded?.has(row.seq);
            const focusedRow = focus?.seq === row.seq;
            return (
              <Fragment key={row.seq}>
                <tr className={cn('border-b border-border/60', open && 'bg-muted/30')}>
                  <th scope="row" onClick={compact ? undefined : () => onToggleRow?.(row.seq)}
                    className={cn(NUM_COL,
                      'sticky left-0 z-10 bg-card px-2 py-1 text-xs font-normal tabular-nums text-muted-foreground',
                      !compact && 'cursor-pointer')}>
                    {row.number}
                  </th>
                  <td onClick={compact ? undefined : () => onToggleRow?.(row.seq)}
                    className={cn(NAME_COL, 'sticky z-10 bg-card px-2 py-1 text-xs',
                      !compact && 'cursor-pointer')}>
                    <span className="flex items-center gap-1">
                      {!compact && (open
                        ? <ChevronDown className="size-3 shrink-0" />
                        : <ChevronRight className="size-3 shrink-0" />)}
                      <span className="truncate" title={row.name}>{row.name}</span>
                    </span>
                  </td>
                  {columns.map((dept) => {
                    const key = deptKey(dept);
                    const item = cells.get(`${key}-${row.seq}`);
                    return (
                      <MatrixCell
                        key={key}
                        item={item}
                        labels={labels}
                        dimmed={!compact && !!filter && item?.status !== filter}
                        focused={!compact && focusedRow && focus?.deptId === key}
                        onOpen={() => onCell?.(row.seq, key)}
                      />
                    );
                  })}
                </tr>
                {open && (
                  <tr className="border-b border-border/60 bg-muted/25">
                    <td colSpan={2 + columns.length} className="px-3 py-2">
                      <DetailBlock
                        row={row}
                        columns={columns}
                        cellMap={cells}
                        labels={labels}
                        focus={focus ?? null}
                        onClear={() => onClearFocus?.()}
                      />
                    </td>
                  </tr>
                )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

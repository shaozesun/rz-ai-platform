import {
  CheckCircle2, FileMinus, FileQuestionMark, FileX, Flag, type LucideIcon,
} from 'lucide-react';
import type { DocUpdateItem, DocUpdateStatus } from '@/api/scheduledTasks';

/** 文档更新检查的状态视觉（面板与看板共用，避免两处色表漂移）。 */
export interface DocStatusVisual {
  Icon: LucideIcon;
  chip: string;
  dot: string;
  bar: string;
}

export const STATUS_VISUALS: Record<DocUpdateStatus, DocStatusVisual> = {
  updated: {
    Icon: CheckCircle2, chip: 'bg-success/12 text-success',
    dot: 'bg-success', bar: 'bg-success',
  },
  stale: {
    Icon: FileMinus, chip: 'bg-muted text-muted-foreground',
    dot: 'bg-muted-foreground/60', bar: 'bg-muted-foreground/50',
  },
  missing: {
    Icon: FileQuestionMark, chip: 'bg-warning/12 text-warning',
    dot: 'bg-warning', bar: 'bg-warning',
  },
  failed: {
    Icon: FileX, chip: 'bg-destructive/10 text-destructive',
    dot: 'bg-destructive', bar: 'bg-destructive',
  },
  baseline: {
    Icon: Flag, chip: 'bg-chart-2/15 text-chart-2',
    dot: 'bg-chart-2', bar: 'bg-chart-2',
  },
};

/** 口径四种状态（基线是「期」的状态，不进计数条）。 */
export const STATUS_ORDER: DocUpdateStatus[] = ['updated', 'stale', 'missing', 'failed'];

interface DeptIdent {
  company_id?: string;
  company_name?: string;
  dept_id?: string;
  dept_name?: string;
}

/** 部门标识：同名部门在不同公司各有一份，键必须带公司（dept_id/名称都可能为空，用名称兜底）。 */
export const deptKey = (row: DeptIdent): string =>
  `${row.company_id || row.company_name || ''}::${row.dept_id || row.dept_name || ''}`;

/** 展示名：跨公司时带公司前缀（`廊坊 · A1`），单公司保持原样。 */
export const deptLabel = (row: DeptIdent): string => {
  const name = row.dept_name || row.dept_id || '—';
  const company = row.company_name || '';
  return company ? `${company} · ${name}` : name;
};

/** 按状态计数（选了具体部门时用过滤后的 items 现算）。 */
export function countStatus(items: DocUpdateItem[]): Partial<Record<DocUpdateStatus, number>> {
  const out: Partial<Record<DocUpdateStatus, number>> = {};
  for (const item of items) out[item.status] = (out[item.status] ?? 0) + 1;
  return out;
}

/** 快照时间 → `YYYY-MM-DD HH:mm`（浏览器按本地时区渲染）。 */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
    + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

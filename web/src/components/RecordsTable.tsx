import { useEffect, useMemo, useRef, useState, type MouseEvent as ReactMouseEvent } from 'react';
import { createPortal } from 'react-dom';
import { AlertCircle, ChevronLeft, ChevronRight, Loader2, Search, X } from 'lucide-react';
import client from '../api/client';
import { Input } from './ui/input';

interface RecordsData {
  columns: string[];
  records: Record<string, unknown>[];
}

const PAGE_SIZE = 20;
const MIN_COL_WIDTH = 80;
const MAX_COL_WIDTH = 360;
const WIDTH_SAMPLE_ROWS = 50;

/** 显示宽度：CJK/全角按 2 计、ASCII 按 1 计（与 xlsx 行高估算同口径）。 */
function charWidth(text: string): number {
  let width = 0;
  for (const ch of text) width += ch.codePointAt(0)! > 255 ? 2 : 1;
  return width;
}

/** 按内容 90 分位估算列宽，避免长文本列被挤成一行。 */
function autoWidth(col: string, rows: Record<string, unknown>[]): number {
  const sample = rows.slice(0, WIDTH_SAMPLE_ROWS);
  const widths = sample
    .map((row) => charWidth(String(row[col] ?? '')))
    .sort((a, b) => a - b);
  const p90Index = Math.min(widths.length - 1, Math.floor(widths.length * 0.9));
  const p90 = widths.length ? widths[p90Index] : 0;
  const units = Math.max(charWidth(col), p90);
  return Math.min(MAX_COL_WIDTH, Math.max(MIN_COL_WIDTH, Math.round(units * 7.2 + 24)));
}

/** 自适应列宽：空值不参与分位、覆盖全部行，长文本列给足宽度（配合多行换行）。 */
function adaptiveWidth(col: string, rows: Record<string, unknown>[]): number {
  const widths = rows
    .map((row) => String(row[col] ?? '').trim())
    .filter(Boolean)
    .map(charWidth)
    .sort((a, b) => a - b);
  const p90Index = widths.length
    ? Math.min(widths.length - 1, Math.floor(widths.length * 0.9))
    : 0;
  const p90 = widths[p90Index] ?? 0;
  const units = Math.max(charWidth(col), p90);
  const min = units > 24 ? 240 : units > 12 ? 120 : MIN_COL_WIDTH;
  const max = units > 24 ? 480 : units > 12 ? 320 : 160;
  return Math.min(max, Math.max(min, Math.round(units * 7.2 + 24)));
}

const IMAGE_COL_WIDTH = 120;

/** 图片源：平台端点（/api/…）要带 Bearer，`<img>` 带不了 → 走 axios 拉 blob；
 *  外部直链（S3 预签名）直接交给 `src`。 */
function useImageSrc(url: string) {
  const [src, setSrc] = useState('');
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    setSrc('');
    setFailed(false);
    if (!url.startsWith('/api/')) {
      setSrc(url);
      return;
    }
    let cancelled = false;
    let objectUrl = '';
    client.get(url.replace(/^\/api\/v1/, ''), { responseType: 'blob' })
      .then((resp) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(resp.data);
        setSrc(objectUrl);
      })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);
  return { src, failed, setFailed };
}

/** 图片单元格：缩略图点击看大图；平台端点失败只提示，外部链接给可点兜底（多为已过期）。 */
function ImageCell({ url, onOpen }: { url: string; onOpen: (url: string) => void }) {
  const { src, failed, setFailed } = useImageSrc(url);
  if (failed) {
    return url.startsWith('/api/') ? (
      <span className="text-xs text-muted-foreground">图片无法加载</span>
    ) : (
      <a href={url} target="_blank" rel="noreferrer" title={url}
        className="text-xs text-muted-foreground underline-offset-2 hover:text-primary hover:underline">
        图片无法加载，打开链接
      </a>
    );
  }
  if (!src) {
    return <div className="h-14 w-20 animate-pulse rounded border border-border bg-muted" />;
  }
  return (
    <button type="button" onClick={() => onOpen(url)} title={url} className="block cursor-zoom-in">
      <img src={src} alt="" onError={() => setFailed(true)}
        className="h-14 w-20 rounded border border-border object-cover" />
    </button>
  );
}

/** 大图预览：与缩略图同一套取图逻辑（鉴权端点走 blob）。 */
function PreviewImage({ url, title }: { url: string; title: string }) {
  const { src, failed, setFailed } = useImageSrc(url);
  if (failed) return <p className="text-sm text-muted-foreground">图片加载失败</p>;
  if (!src) return <div className="mx-auto h-40 w-64 animate-pulse rounded bg-muted" />;
  return (
    <img src={src} alt={title} onError={() => setFailed(true)}
      className="mx-auto max-h-[64vh] max-w-full object-contain" />
  );
}

/** 数据集可交互表格：拉 /records JSON，本地分页 + 关键字搜索 + 点击列排序。
 *  `adaptive`：宽表模式（事件统计表）——按非空内容自适应列宽、长文本多行不截断。
 *  `imageColumns`：按列名声明图片列，单元格渲染缩略图而不是 URL 文本（默认空，行为不变）。 */
export function RecordsTable(
  { url, name, adaptive = false, imageColumns = [] }: {
    url: string; name: string; adaptive?: boolean; imageColumns?: string[];
  },
) {
  const [data, setData] = useState<RecordsData | null>(null);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState<{ col: string; asc: boolean } | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError('');
    client
      .get(url.replace(/^\/api\/v1/, ''))
      .then((resp) => {
        if (cancelled) return;
        setData(resp.data);
      })
      .catch(() => {
        if (cancelled) return;
        setError('数据加载失败');
      });
    return () => {
      cancelled = true;
    };
  }, [url]);

  const filtered = useMemo(() => {
    if (!data) return [];
    let rows = data.records;
    if (query) {
      const q = query.toLowerCase();
      rows = rows.filter((r) =>
        Object.values(r).some((v) => String(v ?? '').toLowerCase().includes(q)),
      );
    }
    if (sort) {
      const { col, asc } = sort;
      rows = [...rows].sort((a, b) => {
        const av = a[col];
        const bv = b[col];
        if (av == null) return bv == null ? 0 : -1;
        if (bv == null) return 1;
        return asc ? String(av).localeCompare(String(bv)) : String(bv).localeCompare(String(av));
      });
    }
    return rows;
  }, [data, query, sort]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const pageRows = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  const cols = data?.columns?.length
    ? data.columns
    : data?.records?.length
      ? Object.keys(data.records[0])
      : [];

  // 自动列宽：按内容估算，用户拖拽过的列优先用拖拽值；图片列固定缩略图宽度
  const autoWidths = useMemo(() => {
    if (!data) return {} as Record<string, number>;
    const columns = data.columns?.length ? data.columns : Object.keys(data.records[0] ?? {});
    const measure = adaptive ? adaptiveWidth : autoWidth;
    return Object.fromEntries(columns.map((col) => [
      col,
      imageColumns.includes(col) ? IMAGE_COL_WIDTH : measure(col, data.records),
    ]));
  }, [data, adaptive, imageColumns]);

  // 点单元格看全文（长文本 3 行限高）；图片列点开看大图
  const [expanded, setExpanded] = useState<{ title: string; text: string; image?: string } | null>(
    null,
  );
  useEffect(() => {
    if (!expanded) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setExpanded(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [expanded]);

  const toggleSort = (col: string) => {
    setSort((s) => (s?.col === col ? { col, asc: !s.asc } : { col, asc: true }));
    setPage(1);
  };

  // 列宽拖拽：表头右侧热区（双击该列复位）。首次拖动先量一遍实际宽度，
  // 避免切换 table-layout: fixed 后其余列跳成等宽。
  const [widths, setWidths] = useState<Record<string, number>>({});
  const [resizing, setResizing] = useState<{ col: string; startX: number; startW: number } | null>(
    null,
  );
  const headerRefs = useRef<Record<string, HTMLTableCellElement | null>>({});

  const startResize = (e: ReactMouseEvent<HTMLSpanElement>, col: string) => {
    e.preventDefault();
    e.stopPropagation();
    const measured: Record<string, number> = {};
    cols.forEach((c) => {
      const th = headerRefs.current[c];
      measured[c] = th ? Math.round(th.getBoundingClientRect().width) : MIN_COL_WIDTH;
    });
    const next = { ...measured, ...widths };
    setWidths(next);
    setResizing({ col, startX: e.clientX, startW: next[col] ?? MIN_COL_WIDTH });
  };

  useEffect(() => {
    if (!resizing) return;
    const prevCursor = document.body.style.cursor;
    const prevSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const onMove = (e: MouseEvent) => {
      const w = Math.max(MIN_COL_WIDTH, resizing.startW + e.clientX - resizing.startX);
      setWidths((prev) => ({ ...prev, [resizing.col]: w }));
    };
    const onUp = () => setResizing(null);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = prevCursor;
      document.body.style.userSelect = prevSelect;
    };
  }, [resizing]);

  const resetWidths = (e: ReactMouseEvent<HTMLSpanElement>) => {
    e.stopPropagation();
    setWidths({});
  };

  const widthOf = (col: string) => widths[col] ?? autoWidths[col] ?? MIN_COL_WIDTH;
  const totalWidth = cols.reduce((sum, col) => sum + widthOf(col), 0);

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background text-[13px]">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <span className="min-w-0 flex-1 truncate text-sm font-semibold">{name}</span>
        {data && <span className="shrink-0 text-muted-foreground">共 {filtered.length} 行</span>}
        <div className="relative shrink-0">
          <Search className="pointer-events-none absolute left-2 top-1/2 size-3 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => { setQuery(e.target.value); setPage(1); }}
            placeholder="搜索…"
            className="h-8 w-44 pl-6"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-auto">
        {error ? (
          <div className="flex items-center gap-1.5 p-4 text-destructive">
            <AlertCircle className="size-3.5 shrink-0" />
            {error}
          </div>
        ) : !data ? (
          <div className="flex items-center justify-center p-6">
            <Loader2 className="size-4 animate-spin text-muted-foreground" />
          </div>
        ) : (
          <table
            className="border-collapse"
            style={totalWidth
              ? { tableLayout: 'fixed', width: totalWidth, minWidth: '100%' }
              : undefined}
          >
            <thead className="sticky top-0 z-10 bg-muted">
              <tr>
                {cols.map((c) => (
                  <th
                    key={c}
                    ref={(el) => { headerRefs.current[c] = el; }}
                    onClick={() => toggleSort(c)}
                    style={{ width: widthOf(c) }}
                    className="relative cursor-pointer select-none whitespace-nowrap border-b border-border px-3 py-2.5 text-left font-medium hover:bg-accent/50"
                  >
                    {c} {sort?.col === c ? (sort.asc ? '↑' : '↓') : ''}
                    <span
                      onMouseDown={(e) => startResize(e, c)}
                      onClick={(e) => e.stopPropagation()}
                      onDoubleClick={resetWidths}
                      title="拖动调整列宽，双击恢复自动宽度"
                      className="absolute inset-y-0 right-0 w-1.5 cursor-col-resize bg-transparent transition-colors hover:bg-primary/50"
                    />
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pageRows.map((r, i) => (
                <tr key={i} className="border-b border-border/50 last:border-0 even:bg-muted/25 hover:bg-muted/50">
                  {cols.map((c) => {
                    const text = String(r[c] ?? '');
                    if (imageColumns.includes(c)) {
                      return (
                        <td key={c} style={{ width: widthOf(c) }} className="px-3 py-2 align-top">
                          {text && (
                            <ImageCell url={text}
                              onOpen={(u) => setExpanded({ title: '图片预览', text: u, image: u })} />
                          )}
                        </td>
                      );
                    }
                    return (
                      <td
                        key={c}
                        style={{ width: widthOf(c) }}
                        className="px-3 py-2 align-top"
                      >
                        <div
                          title={text}
                          onClick={text ? () => setExpanded({ title: c, text }) : undefined}
                          className={
                            (adaptive
                              ? 'whitespace-pre-wrap break-words'
                              : 'line-clamp-3 whitespace-pre-wrap break-words')
                            + (text ? ' cursor-pointer hover:text-primary' : '')
                          }
                        >
                          {text}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex items-center gap-1.5 border-t border-border px-4 py-2.5">
        <button
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={page <= 1}
          className="rounded px-2 py-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-40"
        >
          <ChevronLeft className="size-3.5" />
        </button>
        <span className="text-muted-foreground">
          {page} / {totalPages}
        </span>
        <button
          onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          disabled={page >= totalPages}
          className="rounded px-2 py-1 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-40"
        >
          <ChevronRight className="size-3.5" />
        </button>
      </div>

      {expanded && createPortal(
        <div
          className="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 p-6"
          onClick={() => setExpanded(null)}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-label={expanded.title}
            className="flex max-h-[75vh] w-full max-w-3xl flex-col overflow-hidden rounded-xl border border-border bg-background shadow-xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-2.5">
              <span className="truncate text-sm font-semibold">{expanded.title}</span>
              <div className="flex shrink-0 items-center gap-2">
                {expanded.image && !expanded.image.startsWith('/api/') && (
                  <a href={expanded.image} target="_blank" rel="noreferrer"
                    className="text-xs text-primary hover:underline">
                    在新窗口打开
                  </a>
                )}
                <button
                  onClick={() => setExpanded(null)}
                  aria-label="关闭"
                  className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
                >
                  <X className="size-4" />
                </button>
              </div>
            </div>
            <div className="min-h-0 flex-1 overflow-auto px-4 py-3 leading-relaxed">
              {expanded.image ? (
                <PreviewImage url={expanded.image} title={expanded.title} />
              ) : (
                <div className="whitespace-pre-wrap break-words">{expanded.text}</div>
              )}
            </div>
          </div>
        </div>,
        document.body,
      )}
    </div>
  );
}

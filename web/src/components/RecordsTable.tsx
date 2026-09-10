import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, ChevronLeft, ChevronRight, Loader2, Search } from 'lucide-react';
import client from '../api/client';
import { Input } from './ui/input';

interface RecordsData {
  columns: string[];
  records: Record<string, unknown>[];
}

const PAGE_SIZE = 20;

/** 数据集可交互表格：拉 /records JSON，本地分页 + 关键字搜索 + 点击列排序 */
export function RecordsTable({ url, name }: { url: string; name: string }) {
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

  const toggleSort = (col: string) => {
    setSort((s) => (s?.col === col ? { col, asc: !s.asc } : { col, asc: true }));
    setPage(1);
  };

  return (
    <div className="flex h-full flex-col overflow-hidden bg-background text-xs">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2">
        <span className="min-w-0 flex-1 truncate text-sm font-semibold">{name}</span>
        {data && <span className="shrink-0 text-muted-foreground">共 {filtered.length} 行</span>}
        <div className="relative shrink-0">
          <Search className="pointer-events-none absolute left-2 top-1/2 size-3 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={query}
            onChange={(e) => { setQuery(e.target.value); setPage(1); }}
            placeholder="搜索…"
            className="h-7 w-44 pl-6"
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
          <table className="w-full border-collapse">
            <thead className="sticky top-0 z-10 bg-muted/90 backdrop-blur">
              <tr>
                {cols.map((c) => (
                  <th
                    key={c}
                    onClick={() => toggleSort(c)}
                    className="cursor-pointer select-none whitespace-nowrap border-b border-border px-2.5 py-1.5 text-left font-medium hover:bg-accent/50"
                  >
                    {c} {sort?.col === c ? (sort.asc ? '↑' : '↓') : ''}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {pageRows.map((r, i) => (
                <tr key={i} className="border-b border-border/50 last:border-0 hover:bg-muted/30">
                  {cols.map((c) => (
                    <td key={c} className="max-w-48 truncate whitespace-nowrap px-2.5 py-1">
                      {String(r[c] ?? '')}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="flex items-center gap-1.5 border-t border-border px-4 py-1.5">
        <button
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          disabled={page <= 1}
          className="rounded px-1.5 py-0.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-40"
        >
          <ChevronLeft className="size-3.5" />
        </button>
        <span className="text-muted-foreground">
          {page} / {totalPages}
        </span>
        <button
          onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
          disabled={page >= totalPages}
          className="rounded px-1.5 py-0.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground disabled:opacity-40"
        >
          <ChevronRight className="size-3.5" />
        </button>
      </div>
    </div>
  );
}

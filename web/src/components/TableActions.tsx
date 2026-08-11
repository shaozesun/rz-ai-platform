import { useState, useCallback, useRef } from 'react';
import { Copy, Download, Check, Table2 } from 'lucide-react';
import { Button } from '@/components/ui/button';

function tableToCSV(table: HTMLTableElement): string {
  const rows = table.querySelectorAll('tr');
  const data: string[][] = [];
  for (const row of rows) {
    const cells = row.querySelectorAll('th, td');
    data.push(Array.from(cells).map((cell) => {
      let text = (cell.textContent || '').trim();
      if (text.includes(',') || text.includes('"') || text.includes('\n')) {
        text = `"${text.replace(/"/g, '""')}"`;
      }
      return text;
    }));
  }
  return data.map((row) => row.join(',')).join('\n');
}

function tableToTSV(table: HTMLTableElement): string {
  const rows = table.querySelectorAll('tr');
  const data: string[][] = [];
  for (const row of rows) {
    const cells = row.querySelectorAll('th, td');
    data.push(Array.from(cells).map((cell) => (cell.textContent || '').trim() || '-'));
  }
  return data.map((row) => row.join('\t')).join('\n');
}

export default function TableActions({ children }: { children: React.ReactNode }) {
  const wrapperRef = useRef<HTMLDivElement>(null);
  const [copied, setCopied] = useState(false);

  const getTable = useCallback((): HTMLTableElement | null => {
    return wrapperRef.current?.querySelector('table') || null;
  }, []);

  const handleCopy = useCallback(() => {
    const table = getTable();
    if (!table) return;
    navigator.clipboard.writeText(tableToTSV(table)).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [getTable]);

  const handleDownloadCSV = useCallback(() => {
    const table = getTable();
    if (!table) return;
    const csv = tableToCSV(table);
    const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'table.csv';
    a.click();
    URL.revokeObjectURL(url);
  }, [getTable]);

  return (
    <div ref={wrapperRef} className="my-4 rounded-lg border border-border overflow-hidden">
      <div className="flex items-center justify-between bg-muted/50 px-3 py-1.5 border-b border-border">
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <Table2 className="size-3" />
          表格
        </span>
        <div className="flex gap-1.5">
          <Button
            size="sm"
            variant="outline"
            onClick={handleCopy}
            className="h-7 px-2 text-xs"
          >
            {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
            {copied ? '已复制' : '复制'}
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={handleDownloadCSV}
            className="h-7 px-2 text-xs"
          >
            <Download className="size-3" />
            CSV
          </Button>
        </div>
      </div>
      <div className="overflow-x-auto">
        {children}
      </div>
    </div>
  );
}

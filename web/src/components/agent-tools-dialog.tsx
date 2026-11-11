import { useCallback, useEffect, useMemo, useState } from 'react';
import { Loader2, Search, Wrench, X } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { listTools, type ToolGroup, type ToolItem, type ToolVisibility } from '@/api/tools';

const VISIBILITY: Record<ToolVisibility, { label: string; dot: string }> = {
  always: { label: '常驻可用', dot: 'bg-emerald-500' },
  on_demand: { label: '按需检索', dot: 'bg-muted-foreground/40' },
  mode_gated: { label: '按任务模式', dot: 'bg-amber-500' },
};

function matches(tool: ToolItem, keyword: string): boolean {
  const parts = [tool.title, tool.name, tool.summary, tool.category_label];
  return parts.join(' ').toLowerCase().includes(keyword);
}

/** 工具行：照 DeerFlow 设置页的 Item 长条（左图标 + 标题/说明，右操作区）。 */
function ToolRow({ tool }: { tool: ToolItem }) {
  const visibility = VISIBILITY[tool.visibility] ?? VISIBILITY.on_demand;
  return (
    <div
      title={tool.description}
      className={cn(
        'flex flex-wrap items-center gap-4 rounded-md border border-border p-4 text-sm',
        'transition-colors hover:bg-accent/50',
      )}
    >
      <div className="flex size-9 shrink-0 items-center justify-center rounded-lg border bg-muted">
        <Wrench className="size-4" />
      </div>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-sm leading-snug font-medium">{tool.title}</span>
          <span className="rounded-md bg-muted px-2 py-0.5 text-xs text-muted-foreground">
            {tool.category_label || '内置'}
          </span>
        </div>
        <p className="line-clamp-2 text-sm leading-normal text-muted-foreground">
          {tool.summary || '—'}
        </p>
      </div>
      <div className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
        <span className={cn('size-1.5 rounded-full', visibility.dot)} />
        {visibility.label}
      </div>
    </div>
  );
}

/** 工具目录小窗：只读列出智能体当前可调用的全部工具，按平台分组。 */
export function AgentToolsDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [groups, setGroups] = useState<ToolGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setGroups(await listTools());
      setLoaded(true);
    } catch {
      setError('工具列表加载失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  }, []);

  // 首次打开才请求，关掉再开沿用已加载的数据
  useEffect(() => {
    if (open && !loaded && !loading) void load();
  }, [open, loaded, loading, load]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  const visible = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return groups
      .map((group) => ({
        ...group,
        tools: keyword ? group.tools.filter((tool) => matches(tool, keyword)) : group.tools,
      }))
      .filter((group) => group.tools.length > 0);
  }, [groups, query]);

  const total = visible.reduce((sum, group) => sum + group.tools.length, 0);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-foreground/30" onClick={onClose} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="工具目录"
        className={cn(
          'relative z-10 flex max-h-[85vh] w-full max-w-3xl flex-col',
          'rounded-xl border border-border bg-card shadow-lg',
        )}
      >
        <header
          className={cn(
            'flex shrink-0 flex-wrap items-end justify-between gap-3',
            'border-b border-border px-5 py-4',
          )}
        >
          <div className="min-w-0">
            <h3 className="text-lg font-semibold tracking-tight">工具目录</h3>
            <p className="mt-1 text-sm leading-6 text-muted-foreground">
              智能体当前可调用的全部工具（只读）。可用性由部署开关与角色权限决定。
            </p>
          </div>
          <div className="flex items-center gap-2">
            <div className="relative w-56 sm:w-64">
              <Search
                className={cn(
                  'pointer-events-none absolute top-1/2 left-3 size-4',
                  '-translate-y-1/2 text-muted-foreground',
                )}
              />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索工具标题、分类或说明"
                className="h-9 rounded-xl bg-muted/30 pl-9 shadow-none"
              />
            </div>
            <button
              onClick={onClose}
              className="rounded-md p-1 text-muted-foreground hover:bg-accent"
              aria-label="关闭工具目录"
            >
              <X className="size-4" />
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5">
          {loading ? (
            <div className="flex items-center justify-center py-20">
              <Loader2 className="size-6 animate-spin text-muted-foreground" />
            </div>
          ) : error ? (
            <div
              className={cn(
                'flex items-center justify-between gap-3 rounded-lg bg-destructive/10',
                'px-3 py-2 text-sm text-destructive',
              )}
            >
              <span>{error}</span>
              <button
                onClick={() => void load()}
                className="shrink-0 underline-offset-2 hover:underline"
              >
                重试
              </button>
            </div>
          ) : total === 0 ? (
            <p className="py-20 text-center text-sm text-muted-foreground">
              {query.trim() ? '没有匹配的工具' : '暂无可用工具'}
            </p>
          ) : (
            <div className="space-y-8">
              <p className="text-xs text-muted-foreground">
                {query.trim() ? `命中 ${total} 个工具` : `共 ${total} 个工具`}
              </p>
              {visible.map((group) => (
                <section key={group.key}>
                  <div>
                    <h4 className="text-base font-semibold tracking-tight">
                      {group.label}
                      <span className="ml-2 text-xs font-normal text-muted-foreground">
                        {group.tools.length}
                      </span>
                    </h4>
                    {group.description && (
                      <p
                        className={cn(
                          'mt-1.5 line-clamp-2 text-sm leading-6',
                          'text-muted-foreground',
                        )}
                      >
                        {group.description}
                      </p>
                    )}
                  </div>
                  <div className="mt-4 space-y-3">
                    {group.tools.map((tool) => (
                      <ToolRow key={tool.name} tool={tool} />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

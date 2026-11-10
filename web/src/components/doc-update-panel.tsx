import { useCallback, useEffect, useState } from 'react';
import {
  ArrowUpRight, CheckCircle2, FileCheck2, FileSearch, FileX,
  Loader2, RefreshCw,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { FilterSelect } from '@/components/filter-select';
import { cn } from '@/lib/utils';
import { DocUpdateBoard } from '@/components/doc-update-board';
import {
  STATUS_ORDER, STATUS_VISUALS, countStatus, deptKey, deptLabel, fmtTime,
} from '@/components/doc-update-status';
import {
  getDocUpdateOverview, triggerDocUpdateSync,
  type DocUpdateItem, type DocUpdateOverview,
} from '@/api/scheduledTasks';

/** 只按 §口径 展示四种状态；基线期另说。
 *  轮询期间按钮禁用，后端租约释放即停（租约 120s + 30s 心跳，进程被 kill 也会自愈）。 */
const POLL_INTERVAL = 3000;

function ItemRow({ item, showDept }: { item: DocUpdateItem; showDept: boolean }) {
  const age = item.changed_days_ago == null ? '—' : `${item.changed_days_ago} 天前`;
  return (
    <tr className="border-b border-border/60 last:border-0">
      {showDept && (
        <td className="whitespace-nowrap px-3 py-1.5 text-xs">{deptLabel(item)}</td>
      )}
      <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
        {item.number}
      </td>
      <td className="px-3 py-1.5 text-xs">{item.name}</td>
      <td className="px-3 py-1.5">
        <span className={cn('inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[11px]',
          STATUS_VISUALS[item.status].chip)}>
          <span className={cn('size-1.5 rounded-full', STATUS_VISUALS[item.status].dot)} />
          {item.status_label}
        </span>
      </td>
      <td className="px-3 py-1.5 text-xs tabular-nums text-muted-foreground">{item.file_count}</td>
      <td className="whitespace-nowrap px-3 py-1.5 text-xs tabular-nums text-muted-foreground">
        {age}
      </td>
      <td className="max-w-48 truncate px-3 py-1.5 text-xs text-muted-foreground/80"
        title={item.error || undefined}>{item.error || ''}</td>
    </tr>
  );
}

export function DocUpdatePanel() {
  const [data, setData] = useState<DocUpdateOverview | null>(null);
  const [notice, setNotice] = useState('');
  const [open, setOpen] = useState(false);
  const [period, setPeriod] = useState('');
  // 默认选第一个部门：一进来就是该部门的 25 行列表
  const [dept, setDept] = useState('');

  const load = useCallback(async () => {
    try {
      setData(await getDocUpdateOverview(period));
    } catch {
      // 面板是附加信息，取不到不打断合规页
    }
  }, [period]);

  useEffect(() => { void load(); }, [load]);

  // 部门列到达后默认选第一个；之后保持用户的选择
  const firstDept = data?.dept_columns?.length ? deptKey(data.dept_columns[0]) : '';
  useEffect(() => { if (firstDept) setDept(firstDept); }, [firstDept]);

  // 后端持有租约期间（running=true）每 3s 拉一次；租约释放即停，以后端为准
  useEffect(() => {
    if (!data?.running) return;
    const timer = window.setInterval(() => {
      void (async () => {
        try {
          setData(await getDocUpdateOverview(period));
        } catch {
          // 网络抖动继续轮询
        }
      })();
    }, POLL_INTERVAL);
    return () => window.clearInterval(timer);
  }, [data?.running, period]);

  const onCheck = async () => {
    setNotice('');
    try {
      const res = await triggerDocUpdateSync();
      if (!res.started) {
        setNotice(res.reason || '已有一次检查在跑，请稍候');
        await load();
        return;
      }
      setPeriod('');   // 新跑的是当前月，回到最新一期
      setData((cur) => (cur ? { ...cur, running: true } : cur));
    } catch {
      setNotice('启动检查失败，请稍后重试');
    }
  };

  if (!data?.enabled) return null;

  const verdict = data.verdict;
  const labels = verdict?.labels ?? {};
  const deptColumns = data.dept_columns ?? [];
  const periods = data.periods ?? [];
  const deptOptions = deptColumns.map((col) => ({
    value: deptKey(col), label: deptLabel(col),
  }));
  const periodOptions = [
    { value: '', label: '最新' },
    ...periods.map((p) => ({ value: p.period_key, label: p.period_label || p.period_key })),
  ];
  const isDept = verdict?.scope === 'dept';
  const deptFilter = isDept ? dept : '';
  const allItems = verdict?.items ?? [];
  const items = deptFilter ? allItems.filter((i) => deptKey(i) === deptFilter) : allItems;
  const showDept = isDept && !deptFilter;
  const counts = deptFilter ? countStatus(items) : (verdict?.counts ?? {});
  const deptCount = deptColumns.length || data.latest?.dept_count || 0;
  const companyCount = new Set(deptColumns.map((c) => c.company_id || c.company_name || '')).size;
  const selectedDept = deptColumns.find((c) => deptKey(c) === deptFilter);
  const deptTitle = selectedDept ? deptLabel(selectedDept)
    : (items[0] ? deptLabel(items[0]) : '');
  const scopeTitle = companyCount > 1
    ? `${companyCount} 家公司 · ${deptCount} 个部门` : `${deptCount} 个部门`;
  const baseline = verdict?.baseline ?? true;
  const progress = data.progress;
  const pct = progress && progress.total > 0
    ? Math.round((progress.done / progress.total) * 100) : 0;
  const startedAt = progress?.started_at
    ? new Date(progress.started_at * 1000).toLocaleTimeString('zh-CN', {
      hour: '2-digit', minute: '2-digit',
    })
    : '';

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
        <span className="flex size-8 items-center justify-center rounded-lg bg-chart-2/15 text-chart-2">
          <FileCheck2 className="size-4" />
        </span>
        <span className="text-base font-semibold">文档更新检查</span>
        <Badge variant="outline">
          {isDept ? `月度 · 25 项 × ${scopeTitle}` : '月度 · 25 项'}
        </Badge>
        <span className="text-xs tabular-nums text-muted-foreground">
          {data.latest ? `快照 ${fmtTime(data.latest.taken_at)}` : '尚无快照'}
        </span>
        <div className="ml-auto flex items-center gap-1">
          <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); setOpen(true); }}>
            查看全部 <ArrowUpRight className="size-4" />
          </Button>
          <Button size="sm" variant="outline"
            disabled={data.running} onClick={(e) => { e.stopPropagation(); void onCheck(); }}>
            {data.running
              ? <Loader2 className="size-4 animate-spin" />
              : <RefreshCw className="size-4" />}
            {data.running ? '检查中' : '立即检查'}
          </Button>
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {STATUS_ORDER.map((key) => (
          <span key={key}
            className={cn('inline-flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs',
              STATUS_VISUALS[key].chip)}>
            <span className={cn('size-1.5 rounded-full', STATUS_VISUALS[key].dot)} />
            {labels[key] ?? (key === 'updated' ? '已更新' : key === 'stale' ? '未更新'
              : key === 'missing' ? '未找到' : '下载失败')}
            <b className="tabular-nums">{counts[key] ?? 0}</b>
          </span>
        ))}
        <span className="text-xs text-muted-foreground">
          {isDept
            ? deptFilter
              ? `${deptTitle} · ${items.length} 项 · ${verdict?.period_label ?? ''}`
              : `${scopeTitle} × ${data.catalog?.length ?? 25} 项 · 共 ${verdict?.total ?? 0} 格 · ${verdict?.period_label ?? ''}`
            : `共 ${verdict?.total ?? 0} 项 · ${verdict?.period_label ?? ''}`}
          {data.latest && baseline
            ? (isDept ? ' · 首期基线（原地更新型），下期起比对' : ' · 首期基线，下期起比对')
            : ''}
        </span>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2" onClick={(e) => e.stopPropagation()}>
        {deptColumns.length > 0 && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            部门
            <FilterSelect ariaLabel="按部门筛选" value={dept}
              options={deptOptions} onChange={(v) => setDept(String(v))} />
          </span>
        )}
        {periods.length > 1 && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            期间
            <FilterSelect ariaLabel="按期间筛选" value={period}
              options={periodOptions} onChange={(v) => setPeriod(String(v))} />
          </span>
        )}
      </div>

      {data.missing_period ? (
        <p className="mt-3 rounded-md border border-border px-3 py-3 text-xs text-muted-foreground">
          该期没有快照（{data.missing_period}），请换一个期间。
        </p>
      ) : !data.latest ? (
        <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
          逐公司逐部门核对 25 条月度文档：有月份文件夹的看本期月份有没有文件，
          无月份文件夹的比对内容哈希。点击「立即检查」建立首期基线，之后每月 10 日自动比对。
        </p>
      ) : items.length > 0 ? (
        <div className="mt-3 max-h-[16vh] min-h-[120px] overflow-y-auto rounded-lg border border-border/60">
          <table className="w-full border-collapse text-left">
            <thead className="sticky top-0 z-10 bg-card">
              <tr className="border-b border-border text-[11px] text-muted-foreground">
                {showDept && <th className="px-3 py-1.5 font-medium">部门</th>}
                <th className="px-3 py-1.5 font-medium">编号</th>
                <th className="px-3 py-1.5 font-medium">文档</th>
                <th className="px-3 py-1.5 font-medium">状态</th>
                <th className="px-3 py-1.5 font-medium">文件</th>
                <th className="px-3 py-1.5 font-medium">上次变更</th>
                <th className="px-3 py-1.5 font-medium">说明</th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <ItemRow key={`${deptKey(item)}-${item.seq ?? item.number}`}
                  item={item} showDept={showDept} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="mt-2 text-xs text-muted-foreground">该范围内暂无检查结果。</p>
      )}

      {(verdict?.warnings?.length ?? 0) > 0 && (
        <p className="mt-2 text-[11px] leading-relaxed text-warning">
          待复核 {verdict?.warnings.length} 项（档案类型/大小与实际字节不符）：
          {verdict?.warnings.slice(0, 3).map((w) => w.number).join('、')}
          {(verdict?.warnings.length ?? 0) > 3 ? ' 等' : ''}
        </p>
      )}
      <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground/80">
        「未找到」「下载失败」不等于「未更新」：前者是未查询到相关文件，
        后者是取不到字节或类型校验不过，需人工复核。
      </p>

      {notice && <p className="mt-2 text-xs text-muted-foreground">{notice}</p>}
      {data.running && (
        <p className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground">
          <FileSearch className="size-3.5" />
          {progress && progress.total > 0
            ? `正在逐部门检查 · ${pct}%（${progress.current || '准备中'}）${startedAt ? ` · ${startedAt} 开始` : ''}`
            : '正在逐部门检查，约 5~15 分钟'}
        </p>
      )}
      {!data.running && counts.failed ? (
        <p className="mt-1 flex items-center gap-1 text-[11px] text-destructive/80">
          <FileX className="size-3.5" /> {counts.failed} {isDept ? '格' : '项'}下载失败
        </p>
      ) : null}
      {!data.running && counts.updated ? (
        <p className="mt-1 flex items-center gap-1 text-[11px] text-success">
          <CheckCircle2 className="size-3.5" /> {counts.updated} {isDept ? '格' : '项'}本月已更新
        </p>
      ) : null}
      <p className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground/70 opacity-0 transition-opacity group-hover:opacity-100">
        查看全部 <ArrowUpRight className="size-3.5" />
      </p>
    </div>
    {open && (
      <DocUpdateBoard
        data={data}
        running={data.running}
        notice={notice}
        onCheck={() => void onCheck()}
        onClose={() => setOpen(false)}
        dept={dept}
        onDeptChange={setDept}
        period={period}
        onPeriodChange={setPeriod}
      />
    )}
    </>
  );
}

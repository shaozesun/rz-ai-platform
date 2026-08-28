import { useEffect, useState } from 'react';
import {
  Plus, Trash2, Play, History, ChevronDown, Loader2, Download, Clock,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Select } from '@/components/ui/select';
import { getAccessToken } from '@/api/client';
import {
  listTasks, createTask, updateTask, deleteTask, triggerTask, listRuns,
  type ScheduledTask, type ScheduledTaskRun, type ScheduledTaskInput,
} from '@/api/scheduledTasks';

const RUN_LABEL: Record<string, string> = {
  pending: '排队中', running: '运行中', success: '成功', failed: '失败',
};
const RUN_COLOR: Record<string, 'default' | 'primary' | 'success' | 'destructive'> = {
  pending: 'default', running: 'primary', success: 'success', failed: 'destructive',
};

function scheduleSummary(t: ScheduledTask): string {
  if (t.schedule_type === 'once') return `一次性 ${t.once_at ? new Date(t.once_at).toLocaleString('zh-CN') : ''}`;
  if (t.schedule_type === 'interval') {
    const s = t.interval_seconds || 3600;
    if (s % 3600 === 0) return `每 ${s / 3600} 小时`;
    if (s % 60 === 0) return `每 ${s / 60} 分钟`;
    return `每 ${s} 秒`;
  }
  return `Cron ${t.cron_expr || ''}`;
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString('zh-CN');
}

async function downloadArtifact(url: string) {
  const token = getAccessToken();
  const resp = await fetch(url, { headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!resp.ok) throw new Error('下载失败');
  const blob = await resp.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = url.split('/').pop() || 'artifact';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

const emptyForm: ScheduledTaskInput = {
  name: '', description: '', schedule_type: 'interval',
  interval_seconds: 3600, once_at: null, cron_expr: null,
  prompt: '', task_mode: 'normal', thread_strategy: 'new', enabled: true,
};

export default function ScheduledTasksPage() {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<ScheduledTask | null>(null);
  const [form, setForm] = useState<ScheduledTaskInput>(emptyForm);
  const [busy, setBusy] = useState(false);
  const [runsByTask, setRunsByTask] = useState<Record<string, ScheduledTaskRun[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const load = async () => {
    setLoading(true);
    try {
      setTasks(await listTasks());
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const startCreate = () => {
    setEditing(null);
    setForm(emptyForm);
    setShowForm(true);
  };

  const startEdit = (t: ScheduledTask) => {
    setEditing(t);
    setForm({
      name: t.name, description: t.description, schedule_type: t.schedule_type,
      once_at: t.once_at, interval_seconds: t.interval_seconds, cron_expr: t.cron_expr,
      prompt: t.prompt, task_mode: t.task_mode, thread_strategy: t.thread_strategy, enabled: t.enabled,
    });
    setShowForm(true);
  };

  const submit = async () => {
    setBusy(true);
    try {
      const payload: ScheduledTaskInput = { ...form };
      if (payload.schedule_type === 'once') { payload.interval_seconds = null; payload.cron_expr = null; }
      if (payload.schedule_type === 'interval') { payload.once_at = null; payload.cron_expr = null; }
      if (payload.schedule_type === 'cron') { payload.once_at = null; payload.interval_seconds = null; }
      if (editing) {
        await updateTask(editing.task_id, payload);
      } else {
        await createTask(payload);
      }
      setShowForm(false);
      await load();
    } finally {
      setBusy(false);
    }
  };

  const onTrigger = async (t: ScheduledTask) => {
    await triggerTask(t.task_id);
  };

  const onDelete = async (t: ScheduledTask) => {
    if (!window.confirm(`确定删除定时任务「${t.name}」？`)) return;
    await deleteTask(t.task_id);
    await load();
  };

  const toggleRuns = async (t: ScheduledTask) => {
    const next = new Set(expanded);
    if (next.has(t.task_id)) {
      next.delete(t.task_id);
    } else {
      next.add(t.task_id);
      if (!runsByTask[t.task_id]) setRunsByTask((m) => ({ ...m, [t.task_id]: [] }));
      try {
        const runs = await listRuns(t.task_id);
        setRunsByTask((m) => ({ ...m, [t.task_id]: runs }));
      } catch {
        // ignore
      }
    }
    setExpanded(next);
  };

  return (
    <PlatformShell title="定时任务" description="到点自动跑一轮 Agent，生成巡检/数据报告">
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <Button onClick={startCreate}><Plus className="size-4" /> 新建任务</Button>
        </div>

        {showForm && (
          <Card>
            <CardContent className="space-y-4 p-4">
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">任务名称</label>
                  <Input value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="如：每日巡检报告" />
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">调度类型</label>
                  <Select value={form.schedule_type} onChange={(e) => setForm({ ...form, schedule_type: e.target.value as ScheduledTaskInput['schedule_type'] })}>
                    <option value="interval">间隔（每 N 秒/分钟/小时）</option>
                    <option value="cron">Cron 表达式</option>
                    <option value="once">一次性</option>
                  </Select>
                </div>
              </div>

              {form.schedule_type === 'interval' && (
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">间隔（秒，如 3600=每小时、86400=每天）</label>
                  <Input type="number" value={form.interval_seconds ?? 3600} onChange={(e) => setForm({ ...form, interval_seconds: Number(e.target.value) })} />
                </div>
              )}
              {form.schedule_type === 'cron' && (
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">Cron 表达式（如 0 8 * * * = 每天 8 点）</label>
                  <Input value={form.cron_expr ?? ''} onChange={(e) => setForm({ ...form, cron_expr: e.target.value })} placeholder="0 8 * * *" />
                </div>
              )}
              {form.schedule_type === 'once' && (
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">运行时间（如 2026-08-26T08:00:00）</label>
                  <Input type="datetime-local" value={form.once_at ? form.once_at.slice(0, 16) : ''} onChange={(e) => setForm({ ...form, once_at: e.target.value ? new Date(e.target.value).toISOString() : null })} />
                </div>
              )}

              <div className="space-y-1.5">
                <label className="text-xs text-muted-foreground">任务指令（作为消息喂给 Agent）</label>
                <Textarea rows={3} value={form.prompt} onChange={(e) => setForm({ ...form, prompt: e.target.value })} placeholder="如：查询昨日各机房告警并生成巡检报告" />
              </div>

              <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">任务模式</label>
                  <Select value={form.task_mode} onChange={(e) => setForm({ ...form, task_mode: e.target.value })}>
                    <option value="normal">常规</option>
                    <option value="full_analysis">全量分析</option>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">会话策略</label>
                  <Select value={form.thread_strategy} onChange={(e) => setForm({ ...form, thread_strategy: e.target.value as ScheduledTaskInput['thread_strategy'] })}>
                    <option value="new">每次新会话</option>
                    <option value="reuse">固定会话（累积历史）</option>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">启用</label>
                  <Select value={String(form.enabled)} onChange={(e) => setForm({ ...form, enabled: e.target.value === 'true' })}>
                    <option value="true">启用</option>
                    <option value="false">停用</option>
                  </Select>
                </div>
              </div>

              <div className="flex justify-end gap-2">
                <Button variant="ghost" onClick={() => setShowForm(false)}>取消</Button>
                <Button onClick={submit} disabled={busy || !form.name || !form.prompt}>
                  {busy ? <Loader2 className="size-4 animate-spin" /> : null} {editing ? '保存' : '创建'}
                </Button>
              </div>
            </CardContent>
          </Card>
        )}

        {loading ? (
          <div className="flex justify-center py-12"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>
        ) : tasks.length === 0 ? (
          <div className="py-12 text-center text-sm text-muted-foreground">暂无定时任务，点「新建任务」创建</div>
        ) : (
          tasks.map((t) => (
            <Card key={t.task_id}>
              <CardContent className="space-y-3 p-4">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="font-medium">{t.name}</span>
                      <Badge variant={t.enabled ? 'success' : 'default'}>{t.enabled ? '启用' : '停用'}</Badge>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                      <span className="inline-flex items-center gap-1"><Clock className="size-3" />{scheduleSummary(t)}</span>
                      <span>下次运行：{fmtTime(t.next_run_at)}</span>
                      {t.description && <span className="truncate">{t.description}</span>}
                    </div>
                  </div>
                  <div className="flex shrink-0 items-center gap-1.5">
                    <Button size="icon-sm" variant="ghost" title="立即触发" onClick={() => onTrigger(t)}><Play className="size-4" /></Button>
                    <Button size="icon-sm" variant="ghost" title="编辑" onClick={() => startEdit(t)}>编辑</Button>
                    <Button size="icon-sm" variant="ghost" title="运行历史" onClick={() => toggleRuns(t)}>
                      {expanded.has(t.task_id) ? <ChevronDown className="size-4" /> : <History className="size-4" />}
                    </Button>
                    <Button size="icon-sm" variant="ghost" title="删除" onClick={() => onDelete(t)}><Trash2 className="size-4" /></Button>
                  </div>
                </div>

                <p className="rounded-lg bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
                  <span className="font-medium text-foreground">指令：</span>{t.prompt}
                </p>

                {expanded.has(t.task_id) && (
                  <div className="space-y-2">
                    {(runsByTask[t.task_id] ?? []).length === 0 ? (
                      <p className="py-2 text-center text-xs text-muted-foreground">暂无运行记录</p>
                    ) : (
                      (runsByTask[t.task_id] ?? []).map((r) => (
                        <div key={r.run_id} className="rounded-lg border border-border/60 px-3 py-2 text-sm">
                          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
                            <Badge variant={RUN_COLOR[r.status]}>{RUN_LABEL[r.status] || r.status}</Badge>
                            <span className="text-xs text-muted-foreground">{fmtTime(r.triggered_at)}</span>
                            {r.duration_ms > 0 && <span className="text-xs text-muted-foreground">{r.duration_ms}ms</span>}
                            <span className="ml-auto flex items-center gap-1">
                              {r.artifact_urls.map((u) => (
                                <Button key={u} size="sm" variant="outline" onClick={() => downloadArtifact(u)}>
                                  <Download className="size-3.5" /> {decodeURIComponent(u.split('/').pop() || '')}
                                </Button>
                              ))}
                            </span>
                          </div>
                          {r.status === 'failed' && r.error && <p className="mt-1 text-xs text-destructive">{r.error}</p>}
                          {r.final_reply && (
                            <p className="mt-1 whitespace-pre-wrap text-xs text-muted-foreground">{r.final_reply}</p>
                          )}
                        </div>
                      ))
                    )}
                  </div>
                )}
              </CardContent>
            </Card>
          ))
        )}
      </div>
    </PlatformShell>
  );
}

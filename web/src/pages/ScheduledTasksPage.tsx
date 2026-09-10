import { useEffect, useState } from 'react';
import {
  Plus, Trash2, Play, History, ChevronDown, Loader2, Download, Clock, MessageSquare, ArrowLeft,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Select } from '@/components/ui/select';
import { getAccessToken } from '@/api/client';
import { MessageBubble } from '@/components/ChatArea';
import type { Message } from '@/types';
import {
  listTasks, createTask, updateTask, deleteTask, triggerTask, listRuns, getRunMessages,
  type ScheduledTask, type ScheduledTaskRun, type ScheduledTaskInput,
} from '@/api/scheduledTasks';

const RUN_LABEL: Record<string, string> = {
  pending: '排队中', running: '运行中', success: '成功', failed: '失败',
};
const RUN_COLOR: Record<string, 'default' | 'primary' | 'success' | 'destructive'> = {
  pending: 'default', running: 'primary', success: 'success', failed: 'destructive',
};

// ---------- 预设式定时方案 ----------
type Preset = 'once' | 'interval' | 'daily' | 'weekly' | 'monthly' | 'custom';

interface ScheduleForm {
  preset: Preset;
  onceAt: string;            // datetime-local 值
  intervalValue: number;
  intervalUnit: 'minute' | 'hour' | 'day';
  dailyTime: string;         // HH:MM
  weeklyDow: number;         // 1=周一 .. 7=周日
  weeklyTime: string;
  monthlyDay: number;        // 1-31
  monthlyTime: string;
  cronExpr: string;
}

const DEFAULT_SCHEDULE: ScheduleForm = {
  preset: 'interval',
  onceAt: '',
  intervalValue: 1,
  intervalUnit: 'hour',
  dailyTime: '08:00',
  weeklyDow: 1,
  weeklyTime: '08:00',
  monthlyDay: 1,
  monthlyTime: '08:00',
  cronExpr: '0 8 * * *',
};

const WEEK_LABEL = ['', '周一', '周二', '周三', '周四', '周五', '周六', '周日'];

const pad = (n: number) => String(n).padStart(2, '0');

function toDatetimeLocal(iso: string): string {
  const d = new Date(iso);
  const local = new Date(d.getTime() - d.getTimezoneOffset() * 60000);
  return local.toISOString().slice(0, 16);
}

/** cron 星期：UI 1=周一..7=周日 → cron 0=周日..6=周六 */
function toCronDow(uiDow: number): number {
  return uiDow === 7 ? 0 : uiDow;
}
function toUiDow(cronDow: number): number {
  return cronDow === 0 ? 7 : cronDow;
}

function timeToCron(time: string, dow?: number, dom?: number): string {
  const [h, m] = time.split(':').map(Number);
  const M = pad(m), H = pad(h);
  if (dow !== undefined) return `${M} ${H} * * ${toCronDow(dow)}`;
  if (dom !== undefined) return `${M} ${H} ${dom} * *`;
  return `${M} ${H} * * *`;
}

/** 把预设表单转成后端调度字段 */
function scheduleToTaskInput(f: ScheduleForm): Pick<ScheduledTaskInput, 'schedule_type' | 'once_at' | 'interval_seconds' | 'cron_expr'> {
  switch (f.preset) {
    case 'once':
      return { schedule_type: 'once', once_at: f.onceAt ? new Date(f.onceAt).toISOString() : null, interval_seconds: null, cron_expr: null };
    case 'interval': {
      const mul = f.intervalUnit === 'minute' ? 60 : f.intervalUnit === 'hour' ? 3600 : 86400;
      return { schedule_type: 'interval', interval_seconds: Math.max(1, f.intervalValue) * mul, once_at: null, cron_expr: null };
    }
    case 'daily':
      return { schedule_type: 'cron', cron_expr: timeToCron(f.dailyTime), once_at: null, interval_seconds: null };
    case 'weekly':
      return { schedule_type: 'cron', cron_expr: timeToCron(f.weeklyTime, f.weeklyDow), once_at: null, interval_seconds: null };
    case 'monthly':
      return { schedule_type: 'cron', cron_expr: timeToCron(f.monthlyTime, undefined, f.monthlyDay), once_at: null, interval_seconds: null };
    default:
      return { schedule_type: 'cron', cron_expr: f.cronExpr.trim(), once_at: null, interval_seconds: null };
  }
}

/** 把已有任务的调度字段反向解析成预设表单 */
function taskToSchedule(t: ScheduledTask): ScheduleForm {
  if (t.schedule_type === 'once') {
    return { ...DEFAULT_SCHEDULE, preset: 'once', onceAt: t.once_at ? toDatetimeLocal(t.once_at) : '' };
  }
  if (t.schedule_type === 'interval') {
    const s = t.interval_seconds || 3600;
    if (s % 86400 === 0) return { ...DEFAULT_SCHEDULE, preset: 'interval', intervalValue: s / 86400, intervalUnit: 'day' };
    if (s % 3600 === 0) return { ...DEFAULT_SCHEDULE, preset: 'interval', intervalValue: s / 3600, intervalUnit: 'hour' };
    return { ...DEFAULT_SCHEDULE, preset: 'interval', intervalValue: s / 60, intervalUnit: 'minute' };
  }
  const parts = (t.cron_expr || '').trim().split(/\s+/);
  if (parts.length === 5) {
    const [M, H, dom, mo, dow] = parts;
    const time = `${pad(Number(H) || 0)}:${pad(Number(M) || 0)}`;
    if (dom === '*' && mo === '*' && dow === '*') {
      return { ...DEFAULT_SCHEDULE, preset: 'daily', dailyTime: time };
    }
    if (dom === '*' && mo === '*' && dow !== '*' && !Number.isNaN(Number(dow))) {
      return { ...DEFAULT_SCHEDULE, preset: 'weekly', weeklyDow: toUiDow(Number(dow)), weeklyTime: time };
    }
    if (dow === '*' && mo === '*' && dom !== '*' && !Number.isNaN(Number(dom))) {
      return { ...DEFAULT_SCHEDULE, preset: 'monthly', monthlyDay: Math.min(31, Number(dom) || 1), monthlyTime: time };
    }
  }
  return { ...DEFAULT_SCHEDULE, preset: 'custom', cronExpr: t.cron_expr || '' };
}

/** 调度摘要（人类文案） */
function scheduleSummary(t: ScheduledTask): string {
  if (t.schedule_type === 'once') return `一次性 ${t.once_at ? new Date(t.once_at).toLocaleString('zh-CN') : ''}`;
  if (t.schedule_type === 'interval') {
    const s = t.interval_seconds || 3600;
    if (s % 86400 === 0) return `每 ${s / 86400} 天`;
    if (s % 3600 === 0) return `每 ${s / 3600} 小时`;
    if (s % 60 === 0) return `每 ${s / 60} 分钟`;
    return `每 ${s} 秒`;
  }
  const parts = (t.cron_expr || '').trim().split(/\s+/);
  if (parts.length === 5) {
    const [M, H, dom, mo, dow] = parts;
    const time = `${pad(Number(H) || 0)}:${pad(Number(M) || 0)}`;
    if (dom === '*' && mo === '*' && dow === '*') return `每天 ${time}`;
    if (dom === '*' && mo === '*' && dow !== '*' && !Number.isNaN(Number(dow))) return `每${WEEK_LABEL[toUiDow(Number(dow))] || ''} ${time}`;
    if (dow === '*' && mo === '*' && dom !== '*' && !Number.isNaN(Number(dom))) return `每月${Number(dom)}日 ${time}`;
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

const PRESET_LABEL: Record<Preset, string> = {
  once: '一次性',
  interval: '间隔（每 N 分钟/小时/天）',
  daily: '每天固定时间',
  weekly: '每周某天固定时间',
  monthly: '每月某日固定时间',
  custom: '自定义 Cron（高级）',
};

export default function ScheduledTasksPage() {
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(true);
  const [showForm, setShowForm] = useState(false);
  const [editing, setEditing] = useState<ScheduledTask | null>(null);
  const [form, setForm] = useState<ScheduledTaskInput>({
    name: '', description: '', schedule_type: 'interval',
    interval_seconds: 3600, once_at: null, cron_expr: null,
    prompt: '', task_mode: 'normal', thread_strategy: 'new', enabled: true,
  });
  const [sched, setSched] = useState<ScheduleForm>(DEFAULT_SCHEDULE);
  const [busy, setBusy] = useState(false);
  const [runsByTask, setRunsByTask] = useState<Record<string, ScheduledTaskRun[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [triggeringId, setTriggeringId] = useState<string | null>(null);
  const [viewingRun, setViewingRun] = useState<{
    task: ScheduledTask;
    run: ScheduledTaskRun;
    messages: Message[];
    loading: boolean;
  } | null>(null);

  const openRun = async (task: ScheduledTask, r: ScheduledTaskRun) => {
    setViewingRun({ task, run: r, messages: [], loading: true });
    try {
      const msgs = await getRunMessages(task.task_id, r.run_id);
      setViewingRun((cur) => (cur ? { ...cur, messages: msgs, loading: false } : cur));
    } catch {
      setViewingRun((cur) => (cur ? { ...cur, loading: false } : cur));
    }
  };

  const closeRun = () => setViewingRun(null);

  const load = async () => {
    setLoading(true);
    try {
      setTasks((await listTasks()) ?? []);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  const startCreate = () => {
    setEditing(null);
    setForm({
      name: '', description: '', schedule_type: 'interval',
      interval_seconds: 3600, once_at: null, cron_expr: null,
      prompt: '', task_mode: 'normal', thread_strategy: 'new', enabled: true,
    });
    setSched(DEFAULT_SCHEDULE);
    setShowForm(true);
  };

  const startEdit = (t: ScheduledTask) => {
    setEditing(t);
    setForm({
      name: t.name, description: t.description, schedule_type: t.schedule_type,
      once_at: t.once_at, interval_seconds: t.interval_seconds, cron_expr: t.cron_expr,
      prompt: t.prompt, task_mode: t.task_mode, thread_strategy: t.thread_strategy, enabled: t.enabled,
    });
    setSched(taskToSchedule(t));
    setShowForm(true);
  };

  const submit = async () => {
    setBusy(true);
    try {
      const schedFields = scheduleToTaskInput(sched);
      const payload: ScheduledTaskInput = { ...form, ...schedFields };
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
    setTriggeringId(t.task_id);
    try {
      await triggerTask(t.task_id);
      // 触发后自动展开并刷新运行记录，让新运行立即可见（后端后台执行，先显示 running）
      setExpanded((prev) => new Set(prev).add(t.task_id));
      const runs = await listRuns(t.task_id);
      setRunsByTask((m) => ({ ...m, [t.task_id]: runs }));
    } catch {
      // 触发失败：保持现状
    } finally {
      setTriggeringId(null);
    }
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

  const shellTitle = viewingRun ? `运行对话 · ${viewingRun.task.name}` : '定时任务';
  const shellDesc = viewingRun
    ? `run ${viewingRun.run.run_id.slice(0, 8)} · ${fmtTime(viewingRun.run.triggered_at)} · ${RUN_LABEL[viewingRun.run.status] || viewingRun.run.status}`
    : '按预设调度计划自动执行 Agent，适用于报告生成、巡检、数据汇总等周期性自动化场景';

  return (
    <PlatformShell
      title={shellTitle}
      description={shellDesc}
      headerActionsLeft={viewingRun ? (
        <Button size="sm" variant="ghost" onClick={closeRun}><ArrowLeft className="size-4" /> 返回</Button>
      ) : undefined}
    >
      {viewingRun ? (
        <div className="flex-1 overflow-y-auto rounded-lg border bg-card p-4">
          {viewingRun.loading ? (
            <div className="flex justify-center py-10"><Loader2 className="size-6 animate-spin text-muted-foreground" /></div>
          ) : viewingRun.messages.length === 0 ? (
            <p className="py-10 text-center text-sm text-muted-foreground">暂无对话内容</p>
          ) : (
            viewingRun.messages.map((m) => (
              <MessageBubble key={m.message_id} msg={m} isStreaming={false} isLast={false} userName="我" />
            ))
          )}
        </div>
      ) : (
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
                  <label className="text-xs text-muted-foreground">定时方式</label>
                  <Select value={sched.preset} onChange={(e) => setSched({ ...sched, preset: e.target.value as Preset })}>
                    {(Object.keys(PRESET_LABEL) as Preset[]).map((p) => (
                      <option key={p} value={p}>{PRESET_LABEL[p]}</option>
                    ))}
                  </Select>
                </div>
              </div>

              {sched.preset === 'once' && (
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">运行时间</label>
                  <Input type="datetime-local" value={sched.onceAt} onChange={(e) => setSched({ ...sched, onceAt: e.target.value })} />
                </div>
              )}

              {sched.preset === 'interval' && (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="text-xs text-muted-foreground">间隔数值</label>
                    <Input type="number" min={1} value={sched.intervalValue} onChange={(e) => setSched({ ...sched, intervalValue: Number(e.target.value) })} />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-muted-foreground">单位</label>
                    <Select value={sched.intervalUnit} onChange={(e) => setSched({ ...sched, intervalUnit: e.target.value as ScheduleForm['intervalUnit'] })}>
                      <option value="minute">分钟</option>
                      <option value="hour">小时</option>
                      <option value="day">天</option>
                    </Select>
                  </div>
                </div>
              )}

              {sched.preset === 'daily' && (
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">每天几点运行</label>
                  <Input type="time" value={sched.dailyTime} onChange={(e) => setSched({ ...sched, dailyTime: e.target.value })} />
                </div>
              )}

              {sched.preset === 'weekly' && (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="text-xs text-muted-foreground">星期几</label>
                    <Select value={sched.weeklyDow} onChange={(e) => setSched({ ...sched, weeklyDow: Number(e.target.value) })}>
                      {[1, 2, 3, 4, 5, 6, 7].map((d) => <option key={d} value={d}>{WEEK_LABEL[d]}</option>)}
                    </Select>
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-muted-foreground">几点运行</label>
                    <Input type="time" value={sched.weeklyTime} onChange={(e) => setSched({ ...sched, weeklyTime: e.target.value })} />
                  </div>
                </div>
              )}

              {sched.preset === 'monthly' && (
                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <label className="text-xs text-muted-foreground">每月几号</label>
                    <Input type="number" min={1} max={31} value={sched.monthlyDay} onChange={(e) => setSched({ ...sched, monthlyDay: Math.min(31, Number(e.target.value) || 1) })} />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-xs text-muted-foreground">几点运行</label>
                    <Input type="time" value={sched.monthlyTime} onChange={(e) => setSched({ ...sched, monthlyTime: e.target.value })} />
                  </div>
                </div>
              )}

              {sched.preset === 'custom' && (
                <div className="space-y-1.5">
                  <label className="text-xs text-muted-foreground">Cron 表达式（如 0 8 * * * = 每天8点）</label>
                  <Input value={sched.cronExpr} onChange={(e) => setSched({ ...sched, cronExpr: e.target.value })} placeholder="0 8 * * *" />
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

              <div className="flex flex-col items-end gap-1.5">
                {!form.name || !form.prompt ? (
                  <p className="text-xs text-destructive">请先填写任务名称和任务指令</p>
                ) : null}
                <div className="flex gap-2">
                  <Button variant="ghost" onClick={() => setShowForm(false)}>取消</Button>
                  <Button onClick={submit} disabled={busy || !form.name || !form.prompt}>
                    {busy ? <Loader2 className="size-4 animate-spin" /> : null} {editing ? '保存' : '创建'}
                  </Button>
                </div>
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
                    <Button size="icon-sm" variant="ghost" data-tip="立即触发" onClick={() => onTrigger(t)} disabled={triggeringId === t.task_id}>
                      {triggeringId === t.task_id ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                    </Button>
                    <Button size="icon-sm" variant="ghost" data-tip="编辑" onClick={() => startEdit(t)}>编辑</Button>
                    <Button size="icon-sm" variant="ghost" data-tip="运行历史" onClick={() => toggleRuns(t)}>
                      {expanded.has(t.task_id) ? <ChevronDown className="size-4" /> : <History className="size-4" />}
                    </Button>
                    <Button size="icon-sm" variant="ghost" data-tip="删除" onClick={() => onDelete(t)}><Trash2 className="size-4" /></Button>
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
                              <Button size="sm" variant="outline" onClick={() => openRun(t, r)}>
                                <MessageSquare className="size-3.5" /> 查看
                              </Button>
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
      )}
    </PlatformShell>
  );
}

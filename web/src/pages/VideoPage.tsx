import { useState, useEffect, useCallback, useRef } from 'react';
import { Upload, Download, History, Trash2, Film, CheckCircle2, AlertCircle, Loader2, X } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Select } from '@/components/ui/select';
import { useVideoStore } from '@/stores/videoStore';
import { getAccessToken } from '@/api/client';
import type { VideoTask } from '@/types';

function videoUrl(taskId: string) {
  const token = getAccessToken();
  const base = `/api/v1/video/tasks/${taskId}/video`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

const STATUS_COLOR: Record<string, 'default' | 'primary' | 'success' | 'destructive' | 'warning'> = {
  pending: 'default',
  extracting: 'primary',
  generating_script: 'primary',
  converting_images: 'primary',
  synthesizing_audio: 'primary',
  rendering: 'primary',
  success: 'success',
  failed: 'destructive',
};

const STATUS_LABEL: Record<string, string> = {
  pending: '排队中', extracting: '提取文字', generating_script: '生成解说词',
  converting_images: '转换图片', synthesizing_audio: '合成语音',
  rendering: '合成视频', success: '已完成', failed: '失败',
};

function TaskCard({ task, onDelete }: { task: VideoTask; onDelete: (id: string) => void }) {
  const isDone = task.status === 'success';
  const isFailed = task.status === 'failed';
  const isProcessing = !isDone && !isFailed;
  const [showConfirm, setShowConfirm] = useState(false);

  return (
    <Card>
      <CardContent className="space-y-3 p-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2 min-w-0">
            <span className="max-w-[200px] truncate text-sm font-medium">
              {task.original_filename}
            </span>
            <Badge variant={STATUS_COLOR[task.status]}>
              {STATUS_LABEL[task.status] || task.status}
            </Badge>
          </div>
          <span className="text-xs text-muted-foreground shrink-0">
            {new Date(task.created_at).toLocaleString('zh-CN')}
          </span>
        </div>

        {isProcessing && (
          <div className="flex items-center gap-3">
            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-secondary">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${task.progress}%` }}
              />
            </div>
            <span className="text-xs text-muted-foreground">{task.progress_text}</span>
          </div>
        )}

        {isFailed && task.error && (
          <p className="text-xs text-destructive">错误: {task.error}</p>
        )}

        {isDone && task.result_url && (
          <div>
            <video
              controls
              className="w-full max-h-[360px] rounded-md bg-black"
              src={videoUrl(task.task_id)}
            />
          </div>
        )}

        {/* Bottom actions */}
        <div className="flex items-center justify-between pt-1 border-t border-border">
          {isDone && task.result_url ? (
            <a
              href={videoUrl(task.task_id)}
              download={`${task.original_filename.replace(/\.[^.]+$/, '')}.mp4`}
              className="inline-flex items-center gap-1 rounded-lg border border-border bg-background px-2.5 h-7 text-[0.8rem] font-medium hover:bg-muted transition-colors"
            >
              <Download className="size-3.5" /> 下载视频
            </a>
          ) : (
            <span />
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setShowConfirm(true)}
            className="text-muted-foreground hover:text-destructive"
          >
            <Trash2 className="size-4" />
            删除
          </Button>
        </div>

        {/* Confirm dialog */}
        {showConfirm && (
          <div className="fixed inset-0 z-50 flex items-center justify-center">
            <div className="absolute inset-0 bg-foreground/30" onClick={() => setShowConfirm(false)} />
            <div className="relative z-10 w-full max-w-sm rounded-xl border border-border bg-card p-6 shadow-lg">
              <div className="flex items-center justify-between mb-4">
                <h3 className="text-lg font-semibold">确认删除</h3>
                <button
                  onClick={() => setShowConfirm(false)}
                  className="rounded-md p-1 text-muted-foreground hover:bg-accent"
                >
                  <X className="size-4" />
                </button>
              </div>
              <p className="text-sm text-muted-foreground mb-4">
                确定要删除任务 「{task.original_filename}」吗？此操作不可撤销。
              </p>
              <div className="flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setShowConfirm(false)}>取消</Button>
                <Button
                  variant="destructive"
                  size="sm"
                  onClick={() => { onDelete(task.task_id); setShowConfirm(false); }}
                >
                  确认删除
                </Button>
              </div>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

export default function VideoPage() {
  const { tasks, loading, loadTasks, submitTask, deleteTask } = useVideoStore();
  const [uploading, setUploading] = useState(false);
  const [aspectRatio, setAspectRatio] = useState('16:9');
  const [resolution, setResolution] = useState('1080p');
  const [voice, setVoice] = useState('male');
  const [logMsg, setLogMsg] = useState('');
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => { loadTasks(); }, [loadTasks]);

  useEffect(() => {
    const hasActive = tasks.some((t) => t.status !== 'success' && t.status !== 'failed');
    if (!hasActive) return;
    const timer = setInterval(() => loadTasks(), 5000);
    return () => clearInterval(timer);
  }, [tasks, loadTasks]);

  const handleUpload = useCallback(async (file: File) => {
    setUploading(true);
    try {
      await submitTask(file, {
        aspect_ratio: aspectRatio, resolution, voice, subtitle_style: 'default',
      });
      setLogMsg('任务已提交');
      setTimeout(() => setLogMsg(''), 3000);
    } catch {
      setLogMsg('提交失败');
      setTimeout(() => setLogMsg(''), 3000);
    } finally {
      setUploading(false);
    }
  }, [submitTask, aspectRatio, resolution, voice]);

  const handleDelete = useCallback(async (taskId: string) => {
    try {
      await deleteTask(taskId);
      setLogMsg('已删除任务');
      setTimeout(() => setLogMsg(''), 3000);
    } catch {
      setLogMsg('删除失败');
      setTimeout(() => setLogMsg(''), 3000);
    }
  }, [deleteTask]);

  const doneCount = tasks.filter((t) => t.status === 'success').length;
  const failedCount = tasks.filter((t) => t.status === 'failed').length;
  const processingCount = tasks.filter((t) => t.status !== 'success' && t.status !== 'failed').length;

  return (
    <PlatformShell
      title="视频生成"
      description="上传 PPT，AI 自动转换解说视频"
    >
      <div>
        {/* Stats row */}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4 mb-6">
          {[
            { label: '历史记录', value: String(tasks.length), icon: History, tone: 'text-primary bg-primary/10' },
            { label: '已完成', value: String(doneCount), icon: CheckCircle2, tone: 'text-success bg-success/12' },
            { label: '处理中/失败', value: `${processingCount}/${failedCount}`, icon: AlertCircle, tone: 'text-warning bg-warning/10' },
          ].map((s) => {
            const Icon = s.icon;
            return (
              <Card key={s.label}>
                <CardContent className="flex items-center gap-3 p-4">
                  <div className={`flex size-10 items-center justify-center rounded-lg ${s.tone}`}>
                    <Icon className="size-5" />
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">{s.label}</p>
                    <p className="text-xl font-semibold tracking-tight">{s.value}</p>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>

        {/* Upload */}
        <div
          className={`rounded-xl border-2 border-dashed p-5 sm:p-8 text-center transition-colors ${
            uploading ? 'pointer-events-none opacity-50' : 'cursor-pointer hover:border-primary/50'
          }`}
          onDragOver={(e) => { e.preventDefault(); }}
          onDrop={(e) => {
            e.preventDefault();
            const file = e.dataTransfer.files?.[0];
            if (file) handleUpload(file);
          }}
          onClick={() => fileRef.current?.click()}
        >
          <Upload className="mx-auto size-10 text-muted-foreground" />
          <p className="mt-4 text-sm font-medium">
            {uploading ? '正在提交...' : '点击或拖拽上传 PPT 文件'}
          </p>
          <p className="text-xs text-muted-foreground">支持 .ppt / .pptx 格式</p>
          <input
            ref={fileRef}
            type="file"
            className="hidden"
            accept=".ppt,.pptx"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleUpload(f);
              e.target.value = '';
            }}
          />
        </div>

        {/* Parameters */}
        <div className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3 sm:gap-4">
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">画面比例</label>
            <Select value={aspectRatio} onChange={(e) => setAspectRatio(e.target.value)}>
              <option value="16:9">16:9</option>
              <option value="9:16">9:16</option>
            </Select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">分辨率</label>
            <Select value={resolution} onChange={(e) => setResolution(e.target.value)}>
              <option value="1080p">1080p</option>
              <option value="720p">720p</option>
            </Select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">语音</label>
            <Select value={voice} onChange={(e) => setVoice(e.target.value)}>
              <option value="male">通用男声</option>
            </Select>
          </div>
        </div>

        {logMsg && (
          <p className="mt-4 rounded-lg bg-accent px-4 py-2 text-sm text-accent-foreground">{logMsg}</p>
        )}

        {/* Task list */}
        <div className="mt-8 border-t border-border pt-6">
          <div className="mb-4 flex items-center gap-2">
            <Film className="size-4 text-muted-foreground" />
            <h3 className="text-sm font-medium">历史任务</h3>
            {tasks.length > 0 && (
              <span className="text-xs text-muted-foreground">({tasks.length})</span>
            )}
          </div>

          {loading && tasks.length === 0 ? (
            <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" /> 加载中...
            </div>
          ) : tasks.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">暂无任务记录</p>
          ) : (
            <div className="space-y-3">
              {tasks.map((task) => (
                <TaskCard key={task.task_id} task={task} onDelete={handleDelete} />
              ))}
            </div>
          )}
        </div>
      </div>
    </PlatformShell>
  );
}

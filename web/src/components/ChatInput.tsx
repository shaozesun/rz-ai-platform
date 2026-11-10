import { useRef, useState } from 'react';
import {
  Send, StopCircle, Paperclip, Layers, Workflow, Globe, FileText, X, Loader2,
  AlertCircle, type LucideIcon,
} from 'lucide-react';
import { Menu } from '@base-ui/react/menu';
import { useChatStore, type TaskMode, type InteractionMode } from '../stores/chatStore';
import { useAuthStore } from '../stores/authStore';
import { cn } from '@/lib/utils';

interface ChatInputProps {
  suggestions?: string[];
}

interface ModeOption {
  value: string;
  label: string;
  description?: string;
}

const TASK_MODES: ModeOption[] = [
  { value: 'normal', label: '常规模式', description: '适用于大部分任务' },
  { value: 'full_analysis', label: '全量分析模式', description: '多维度分析全量数据' },
  { value: 'research', label: '联网调研', description: '联网检索公开信息，多轮深挖并标注来源' },
];

const INTERACTION_MODES: ModeOption[] = [
  { value: 'plan', label: '计划模式', description: '执行时维护任务清单，进度实时可见' },
  { value: 'trust', label: '信任模式', description: '直接执行、只给最终结果' },
];

// 上下文提示的阈值（绝对 token 数，非窗口占比）。取 32k：与压缩触发阈值同口径，
// 也正是「首次响应明显变慢（≈14s）」的量级。
const CONTEXT_HINT_TOKENS = 32_000;

function ModeSelect({
  icon: Icon,
  value,
  options,
  onChange,
  ariaLabel,
}: {
  icon: LucideIcon;
  value: string;
  options: ModeOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
}) {
  const current = options.find((o) => o.value === value) ?? options[0];
  return (
    <Menu.Root>
      <Menu.Trigger
        aria-label={ariaLabel}
        className="flex shrink-0 items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm text-muted-foreground transition-all outline-none hover:bg-muted hover:text-foreground hover:shadow-md"
      >
        <Icon className="size-4" />
        <span>{current.label}</span>
      </Menu.Trigger>
      <Menu.Portal>
        <Menu.Positioner sideOffset={8} align="start">
          <Menu.Popup className="min-w-44 rounded-lg border border-border bg-popover p-1 shadow-lg outline-none">
            {options.map((opt) => (
              <Menu.Item
                key={opt.value}
                onClick={() => onChange(opt.value)}
                className="flex cursor-pointer flex-col gap-0.5 rounded-md px-2.5 py-2 text-left text-sm outline-none data-highlighted:bg-muted"
              >
                <span className="font-medium text-foreground">{opt.label}</span>
                {opt.description && (
                  <span className="text-xs text-muted-foreground">{opt.description}</span>
                )}
              </Menu.Item>
            ))}
          </Menu.Popup>
        </Menu.Positioner>
      </Menu.Portal>
    </Menu.Root>
  );
}

function UploadCard({
  displayName,
  status,
  error,
  onRemove,
}: {
  displayName: string;
  status: 'uploading' | 'ready' | 'error';
  error?: string;
  onRemove: () => void;
}) {
  return (
    <div
      className="flex items-center gap-1.5 rounded-lg border border-border bg-secondary/60 px-2 py-1 text-xs"
      title={status === 'error' ? error : displayName}
    >
      {status === 'uploading' ? (
        <Loader2 className="size-3.5 shrink-0 animate-spin text-muted-foreground" />
      ) : status === 'error' ? (
        <AlertCircle className="size-3.5 shrink-0 text-destructive" />
      ) : (
        <FileText className="size-3.5 shrink-0 text-primary" />
      )}
      <span className="max-w-40 truncate">{displayName}</span>
      {status === 'error' && error && (
        <span className="max-w-52 truncate text-destructive">· {error}</span>
      )}
      <button
        onClick={onRemove}
        aria-label="移除附件"
        className="rounded p-0.5 text-muted-foreground hover:bg-muted/70"
      >
        <X className="size-3" />
      </button>
    </div>
  );
}

export default function ChatInput({ suggestions }: ChatInputProps) {
  const [text, setText] = useState('');
  const fileInputRef = useRef<HTMLInputElement>(null);
  const webSearchEnabled = useAuthStore((s) => s.webSearchEnabled);
  const {
    sendMessage, stopStreaming, isStreaming,
    taskMode, interactionMode, setTaskMode, setInteractionMode,
    webSearch, setWebSearch, contextUsage,
    pendingUploads, addPendingUploadFiles, removePendingUpload,
  } = useChatStore();


  // 只在「历史确实很长」时显示。判据用**绝对 token 数**，不用窗口占用率：拖慢响应的是
  // token 绝对量（冷启动 prefill 实测 ≈2300 tokens/s：26k≈11s、32k≈14s、160k≈68s），
  // 与「占窗口百分之几」无关。此前按窗口 60% 判，262k 窗口要 157k tokens（≈68s）才亮，
  // 等它出现早就没法用了 —— 2026-09-28 实测：32k 时用户已明显感到首字慢，却只占窗口
  // 12%，提示永远看不到。
  const usagePct = contextUsage?.window
    ? Math.round((contextUsage.input_tokens / contextUsage.window) * 100)
    : null;
  const contextTokens = contextUsage?.input_tokens ?? 0;
  const usageWarn = contextTokens >= CONTEXT_HINT_TOKENS;

  const handleSend = (inputText?: string) => {
    const trimmed = (inputText || text).trim();
    if (!trimmed || isStreaming) return;
    setText('');
    sendMessage(trimmed);
  };

  const handleFilesSelected = (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(e.target.files || []);
    if (files.length) addPendingUploadFiles(files);
    e.target.value = ''; // 允许重复选择同一文件
  };

  return (
    <div className="p-3">
      {/* Suggestions */}
      {suggestions && suggestions.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {suggestions.map((s) => (
            <button
              key={s}
              onClick={() => handleSend(s)}
              className="rounded-full border border-border bg-secondary/60 px-3 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              {s}
            </button>
          ))}
        </div>
      )}

      {/* Pending upload preview cards */}
      {pendingUploads.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {pendingUploads.map((u) => (
            <UploadCard
              key={u.key}
              displayName={u.displayName}
              status={u.status}
              error={u.error}
              onRemove={() => removePendingUpload(u.key)}
            />
          ))}
        </div>
      )}

      {/* 上下文用量：历史很长（≥32k tokens）时出现，作为「为什么这次变慢」的解释 */}
      {usageWarn && (
        <div
          className="mb-1 px-1 text-[10px] text-amber-600 dark:text-amber-500"
          title={`本轮上下文 ${contextTokens.toLocaleString()} tokens`
            + `（占模型窗口 ${usagePct}%，窗口 ${contextUsage?.window?.toLocaleString()}）`
            + '；超过 32k 会自动压缩历史，老消息仍可回读'}
        >
          上下文 {contextTokens.toLocaleString()} tokens
          · 当前历史较长，建议开启新对话降低首字延迟
        </div>
      )}

      {/* Input bar */}
      <div className="flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-lg transition-all focus-within:border-ring focus-within:shadow-xl">
        <input
          ref={fileInputRef}
          type="file"
          multiple
          className="hidden"
          onChange={handleFilesSelected}
        />
        <button
          onClick={() => fileInputRef.current?.click()}
          className="rounded-md p-2 text-muted-foreground hover:bg-secondary shrink-0"
          aria-label="附件"
        >
          <Paperclip className="size-5" />
        </button>
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault();
              handleSend();
            }
          }}
          rows={2}
          placeholder="输入您的问题，Enter 发送…"
          disabled={isStreaming}
          className="max-h-56 min-w-0 flex-1 resize-none bg-transparent py-2 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-50"
        />
        <ModeSelect
          icon={Layers}
          value={taskMode}
          options={TASK_MODES}
          onChange={(v) => setTaskMode(v as TaskMode)}
          ariaLabel="模式选择"
        />
        <ModeSelect
          icon={Workflow}
          value={interactionMode}
          options={INTERACTION_MODES}
          onChange={(v) => setInteractionMode(v as InteractionMode)}
          ariaLabel="交互模式选择"
        />
        {/* 联网能力未启用（WEB_ENABLED=false）时不渲染，避免死按钮 */}
        {webSearchEnabled && (
          <button
            onClick={() => setWebSearch(!webSearch)}
            aria-pressed={webSearch}
            aria-label="联网搜索"
            title="联网搜索：检索公开网页并标注来源"
            className={cn(
              'flex shrink-0 items-center gap-1.5 rounded-lg px-2 py-1.5 text-sm transition-all outline-none hover:shadow-md',
              webSearch
                ? 'bg-primary/10 text-primary hover:bg-primary/15'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground',
            )}
          >
            <Globe className="size-4" />
            <span>联网搜索</span>
          </button>
        )}
        {/* 视频生成**没有开关**：视频工具常驻注册，用户直接说「把这份 PPT 做成视频」即可。
            （原先那个开关只影响"注不注流程引导"，开不开都能生成，已按用户要求删掉。） */}
        {isStreaming ? (
          <button
            onClick={stopStreaming}
            className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-destructive text-destructive-foreground transition-opacity hover:opacity-90"
            aria-label="停止"
          >
            <StopCircle className="size-5" />
          </button>
        ) : (
          <button
            onClick={() => handleSend()}
            className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
            disabled={!text.trim()}
            aria-label="发送"
          >
            <Send className="size-5" />
          </button>
        )}
      </div>
    </div>
  );
}

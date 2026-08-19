import { useState } from 'react';
import {
  Send, StopCircle, Paperclip, Layers, Workflow, type LucideIcon,
} from 'lucide-react';
import { Menu } from '@base-ui/react/menu';
import { useChatStore, type TaskMode, type InteractionMode } from '../stores/chatStore';

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
  { value: 'complex', label: '复杂任务', description: '执行多轮次长耗时 agent 任务' },
];

const INTERACTION_MODES: ModeOption[] = [
  { value: 'plan', label: '计划模式', description: '先规划再执行、逐步确认' },
  { value: 'trust', label: '信任模式', description: '自主执行、减少确认' },
];

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

export default function ChatInput({ suggestions }: ChatInputProps) {
  const [text, setText] = useState('');
  const {
    sendMessage, stopStreaming, isStreaming,
    taskMode, interactionMode, setTaskMode, setInteractionMode,
  } = useChatStore();

  const handleSend = (inputText?: string) => {
    const trimmed = (inputText || text).trim();
    if (!trimmed || isStreaming) return;
    setText('');
    sendMessage(trimmed);
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

      {/* Input bar */}
      <div className="flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-lg transition-all focus-within:border-ring focus-within:shadow-xl">
        <button
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

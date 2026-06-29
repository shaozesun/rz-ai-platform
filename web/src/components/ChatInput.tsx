import { useState } from 'react';
import { Send, StopCircle, Paperclip } from 'lucide-react';
import { useChatStore } from '../stores/chatStore';
import { Button } from '@/components/ui/button';

interface ChatInputProps {
  suggestions?: string[];
}

export default function ChatInput({ suggestions }: ChatInputProps) {
  const [text, setText] = useState('');
  const { sendMessage, stopStreaming, isStreaming } = useChatStore();

  const handleSend = (inputText?: string) => {
    const trimmed = (inputText || text).trim();
    if (!trimmed || isStreaming) return;
    setText('');
    sendMessage(trimmed);
  };

  return (
    <div className="border-t border-border p-3">
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
      <div className="flex items-end gap-2 rounded-xl border border-border bg-card p-2 focus-within:border-ring transition-colors">
        <button
          className="rounded-md p-1.5 text-muted-foreground hover:bg-secondary shrink-0"
          aria-label="附件"
        >
          <Paperclip className="size-4" />
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
          rows={1}
          placeholder="输入您的问题，Enter 发送…"
          disabled={isStreaming}
          className="max-h-32 flex-1 resize-none bg-transparent py-1.5 text-sm outline-none placeholder:text-muted-foreground disabled:opacity-50"
        />
        {isStreaming ? (
          <button
            onClick={stopStreaming}
            className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-destructive text-destructive-foreground transition-opacity hover:opacity-90"
            aria-label="停止"
          >
            <StopCircle className="size-4" />
          </button>
        ) : (
          <button
            onClick={() => handleSend()}
            className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground transition-opacity hover:opacity-90 disabled:opacity-40"
            disabled={!text.trim()}
            aria-label="发送"
          >
            <Send className="size-4" />
          </button>
        )}
      </div>
    </div>
  );
}

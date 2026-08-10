import { useState, useCallback, useEffect, useRef, useMemo, memo } from 'react';
import type { ReactNode } from 'react';
import {
  Bot, User, Sparkles, Trash2, Search, Menu, Plus, Wrench, ChevronDown,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import TableActions from './TableActions';
import MermaidRenderer from './MermaidRenderer';

function fixMermaidBlocks(text: string): string {
  if (!text) return text;
  let result = text;
  result = result.replace(
    /mermaid(graph\s+\w+[\s\S]*?)(?=##|---|\*\*|```|$)/gm,
    (_match, code) => {
      let body = code.trim();
      const dirMatch = body.match(/^(graph\s+\w+)\s*/);
      const dir = dirMatch ? dirMatch[1] : 'graph LR';
      if (dirMatch) body = body.slice(dirMatch[0].length);
      body = body.replace(/\["\s+/g, '["').replace(/\s+"\]/g, '"]');
      // 节点标签含 | 但未加引号，自动加双引号（否则 mermaid 解析报错）
      body = body.replace(/\[([^\]"]*?[|][^\]"]*?)\]/g, '["$1"]');
      body = body.replace(/\(([^)"]*?[|][^)"]*?)\)/g, '("$1")');
      body = body
        .replace(/(\)\]?)\s+(\w)/g, '$1\n$2')
        .replace(/]\s+(\w)/g, ']\n$1')
        .replace(/"\s+(\w)/g, '"\n$1')
        .trim();
      return `\n\n\`\`\`mermaid\n${dir}\n${body}\n\`\`\`\n`;
    },
  );
  return result;
}

function hideIncompleteMermaid(text: string): string {
  // 流式输出时，隐藏未闭合的 mermaid 代码块以避免显示原始语法
  const lastOpen = text.lastIndexOf('```mermaid');
  if (lastOpen === -1) return text;
  const afterOpen = text.slice(lastOpen + '```mermaid'.length);
  // 检查是否有闭合的 ```
  const closeIdx = afterOpen.indexOf('\n```');
  if (closeIdx !== -1) return text; // 已闭合，正常渲染
  // 未闭合：截掉 mermaid 块内容，显示占位文字
  return text.slice(0, lastOpen) + '\n\n> 流程图生成中...\n\n';
}
import { useChatStore } from '../stores/chatStore';
import { useAuthStore } from '../stores/authStore';
import ChatInput from './ChatInput';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { Message } from '../types';

// ── Agent 工具调用段解析 ──
// chatStore 在 agent 流式时会插入 <tool-call name="x">args</tool-call> /
// <tool-result name="x">result</tool-result> 标记，这里解析成结构化段落
type MsgSegment =
  | { kind: 'text'; content: string }
  | { kind: 'tool'; name: string; args?: string; result?: string };

const TOOL_TAG_RE = /<tool-(call|result) name="([^"]*)">([\s\S]*?)<\/tool-\1>/g;

function parseMessageSegments(text: string): MsgSegment[] {
  if (!text.includes('<tool-')) return [{ kind: 'text', content: text }];
  const segments: MsgSegment[] = [];
  const re = new RegExp(TOOL_TAG_RE.source, 'g');
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIndex) {
      const t = text.slice(lastIndex, m.index);
      if (t.trim()) segments.push({ kind: 'text', content: t });
    }
    const [, tagType, name, body] = m;
    if (tagType === 'call') {
      segments.push({ kind: 'tool', name, args: body });
    } else {
      // result 挂到最近一个同名且还没有 result 的工具段上
      const target = [...segments].reverse().find(
        (s): s is Extract<MsgSegment, { kind: 'tool' }> =>
          s.kind === 'tool' && s.name === name && s.result === undefined,
      );
      if (target) target.result = body;
      else segments.push({ kind: 'tool', name, result: body });
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    const t = text.slice(lastIndex);
    if (t.trim()) segments.push({ kind: 'text', content: t });
  }
  return segments;
}

function ToolCard({ name, args, result }: { name: string; args?: string; result?: string }) {
  const [open, setOpen] = useState(false);
  const done = result !== undefined;
  return (
    <div className="my-2 overflow-hidden rounded-lg border border-border bg-card/60 text-xs">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted/50 transition-colors"
      >
        <Wrench className="size-3.5 shrink-0 text-primary" />
        <span className="font-medium font-mono">{name}</span>
        <span className="text-muted-foreground">
          {done ? '调用完成' : '调用中...'}
        </span>
        <ChevronDown
          className={cn('ml-auto size-3.5 shrink-0 text-muted-foreground transition-transform', open && 'rotate-180')}
        />
      </button>
      {open && (
        <div className="space-y-2 border-t border-border px-3 py-2">
          {args && args !== '{}' && (
            <div>
              <p className="mb-1 text-muted-foreground">参数</p>
              <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono">{args}</pre>
            </div>
          )}
          {result !== undefined && (
            <div>
              <p className="mb-1 text-muted-foreground">返回</p>
              <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded bg-muted p-2 font-mono">{result}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  );
}


const SUGGESTIONS = [
  '智能运维平台有哪些功能？',
  '如何配置消防设施？',
  '安全隐患检测怎么做？',
];

const MessageBubble = memo(function MessageBubble({
  msg,
  isStreaming,
  isLast,
  userName,
}: {
  msg: Message;
  isStreaming: boolean;
  isLast: boolean;
  userName: string;
}) {
  const segments = useMemo(() => {
    const raw = msg.content || '';
    const parsed = parseMessageSegments(raw);
    return parsed.map((seg) => {
      if (seg.kind !== 'text') return seg;
      const fixed = isStreaming && isLast
        ? fixMermaidBlocks(hideIncompleteMermaid(seg.content))
        : fixMermaidBlocks(seg.content);
      return { ...seg, content: fixed };
    });
  }, [msg.content, isStreaming, isLast]);
  const isAssistant = msg.role === 'assistant';

  const markdownComponents = {
    table: ({ children }: { children?: ReactNode }) => {
      return (
        <TableActions>
          <table className="assistant-table">{children}</table>
        </TableActions>
      );
    },
    code: ({ className, children, ...props }: { className?: string; children?: ReactNode }) => {
      const codeText = String(children).replace(/\n$/, '');
      if (className === 'language-mermaid') {
        return <MermaidRenderer code={codeText} />;
      }
      if (!className) {
        return (
          <code className="rounded bg-muted px-1 py-0.5 text-xs font-mono" {...props}>
            {children}
          </code>
        );
      }
      return (
        <pre className="overflow-x-auto rounded-lg bg-muted p-3 my-3">
          <code className="text-xs font-mono">{codeText}</code>
        </pre>
      );
    },
  };

  return (
    <div
      className={cn('flex gap-3', msg.role === 'user' && 'flex-row-reverse')}
    >
      <div
        className={cn(
          'flex size-8 shrink-0 items-center justify-center rounded-lg',
          msg.role === 'user'
            ? 'bg-secondary text-secondary-foreground'
            : 'bg-primary text-primary-foreground',
        )}
      >
        {msg.role === 'user'
          ? <User className="size-4" />
          : <Bot className="size-4" />
        }
      </div>
      <div className={cn('max-w-[80%]', msg.role === 'user' && 'flex flex-col items-end')}>
        <p className="mb-1 text-xs font-medium text-muted-foreground">
          {msg.role === 'user' ? userName : 'AI 助手'}
        </p>
        <div
          className={cn(
            'rounded-2xl px-4 py-2.5 text-sm leading-relaxed',
            msg.role === 'user'
              ? 'rounded-tr-sm bg-primary text-primary-foreground'
              : 'rounded-tl-sm bg-secondary text-secondary-foreground',
          )}
        >
          {isAssistant ? (
            <div className="assistant-msg max-w-none">
              {segments.map((seg, i) =>
                seg.kind === 'tool' ? (
                  <ToolCard key={i} name={seg.name} args={seg.args} result={seg.result} />
                ) : (
                  <ReactMarkdown
                    key={i}
                    remarkPlugins={[remarkGfm]}
                    components={markdownComponents}
                  >
                    {seg.content}
                  </ReactMarkdown>
                ),
              )}
            </div>
          ) : (
            <p className="whitespace-pre-line">{msg.content}</p>
          )}
        </div>
      </div>
    </div>
  );
});

export default function ChatArea() {
  const {
    messages, isStreaming, sessions, currentSessionId,
    selectSession, deleteSession, newSession,
  } = useChatStore();
  const user = useAuthStore((s) => s.user);
  const listRef = useRef<HTMLDivElement>(null);
  const [sessionSearch, setSessionSearch] = useState('');
  const [mobilePanelOpen, setMobilePanelOpen] = useState(false);

  const scrollToBottom = useCallback(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => { scrollToBottom(); }, [messages, scrollToBottom]);

  // Hide empty "新对话" sessions (created before first message was sent)
  const displaySessions = sessions.filter(
    (s) => s.title !== '新对话' || s.session_id === currentSessionId,
  );
  const filteredSessions = sessionSearch
    ? displaySessions.filter((s) => s.title.toLowerCase().includes(sessionSearch.toLowerCase()))
    : displaySessions;

  return (
    <div className="grid min-h-0 flex-1 grid-cols-1">
      {/* Backdrop */}
      {mobilePanelOpen && (
        <div
          className="fixed inset-0 z-40 bg-foreground/30"
          onClick={() => setMobilePanelOpen(false)}
        />
      )}

      {/* Sessions sidebar — slide-out panel on all sizes */}
      <div
        className={cn(
          'flex flex-col overflow-hidden rounded-none border-0 lg:rounded-xl lg:border lg:border-border bg-card',
          'fixed inset-y-0 left-0 z-50 w-72 transition-transform',
          mobilePanelOpen ? 'translate-x-0 shadow-2xl' : '-translate-x-full',
        )}
      >
        <div className="border-b border-border px-3 py-3">
          <p className="text-sm font-semibold">历史会话</p>
        </div>
        <div className="border-b border-border px-3 py-2">
          <div className="relative">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              placeholder="搜索会话"
              value={sessionSearch}
              onChange={(e) => setSessionSearch(e.target.value)}
              className="h-8 w-full rounded-md border border-border bg-secondary/60 pl-8 pr-2 text-sm outline-none focus:border-ring focus:bg-card"
            />
          </div>
        </div>
        <div className="flex-1 overflow-y-auto p-2">
          {filteredSessions.length === 0 && (
            <p className="px-2 py-4 text-center text-xs text-muted-foreground">
              {sessionSearch ? '无匹配会话' : '暂无会话'}
            </p>
          )}
          {filteredSessions.map((s) => (
            <div key={s.session_id} className="group flex w-full items-center">
              <button
                onClick={() => { selectSession(s.session_id); setMobilePanelOpen(false); }}
                className={cn(
                  'flex flex-1 items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors',
                  s.session_id === currentSessionId
                    ? 'bg-accent text-accent-foreground'
                    : 'hover:bg-secondary',
                )}
              >
                <Sparkles className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium leading-tight">{s.title}</p>
                  <p className="text-xs text-muted-foreground">
                    {s.updated_at
                      ? new Date(s.updated_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
                      : ''}
                  </p>
                </div>
              </button>
              <button
                onClick={(e) => { e.stopPropagation(); deleteSession(s.session_id); }}
                className="shrink-0 rounded p-1 text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-destructive transition-opacity mr-1"
                title="删除会话"
              >
                <Trash2 className="size-3" />
              </button>
            </div>
          ))}
        </div>
      </div>

      {/* Chat area */}
      <div className="flex min-h-0 flex-col overflow-hidden rounded-none border-0 lg:rounded-xl lg:border lg:border-border bg-card">
        {/* Header */}
        <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
          <button
            onClick={() => setMobilePanelOpen(true)}
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1.5 text-sm text-muted-foreground hover:bg-accent"
            aria-label="会话列表"
          >
            <Menu className="size-4" />
            会话历史
          </button>
          <span className="flex-1" />
          <Button size="sm" variant="outline" onClick={() => newSession()} className="shrink-0">
            <Plus className="size-3.5" />
            开启新对话
          </Button>
        </div>

        {/* Messages */}
        <div ref={listRef} className="flex-1 space-y-5 overflow-y-auto p-4 [touch-action:pan-y]">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
              <div className="flex size-14 items-center justify-center rounded-2xl bg-primary/10">
                <Bot className="size-7 text-primary" />
              </div>
              <div>
                <p className="text-sm font-semibold">智能运维助手</p>
                <p className="mt-1 text-xs text-muted-foreground max-w-xs">
                  基于企业知识库的智能问答与办公助理，可以帮您查询文档、分析数据
                </p>
              </div>
            </div>
          ) : (
            messages.map((msg: Message, idx: number) => (
              <MessageBubble
                key={msg.message_id}
                msg={msg}
                isStreaming={isStreaming}
                isLast={idx === messages.length - 1}
                userName={user?.name || user?.phone || '我'}
              />
            ))
          )}
        </div>

        {/* Input */}
        <ChatInput suggestions={SUGGESTIONS} />
      </div>
    </div>
  );
}

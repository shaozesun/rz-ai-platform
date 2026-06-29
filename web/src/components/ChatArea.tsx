import { useState, useCallback, useEffect, useRef } from 'react';
import {
  Bot, User, Sparkles, Trash2, Search, Menu, Plus,
} from 'lucide-react';
import ReactMarkdown from 'react-markdown';
import { useChatStore } from '../stores/chatStore';
import { useAuthStore } from '../stores/authStore';
import ChatInput from './ChatInput';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { Message } from '../types';

const SUGGESTIONS = [
  '润泽平台有哪些功能？',
  '如何配置消防设施？',
  '安全隐患检测怎么做？',
];

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
                <p className="text-sm font-semibold">润泽 AI 助手</p>
                <p className="mt-1 text-xs text-muted-foreground max-w-xs">
                  基于企业知识库的智能问答与办公助理，可以帮您查询文档、分析数据
                </p>
              </div>
            </div>
          ) : (
            messages.map((msg: Message) => (
              <div
                key={msg.message_id}
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
                    {msg.role === 'user' ? user?.name || user?.phone || '我' : 'AI 助手'}
                  </p>
                  <div
                    className={cn(
                      'rounded-2xl px-4 py-2.5 text-sm leading-relaxed',
                      msg.role === 'user'
                        ? 'rounded-tr-sm bg-primary text-primary-foreground'
                        : 'rounded-tl-sm bg-secondary text-secondary-foreground',
                    )}
                  >
                    {msg.role === 'assistant' ? (
                      <div className="prose prose-sm max-w-none">
                        <ReactMarkdown>
                          {msg.content || (isStreaming ? '...' : '')}
                        </ReactMarkdown>
                      </div>
                    ) : (
                      <p className="whitespace-pre-line">{msg.content}</p>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
        </div>

        {/* Input */}
        <ChatInput suggestions={SUGGESTIONS} />
      </div>
    </div>
  );
}

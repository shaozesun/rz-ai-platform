import { useState, useCallback, useEffect, useRef, useMemo, memo } from 'react';
import { createPortal } from 'react-dom';
import type { ReactNode } from 'react';
import {
  Bot, User, Sparkles, Trash2, Search, Wrench, ChevronDown, FileText, Copy, Check,
  AlertCircle, RotateCcw, Loader2, MoreHorizontal, Pin, PinOff, Pencil,
} from 'lucide-react';
import { Input } from './ui/input';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import TableActions from './TableActions';
import MermaidRenderer from './MermaidRenderer';
import ChartRenderer from './ChartRenderer';
import FileCard from './FileCard';
import ArtifactPanel from './ArtifactPanel';
import PlanCard from './PlanCard';
import InterviewPanel from './InterviewPanel';

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

function hideIncompleteChart(text: string): string {
  // 流式输出时，隐藏未闭合的 <chart> 标签以避免显示原始语法
  const lastOpen = text.lastIndexOf('<chart');
  if (lastOpen === -1) return text;
  if (text.indexOf('</chart>', lastOpen) !== -1) return text; // 已闭合，正常渲染
  return text.slice(0, lastOpen); // 未闭合：截掉从 <chart 起的部分
}

function hideIncompleteFile(text: string): string {
  // 流式输出时，隐藏未闭合的 <file> 标签以避免显示原始语法（<file .../> 自闭合）
  const lastOpen = text.lastIndexOf('<file');
  if (lastOpen === -1) return text;
  if (text.indexOf('/>', lastOpen) !== -1) return text; // 已闭合，正常渲染
  return text.slice(0, lastOpen); // 未闭合：截掉从 <file 起的部分
}
import { useChatStore, collectArtifacts } from '../stores/chatStore';
import { useAuthStore } from '../stores/authStore';
import ChatInput from './ChatInput';
import { cn } from '@/lib/utils';
import type { Message, ExecutionPlan, InterviewQuestion } from '../types';

// ── Agent 工具调用段解析 ──
// chatStore 在 agent 流式时会插入 <tool-call name="x">args</tool-call> /
// <tool-result name="x">result</tool-result> 标记；LLM 输出图表时会在文本里带
// <chart ...>data</chart> 标记。这里统一解析成结构化段落。
type MsgSegment =
  | { kind: 'text'; content: string }
  | { kind: 'tool'; name: string; args?: string; result?: string }
  | { kind: 'chart'; attrs: Record<string, string>; data: string }
  | { kind: 'file'; url: string; name: string }
  | { kind: 'plan'; plan: ExecutionPlan }
  | { kind: 'interview'; questions: InterviewQuestion[] };

const TOOL_TAG_RE = /<tool-(call|result) name="([^"]*)">([\s\S]*?)<\/tool-\1>/g;
const CHART_TAG_RE = /<chart\b([^>]*)>([\s\S]*?)<\/chart>/g;
const FILE_TAG_RE = /<file\b([^>]*)\/>/g;
const PLAN_TAG_RE = /<plan\b[^>]*>([\s\S]*?)<\/plan>/g;
const INTERVIEW_TAG_RE = /<interview\b[^>]*>([\s\S]*?)<\/interview>/g;

// 产物 URL（Markdown 链接 / 裸 URL）识别——LLM 偶尔跑偏不输出 <file> 标签而写成链接，
// 这里兜底拆成 file 段，避免纯文字看不出是可下载文件。URL 归一化由 FileCard 内部完成。
const ARTIFACT_PATTERN = new RegExp(
  `\\[([^\\]]*)\\]\\(((?:https?://[^/]*)?/api/v1/chat/agent/artifacts/[^)\\s]*)\\)` +
  `|((?:https?://[^\\s/]+)?/api/v1/chat/agent/artifacts/[^\\s)\\]]+)`,
  'g',
);

// 裸 URL 时从路径尾部取文件名（剥离签名/查询串）
function artifactNameOf(url: string): string {
  const tail = url.split('?')[0].split('#')[0].split('/').pop();
  return tail || '文件';
}

function splitArtifactRefs(text: string): MsgSegment[] {
  const out: MsgSegment[] = [];
  let last = 0;
  let m: RegExpExecArray | null;
  ARTIFACT_PATTERN.lastIndex = 0;
  while ((m = ARTIFACT_PATTERN.exec(text)) !== null) {
    if (m.index > last) {
      const t = text.slice(last, m.index);
      if (t.trim()) out.push({ kind: 'text', content: t });
    }
    const url = m[2] || m[3];
    const name = m[1] || artifactNameOf(url);
    out.push({ kind: 'file', url, name });
    last = ARTIFACT_PATTERN.lastIndex;
  }
  if (last < text.length) {
    const t = text.slice(last);
    if (t.trim()) out.push({ kind: 'text', content: t });
  }
  return out.length ? out : [{ kind: 'text', content: text }];
}

function parseAttrs(raw: string): Record<string, string> {
  const attrs: Record<string, string> = {};
  const re = /([\w-]+)="([^"]*)"/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw)) !== null) {
    attrs[m[1]] = m[2];
  }
  return attrs;
}

function parseMessageSegments(text: string): MsgSegment[] {
  if (
    !text.includes('<tool-') && !text.includes('<chart') && !text.includes('<file') &&
    !text.includes('<plan') && !text.includes('<interview')
  ) {
    // 无结构化标签也可能带产物链接/裸 URL，仍走拆分
    return splitArtifactRefs(text);
  }
  const segments: MsgSegment[] = [];
  const re = new RegExp(
    `(?:${TOOL_TAG_RE.source})|(?:${CHART_TAG_RE.source})|(?:${FILE_TAG_RE.source})|` +
    `(?:${PLAN_TAG_RE.source})|(?:${INTERVIEW_TAG_RE.source})`,
    'g',
  );
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > lastIndex) {
      const t = text.slice(lastIndex, m.index);
      if (t.trim()) segments.push(...splitArtifactRefs(t));
    }
    // 组：tool 1-3，chart 4-5，file 6，plan 7，interview 8（未参与匹配的组为 undefined）
    const [, tagType, name, toolBody, chartAttrs, chartBody, fileAttrs, planBody, interviewBody] = m;
    if (tagType) {
      if (tagType === 'call') {
        segments.push({ kind: 'tool', name, args: toolBody });
      } else {
        // result 挂到最近一个同名且还没有 result 的工具段上
        const target = [...segments].reverse().find(
          (s): s is Extract<MsgSegment, { kind: 'tool' }> =>
            s.kind === 'tool' && s.name === name && s.result === undefined,
        );
        if (target) target.result = toolBody;
        else segments.push({ kind: 'tool', name, result: toolBody });
      }
    } else if (chartAttrs !== undefined) {
      segments.push({ kind: 'chart', attrs: parseAttrs(chartAttrs || ''), data: chartBody });
    } else if (fileAttrs !== undefined) {
      const attrs = parseAttrs(fileAttrs || '');
      segments.push({ kind: 'file', url: attrs.url || '', name: attrs.name || '' });
    } else if (planBody !== undefined) {
      try {
        segments.push({ kind: 'plan', plan: JSON.parse(planBody) as ExecutionPlan });
      } catch {
        // 计划 JSON 解析失败则忽略该段
      }
    } else if (interviewBody !== undefined) {
      try {
        segments.push({ kind: 'interview', questions: JSON.parse(interviewBody) as InterviewQuestion[] });
      } catch {
        // 访谈 JSON 解析失败则忽略该段
      }
    }
    lastIndex = re.lastIndex;
  }
  if (lastIndex < text.length) {
    const t = text.slice(lastIndex);
    if (t.trim()) segments.push(...splitArtifactRefs(t));
  }
  return segments;
}

function ToolCard({ name, args, result }: { name: string; args?: string; result?: string }) {
  const [open, setOpen] = useState(false);
  const status = useChatStore((s) => s.toolStatus?.[name]);
  const done = result !== undefined;
  return (
    <div className="my-2 overflow-hidden rounded-lg border border-border bg-card/85 text-xs">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-muted/50 transition-colors"
      >
        <Wrench className="size-3.5 shrink-0 text-primary" />
        <span className="font-medium font-mono">{name}</span>
        <span className="text-muted-foreground">
          {done ? '调用完成' : (status || '调用中...')}
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
  '介绍一下平台和智能体有哪些能力',
  '查一下我的待办工单',
  '查询设备台账或人员信息',
  '分析数据并生成图表',
  '帮我创建一个自定义技能',
];

function formatDateTime(iso?: string): string {
  if (!iso) return '';
  try {
    return new Date(iso).toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' });
  } catch {
    return '';
  }
}

/** 超长标题：无省略号，悬停时向后滚动（跑马灯）显示全文 */
function MarqueeTitle({ text }: { text: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setOverflow(el.scrollWidth > el.clientWidth + 1);
  }, [text]);
  return (
    <div
      ref={ref}
      className="rz-marquee min-w-0 text-sm font-medium leading-tight"
      data-overflow={overflow ? 'true' : undefined}
    >
      <span className="rz-marquee__track">
        {overflow && <span className="pr-2" aria-hidden>{text}</span>}
        <span className="pr-2">{text}</span>
      </span>
    </div>
  );
}

export const MessageBubble = memo(function MessageBubble({
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
        ? fixMermaidBlocks(hideIncompleteFile(hideIncompleteChart(hideIncompleteMermaid(seg.content))))
        : fixMermaidBlocks(seg.content);
      return { ...seg, content: fixed };
    });
  }, [msg.content, isStreaming, isLast]);
  const isAssistant = msg.role === 'assistant';
  // 空气泡：流式且最后一条且尚无任何内容时，显示思考指示器替代空泡
  const showThinking = isAssistant && isStreaming && isLast && !(msg.content || '').trim();
  const deleteMessage = useChatStore((s) => s.deleteMessage);
  const retryFailed = useChatStore((s) => s.retryFailed);
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(msg.content || '');
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // ignore
    }
  };

  const handleDelete = () => {
    if (window.confirm('删除这条回答？')) {
      void deleteMessage(msg.session_id, msg.message_id);
    }
  };

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
      data-mid={msg.message_id}
      className={cn('flex gap-3', msg.role === 'user' && 'flex-row-reverse')}
    >
      <div
        className={cn(
          'flex size-8 shrink-0 items-center justify-center rounded-lg',
          msg.role === 'user'
            ? 'bg-secondary text-secondary-foreground'
            : 'bg-primary/20 text-primary',
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
        {msg.role === 'user' && msg.uploaded_filenames && msg.uploaded_filenames.length > 0 && (
          <div className="mb-1.5 flex flex-wrap justify-end gap-1.5">
            {msg.uploaded_filenames.map((name) => (
              <span
                key={name}
                className="flex items-center gap-1.5 rounded-lg border border-border bg-secondary/60 px-2 py-1 text-xs text-secondary-foreground"
                title={name}
              >
                <FileText className="size-3.5 shrink-0 text-primary" />
                <span className="max-w-40 truncate">{name}</span>
              </span>
            ))}
          </div>
        )}
        <div
          className={cn(
            'rounded-2xl px-4 py-2.5 text-sm leading-relaxed',
            msg.role === 'user'
              ? 'rounded-tr-sm bg-secondary text-secondary-foreground'
              : 'rounded-tl-sm bg-secondary text-secondary-foreground',
          )}
        >
          {isAssistant ? (
            <div className="assistant-msg max-w-none">
              {msg.streamError ? (
                <div className="flex flex-col items-start gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2">
                  <p className="flex items-center gap-1.5 text-sm text-destructive">
                    <AlertCircle className="size-4 shrink-0" />
                    {msg.streamError}
                  </p>
                  <button
                    type="button"
                    onClick={() => void retryFailed()}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border px-2 py-1 text-xs text-foreground transition-colors hover:bg-muted"
                  >
                    <RotateCcw className="size-3" />
                    重试
                  </button>
                </div>
              ) : showThinking ? (
                <div className="flex items-center gap-1.5 py-1">
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:150ms]" />
                  <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:300ms]" />
                  <span className="ml-1 text-xs text-muted-foreground">正在思考…</span>
                </div>
              ) : (
                <>
                {segments.map((seg, i) => {
                if (seg.kind === 'tool') {
                  return <ToolCard key={i} name={seg.name} args={seg.args} result={seg.result} />;
                }
                if (seg.kind === 'chart') {
                  return (
                    <ChartRenderer
                      key={i}
                      spec={{ type: seg.attrs.type || '', attrs: seg.attrs, data: seg.data }}
                    />
                  );
                }
                if (seg.kind === 'file') {
                  return <FileCard key={i} url={seg.url} name={seg.name} />;
                }
                if (seg.kind === 'plan') {
                  return <PlanCard key={i} plan={seg.plan} />;
                }
                if (seg.kind === 'interview') {
                  return <InterviewPanel key={i} questions={seg.questions} />;
                }
                return (
                  <ReactMarkdown
                    key={i}
                    remarkPlugins={[remarkGfm]}
                    components={markdownComponents}
                  >
                    {seg.content}
                  </ReactMarkdown>
                );
              })}
                {isAssistant && isStreaming && isLast && (
                  <div className="mt-1.5 flex items-center gap-1.5 text-xs text-muted-foreground">
                    <span className="size-1.5 animate-pulse rounded-full bg-primary/60" />
                    <span>正在生成…</span>
                  </div>
                )}
                </>
              )}
            </div>
          ) : (
            <p className="whitespace-pre-line">{msg.content}</p>
          )}
        </div>
        {isAssistant && (
          <div className="mt-1.5 flex items-center gap-1.5">
            {msg.created_at && (
              <span className="text-xs text-muted-foreground/70">{formatDateTime(msg.created_at)}</span>
            )}
            <button
              type="button"
              onClick={handleCopy}
              title={copied ? '已复制' : '复制'}
              className="rounded p-1 text-muted-foreground/70 transition-colors hover:bg-muted/60 hover:text-foreground"
            >
              {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
            </button>
            <button
              type="button"
              onClick={handleDelete}
              data-tip="删除"
              className="rounded p-1 text-muted-foreground/70 transition-colors hover:bg-muted/60 hover:text-destructive"
            >
              <Trash2 className="size-3.5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
});

export default function ChatArea() {
  const {
    messages, isStreaming, isLoadingMessages, sessions, currentSessionId,
    selectSession, deleteSession, pinSession, renameSession, sessionPanelOpen, closeSessionPanel,
  } = useChatStore();
  const user = useAuthStore((s) => s.user);
  const pushArtifact = useChatStore((s) => s.pushArtifact);
  const pendingJump = useChatStore((s) => s.pendingJump);
  const clearPendingJump = useChatStore((s) => s.clearPendingJump);
  const listRef = useRef<HTMLDivElement>(null);
  const [sessionSearch, setSessionSearch] = useState('');
  // 会话项省略号菜单 + 行内重命名状态。菜单用 fixed 定位向右弹（避开侧栏 overflow 裁剪），
  // 记录触发按钮的位置；省略号选中时显示气泡高亮。
  const [menu, setMenu] = useState<{ id: string; top: number; left: number } | null>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [renamingId, setRenamingId] = useState<string | null>(null);
  const [renameValue, setRenameValue] = useState('');

  // 点菜单外关闭（省略号本身除外，toggle 交给其 click 处理）
  useEffect(() => {
    if (!menu) return;
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target?.closest?.('[data-menu-trigger]')) return;
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenu(null);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [menu]);

  const saveRename = (id: string) => {
    const title = renameValue.trim();
    if (title) renameSession(id, title);
    setRenamingId(null);
    setMenu(null);
  };

  // 流式/历史消息里的 HTML 产物自动进右侧面板：只推 store 里还没有的（新产物）。
  // 不能全量重推——pushArtifact 现在对已存在产物也会打开面板，历史会话预填后若重推
  // 会强制弹开面板；只推新的才能在流式生成新看板时自动打开、又不打扰用户手动开关。
  useEffect(() => {
    // 会话切换加载中：messages 可能仍是旧会话内容，跳过避免把旧会话产物错推进来
    if (isLoadingMessages) return;
    const known = new Set(useChatStore.getState().artifacts.map((a) => a.url));
    collectArtifacts(messages, currentSessionId)
      .filter((a) => !known.has(a.url))
      .forEach((a) => pushArtifact(a.url, a.name, a.type === 'records'));
  }, [messages, pushArtifact, currentSessionId, isLoadingMessages]);

  const scrollToBottom = useCallback(() => {
    if (listRef.current) {
      listRef.current.scrollTop = listRef.current.scrollHeight;
    }
  }, []);

  useEffect(() => { scrollToBottom(); }, [messages, scrollToBottom]);

  // 铃铛通知跳转：切到目标会话后，滚动到通知对应的那条回答并短暂高亮；
  // 该回复已不可定位（重载后 id 变化/已删除）→ 回退到该会话最新一条助手回复，再兜底到底部。
  useEffect(() => {
    if (!pendingJump || pendingJump.sessionId !== currentSessionId) return;
    if (isLoadingMessages) return;
    const container = listRef.current;
    if (!container || messages.length === 0) return;
    const findEl = (mid?: string | null) => {
      if (!mid) return null;
      return (
        (Array.from(container.querySelectorAll('[data-mid]')).find(
          (el) => (el as HTMLElement).dataset.mid === mid,
        ) as HTMLElement | null) ?? null
      );
    };
    let target = findEl(pendingJump.messageId);
    if (!target) {
      const lastAsst = [...messages].reverse().find((m) => m.role === 'assistant');
      target = findEl(lastAsst?.message_id);
    }
    const anchor = target ?? (container.lastElementChild as HTMLElement | null);
    if (!anchor) return;
    anchor.scrollIntoView({ block: 'center', behavior: 'smooth' });
    clearPendingJump();
  }, [pendingJump, currentSessionId, isLoadingMessages, messages, clearPendingJump]);

  // Hide empty "新对话" sessions (created before first message was sent)
  const displaySessions = sessions.filter(
    (s) => s.title !== '新对话' || s.session_id === currentSessionId,
  );
  const filteredSessions = sessionSearch
    ? displaySessions.filter((s) => s.title.toLowerCase().includes(sessionSearch.toLowerCase()))
    : displaySessions;

  return (
    <div className="flex min-h-0 flex-1">
      {/* Backdrop */}
      {sessionPanelOpen && (
        <div
          className="fixed inset-0 z-40 bg-foreground/30"
          onClick={closeSessionPanel}
        />
      )}

      {/* Sessions sidebar — slide-out panel on all sizes */}
      <div
        className={cn(
          'flex flex-col overflow-hidden rounded-none border-0 lg:rounded-xl lg:border lg:border-border bg-card',
          'fixed inset-y-0 left-0 z-50 w-72 transition-transform',
          sessionPanelOpen ? 'translate-x-0 shadow-2xl' : '-translate-x-full',
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
                onClick={() => { selectSession(s.session_id); closeSessionPanel(); }}
                className={cn(
                  'flex min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors',
                  s.session_id === currentSessionId
                    ? 'bg-accent text-accent-foreground'
                    : 'hover:bg-secondary',
                )}
              >
                {s.pinned
                  ? <Pin className="mt-0.5 size-3.5 shrink-0 text-primary" />
                  : <Sparkles className="mt-0.5 size-3.5 shrink-0 text-muted-foreground" />}
                <div className="min-w-0 flex-1">
                  {renamingId === s.session_id ? (
                    <Input
                      autoFocus
                      value={renameValue}
                      onChange={(e) => setRenameValue(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveRename(s.session_id);
                        if (e.key === 'Escape') { setRenamingId(null); setMenu(null); }
                      }}
                      onBlur={() => saveRename(s.session_id)}
                      onClick={(e) => e.stopPropagation()}
                      className="h-7 w-full text-sm"
                    />
                  ) : (
                    <MarqueeTitle text={s.title} />
                  )}
                  <p className="truncate text-xs text-muted-foreground">
                    {s.updated_at
                      ? new Date(s.updated_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' })
                      : ''}
                  </p>
                </div>
              </button>
              {/* 省略号菜单：置顶/重命名/删除（fixed 定位向右弹，避开侧栏 overflow 裁剪） */}
              <div className="mr-1 shrink-0">
                <button
                  data-menu-trigger
                  onClick={(e) => {
                    e.stopPropagation();
                    const rect = (e.currentTarget as HTMLButtonElement).getBoundingClientRect();
                    setMenu(menu?.id === s.session_id
                      ? null
                      : { id: s.session_id, top: rect.bottom + 4, left: rect.right + 4 });
                  }}
                  className={cn(
                    'rounded-full p-1 transition-colors',
                    menu?.id === s.session_id
                      ? 'bg-primary text-primary-foreground opacity-100 shadow-sm'
                      : 'text-muted-foreground opacity-0 group-hover:opacity-100 hover:bg-muted/60',
                  )}
                >
                  <MoreHorizontal className="size-3.5" />
                </button>
                {menu?.id === s.session_id && createPortal(
                  <div
                    ref={menuRef}
                    style={{ position: 'fixed', top: menu.top, left: menu.left, zIndex: 50 }}
                    className="min-w-32 overflow-hidden rounded-md border border-border bg-popover p-1 shadow-lg"
                  >
                    <button
                      onClick={(e) => { e.stopPropagation(); pinSession(s.session_id, !s.pinned); setMenu(null); }}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-muted/60"
                    >
                      {s.pinned ? <PinOff className="size-3" /> : <Pin className="size-3" />}
                      {s.pinned ? '取消置顶' : '置顶'}
                    </button>
                    <button
                      onClick={(e) => { e.stopPropagation(); setRenamingId(s.session_id); setRenameValue(s.title); setMenu(null); }}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs hover:bg-muted/60"
                    >
                      <Pencil className="size-3" />
                      重命名
                    </button>
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        setMenu(null);
                        if (window.confirm(`删除会话「${s.title}」？`)) deleteSession(s.session_id);
                      }}
                      className="flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-destructive hover:bg-destructive/10"
                    >
                      <Trash2 className="size-3" />
                      删除
                    </button>
                  </div>,
                  document.body,
                )}
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* 聊天列（看板为 fixed 全屏覆盖层，不进卡片流，不占横向空间） */}
      <div className="flex min-h-0 flex-1 overflow-hidden rounded-none border-0 lg:rounded-xl lg:border lg:border-border bg-background">
        <div className="flex min-w-0 min-h-0 flex-1 flex-col">
          {/* Messages */}
          <div ref={listRef} className="flex-1 space-y-5 overflow-y-auto p-4 [touch-action:pan-y]">
            {isLoadingMessages ? (
              <div className="flex h-full items-center justify-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                加载会话…
              </div>
            ) : messages.length === 0 ? (
              <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
                <div className="flex size-14 items-center justify-center rounded-2xl bg-primary/10">
                  <Bot className="size-7 text-primary" />
                </div>
                <div>
                  <p className="text-sm font-semibold">智能运维助手</p>
                  <p className="mt-1 text-xs text-muted-foreground max-w-xs">
                    欢迎使用润泽 AI 平台，一个面向数据中心运维的超级智能体。可以帮您检索知识文档、分析业务数据，还能识别安全隐患、生成图表看板、制作报告与视频等。
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
        <ArtifactPanel />
      </div>
    </div>
  );
}

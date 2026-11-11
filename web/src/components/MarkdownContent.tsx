import type { ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import TableActions from './TableActions';
import MermaidRenderer from './MermaidRenderer';
import { cn } from '@/lib/utils';

/**
 * 助手 Markdown 的渲染规则（聊天回答与 .md 产物预览共用同一套）。
 *
 * - 外链：联网调研会产出模型写的来源链接，必须新窗口打开 + rel=noopener，
 *   并显示域名让用户看清去哪（模型生成的 URL 不可信，不能让它静默接管当前页）。
 * - 表格：包 TableActions（复制/导出）。
 * - 代码块：language-mermaid 走本地 MermaidRenderer。
 */
const markdownComponents = {
  a: ({ href, children }: { href?: string; children?: ReactNode }) => {
    let host = '';
    try {
      host = href ? new URL(href).hostname : '';
    } catch {
      host = '';
    }
    return (
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        title={href}
        className="break-all text-primary underline underline-offset-2 hover:opacity-80"
      >
        {children}
        {host && <span className="ml-1 text-xs text-muted-foreground">({host})</span>}
      </a>
    );
  },
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

/**
 * 统一的 Markdown 渲染块。
 *
 * 外层带 `assistant-msg` —— 标题/列表/表格/引用等排版样式都挂在该作用域下
 * （见 `web/src/index.css`），所以产物预览必须套同一层，否则排版会散掉。
 */
export function MarkdownContent({
  content,
  className,
}: {
  content: string;
  className?: string;
}) {
  return (
    <div className={cn('assistant-msg max-w-none', className)}>
      {/*
        ⚠️ `singleTilde: false` —— 只让 `~~成对~~` 才是删除线，**单个 `~` 原样显示**。
        为什么必须关：中文里 `~` 当**区间符**太常见（模型和人都这么写：`25.4~26.1℃`、`30~60%`），
        而 remark-gfm 的删除线**默认允许单个 `~`**
        （`micromark-extension-gfm-strikethrough` 的 `singleTilde` 默认 true）→
        一行里第 1、2 个 `~` 会配成一对、把中间整段划掉 ——
        实测：「重要告警 30~60%，紧急告警 20~80%」渲染成了划掉的 "3060%"、"2080%"。
      */}
      <ReactMarkdown
        remarkPlugins={[[remarkGfm, { singleTilde: false }]]}
        components={markdownComponents}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

export default MarkdownContent;

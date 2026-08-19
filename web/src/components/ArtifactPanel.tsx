import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, FileCode2, X } from 'lucide-react';
import { useChatStore } from '../stores/chatStore';
import { HtmlPreview } from './HtmlPreview';
import client from '../api/client';
import { cn } from '@/lib/utils';

// 从看板 HTML 里提取 <title> 作为区分标题（模板/生成内容均带 title 标签）
function extractHtmlTitle(html: string): string {
  const m = /<title>([\s\S]*?)<\/title>/i.exec(html);
  return m ? m[1].trim() : '';
}

/** 拉 blob 下载（带 token，与 FileCard 同一套鉴权逻辑） */
async function downloadArtifact(url: string, name: string) {
  const rel = url.replace(/^\/api\/v1/, '');
  const resp = await client.get(rel, { responseType: 'blob' });
  const objectUrl = URL.createObjectURL(resp.data);
  const a = document.createElement('a');
  a.href = objectUrl;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
}

/**
 * 看板面板：HTML 看板预览。展开态为 fixed 全屏覆盖层（盖住聊天区+平台侧边栏，
 * 图表占满视口）；有产物但未展开时显示折叠触发器；无产物时不渲染。
 */
export function ArtifactPanel() {
  const artifacts = useChatStore((s) => s.artifacts);
  const activeArtifactUrl = useChatStore((s) => s.activeArtifactUrl);
  const panelOpen = useChatStore((s) => s.panelOpen);
  const selectArtifact = useChatStore((s) => s.selectArtifact);
  const closePanel = useChatStore((s) => s.closePanel);

  const active = artifacts.find((a) => a.url === activeArtifactUrl) ?? null;

  const handleDownload = useCallback(() => {
    if (active) downloadArtifact(active.url, active.name);
  }, [active]);

  // 提取每个看板 HTML 的 <title> 作区分标题（失败回退文件名）
  const [titles, setTitles] = useState<Record<string, string>>({});
  const fetchedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    let cancelled = false;
    for (const a of artifacts) {
      if (fetchedRef.current.has(a.url)) continue;
      fetchedRef.current.add(a.url);
      const rel = a.url.replace(/^\/api\/v1/, '');
      client
        .get(rel, { responseType: 'blob' })
        .then((resp) => resp.data.text())
        .then((html) => {
          if (cancelled) return;
          const t = extractHtmlTitle(html);
          if (t) setTitles((prev) => (prev[a.url] === t ? prev : { ...prev, [a.url]: t }));
        })
        .catch(() => {});
    }
    return () => {
      cancelled = true;
    };
  }, [artifacts]);

  // 全屏模态：Esc 关闭，避免用户被困在全屏看板里
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') closePanel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [closePanel]);

  const activeTitle = active ? titles[active.url] || active.name || '看板预览' : '看板预览';

  // 无产物：整个面板隐藏，不占布局
  if (artifacts.length === 0) return null;

  // 折叠态：右侧竖条触发器（桌面）+ 移动端悬浮按钮
  if (!panelOpen) {
    return (
      <>
        <button
          onClick={() => selectArtifact(active?.url ?? artifacts[0].url)}
          className="hidden w-9 shrink-0 flex-col items-center gap-1.5 border-l border-border bg-card py-3 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground lg:flex"
          title="打开看板预览"
        >
          <FileCode2 className="size-4 text-primary" />
          <span className="text-[10px] [writing-mode:vertical-rl]">看板</span>
        </button>
        <button
          onClick={() => selectArtifact(active?.url ?? artifacts[0].url)}
          className="fixed bottom-20 right-4 z-40 flex size-11 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg lg:hidden"
          title="打开看板预览"
        >
          <FileCode2 className="size-5" />
        </button>
      </>
    );
  }

  return (
    <div className="fixed inset-0 z-[60] flex flex-col bg-background">
      <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
        <FileCode2 className="size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold" title={activeTitle}>
          {activeTitle}
        </span>
        {active && (
          <button
            onClick={handleDownload}
            className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            title="下载"
          >
            <Download className="size-4" />
          </button>
        )}
        <button
          onClick={closePanel}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          title="关闭看板面板"
        >
          <X className="size-3.5" />
          关闭
        </button>
      </div>

      {artifacts.length > 1 && (
        <div className="flex gap-1 overflow-x-auto border-b border-border px-3 py-2">
          {artifacts.map((a) => (
            <button
              key={a.url}
              onClick={() => selectArtifact(a.url)}
              className={cn(
                'max-w-48 shrink-0 truncate rounded-md px-2 py-1 text-xs transition-colors',
                a.url === activeArtifactUrl
                  ? 'bg-accent text-accent-foreground'
                  : 'text-muted-foreground hover:bg-secondary',
              )}
            >
              {titles[a.url] || a.name}
            </button>
          ))}
        </div>
      )}

      <div className="min-h-0 flex-1">
        {active ? (
          <HtmlPreview url={active.url} name={active.name} />
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 text-xs text-muted-foreground">
            <FileCode2 className="size-5" />
            <span>暂无看板</span>
          </div>
        )}
      </div>
    </div>
  );
}

export default ArtifactPanel;

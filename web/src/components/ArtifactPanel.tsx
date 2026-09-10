import { useCallback, useEffect, useRef, useState } from 'react';
import { Download, FileCode2, Maximize2, Minimize2, X } from 'lucide-react';
import { useChatStore } from '../stores/chatStore';
import { HtmlPreview } from './HtmlPreview';
import { RecordsTable } from './RecordsTable';
import client from '../api/client';
import { downloadArtifact } from '@/lib/artifact-download';
import { cn } from '@/lib/utils';

// 从看板 HTML 里提取 <title> 作为区分标题（模板/生成内容均带 title 标签）
function extractHtmlTitle(html: string): string {
  const m = /<title>([\s\S]*?)<\/title>/i.exec(html);
  return m ? m[1].trim() : '';
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
  // records 列表产物用半屏分栏（可拖宽/全屏）；HTML 产物保持全屏大屏
  const isRecords = active?.type === 'records';

  // records 分栏宽度（px）+ 全屏标志 + 拖拽状态
  const [full, setFull] = useState(false);
  const [panelWidth, setPanelWidth] = useState(
    () => Math.min(620, Math.max(360, Math.round(window.innerWidth * 0.46))),
  );
  const [dragging, setDragging] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  const handleDownload = useCallback(() => {
    if (active) downloadArtifact(active.url.replace(/^\/api\/v1/, ''), active.name);
  }, [active]);

  // 提取每个看板 HTML 的 <title> 作区分标题（失败回退文件名）
  const [titles, setTitles] = useState<Record<string, string>>({});
  const fetchedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    let cancelled = false;
    for (const a of artifacts) {
      if (fetchedRef.current.has(a.url)) continue;
      fetchedRef.current.add(a.url);
      // records 产物是 JSON 无 <title>，跳过 HTML 标题提取
      if (a.type === 'records') continue;
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

  // Esc：records 全屏时先还原分栏，其余情况收起面板（避免被困在全屏）
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (full && isRecords) setFull(false);
      else closePanel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [full, isRecords, closePanel]);

  // 收起面板或切到非 records 产物时复位全屏态
  useEffect(() => {
    if (!panelOpen || !isRecords) setFull(false);
  }, [panelOpen, isRecords]);

  // 拖动左分隔线调宽：拖动期间挂全局 mousemove/mouseup
  const startDrag = () => setDragging(true);
  useEffect(() => {
    if (!dragging) return;
    const prevCursor = document.body.style.cursor;
    const prevSelect = document.body.style.userSelect;
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const onMove = (e: MouseEvent) => {
      const el = panelRef.current;
      if (!el) return;
      const right = el.getBoundingClientRect().right;
      const minW = 320;
      const maxW = Math.max(minW, right - 260); // 聊天区至少留 ~260px
      setPanelWidth(Math.min(maxW, Math.max(minW, right - e.clientX)));
    };
    const onUp = () => setDragging(false);
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
    return () => {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      document.body.style.cursor = prevCursor;
      document.body.style.userSelect = prevSelect;
    };
  }, [dragging]);

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
          data-tip="打开看板预览"
        >
          <FileCode2 className="size-4 text-primary" />
          <span className="text-[10px] [writing-mode:vertical-rl]">看板</span>
        </button>
        <button
          onClick={() => selectArtifact(active?.url ?? artifacts[0].url)}
          className="fixed bottom-20 right-4 z-40 flex size-11 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-lg lg:hidden"
          data-tip="打开看板预览"
        >
          <FileCode2 className="size-5" />
        </button>
      </>
    );
  }

  // records 列表用半屏 docked（左右分栏，聊天区保持可见，参考 DeerFlow 右侧面板）；
  // HTML 看板/驾驶舱保持原全屏覆盖层（盖住聊天区看大屏）。
  const panelBody = (
    <>
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <FileCode2 className="size-4 shrink-0 text-primary" />
        <span className="min-w-0 flex-1 truncate text-sm font-semibold" title={activeTitle}>
          {activeTitle}
        </span>
        {active && (
          <button
            onClick={handleDownload}
            className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            data-tip="下载"
          >
            <Download className="size-4" />
          </button>
        )}
        {isRecords && (
          <button
            onClick={() => setFull((v) => !v)}
            className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            data-tip={full ? '还原为分栏' : '全屏'}
          >
            {full ? <Minimize2 className="size-4" /> : <Maximize2 className="size-4" />}
          </button>
        )}
        <button
          onClick={closePanel}
          className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
          data-tip="关闭看板面板"
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
          active.type === 'records' ? (
            <RecordsTable url={active.url} name={active.name} />
          ) : (
            <HtmlPreview url={active.url} name={active.name} />
          )
        ) : (
          <div className="flex h-full flex-col items-center justify-center gap-1.5 text-xs text-muted-foreground">
            <FileCode2 className="size-5" />
            <span>暂无看板</span>
          </div>
        )}
      </div>
    </>
  );

  if (isRecords) {
    if (full) {
      // records 全屏：盖住聊天区看大数据（头部「还原」/ Esc 可回到分栏）
      return (
        <div className="fixed inset-0 z-[60] flex flex-col bg-background">
          {panelBody}
        </div>
      );
    }
    // records 分栏：右侧半屏，左分隔线可拖动调宽，聊天区 flex-1 收窄仍可见
    return (
      <div
        ref={panelRef}
        style={{ width: panelWidth }}
        className="relative flex shrink-0 flex-col overflow-hidden border-l border-border bg-background"
      >
        <div
          onMouseDown={startDrag}
          data-tip="拖动调整宽度"
          className={cn(
            'absolute inset-y-0 left-0 z-10 w-1.5 cursor-col-resize bg-transparent transition-colors hover:bg-primary/60',
            dragging && 'bg-primary/60',
          )}
        />
        {panelBody}
      </div>
    );
  }
  // HTML 看板/驾驶舱：全屏覆盖层
  return (
    <div className="fixed inset-0 z-[60] flex flex-col bg-background">
      {panelBody}
    </div>
  );
}

export default ArtifactPanel;

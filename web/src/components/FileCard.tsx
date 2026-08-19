import { useEffect, useMemo, useState } from 'react';
import { Download, Eye, File as FileIcon, X } from 'lucide-react';
import client from '../api/client';
import { cn } from '@/lib/utils';
import { useChatStore, normalizeArtifactUrl } from '../stores/chatStore';

// 图片产物内联预览，其余走下载（后端经鉴权端点返回，需带 token）
const IMAGE_EXT = ['png', 'jpg', 'jpeg', 'gif', 'webp'];

interface FileCardProps {
  url: string;
  name: string;
  size?: number;
}

function extOf(name: string): string {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i + 1).toLowerCase() : '';
}

function prettySize(size?: number): string {
  if (size == null) return '';
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`;
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${size} B`;
}

export function FileCard({ url, name, size }: FileCardProps) {
  const isImage = IMAGE_EXT.includes(extOf(name));
  const isHtml = extOf(name) === 'html';
  const isPdf = extOf(name) === 'pdf';
  const sessionId = useChatStore((s) => s.currentSessionId);
  const pushArtifact = useChatStore((s) => s.pushArtifact);
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  // LLM 可能编造 url，先归一化成权威相对路径（已是权威前缀则原样）
  const safeUrl = useMemo(() => normalizeArtifactUrl(url, sessionId), [url, sessionId]);

  // 相对路径供 axios client（自动带 token + 401 刷新）
  const rel = safeUrl.replace(/^\/api\/v1/, '');

  useEffect(() => {
    if (!isImage) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    client.get(rel, { responseType: 'blob' })
      .then((resp) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(resp.data);
        setImgUrl(objectUrl);
      })
      .catch(() => { if (!cancelled) setFailed(true); });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [rel, isImage]);

  async function handleDownload() {
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

  // PDF 预览：带 token 拉 blob → objectURL → 全屏覆盖层 iframe 渲染（Esc 关闭）
  async function handlePreview() {
    try {
      const resp = await client.get(rel, { responseType: 'blob' });
      setPreviewUrl(URL.createObjectURL(resp.data));
    } catch {
      setFailed(true);
    }
  }

  function closePreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setPreviewUrl(null);
  }

  useEffect(() => {
    if (!previewUrl) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        URL.revokeObjectURL(previewUrl);
        setPreviewUrl(null);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [previewUrl]);

  return (
    <>
    <div className="my-2 flex max-w-sm items-start gap-3 rounded-lg border border-border bg-card/60 p-3">
      <div className="flex flex-col gap-2 overflow-hidden">
        {isImage && imgUrl ? (
          <img
            src={imgUrl}
            alt={name}
            className="max-h-52 rounded border border-border object-contain"
          />
        ) : (
          <div className="flex items-center gap-2">
            <FileIcon className="size-5 shrink-0 text-primary" />
            <div className="min-w-0">
              <p className="truncate text-xs font-medium">{name}</p>
              {prettySize(size) && (
                <p className="text-[10px] text-muted-foreground">{prettySize(size)}</p>
              )}
            </div>
          </div>
        )}
        {failed && <p className="text-[10px] text-destructive">预览加载失败</p>}
        <div className="flex flex-wrap items-center gap-1.5">
          <button
            onClick={handleDownload}
            className={cn(
              'flex items-center gap-1.5 rounded bg-muted px-2 py-1 text-xs hover:bg-muted/70 transition-colors',
              isImage && 'self-start',
            )}
          >
            <Download className="size-3.5" />
            下载
          </button>
          {isPdf && (
            <button
              onClick={handlePreview}
              className="flex items-center gap-1.5 rounded bg-muted px-2 py-1 text-xs hover:bg-muted/70 transition-colors"
            >
              <Eye className="size-3.5" />
              查看
            </button>
          )}
          {isHtml && (
            <button
              onClick={() => pushArtifact(safeUrl, name)}
              className="flex items-center gap-1.5 rounded bg-muted px-2 py-1 text-xs hover:bg-muted/70 transition-colors"
            >
              <Eye className="size-3.5" />
              在看板中查看
            </button>
          )}
        </div>
      </div>
    </div>
    {previewUrl && (
      <div className="fixed inset-0 z-[60] flex flex-col bg-background">
        <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
          <FileIcon className="size-4 shrink-0 text-primary" />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold" title={name}>
            {name}
          </span>
          <button
            onClick={handleDownload}
            className="rounded p-1.5 text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            title="下载"
          >
            <Download className="size-4" />
          </button>
          <button
            onClick={closePreview}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            title="关闭预览"
          >
            <X className="size-3.5" />
            关闭
          </button>
        </div>
        <iframe
          src={previewUrl}
          title={name}
          className="h-full w-full"
          sandbox="allow-same-origin allow-scripts"
        />
      </div>
    )}
  </>
);

}

export default FileCard;

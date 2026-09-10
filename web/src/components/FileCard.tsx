import { useEffect, useMemo, useState } from 'react';
import { Download, Eye, File as FileIcon, X } from 'lucide-react';
import client from '../api/client';
import { cn } from '@/lib/utils';
import { downloadArtifact } from '@/lib/artifact-download';
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
  // records 数据集产物（analysis_load 物化列表），url 以 /records（或 /records?file=…）结尾
  const isRecords = /\/records(?:\?|$)/.test(safeUrl);
  // 数据集内的 .csv 产物（模型误把列表导出成 csv 下载卡）：给「查看表格」兜底，
  // 点开用同数据集 /records?file=xxx 渲染成可搜索表格
  const isDatasetCsv =
    safeUrl.endsWith('.csv') && safeUrl.includes('/chat/agent/artifacts/') && !isRecords;
  const datasetCsvRecordsUrl = isDatasetCsv
    ? safeUrl.replace(
        /\/[^/]+\.csv$/,
        `/records?file=${encodeURIComponent(decodeURIComponent(safeUrl.split('/').pop() || ''))}`,
      )
    : '';

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
    await downloadArtifact(rel, name);
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
    <div className="my-2 flex max-w-sm items-start gap-3 rounded-lg border border-border bg-card/60 p-3 shadow-sm">
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
              onClick={() => pushArtifact(safeUrl, name, true)}
              className="flex items-center gap-1.5 rounded bg-muted px-2 py-1 text-xs hover:bg-muted/70 transition-colors"
            >
              <Eye className="size-3.5" />
              在看板中查看
            </button>
          )}
          {isRecords && (
            <button
              onClick={() => pushArtifact(safeUrl, name, true)}
              className="flex items-center gap-1.5 rounded bg-muted px-2 py-1 text-xs hover:bg-muted/70 transition-colors"
            >
              <Eye className="size-3.5" />
              查看表格
            </button>
          )}
          {isDatasetCsv && (
            <button
              onClick={() => pushArtifact(datasetCsvRecordsUrl, name, true)}
              className="flex items-center gap-1.5 rounded bg-muted px-2 py-1 text-xs hover:bg-muted/70 transition-colors"
            >
              <Eye className="size-3.5" />
              查看表格
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
            data-tip="下载"
          >
            <Download className="size-4" />
          </button>
          <button
            onClick={closePreview}
            className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            data-tip="关闭预览"
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

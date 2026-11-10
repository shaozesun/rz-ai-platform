import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Download, Eye, File as FileIcon, X } from 'lucide-react';
import client from '../api/client';
import * as agentVideoApi from '../api/agentVideo';
import MarkdownContent from './MarkdownContent';
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
  const sessionId = useChatStore((s) => s.currentSessionId);
  const pushArtifact = useChatStore((s) => s.pushArtifact);
  const [imgUrl, setImgUrl] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewText, setPreviewText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  // LLM 可能编造 url，先归一化成权威相对路径（已是权威前缀则原样）
  const safeUrl = useMemo(() => normalizeArtifactUrl(url, sessionId), [url, sessionId]);
  // 产物类型只看**后缀**，但模型给的 name 经常没有后缀（如 name="润泽数据中心发展计划"，
  // 而 url=".../workspace/润泽数据中心发展计划.md"）→ name 取不到就回退用 url。
  // url 一定带真实后缀，中文名要先 percent-decode 才能取到 ".md"。
  const ext = useMemo(() => {
    const fromName = extOf(name);
    if (fromName) return fromName;
    let path = safeUrl.split('?')[0];
    try { path = decodeURIComponent(path); } catch { /* 非法编码就用原串 */ }
    return extOf(path);
  }, [name, safeUrl]);
  const isImage = IMAGE_EXT.includes(ext);
  const isHtml = ext === 'html';
  const isPdf = ext === 'pdf';
  const isMarkdown = ext === 'md';
  // mp4 = 对话窗口「讲解视频」（签名直链）；webm = 交互浏览录屏（鉴权端点 blob）
  const isVideo = ext === 'mp4' || ext === 'webm';
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

  // 视频走**带签名的直链**（<video>/<a> 没法带 Authorization 头，浏览器走不了 JWT）：
  // 大视频（实测有 150MB）用 blob 得先全量进内存，播放/下载都会卡死或失败；直链则边下边播、
  // 支持 Range 拖进度，下载也走浏览器原生流式落盘。
  //
  // **链接会过期**（签名 TTL VIDEO_URL_TOKEN_TTL=1h，与任务绑定），而消息里存的是**当时那条**：
  // 隔天再打开这张卡片，浏览器只会拿到 401 → <video> 读不到元数据显示 00:00、点也点不开
  // （实测故障）。所以拿得到 task_id 时一律**先换一条新签名**再播；换不到才用消息里的原链接，
  // 并在播放失败时再换一次。
  const videoTaskId = useMemo(() => {
    const m = safeUrl.match(/\/chat\/agent-video\/([^/?#]+)\/video/);
    return m ? m[1] : '';
  }, [safeUrl]);
  const [videoSrc, setVideoSrc] = useState('');
  const [videoErr, setVideoErr] = useState(false);
  const retriedRef = useRef(0);

  const refreshVideoUrl = useCallback(async (): Promise<boolean> => {
    if (!videoTaskId) return false;
    try {
      const fresh = await agentVideoApi.getAgentVideoTask(videoTaskId);
      if (fresh?.video_url) {
        setVideoErr(false);
        setVideoSrc(fresh.video_url);
        return true;
      }
    } catch { /* 换不到就退回原链接 */ }
    return false;
  }, [videoTaskId]);

  useEffect(() => {
    // 只有「讲解视频」有签名直链；webm 录屏走下面的 blob 分支
    if (!videoTaskId) return;
    let cancelled = false;
    void refreshVideoUrl().then((ok) => {
      // 换不到（接口失败）才退回消息里的原链接；**已有链接不覆盖**，
      // 否则并发/重复渲染时会把刚换到的新签名冲掉，又变回过期那条
      if (!cancelled && !ok) setVideoSrc((prev) => prev || safeUrl);
    });
    return () => { cancelled = true; };
  }, [videoTaskId, safeUrl, refreshVideoUrl]);

  // 浏览录屏的 webm：safeUrl 直接给 <video> 带不上 Bearer → 401，必须走鉴权端点 blob
  useEffect(() => {
    if (!isVideo || videoTaskId) return;
    let cancelled = false;
    let objectUrl: string | null = null;
    setVideoErr(false);
    client.get(rel, { responseType: 'blob' })
      .then((resp) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(resp.data);
        setVideoSrc(objectUrl);
      })
      .catch(() => { if (!cancelled) setVideoErr(true); });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [rel, isVideo, videoTaskId]);

  /** 播放失败：讲解视频多半是签名过期 → 换一条新的重试（最多 2 次）；
      webm 录屏是 blob，没有签名可换，直接落到错误态 */
  async function handleVideoError() {
    setVideoErr(true);
    if (!videoTaskId || retriedRef.current >= 2) return;
    retriedRef.current += 1;
    await refreshVideoUrl();
  }

  /** 手动重试：清掉计数再来一轮（给"链接过期 / 网络抖动"一个出口） */
  async function retryVideo() {
    retriedRef.current = 0;
    if (!(await refreshVideoUrl())) setVideoErr(true);
  }

  // 图片仍走 blob（体积小、且要避免 token 出现在 img src 里）
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
    // 视频：mp4 用签名直链原生流式下载（blob 会先把 150MB 拉进内存）；
    // webm 录屏已是 ≤30MB 的 blob URL，直接下载即可
    if (isVideo && videoSrc) {
      const a = document.createElement('a');
      a.href = videoSrc;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      return;
    }
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

  // Markdown 预览：拉文本直接用统一 Markdown 渲染（与聊天回答同一套排版）
  async function handleMarkdownPreview() {
    try {
      const resp = await client.get(rel, { responseType: 'text' });
      setPreviewText(typeof resp.data === 'string' ? resp.data : String(resp.data));
    } catch {
      setFailed(true);
    }
  }

  function closePreview() {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    setPreviewUrl(null);
    setPreviewText(null);
  }

  useEffect(() => {
    if (!previewUrl && previewText === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (previewUrl) URL.revokeObjectURL(previewUrl);
      setPreviewUrl(null);
      setPreviewText(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [previewUrl, previewText]);

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
        ) : isVideo && videoSrc ? (
          /* 讲解视频：带签名的直链内联播放（浏览器自动 Range 边下边播，不预载整个文件）；
             播放失败多为签名过期 → onError 里换一条新签名重试一次 */
          <video
            src={videoSrc}
            controls
            preload="metadata"
            onError={handleVideoError}
            className="max-h-72 w-full rounded border border-border bg-black"
          />
        ) : isVideo ? (
          <div className="flex h-24 flex-col items-center justify-center gap-2 rounded border border-border bg-black/40 text-xs text-muted-foreground">
            <span>
              {videoErr
                ? (videoTaskId ? '视频加载失败（播放链接可能已过期）' : '视频加载失败')
                : '正在获取播放链接…'}
            </span>
            {videoErr && videoTaskId && (
              <button
                onClick={retryVideo}
                className="rounded bg-muted px-2 py-1 text-[11px] text-foreground hover:bg-muted/70"
              >
                重新加载
              </button>
            )}
          </div>
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
          {isMarkdown && (
            <button
              onClick={handleMarkdownPreview}
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
    {(previewUrl || previewText !== null) && (
      <div className="fixed inset-0 z-[60] flex flex-col bg-background">
        <div className="flex items-center gap-2 border-b border-border px-4 py-2.5">
          <FileIcon className="size-4 shrink-0 text-primary" />
          <span className="min-w-0 flex-1 truncate text-sm font-semibold" title={name}>
            {name}
          </span>
          {/* PDF 兜底：浏览器原生查看器最稳（沙箱 iframe 里 Chrome 的 PDF 查看器可能不渲染） */}
          {previewUrl && (
            <a
              href={previewUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="shrink-0 rounded-md border border-border px-2 py-1 text-xs text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground"
            >
              新标签页打开
            </a>
          )}
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
        {previewText !== null ? (
          <div className="min-h-0 flex-1 overflow-auto">
            <div className="mx-auto max-w-3xl px-6 py-6">
              <MarkdownContent content={previewText} />
            </div>
          </div>
        ) : (
          /* 不加 sandbox：Chrome 的内置 PDF 查看器在 sandbox iframe 里会被禁（白屏）。
             blob 是我们自己产物的对象 URL，非外部页面。 */
          <iframe src={previewUrl!} title={name} className="h-full w-full" />
        )}
      </div>
    )}
  </>
);

}

export default FileCard;

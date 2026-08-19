import { useEffect, useMemo, useState } from 'react';
import { FileWarning } from 'lucide-react';
import client from '../api/client';

// 打包期把 ECharts 源码内联成字符串，运行时注入到 iframe 的 srcdoc，
// 让 agent 生成的自包含 HTML 里 `echarts` 全局可用——不引 CDN、不出内网。
// vite/client 已声明 `*?raw` 类型（tsconfig types: ["vite/client"]）。
import echartsSrc from 'echarts/dist/echarts.min.js?raw';

interface HtmlPreviewProps {
  url: string;
  name: string;
}

/**
 * Agent HTML 产物（<file name=xxx.html>）的本地预览：带 token 拉取 blob → 读文本
 * → 前置注入 echarts 源码 → sandbox iframe 渲染「迷你看板」。数据全内联，不出内网。
 */
export function HtmlPreview({ url, name }: HtmlPreviewProps) {
  const rel = useMemo(() => url.replace(/^\/api\/v1/, ''), [url]);
  const [srcDoc, setSrcDoc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setFailed(false);
    setSrcDoc(null);
    client
      .get(rel, { responseType: 'blob' })
      .then((resp) => {
        if (cancelled) return;
        return resp.data.text().then((html: string) => {
          if (cancelled) return;
          setSrcDoc(`<script>${echartsSrc}</script>\n${html}`);
        });
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [rel]);

  if (failed) {
    return (
      <div className="flex flex-col items-center gap-1.5 py-10 text-xs text-muted-foreground">
        <FileWarning className="size-5" />
        <span>看板加载失败</span>
        <span className="font-mono">{name}</span>
      </div>
    );
  }

  if (srcDoc === null) {
    return (
      <div className="flex h-full items-center justify-center gap-2 py-10 text-xs text-muted-foreground">
        <span className="size-1.5 animate-pulse rounded-full bg-primary/60" />
        看板加载中…
      </div>
    );
  }

  return (
    <iframe
      title={name}
      sandbox="allow-scripts"
      srcDoc={srcDoc}
      className="h-full w-full bg-white"
    />
  );
}

export default HtmlPreview;

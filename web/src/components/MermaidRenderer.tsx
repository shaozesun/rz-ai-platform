import { useEffect, useRef, useState, useCallback, memo } from 'react';
import { ZoomIn, ZoomOut, Maximize, X } from 'lucide-react';
import mermaid from 'mermaid';

let initialized = false;

function initMermaid() {
  if (initialized) return;
  mermaid.initialize({
    startOnLoad: false,
    theme: 'default',
    securityLevel: 'sandbox',
    suppressErrorRendering: true,
  });
  initialized = true;
}

// 容器宽度百分比，scale=1 等于对话区 100% 宽
const SCALES: Record<number, string> = {
  0.5: '50%',
  0.75: '75%',
  1: '100%',
  1.5: '150%',
  2: '200%',
  3: '300%',
};

const MAX_SVG_BYTES = 500 * 1024;

function sanitizeSvg(raw: string): string {
  // 简单的验证/净化：解析 SVG，去除危险元素和属性
  try {
    const parser = new DOMParser();
    const doc = parser.parseFromString(raw, 'image/svg+xml');
    const errorNode = doc.querySelector('parsererror');
    if (errorNode) return ''; // 解析失败，不渲染

    // 移除 script 标签
    doc.querySelectorAll('script').forEach((el) => el.remove());

    // 移除事件处理器属性 (on*=)
    doc.querySelectorAll('*').forEach((el) => {
      for (const attr of [...el.attributes]) {
        if (/^on\w+/i.test(attr.name)) {
          el.removeAttribute(attr.name);
        }
      }
    });

    const serializer = new XMLSerializer();
    return serializer.serializeToString(doc);
  } catch {
    return '';
  }
}

export default memo(function MermaidRenderer({ code }: { code: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string>('');
  const [scale, setScale] = useState(1);
  const [fullscreen, setFullscreen] = useState(false);

  useEffect(() => {
    initMermaid();
    const id = `mermaid-${Math.random().toString(36).slice(2, 10)}`;
    mermaid
      .render(id, code)
      .then(({ svg: result }) => {
        // 检测 mermaid 是否返回了错误 SVG（mermaid 11 可能不抛异常）
        if (/<text[^>]*>\s*(?:Syntax error|Parse error|Lexical error)/i.test(result)) {
          setError('流程图语法错误');
          setSvg('');
          return;
        }
        if (result.length > MAX_SVG_BYTES) {
          setError('流程图过大，无法显示');
          setSvg('');
          return;
        }
        const clean = sanitizeSvg(result);
        setSvg(clean || result);
        setError('');
      })
      .catch((err) => {
        setError(err.message || '渲染失败');
        setSvg('');
      });
  }, [code]);

  const zoomIn = useCallback(
    () => setScale((s) => {
      const keys = Object.keys(SCALES).map(Number).sort((a, b) => a - b);
      return keys.find((z) => z > s + 0.01) || keys[keys.length - 1];
    }),
    [],
  );
  const zoomOut = useCallback(
    () => setScale((s) => {
      const keys = Object.keys(SCALES).map(Number).sort((a, b) => a - b);
      const idx = keys.findLastIndex((z) => z < s - 0.01);
      return idx >= 0 ? keys[idx] : keys[0];
    }),
    [],
  );

  const controls = (
    <div className="flex items-center gap-1">
      <button
        onClick={zoomOut}
        disabled={scale <= 0.5}
        className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30"
        title="缩小"
      >
        <ZoomOut className="size-3.5" />
      </button>
      <span className="text-xs text-muted-foreground min-w-[40px] text-center select-none">
        {Math.round(scale * 100)}%
      </span>
      <button
        onClick={zoomIn}
        disabled={scale >= 3}
        className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground disabled:opacity-30"
        title="放大"
      >
        <ZoomIn className="size-3.5" />
      </button>
      <span className="mx-0.5 h-4 w-px bg-border" />
      <button
        onClick={() => setFullscreen(true)}
        className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
        title="全屏"
      >
        <Maximize className="size-3.5" />
      </button>
    </div>
  );

  if (error) {
    return (
      <pre className="overflow-x-auto rounded-lg bg-muted p-3 my-3">
        <code className="text-xs font-mono">{code}</code>
      </pre>
    );
  }

  if (!svg) {
    return (
      <div className="my-3 rounded-lg border border-border bg-secondary/30 p-4 text-center text-xs text-muted-foreground">
        流程图加载中...
      </div>
    );
  }

  const widthPct = SCALES[scale] || '100%';
  const sizeStyle = scale > 1
    ? { width: widthPct, maxWidth: widthPct }
    : { maxWidth: widthPct };

  const body = (
    <div className="overflow-auto py-3 px-2">
      <div
        ref={containerRef}
        dangerouslySetInnerHTML={{ __html: svg }}
        className="flex justify-center [&>svg]:max-w-full [&>svg]:!w-full [&>svg]:h-auto"
        style={sizeStyle}
      />
    </div>
  );

  return (
    <>
      <div className="mermaid-wrapper my-3 overflow-hidden rounded-lg border border-border bg-white">
        <div className="flex items-center justify-end border-b border-border px-3 py-1.5 bg-muted/30">
          {controls}
        </div>
        {body}
      </div>

      {fullscreen && (
        <div className="fixed inset-0 z-[9999] flex flex-col bg-background">
          <div className="flex items-center justify-between border-b border-border px-4 py-2 shrink-0">
            <span className="text-sm font-medium">流程图</span>
            <div className="flex items-center gap-3">
              {controls}
              <button
                onClick={() => setFullscreen(false)}
                className="rounded-md p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
                title="关闭全屏"
              >
                <X className="size-4" />
              </button>
            </div>
          </div>
          <div className="flex-1 overflow-auto p-6 flex items-center justify-center">
            <div style={{ width: '100%', maxWidth: '1400px' }}>
              <div
                dangerouslySetInnerHTML={{ __html: svg }}
                className="flex justify-center [&>svg]:max-w-full [&>svg]:!w-full [&>svg]:h-auto"
              />
            </div>
          </div>
        </div>
      )}
    </>
  );
});

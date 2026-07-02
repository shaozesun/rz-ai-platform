import { useState, useRef, useEffect, useCallback } from 'react';
import {
  Upload, Camera, CheckCircle, ChevronDown,
  X, RotateCcw, ShieldAlert, ImageIcon, Loader2, Trash2,
  AlertTriangle, GripHorizontal, Search, Download,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  checkImage, batchCheckImages, getRiskHistory,
  getRiskCheckDetail, deleteRiskCheck, downloadReportDocx,
} from '@/api/risk';
import type { CheckResult, RiskHistoryItem } from '@/types';
import { cn } from '@/lib/utils';

const MAX_HISTORY = 10;

const SEVERITY_CONFIG: Record<string, { badge: 'destructive' | 'warning' | 'success'; color: string; label: string }> = {
  '高': { badge: 'destructive', color: '#dc2626', label: '高风险' },
  '中': { badge: 'warning', color: '#d97706', label: '中风险' },
  '低': { badge: 'success', color: '#16a34a', label: '低风险' },
};

export default function RiskPage() {
  const [loading, setLoading] = useState(false);
  const [results, setResults] = useState<CheckResult[]>([]);
  const [history, setHistory] = useState<RiskHistoryItem[]>([]);
  const [historySearch, setHistorySearch] = useState('');
  const [restoringId, setRestoringId] = useState<string | null>(null);
  const [previewUrls, setPreviewUrls] = useState<string[]>([]);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const [resultImages, setResultImages] = useState<string[]>([]);
  const [expandedHazards, setExpandedHazards] = useState<Set<string>>(new Set());

  const [camOpen, setCamOpen] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  const stopCamera = useCallback(() => {
    if (streamRef.current) {
      streamRef.current.getTracks().forEach((t) => t.stop());
      streamRef.current = null;
    }
    setCamOpen(false);
  }, []);

  useEffect(() => () => stopCamera(), [stopCamera]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const records = await getRiskHistory(MAX_HISTORY, 0);
        if (cancelled) return;
        setHistory(records);
        if (records.length > 0) {
          const detail = await getRiskCheckDetail(records[0].check_id);
          if (cancelled) return;
          const r = detail.result;
          setResults([r]);
          setResultImages(detail.thumbnail ? [`data:image/jpeg;base64,${detail.thumbnail}`] : []);
        }
      } catch { /* 静默失败 */ }
    })();
    return () => { cancelled = true; };
  }, []);

  const startCamera = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: 'environment', width: { ideal: 1280 }, height: { ideal: 720 } },
      });
      streamRef.current = stream;
      setCamOpen(true);
      setTimeout(() => {
        if (videoRef.current) videoRef.current.srcObject = stream;
      }, 100);
    } catch {
      alert('无法打开摄像头，请检查权限设置');
    }
  };

  const capturePhoto = () => {
    const video = videoRef.current;
    const canvas = canvasRef.current;
    if (!video || !canvas) return;
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    ctx.drawImage(video, 0, 0);
    canvas.toBlob((blob) => {
      if (!blob) return;
      const file = new File([blob], `camera-${Date.now()}.jpg`, { type: 'image/jpeg' });
      stopCamera();
      const url = URL.createObjectURL(file);
      setResults([]);
      setPreviewUrls([url]);
      setPendingFiles([file]);
    }, 'image/jpeg', 0.9);
  };

  const startDetection = async () => {
    if (pendingFiles.length === 0) return;
    const files = pendingFiles;
    const urls = previewUrls;
    setPendingFiles([]);
    setLoading(true);
    setResults([]);
    setExpandedHazards(new Set());
    try {
      let allResults: CheckResult[];
      if (files.length === 1) {
        const res = await checkImage(files[0]);
        allResults = [res];
      } else {
        const batchRes = await batchCheckImages(files);
        allResults = batchRes.results;
      }
      setResults(allResults);
      setResultImages(urls);
      try {
        const records = await getRiskHistory(MAX_HISTORY, 0);
        setHistory(records);
      } catch { /* 静默失败 */ }
    } catch {
      setResults([{
        ok: false, check_id: '', image_name: files.map(f => f.name).join(', '),
        hazards: [], summary: '检测失败', error: '检测失败，请稍后重试', checked_at: '',
      } as CheckResult]);
    } finally {
      setLoading(false);
    }
  };

  const handleFiles = (files: File[]) => {
    if (files.length === 0) return;
    const fresh = pendingFiles.length === 0;
    setResults([]);
    setResultImages([]);
    setExpandedHazards(new Set());
    if (fresh) {
      setPreviewUrls(files.map((f) => URL.createObjectURL(f)));
      setPendingFiles(files);
    } else {
      setPreviewUrls((prev) => [...prev, ...files.map((f) => URL.createObjectURL(f))]);
      setPendingFiles((prev) => [...prev, ...files]);
    }
  };

  const removePendingFile = (i: number) => {
    setPreviewUrls((prev) => prev.filter((_, j) => j !== i));
    setPendingFiles((prev) => {
      const next = prev.filter((_, j) => j !== i);
      if (next.length === 0) setPreviewUrls([]);
      return next;
    });
  };

  const restoreFromHistory = async (checkId: string) => {
    setRestoringId(checkId);
    try {
      const detail = await getRiskCheckDetail(checkId);
      const r = detail.result;
      setResults([r]);
      setPendingFiles([]);
      setPreviewUrls([]);
      setResultImages(detail.thumbnail ? [`data:image/jpeg;base64,${detail.thumbnail}`] : []);
      setExpandedHazards(new Set());
    } catch { /* 静默失败 */ }
    finally { setRestoringId(null); }
  };

  const clearHistory = async () => {
    const ids = history.map((e) => e.check_id);
    setHistory([]);
    await Promise.allSettled(ids.map((id) => deleteRiskCheck(id)));
  };

  const downloadReport= () => {
    downloadReportDocx(results, '安全隐患检测报告').catch(() => {});
  };

  const deleteHistoryEntry = async (checkId: string) => {
    setHistory((prev) => prev.filter((e) => e.check_id !== checkId));
    await deleteRiskCheck(checkId).catch(() => {});
  };

  const toggleHazard = (resultIdx: number, hazardIdx: number) => {
    const key = `${resultIdx}-${hazardIdx}`;
    setExpandedHazards((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const fileList = e.dataTransfer.files;
    if (fileList && fileList.length > 0) {
      const files = Array.from(fileList).filter((f) => f.type.startsWith('image/'));
      if (files.length > 0) handleFiles(files);
    }
  };

  const hasPending = pendingFiles.length > 0;
  const hasResults = results.length > 0;

  const filteredHistory = historySearch
    ? history.filter((e) => e.image_name.toLowerCase().includes(historySearch.toLowerCase()))
    : history;

  // Stats
  const totalHazards = results.reduce((acc, r) => acc + r.hazards.length, 0);
  const highCount = results.reduce((acc, r) => acc + r.hazards.filter(h => h.severity === '高').length, 0);
  const mediumCount = results.reduce((acc, r) => acc + r.hazards.filter(h => h.severity === '中').length, 0);

  return (
    <PlatformShell title="隐患识别" description="AI 图像安全隐患智能检测">
      <div className="w-full max-w-6xl space-y-6">
        {/* hidden inputs */}
        <input
          ref={fileRef}
          type="file"
          className="hidden"
          accept="image/*"
          multiple
          onChange={(e) => {
            const fileList = e.target.files;
            if (fileList && fileList.length > 0) handleFiles(Array.from(fileList));
            e.target.value = '';
          }}
        />
        <canvas ref={canvasRef} className="hidden" />

        {/* ========== Upload Card ========== */}
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Upload className="size-5 text-primary" />
              <CardTitle>图片上传</CardTitle>
            </div>
            <CardDescription>上传现场图片，AI 自动进行安全隐患识别与评估</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Buttons */}
            <div className="flex flex-wrap items-center gap-2">
              <Button onClick={() => fileRef.current?.click()} disabled={loading}>
                <Upload className="size-4" />
                上传图片
              </Button>
              <Button onClick={startCamera} disabled={loading || camOpen} variant="outline">
                <Camera className="size-4" />
                拍照检测
              </Button>
              <span className="text-xs text-muted-foreground hidden sm:inline">或拖放图片到下方区域</span>
            </div>

            {/* Camera feed */}
            {camOpen && (
              <div className="relative overflow-hidden rounded-xl border border-border bg-black">
                <video
                  ref={videoRef}
                  autoPlay
                  playsInline
                  className="w-full max-h-72 object-contain"
                />
                <div className="absolute bottom-3 left-0 right-0 flex justify-center gap-3">
                  <Button variant="outline" size="icon-sm" onClick={stopCamera} className="rounded-full bg-background/80">
                    <X className="size-4" />
                  </Button>
                  <Button size="icon-sm" onClick={capturePhoto} className="rounded-full size-10">
                    <Camera className="size-4" />
                  </Button>
                  <Button variant="ghost" size="icon-sm" onClick={() => { stopCamera(); startCamera(); }} className="rounded-full bg-background/80">
                    <RotateCcw className="size-4" />
                  </Button>
                </div>
              </div>
            )}

            {/* Drag-drop or preview */}
            {!loading && !hasPending && (
              <div
                className={cn(
                  'flex flex-col items-center justify-center rounded-lg border-2 border-dashed py-10 transition-colors',
                  dragOver ? 'border-primary bg-primary/5' : 'border-border bg-muted/20',
                )}
                onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                onDragLeave={() => setDragOver(false)}
                onDrop={handleDrop}
              >
                <GripHorizontal className="size-6 text-muted-foreground mb-2" />
                <p className="text-sm text-muted-foreground">
                  拖放图片到此处，或点击上方按钮选择文件
                </p>
                <p className="text-xs text-muted-foreground mt-1">
                  支持 JPG、PNG、WebP、BMP，单次最多 20 张
                </p>
              </div>
            )}

            {/* Preview grid — always visible when has pending files */}
            {hasPending && (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
                  {previewUrls.map((url, i) => (
                    <div key={i} className="group relative aspect-square overflow-hidden rounded-xl border border-border bg-muted/30">
                      <img
                        src={url}
                        alt={`预览 ${i + 1}`}
                        className="size-full object-cover"
                      />
                      {!loading && (
                        <button
                          onClick={() => removePendingFile(i)}
                          className="absolute right-2 top-2 flex size-6 items-center justify-center rounded-full bg-black/50 text-white hover:bg-destructive"
                          title="移除"
                        >
                          <X className="size-3.5" />
                        </button>
                      )}
                      <span className="absolute bottom-0 left-0 right-0 bg-gradient-to-t from-black/50 to-transparent px-2.5 py-3 text-xs text-white">
                        图片 {i + 1}
                      </span>
                    </div>
                  ))}
                </div>
                {!loading && (
                  <Button onClick={startDetection} size="lg" className="w-full sm:w-auto">
                    <ShieldAlert className="size-4" />
                    开始检测 ({pendingFiles.length} 张)
                  </Button>
                )}
              </div>
            )}
          </CardContent>
        </Card>

        {/* ========== Loading placeholder ========== */}
        {loading && (
          <Card className="border-primary/30 bg-primary/5">
            <CardContent className="flex flex-col items-center gap-3 py-8">
              <div className="size-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
              <p className="text-sm text-muted-foreground">AI 正在分析 {previewUrls.length} 张图像中的安全隐患，请稍候...</p>
            </CardContent>
          </Card>
        )}

        {/* ========== Error ========== */}
        {hasResults && results.some(r => r.error || !r.ok) && (
          <Card className="border-destructive/50 bg-destructive/5">
            <CardContent className="flex items-center gap-3 py-4">
              <AlertTriangle className="size-5 text-destructive shrink-0" />
              <div>
                {results.filter(r => r.error || !r.ok).map((r, i) => (
                  <p key={i} className="text-sm text-destructive">{r.error || '检测失败'}</p>
                ))}
              </div>
            </CardContent>
          </Card>
        )}

        {/* ========== Results ========== */}
        {hasResults && results.filter(r => r.ok && !r.error).length > 0 && (
          <>
            {/* Summary stats */}
            {totalHazards > 0 && (
              <Card className="bg-linear-to-br from-muted/50 to-muted/30">
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <CheckCircle className="size-5 text-success" />
                      <CardTitle>检测总览</CardTitle>
                    </div>
                    <Badge variant={highCount > 0 ? 'destructive' : mediumCount > 0 ? 'warning' : 'success'}>
                      {highCount > 0 ? '高风险' : mediumCount > 0 ? '中风险' : '低风险'}隐患
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="mb-4 grid grid-cols-3 gap-3">
                    <div className="flex flex-col items-center rounded-lg border border-red-200 bg-red-50 px-3 py-2.5">
                      <span className="text-xl font-bold text-red-600">{highCount}</span>
                      <span className="text-xs text-muted-foreground">高风险隐患</span>
                    </div>
                    <div className="flex flex-col items-center rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5">
                      <span className="text-xl font-bold text-amber-600">{mediumCount}</span>
                      <span className="text-xs text-muted-foreground">中风险隐患</span>
                    </div>
                    <div className="flex flex-col items-center rounded-lg border border-green-200 bg-green-50 px-3 py-2.5">
                      <span className="text-xl font-bold text-green-600">{totalHazards - highCount - mediumCount}</span>
                      <span className="text-xs text-muted-foreground">低风险隐患</span>
                    </div>
                  </div>
                  <p className="text-sm text-muted-foreground">
                    共检测 {results.filter(r => r.ok && !r.error).length} 张图片，发现 {totalHazards} 项安全隐患
                  </p>
                  <div className="mt-4 flex flex-wrap gap-2">
                    <Button variant="outline" onClick={downloadReport}>
                      <Download className="size-4" />
                      下载 Word 报告
                    </Button>
                  </div>
                </CardContent>
              </Card>
            )}

            {/* Per-result cards */}
            {results.filter(r => r.ok && !r.error).map((result, ri) => (
              <Card key={result.check_id || ri}>
                <CardHeader>
                  <div className="flex items-center gap-2">
                    {result.hazards.length > 0 ? (
                      <AlertTriangle className="size-5 text-destructive" />
                    ) : (
                      <CheckCircle className="size-5 text-success" />
                    )}
                    <CardTitle className="truncate">{result.image_name}</CardTitle>
                    {result.hazards.length > 0 && (
                      <Badge variant="destructive">{result.hazards.length} 项隐患</Badge>
                    )}
                  </div>
                  <CardDescription>{result.description || result.summary || (result.hazards.length === 0 ? '未发现安全隐患' : '')}</CardDescription>
                </CardHeader>
                <CardContent>
                  <div className="gap-5 lg:grid lg:grid-cols-5">
                    {/* Image */}
                    <div className="lg:col-span-2 mb-4 lg:mb-0">
                      {resultImages[ri] || previewUrls[ri] ? (
                        <img
                          src={resultImages[ri] || previewUrls[ri]}
                          alt={result.image_name}
                          className="w-full rounded-xl border border-border object-contain max-h-80 bg-muted/20"
                        />
                      ) : (
                        <div className="flex items-center justify-center rounded-xl border border-border bg-muted/20 h-48">
                          <ImageIcon className="size-10 text-muted-foreground" />
                        </div>
                      )}
                    </div>

                    {/* Hazard list */}
                    <div className="lg:col-span-3 space-y-2">
                      {result.hazards.length > 0 ? (
                        result.hazards.map((h, hi) => {
                          const cfg = SEVERITY_CONFIG[h.severity] || SEVERITY_CONFIG['低'];
                          const expanded = expandedHazards.has(`${ri}-${hi}`);
                          return (
                            <div
                              key={hi}
                              className="rounded-lg border border-border transition-shadow hover:shadow-sm"
                              style={{ borderLeftColor: cfg.color, borderLeftWidth: '3px' }}
                            >
                              <button
                                onClick={() => toggleHazard(ri, hi)}
                                className="flex w-full items-center gap-2 px-3 py-2.5 text-left"
                              >
                                <span className="text-xs font-semibold" style={{ color: cfg.color }}>
                                  {h.severity}
                                </span>
                                <Badge variant={cfg.badge} className="shrink-0 text-[10px]">{cfg.label}</Badge>
                                <span className="text-sm font-medium truncate">{h.category}</span>
                                <span className="text-xs text-muted-foreground truncate hidden sm:inline ml-2">
                                  {h.location}
                                </span>
                                <ChevronDown
                                  className={cn(
                                    'ml-auto size-4 shrink-0 text-muted-foreground transition-transform duration-200',
                                    expanded && 'rotate-180',
                                  )}
                                />
                              </button>
                              <div
                                className={cn(
                                  'grid transition-all duration-200',
                                  expanded ? 'grid-rows-[1fr] opacity-100' : 'grid-rows-[0fr] opacity-0',
                                )}
                              >
                                <div className="overflow-hidden">
                                  <div className="border-t border-border px-3 py-2.5 space-y-1.5 text-sm">
                                    <p><span className="font-medium text-foreground">位置：</span>{h.location}</p>
                                    <p><span className="font-medium text-foreground">描述：</span>{h.description}</p>
                                    {h.recommendation && (
                                      <p><span className="font-medium text-foreground">整改建议：</span>{h.recommendation}</p>
                                    )}
                                    {h.reference && (
                                      <div className="text-xs text-muted-foreground border-t border-border pt-2 mt-1.5">
                                        <span className="font-medium text-foreground">参考规范：</span>{h.reference}
                                      </div>
                                    )}
                                  </div>
                                </div>
                              </div>
                            </div>
                          );
                        })
                      ) : (
                        <div className="flex flex-col items-center gap-3 py-8 text-center">
                          <CheckCircle className="size-10 text-emerald-500" />
                          <div>
                            <p className="font-medium text-sm">检测通过</p>
                            <p className="text-sm text-muted-foreground mt-1">未发现明显安全隐患</p>
                          </div>
                        </div>
                      )}
                    </div>
                  </div>
                </CardContent>
              </Card>
            ))}
          </>
        )}

        {/* ========== History ========== */}
        {history.length > 0 && (
          <Card>
            <CardHeader>
              <div className="flex flex-row items-center justify-between">
                <div>
                  <CardTitle>检测历史</CardTitle>
                  <CardDescription>点击可查看过往检测详情</CardDescription>
                </div>
                <div className="flex items-center gap-2">
                  <Button variant="outline" size="xs" onClick={clearHistory}>清空记录</Button>
                </div>
              </div>
              {history.length > 5 && (
                <div className="relative mt-2">
                  <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                  <input
                    placeholder="搜索记录..."
                    value={historySearch}
                    onChange={(e) => setHistorySearch(e.target.value)}
                    className="h-8 w-full rounded-md border border-border bg-secondary/60 pl-8 pr-2 text-sm outline-none focus:border-ring focus:bg-card"
                  />
                </div>
              )}
            </CardHeader>
            <CardContent>
              <div className="space-y-2">
                {filteredHistory.map((entry) => {
                  const thumbSrc = entry.thumbnail
                    ? `data:image/jpeg;base64,${entry.thumbnail}`
                    : '';
                  const isRestoring = restoringId === entry.check_id;
                  const riskColor = entry.risk_level === '高' ? '#dc2626'
                    : entry.risk_level === '中' ? '#d97706'
                    : entry.risk_level === '低' ? '#16a34a'
                    : '#9ca3af';
                  return (
                    <div
                      key={entry.check_id}
                      className="group relative cursor-pointer rounded-lg border border-border p-3 hover:shadow-sm transition-shadow"
                      style={{ borderTopWidth: '3px', borderTopColor: riskColor }}
                      onClick={() => restoreFromHistory(entry.check_id)}
                    >
                      <button
                        className="absolute right-2 top-2 rounded p-0.5 text-muted-foreground opacity-0 hover:bg-destructive/10 hover:text-destructive group-hover:opacity-100 transition-opacity"
                        onClick={(e) => { e.stopPropagation(); deleteHistoryEntry(entry.check_id); }}
                      >
                        &times;
                      </button>
                      <div className="flex items-center gap-3 pr-6">
                        {thumbSrc ? (
                          <img src={thumbSrc} alt="" className="size-10 shrink-0 rounded-md object-cover" />
                        ) : (
                          <div className="flex size-10 shrink-0 items-center justify-center rounded-md bg-muted">
                            {isRestoring
                              ? <Loader2 className="size-4 animate-spin text-muted-foreground" />
                              : <ImageIcon className="size-4 text-muted-foreground" />
                            }
                          </div>
                        )}
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-sm font-medium text-foreground">{entry.image_name}</p>
                          {(entry.description || entry.summary) && (
                            <p className="truncate text-xs text-muted-foreground mt-0.5">{entry.description || entry.summary}</p>
                          )}
                          <div className="mt-1 flex items-center gap-2 text-xs text-muted-foreground">
                            {entry.risk_level && (
                              <Badge variant={entry.risk_level === '高' || entry.risk_level === 'critical' ? 'destructive' : entry.risk_level === '中' ? 'warning' : 'success'} className="text-xs">
                                {SEVERITY_CONFIG[entry.risk_level]?.label || entry.risk_level}
                              </Badge>
                            )}
                            <span>
                              {entry.checked_at
                                ? new Date(entry.checked_at).toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
                                : ''}
                            </span>
                          </div>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </CardContent>
          </Card>
        )}
      </div>
    </PlatformShell>
  );
}

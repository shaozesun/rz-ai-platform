import { useState, useRef, useEffect, useCallback } from 'react';
import {
  FileText, Upload, Search, FileCode2, FileSpreadsheet,
  Folder, CheckCircle2, Trash2, AlertTriangle,
  Database, Loader2, Plus, X,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { uploadFile, deleteFile, getVectorStoreInfo, getGroups, createGroup as createGroupApi, deleteGroupApi } from '@/api/rag';
import { useAuthStore } from '@/stores/authStore';

interface DocItem {
  name: string;
  size: number;
  chunks: number;
  updatedAt: string;
}

const ACCEPT = '.pdf,.docx,.xlsx,.xls,.csv,.txt,.md';

function fileIcon(name: string) {
  const ext = name.split('.').pop()?.toLowerCase();
  switch (ext) {
    case 'md': return FileCode2;
    case 'xlsx':
    case 'xls':
    case 'csv': return FileSpreadsheet;
    default: return FileText;
  }
}

function fileType(name: string) {
  const ext = name.split('.').pop()?.toLowerCase();
  switch (ext) {
    case 'pdf': return 'PDF';
    case 'docx': return 'DOCX';
    case 'md': return 'Markdown';
    case 'xlsx':
    case 'xls': return 'Excel';
    case 'csv': return 'CSV';
    case 'txt': return 'TXT';
    default: return ext?.toUpperCase() || 'FILE';
  }
}

function fmtSize(bytes: number) {
  if (bytes > 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${Math.round(bytes / 1024)} KB`;
}

function fmtTime(ts: string) {
  if (!ts) return '';
  const d = new Date(ts);
  const now = Date.now();
  const diff = now - d.getTime();
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  if (mins < 1) return '刚刚';
  if (mins < 60) return `${mins} 分钟前`;
  if (hours < 24) return `${hours} 小时前`;
  return d.toLocaleDateString('zh-CN');
}

export default function KnowledgePage() {
  const [uploading, setUploading] = useState<{ current: number; total: number } | null>(null);
  const [docs, setDocs] = useState<DocItem[]>([]);
  const [search, setSearch] = useState('');
  const [logMsg, setLogMsg] = useState('');
  const [loadInfo, setLoadInfo] = useState(false);
  const [groups, setGroups] = useState<{ group_id: string; name: string }[]>([]);
  const [activeGroup, setActiveGroup] = useState<string>('默认知识库');
  const [showCreateDialog, setShowCreateDialog] = useState(false);
  const [newGroupName, setNewGroupName] = useState('');
  const [totalDocs, setTotalDocs] = useState(0);
  const [totalChunksAll, setTotalChunksAll] = useState(0);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { hasPermission } = useAuthStore();

  const canUpload = hasPermission('ai:knowledge');
  const canDelete = hasPermission('ai:knowledge');

  const showMsg = useCallback((msg: string) => {
    setLogMsg(msg);
    setTimeout(() => setLogMsg(''), 4000);
  }, []);

  const loadGroups = useCallback(async () => {
    try {
      const res = await getGroups();
      if (res.ok && res.groups.length > 0) {
        setGroups(res.groups);
        if (!res.groups.find((g) => g.group_id === activeGroup)) {
          setActiveGroup(res.groups[0].group_id);
        }
        // Count total docs across all groups
        let total = 0;
        let totalChunks = 0;
        const results = await Promise.allSettled(
          res.groups.map((g) => getVectorStoreInfo(g.group_id)),
        );
        for (const r of results) {
          if (r.status === 'fulfilled' && r.value.ok) {
            const d = r.value.data as Record<string, unknown>;
            const sources = d?.sources as Record<string, number> | undefined;
            if (sources) total += Object.keys(sources).length;
            totalChunks += (d?.total_chunks as number) || 0;
          }
        }
        setTotalDocs(total);
        setTotalChunksAll(totalChunks);
      }
    } catch { /* ignore */ }
  }, []);

  const loadFiles = useCallback(async (groupId: string) => {
    setLoadInfo(false);
    setDocs([]);
    try {
      const res = await getVectorStoreInfo(groupId);
      if (res.ok) {
        const d = res.data as Record<string, unknown>;
        if (d) {
          const sources = d.sources as Record<string, { chunks: number; size: number }> | undefined;
          if (sources && typeof sources === 'object') {
            const files: DocItem[] = Object.entries(sources).map(([path, info]) => ({
              name: path.split('/').pop() || path,
              size: info.size || 0,
              chunks: info.chunks || 0,
              updatedAt: '',
            }));
            setDocs(files);
          }
        }
      }
    } catch { /* ignore */ }
    setLoadInfo(true);
  }, []);

  useEffect(() => { loadGroups(); }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (groups.length > 0 || activeGroup === '默认知识库') {
      loadFiles(activeGroup);
    }
  }, [activeGroup, groups.length, loadFiles]);

  const handleUploadFiles = async (files: FileList | File[]) => {
    const list = Array.from(files);
    if (list.length === 0) return;
    setUploading({ current: 0, total: list.length });
    let success = 0;
    let fail = 0;
    for (let i = 0; i < list.length; i++) {
      const file = list[i];
      setUploading({ current: i + 1, total: list.length });
      try {
        const res = await uploadFile(file, activeGroup);
        if (res.ok) {
          success++;
          setDocs((prev) => {
            const filtered = prev.filter((f) => f.name !== res.name);
            return [{ name: res.name, size: res.size, chunks: res.chunks, updatedAt: new Date().toISOString() }, ...filtered];
          });
        } else {
          fail++;
        }
      } catch {
        fail++;
      }
    }
    setUploading(null);
    if (fail === 0) {
      showMsg(`全部上传成功: ${success} 个文件`);
    } else if (success === 0) {
      showMsg('全部上传失败');
    } else {
      showMsg(`上传完成: ${success} 成功, ${fail} 失败`);
    }
  };

  const handleDelete = async (name: string) => {
    try {
      const res = await deleteFile(name, activeGroup);
      if (res.ok) {
        showMsg(`已删除: ${name}`);
        setDocs((prev) => prev.filter((f) => f.name !== name));
      }
    } catch {
      showMsg('删除失败');
    }
  };

  const handleCreateGroup = async () => {
    const trimmed = newGroupName.trim();
    if (!trimmed) return;
    try {
      const res = await createGroupApi(trimmed);
      if (res.ok) {
        setGroups((prev) => [...prev, res.group]);
        setActiveGroup(res.group.group_id);
        setNewGroupName('');
        setShowCreateDialog(false);
        showMsg(`已创建知识库: ${trimmed}`);
      } else {
        showMsg((res as unknown as { msg?: string }).msg || '创建失败');
      }
    } catch {
      showMsg('创建失败');
    }
  };

  const handleDeleteGroup = async (groupId: string) => {
    if (groupId === 'default') {
      showMsg('默认知识库不可删除');
      return;
    }
    try {
      const res = await deleteGroupApi(groupId);
      if (res.ok) {
        setGroups((prev) => prev.filter((g) => g.group_id !== groupId));
        if (activeGroup === groupId) {
          setActiveGroup('默认知识库');
          loadFiles('默认知识库');
        }
        showMsg('知识库已删除');
      }
    } catch {
      showMsg('删除失败');
    }
  };

  const activeGroupName = groups.find((g) => g.group_id === activeGroup)?.name || activeGroup;
  const filtered = search
    ? docs.filter((d) => d.name.toLowerCase().includes(search.toLowerCase()))
    : docs;

  const totalChunks = docs.reduce((sum, d) => sum + (d.chunks || 0), 0);

  return (
    <PlatformShell
      title="知识库"
      description="管理企业文档与数据源，为对话助手提供可溯源的知识检索能力"
      actions={
        <div className="flex items-center gap-2">
          {canUpload && (
            <>
              <Button variant="outline" size="sm" onClick={() => fileInputRef.current?.click()}>
                <Upload className="size-4" />
                上传文档
              </Button>
              <Button size="sm" onClick={() => setShowCreateDialog(true)}>
                <Plus className="size-4" />
                新建知识库
              </Button>
            </>
          )}
        </div>
      }
    >
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        className="hidden"
        accept={ACCEPT}
        multiple
        onChange={(e) => {
          if (e.target.files && e.target.files.length > 0) handleUploadFiles(e.target.files);
          e.target.value = '';
        }}
      />

      {/* Create group dialog */}
      {showCreateDialog && (
        <div className="fixed inset-0 z-50 flex items-center justify-center">
          <div className="absolute inset-0 bg-foreground/30" onClick={() => setShowCreateDialog(false)} />
          <div className="relative z-10 w-full max-w-sm rounded-xl border border-border bg-card p-6 shadow-lg">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-lg font-semibold">新建知识库</h3>
              <button
                onClick={() => setShowCreateDialog(false)}
                className="rounded-md p-1 text-muted-foreground hover:bg-accent"
              >
                <X className="size-4" />
              </button>
            </div>
            <label className="block text-sm font-medium mb-1.5">知识库名称</label>
            <input
              type="text"
              value={newGroupName}
              onChange={(e) => setNewGroupName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') handleCreateGroup(); }}
              placeholder="例如：技术文档、产品手册"
              className="h-9 w-full rounded-lg border border-border bg-secondary/60 px-3 text-sm outline-none placeholder:text-muted-foreground focus:border-ring focus:bg-card"
              autoFocus
            />
            <div className="flex justify-end gap-2 mt-4">
              <Button variant="outline" size="sm" onClick={() => setShowCreateDialog(false)}>取消</Button>
              <Button size="sm" onClick={handleCreateGroup} disabled={!newGroupName.trim()}>创建</Button>
            </div>
          </div>
        </div>
      )}

      {/* Stats row */}
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3 sm:gap-4">
        {[
          { label: '知识库总数', value: String(groups.length), icon: Database, tone: 'text-primary bg-primary/10' },
          { label: '文档总数', value: String(totalDocs), icon: FileText, tone: 'text-chart-1 bg-chart-1/12' },
          { label: '切片总数', value: String(totalChunksAll || '—'), icon: FileCode2, tone: 'text-chart-2 bg-chart-2/12' },
          { label: '支持格式', value: '7 种', icon: Search, tone: 'text-chart-3 bg-chart-3/12' },
        ].map((s) => {
          const Icon = s.icon;
          return (
            <Card key={s.label}>
              <CardContent className="flex items-center gap-3 sm:gap-4 p-4 sm:p-5">
                <div className={`flex size-11 items-center justify-center rounded-lg ${s.tone}`}>
                  <Icon className="size-5" />
                </div>
                <div>
                  <p className="text-sm text-muted-foreground">{s.label}</p>
                  <p className="text-2xl font-semibold tracking-tight">{s.value}</p>
                </div>
              </CardContent>
            </Card>
          );
        })}
      </div>

      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-4">
        {/* Left: group list */}
        <Card className="lg:col-span-1">
          <CardHeader>
            <CardTitle>知识库</CardTitle>
            <p className="text-sm text-muted-foreground">按分组划分</p>
          </CardHeader>
          <CardContent className="space-y-1 p-2">
            {groups.map((group) => (
              <div key={group.group_id} className="group flex w-full items-center">
                <button
                  className={`flex flex-1 items-center gap-3 rounded-lg px-3 py-2.5 text-left text-sm font-medium transition-colors ${
                    activeGroup === group.group_id
                      ? 'bg-secondary text-foreground'
                      : 'text-muted-foreground hover:bg-secondary/60 hover:text-foreground'
                  }`}
                  onClick={() => setActiveGroup(group.group_id)}
                >
                  <Folder className={`size-4 shrink-0 ${activeGroup === group.group_id ? 'text-primary' : ''}`} />
                  <span className="flex-1 truncate">{group.name}</span>
                </button>
                {group.group_id !== 'default' && (
                  <button
                    onClick={(e) => { e.stopPropagation(); handleDeleteGroup(group.group_id); }}
                    className="shrink-0 rounded p-1 text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-destructive transition-opacity"
                    title="删除知识库"
                  >
                    <Trash2 className="size-3.5" />
                  </button>
                )}
              </div>
            ))}
          </CardContent>
        </Card>

        {/* Right: document list */}
        <Card className="lg:col-span-3">
          <CardHeader className="flex-row items-center justify-between">
            <div>
              <CardTitle>{activeGroupName}</CardTitle>
              <p className="mt-1 text-sm text-muted-foreground">
                {docs.length} 个文档 · {totalChunks || 0} 个切片 · 自动向量化
              </p>
            </div>
            <div className="relative hidden sm:block">
              <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                placeholder="搜索文档…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-9 w-56 rounded-lg border border-border bg-secondary/60 pl-9 pr-3 text-sm outline-none transition-colors placeholder:text-muted-foreground focus:border-ring focus:bg-card"
              />
            </div>
          </CardHeader>
          <CardContent className="p-0">
            {/* Uploading indicator */}
            {uploading && (
              <div className="flex items-center gap-3 px-5 py-3 text-sm text-muted-foreground border-b border-border">
                <Loader2 className="size-4 animate-spin" />
                上传中 {uploading.current}/{uploading.total}...
              </div>
            )}

            {/* Log message */}
            {logMsg && (
              <div className="flex items-center gap-3 px-5 py-3 text-sm bg-accent/50 border-b border-border">
                {logMsg}
              </div>
            )}

            {/* Permission warning */}
            {!canUpload && (
              <div className="flex items-center gap-3 px-5 py-3 text-sm text-muted-foreground bg-warning/5 border-b border-border">
                <AlertTriangle className="size-4 text-warning shrink-0" />
                需要 ai:knowledge 权限才能上传文件
              </div>
            )}

            {/* Mobile search */}
            <div className="relative sm:hidden px-4 py-3 border-b border-border">
              <Search className="pointer-events-none absolute left-7 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <input
                type="search"
                placeholder="搜索文档…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                className="h-9 w-full rounded-lg border border-border bg-secondary/60 pl-9 pr-3 text-sm outline-none transition-colors placeholder:text-muted-foreground focus:border-ring focus:bg-card"
              />
            </div>

            {/* Drag-to-upload overlay hint */}
            {canUpload && docs.length === 0 && !uploading && loadInfo && (
              <div
                className="flex flex-col items-center justify-center gap-2 px-5 py-12 text-muted-foreground cursor-pointer hover:bg-secondary/30 transition-colors"
                onClick={() => fileInputRef.current?.click()}
                onDragOver={(e) => { e.preventDefault(); }}
                onDrop={(e) => {
                  e.preventDefault();
                  if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                    handleUploadFiles(e.dataTransfer.files);
                  }
                }}
              >
                <Upload className="size-10 opacity-30" />
                <p className="text-sm">点击或拖拽文件到此处上传（支持批量）</p>
                <p className="text-xs">支持 PDF、Word、Excel、CSV、TXT、Markdown</p>
              </div>
            )}

            {/* Doc list */}
            {filtered.length > 0 && (
              <div className="divide-y divide-border">
                {filtered.map((d, i) => {
                  const Icon = fileIcon(d.name);
                  return (
                    <div
                      key={d.name + i}
                      className="flex items-center gap-4 px-5 py-4 transition-colors hover:bg-secondary/50"
                      onDragOver={(e) => { if (canUpload) e.preventDefault(); }}
                      onDrop={(e) => {
                        if (!canUpload) return;
                        e.preventDefault();
                        if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
                          handleUploadFiles(e.dataTransfer.files);
                        }
                      }}
                    >
                      <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-secondary text-muted-foreground">
                        <Icon className="size-5" />
                      </div>
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-sm font-medium">{d.name}</p>
                        <p className="mt-0.5 text-xs text-muted-foreground">
                          {fileType(d.name)} · {fmtSize(d.size)}
                          {d.chunks > 0 && ` · ${d.chunks} 切片`}
                          {d.updatedAt && ` · ${fmtTime(d.updatedAt)}`}
                        </p>
                      </div>
                      <Badge variant={d.chunks > 0 ? 'success' : 'warning'} className="shrink-0">
                        <CheckCircle2 className="size-3" />
                        {d.chunks > 0 ? '已就绪' : '已上传'}
                      </Badge>
                      {canDelete && (
                        <Button
                          variant="ghost"
                          size="icon-sm"
                          aria-label="删除"
                          onClick={() => handleDelete(d.name)}
                          className="text-muted-foreground hover:text-destructive"
                        >
                          <Trash2 className="size-4" />
                        </Button>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {/* No results */}
            {search && filtered.length === 0 && docs.length > 0 && (
              <div className="px-5 py-12 text-center text-sm text-muted-foreground">
                没有匹配 "{search}" 的文档
              </div>
            )}

            {/* Empty state */}
            {!loadInfo && docs.length === 0 && (
              <div className="flex items-center gap-3 px-5 py-12 text-center text-sm text-muted-foreground justify-center">
                <Loader2 className="size-4 animate-spin" /> 加载中...
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </PlatformShell>
  );
}

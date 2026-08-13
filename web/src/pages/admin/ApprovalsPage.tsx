import { useState, useEffect } from 'react';
import { X, Check, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent } from '@/components/ui/card';
import { getApplications, approveApplication, rejectApplication, batchApproveApplications, batchRejectApplications } from '../../api/admin';
import type { Application } from '../../types';

const PERM_LABELS: Record<string, string> = {
  'ai:chat': 'AI 对话',
  'ai:knowledge': '知识库管理',
  'ai:risk': '隐患识别',
  'ai:fire_safety': '消防配置',
  'ai:video': '视频生成',
};

const ROLE_LABELS: Record<string, string> = {
  admin: '管理员',
  power_user: '全功能用户',
  user: '普通用户',
};

const PAGE_SIZE = 20;

export default function ApprovalsPage() {
  const [applications, setApplications] = useState<Application[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [statusFilter, setStatusFilter] = useState('');
  const [search, setSearch] = useState('');
  const [rejectModal, setRejectModal] = useState<{ id: string; reason: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [batchApproving, setBatchApproving] = useState(false);
  const [batchRejectModal, setBatchRejectModal] = useState<{ reason: string } | null>(null);
  const [batchRejecting, setBatchRejecting] = useState(false);

  const showMsg = (text: string) => { setMsg(text); setTimeout(() => setMsg(''), 3000); };

  const fetchApps = async () => {
    setLoading(true);
    try {
      const res = await getApplications({ page, page_size: PAGE_SIZE, status: statusFilter, search });
      setApplications(res.items ?? []);
      setTotal(res.total ?? 0);
    } catch { showMsg('获取申请列表失败'); }
    finally { setLoading(false); }
  };

  useEffect(() => { setSelected(new Set()); fetchApps(); }, [page, statusFilter, search]);

  const pendingApps = applications.filter((a) => a.status === 'pending');

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  };

  const toggleSelectAll = () => {
    if (selected.size === pendingApps.length && pendingApps.length > 0) {
      setSelected(new Set());
    } else {
      setSelected(new Set(pendingApps.map((a) => a.application_id)));
    }
  };

  const handleApprove = async (id: string) => {
    try { await approveApplication(id); showMsg('已审批通过'); fetchApps(); }
    catch { showMsg('操作失败'); }
  };

  const handleBatchApprove = async () => {
    if (selected.size === 0) return;
    setBatchApproving(true);
    try {
      const res = await batchApproveApplications([...selected]);
      const { ok, fail } = res.data;
      showMsg(fail === 0 ? `已批量通过 ${ok} 条申请` : `通过 ${ok} 条，失败 ${fail} 条`);
    } catch {
      showMsg('操作失败');
    }
    setSelected(new Set());
    setBatchApproving(false);
    fetchApps();
  };

  const handleBatchRejectSubmit = async () => {
    if (!batchRejectModal) return;
    setBatchRejecting(true);
    try {
      const res = await batchRejectApplications([...selected], batchRejectModal.reason);
      const { ok, fail } = res.data;
      showMsg(fail === 0 ? `已批量驳回 ${ok} 条申请` : `驳回 ${ok} 条，失败 ${fail} 条`);
    } catch {
      showMsg('操作失败');
    }
    setSelected(new Set());
    setBatchRejecting(false);
    setBatchRejectModal(null);
    fetchApps();
  };

  const handleReject = async () => {
    if (!rejectModal) return;
    try { await rejectApplication(rejectModal.id, rejectModal.reason); showMsg('已驳回'); setRejectModal(null); fetchApps(); }
    catch { showMsg('操作失败'); }
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);

  const statusBadge = (status: string) => {
    switch (status) {
      case 'pending': return <Badge variant="warning">待审批</Badge>;
      case 'approved': return <Badge variant="success">已通过</Badge>;
      case 'rejected': return <Badge variant="destructive">已驳回</Badge>;
      default: return <Badge>{status}</Badge>;
    }
  };

  return (
    <div>
      <h2 className="mb-4 text-lg font-semibold">审批管理</h2>

      <div className="mb-4 flex flex-wrap items-center gap-3">
        <input
          type="text"
          className="h-9 w-64 rounded-lg border border-border bg-card px-3 text-sm placeholder:text-muted-foreground"
          placeholder="搜索申请人名称或手机号"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(1); }}
        />
        <select
          className="h-9 rounded-lg border border-border bg-card px-3 text-sm"
          value={statusFilter}
          onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}
        >
          <option value="">全部状态</option>
          <option value="pending">待审批</option>
          <option value="approved">已通过</option>
          <option value="rejected">已驳回</option>
        </select>
        {selected.size > 0 && (
          <>
            <Button
              size="sm"
              onClick={handleBatchApprove}
              disabled={batchApproving || batchRejecting}
            >
              {batchApproving ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <Check className="size-3.5" />
              )}
              <span className="ml-1">一键审批 ({selected.size})</span>
            </Button>
            <Button
              size="sm"
              variant="destructive"
              onClick={() => setBatchRejectModal({ reason: '' })}
              disabled={batchApproving || batchRejecting}
            >
              <X className="size-3.5" />
              <span className="ml-1">一键驳回 ({selected.size})</span>
            </Button>
          </>
        )}
      </div>

      {msg && <p className="mb-3 rounded-lg bg-accent px-3 py-1.5 text-sm text-accent-foreground">{msg}</p>}

      <Card>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-muted-foreground">
                  <th className="w-10 px-3 py-2.5">
                    <button
                      className="flex size-4 items-center justify-center rounded border border-muted-foreground/30 hover:border-primary"
                      onClick={toggleSelectAll}
                    >
                      {selected.size > 0 && selected.size === pendingApps.length && (
                        <Check className="size-3 text-primary" />
                      )}
                    </button>
                  </th>
                  <th className="px-4 py-2.5 font-medium">申请人</th>
                  <th className="px-4 py-2.5 font-medium">手机号</th>
                  <th className="px-4 py-2.5 font-medium">申请角色</th>
                  <th className="px-4 py-2.5 font-medium">申请权限</th>
                  <th className="px-4 py-2.5 font-medium">原因</th>
                  <th className="px-4 py-2.5 font-medium">状态</th>
                  <th className="px-4 py-2.5 font-medium">时间</th>
                  <th className="px-4 py-2.5 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={9} className="px-4 py-8 text-center text-muted-foreground">加载中...</td></tr>
                ) : applications.length === 0 ? (
                  <tr><td colSpan={9} className="px-4 py-8 text-center text-muted-foreground">暂无申请</td></tr>
                ) : (
                  applications.map((a) => (
                    <tr key={a.application_id} className="border-b border-border">
                      <td className="px-3 py-2.5">
                        {a.status === 'pending' && (
                          <button
                            className="flex size-4 items-center justify-center rounded border border-muted-foreground/30 hover:border-primary"
                            onClick={() => toggleSelect(a.application_id)}
                          >
                            {selected.has(a.application_id) && (
                              <Check className="size-3 text-primary" />
                            )}
                          </button>
                        )}
                      </td>
                      <td className="px-4 py-2.5">{a.name || '-'}</td>
                      <td className="px-4 py-2.5">{a.phone}</td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {a.requested_roles?.map((r) => <Badge key={r} variant="outline">{ROLE_LABELS[r] || r}</Badge>)}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {a.requested_permissions?.length > 0
                            ? a.requested_permissions.map((p) => <Badge key={p} variant="outline">{PERM_LABELS[p] || p}</Badge>)
                            : <span className="text-xs text-muted-foreground">-</span>}
                        </div>
                      </td>
                      <td className="max-w-[180px] px-4 py-2.5 text-muted-foreground">
                        {a.reason === '新用户注册申请' ? (
                          <Badge variant="warning">注册申请</Badge>
                        ) : (
                          <span className="truncate block max-w-[150px]">{a.reason || '-'}</span>
                        )}
                      </td>
                      <td className="px-4 py-2.5">{statusBadge(a.status)}</td>
                      <td className="px-4 py-2.5 text-xs text-muted-foreground">
                        {new Date(a.created_at).toLocaleString('zh-CN')}
                      </td>
                      <td className="px-4 py-2.5">
                        {a.status === 'pending' ? (
                          <div className="flex gap-1.5">
                            <Button size="xs" onClick={() => handleApprove(a.application_id)}>通过</Button>
                            <Button size="xs" variant="destructive" onClick={() => setRejectModal({ id: a.application_id, reason: '' })}>驳回</Button>
                          </div>
                        ) : a.status === 'rejected' && a.review_reason ? (
                          <span className="text-xs text-muted-foreground">{a.review_reason}</span>
                        ) : null}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-border px-4 py-3">
              <span className="text-xs text-muted-foreground">共 {total} 条</span>
              <div className="flex items-center gap-1">
                <Button size="xs" variant="outline" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button>
                <span className="px-2 text-xs text-muted-foreground">{page} / {totalPages}</span>
                <Button size="xs" variant="outline" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>下一页</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Batch Reject Modal */}
      {batchRejectModal && (
        <>
          <div className="fixed inset-0 z-50 bg-foreground/30" onClick={() => setBatchRejectModal(null)} />
          <div className="fixed inset-x-4 top-1/2 z-50 mx-auto max-w-md -translate-y-1/2 rounded-xl border border-border bg-card p-6 shadow-lg">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold">批量驳回 ({selected.size} 条)</h3>
              <button onClick={() => setBatchRejectModal(null)} className="rounded-md p-1 text-muted-foreground hover:bg-accent">
                <X className="size-4" />
              </button>
            </div>
            <Textarea
              placeholder="驳回原因（选填，将应用于所选申请）"
              value={batchRejectModal.reason}
              onChange={(e) => setBatchRejectModal({ reason: e.target.value })}
              rows={3}
            />
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setBatchRejectModal(null)}>取消</Button>
              <Button size="sm" variant="destructive" onClick={handleBatchRejectSubmit} disabled={batchRejecting}>
                {batchRejecting ? '驳回中...' : '确认批量驳回'}
              </Button>
            </div>
          </div>
        </>
      )}

      {/* Reject Modal */}
      {rejectModal && (
        <>
          <div className="fixed inset-0 z-50 bg-foreground/30" onClick={() => setRejectModal(null)} />
          <div className="fixed inset-x-4 top-1/2 z-50 mx-auto max-w-md -translate-y-1/2 rounded-xl border border-border bg-card p-6 shadow-lg">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold">驳回申请</h3>
              <button onClick={() => setRejectModal(null)} className="rounded-md p-1 text-muted-foreground hover:bg-accent">
                <X className="size-4" />
              </button>
            </div>
            <Textarea
              placeholder="驳回原因（选填）"
              value={rejectModal.reason}
              onChange={(e) => setRejectModal({ ...rejectModal, reason: e.target.value })}
              rows={3}
            />
            <div className="mt-4 flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setRejectModal(null)}>取消</Button>
              <Button size="sm" variant="destructive" onClick={handleReject}>确认驳回</Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

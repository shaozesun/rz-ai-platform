import { useState, useEffect } from 'react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { getAuditLogs } from '../../api/admin';
import type { AuditLog } from '../../types';

const PAGE_SIZE = 30;

const ACTION_LABELS: Record<string, string> = {
  'user.apply': '申请权限',
  'admin.update_status': '修改用户状态',
  'admin.update_roles': '修改用户角色',
  'admin.update_permissions': '修改用户权限',
  'admin.reset_password': '重置密码',
  'admin.create_role': '创建角色',
  'admin.update_role': '修改角色',
  'admin.approve': '审批通过',
  'admin.reject': '审批驳回',
};

const ACTIONS = Object.keys(ACTION_LABELS);

export default function AuditLogsPage() {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [actionFilter, setActionFilter] = useState('');
  const [loading, setLoading] = useState(false);

  const fetchLogs = async () => {
    setLoading(true);
    try {
      const res = await getAuditLogs({ page, page_size: PAGE_SIZE, action: actionFilter });
      const items = (res.items ?? []).filter((log) => log.action !== 'user.login');
      setLogs(items);
      setTotal(res.total ?? 0);
    } catch { /* */ }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchLogs(); }, [page, actionFilter]);

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div>
      <h2 className="mb-4 text-lg font-semibold">审计日志</h2>

      <div className="mb-4 flex flex-wrap gap-3">
        <select
          className="h-9 rounded-lg border border-border bg-card px-3 text-sm"
          value={actionFilter}
          onChange={(e) => { setActionFilter(e.target.value); setPage(1); }}
        >
          <option value="">全部操作</option>
          {ACTIONS.map((a) => <option key={a} value={a}>{ACTION_LABELS[a]}</option>)}
        </select>
      </div>

      <Card>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-muted-foreground">
                  <th className="px-4 py-2.5 font-medium">时间</th>
                  <th className="px-4 py-2.5 font-medium">操作人</th>
                  <th className="px-4 py-2.5 font-medium">手机号</th>
                  <th className="px-4 py-2.5 font-medium">操作</th>
                  <th className="px-4 py-2.5 font-medium">详情</th>
                  <th className="px-4 py-2.5 font-medium">IP</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">加载中...</td></tr>
                ) : logs.length === 0 ? (
                  <tr><td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">暂无日志</td></tr>
                ) : (
                  logs.map((log) => (
                    <tr key={log.log_id} className="border-b border-border">
                      <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                        {new Date(log.created_at).toLocaleString('zh-CN')}
                      </td>
                      <td className="px-4 py-2.5 whitespace-nowrap">{log.operator_name || '-'}</td>
                      <td className="px-4 py-2.5 whitespace-nowrap">{log.phone || '-'}</td>
                      <td className="px-4 py-2.5">
                        <Badge variant="outline">{ACTION_LABELS[log.action] || log.action}</Badge>
                      </td>
                      <td className="px-4 py-2.5 text-muted-foreground whitespace-normal break-all">{log.detail || '-'}</td>
                      <td className="px-4 py-2.5 font-mono text-xs text-muted-foreground whitespace-nowrap">{log.ip || '-'}</td>
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
    </div>
  );
}

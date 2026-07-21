import { useState, useEffect } from 'react';
import { Search, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { getUsers, updateUserStatus, updateUserRoles, updateUserPermissions } from '../../api/admin';
import type { AdminUser } from '../../types';

const ALL_ROLES = ['user', 'admin'];
const ALL_PERMISSIONS: { key: string; label: string }[] = [
  { key: 'ai:chat', label: 'AI 对话' },
  { key: 'ai:knowledge', label: '知识库管理' },
  { key: 'ai:risk', label: '隐患识别' },
  { key: 'ai:fire_safety', label: '消防配置' },
  { key: 'ai:video', label: '视频生成' },
];

const PAGE_SIZE = 20;

export default function UsersPage() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [editModal, setEditModal] = useState<{ user: AdminUser; roles: string[]; permissions: string[] } | null>(null);
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');

  const showMsg = (text: string) => { setMsg(text); setTimeout(() => setMsg(''), 3000); };

  const fetchUsers = async () => {
    setLoading(true);
    try {
      const res = await getUsers({ page, page_size: PAGE_SIZE, search, status: statusFilter });
      setUsers(res.items ?? []);
      setTotal(res.total ?? 0);
    } catch {
      showMsg('获取用户列表失败');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { fetchUsers(); }, [page, statusFilter]);

  const handleSearch = () => { setPage(1); fetchUsers(); };

  const handleStatusChange = async (userId: string, status: string) => {
    try { await updateUserStatus(userId, status); showMsg('状态已更新'); fetchUsers(); }
    catch { showMsg('更新失败'); }
  };

  const handleSaveEdit = async () => {
    if (!editModal) return;
    try {
      await updateUserRoles(editModal.user.user_id, editModal.roles);
      // 只有当直接权限被手动改动时才重新写入，避免把刚清空的残留权限写回去
      const origPerms = [...(editModal.user.direct_permissions || [])].sort();
      const currPerms = [...editModal.permissions].sort();
      if (JSON.stringify(origPerms) !== JSON.stringify(currPerms)) {
        await updateUserPermissions(editModal.user.user_id, editModal.permissions);
      }
      showMsg('已更新');
      setEditModal(null);
      fetchUsers();
    } catch { showMsg('更新失败'); }
  };

  const totalPages = Math.ceil(total / PAGE_SIZE);

  return (
    <div>
      <h2 className="mb-4 text-lg font-semibold">用户管理</h2>

      {/* Filters */}
      <div className="mb-4 flex flex-wrap gap-3">
        <div className="relative w-[200px]">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="pl-8"
            placeholder="搜索手机号"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
          />
        </div>
        <select
          className="h-9 rounded-lg border border-border bg-card px-3 text-sm shadow-sm"
          value={statusFilter}
          onChange={(e) => { setStatusFilter(e.target.value); setPage(1); }}
        >
          <option value="">全部状态</option>
          <option value="ACTIVE">正常</option>
          <option value="DISABLED">已禁用</option>
        </select>
        <Button variant="outline" size="sm" onClick={handleSearch}>搜索</Button>
      </div>

      {msg && <p className="mb-3 rounded-lg bg-accent px-3 py-1.5 text-sm text-accent-foreground">{msg}</p>}

      {/* Table */}
      <Card>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-muted-foreground">
                  <th className="px-4 py-2.5 font-medium">手机号</th>
                  <th className="px-4 py-2.5 font-medium">姓名</th>
                  <th className="px-4 py-2.5 font-medium">类型</th>
                  <th className="px-4 py-2.5 font-medium">状态</th>
                  <th className="px-4 py-2.5 font-medium">角色</th>
                  <th className="px-4 py-2.5 font-medium">直接权限</th>
                  <th className="px-4 py-2.5 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">加载中...</td></tr>
                ) : users.length === 0 ? (
                  <tr><td colSpan={7} className="px-4 py-8 text-center text-muted-foreground">暂无数据</td></tr>
                ) : (
                  users.map((u) => (
                    <tr key={u.user_id} className="border-b border-border">
                      <td className="px-4 py-2.5">{u.phone}</td>
                      <td className="px-4 py-2.5">{u.name || '-'}</td>
                      <td className="px-4 py-2.5">
                        <Badge variant={u.user_type === 'INTERNAL' ? 'primary' : u.user_type === 'EXTERNAL' ? 'warning' : 'default'}>
                          {u.user_type}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge variant={u.status === 'ACTIVE' ? 'success' : 'destructive'}>
                          {u.status === 'ACTIVE' ? '正常' : '已禁用'}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {u.roles?.map((r) => <Badge key={r} variant="outline">{r}</Badge>)}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {(u.permissions || []).length === 0
                            ? <span className="text-xs text-muted-foreground">—</span>
                            : (u.permissions || []).map((p) => {
                              const info = ALL_PERMISSIONS.find((ap) => ap.key === p);
                              return <Badge key={p} variant="primary">{info?.label || p}</Badge>;
                            })
                          }
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <div className="flex gap-1.5">
                          <Button size="xs" variant="outline" onClick={() => setEditModal({
                            user: u,
                            roles: [...u.roles],
                            permissions: [...(u.direct_permissions || [])],
                          })}>
                            编辑权限
                          </Button>
                          {u.status === 'ACTIVE' ? (
                            <Button size="xs" variant="destructive" onClick={() => handleStatusChange(u.user_id, 'DISABLED')}>禁用</Button>
                          ) : (
                            <Button size="xs" onClick={() => handleStatusChange(u.user_id, 'ACTIVE')}>启用</Button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between border-t border-border px-4 py-3">
              <span className="text-xs text-muted-foreground">共 {total} 个用户</span>
              <div className="flex items-center gap-1">
                <Button size="xs" variant="outline" disabled={page <= 1} onClick={() => setPage(page - 1)}>上一页</Button>
                <span className="px-2 text-xs text-muted-foreground">{page} / {totalPages}</span>
                <Button size="xs" variant="outline" disabled={page >= totalPages} onClick={() => setPage(page + 1)}>下一页</Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Edit Modal: 角色 + 权限 */}
      {editModal && (
        <>
          <div className="fixed inset-0 z-50 bg-foreground/30" onClick={() => setEditModal(null)} />
          <div className="fixed inset-x-4 top-1/2 z-50 mx-auto max-w-md -translate-y-1/2 rounded-xl border border-border bg-card p-6 shadow-lg">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold">编辑权限 - {editModal.user.phone}</h3>
              <button onClick={() => setEditModal(null)} className="rounded-md p-1 text-muted-foreground hover:bg-accent">
                <X className="size-4" />
              </button>
            </div>

            {/* 角色选择 */}
            <p className="mb-2 text-xs font-medium text-muted-foreground">角色</p>
            <div className="flex flex-wrap gap-2 mb-4">
              {ALL_ROLES.map((role) => (
                <button
                  key={role}
                  onClick={() => {
                    setEditModal((prev) => {
                      if (!prev) return null;
                      const roles = prev.roles.includes(role)
                        ? prev.roles.filter((r) => r !== role)
                        : [...prev.roles, role];
                      return { ...prev, roles };
                    });
                  }}
                  className={`rounded-full px-2.5 py-0.5 text-xs font-medium border transition-colors ${
                    editModal.roles.includes(role)
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:border-primary/50'
                  }`}
                >
                  {role}
                </button>
              ))}
            </div>

            {/* 直接权限选择 */}
            <p className="mb-2 text-xs font-medium text-muted-foreground">直接权限（叠加在角色之上）</p>
            <div className="flex flex-wrap gap-2 mb-4">
              {ALL_PERMISSIONS.map((perm) => (
                <button
                  key={perm.key}
                  onClick={() => {
                    setEditModal((prev) => {
                      if (!prev) return null;
                      const permissions = prev.permissions.includes(perm.key)
                        ? prev.permissions.filter((p) => p !== perm.key)
                        : [...prev.permissions, perm.key];
                      return { ...prev, permissions };
                    });
                  }}
                  className={`rounded-full px-2.5 py-0.5 text-xs font-medium border transition-colors ${
                    editModal.permissions.includes(perm.key)
                      ? 'border-primary bg-primary/10 text-primary'
                      : 'border-border text-muted-foreground hover:border-primary/50'
                  }`}
                >
                  {perm.label}
                </button>
              ))}
            </div>

            <div className="flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setEditModal(null)}>取消</Button>
              <Button size="sm" onClick={handleSaveEdit}>保存</Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

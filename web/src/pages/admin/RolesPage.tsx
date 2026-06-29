import { useState, useEffect } from 'react';
import { Plus, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { getRoles, createRole, updateRole } from '../../api/admin';
import type { Role } from '../../types';

const ALL_PERMISSIONS = [
  'ai:chat',
  'ai:knowledge',
  'ai:risk',
  'ai:fire_safety',
  'ai:video',
];

export default function RolesPage() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingRole, setEditingRole] = useState<Role | null>(null);
  const [form, setForm] = useState({ role_id: '', name: '', description: '', permissions: [] as string[] });
  const [msg, setMsg] = useState('');

  const showMsg = (text: string) => { setMsg(text); setTimeout(() => setMsg(''), 3000); };

  const fetchRoles = async () => {
    setLoading(true);
    try { const list = await getRoles(); setRoles(list); }
    catch { showMsg('获取角色列表失败'); }
    finally { setLoading(false); }
  };

  useEffect(() => { fetchRoles(); }, []);

  const handleCreate = () => {
    setEditingRole(null);
    setForm({ role_id: '', name: '', description: '', permissions: [] });
    setModalOpen(true);
  };

  const handleEdit = (role: Role) => {
    setEditingRole(role);
    setForm({ role_id: role.role_id, name: role.name, description: role.description, permissions: [...role.permissions] });
    setModalOpen(true);
  };

  const handleSave = async () => {
    if (!form.role_id.trim() || !form.name.trim() || form.permissions.length === 0) {
      showMsg('请填写角色 ID、名称并选择至少一个权限'); return;
    }
    const roleId = editingRole?.role_id;
    try {
      if (roleId) {
        await updateRole(roleId, { name: form.name, description: form.description, permissions: form.permissions });
        showMsg('角色已更新');
      } else {
        await createRole({ role_id: form.role_id, name: form.name, description: form.description, permissions: form.permissions });
        showMsg('角色已创建');
      }
      setEditingRole(null);
      setModalOpen(false);
      fetchRoles();
    } catch { showMsg('保存失败'); }
  };

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-semibold">角色管理</h2>
        <Button size="sm" onClick={handleCreate}>
          <Plus className="size-4" /> 创建角色
        </Button>
      </div>

      {msg && <p className="mb-3 rounded-lg bg-accent px-3 py-1.5 text-sm text-accent-foreground">{msg}</p>}

      <Card>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border text-left text-muted-foreground">
                  <th className="px-4 py-2.5 font-medium">角色 ID</th>
                  <th className="px-4 py-2.5 font-medium">名称</th>
                  <th className="px-4 py-2.5 font-medium">描述</th>
                  <th className="px-4 py-2.5 font-medium">权限</th>
                  <th className="px-4 py-2.5 font-medium">类型</th>
                  <th className="px-4 py-2.5 font-medium">操作</th>
                </tr>
              </thead>
              <tbody>
                {loading ? (
                  <tr><td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">加载中...</td></tr>
                ) : roles.length === 0 ? (
                  <tr><td colSpan={6} className="px-4 py-8 text-center text-muted-foreground">暂无角色</td></tr>
                ) : (
                  roles.map((r) => (
                    <tr key={r.role_id} className="border-b border-border">
                      <td className="px-4 py-2.5 font-mono text-xs">{r.role_id}</td>
                      <td className="px-4 py-2.5">{r.name}</td>
                      <td className="px-4 py-2.5 text-muted-foreground">{r.description}</td>
                      <td className="px-4 py-2.5">
                        <div className="flex flex-wrap gap-1">
                          {r.permissions?.map((p) => <Badge key={p} variant="primary">{p}</Badge>)}
                        </div>
                      </td>
                      <td className="px-4 py-2.5">
                        <Badge variant={r.is_builtin ? 'warning' : 'outline'}>
                          {r.is_builtin ? '内置' : '自定义'}
                        </Badge>
                      </td>
                      <td className="px-4 py-2.5">
                        <Button
                          size="xs"
                          variant="outline"
                          onClick={() => handleEdit(r)}
                          disabled={r.is_builtin}
                        >
                          {r.is_builtin ? '不可编辑' : '编辑'}
                        </Button>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {/* Modal */}
      {modalOpen && (
        <>
          <div className="fixed inset-0 z-50 bg-foreground/30" onClick={() => setModalOpen(false)} />
          <div className="fixed inset-x-4 top-1/2 z-50 mx-auto max-w-lg -translate-y-1/2 rounded-xl border border-border bg-card p-6 shadow-lg">
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-base font-semibold">{editingRole ? '编辑角色' : '创建角色'}</h3>
              <button onClick={() => setModalOpen(false)} className="rounded-md p-1 text-muted-foreground hover:bg-accent">
                <X className="size-4" />
              </button>
            </div>
            <div className="space-y-4">
              <div>
                <label className="mb-1 block text-sm font-medium">角色 ID</label>
                <Input
                  placeholder="如: audit_viewer"
                  value={form.role_id}
                  onChange={(e) => setForm({ ...form, role_id: e.target.value })}
                  disabled={!!editingRole}
                />
                <p className="mt-0.5 text-xs text-muted-foreground">仅限小写字母、数字、下划线，创建后不可修改</p>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium">显示名称</label>
                <Input
                  placeholder="如: 审计查看者"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium">描述</label>
                <Input
                  placeholder="角色描述"
                  value={form.description}
                  onChange={(e) => setForm({ ...form, description: e.target.value })}
                />
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium">权限列表</label>
                <div className="flex flex-wrap gap-2">
                  {ALL_PERMISSIONS.map((perm) => (
                    <button
                      key={perm}
                      onClick={() => {
                        setForm((prev) => ({
                          ...prev,
                          permissions: prev.permissions.includes(perm)
                            ? prev.permissions.filter((p) => p !== perm)
                            : [...prev.permissions, perm],
                        }));
                      }}
                      className={`rounded-full px-2.5 py-0.5 text-xs font-medium border transition-colors ${
                        form.permissions.includes(perm)
                          ? 'border-primary bg-primary/10 text-primary'
                          : 'border-border text-muted-foreground hover:border-primary/50'
                      }`}
                    >
                      {perm}
                    </button>
                  ))}
                </div>
              </div>
            </div>
            <div className="mt-6 flex justify-end gap-2">
              <Button variant="outline" size="sm" onClick={() => setModalOpen(false)}>取消</Button>
              <Button size="sm" onClick={handleSave}>保存</Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

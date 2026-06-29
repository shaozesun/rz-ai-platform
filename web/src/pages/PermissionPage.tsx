import { useState } from 'react';
import { Shield, Key, Check } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { useAuthStore } from '@/stores/authStore';
import { applyRoles } from '@/api/auth';

const AVAILABLE_ROLES = [
  { id: 'power_user', name: '全功能用户', desc: '可使用全部 AI 功能（对话、知识库、隐患识别、消防配置、视频生成）' },
  { id: 'admin', name: '管理员', desc: '拥有全部 AI 功能和系统管理权限' },
];

const AVAILABLE_PERMISSIONS = [
  { id: 'ai:risk', name: '隐患识别', desc: '图片安全隐患检测、消防控制柜状态对比' },
  { id: 'ai:fire_safety', name: '消防配置', desc: '根据建筑参数推荐消防配置方案' },
  { id: 'ai:knowledge', name: '知识库管理', desc: '知识库文档上传、管理、检索' },
  { id: 'ai:video', name: '视频生成', desc: 'PPT 转视频、数字人播报' },
];

const PERM_LABELS: Record<string, string> = {
  'ai:chat': 'AI 对话',
  'ai:knowledge': '知识库',
  'ai:risk': '隐患识别',
  'ai:fire_safety': '消防配置',
  'ai:video': '视频生成',
};

function useApplyForm(type: 'role' | 'perm') {
  const [selected, setSelected] = useState<string[]>([]);
  const [reason, setReason] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [msg, setMsg] = useState('');
  const [applied, setApplied] = useState<string[]>([]);

  const toggle = (id: string) => {
    setSelected((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id],
    );
  };

  const submit = async () => {
    if (selected.length === 0) { setMsg('请先选择'); return; }
    if (!reason.trim()) { setMsg('请填写申请理由'); return; }

    setSubmitting(true);
    try {
      const roles = type === 'role' ? selected : [];
      const perms = type === 'perm' ? selected : [];
      const res = await applyRoles(roles, reason, perms);
      if (res.ok) {
        setApplied((prev) => [...prev, ...selected]);
        setSelected([]);
        setReason('');
        setMsg('申请已提交，等待管理员审批');
      } else {
        setMsg(res.msg || '申请失败');
      }
    } catch {
      setMsg('申请提交失败，请稍后重试');
    } finally {
      setSubmitting(false);
      setTimeout(() => setMsg(''), 4000);
    }
  };

  return { selected, reason, setReason, submitting, msg, applied, toggle, submit };
}

export default function PermissionPage() {
  const { user, permissions } = useAuthStore();
  const rolesForm = useApplyForm('role');
  const permsForm = useApplyForm('perm');

  const currentRoles: string[] = user?.roles || [];

  const availableRoles = AVAILABLE_ROLES.filter(
    (r) => !currentRoles.includes(r.id) && !rolesForm.applied.includes(r.id),
  );
  const availablePerms = AVAILABLE_PERMISSIONS.filter(
    (p) => !permissions.includes(p.id) && !permsForm.applied.includes(p.id),
  );

  return (
    <PlatformShell
      title="权限中心"
      description="查看当前权限，申请更多 AI 能力"
    >
      <div className="space-y-6">
        {/* Current permissions */}
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Key className="size-5 text-primary" />
              <CardTitle className="text-lg">当前权限</CardTitle>
            </div>
            <CardDescription className="text-sm">您已有的角色与功能权限</CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <div>
              <p className="mb-2 text-sm font-medium text-foreground">角色</p>
              <div className="flex flex-wrap gap-2">
                {currentRoles.length > 0 ? (
                  currentRoles.map((r) => (
                    <Badge key={r} variant="primary" className="text-sm px-3 py-1">{r}</Badge>
                  ))
                ) : (
                  <Badge variant="outline" className="text-sm px-3 py-1">user</Badge>
                )}
              </div>
            </div>
            <div>
              <p className="mb-2 text-sm font-medium text-foreground">功能权限</p>
              <div className="flex flex-wrap gap-2">
                {permissions.length > 0 ? (
                  permissions.map((p) => (
                    <Badge key={p} variant="outline" className="text-sm px-3 py-1">
                      {PERM_LABELS[p] || p}
                    </Badge>
                  ))
                ) : (
                  <span className="text-sm text-muted-foreground">暂无特殊权限</span>
                )}
              </div>
            </div>
          </CardContent>
        </Card>

        {/* 申请角色 */}
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Shield className="size-5 text-primary" />
              <CardTitle className="text-lg">申请角色</CardTitle>
            </div>
            <CardDescription className="text-sm">角色包含一组功能权限，获得后即可使用对应功能</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {availableRoles.length === 0 ? (
              <p className="text-sm text-muted-foreground py-3">
                {currentRoles.length >= AVAILABLE_ROLES.length + 1
                  ? '您已拥有所有角色'
                  : '所有角色均已申请，等待审批中'}
              </p>
            ) : (
              <div className="space-y-2">
                {availableRoles.map((role) => (
                  <div
                    key={role.id}
                    className={`rounded-lg border-2 p-4 transition-colors cursor-pointer ${
                      rolesForm.selected.includes(role.id)
                        ? 'border-primary bg-primary/5 shadow-sm'
                        : 'border-border hover:border-primary/40 hover:bg-muted/30'
                    }`}
                    onClick={() => rolesForm.toggle(role.id)}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1">
                        <p className="text-base font-semibold">{role.name}</p>
                        <p className="mt-1 text-sm text-muted-foreground">{role.desc}</p>
                      </div>
                      <div className={`flex size-6 items-center justify-center rounded border-2 transition-colors shrink-0 mt-1 ${
                        rolesForm.selected.includes(role.id)
                          ? 'border-primary bg-primary text-primary-foreground'
                          : 'border-muted-foreground/30'
                      }`}>
                        {rolesForm.selected.includes(role.id) && <Check className="size-3.5" />}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div>
              <label className="mb-2 block text-sm font-medium text-foreground">申请理由</label>
              <Textarea
                placeholder="请说明申请此角色的原因"
                value={rolesForm.reason}
                onChange={(e) => rolesForm.setReason(e.target.value)}
                rows={2}
                className="min-h-[70px]"
              />
            </div>

            {rolesForm.msg && (
              <p className="rounded-lg bg-accent px-4 py-3 text-sm text-accent-foreground">{rolesForm.msg}</p>
            )}

            <Button
              onClick={rolesForm.submit}
              disabled={rolesForm.submitting || rolesForm.selected.length === 0}
              className="w-full"
            >
              {rolesForm.submitting
                ? '提交中...'
                : rolesForm.selected.length > 0
                  ? `提交角色申请 (${rolesForm.selected.length})`
                  : '请先选择要申请的角色'}
            </Button>
          </CardContent>
        </Card>

        {/* 申请功能权限 */}
        <Card>
          <CardHeader>
            <div className="flex items-center gap-2">
              <Shield className="size-5 text-primary" />
              <CardTitle className="text-lg">申请功能权限</CardTitle>
            </div>
            <CardDescription className="text-sm">按需申请单个功能权限，无需获取整个角色</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            {availablePerms.length === 0 ? (
              <p className="text-sm text-muted-foreground py-3">
                所有单独权限已拥有或已申请
              </p>
            ) : (
              <div className="space-y-2">
                {availablePerms.map((perm) => (
                  <div
                    key={perm.id}
                    className={`rounded-lg border-2 p-4 transition-colors cursor-pointer ${
                      permsForm.selected.includes(perm.id)
                        ? 'border-primary bg-primary/5 shadow-sm'
                        : 'border-border hover:border-primary/40 hover:bg-muted/30'
                    }`}
                    onClick={() => permsForm.toggle(perm.id)}
                  >
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex-1">
                        <p className="text-base font-semibold">{perm.name}</p>
                        <p className="mt-1 text-sm text-muted-foreground">{perm.desc}</p>
                      </div>
                      <div className={`flex size-6 items-center justify-center rounded border-2 transition-colors shrink-0 mt-1 ${
                        permsForm.selected.includes(perm.id)
                          ? 'border-primary bg-primary text-primary-foreground'
                          : 'border-muted-foreground/30'
                      }`}>
                        {permsForm.selected.includes(perm.id) && <Check className="size-3.5" />}
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}

            <div>
              <label className="mb-2 block text-sm font-medium text-foreground">申请理由</label>
              <Textarea
                placeholder="请说明需要此功能的原因"
                value={permsForm.reason}
                onChange={(e) => permsForm.setReason(e.target.value)}
                rows={2}
                className="min-h-[70px]"
              />
            </div>

            {permsForm.msg && (
              <p className="rounded-lg bg-accent px-4 py-3 text-sm text-accent-foreground">{permsForm.msg}</p>
            )}

            <Button
              onClick={permsForm.submit}
              disabled={permsForm.submitting || permsForm.selected.length === 0}
              className="w-full"
            >
              {permsForm.submitting
                ? '提交中...'
                : permsForm.selected.length > 0
                  ? `提交权限申请 (${permsForm.selected.length})`
                  : '请先选择要申请的权限'}
            </Button>
          </CardContent>
        </Card>
      </div>
    </PlatformShell>
  );
}
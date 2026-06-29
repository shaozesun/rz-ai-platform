import { useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { LockKeyhole, ArrowRight, Sparkles, Check, Loader2 } from 'lucide-react';
import { useAuthStore } from '../stores/authStore';
import { PlatformShell } from './platform-shell';
import { Button } from '@/components/ui/button';
import { applyRoles } from '@/api/auth';

interface Props {
  children: React.ReactNode;
  requirePerm?: string;
  requireRole?: string;
}

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
};

export function AccessDeniedPage({ reason, missingPerm, missingRole }: {
  reason: string;
  missingPerm?: string;
  missingRole?: string;
}) {
  const permLabel = missingPerm ? PERM_LABELS[missingPerm] || missingPerm : null;
  const roleLabel = missingRole ? ROLE_LABELS[missingRole] || missingRole : null;
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState(false);
  const [error, setError] = useState('');

  const handleApply = async () => {
    setApplying(true);
    setError('');
    try {
      const roles = missingRole ? [missingRole] : [];
      const permissions = missingPerm ? [missingPerm] : [];
      const label = permLabel || roleLabel || '';
      await applyRoles(roles, `申请开通${label}`, permissions);
      setApplied(true);
    } catch {
      setError('提交失败，请稍后重试');
    } finally {
      setApplying(false);
    }
  };

  return (
    <PlatformShell title="">
      <div className="flex flex-1 flex-col items-center justify-center gap-8 py-24">
        {/* 视觉区域 */}
        <div className="relative">
          <div className="flex size-52 items-center justify-center rounded-[2rem] bg-linear-to-br from-amber-50 to-orange-100 ring-1 ring-amber-200/60 shadow-sm">
            <LockKeyhole className="size-20 text-amber-500" strokeWidth={1} />
          </div>
          <div className="absolute -bottom-4 -right-4 flex size-18 items-center justify-center rounded-full bg-background ring-1 ring-border shadow-md">
            <Sparkles className="size-9 text-primary" />
          </div>
        </div>

        {/* 标题与描述 */}
        <div className="text-center">
          <h2 className="text-4xl font-bold tracking-tight text-foreground">
            此功能需要申请开通
          </h2>
          <p className="mt-4 max-w-2xl text-lg leading-relaxed text-muted-foreground">
            {permLabel && (
              <>您当前的账号还未开通 <span className="font-semibold text-foreground">{permLabel}</span> 权限</>
            )}
            {roleLabel && (
              <>此页面需要 <span className="font-semibold text-foreground">{roleLabel}</span> 角色才能访问</>
            )}
            {!permLabel && !roleLabel && reason}
          </p>
        </div>

        {/* CTA */}
        {applied ? (
          <div className="flex flex-col items-center gap-3">
            <div className="flex size-14 items-center justify-center rounded-full bg-green-100">
              <Check className="size-7 text-green-600" />
            </div>
            <p className="text-lg font-semibold text-green-700">已成功申请</p>
            <p className="text-sm text-muted-foreground">请等待管理员审批，通过后刷新页面即可使用</p>
          </div>
        ) : (
          <>
            <Button
              className="gap-3 px-10 py-7 text-lg rounded-xl"
              onClick={handleApply}
              disabled={applying}
            >
              {applying ? (
                <>
                  <Loader2 className="size-6 animate-spin" />
                  正在提交…
                </>
              ) : (
                <>
                  申请开通
                  <ArrowRight className="size-6" />
                </>
              )}
            </Button>

            {error && (
              <p className="text-sm text-destructive">{error}</p>
            )}

            <p className="text-base text-muted-foreground">
              提交申请后需等待管理员审批，通过后即可使用
            </p>
          </>
        )}
      </div>
    </PlatformShell>
  );
}

export default function ProtectedRoute({ children, requirePerm, requireRole }: Props) {
  const { isAuthenticated, initialized, hasPermission, user } = useAuthStore();
  const location = useLocation();

  // 初始化未完成时先不跳转，避免闪现登录页
  if (!initialized) {
    return null;
  }

  if (!isAuthenticated) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }

  if (requirePerm && !hasPermission(requirePerm)) {
    return <AccessDeniedPage
      reason={`此功能需要 "${requirePerm}" 权限。`}
      missingPerm={requirePerm}
    />;
  }

  if (requireRole && !(user?.roles || []).includes(requireRole)) {
    return <AccessDeniedPage
      reason={`此页面仅限 "${requireRole}" 角色访问。`}
      missingRole={requireRole}
    />;
  }

  return <>{children}</>;
}
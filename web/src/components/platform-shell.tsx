import { useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import {
  LayoutDashboard,
  Lightbulb,
  MessageSquareText,
  Clapperboard,
  ShieldAlert,
  Flame,
  BookOpen,
  Users,
  Shield,
  Settings,
  Bell,
  Menu,
  X,
  LogOut,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAuthStore } from '@/stores/authStore';
import { Button } from '@/components/ui/button';

const navGroups = [
  {
    label: '工作台',
    items: [
      { href: '/', label: '总览', icon: LayoutDashboard },
      { href: '/innovation', label: '创新空间', icon: Lightbulb },
    ],
  },
  {
    label: 'AI 能力',
    items: [
      { href: '/assistant', label: '对话助手', icon: MessageSquareText, perm: 'ai:chat' },
      { href: '/video', label: '视频生成', icon: Clapperboard, perm: 'ai:video' },
      { href: '/knowledge', label: '知识库', icon: BookOpen, perm: 'ai:knowledge' },
      { href: '/risk', label: '隐患识别', icon: ShieldAlert, perm: 'ai:risk' },
      { href: '/fire-safety', label: '消防配置', icon: Flame, perm: 'ai:fire_safety' },
    ],
  },
  {
    label: '系统管理',
    items: [
      { href: '/admin', label: '用户与权限', icon: Users, role: 'admin' },
      { href: '/permission', label: '权限中心', icon: Shield },
      { href: '/settings', label: '个人设置', icon: Settings },
    ],
  },
];

export function PlatformShell({
  children,
  title,
  description,
  actions,
  edgeToEdge,
}: {
  children: React.ReactNode;
  title: string;
  description?: string;
  actions?: React.ReactNode;
  edgeToEdge?: boolean;
}) {
  const location = useLocation();
  const user = useAuthStore((s) => s.user);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const [mobileOpen, setMobileOpen] = useState(false);

  const userInitials = user?.name
    ? user.name.slice(0, 2).toUpperCase()
    : user?.phone
      ? user.phone.slice(-2)
      : 'U';

  const filteredGroups = navGroups;

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Sidebar */}
      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-50 flex w-64 flex-col border-r border-sidebar-border bg-sidebar transition-transform lg:static lg:translate-x-0',
          mobileOpen ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex h-16 items-center gap-2.5 border-b border-sidebar-border px-5">
          <img src="/favicon.svg" alt="润泽" className="size-8 rounded-lg" />
          <div className="flex flex-col leading-none">
            <span className="text-sm font-semibold text-sidebar-foreground">润泽智能化平台</span>
            <span className="mt-0.5 text-xs text-muted-foreground">企业 AI 能力中心</span>
          </div>
          <button
            onClick={() => setMobileOpen(false)}
            className="ml-auto rounded-md p-1 text-muted-foreground hover:bg-accent lg:hidden"
            aria-label="关闭菜单"
          >
            <X className="size-4" />
          </button>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 py-4">
          {filteredGroups.map((group) => (
            <div key={group.label} className="mb-6">
              <p className="px-3 pb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground">
                {group.label}
              </p>
              <ul className="flex flex-col gap-0.5">
                {group.items.map((item) => {
                  const active = location.pathname === item.href
                    || (item.href !== '/' && location.pathname.startsWith(item.href));
                  const Icon = item.icon;
                  return (
                    <li key={item.href}>
                      <Link
                        to={item.href}
                        onClick={() => setMobileOpen(false)}
                        className={cn(
                          'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                          active
                            ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                            : 'text-sidebar-foreground/75 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
                        )}
                      >
                        <Icon className="size-4.5 shrink-0" />
                        <span className="flex-1">{item.label}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t border-sidebar-border p-3">
          <div className="flex items-center gap-3 rounded-lg px-2 py-2">
            <div className="flex size-9 items-center justify-center overflow-hidden rounded-full bg-accent text-sm font-semibold text-accent-foreground">
              {user?.avatar ? (
                <img src={user.avatar} alt="" className="size-full object-cover" />
              ) : (
                userInitials
              )}
            </div>
            <div className="flex-1 leading-tight">
              <p className="text-sm font-medium text-sidebar-foreground">
                {user?.name || user?.phone || '用户'}
              </p>
              <p className="text-xs text-muted-foreground">
                {user?.company || '企业版'}
              </p>
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={clearAuth}
              title="退出登录"
            >
              <LogOut className="size-4" />
            </Button>
          </div>
        </div>
      </aside>

      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-foreground/30 lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-30 flex h-16 items-center gap-4 border-b border-border bg-background/80 px-4 backdrop-blur-md lg:px-8">
          <button
            onClick={() => setMobileOpen(true)}
            className="rounded-md p-1.5 text-muted-foreground hover:bg-accent lg:hidden"
            aria-label="打开菜单"
          >
            <Menu className="size-5" />
          </button>

          <div className="ml-auto flex items-center gap-2">
            <button
              className="relative rounded-lg p-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              aria-label="通知"
            >
              <Bell className="size-5" />
            </button>
            <div className="flex size-9 items-center justify-center overflow-hidden rounded-full bg-accent text-sm font-semibold text-accent-foreground lg:hidden">
              {user?.avatar ? (
                <img src={user.avatar} alt="" className="size-full object-cover" />
              ) : (
                userInitials
              )}
            </div>
          </div>
        </header>

        <main
          className={cn(
            'flex flex-1 flex-col overflow-y-auto [touch-action:pan-y]',
            edgeToEdge ? 'px-0 lg:px-8 py-0 lg:py-8' : 'px-4 py-6 lg:px-8 lg:py-8',
          )}
        >
          <div
            className={cn(
              'flex w-full flex-1 flex-col',
              !edgeToEdge && 'mx-auto max-w-7xl',
            )}
          >
            <div
              className={cn(
                'flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between',
                edgeToEdge ? 'mb-2 px-4 pt-4 lg:px-0 lg:pt-0' : 'mb-6',
              )}
            >
              <div>
                <h1 className="text-2xl font-semibold tracking-tight text-balance">{title}</h1>
                {description && (
                  <p className="mt-1 text-sm text-muted-foreground text-pretty">{description}</p>
                )}
              </div>
              {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
            </div>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

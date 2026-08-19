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
  PanelLeftClose,
  PanelLeftOpen,
  X,
  LogOut,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAuthStore } from '@/stores/authStore';
import { Button } from '@/components/ui/button';

const SIDEBAR_COLLAPSED_KEY = 'sidebar-collapsed';

interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
  perm?: string;
  role?: string;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const navGroups: NavGroup[] = [
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
  headerActions,
  headerActionsLeft,
  edgeToEdge,
}: {
  children: React.ReactNode;
  title?: string;
  description?: string;
  actions?: React.ReactNode;
  headerActions?: React.ReactNode;
  headerActionsLeft?: React.ReactNode;
  edgeToEdge?: boolean;
}) {
  const location = useLocation();
  const user = useAuthStore((s) => s.user);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true',
  );

  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
  };

  const hasPermission = useAuthStore((s) => s.hasPermission);

  const userInitials = user?.name
    ? user.name.slice(0, 2).toUpperCase()
    : user?.phone
      ? user.phone.slice(-2)
      : 'U';

  const filteredGroups = navGroups
    .map(group => ({
      ...group,
      items: group.items.filter(item => {
        if (item.role) return (user?.roles || []).includes(item.role);
        if (item.perm) return hasPermission(item.perm);
        return true;
      }),
    }))
    .filter(group => group.items.length > 0);

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Sidebar */}
      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-50 flex w-64 flex-col overflow-hidden border-r border-sidebar-border bg-sidebar transition-all lg:static lg:translate-x-0',
          mobileOpen ? 'translate-x-0' : '-translate-x-full',
          collapsed && 'lg:w-16',
        )}
      >
        <div className={cn('flex h-14 items-center gap-2.5 border-b border-sidebar-border px-5', collapsed && 'lg:justify-center lg:px-0')}>
          <img src="/logo.jpg" alt="润泽科技" className="size-8 shrink-0 rounded-lg" />
          <div className={cn('flex flex-col leading-none', collapsed && 'lg:hidden')}>
            <span className="whitespace-nowrap text-sm font-semibold text-sidebar-foreground">智能运维平台</span>
            <span className="mt-0.5 whitespace-nowrap text-xs text-muted-foreground">润泽 AI 能力中心</span>
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
            <div key={group.label} className={cn('mb-6', collapsed && 'lg:mb-2')}>
              <p className={cn('whitespace-nowrap px-3 pb-2 text-xs font-medium uppercase tracking-wider text-muted-foreground', collapsed && 'lg:hidden')}>
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
                        title={collapsed ? item.label : undefined}
                        className={cn(
                          'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                          collapsed && 'lg:justify-center lg:px-0',
                          active
                            ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                            : 'text-sidebar-foreground/75 hover:bg-sidebar-accent/60 hover:text-sidebar-foreground',
                        )}
                      >
                        <Icon className="size-4.5 shrink-0" />
                        <span className={cn('flex-1 whitespace-nowrap', collapsed && 'lg:hidden')}>{item.label}</span>
                      </Link>
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
        </nav>

        <div className="border-t border-sidebar-border p-3">
          <div className={cn('flex items-center gap-3 rounded-lg px-2 py-2', collapsed && 'lg:justify-center lg:px-0')}>
            <div className="flex size-9 items-center justify-center overflow-hidden rounded-full bg-accent text-sm font-semibold text-accent-foreground">
              {user?.avatar ? (
                <img src={user.avatar} alt="" className="size-full object-cover" />
              ) : (
                userInitials
              )}
            </div>
            <div className={cn('flex-1 leading-tight', collapsed && 'lg:hidden')}>
              <p className="whitespace-nowrap text-sm font-medium text-sidebar-foreground">
                {user?.name || user?.phone || '用户'}
              </p>
              <p className="whitespace-nowrap text-xs text-muted-foreground">
                {user?.company || '企业版'}
              </p>
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={clearAuth}
              title="退出登录"
              className={collapsed ? 'lg:hidden' : undefined}
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
        <header className="sticky top-0 z-30 flex h-14 items-center gap-4 border-b border-border bg-background/80 px-4 backdrop-blur-md lg:px-8">
          <button
            onClick={() => setMobileOpen(true)}
            className="rounded-md p-1.5 text-muted-foreground hover:bg-accent lg:hidden"
            aria-label="打开菜单"
          >
            <Menu className="size-5" />
          </button>
          <button
            onClick={toggleCollapsed}
            className="hidden rounded-md p-1.5 text-muted-foreground hover:bg-accent lg:block"
            aria-label={collapsed ? '展开侧边栏' : '收起侧边栏'}
          >
            {collapsed ? <PanelLeftOpen className="size-5" /> : <PanelLeftClose className="size-5" />}
          </button>

          {headerActionsLeft}

          <div className="ml-auto flex items-center gap-2">
            {headerActions}
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
            edgeToEdge ? 'px-0 lg:px-8 py-0 lg:py-2' : 'px-4 py-6 lg:px-8 lg:py-8',
          )}
        >
          <div
            className={cn(
              'flex w-full min-h-0 flex-1 flex-col',
              !edgeToEdge && 'mx-auto max-w-7xl',
            )}
          >
            {Boolean(title || actions) && (
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
            )}
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

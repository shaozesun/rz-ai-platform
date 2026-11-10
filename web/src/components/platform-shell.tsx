import { useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import {
  LayoutDashboard,
  Lightbulb,
  MessageSquareText,
  FileText,
  Clapperboard,
  ShieldAlert,
  Flame,
  BookOpen,
  Clock,
  ShieldCheck,
  Users,
  User,
  Shield,
  KeyRound,
  Settings,
  Bell,
  Menu,
  PanelLeftClose,
  PanelLeftOpen,
  X,
  LogOut,
  Bot,
  Wrench,
  type LucideIcon,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import { useAuthStore } from '@/stores/authStore';
import { useNotificationStore, type AppNotification } from '@/stores/notificationStore';
import { useChatStore } from '@/stores/chatStore';
import { listNotifications } from '@/api/scheduledTasks';
import { pushInAppNotification } from '@/lib/notifications';
import { Button } from '@/components/ui/button';

const SIDEBAR_COLLAPSED_KEY = 'sidebar-collapsed';

/** 相对时间：刚刚 / X分钟前 / X小时前 / 日期。 */
function formatRelativeTime(ts: number): string {
  const diff = Date.now() - ts;
  const min = Math.floor(diff / 60000);
  if (min < 1) return '刚刚';
  if (min < 60) return `${min} 分钟前`;
  const hour = Math.floor(min / 60);
  if (hour < 24) return `${hour} 小时前`;
  return new Date(ts).toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' });
}

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
    label: '智能体',
    items: [
      { href: '/agent-skills', label: '技能中心', icon: Bot, perm: 'ai:agent' },
      { href: '/agent-tools', label: '工具中心', icon: Wrench, perm: 'ai:agent' },
    ],
  },
  {
    label: 'AI 能力',
    items: [
      { href: '/assistant', label: '对话助手', icon: MessageSquareText, perm: 'ai:chat' },
      // 定时任务/分析报告：后端 API 门控是 scheduled_task:manage，前端按同一口径（此前用 ai:agent，会出现"能进页面但接口 403"）
      { href: '/analysis-reports', label: '分析报告', icon: FileText, perm: 'ai:agent' },
      { href: '/compliance', label: '合规检查', icon: ShieldCheck, perm: 'ai:agent' },
      { href: '/scheduled-tasks', label: '定时任务', icon: Clock, perm: 'scheduled_task:manage' },
      // 名字保持「视频生成」（表单页：批量提交/看历史）；输入栏那个同名开关是"本轮对话里做视频"
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
      { href: '/auth-center', label: '认证中心', icon: KeyRound },
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
  bleed,
}: {
  children: React.ReactNode;
  title?: string;
  description?: string;
  actions?: React.ReactNode;
  headerActions?: React.ReactNode;
  headerActionsLeft?: React.ReactNode;
  edgeToEdge?: boolean;
  /** 内容**四边都不留白**（比 edgeToEdge 更彻底：连 lg 断点的 px-8/py-2 也去掉、不限最大宽度）。
   *  用于"整屏查看器"式的页面（如报告 PDF 预览）——edgeToEdge 在 lg 上仍留 32px。 */
  bleed?: boolean;
}) {
  const location = useLocation();
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [collapsed, setCollapsed] = useState(
    () => localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === 'true',
  );
  const [notifOpen, setNotifOpen] = useState(false);
  const notifRef = useRef<HTMLDivElement>(null);
  const notifItems = useNotificationStore((s) => s.items);
  const notifUnread = useNotificationStore((s) => s.unread);
  const notifMarkAllRead = useNotificationStore((s) => s.markAllRead);
  const notifClear = useNotificationStore((s) => s.clear);
  const jumpToAnswer = useChatStore((s) => s.jumpToAnswer);

  // 点击通知：带通用跳转路径时直接跳转；否则带会话目标时切到「对话助手」页并定位对应回答；无目标仅收起
  const handleOpenNotification = (n: AppNotification) => {
    setNotifOpen(false);
    if (n.href) {
      navigate(n.href);
      return;
    }
    if (!n.sessionId) return;
    if (location.pathname !== '/assistant') navigate('/assistant');
    void jumpToAnswer(n.sessionId, n.messageId);
  };

  // 定时任务完成通知轮询：拉后端通知推铃铛，localStorage 去重（首次仅播种，避免刷屏历史）
  useEffect(() => {
    const SEEN_KEY = 'rz_sched_notif_seen';
    const readSeen = (): string[] => {
      try {
        const raw = localStorage.getItem(SEEN_KEY);
        return raw ? (JSON.parse(raw) as string[]) : [];
      } catch {
        return [];
      }
    };
    const writeSeen = (ids: string[]) => {
      localStorage.setItem(SEEN_KEY, JSON.stringify(ids.slice(-200)));
    };

    let cancelled = false;
    const poll = async () => {
      try {
        const items = await listNotifications();
        if (cancelled) return;
        if (localStorage.getItem(SEEN_KEY) === null) {
          writeSeen(items.map((n) => n.notification_id));
          return;
        }
        const seen = new Set(readSeen());
        const fresh = items.filter((n) => !seen.has(n.notification_id));
        if (fresh.length > 0) {
          fresh.forEach((n) => pushInAppNotification(n.title, n.body, { href: '/scheduled-tasks' }));
          writeSeen([...readSeen(), ...fresh.map((n) => n.notification_id)]);
        }
      } catch {
        // 无权限/失败静默
      }
    };

    void poll();
    const timer = setInterval(() => void poll(), 30_000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, []);

  // 面板打开即视为已读
  useEffect(() => {
    if (notifOpen) notifMarkAllRead();
  }, [notifOpen, notifMarkAllRead]);

  // 点击面板外部关闭（header 有 backdrop-blur 会限制 fixed 定位，改用文档级监听）
  useEffect(() => {
    if (!notifOpen) return;
    const onDocClick = (e: MouseEvent) => {
      if (notifRef.current && !notifRef.current.contains(e.target as Node)) {
        setNotifOpen(false);
      }
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [notifOpen]);

  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem(SIDEBAR_COLLAPSED_KEY, String(next));
  };

  const hasPermission = useAuthStore((s) => s.hasPermission);

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
          <img src="/logo.png" alt="润泽科技" className="size-10 shrink-0 rounded-lg" />
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
                <User className="size-5" />
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
              onClick={() => {
                if (window.confirm('确定退出登录吗？')) {
                  clearAuth();
                }
              }}
              data-tip="退出登录"
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
            <div className="relative" ref={notifRef}>
              <button
                onClick={() => setNotifOpen((v) => !v)}
                className="relative rounded-lg p-2 text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                aria-label="通知"
              >
                <Bell className="size-5" />
                {notifUnread > 0 && (
                  <span className="absolute right-0.5 top-0.5 flex size-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-0.5 text-[10px] font-medium leading-none text-white">
                    {notifUnread > 99 ? '99+' : notifUnread}
                  </span>
                )}
              </button>
              {notifOpen && (
                  <div className="absolute right-0 top-10 z-50 w-80 overflow-hidden rounded-lg border border-border bg-background shadow-lg">
                    <div className="flex items-center justify-between border-b border-border px-3 py-2">
                      <span className="text-sm font-medium">通知</span>
                      <div className="flex items-center gap-2 text-xs text-muted-foreground">
                        {notifItems.length > 0 && (
                          <button
                            className="hover:text-foreground"
                            onClick={() => notifClear()}
                          >
                            清空
                          </button>
                        )}
                        <button className="hover:text-foreground" onClick={() => setNotifOpen(false)}>
                          关闭
                        </button>
                      </div>
                    </div>
                    <div className="max-h-96 overflow-y-auto">
                      {notifItems.length === 0 ? (
                        <p className="px-3 py-8 text-center text-xs text-muted-foreground">暂无通知</p>
                      ) : (
                        notifItems.map((n) => (
                          <button
                            key={n.id}
                            type="button"
                            onClick={() => handleOpenNotification(n)}
                            className="block w-full border-b border-border/50 px-3 py-2 text-left transition-colors last:border-b-0 hover:bg-muted/40"
                          >
                            <span className="flex items-center justify-between gap-2">
                              <span className="text-sm font-medium text-foreground">{n.title}</span>
                              <span className="shrink-0 text-xs text-muted-foreground">
                                {formatRelativeTime(n.createdAt)}
                              </span>
                            </span>
                            {n.body && (
                              <span className="mt-0.5 block text-xs text-muted-foreground">{n.body}</span>
                            )}
                          </button>
                        ))
                      )}
                    </div>
                  </div>
              )}
            </div>
            <div className="flex size-9 items-center justify-center overflow-hidden rounded-full bg-accent text-sm font-semibold text-accent-foreground lg:hidden">
              {user?.avatar ? (
                <img src={user.avatar} alt="" className="size-full object-cover" />
              ) : (
                <User className="size-5" />
              )}
            </div>
          </div>
        </header>

        <main
          className={cn(
            'flex flex-1 flex-col overflow-y-auto [touch-action:pan-y]',
            bleed ? 'p-0' : edgeToEdge ? 'px-0 lg:px-8 py-0 lg:py-2' : 'px-4 py-6 lg:px-8 lg:py-8',
          )}
        >
          <div
            className={cn(
              'flex w-full min-h-0 flex-1 flex-col',
              !edgeToEdge && !bleed && 'mx-auto max-w-7xl',
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

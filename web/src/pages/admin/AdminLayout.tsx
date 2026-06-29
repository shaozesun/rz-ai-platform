import { useState, useEffect } from 'react';
import { useNavigate, useLocation, Outlet, Link } from 'react-router-dom';
import { Users, Shield, CheckCircle, FileText, ArrowLeft, Menu, X } from 'lucide-react';
import { cn } from '@/lib/utils';

const menuItems = [
  { key: '/admin/users', icon: Users, label: '用户管理' },
  { key: '/admin/roles', icon: Shield, label: '角色管理' },
  { key: '/admin/approvals', icon: CheckCircle, label: '审批管理' },
  { key: '/admin/audit-logs', icon: FileText, label: '审计日志' },
];

export default function AdminLayout() {
  const navigate = useNavigate();
  const location = useLocation();
  const [mobileMenuOpen, setMobileMenuOpen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);

  useEffect(() => {
    const check = () => setIsMobile(window.innerWidth < 768);
    check();
    window.addEventListener('resize', check);
    return () => window.removeEventListener('resize', check);
  }, []);

  const selectedKey = menuItems.find((item) =>
    location.pathname.startsWith(item.key),
  )?.key || '/admin/users';

  const navLinks = (
    <nav className="flex flex-col gap-0.5 px-2 py-3">
      {menuItems.map((item) => {
        const Icon = item.icon;
        const active = selectedKey === item.key;
        return (
          <Link
            key={item.key}
            to={item.key}
            onClick={() => isMobile && setMobileMenuOpen(false)}
            className={cn(
              'flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
              active
                ? 'bg-accent text-accent-foreground'
                : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
            )}
          >
            <Icon className="size-4" />
            {item.label}
          </Link>
        );
      })}
    </nav>
  );

  return (
    <div className="flex h-screen bg-background">
      {/* Desktop sidebar */}
      {!isMobile && (
        <aside className="flex w-[200px] shrink-0 flex-col border-r border-border bg-card">
          <div className="flex h-12 items-center border-b border-border px-4">
            <span className="text-sm font-semibold">管理平台</span>
          </div>
          {navLinks}
          <div className="mt-auto border-t border-border p-3">
            <button
              onClick={() => navigate('/')}
              className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
            >
              <ArrowLeft className="size-4" />
              返回主页
            </button>
          </div>
        </aside>
      )}

      {/* Mobile */}
      {isMobile && (
        <>
          <header className="fixed inset-x-0 top-0 z-30 flex h-12 items-center gap-3 border-b border-border bg-card px-4">
            <button
              onClick={() => setMobileMenuOpen(true)}
              className="rounded-md p-1 text-muted-foreground hover:bg-accent"
            >
              <Menu className="size-5" />
            </button>
            <span className="text-sm font-semibold">管理平台</span>
          </header>
          {mobileMenuOpen && (
            <div className="fixed inset-0 z-40 bg-foreground/30" onClick={() => setMobileMenuOpen(false)} />
          )}
          <aside
            className={cn(
              'fixed inset-y-0 left-0 z-50 w-[200px] border-r border-border bg-card transition-transform',
              mobileMenuOpen ? 'translate-x-0' : '-translate-x-full',
            )}
          >
            <div className="flex h-12 items-center justify-between border-b border-border px-4">
              <span className="text-sm font-semibold">管理平台</span>
              <button onClick={() => setMobileMenuOpen(false)} className="rounded-md p-1 text-muted-foreground hover:bg-accent">
                <X className="size-4" />
              </button>
            </div>
            {navLinks}
            <div className="mt-auto border-t border-border p-3">
              <button
                onClick={() => { navigate('/'); setMobileMenuOpen(false); }}
                className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-foreground transition-colors"
              >
                <ArrowLeft className="size-4" />
                返回主页
              </button>
            </div>
          </aside>
        </>
      )}

      <main className="flex-1 overflow-auto p-4 lg:p-6 pt-12 md:pt-4 lg:pt-6">
        <Outlet />
      </main>
    </div>
  );
}

import { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Lock, Phone, UserPlus, ArrowLeft } from 'lucide-react';
import { useAuthStore } from '../stores/authStore';
import { login, resetPassword } from '../api/auth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent } from '@/components/ui/card';

export default function LoginPage() {
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [activeTab, setActiveTab] = useState<'login' | 'register'>('login');
  const [logging, setLogging] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  // 忘记密码
  const [forgotPw, setForgotPw] = useState(false);

  const navigate = useNavigate();
  const location = useLocation();
  const { isAuthenticated, setAuth } = useAuthStore();

  const from = (location.state as { from?: { pathname: string } })?.from?.pathname || '/';

  useEffect(() => {
    if (isAuthenticated) {
      navigate(from, { replace: true });
    }
  }, [isAuthenticated, navigate, from]);

  const PHONE_RE = /^1[3-9]\d{9}$/;

  const extractError = (err: unknown, fallback: string): string => {
    const data = (err as { response?: { data?: { detail?: unknown; msg?: string } } })?.response?.data;
    if (!data) return fallback;
    if (data.msg) return data.msg;
    if (Array.isArray(data.detail)) {
      const msgs = data.detail
        .map((d: { msg?: string }) => d.msg || '')
        .filter(Boolean);
      return msgs.length > 0 ? msgs.join('；') : fallback;
    }
    if (typeof data.detail === 'string') return data.detail;
    return fallback;
  };

  const doAuth = async () => {
    setErrorMsg('');
    if (!phone.trim()) { setErrorMsg('请输入手机号'); return; }
    if (!PHONE_RE.test(phone)) { setErrorMsg('请输入正确的 11 位手机号'); return; }
    if (!password) { setErrorMsg('请输入密码'); return; }
    if (activeTab === 'register' && password !== confirmPassword) {
      setErrorMsg('两次密码不一致'); return;
    }
    setLogging(true);
    try {
      const res = await login(phone, password);
      if (res.ok) {
        const d = res.data;
        setAuth(d.access_token, d.refresh_token, d.user, d.user.permissions);
      } else {
        setErrorMsg(res.msg || '登录失败');
      }
    } catch (err: unknown) {
      setErrorMsg(extractError(err, '登录失败，请检查手机号或密码'));
    } finally {
      setLogging(false);
    }
  };

  const handleResetPw = async () => {
    setErrorMsg('');
    if (!phone.trim()) { setErrorMsg('请输入手机号'); return; }
    if (!PHONE_RE.test(phone)) { setErrorMsg('请输入正确的 11 位手机号'); return; }
    if (!newPassword) { setErrorMsg('请输入新密码'); return; }
    if (newPassword !== confirmPassword) { setErrorMsg('两次密码不一致'); return; }
    if (newPassword.length < 6) { setErrorMsg('新密码至少 6 位'); return; }
    setLogging(true);
    try {
      const res = await resetPassword(phone, newPassword);
      if (res.ok) {
        setSuccessMsg('密码已重置，请登录');
        setForgotPw(false);
        setActiveTab('login');
        setPassword('');
        setNewPassword('');
        setConfirmPassword('');
        setTimeout(() => setSuccessMsg(''), 3000);
      }
    } catch (err: unknown) {
      setErrorMsg(extractError(err, '重置失败，请确认手机号已注册'));
    } finally {
      setLogging(false);
    }
  };

  const exitForgot = () => {
    setForgotPw(false);
    setErrorMsg('');
    setSuccessMsg('');
    setNewPassword('');
    setConfirmPassword('');
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-linear-to-br from-[#667eea] to-[#764ba2] p-4">
      <Card className="w-full max-w-[400px] shadow-2xl">
        <CardContent className="p-10">
          <div className="mb-6 text-center">
            <h1 className="text-2xl font-bold text-foreground">智能运维平台</h1>
          </div>

          {/* ===== 忘记密码 ===== */}
          {forgotPw ? (
            <div className="flex flex-col gap-4">
              <button
                onClick={exitForgot}
                className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
              >
                <ArrowLeft className="size-3.5" /> 返回登录
              </button>

              <h2 className="text-base font-semibold text-center">重置密码</h2>

              <div className="relative">
                <Phone className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="pl-10"
                  placeholder="请输入手机号"
                  value={phone}
                  onChange={(e) => setPhone(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleResetPw()}
                />
              </div>
              <div className="relative">
                <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="pl-10"
                  type="password"
                  placeholder="新密码（至少 6 位）"
                  value={newPassword}
                  onChange={(e) => setNewPassword(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleResetPw()}
                />
              </div>
              <div className="relative">
                <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="pl-10"
                  type="password"
                  placeholder="再次输入新密码"
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleResetPw()}
                />
              </div>

              {errorMsg && (
                <p className="rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">{errorMsg}</p>
              )}
              {successMsg && (
                <p className="rounded-lg bg-success/10 px-3 py-2 text-sm text-success">{successMsg}</p>
              )}

              <Button
                className="w-full"
                size="lg"
                onClick={handleResetPw}
                disabled={logging}
              >
                重置密码
              </Button>
            </div>
          ) : (
            <>
              {/* Tabs */}
              <div className="mb-6 flex rounded-lg bg-muted p-0.5">
                <button
                  className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                    activeTab === 'login'
                      ? 'bg-card text-foreground shadow-xs'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                  onClick={() => { setActiveTab('login'); setErrorMsg(''); setPassword(''); setConfirmPassword(''); }}
                >
                  登录
                </button>
                <button
                  className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                    activeTab === 'register'
                      ? 'bg-card text-foreground shadow-xs'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                  onClick={() => { setActiveTab('register'); setErrorMsg(''); setPassword(''); setConfirmPassword(''); }}
                >
                  注册
                </button>
              </div>

              <div className="flex flex-col gap-4">
                <div className="relative">
                  <Phone className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    className="pl-10"
                    placeholder="请输入手机号"
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && doAuth()}
                  />
                </div>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <Input
                    className="pl-10"
                    type="password"
                    placeholder="请输入密码"
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && doAuth()}
                  />
                </div>
                {activeTab === 'register' && (
                  <div className="relative">
                    <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                    <Input
                      className="pl-10"
                      type="password"
                      placeholder="请再次输入密码"
                      value={confirmPassword}
                      onChange={(e) => setConfirmPassword(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && doAuth()}
                    />
                  </div>
                )}

                {activeTab === 'login' && (
                  <button
                    onClick={() => { setForgotPw(true); setErrorMsg(''); setSuccessMsg(''); }}
                    className="self-end -mt-2 text-xs text-muted-foreground hover:text-primary"
                  >
                    忘记密码？
                  </button>
                )}

                {errorMsg && (
                  <p className="rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">{errorMsg}</p>
                )}
                {successMsg && (
                  <p className="rounded-lg bg-success/10 px-3 py-2 text-sm text-success">{successMsg}</p>
                )}

                <Button
                  className="w-full"
                  size="lg"
                  onClick={doAuth}
                  disabled={logging}
                >
                  {activeTab === 'register' ? <UserPlus className="size-4" /> : <Lock className="size-4" />}
                  {activeTab === 'register' ? '注册' : '登录'}
                </Button>

                {activeTab === 'register' && (
                  <p className="text-center text-xs text-muted-foreground">
                    注册后默认开通知识库问答权限，其他功能需申请
                  </p>
                )}
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

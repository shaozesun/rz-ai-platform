import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Lock, Phone, UserPlus, ArrowLeft } from 'lucide-react';
import { useAuthStore } from '../stores/authStore';
import { login, register, resetPassword, fetchCaptcha } from '../api/auth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent } from '@/components/ui/card';

// 验证码方案切换: 'pillow' | 'turnstile'
const CAPTCHA_PROVIDER: string = 'pillow';
const TURNSTILE_SITE_KEY = '0x4AAAAAAEIxMciQ03FoqMNu';

const PASSWORD_RE = /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?~`]).{8,}$/;
const PASSWORD_MSG = '密码至少8位，必须包含大写字母、小写字母、数字和特殊符号';

declare global {
  interface Window {
    turnstile: {
      render: (selector: string, options: Record<string, unknown>) => string;
      reset: (widgetId: string) => void;
      remove: (widgetId: string) => void;
    };
  }
}

export default function LoginPage() {
  const [phone, setPhone] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [activeTab, setActiveTab] = useState<'login' | 'register'>('login');
  const [logging, setLogging] = useState(false);
  const [errorMsg, setErrorMsg] = useState('');
  const [successMsg, setSuccessMsg] = useState('');

  // Pillow 验证码
  const [captchaId, setCaptchaId] = useState('');
  const [captchaImg, setCaptchaImg] = useState('');
  const [captchaCode, setCaptchaCode] = useState('');

  // Turnstile
  const [turnstileToken, setTurnstileToken] = useState('');
  const turnstileWidgetId = useRef('');

  const [forgotPw, setForgotPw] = useState(false);

  const loadCaptcha = useCallback(async () => {
    if (CAPTCHA_PROVIDER !== 'pillow') return;
    try {
      const c = await fetchCaptcha();
      setCaptchaId(c.captcha_id);
      setCaptchaImg(c.image_base64);
    } catch {
      // ignore
    }
  }, []);

  const resetTurnstile = useCallback(() => {
    setTurnstileToken('');
    if (turnstileWidgetId.current && window.turnstile) {
      window.turnstile.reset(turnstileWidgetId.current);
    }
  }, []);

  const refreshCaptcha = useCallback(() => {
    setCaptchaCode('');
    if (CAPTCHA_PROVIDER === 'pillow') loadCaptcha();
    else resetTurnstile();
  }, [loadCaptcha, resetTurnstile]);

  // Load Turnstile script
  useEffect(() => {
    if (CAPTCHA_PROVIDER !== 'turnstile') return;
    if (document.getElementById('turnstile-script')) return;
    const script = document.createElement('script');
    script.id = 'turnstile-script';
    script.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
    script.async = true;
    script.defer = true;
    document.body.appendChild(script);
  }, []);

  const mountTurnstile = useCallback(() => {
    if (CAPTCHA_PROVIDER !== 'turnstile') return;
    const container = document.getElementById('turnstile-widget');
    if (!container || !window.turnstile) return;
    if (turnstileWidgetId.current) {
      window.turnstile.remove(turnstileWidgetId.current);
    }
    container.innerHTML = '';
    turnstileWidgetId.current = window.turnstile.render('#turnstile-widget', {
      sitekey: TURNSTILE_SITE_KEY,
      callback: (token: string) => setTurnstileToken(token),
      'expired-callback': resetTurnstile,
      'error-callback': resetTurnstile,
    });
  }, [resetTurnstile]);

  // Mount Turnstile or load Pillow captcha
  useEffect(() => {
    if (CAPTCHA_PROVIDER === 'turnstile') {
      if (window.turnstile) {
        mountTurnstile();
      } else {
        const check = setInterval(() => {
          if (window.turnstile) { mountTurnstile(); clearInterval(check); }
        }, 200);
        return () => clearInterval(check);
      }
    } else {
      loadCaptcha();
    }
  }, [mountTurnstile, loadCaptcha, activeTab, forgotPw]);

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

  const validateCaptcha = (): boolean => {
    if (CAPTCHA_PROVIDER === 'turnstile') {
      if (!turnstileToken) { setErrorMsg('请完成安全验证'); return false; }
    } else {
      if (!captchaCode.trim()) { setErrorMsg('请输入验证码'); return false; }
    }
    return true;
  };

  const doAuth = async () => {
    setErrorMsg('');
    if (!phone.trim()) { setErrorMsg('请输入手机号'); return; }
    if (!PHONE_RE.test(phone)) { setErrorMsg('请输入正确的 11 位手机号'); return; }
    if (!password) { setErrorMsg('请输入密码'); return; }
    if (!validateCaptcha()) return;

    if (activeTab === 'register') {
      if (!PASSWORD_RE.test(password)) { setErrorMsg(PASSWORD_MSG); return; }
      if (password !== confirmPassword) { setErrorMsg('两次密码不一致'); return; }
    }

    setLogging(true);
    try {
      const res = activeTab === 'login'
        ? await login(phone, password, captchaId, captchaCode, turnstileToken)
        : await register(phone, password, captchaId, captchaCode, turnstileToken);
      if (res.ok) {
        const d = res.data;
        setAuth(d.access_token, d.refresh_token, d.user, d.user.permissions);
      } else {
        setErrorMsg(res.msg || (activeTab === 'login' ? '登录失败' : '注册失败'));
      }
    } catch (err: unknown) {
      setErrorMsg(extractError(err, activeTab === 'login' ? '登录失败，请检查手机号或密码' : '注册失败'));
      refreshCaptcha();
    } finally {
      setLogging(false);
    }
  };

  const handleResetPw = async () => {
    setErrorMsg('');
    if (!phone.trim()) { setErrorMsg('请输入手机号'); return; }
    if (!PHONE_RE.test(phone)) { setErrorMsg('请输入正确的 11 位手机号'); return; }
    if (!newPassword) { setErrorMsg('请输入新密码'); return; }
    if (!PASSWORD_RE.test(newPassword)) { setErrorMsg(PASSWORD_MSG); return; }
    if (newPassword !== confirmPassword) { setErrorMsg('两次密码不一致'); return; }
    if (!validateCaptcha()) return;
    setLogging(true);
    try {
      const res = await resetPassword(phone, newPassword, captchaId, captchaCode, turnstileToken);
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
      refreshCaptcha();
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

  // 验证码 UI 组件
  const captchaSection = CAPTCHA_PROVIDER === 'turnstile' ? (
    <div id="turnstile-widget" className="flex justify-center" />
  ) : (
    <div className="flex gap-2">
      {captchaImg ? (
        <img
          src={`data:image/png;base64,${captchaImg}`}
          alt="验证码"
          className="h-10 w-[120px] cursor-pointer rounded border border-border object-contain bg-white shrink-0"
          onClick={loadCaptcha}
          title="点击刷新验证码"
        />
      ) : (
        <div className="h-10 w-[120px] rounded border border-border bg-muted shrink-0 animate-pulse" />
      )}
      <Input
        placeholder="验证码"
        value={captchaCode}
        onChange={(e) => setCaptchaCode(e.target.value)}
        className="flex-1"
      />
    </div>
  );

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

              {captchaSection}

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
                  onClick={() => { setActiveTab('login'); setErrorMsg(''); setPassword(''); setConfirmPassword(''); refreshCaptcha(); }}
                >
                  登录
                </button>
                <button
                  className={`flex-1 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                    activeTab === 'register'
                      ? 'bg-card text-foreground shadow-xs'
                      : 'text-muted-foreground hover:text-foreground'
                  }`}
                  onClick={() => { setActiveTab('register'); setErrorMsg(''); setPassword(''); setConfirmPassword(''); refreshCaptcha(); }}
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

                {captchaSection}

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

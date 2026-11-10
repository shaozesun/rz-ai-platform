import { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Lock, Phone, User, UserPlus, ArrowLeft, Eye, EyeOff } from 'lucide-react';
import { useAuthStore } from '../stores/authStore';
import { login, register, resetPassword, mgmtLogin, fetchCaptcha } from '../api/auth';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Card, CardContent } from '@/components/ui/card';

// 验证码方案切换: 'pillow' | 'turnstile'
const CAPTCHA_PROVIDER: string = 'pillow';
const TURNSTILE_SITE_KEY = '0x4AAAAAAEIxMciQ03FoqMNu';

const PASSWORD_RE = /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[!@#$%^&*()_+\-=\[\]{};':"\\|,.<>\/?~`]).{8,}$/;
const PASSWORD_MSG = '密码至少8位，必须包含大写字母、小写字母、数字和特殊符号';

// 毛玻璃风格：输入框与提交按钮的公共类（覆盖 shadcn Input/Button 基础类）
const GLASS_INPUT =
  'h-11 rounded-xl border border-white/20 bg-white/10 text-white placeholder:text-white ' +
  'focus-visible:border-white/40 focus-visible:ring-white/20';
const GLASS_BTN =
  'h-11 w-full rounded-xl border border-white/30 bg-white/20 font-medium text-white ' +
  'hover:bg-white/30 backdrop-blur-sm';

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
  const [mgmtUsername, setMgmtUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [activeTab, setActiveTab] = useState<'login' | 'register' | 'mgmt'>('login');
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
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);

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

  const fromLoc = (location.state as { from?: { pathname: string; search?: string } })?.from;
  const from = fromLoc ? `${fromLoc.pathname}${fromLoc.search || ''}` : '/';

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
    if (activeTab === 'mgmt') {
      if (!mgmtUsername.trim()) { setErrorMsg('请输入综合管理平台账号'); return; }
      if (!password) { setErrorMsg('请输入密码'); return; }
      if (!validateCaptcha()) return;
      setLogging(true);
      try {
        const res = await mgmtLogin(mgmtUsername.trim(), password, captchaId, captchaCode, turnstileToken);
        if (res.ok) {
          const d = res.data;
          setAuth(d.access_token, d.refresh_token, d.user, d.user.permissions);
        } else {
          setErrorMsg(res.msg || '登录失败');
        }
      } catch (err: unknown) {
        setErrorMsg(extractError(err, '综合管理平台登录失败，请检查账号或密码'));
        refreshCaptcha();
      } finally {
        setLogging(false);
      }
      return;
    }
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
        if (activeTab === 'register' && d?.pending) {
          setSuccessMsg(d.message || '注册申请已提交，请等待管理员审批');
          setActiveTab('login');
          setPassword('');
          setConfirmPassword('');
          refreshCaptcha();
          setTimeout(() => setSuccessMsg(''), 5000);
        } else {
          setAuth(d.access_token, d.refresh_token, d.user, d.user.permissions);
        }
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

  const switchTab = (tab: 'login' | 'mgmt' | 'register') => {
    setActiveTab(tab);
    setErrorMsg('');
    setSuccessMsg('');
    setPassword('');
    setConfirmPassword('');
    refreshCaptcha();
  };

  // 登录框高度按模式固定，切换时用 CSS transition 平滑过渡（参考毛玻璃示例）
  const cardHeight = forgotPw ? 'h-[600px]' : activeTab === 'register' ? 'h-[660px]' : 'h-[540px]';

  // 验证码 UI 组件
  const captchaSection = CAPTCHA_PROVIDER === 'turnstile' ? (
    <div id="turnstile-widget" className="flex justify-center" />
  ) : (
    <div>
      <label className="mb-1.5 block text-sm font-medium text-white/90">验证码</label>
      <div className="flex gap-2">
        {captchaImg ? (
          <img
            src={`data:image/png;base64,${captchaImg}`}
            alt="验证码"
            className="h-10 w-[120px] shrink-0 cursor-pointer rounded border border-white/20 object-contain bg-white/10"
            onClick={loadCaptcha}
            title="点击刷新验证码"
          />
        ) : (
          <div className="h-10 w-[120px] shrink-0 animate-pulse rounded border border-white/20 bg-white/10" />
        )}
        <Input
          placeholder="验证码"
          value={captchaCode}
          onChange={(e) => setCaptchaCode(e.target.value)}
          className={`flex-1 ${GLASS_INPUT}`}
        />
      </div>
    </div>
  );

  return (
    <div className="login-page relative flex min-h-screen items-center justify-center overflow-hidden p-4">
      {/* 背景图 */}
      <div
        aria-hidden
        className="absolute inset-0 bg-cover bg-center"
        style={{ backgroundImage: "url('/idc.jpg')" }}
      />
      {/* 暗遮罩增强对比 */}
      <div aria-hidden className="absolute inset-0 bg-black/20" />

      {/* 毛玻璃卡片 */}
      <Card className={`relative z-10 w-full max-w-[450px] overflow-hidden rounded-3xl border border-white/20 bg-white/10 text-white shadow-2xl backdrop-blur-xl transition-all duration-500 ease-out ${cardHeight}`}>
        {/* 玻璃高光层 */}
        <div aria-hidden className="pointer-events-none absolute inset-0 bg-gradient-to-br from-white/20 via-white/10 to-transparent" />
        <CardContent className="relative flex h-full flex-col justify-center px-12 py-8">
          {!forgotPw && (
            <div className="mb-6 text-center">
              <h1 className="text-2xl font-medium text-white drop-shadow-md">智能运维平台</h1>
            </div>
          )}

          {/* ===== 忘记密码 ===== */}
          {forgotPw ? (
            <div className="flex flex-col gap-4">
              <button
                onClick={exitForgot}
                className="flex items-center gap-1 text-sm text-white/70 hover:text-white"
              >
                <ArrowLeft className="size-3.5" /> 返回登录
              </button>

              <h2 className="text-center text-base font-semibold text-white">重置密码</h2>

              <div>
                <label className="mb-1.5 block text-sm font-medium text-white/90">手机号</label>
                <div className="relative">
                  <Phone className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-white/50" />
                  <Input
                    className={`pl-10 ${GLASS_INPUT}`}
                    placeholder="请输入手机号"
                    value={phone}
                    onChange={(e) => setPhone(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleResetPw()}
                  />
                </div>
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium text-white/90">新密码</label>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-white/50" />
                  <Input
                    className={`pl-10 pr-10 ${GLASS_INPUT}`}
                    type={showPassword ? 'text' : 'password'}
                    placeholder="新密码（至少 6 位）"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleResetPw()}
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword(!showPassword)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-white/50 hover:text-white/80"
                    aria-label={showPassword ? '隐藏密码' : '显示密码'}
                  >
                    {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
              </div>
              <div>
                <label className="mb-1.5 block text-sm font-medium text-white/90">确认密码</label>
                <div className="relative">
                  <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-white/50" />
                  <Input
                    className={`pl-10 pr-10 ${GLASS_INPUT}`}
                    type={showConfirmPassword ? 'text' : 'password'}
                    placeholder="再次输入新密码"
                    value={confirmPassword}
                    onChange={(e) => setConfirmPassword(e.target.value)}
                    onKeyDown={(e) => e.key === 'Enter' && handleResetPw()}
                  />
                  <button
                    type="button"
                    onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                    className="absolute right-3 top-1/2 -translate-y-1/2 text-white/50 hover:text-white/80"
                    aria-label={showConfirmPassword ? '隐藏密码' : '显示密码'}
                  >
                    {showConfirmPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                  </button>
                </div>
              </div>

              {captchaSection}

              {errorMsg && (
                <p className="rounded-lg bg-red-500/20 px-3 py-2 text-sm text-red-200">{errorMsg}</p>
              )}
              {successMsg && (
                <p className="rounded-lg bg-green-500/20 px-3 py-2 text-sm text-green-200">{successMsg}</p>
              )}

              <Button
                className={GLASS_BTN}
                size="lg"
                onClick={handleResetPw}
                disabled={logging}
              >
                重置密码
              </Button>
            </div>
          ) : (
            <>
              <div className="flex flex-col gap-4">
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-white/90">
                    {activeTab === 'mgmt' ? '综合管理平台账号' : '手机号'}
                  </label>
                  <div className="relative">
                    {activeTab === 'mgmt' ? (
                      <User className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-white/50" />
                    ) : (
                      <Phone className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-white/50" />
                    )}
                    <Input
                      className={`pl-10 ${GLASS_INPUT}`}
                      placeholder={activeTab === 'mgmt' ? '请输入综合管理平台账号' : '请输入手机号'}
                      value={activeTab === 'mgmt' ? mgmtUsername : phone}
                      onChange={(e) => (activeTab === 'mgmt' ? setMgmtUsername(e.target.value) : setPhone(e.target.value))}
                      onKeyDown={(e) => e.key === 'Enter' && doAuth()}
                    />
                  </div>
                </div>
                <div>
                  <label className="mb-1.5 block text-sm font-medium text-white/90">密码</label>
                  <div className="relative">
                    <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-white/50" />
                    <Input
                      className={`pl-10 pr-10 ${GLASS_INPUT}`}
                      type={showPassword ? 'text' : 'password'}
                      placeholder="请输入密码"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      onKeyDown={(e) => e.key === 'Enter' && doAuth()}
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword(!showPassword)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-white/50 hover:text-white/80"
                      aria-label={showPassword ? '隐藏密码' : '显示密码'}
                    >
                      {showPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                    </button>
                  </div>
                </div>
                {activeTab === 'register' && (
                  <div>
                    <label className="mb-1.5 block text-sm font-medium text-white/90">确认密码</label>
                    <div className="relative">
                      <Lock className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-white/50" />
                      <Input
                        className={`pl-10 pr-10 ${GLASS_INPUT}`}
                        type={showConfirmPassword ? 'text' : 'password'}
                        placeholder="请再次输入密码"
                        value={confirmPassword}
                        onChange={(e) => setConfirmPassword(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && doAuth()}
                      />
                      <button
                        type="button"
                        onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-white/50 hover:text-white/80"
                        aria-label={showConfirmPassword ? '隐藏密码' : '显示密码'}
                      >
                        {showConfirmPassword ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                      </button>
                    </div>
                  </div>
                )}

                {captchaSection}

                {activeTab === 'login' && (
                  <button
                    onClick={() => { setForgotPw(true); setErrorMsg(''); setSuccessMsg(''); }}
                    className="-mt-2 self-end text-xs text-white/70 hover:text-white"
                  >
                    忘记密码？
                  </button>
                )}

                {errorMsg && (
                  <p className="rounded-lg bg-red-500/20 px-3 py-2 text-sm text-red-200">{errorMsg}</p>
                )}
                {successMsg && (
                  <p className="rounded-lg bg-green-500/20 px-3 py-2 text-sm text-green-200">{successMsg}</p>
                )}

                <Button
                  className={GLASS_BTN}
                  size="lg"
                  onClick={doAuth}
                  disabled={logging}
                >
                  {activeTab === 'register' ? <UserPlus className="size-4" /> : <Lock className="size-4" />}
                  {activeTab === 'register' ? '注册' : '登录'}
                </Button>

                {activeTab === 'register' && (
                  <p className="text-center text-xs text-white/70">
                    提交后需管理员审批，通过后方可登录；默认开通 AI 对话权限，其他功能需另行申请
                  </p>
                )}

                <div className="flex items-center justify-center gap-3 text-sm">
                  {activeTab !== 'login' && (
                    <button onClick={() => switchTab('login')} className="text-white/70 transition-colors hover:text-white">手机号登录</button>
                  )}
                  {activeTab !== 'mgmt' && (
                    <button onClick={() => switchTab('mgmt')} className="text-white/70 transition-colors hover:text-white">综合管理平台登录</button>
                  )}
                  {activeTab !== 'register' && (
                    <button onClick={() => switchTab('register')} className="text-white/70 transition-colors hover:text-white">
                      {activeTab === 'login' ? '还没有账号？注册' : '注册'}
                    </button>
                  )}
                </div>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

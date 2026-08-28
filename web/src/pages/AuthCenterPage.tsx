import { useState, useEffect } from 'react';
import { KeyRound, ShieldCheck } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { getMgmtStatus, bindMgmtAccount, unbindMgmtAccount } from '@/api/auth';

export default function AuthCenterPage() {
  const [bound, setBound] = useState(false);
  const [usernameMasked, setUsernameMasked] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const [msgType, setMsgType] = useState<'success' | 'error'>('success');

  const showMsg = (text: string, type: 'success' | 'error' = 'success') => {
    setMsg(text);
    setMsgType(type);
    setTimeout(() => setMsg(''), 3000);
  };

  useEffect(() => {
    let cancelled = false;
    getMgmtStatus()
      .then((s) => {
        if (cancelled) return;
        setBound(s.bound);
        setUsernameMasked(s.username_masked);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  const handleBind = async () => {
    if (!username.trim() || !password) {
      showMsg('请输入综合管理平台账号和密码', 'error');
      return;
    }
    setLoading(true);
    try {
      await bindMgmtAccount(username.trim(), password);
      const s = await getMgmtStatus();
      setBound(s.bound);
      setUsernameMasked(s.username_masked);
      setUsername('');
      setPassword('');
      showMsg('绑定成功');
    } catch (e) {
      showMsg(
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail || '绑定失败',
        'error',
      );
    } finally {
      setLoading(false);
    }
  };

  const handleUnbind = async () => {
    setLoading(true);
    try {
      await unbindMgmtAccount();
      setBound(false);
      setUsernameMasked('');
      showMsg('已解绑');
    } catch {
      showMsg('解绑失败', 'error');
    } finally {
      setLoading(false);
    }
  };

  return (
    <PlatformShell
      title="认证中心"
      description="授权平台账号，Agent 查询综合管理平台数据时按您的账号身份鉴权"
    >
      <div className="space-y-6">
        {/* 说明 */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="size-4" />
              说明
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm text-muted-foreground">
            <p>
              绑定您的综合管理平台账号后，Agent 调用平台接口（人员、设备、工单、巡检等）时将使用
              您自己的账号身份，数据权限按您在综合管理平台中的部门 / 公司可见范围生效。
            </p>
            <p>
              未绑定时默认使用「手机号 + 初始密码」登录；若您已修改综合管理平台密码，请绑定正确的
              账号密码，否则相关查询会提示认证失败。
            </p>
          </CardContent>
        </Card>

        {/* mgmt 账号绑定 */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <KeyRound className="size-4" />
              综合管理平台账号
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {bound ? (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-foreground">已绑定账号：{usernameMasked}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    Agent 查询综合管理平台数据时将使用该账号身份
                  </p>
                </div>
                <Button size="sm" variant="outline" onClick={handleUnbind} disabled={loading}>
                  {loading ? '处理中…' : '解绑'}
                </Button>
              </div>
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  未绑定则默认使用「手机号 + 初始密码」登录；若密码已修改，请在此绑定自己的账号。
                </p>
                <div className="grid gap-3 sm:grid-cols-2">
                  <div>
                    <label className="text-sm font-medium text-foreground">账号</label>
                    <Input
                      className="mt-1.5"
                      placeholder="综合管理平台账号"
                      value={username}
                      onChange={(e) => setUsername(e.target.value)}
                    />
                  </div>
                  <div>
                    <label className="text-sm font-medium text-foreground">密码</label>
                    <Input
                      className="mt-1.5"
                      type="password"
                      placeholder="综合管理平台密码"
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                    />
                  </div>
                </div>
                <div className="flex items-center justify-between">
                  {msg && (
                    <p className={msgType === 'success' ? 'text-sm text-green-600' : 'text-sm text-destructive'}>
                      {msg}
                    </p>
                  )}
                  {!msg && <span />}
                  <Button size="sm" onClick={handleBind} disabled={loading}>
                    <KeyRound className="size-3.5" />
                    {loading ? '绑定中…' : '绑定'}
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </PlatformShell>
  );
}

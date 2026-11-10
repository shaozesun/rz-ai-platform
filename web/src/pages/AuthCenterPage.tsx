import { useState, useEffect } from 'react';
import {
  KeyRound, ShieldCheck, Bot, Copy, Check, MessageCircle,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { getMgmtStatus, bindMgmtAccount, unbindMgmtAccount } from '@/api/auth';
import { getFeishuStatus, connectFeishu, unbindFeishu } from '@/api/feishu';
import { getWechatStatus, connectWechat, unbindWechat } from '@/api/wechat';
import { copyText } from '@/lib/clipboard';

// 微信通道入口开关：暂先关闭（扫码接入方案确定后再打开）
const WECHAT_ENTRY_ENABLED: boolean = false;

export default function AuthCenterPage() {
  const [bound, setBound] = useState(false);
  const [usernameMasked, setUsernameMasked] = useState('');
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState('');
  const [msgType, setMsgType] = useState<'success' | 'error'>('success');
  const [larkBound, setLarkBound] = useState(false);
  const [larkName, setLarkName] = useState('');
  const [larkCode, setLarkCode] = useState('');
  const [copied, setCopied] = useState(false);
  const [wechatBound, setWechatBound] = useState(false);
  const [wechatName, setWechatName] = useState('');
  const [wechatCode, setWechatCode] = useState('');
  const [wechatCopied, setWechatCopied] = useState(false);

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
    getFeishuStatus()
      .then((s) => {
        if (cancelled) return;
        setLarkBound(s.lark_bound);
        setLarkName(s.lark_name);
      })
      .catch(() => {});
    if (WECHAT_ENTRY_ENABLED) {
      getWechatStatus()
        .then((s) => {
          if (cancelled) return;
          setWechatBound(s.wechat_bound);
          setWechatName(s.wechat_name);
        })
        .catch(() => {});
    }
    return () => { cancelled = true; };
  }, []);

  const handleGenerateCode = async () => {
    try {
      const d = await connectFeishu();
      setLarkCode(d.code);
    } catch {
      showMsg('生成绑定码失败', 'error');
    }
  };

  const handleUnbindFeishu = async () => {
    try {
      await unbindFeishu();
      setLarkBound(false);
      setLarkName('');
      setLarkCode('');
      showMsg('已解绑飞书');
    } catch {
      showMsg('解绑失败', 'error');
    }
  };

  const copyCode = async () => {
    // 降级逻辑收敛到 lib/clipboard（这里原来有一份本地实现，重复了）
    const ok = await copyText(larkCode);
    setCopied(ok);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleGenerateWechatCode = async () => {
    try {
      const d = await connectWechat();
      setWechatCode(d.code);
    } catch {
      showMsg('生成绑定码失败', 'error');
    }
  };

  const handleUnbindWechat = async () => {
    try {
      await unbindWechat();
      setWechatBound(false);
      setWechatName('');
      setWechatCode('');
      showMsg('已解绑微信');
    } catch {
      showMsg('解绑失败', 'error');
    }
  };

  const copyWechatCode = async () => {
    const ok = await copyText(wechatCode);
    setWechatCopied(ok);
    setTimeout(() => setWechatCopied(false), 2000);
  };

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
            <p>
              绑定飞书机器人后，可在飞书群聊 @机器人 或私聊与 Agent 对话，并支持拉取群消息、总结
              群消息、查看私聊记录。绑定方式：生成一次性验证码后，把验证码直接发给飞书机器人即可完成绑定。
            </p>
            {WECHAT_ENTRY_ENABLED && (
              <p>
                绑定微信机器人后，可在微信私聊与 Agent 对话（暂不支持读取微信群消息）。绑定方式：
                生成一次性验证码后，把验证码直接发给微信机器人即可完成绑定。
              </p>
            )}
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

        {/* 飞书绑定 */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Bot className="size-4" />
              飞书机器人
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {larkBound ? (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-foreground">
                    已绑定飞书{larkName ? `：${larkName}` : ''}
                  </p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    可在飞书群聊 @机器人 或私聊直接与平台 Agent 对话
                  </p>
                </div>
                <Button size="sm" variant="outline" onClick={handleUnbindFeishu}>
                  解绑
                </Button>
              </div>
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  绑定后即可在飞书里 @机器人 问答、私聊，或总结群消息。点击生成一次性验证码，
                  再到飞书私聊机器人直接发送，或群聊 @机器人 发送。
                </p>
                {larkCode ? (
                  <div className="space-y-2">
                    <p className="text-sm text-muted-foreground">
                      把下方验证码直接发给飞书机器人即可完成绑定
                    </p>
                    <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2">
                      <code className="flex-1 truncate text-sm font-mono">{larkCode}</code>
                      <Button size="sm" variant="ghost" onClick={copyCode}>
                        {copied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                        {copied ? '已复制' : '复制'}
                      </Button>
                    </div>
                  </div>
                ) : null}
                <Button size="sm" onClick={handleGenerateCode}>
                  <Bot className="size-3.5" />
                  生成绑定码
                </Button>
              </div>
            )}
          </CardContent>
        </Card>

        {/* 微信绑定（入口暂关） */}
        {WECHAT_ENTRY_ENABLED && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <MessageCircle className="size-4" />
              微信机器人
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {wechatBound ? (
              <div className="flex items-center justify-between">
                <div>
                  <p className="text-sm font-medium text-foreground">
                    已绑定微信{wechatName ? `：${wechatName}` : ''}
                  </p>
                  <p className="text-xs text-muted-foreground mt-0.5">
                    可在微信私聊直接与平台 Agent 对话
                  </p>
                </div>
                <Button size="sm" variant="outline" onClick={handleUnbindWechat}>
                  解绑
                </Button>
              </div>
            ) : (
              <div className="space-y-3">
                <p className="text-sm text-muted-foreground">
                  绑定后即可在微信私聊与 Agent 问答。点击生成一次性验证码，再到微信把这个
                  验证码直接发给机器人即可完成绑定。
                </p>
                {wechatCode ? (
                  <div className="space-y-2">
                    <p className="text-sm text-muted-foreground">
                      把下方验证码直接发给微信机器人即可完成绑定
                    </p>
                    <div className="flex items-center gap-2 rounded-lg border border-border bg-muted/40 px-3 py-2">
                      <code className="flex-1 truncate text-sm font-mono">{wechatCode}</code>
                      <Button size="sm" variant="ghost" onClick={copyWechatCode}>
                        {wechatCopied ? <Check className="size-3.5" /> : <Copy className="size-3.5" />}
                        {wechatCopied ? '已复制' : '复制'}
                      </Button>
                    </div>
                  </div>
                ) : null}
                <Button size="sm" onClick={handleGenerateWechatCode}>
                  <MessageCircle className="size-3.5" />
                  生成绑定码
                </Button>
              </div>
            )}
          </CardContent>
        </Card>
        )}
      </div>
    </PlatformShell>
  );
}

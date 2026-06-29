import { useState, useRef } from 'react';
import { User, Bell, Trash2, Save, Camera } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { useAuthStore } from '@/stores/authStore';
import { updateProfile } from '@/api/auth';

const NOTIFY_KEY = 'rz_notify_enabled';

const AVATAR_SIZE = 128;

function compressImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const canvas = document.createElement('canvas');
        canvas.width = AVATAR_SIZE;
        canvas.height = AVATAR_SIZE;
        const ctx = canvas.getContext('2d')!;
        ctx.drawImage(img, 0, 0, AVATAR_SIZE, AVATAR_SIZE);
        resolve(canvas.toDataURL('image/jpeg', 0.7));
      };
      img.onerror = reject;
      img.src = reader.result as string;
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export default function SettingsPage() {
  const user = useAuthStore((s) => s.user);
  const updateUser = useAuthStore((s) => s.updateUser);
  const clearAuth = useAuthStore((s) => s.clearAuth);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const [name, setName] = useState(user?.name || '');
  const [phone, setPhone] = useState(user?.phone || '');
  const [avatar, setAvatar] = useState(user?.avatar || '');
  const [avatarPreview, setAvatarPreview] = useState(user?.avatar || '');
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState('');
  const [msgType, setMsgType] = useState<'success' | 'error'>('success');

  const [notifyEnabled, setNotifyEnabled] = useState(() => {
    const stored = localStorage.getItem(NOTIFY_KEY);
    return stored !== 'false';
  });

  const showMsg = (text: string, type: 'success' | 'error' = 'success') => {
    setMsg(text);
    setMsgType(type);
    setTimeout(() => setMsg(''), 3000);
  };

  const handleSave = async () => {
    if (!name.trim()) { showMsg('请输入姓名', 'error'); return; }
    setSaving(true);
    try {
      const res = await updateProfile({ name: name.trim(), phone: phone.trim(), avatar });
      if (res.ok) {
        updateUser(res.data);
        setAvatarPreview(avatar);
        showMsg('保存成功');
      }
    } catch {
      showMsg('保存失败', 'error');
    } finally {
      setSaving(false);
    }
  };

  const handleAvatarChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    try {
      const dataUrl = await compressImage(file);
      setAvatar(dataUrl);
      setAvatarPreview(dataUrl);
    } catch {
      showMsg('图片处理失败', 'error');
    }
  };

  const toggleNotify = () => {
    const next = !notifyEnabled;
    setNotifyEnabled(next);
    localStorage.setItem(NOTIFY_KEY, String(next));
  };

  const handleClearCache = () => {
    localStorage.clear();
    clearAuth();
    window.location.href = '/login';
  };

  return (
    <PlatformShell
      title="个人设置"
      description="编辑个人资料与管理偏好"
    >
      <div className="space-y-6">
        {/* Basic info */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <User className="size-4" />
              基本信息
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Avatar */}
            <div className="flex items-center gap-4">
              <div
                className="relative size-16 shrink-0 cursor-pointer overflow-hidden rounded-full bg-accent ring-2 ring-border/60"
                onClick={() => fileInputRef.current?.click()}
              >
                {avatarPreview ? (
                  <img src={avatarPreview} alt="头像" className="size-full object-cover" />
                ) : (
                  <div className="flex size-full items-center justify-center text-2xl font-semibold text-muted-foreground">
                    {user?.name ? user.name.slice(0, 1).toUpperCase() : user?.phone?.slice(-1) || 'U'}
                  </div>
                )}
                <div className="absolute inset-0 flex items-center justify-center rounded-full bg-foreground/0 transition-colors hover:bg-foreground/20">
                  <Camera className="size-5 text-white opacity-0 transition-opacity hover:opacity-100" />
                </div>
              </div>
              <div>
                <p className="text-sm font-medium text-foreground">头像</p>
                <p className="text-xs text-muted-foreground mt-0.5">点击更换，自动裁剪 128x128</p>
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/*"
                className="hidden"
                onChange={handleAvatarChange}
              />
            </div>

            <div>
              <label className="text-sm font-medium text-foreground">姓名</label>
              <Input
                className="mt-1.5"
                placeholder="请输入姓名"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div>
              <label className="text-sm font-medium text-foreground">手机号</label>
              <Input
                className="mt-1.5"
                placeholder="请输入手机号"
                value={phone}
                onChange={(e) => setPhone(e.target.value)}
              />
            </div>
            <div className="flex items-center justify-between">
              {msg && (
                <p className={msgType === 'success'
                  ? 'text-sm text-green-600'
                  : 'text-sm text-destructive'}>
                  {msg}
                </p>
              )}
              {!msg && <span />}
              <Button size="sm" onClick={handleSave} disabled={saving}>
                <Save className="size-3.5" />
                {saving ? '保存中…' : '保存'}
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* Preferences */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Bell className="size-4" />
              偏好设置
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-foreground">动态通知</p>
                <p className="text-xs text-muted-foreground mt-0.5">
                  关闭后个人活动将不在首页动态中展示
                </p>
              </div>
              <button
                onClick={toggleNotify}
                className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${
                  notifyEnabled ? 'bg-primary' : 'bg-muted'
                }`}
              >
                <span
                  className={`inline-block size-4 transform rounded-full bg-white transition-transform ${
                    notifyEnabled ? 'translate-x-6' : 'translate-x-1'
                  }`}
                />
              </button>
            </div>
          </CardContent>
        </Card>

        {/* Danger zone */}
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Trash2 className="size-4" />
              数据管理
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-sm text-muted-foreground mb-3">
              清除所有本地缓存数据，包括登录状态、用户信息等。清除后需要重新登录。
            </p>
            <Button variant="destructive" size="sm" onClick={handleClearCache}>
              <Trash2 className="size-3.5" />
              清除缓存
            </Button>
          </CardContent>
        </Card>
      </div>
    </PlatformShell>
  );
}

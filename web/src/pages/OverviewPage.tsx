import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Activity,
  Zap,
  ArrowUpRight,
  MessageSquareText,
  Clapperboard,
  ShieldAlert,
  Users,
  BookOpen,
  Flame,
} from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { UsageAreaChart, DistributionChart } from '@/components/dashboard/usage-charts';
import { useAuthStore } from '@/stores/authStore';
import PLATFORM_CHANGELOG from '@/data/platformChangelog';
import {
  getActivities,
  getOverview,
  getTrend,
  type ActivityItem,
  type OverviewData,
  type TrendItem,
} from '@/api/stats';

const modules = [
  { href: '/assistant', title: '对话助手', desc: '企业知识库问答、智能客服与办公助理', icon: MessageSquareText, status: '运行中', metric: '知识库 RAG 驱动' },
  { href: '/knowledge', title: '知识库', desc: '文档上传、向量化管理，为对话提供知识溯源', icon: BookOpen, status: '运行中', metric: '支持 7 种格式' },
  { href: '/risk', title: '隐患识别', desc: 'AI 安全隐患检测、两阶段 VLM+CV 流水线', icon: ShieldAlert, status: '运行中', metric: '4 种机柜模板' },
  { href: '/fire-safety', title: '消防配置', desc: '基于建筑参数自动推荐消防设施配置方案', icon: Flame, status: '运行中', metric: '5 种快速填充' },
  { href: '/video', title: '视频生成', desc: 'PPT 转解说视频，TTS 语音合成，MP4 输出', icon: Clapperboard, status: '运行中', metric: '全自动流程' },
];

function timeAgo(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = Date.now();
  const diff = now - d.getTime();
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(diff / 3600000);
  if (mins < 1) return '刚刚';
  if (mins < 60) return `${mins} 分钟前`;
  if (hours < 24) return `${hours} 小时前`;
  return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
}

const dotColor = (type: string) => {
  if (type === 'video') return 'bg-chart-2';
  return 'bg-muted-foreground';
};

export default function OverviewPage() {
  const user = useAuthStore((s) => s.user);
  const userName = user?.name || user?.phone || '用户';
  const [overview, setOverview] = useState<OverviewData>({ api_call_count: 0, compute_usage: 0, user_count: 0, session_count: 0, video_count: 0, group_count: 0, risk_check_count: 0, fire_safety_count: 0 });
  const [activities, setActivities] = useState<ActivityItem[]>([]);
  const [trend, setTrend] = useState<TrendItem[]>([]);

  useEffect(() => {
    getOverview().then(setOverview).catch((err) => { console.error('getOverview failed:', err); });
    getActivities(5).then(setActivities).catch((err) => { console.error('getActivities failed:', err); setActivities([]); });
    getTrend().then(setTrend).catch(() => {});
  }, []);

  return (
    <PlatformShell
      title="总览"
      description={`欢迎回来，${userName}。这是您企业 AI 平台的整体运行情况。`}
    >
      {/* Stat cards — flat, mobile-first */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-4">
        {[
          { label: 'API 调用总量', value: overview.api_call_count.toLocaleString(), icon: Activity, sub: '累计请求', tone: 'bg-chart-5/15 text-chart-5' },
          { label: '算力消耗', value: overview.compute_usage.toLocaleString(), icon: Zap, sub: 'Token 消耗', tone: 'bg-chart-6/15 text-chart-6' },
          { label: '活跃用户', value: overview.user_count.toLocaleString(), icon: Users, sub: '注册用户', tone: 'bg-chart-1/15 text-chart-1' },
          { label: '对话会话', value: overview.session_count.toLocaleString(), icon: MessageSquareText, sub: '累计会话', tone: 'bg-chart-2/15 text-chart-2' },
          { label: '视频生成', value: overview.video_count.toLocaleString(), icon: Clapperboard, sub: '生成任务', tone: 'bg-chart-3/15 text-chart-3' },
          { label: '隐患识别', value: overview.risk_check_count.toLocaleString(), icon: ShieldAlert, sub: '检测次数', tone: 'bg-chart-4/15 text-chart-4' },
          { label: '消防配置', value: overview.fire_safety_count.toLocaleString(), icon: Flame, sub: '推荐次数', tone: 'bg-amber-100 text-amber-600' },
          { label: '知识库', value: overview.group_count.toLocaleString(), icon: BookOpen, sub: '分组数', tone: 'bg-primary/10 text-primary' },
        ].map((s) => {
          const Icon = s.icon;
          return (
            <div
              key={s.label}
              className="flex items-center gap-3 rounded-lg border border-border/60 bg-muted/30 px-3.5 py-3"
            >
              <div className={`flex size-9 shrink-0 items-center justify-center rounded-md ${s.tone}`}>
                <Icon className="size-4.5" />
              </div>
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs text-muted-foreground">{s.label}</p>
                <p className="text-lg font-semibold tracking-tight tabular-nums">{s.value}</p>
                <p className="text-[11px] text-muted-foreground/70">{s.sub}</p>
              </div>
            </div>
          );
        })}
      </div>

      {/* Charts */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <div>
              <CardTitle>调用趋势</CardTitle>
              <p className="mt-1 text-sm text-muted-foreground">近 7 天各能力调用量</p>
            </div>
          </CardHeader>
          <CardContent>
            <UsageAreaChart data={trend} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>能力占比</CardTitle>
            <p className="text-sm text-muted-foreground">按调用量分布</p>
          </CardHeader>
          <CardContent>
            <DistributionChart data={[
              { name: '对话助手', value: overview.session_count, color: 'var(--chart-1)' },
              { name: '视频生成', value: overview.video_count, color: 'var(--chart-2)' },
              { name: '隐患识别', value: overview.risk_check_count, color: 'var(--chart-3)' },
              { name: '消防配置', value: overview.fire_safety_count, color: 'var(--chart-4)' },
            ]} />
          </CardContent>
        </Card>
      </div>

      {/* Modules + activity */}
      <div className="mt-6 grid grid-cols-1 gap-4 lg:grid-cols-3">
        <div className="lg:col-span-2">
          <h2 className="mb-3 text-sm font-medium text-muted-foreground">AI 能力模块</h2>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {modules.map((m) => {
              const Icon = m.icon;
              return (
                <Link key={m.href} to={m.href} className="group">
                  <Card className="h-full transition-all hover:border-primary/40 hover:shadow-md">
                    <CardContent className="p-5">
                      <div className="flex size-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
                        <Icon className="size-5" />
                      </div>
                      <h3 className="mt-4 flex items-center gap-1.5 font-semibold">
                        {m.title}
                        <ArrowUpRight className="size-4 text-muted-foreground transition-transform group-hover:translate-x-0.5 group-hover:-translate-y-0.5" />
                      </h3>
                      <p className="mt-1 text-sm leading-relaxed text-muted-foreground">{m.desc}</p>
                      <div className="mt-4 flex items-center justify-between border-t border-border pt-3">
                        <span className="text-xs text-muted-foreground">{m.metric}</span>
                        <Badge variant="outline">{m.status}</Badge>
                      </div>
                    </CardContent>
                  </Card>
                </Link>
              );
            })}
          </div>
        </div>

        {/* Activity feed: system changelog + user activities */}
        <Card>
          <CardHeader>
            <CardTitle>动态</CardTitle>
            <p className="text-sm text-muted-foreground">系统更新与个人活动</p>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {/* User activities */}
            {activities.length > 0 && (
              <div className="flex flex-col gap-3">
                {activities.map((a, i) => (
                  <div key={i} className="flex gap-3">
                    <span className={`mt-1.5 size-2 shrink-0 rounded-full ${dotColor(a.type)}`} />
                    <div className="leading-snug">
                      <p className="text-sm text-pretty">{a.text}</p>
                      <p className="mt-0.5 text-xs text-muted-foreground">{timeAgo(a.time)}</p>
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* System changelog */}
            {PLATFORM_CHANGELOG.slice(0, 3).map((entry) => (
              <div key={entry.version} className="flex gap-3">
                <span className="mt-1.5 size-2 shrink-0 rounded-full bg-primary" />
                <div className="leading-snug">
                  <div className="flex items-center gap-2">
                    <p className="text-sm font-medium">{entry.title}</p>
                    <Badge variant="outline" className="text-[10px] px-1.5 py-0">{entry.version}</Badge>
                  </div>
                  <p className="mt-0.5 text-xs text-muted-foreground text-pretty">{entry.body}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">{entry.date}</p>
                </div>
              </div>
            ))}

            {activities.length === 0 && PLATFORM_CHANGELOG.length === 0 && (
              <p className="py-4 text-center text-xs text-muted-foreground">暂无动态</p>
            )}
          </CardContent>
        </Card>
      </div>

    </PlatformShell>
  );
}

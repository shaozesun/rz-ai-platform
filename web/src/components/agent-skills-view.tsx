import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Bot, Loader2, Trash2, X } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import {
  deleteSkill,
  getSkill,
  listSkills,
  setSkillEnabled,
  type AgentSkill,
  type SkillDetail,
} from '@/api/skills';

type Tab = 'all' | 'official' | 'mine';

const TABS: { key: Tab; label: string }[] = [
  { key: 'all', label: '全部' },
  { key: 'official', label: '公共' },
  { key: 'mine', label: '自定义' },
];

/** 图标块按技能名 hash 取色 —— 只是视觉区分，不承载语义。 */
const ICON_TONES = [
  'bg-chart-1/12 text-chart-1',
  'bg-chart-2/12 text-chart-2',
  'bg-chart-3/12 text-chart-3',
  'bg-chart-4/12 text-chart-4',
];

function toneOf(name: string): string {
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) >>> 0;
  return ICON_TONES[hash % ICON_TONES.length];
}

/** 手工开关（项目没有 Switch 组件，样式对齐设置页的偏好开关）。 */
function Toggle({ enabled, onToggle }: { enabled: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      onClick={onToggle}
      role="switch"
      aria-checked={enabled}
      aria-label={enabled ? '停用技能' : '启用技能'}
      className={cn(
        'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors',
        enabled ? 'bg-success' : 'bg-muted',
      )}
    >
      <span
        className={cn(
          'inline-block size-4 transform rounded-full bg-white transition-transform',
          enabled ? 'translate-x-6' : 'translate-x-1',
        )}
      />
    </button>
  );
}

function SkillCard({
  skill,
  onOpenDetail,
  onToggle,
  onDelete,
}: {
  skill: AgentSkill;
  onOpenDetail: (skill: AgentSkill) => void;
  onToggle: (skill: AgentSkill) => void;
  onDelete: (skill: AgentSkill) => void;
}) {
  return (
    <Card className="flex flex-col">
      <CardContent className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-start gap-3">
          <div className={cn('flex size-10 shrink-0 items-center justify-center rounded-lg', toneOf(skill.name))}>
            <Bot className="size-5" />
          </div>
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <h3 className="truncate text-sm font-semibold" title={skill.title}>{skill.title}</h3>
              <Badge variant={skill.owner ? 'primary' : 'outline'} className="shrink-0">
                {skill.owner ? '自定义' : '公共'}
              </Badge>
            </div>
            <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">{skill.name}</p>
          </div>
        </div>

        <p className="line-clamp-2 min-h-10 text-sm text-muted-foreground">
          {skill.description || '暂无说明'}
        </p>

        <div className="mt-auto flex items-center justify-between gap-2 border-t border-border pt-3">
          <span className="flex items-center gap-1.5 text-xs">
            <span className={cn('size-1.5 rounded-full', skill.enabled ? 'bg-success' : 'bg-muted-foreground/40')} />
            <span className={skill.enabled ? 'text-foreground' : 'text-muted-foreground'}>
              {skill.enabled ? '已启用' : '已停用'}
            </span>
          </span>
          <div className="flex items-center gap-1">
            <Button size="sm" variant="ghost" onClick={() => onOpenDetail(skill)}>详情</Button>
            {skill.editable && (
              <>
                <Toggle enabled={skill.enabled} onToggle={() => onToggle(skill)} />
                <Button
                  size="icon-sm"
                  variant="ghost"
                  className="text-destructive hover:text-destructive"
                  onClick={() => onDelete(skill)}
                  aria-label={`删除技能 ${skill.title}`}
                >
                  <Trash2 className="size-3.5" />
                </Button>
              </>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

export function AgentSkillsView({ query, searchSlot }: { query: string; searchSlot?: ReactNode }) {
  const [skills, setSkills] = useState<AgentSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<Tab>('all');
  const [detailOpen, setDetailOpen] = useState(false);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setSkills(await listSkills());
    } catch {
      setError('技能列表加载失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const counts = useMemo<Record<Tab, number>>(() => ({
    all: skills.length,
    official: skills.filter((s) => !s.owner).length,
    mine: skills.filter((s) => s.owner).length,
  }), [skills]);

  const visible = useMemo(() => {
    const kw = query.trim().toLowerCase();
    return skills.filter((s) => {
      if (tab === 'official' && s.owner) return false;
      if (tab === 'mine' && !s.owner) return false;
      if (!kw) return true;
      return [s.title, s.name, s.description].join(' ').toLowerCase().includes(kw);
    });
  }, [skills, tab, query]);

  /** 乐观更新：先翻本地状态，失败再回滚并提示。 */
  const handleToggle = async (skill: AgentSkill) => {
    const next = !skill.enabled;
    setSkills((prev) => prev.map((s) => (s.name === skill.name ? { ...s, enabled: next } : s)));
    try {
      await setSkillEnabled(skill.name, next);
    } catch {
      setSkills((prev) => prev.map((s) => (s.name === skill.name ? { ...s, enabled: skill.enabled } : s)));
      setError(`技能「${skill.title}」${next ? '启用' : '停用'}失败，请稍后重试`);
    }
  };

  const handleOpenDetail = async (skill: AgentSkill) => {
    setDetailOpen(true);
    setDetail(null);
    setDetailLoading(true);
    try {
      setDetail(await getSkill(skill.name));
    } catch {
      setError(`技能「${skill.title}」详情加载失败`);
      setDetailOpen(false);
    } finally {
      setDetailLoading(false);
    }
  };

  const handleDelete = async (skill: AgentSkill) => {
    const ok = window.confirm(
      `确定删除技能「${skill.title}」吗？删除后不可恢复。\n如需再次使用，可在对话里让智能体重建。`,
    );
    if (!ok) return;
    try {
      await deleteSkill(skill.name);
      setSkills((prev) => prev.filter((s) => s.name !== skill.name));
      if (detail?.name === skill.name) setDetailOpen(false);
    } catch {
      setError(`技能「${skill.title}」删除失败，请稍后重试`);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-border bg-card p-2 shadow-sm">
        {TABS.map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={cn(
              'rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
              tab === t.key
                ? 'bg-primary text-primary-foreground'
                : 'text-muted-foreground hover:bg-muted hover:text-foreground',
            )}
          >
            {t.label}
            <span className="ml-1.5 text-xs opacity-80">{counts[t.key]}</span>
          </button>
        ))}
        {searchSlot && <div className="ml-auto">{searchSlot}</div>}
      </div>

      {error && (
        <div className="flex items-center justify-between gap-3 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">
          <span>{error}</span>
          <button onClick={() => setError('')} className="shrink-0 opacity-70 hover:opacity-100" aria-label="关闭提示">
            <X className="size-4" />
          </button>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-20">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : visible.length === 0 ? (
        <Card>
          <CardContent className="py-16 text-center text-sm text-muted-foreground">
            {query
              ? '没有匹配的技能'
              : tab === 'mine'
                ? '还没有自定义技能 —— 可以在对话里让智能体帮你创建'
                : '暂无技能'}
          </CardContent>
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {visible.map((skill) => (
            <SkillCard
              key={skill.name}
              skill={skill}
              onOpenDetail={(s) => void handleOpenDetail(s)}
              onToggle={(s) => void handleToggle(s)}
              onDelete={(s) => void handleDelete(s)}
            />
          ))}
        </div>
      )}

      {detailOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
          <div className="absolute inset-0 bg-foreground/30" onClick={() => setDetailOpen(false)} />
          <div className="relative z-10 flex max-h-[85vh] w-full max-w-3xl flex-col rounded-xl border border-border bg-card shadow-lg">
            <div className="flex items-start justify-between gap-3 border-b border-border px-5 py-4">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <h3 className="truncate text-lg font-semibold">{detailLoading ? '加载中…' : detail?.title}</h3>
                  {detail && (
                    <Badge variant={detail.owner ? 'primary' : 'outline'} className="shrink-0">
                      {detail.owner ? '自定义' : '公共'}
                    </Badge>
                  )}
                </div>
                {detail && (
                  <p className="mt-0.5 truncate font-mono text-xs text-muted-foreground">
                    {detail.name}
                    {!detail.enabled && ' · 已停用'}
                  </p>
                )}
              </div>
              <button
                onClick={() => setDetailOpen(false)}
                className="rounded-md p-1 text-muted-foreground hover:bg-accent"
                aria-label="关闭详情"
              >
                <X className="size-4" />
              </button>
            </div>

            <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
              {detailLoading ? (
                <div className="flex items-center justify-center py-16">
                  <Loader2 className="size-6 animate-spin text-muted-foreground" />
                </div>
              ) : detail ? (
                <div className="space-y-4">
                  {detail.description && (
                    <p className="text-sm text-muted-foreground">{detail.description}</p>
                  )}
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
                    <span>分类：{detail.category || '—'}</span>
                    <span>领域：{detail.domain || '—'}</span>
                    <span>类型：{detail.kind || '—'}</span>
                  </div>
                  <div>
                    <p className="mb-1.5 text-xs font-medium text-muted-foreground">SKILL.md</p>
                    <pre className="max-h-[45vh] overflow-y-auto whitespace-pre-wrap break-words rounded-lg border border-border bg-secondary/40 p-3 font-mono text-xs leading-relaxed">
                      {detail.skill_md}
                    </pre>
                  </div>
                  {detail.references.length > 0 && (
                    <div>
                      <p className="mb-1.5 text-xs font-medium text-muted-foreground">
                        附带文档（{detail.references.length}）
                      </p>
                      <ul className="space-y-1">
                        {detail.references.map((ref) => (
                          <li
                            key={ref.type}
                            className="rounded-lg border border-border bg-secondary/30 px-3 py-2 text-sm"
                          >
                            <span className="font-medium">{ref.name}</span>
                            {ref.description && (
                              <span className="ml-2 text-xs text-muted-foreground">{ref.description}</span>
                            )}
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

import { useCallback, useEffect, useMemo, useState } from 'react';
import { Loader2, X } from 'lucide-react';
import { MindMap, type MindMapOptions } from '@ant-design/graphs';
import type { TreeData } from '@antv/g6';
import { Card } from '@/components/ui/card';
import { listTools, type ToolGroup, type ToolItem } from '@/api/tools';

/**
 * 重新打开折叠交互：默认配置里 collapse-expand-react-node 是 `enable: false`（只画节点不响应点击），
 * 这里把它改成 true，节点旁才会出现「展开 / 收起 + 子节点数」的圆形按钮。
 */
const ENABLE_COLLAPSE: MindMapOptions['transforms'] = (prev) =>
  prev.map((item) => {
    if (typeof item !== 'object' || item === null) return item;
    return 'key' in item && item.key === 'collapse-expand-react-node'
      ? { ...item, enable: true }
      : item;
  });

function matches(tool: ToolItem, keyword: string): boolean {
  const parts = [tool.title, tool.name, tool.summary, tool.category_label];
  return parts.join(' ').toLowerCase().includes(keyword);
}

function toolNode(groupKey: string, tool: ToolItem): TreeData {
  return { id: `t:${groupKey}/${tool.name}`, data: { label: tool.title } };
}

/** 平台节点：平台 → 分类 → 工具；无分类的工具（内置工具）直接挂在平台下。 */
function platformNode(
  group: ToolGroup,
  keyword: string,
): { node: TreeData; count: number } | null {
  const tools = keyword ? group.tools.filter((tool) => matches(tool, keyword)) : group.tools;
  if (!tools.length) return null;

  const buckets = new Map<string, ToolItem[]>();
  for (const tool of tools) {
    const label = (tool.category_label || '').trim();
    const list = buckets.get(label);
    if (list) list.push(tool);
    else buckets.set(label, [tool]);
  }
  const direct = buckets.get('') ?? [];
  buckets.delete('');

  const children: TreeData[] = direct.map((tool) => toolNode(group.key, tool));
  const categories = [...buckets];
  if (categories.length === 1 && direct.length === 0) {
    // 只有一个分类且没有直属工具：省掉分类层，工具直接列在平台下
    for (const tool of categories[0][1]) children.push(toolNode(group.key, tool));
  } else {
    for (const [label, items] of categories) {
      children.push({
        id: `c:${group.key}/${label}`,
        data: { label },
        children: items.map((tool) => toolNode(group.key, tool)),
      });
    }
  }
  return {
    node: { id: `p:${group.key}`, data: { label: group.label }, children },
    count: tools.length,
  };
}

function buildTree(groups: ToolGroup[], keyword: string): { tree: TreeData; total: number } {
  const children: TreeData[] = [];
  let total = 0;
  for (const group of groups) {
    const built = platformNode(group, keyword);
    if (!built) continue;
    children.push(built.node);
    total += built.count;
  }
  return { tree: { id: 'root', data: { label: '智能体工具' }, children }, total };
}

/** 工具中心：只读目录，按「平台 → 分类 → 工具」画成可展开 / 收起的思维导图。 */
export function AgentToolsView({ query }: { query: string }) {
  const [groups, setGroups] = useState<ToolGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  // 跟随应用明暗模式：深色（dark/black）下用深色主题，否则节点文字看不清
  const [isDark, setIsDark] = useState(() => document.documentElement.classList.contains('dark'));

  useEffect(() => {
    const el = document.documentElement;
    const check = () => setIsDark(el.classList.contains('dark'));
    check();
    const mo = new MutationObserver(check);
    mo.observe(el, { attributes: true, attributeFilter: ['class'] });
    return () => mo.disconnect();
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      setGroups(await listTools());
    } catch {
      setError('工具列表加载失败，请稍后重试');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const keyword = query.trim().toLowerCase();
  const searching = keyword !== '';
  const { tree, total } = useMemo(() => buildTree(groups, keyword), [groups, keyword]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-20">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {error && (
        <div className="flex items-center justify-between gap-3 rounded-lg bg-destructive/10 px-3 py-2 text-sm text-destructive">
          <span>{error}</span>
          <button onClick={() => setError('')} className="shrink-0 opacity-70 hover:opacity-100" aria-label="关闭提示">
            <X className="size-4" />
          </button>
        </div>
      )}

      {total === 0 ? (
        <Card className="py-16 text-center text-sm text-muted-foreground">
          {searching ? '没有匹配的工具' : '暂无可用工具'}
        </Card>
      ) : (
        <Card className="overflow-hidden p-0">
          <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5">
            <span className="text-xs text-muted-foreground">
              点节点旁的箭头展开 / 收起 · 滚轮缩放 · 拖拽平移
            </span>
            <span className="shrink-0 text-xs text-muted-foreground">
              {searching ? `命中 ${total} 个工具` : `共 ${total} 个工具`}
            </span>
          </div>
          <MindMap
            // 浏览 / 搜索的默认展开层级不同，换 key 重挂载，避免沿用上一次的折叠状态
            key={searching ? 'search' : 'browse'}
            data={tree}
            height={searching ? 620 : 520}
            type="boxed"
            labelField="label"
            theme={isDark ? 'dark' : 'light'}
            defaultExpandLevel={searching ? 99 : 1}
            transforms={ENABLE_COLLAPSE}
            nodeMaxWidth={280}
            autoFit="view"
          />
        </Card>
      )}
    </div>
  );
}

import { useState } from 'react';
import { Search } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Input } from '@/components/ui/input';
import { AgentToolsView } from '@/components/agent-tools-view';

/** 工具中心：只读列出智能体当前可调用的全部工具。 */
export default function AgentToolsPage() {
  const [query, setQuery] = useState('');

  return (
    <PlatformShell
      title="工具中心"
      description="智能体当前可调用的全部工具（只读）。可用性由部署开关与角色权限决定。"
    >
      <div className="space-y-4">
        <div className="flex items-center rounded-xl border border-border bg-card p-2 shadow-sm">
          <div className="relative ml-auto w-full sm:w-64">
            <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索工具标题、分类或说明"
              className="pl-8"
            />
          </div>
        </div>
        <AgentToolsView query={query} />
      </div>
    </PlatformShell>
  );
}

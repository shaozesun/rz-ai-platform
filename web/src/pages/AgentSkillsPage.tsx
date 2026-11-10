import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MessageSquareText, Search } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { AgentSkillsView } from '@/components/agent-skills-view';

/** 技能中心：官方技能只读，自建技能可启停 / 查看详情 / 删除。 */
export default function AgentSkillsPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');

  return (
    <PlatformShell
      title="技能中心"
      description="平台技能目录。官方技能只读；自建技能可停用、查看详情或删除。"
      actions={(
        <Button size="sm" onClick={() => navigate('/assistant')}>
          <MessageSquareText className="size-4" />
          通过对话创建技能
        </Button>
      )}
    >
      <AgentSkillsView
        query={query}
        searchSlot={(
          <div className="relative w-full sm:w-64">
            <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索技能名称或说明"
              className="pl-8"
            />
          </div>
        )}
      />
    </PlatformShell>
  );
}

import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { MessageSquareText, Search, Wrench } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { AgentSkillsView } from '@/components/agent-skills-view';
import { AgentToolsDialog } from '@/components/agent-tools-dialog';

/** 技能中心：官方技能只读，自建技能可启停 / 查看详情 / 删除；工具目录在同一页的小窗查看。 */
export default function AgentSkillsPage() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [toolsOpen, setToolsOpen] = useState(false);

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
          <div className="flex w-full items-center gap-2 sm:w-auto">
            <div className="relative min-w-0 flex-1 sm:w-64 sm:flex-none">
              <Search className="absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索技能名称或说明"
                className="pl-8"
              />
            </div>
            <Button size="sm" variant="outline" onClick={() => setToolsOpen(true)}>
              <Wrench className="size-4" />
              工具
            </Button>
          </div>
        )}
      />
      <AgentToolsDialog open={toolsOpen} onClose={() => setToolsOpen(false)} />
    </PlatformShell>
  );
}

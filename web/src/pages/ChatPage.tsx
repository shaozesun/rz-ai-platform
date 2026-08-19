import { useEffect } from 'react';
import { Menu, Plus } from 'lucide-react';
import { PlatformShell } from '@/components/platform-shell';
import ChatArea from '@/components/ChatArea';
import { Button } from '@/components/ui/button';
import { useChatStore } from '@/stores/chatStore';

export default function ChatPage() {
  const { loadSessions, openSessionPanel, newSession } = useChatStore();

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  return (
    <PlatformShell
      edgeToEdge
      headerActionsLeft={
        <Button
          variant="ghost"
          onClick={openSessionPanel}
          className="gap-1.5 px-2 text-muted-foreground"
          aria-label="会话历史"
        >
          <Menu className="size-4" />
          <span className="hidden sm:inline">会话历史</span>
        </Button>
      }
      headerActions={
        <Button variant="outline" onClick={newSession} className="gap-1.5">
          <Plus className="size-4" />
          开启新对话
        </Button>
      }
    >
      <div className="flex min-h-0 flex-1 flex-col">
        <ChatArea />
      </div>
    </PlatformShell>
  );
}

import { useEffect } from 'react';
import { PlatformShell } from '@/components/platform-shell';
import ChatArea from '@/components/ChatArea';
import { useChatStore } from '@/stores/chatStore';

export default function ChatPage() {
  const { loadSessions } = useChatStore();

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  return (
    <PlatformShell
      title="对话助手"
      description="企业知识库问答、智能客服与办公助理"
      edgeToEdge
    >
      <div className="flex min-h-0 flex-1 flex-col">
        <ChatArea />
      </div>
    </PlatformShell>
  );
}

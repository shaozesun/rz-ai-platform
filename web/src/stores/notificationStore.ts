import { create } from 'zustand';

/** 站内通知（顶部铃铛）：后台任务（对话/Agent/看板）完成时提示。仅内存，不持久化。 */
export interface AppNotification {
  id: string;
  title: string;
  body?: string;
  createdAt: number;
  /** 跳转目标：该完成所属会话；点了通知切到对话助手页打开此会话 */
  sessionId?: string;
  /** 精确到该条回复（尽力而为，重载后 id 可能变化，回退到最新助手回复） */
  messageId?: string;
  /** 通用跳转路径（如定时任务通知跳 /scheduled-tasks），优先于 sessionId */
  href?: string;
}

interface NotificationState {
  items: AppNotification[];
  unread: number;
  push: (item: AppNotification) => void;
  markAllRead: () => void;
  remove: (id: string) => void;
  clear: () => void;
}

const MAX_ITEMS = 30;

export const useNotificationStore = create<NotificationState>()((set) => ({
  items: [],
  unread: 0,
  push: (item) =>
    set((state) => ({
      items: [item, ...state.items].slice(0, MAX_ITEMS),
      unread: state.unread + 1,
    })),
  markAllRead: () => set({ unread: 0 }),
  remove: (id) =>
    set((state) => ({ items: state.items.filter((i) => i.id !== id) })),
  clear: () => set({ items: [], unread: 0 }),
}));

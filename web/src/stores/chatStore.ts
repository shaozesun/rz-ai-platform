import { create } from 'zustand';
import type { Session, Message } from '../types';
import * as chatApi from '../api/chat';
import PLATFORM_INTRO from '../data/platformIntro';

function simulateStream(text: string, onChunk: (t: string) => void, onDone: () => void): AbortController {
  const controller = new AbortController();
  const chars = [...text];
  let i = 0;
  const step = () => {
    if (controller.signal.aborted) return;
    if (i >= chars.length) { onDone(); return; }
    const batch = chars.slice(i, i + 3).join('');
    i += 3;
    onChunk(batch);
    setTimeout(step, 30);
  };
  step();
  return controller;
}

function findIntro(text: string): string | undefined {
  const trimmed = text.trim();
  if (PLATFORM_INTRO[trimmed]) return PLATFORM_INTRO[trimmed];
  // Fuzzy: check if any key is contained in the user question, or vice versa
  const keys = Object.keys(PLATFORM_INTRO);
  for (const key of keys) {
    if (trimmed.includes(key) || key.includes(trimmed)) return PLATFORM_INTRO[key];
  }
  // Keyword overlap: if >= 2 keywords from a key appear in the question
  const questionWords = new Set(trimmed.split(''));
  for (const key of keys) {
    const keyChars = [...key].filter((c) => questionWords.has(c));
    if (keyChars.length >= key.replace(/[？?]/g, '').length * 0.6) return PLATFORM_INTRO[key];
  }
  return undefined;
}

function makeTitle(text: string) {
  return text.replace(/\s+/g, ' ').trim().slice(0, 30) + (text.length > 30 ? '...' : '');
}

interface ChatState {
  sessions: Session[];
  currentSessionId: string | null;
  messages: Message[];
  isStreaming: boolean;
  streamAbort: AbortController | null;

  loadSessions: () => Promise<void>;
  createSession: () => Promise<string>;
  newSession: () => void;
  selectSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  sendMessage: (text: string, groupId?: string) => Promise<void>;
  appendChunk: (text: string) => void;
  stopStreaming: () => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  currentSessionId: null,
  messages: [],
  isStreaming: false,
  streamAbort: null,

  loadSessions: async () => {
    try {
      const remote = await chatApi.getSessions();
      const existing = get().sessions;
      // Preserve locally-updated titles that haven't been persisted yet
      const merged = remote.map((s) => {
        const old = existing.find((e) => e.session_id === s.session_id);
        if (old && old.title !== '新对话' && s.title === '新对话') {
          return { ...s, title: old.title };
        }
        return s;
      });
      set({ sessions: merged });
    } catch {
      // Silently fail - user may not have sessions yet
    }
  },

  createSession: async () => {
    const session = await chatApi.createSession();
    set((s) => ({ sessions: [session, ...s.sessions], currentSessionId: session.session_id }));
    return session.session_id;
  },

  newSession: () => {
    set({ currentSessionId: null, messages: [] });
  },

  selectSession: async (id: string) => {
    set({ currentSessionId: id, messages: [] });
    try {
      const messages = await chatApi.getMessages(id);
      set({ messages });
    } catch {
      // Messages will be empty
    }
  },

  deleteSession: async (id: string) => {
    await chatApi.deleteSession(id);
    set((s) => {
      const sessions = s.sessions.filter((ses) => ses.session_id !== id);
      const currentSessionId = s.currentSessionId === id ? null : s.currentSessionId;
      const messages = s.currentSessionId === id ? [] : s.messages;
      return { sessions, currentSessionId, messages };
    });
  },

  sendMessage: async (text: string, groupId = 'default') => {
    let sessionId = get().currentSessionId;
    if (!sessionId) {
      sessionId = await get().createSession();
    }

    // Auto-title: if session title is "新对话", use first user message as title
    const sessions = get().sessions;
    const current = sessions.find((s) => s.session_id === sessionId);
    if (current && current.title === '新对话') {
      const newTitle = makeTitle(text);
      set((s) => ({
        sessions: s.sessions.map((ses) =>
          ses.session_id === sessionId ? { ...ses, title: newTitle } : ses,
        ),
      }));
      chatApi.updateSession(sessionId, newTitle).catch(() => {});
    }

    const userMsg: Message = {
      message_id: `user-${Date.now()}`,
      session_id: sessionId,
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
    };

    const aiMsg: Message = {
      message_id: `ai-${Date.now()}`,
      session_id: sessionId,
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
    };

    set((s) => ({
      messages: [...s.messages, userMsg, aiMsg],
      isStreaming: true,
    }));

    const introAnswer = findIntro(text);
    const controller = introAnswer
      ? simulateStream(
        introAnswer,
        (chunk) => get().appendChunk(chunk),
        () => {
          set({ isStreaming: false, streamAbort: null });
          get().loadSessions();
        },
      )
      : chatApi.chatStream(
        sessionId,
        text,
        (chunk) => get().appendChunk(chunk),
        () => {
          set({ isStreaming: false, streamAbort: null });
          get().loadSessions();
        },
        (err) => {
          console.error('Stream error:', err);
          set({ isStreaming: false, streamAbort: null });
        },
        groupId,
      );

    set({ streamAbort: controller });
  },

  appendChunk: (text: string) => {
    set((s) => {
      const messages = [...s.messages];
      const last = messages[messages.length - 1];
      if (last && last.role === 'assistant') {
        messages[messages.length - 1] = { ...last, content: last.content + text };
      }
      return { messages };
    });
  },

  stopStreaming: () => {
    const { streamAbort } = get();
    if (streamAbort) {
      streamAbort.abort();
      set({ isStreaming: false, streamAbort: null });
    }
  },
}));

import { create } from 'zustand';
import type { Session, Message, ExecutionPlan, InterviewAnswers, InterviewQuestion } from '../types';
import * as chatApi from '../api/chat';
import PLATFORM_INTRO from '../data/platformIntro';
import { useAuthStore } from './authStore';
import { notifyTurnFinished } from '../lib/notifications';

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
  // 长问题是真实业务提问，不做模糊匹配，交给后端处理
  if (trimmed.length > 20) return undefined;
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

function startAgentStream(
  get: () => ChatState,
  set: (partial: Partial<ChatState>) => void,
  sessionId: string,
  text: string,
  groupId: string,
  options?: chatApi.AgentStreamOptions,
): AbortController {
  // 注入 interview 处理：收到访谈事件 → 记录待确认问题 + 记住原始消息，并把访谈
  // 段写进当前 ai 消息，ChatArea 解析渲染 InterviewPanel。
  const merged: chatApi.AgentStreamOptions = {
    ...options,
    onInterview: (questions) => {
      set({ interview: questions, pendingMessage: text });
      get().appendChunk(`\n\n<interview>${JSON.stringify(questions)}</interview>\n\n`);
    },
  };
  return chatApi.agentStream(
    sessionId,
    text,
    (chunk) => get().appendChunk(chunk),
    (name, args) => {
      get().appendChunk(`\n\n<tool-call name="${name}">${JSON.stringify(args)}</tool-call>\n\n`);
    },
    (name, result) => {
      const preview = result.length > 300 ? result.slice(0, 300) + '...' : result;
      get().appendChunk(`\n\n<tool-result name="${name}">${preview}</tool-result>\n\n`);
    },
    (name, message) => get().setToolStatus(name, message),
    () => {
      notifyTurnFinished(text);
      set({ isStreaming: false, streamAbort: null, toolStatus: {} });
      get().loadSessions();
    },
    (err) => {
      console.error('Agent stream error:', err);
      set({ isStreaming: false, streamAbort: null, toolStatus: {} });
    },
    groupId,
    merged,
  );
}

// 把上一轮 ai 消息里的 <interview> 段替换为一句状态说明（面板作答后不再保留可交互面板）
function stripInterviewTag(content: string, note: string): string {
  if (!content.includes('<interview>')) return content;
  return content.replace(/<interview>[\s\S]*?<\/interview>/g, `\n\n${note}\n\n`);
}

// 访谈作答后的第二轮：不重复追加 user 消息，复用 pendingMessage 重发 → 计划轮/对话轮
function resendInterview(
  get: () => ChatState,
  set: (partial: Partial<ChatState>) => void,
  partial: {
    interactionMode?: string;
    interviewAnswers?: InterviewAnswers | null;
    interviewAction?: string | null;
  },
  note: string,
): void {
  const { currentSessionId, pendingMessage } = get();
  if (!currentSessionId || !pendingMessage || get().isStreaming) return;

  const aiMsg: Message = {
    message_id: `ai-${Date.now()}`,
    session_id: currentSessionId,
    role: 'assistant',
    content: '',
    created_at: new Date().toISOString(),
  };
  const messages = [...get().messages];
  const last = messages[messages.length - 1];
  if (last && last.role === 'assistant' && last.content.includes('<interview>')) {
    messages[messages.length - 1] = { ...last, content: stripInterviewTag(last.content, note) };
  }
  messages.push(aiMsg);
  set({ messages, isStreaming: true, interview: [], pendingMessage: null });

  const controller = startAgentStream(get, set, currentSessionId, pendingMessage, 'default', {
    interactionMode: 'plan',
    ...partial,
    onPlan: (plan) => get().appendChunk(`<plan>${JSON.stringify(plan)}</plan>`),
  });
  set({ streamAbort: controller });
}

export type TaskMode = 'normal' | 'full_analysis' | 'complex';
export type InteractionMode = 'plan' | 'trust';

/** 右侧 artifact 面板展示的产物（当前仅 HTML 看板） */
export interface ArtifactItem {
  url: string;
  name: string;
}

// 产物下载 URL 权威前缀（后端 tools/analysis_tools.py 生成）
const ARTIFACT_URL_PREFIX = '/api/v1/chat/agent/artifacts/';

// LLM 有时会编造 <file url>（加域名/假签名等），这里把它还原成权威相对路径。
// 幂等：已是权威前缀则原样返回；还原不了（路径里缺 dataset_id）则原样，交给显式失败。
export function normalizeArtifactUrl(
  url: string,
  sessionId: string | null | undefined,
): string {
  if (!url || url.startsWith(ARTIFACT_URL_PREFIX)) return url;
  // 剥离协议/主机/查询串（LLM 编造的 url 可能带 ?X-Amz-... 签名），再取路径尾部
  const pathOnly = url.replace(/^https?:\/\/[^/]+/, '').split(/[?#]/)[0];
  const m = /\/([A-Za-z0-9_.-]+)\/([^/?#]+)$/.exec(pathOnly);
  if (m && sessionId) return `${ARTIFACT_URL_PREFIX}rz-agent-${sessionId}/${m[1]}/${m[2]}`;
  return url;
}

// 从 assistant 消息里收集 <file name=...html> 产物（按出现顺序去重）
const FILE_TAG_RE = /<file\b([^>]*)\/>/g;
export function collectHtmlArtifacts(
  messages: Message[],
  sessionId?: string | null,
): ArtifactItem[] {
  const seen = new Set<string>();
  const items: ArtifactItem[] = [];
  for (const msg of messages) {
    if (msg.role !== 'assistant') continue;
    let m: RegExpExecArray | null;
    FILE_TAG_RE.lastIndex = 0;
    while ((m = FILE_TAG_RE.exec(msg.content || '')) !== null) {
      const attrs = m[1] || '';
      const url = /url="([^"]*)"/.exec(attrs)?.[1] || '';
      const name = /name="([^"]*)"/.exec(attrs)?.[1] || '';
      if (!url || !name.toLowerCase().endsWith('.html')) continue;
      const safeUrl = normalizeArtifactUrl(url, sessionId);
      if (seen.has(safeUrl)) continue;
      seen.add(safeUrl);
      items.push({ url: safeUrl, name });
    }
  }
  return items;
}

interface ChatState {
  sessions: Session[];
  currentSessionId: string | null;
  messages: Message[];
  isStreaming: boolean;
  streamAbort: AbortController | null;
  toolStatus: Record<string, string>;
  taskMode: TaskMode;
  interactionMode: InteractionMode;
  artifacts: ArtifactItem[];
  activeArtifactUrl: string | null;
  panelOpen: boolean;
  interview: InterviewQuestion[];
  pendingMessage: string | null;
  sessionPanelOpen: boolean;

  loadSessions: () => Promise<void>;
  createSession: () => Promise<string>;
  newSession: () => void;
  selectSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  sendMessage: (text: string, groupId?: string) => Promise<void>;
  confirmPlan: (plan: ExecutionPlan) => Promise<void>;
  submitInterviewAnswers: (answers: InterviewAnswers) => Promise<void>;
  skipInterview: () => Promise<void>;
  chatAboutThis: () => Promise<void>;
  clearInterview: () => void;
  appendChunk: (text: string) => void;
  setToolStatus: (name: string, message: string) => void;
  clearToolStatus: () => void;
  setTaskMode: (mode: TaskMode) => void;
  setInteractionMode: (mode: InteractionMode) => void;
  pushArtifact: (url: string, name: string) => void;
  selectArtifact: (url: string) => void;
  closePanel: () => void;
  openSessionPanel: () => void;
  closeSessionPanel: () => void;
  stopStreaming: () => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  currentSessionId: null,
  messages: [],
  isStreaming: false,
  streamAbort: null,
  toolStatus: {},
  taskMode: 'normal',
  interactionMode: 'trust',
  artifacts: [],
  activeArtifactUrl: null,
  panelOpen: false,
  interview: [],
  pendingMessage: null,
  sessionPanelOpen: false,

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
    get().stopStreaming(); // 中止进行中的流，避免旧流 chunk 写进新会话
    set({ currentSessionId: null, messages: [], artifacts: [], activeArtifactUrl: null, panelOpen: false, interview: [], pendingMessage: null });
  },

  selectSession: async (id: string) => {
    get().stopStreaming(); // 同上：切换会话前先停流，防跨会话串扰
    set({ currentSessionId: id, messages: [], artifacts: [], activeArtifactUrl: null, interview: [], pendingMessage: null });
    try {
      const messages = await chatApi.getMessages(id);
      const artifacts = collectHtmlArtifacts(messages, id);
      set({
        messages,
        artifacts,
        activeArtifactUrl: artifacts[0]?.url ?? null,
      });
    } catch {
      // Messages will be empty
    }
  },

  deleteSession: async (id: string) => {
    get().stopStreaming();
    await chatApi.deleteSession(id);
    set((s) => {
      const isCurrent = s.currentSessionId === id;
      return {
        sessions: s.sessions.filter((ses) => ses.session_id !== id),
        currentSessionId: isCurrent ? null : s.currentSessionId,
        messages: isCurrent ? [] : s.messages,
        artifacts: isCurrent ? [] : s.artifacts,
        activeArtifactUrl: isCurrent ? null : s.activeArtifactUrl,
        panelOpen: isCurrent ? false : s.panelOpen,
        interview: isCurrent ? [] : s.interview,
        pendingMessage: isCurrent ? null : s.pendingMessage,
      };
    });
  },

  sendMessage: async (text: string, groupId = 'default') => {
    if (get().isStreaming) return; // 防双击竞态重复发送、覆盖 streamAbort
    // 新消息开始时作废上一轮未作答的访谈（替换掉 <interview> 段，避免残留可交互面板）
    const s0 = get();
    if (s0.interview.length) {
      const msgs = [...s0.messages];
      const last = msgs[msgs.length - 1];
      if (last && last.role === 'assistant' && last.content.includes('<interview>')) {
        msgs[msgs.length - 1] = { ...last, content: stripInterviewTag(last.content, '（已放弃访谈）') };
      }
      set({ messages: msgs, interview: [], pendingMessage: null });
    }
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
    const isAgent = useAuthStore.getState().agentEnabled;

    const controller = introAnswer
      ? simulateStream(
        introAnswer,
        (chunk) => get().appendChunk(chunk),
        () => {
          set({ isStreaming: false, streamAbort: null });
          get().loadSessions();
        },
      )
      : isAgent
        ? startAgentStream(get, set, sessionId, text, groupId, {
          interactionMode: get().interactionMode,
          onPlan: (plan) => get().appendChunk(`<plan>${JSON.stringify(plan)}</plan>`),
        })
        : chatApi.chatStream(
        sessionId,
        text,
        (chunk) => get().appendChunk(chunk),
        () => {
          notifyTurnFinished(text);
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

  confirmPlan: async (plan: ExecutionPlan) => {
    const sessionId = get().currentSessionId;
    if (!sessionId || get().isStreaming) return;

    const userMsg: Message = {
      message_id: `user-${Date.now()}`,
      session_id: sessionId,
      role: 'user',
      content: '确认执行计划',
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

    const controller = startAgentStream(get, set, sessionId, '确认执行计划', 'default', {
      interactionMode: 'plan',
      planConfirmed: true,
      plan,
    });
    set({ streamAbort: controller });
  },

  submitInterviewAnswers: async (answers: InterviewAnswers) => {
    if (get().isStreaming) return;
    resendInterview(
      get, set,
      { interactionMode: 'plan', interviewAnswers: answers, interviewAction: 'answer' },
      '（需求已确认，正在生成计划…）',
    );
  },

  skipInterview: async () => {
    if (get().isStreaming) return;
    resendInterview(
      get, set,
      { interactionMode: 'plan', interviewAction: 'skip' },
      '（已跳过提问，正在生成计划…）',
    );
  },

  chatAboutThis: async () => {
    if (get().isStreaming) return;
    resendInterview(
      get, set,
      { interactionMode: 'plan', interviewAction: 'chat' },
      '（转为直接对话）',
    );
  },

  clearInterview: () => set({ interview: [], pendingMessage: null }),

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

  setToolStatus: (name, message) => {
    set((s) => ({ toolStatus: { ...s.toolStatus, [name]: message } }));
  },

  clearToolStatus: () => set({ toolStatus: {} }),

  setTaskMode: (mode) => set({ taskMode: mode }),
  setInteractionMode: (mode) => set({ interactionMode: mode }),

  pushArtifact: (url, name) => {
    set((s) => {
      // 已存在（历史会话预填/流式重推）时不再重复入列，但仍切到该看板并打开面板——
      // 否则历史记录里点 FileCard 的「在看板中查看」会因短路而打不开面板（原 bug）。
      const exists = s.artifacts.some((a) => a.url === url);
      return {
        artifacts: exists ? s.artifacts : [{ url, name }, ...s.artifacts],
        activeArtifactUrl: url,
        panelOpen: true,
      };
    });
  },

  selectArtifact: (url) => set({ activeArtifactUrl: url, panelOpen: true }),

  closePanel: () => set({ panelOpen: false }),

  openSessionPanel: () => set({ sessionPanelOpen: true }),
  closeSessionPanel: () => set({ sessionPanelOpen: false }),

  stopStreaming: () => {
    const { streamAbort } = get();
    if (streamAbort) {
      streamAbort.abort();
      set({ isStreaming: false, streamAbort: null });
    }
  },
}));

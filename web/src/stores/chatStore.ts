import { create } from 'zustand';
import type { Session, Message, ExecutionPlan, InterviewAnswers, InterviewQuestion } from '../types';
import * as chatApi from '../api/chat';
import PLATFORM_INTRO from '../data/platformIntro';
import { useAuthStore } from './authStore';
import { notifyTurnFinished } from '../lib/notifications';

// 会话切换竞态守卫：序列号 + AbortController，晚到的旧会话消息不覆盖当前会话
let sessionLoadSeq = 0;
let sessionLoadAbort: AbortController | null = null;
// 登录账号代次：切换账号（resetForUser）后自增，丢弃上一账号迟到的 loadSessions 结果
let chatEpoch = 0;

/** 把流式错误转成用户可读的友好文案（原始错误只进 console，不写进气泡当回复） */
function friendlyStreamError(err: unknown): string {
  const e = err as { message?: string; code?: string };
  const msg = e.message || '';
  const m = /^HTTP\s+(\d+)/.exec(msg);
  if (m) {
    const status = Number(m[1]);
    if (status === 401) return '登录已过期，请刷新页面后重试';
    if (status >= 500) return '服务暂时不可用，请稍后重试';
    return `请求失败（${status}），请重试`;
  }
  if (e.code === 'ERR_NETWORK' || /network|failed to fetch/i.test(msg)) {
    return '网络连接中断，请检查网络后重试';
  }
  if (/timeout/i.test(msg)) return '请求超时，请重试';
  return '生成失败，请重试';
}

/** 向某会话的流式消息缓冲追加文本：始终写 streamMessages[sid]，当前会话时同步到 messages 供渲染 */
function appendToSession(sessionId: string, text: string): void {
  const s = useChatStore.getState();
  const buf = [...(s.streamMessages[sessionId] ?? [])];
  const last = buf[buf.length - 1];
  if (last && last.role === 'assistant') {
    buf[buf.length - 1] = { ...last, content: last.content + text };
  }
  const patch: Partial<ChatState> = { streamMessages: { ...s.streamMessages, [sessionId]: buf } };
  if (s.currentSessionId === sessionId) patch.messages = buf;
  useChatStore.setState(patch);
}

/** 取某会话缓冲里最后一条助手消息的 message_id（通知跳转定位用；无助手消息返回 undefined） */
function lastAssistantMessageId(sessionId: string): string | undefined {
  const s = useChatStore.getState();
  const buf = s.streamMessages[sessionId] ?? [];
  for (let i = buf.length - 1; i >= 0; i--) {
    if (buf[i].role === 'assistant') return buf[i].message_id;
  }
  return undefined;
}

/** 结束某会话的流（正常完成）：移除活动流，保留已生成的流缓冲（供 selectSession 恢复展示） */
function finishStream(sessionId: string): void {
  const s = useChatStore.getState();
  const { [sessionId]: _abort, ...restStreams } = s.streams;
  const patch: Partial<ChatState> = { streams: restStreams, toolStatus: {} };
  if (s.currentSessionId === sessionId) patch.isStreaming = false;
  useChatStore.setState(patch);
}

/** 流式错误统一处理：在目标会话最后一条 assistant 消息上标本地错误态（不落库），并移除流状态 */
function handleStreamError(sessionId: string, err: Error): void {
  console.error('Stream error:', err);
  const friendly = friendlyStreamError(err);
  const s = useChatStore.getState();
  const next = [...(s.streamMessages[sessionId] ?? s.messages)];
  const last = next[next.length - 1];
  if (last && last.role === 'assistant') {
    next[next.length - 1] = { ...last, streamError: friendly };
  }
  const { [sessionId]: _abort, ...restStreams } = s.streams;
  const patch: Partial<ChatState> = {
    streams: restStreams,
    streamMessages: { ...s.streamMessages, [sessionId]: next },
    toolStatus: {},
  };
  if (s.currentSessionId === sessionId) {
    patch.messages = next;
    patch.isStreaming = false;
  }
  useChatStore.setState(patch);
}

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
      // 只在用户正看着本会话时才弹交互面板；后台流（切走的会话）只落缓冲不弹窗
      if (get().currentSessionId === sessionId) {
        set({ interview: questions, pendingMessage: text });
      }
      appendToSession(sessionId, `\n\n<interview>${JSON.stringify(questions)}</interview>\n\n`);
    },
  };
  return chatApi.agentStream(
    sessionId,
    text,
    (chunk) => appendToSession(sessionId, chunk),
    (name, args) => {
      appendToSession(sessionId, `\n\n<tool-call name="${name}">${JSON.stringify(args)}</tool-call>\n\n`);
    },
    (name, result) => {
      const preview = result.length > 300 ? result.slice(0, 300) + '...' : result;
      appendToSession(sessionId, `\n\n<tool-result name="${name}">${preview}</tool-result>\n\n`);
    },
    (name, message) => get().setToolStatus(name, message),
    () => {
      notifyTurnFinished(text, { sessionId, messageId: lastAssistantMessageId(sessionId) });
      finishStream(sessionId);
      get().loadSessions();
    },
    (err) => handleStreamError(sessionId, err),
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
  if (!currentSessionId || !pendingMessage || get().streams[currentSessionId]) return;

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
  set({
    messages,
    streamMessages: { ...get().streamMessages, [currentSessionId]: messages },
    isStreaming: true,
    interview: [],
    pendingMessage: null,
  });

  const controller = startAgentStream(get, set, currentSessionId, pendingMessage, 'default', {
    interactionMode: 'plan',
    ...partial,
    onPlan: (plan) => appendToSession(currentSessionId, `<plan>${JSON.stringify(plan)}</plan>`),
  });
  set({
    streams: { ...get().streams, [currentSessionId]: controller },
    isStreaming: get().currentSessionId === currentSessionId,
  });
}

export type TaskMode = 'normal' | 'full_analysis' | 'complex';
export type InteractionMode = 'plan' | 'trust';

export type ArtifactType = 'html' | 'records';

/** 右侧 artifact 面板展示的产物（html 看板 / records 数据集表格） */
export interface ArtifactItem {
  url: string;
  name: string;
  type: ArtifactType;
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

/** 判断产物 URL 是否结构合法：/chat/agent/artifacts/{thread}/{dataset}/…，且 dataset 不是 'records'。
 * LLM 常编造形如 .../artifacts/records/mgmt__query_change_ticket 的假 url（把 records 当目录），
 * 结构不对就不渲染文件卡/不收集成产物，避免出现只有「下载」的假卡。 */
export function isValidArtifactUrl(url: string): boolean {
  const m = /^\/api\/v1\/chat\/agent\/artifacts\/([^/]+)\/([^/]+)\/(.+)$/.exec(url);
  if (!m) return false;
  const [, thread, ds, tail] = m;
  if (!thread || !ds || ds === 'records' || !tail) return false;
  return true;
}

/** 按 url 判定产物类型：/records 数据集 → records；其余默认 html */
function artifactTypeOf(url: string): ArtifactType {
  if (/\/records(?:\?|$)/.test(url)) return 'records';
  return 'html';
}

// 从 assistant 消息里收集右侧面板可预览产物（.html 看板 + /records 数据集，按出现顺序去重）
const FILE_TAG_RE = /<file\b([^>]*)\/>/g;
export function collectArtifacts(
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
      const safeUrl = normalizeArtifactUrl(url, sessionId);
      // 仅收集面板可预览类型（.html / /records）；pdf/pptx 等走 FileCard 下载
      if (!safeUrl.endsWith('.html') && !/\/records$/.test(safeUrl)) continue;
      if (seen.has(safeUrl)) continue;
      seen.add(safeUrl);
      items.push({ url: safeUrl, name, type: artifactTypeOf(safeUrl) });
    }
  }
  return items;
}

export type PendingUploadStatus = 'uploading' | 'ready' | 'error';

export interface PendingUpload {
  key: string; // 前端本地临时 key（文件加入时生成），非后端 filename
  displayName: string; // 用户选择时的原始文件名（用于卡片展示）
  status: PendingUploadStatus;
  filename?: string; // 上传成功后后端返回的真实 filename（可能因重名被改写）
  error?: string;
}

interface ChatState {
  sessions: Session[];
  currentSessionId: string | null;
  messages: Message[];
  isStreaming: boolean;
  /** 历史会话消息加载中（selectSession 拉取后端时显示加载态，避免空白闪现） */
  isLoadingMessages: boolean;
  /** 活动流：session_id → AbortController（跨会话并发，切走不停流） */
  streams: Record<string, AbortController>;
  /** 活动流会话的内存消息缓冲：切走后仍持续更新，切回时直接恢复展示（不重载） */
  streamMessages: Record<string, Message[]>;
  toolStatus: Record<string, string>;
  taskMode: TaskMode;
  interactionMode: InteractionMode;
  artifacts: ArtifactItem[];
  activeArtifactUrl: string | null;
  panelOpen: boolean;
  interview: InterviewQuestion[];
  pendingMessage: string | null;
  sessionPanelOpen: boolean;
  pendingUploads: PendingUpload[];
  /** 铃铛通知跳转待办：切到对应会话后由 ChatArea 滚动到目标回答并清除 */
  pendingJump: { sessionId: string; messageId?: string } | null;

  loadSessions: () => Promise<void>;
  createSession: () => Promise<string>;
  newSession: () => void;
  selectSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  pinSession: (id: string, pinned: boolean) => Promise<void>;
  renameSession: (id: string, title: string) => void;
  deleteMessage: (sessionId: string, messageId: string) => Promise<void>;
  retryFailed: () => Promise<void>;
  sendMessage: (text: string, groupId?: string) => Promise<void>;
  confirmPlan: (plan: ExecutionPlan) => Promise<void>;
  submitInterviewAnswers: (answers: InterviewAnswers) => Promise<void>;
  skipInterview: () => Promise<void>;
  chatAboutThis: () => Promise<void>;
  clearInterview: () => void;
  setToolStatus: (name: string, message: string) => void;
  clearToolStatus: () => void;
  /** 切换登录账号时全量复位：清空上个账号的会话/消息/活动流 */
  resetForUser: () => void;
  setTaskMode: (mode: TaskMode) => void;
  setInteractionMode: (mode: InteractionMode) => void;
  pushArtifact: (url: string, name: string, open?: boolean) => void;
  selectArtifact: (url: string) => void;
  closePanel: () => void;
  openSessionPanel: () => void;
  closeSessionPanel: () => void;
  jumpToAnswer: (sessionId: string, messageId?: string) => Promise<void>;
  clearPendingJump: () => void;
  stopStreaming: () => void;
  addPendingUploadFiles: (files: File[]) => Promise<void>;
  removePendingUpload: (key: string) => void;
}

export const useChatStore = create<ChatState>((set, get) => ({
  sessions: [],
  currentSessionId: null,
  messages: [],
  isStreaming: false,
  isLoadingMessages: false,
  streams: {},
  streamMessages: {},
  toolStatus: {},
  taskMode: 'normal',
  interactionMode: 'trust',
  artifacts: [],
  activeArtifactUrl: null,
  panelOpen: false,
  interview: [],
  pendingMessage: null,
  sessionPanelOpen: false,
  pendingUploads: [],
  pendingJump: null,

  loadSessions: async () => {
    const epoch = chatEpoch;
    try {
      const remote = await chatApi.getSessions();
      if (epoch !== chatEpoch) return; // 期间切换过账号，丢弃上个账号迟到的结果
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
    // 不中断任何后台流：切到空白视图即可，旧流 chunk 写进其所属会话的 streamMessages，不污染新视图
    set({ currentSessionId: null, messages: [], isStreaming: false, isLoadingMessages: false, artifacts: [], activeArtifactUrl: null, panelOpen: false, interview: [], pendingMessage: null });
  },

  resetForUser: () => {
    // 切换登录账号：中止上个账号遗留的活动流，再全量清空其会话/消息，避免串号
    chatEpoch += 1; // 使上账号迟到的 loadSessions 结果作废
    sessionLoadSeq += 1; // 作废在途的 selectSession 历史消息加载
    if (sessionLoadAbort) sessionLoadAbort.abort();
    Object.values(get().streams).forEach((c) => c.abort());
    set({
      sessions: [],
      currentSessionId: null,
      messages: [],
      isStreaming: false,
      isLoadingMessages: false,
      streams: {},
      streamMessages: {},
      toolStatus: {},
      artifacts: [],
      activeArtifactUrl: null,
      panelOpen: false,
      interview: [],
      pendingMessage: null,
      sessionPanelOpen: false,
      pendingUploads: [],
      pendingJump: null,
    });
  },

  selectSession: async (id: string) => {
    // 不中断任何后台流：切换会话只换视图，旧流 chunk 写进 streamMessages[旧会话]
    const s0 = get();
    const stashed = s0.streamMessages[id];
    const stillStreaming = !!s0.streams[id];
    // 竞态守卫：记录本次加载的序列号 + 中止上一个未返回的请求，晚到的旧会话响应直接丢弃
    const seq = ++sessionLoadSeq;
    if (sessionLoadAbort) sessionLoadAbort.abort();
    const abort = new AbortController();
    sessionLoadAbort = abort;
    set({ currentSessionId: id, isStreaming: stillStreaming });
    if (stashed) {
      // 该会话有活动流（或刚完成的流）的内存缓冲：直接恢复展示，不重载，避免覆盖实时内容
      const artifacts = collectArtifacts(stashed, id);
      set({ messages: stashed, artifacts, activeArtifactUrl: artifacts[0]?.url ?? null, interview: [], pendingMessage: null });
      // 流已结束 → 缓冲可释放；仍活动 → 保留供 appendToSession 继续写
      if (!stillStreaming) {
        const { [id]: _drop, ...rest } = get().streamMessages;
        set({ streamMessages: rest });
      }
      return;
    }
    // 无内存缓冲：正常从后端加载。不先清空消息（避免空白闪现 + 旧图表 unmount），
    // 置加载态，加载完成整体替换。
    set({ isLoadingMessages: true, artifacts: [], activeArtifactUrl: null, interview: [], pendingMessage: null });
    try {
      const messages = await chatApi.getMessages(id, abort.signal);
      if (seq !== sessionLoadSeq) return; // 已切换到别的会话，过期响应丢弃
      const artifacts = collectArtifacts(messages, id);
      set({ messages, artifacts, activeArtifactUrl: artifacts[0]?.url ?? null, isLoadingMessages: false });
    } catch {
      // 被 abort（切换走了）或加载失败：复位加载态
      if (seq === sessionLoadSeq) set({ isLoadingMessages: false });
    }
  },

  pinSession: async (id: string, pinned: boolean) => {
    // 乐观更新本地 + 置顶优先排序（组内保持原序，sort 稳定），失败回退重载
    set((s) => {
      const sessions = s.sessions
        .map((ses) => (ses.session_id === id ? { ...ses, pinned } : ses))
        .sort((a, b) => Number(b.pinned) - Number(a.pinned));
      return { sessions };
    });
    chatApi.updateSession(id, { pinned }).catch(() => get().loadSessions());
  },

  renameSession: (id: string, title: string) => {
    // 乐观更新本地标题 + 持久化，失败回退重载
    set((s) => ({
      sessions: s.sessions.map((ses) =>
        ses.session_id === id ? { ...ses, title } : ses,
      ),
    }));
    chatApi.updateSession(id, { title }).catch(() => get().loadSessions());
  },

  deleteSession: async (id: string) => {
    // 只停被删除会话自己的流（其它后台流不受影响）
    const controller = get().streams[id];
    if (controller) controller.abort();
    await chatApi.deleteSession(id);
    set((s) => {
      const isCurrent = s.currentSessionId === id;
      const { [id]: _stream, ...restStreams } = s.streams;
      const { [id]: _buf, ...restBufs } = s.streamMessages;
      return {
        streams: restStreams,
        streamMessages: restBufs,
        sessions: s.sessions.filter((ses) => ses.session_id !== id),
        currentSessionId: isCurrent ? null : s.currentSessionId,
        messages: isCurrent ? [] : s.messages,
        isStreaming: isCurrent ? false : s.isStreaming,
        artifacts: isCurrent ? [] : s.artifacts,
        activeArtifactUrl: isCurrent ? null : s.activeArtifactUrl,
        panelOpen: isCurrent ? false : s.panelOpen,
        interview: isCurrent ? [] : s.interview,
        pendingMessage: isCurrent ? null : s.pendingMessage,
      };
    });
  },

  deleteMessage: async (sessionId: string, messageId: string) => {
    try {
      await chatApi.deleteMessage(sessionId, messageId);
    } catch {
      // 后端删除失败也先从视图移除（乐观删除），保持前端一致
    }
    set((s) => {
      const messages = s.messages.filter((m) => m.message_id !== messageId);
      const patch: Partial<ChatState> = { messages };
      // 若目标会话有流式缓冲（当前正在流式/后台流），同步删，避免缓冲与视图不一致
      const buf = s.streamMessages[sessionId];
      if (buf) {
        patch.streamMessages = { ...s.streamMessages, [sessionId]: buf.filter((m) => m.message_id !== messageId) };
      }
      return patch;
    });
  },

  retryFailed: async () => {
    const state = get();
    if (state.isStreaming) return;
    const msgs = state.messages;
    const idx = [...msgs].reverse().findIndex((m) => m.role === 'assistant' && m.streamError);
    if (idx === -1) return;
    const failedIdx = msgs.length - 1 - idx;
    // 找到它前面的 user 消息；连同失败气泡一起移除，让 sendMessage 重新追加 user + 新气泡
    for (let i = failedIdx - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') {
        const text = msgs[i].content;
        set({ messages: msgs.slice(0, i) });
        await get().sendMessage(text);
        return;
      }
    }
  },

  sendMessage: async (text: string, groupId = 'default') => {
    const curSid = get().currentSessionId;
    if (curSid && get().streams[curSid]) return; // 当前会话已在生成，防双击竞态；其它会话后台流不受影响
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
    // 先取本轮已上传文件（仅取上传成功、拿到后端 filename 的），随消息一起清空预览卡片
    const readyUploads = get().pendingUploads.filter((u) => u.status === 'ready' && u.filename);
    const uploadedFilenames = readyUploads.map((u) => u.filename!);

    // 先立即把用户消息与空 AI 气泡加进列表（不等会话创建/网络/token 刷新），
    // 避免输入后页面长时间无反应、连自己的问题都不显示
    const currentSessionId = get().currentSessionId;
    const userMsg: Message = {
      message_id: `user-${Date.now()}`,
      session_id: currentSessionId || '',
      role: 'user',
      content: text,
      created_at: new Date().toISOString(),
      uploaded_filenames: uploadedFilenames.length ? uploadedFilenames : undefined,
    };
    const aiMsg: Message = {
      message_id: `ai-${Date.now()}`,
      session_id: currentSessionId || '',
      role: 'assistant',
      content: '',
      created_at: new Date().toISOString(),
    };
    set((s) => ({
      messages: [...s.messages, userMsg, aiMsg],
      isStreaming: true,
      pendingUploads: [],
    }));

    // 会话创建（新建时会话是网络调用，可能较慢）；完成后回填消息 session_id
    let sessionId = currentSessionId;
    if (!sessionId) {
      try {
        const newSessionId = await get().createSession();
        sessionId = newSessionId;
        set((s) => ({
          messages: s.messages.map((m) => (m.session_id === '' ? { ...m, session_id: newSessionId } : m)),
        }));
      } catch {
        const messages = [...get().messages];
        const last = messages[messages.length - 1];
        if (last && last.role === 'assistant') {
          messages[messages.length - 1] = { ...last, streamError: '创建会话失败，请重试' };
        }
        set({ messages, isStreaming: false });
        return;
      }
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
      chatApi.updateSession(sessionId, { title: newTitle }).catch(() => {});
    }

    // 快照本会话流缓冲：此后 chunk 写进 streamMessages[sessionId]，切走后仍持续累积
    set((s) => ({ streamMessages: { ...s.streamMessages, [sessionId]: s.messages } }));

    const introAnswer = findIntro(text);
    const isAgent = useAuthStore.getState().agentEnabled;

    const controller = introAnswer
      ? simulateStream(
        introAnswer,
        (chunk) => appendToSession(sessionId, chunk),
        () => {
          finishStream(sessionId);
          get().loadSessions();
        },
      )
      : isAgent
        ? startAgentStream(get, set, sessionId, text, groupId, {
          interactionMode: get().interactionMode,
          taskMode: get().taskMode,
          onPlan: (plan) => appendToSession(sessionId, `<plan>${JSON.stringify(plan)}</plan>`),
          uploadedFilenames: uploadedFilenames.length ? uploadedFilenames : null,
        })
        : chatApi.chatStream(
        sessionId,
        text,
        (chunk) => appendToSession(sessionId, chunk),
        () => {
          notifyTurnFinished(text, { sessionId, messageId: lastAssistantMessageId(sessionId) });
          finishStream(sessionId);
          get().loadSessions();
        },
        (err) => handleStreamError(sessionId, err),
        groupId,
      );

    set((s) => ({
      streams: { ...s.streams, [sessionId]: controller },
      isStreaming: s.currentSessionId === sessionId,
    }));
  },

  confirmPlan: async (plan: ExecutionPlan) => {
    const sessionId = get().currentSessionId;
    if (!sessionId || get().streams[sessionId]) return;

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

    set((s) => {
      const msgs = [...s.messages, userMsg, aiMsg];
      return {
        messages: msgs,
        streamMessages: { ...s.streamMessages, [sessionId]: msgs },
        isStreaming: true,
      };
    });

    const controller = startAgentStream(get, set, sessionId, '确认执行计划', 'default', {
      interactionMode: 'plan',
      planConfirmed: true,
      plan,
    });
    set((s) => ({
      streams: { ...s.streams, [sessionId]: controller },
      isStreaming: s.currentSessionId === sessionId,
    }));
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

  setToolStatus: (name, message) => {
    set((s) => ({ toolStatus: { ...s.toolStatus, [name]: message } }));
  },

  clearToolStatus: () => set({ toolStatus: {} }),

  setTaskMode: (mode) => set({ taskMode: mode }),
  setInteractionMode: (mode) => set({ interactionMode: mode }),

  pushArtifact: (url, name, open = false) => {
    set((s) => {
      // 已存在（历史会话预填/流式重推）时不再重复入列，但仍切到该看板。
      // open=true 时打开面板（历史记录里点 FileCard 的「在看板中查看」用）；
      // 流式自动收集默认不打开，避免生成看板时强制弹窗。
      const exists = s.artifacts.some((a) => a.url === url);
      return {
        artifacts: exists ? s.artifacts : [{ url, name, type: artifactTypeOf(url) }, ...s.artifacts],
        activeArtifactUrl: url,
        panelOpen: open ? true : s.panelOpen,
      };
    });
  },

  selectArtifact: (url) => set({ activeArtifactUrl: url, panelOpen: true }),

  closePanel: () => set({ panelOpen: false }),

  openSessionPanel: () => set({ sessionPanelOpen: true }),
  closeSessionPanel: () => set({ sessionPanelOpen: false }),

  jumpToAnswer: async (sessionId, messageId) => {
    // 先记待办（ChatArea 挂载后消费滚动），再确保切到目标会话；已在目标会话则只需滚动
    set({ pendingJump: { sessionId, messageId } });
    if (get().currentSessionId !== sessionId) {
      await get().selectSession(sessionId);
    }
  },
  clearPendingJump: () => set({ pendingJump: null }),

  stopStreaming: () => {
    // 只停当前会话自己的流；其它会话后台流不受影响
    const { streams, currentSessionId } = get();
    const controller = currentSessionId ? streams[currentSessionId] : null;
    if (controller) {
      controller.abort(); // 后端收到断开后 finally 落库已生成的部分内容
    }
    // agentStream 对 AbortError 不触发 onDone/onError，这里主动移除当前会话的流状态；
    // 已生成的部分内容保留在 streamMessages，切回时仍可见
    if (currentSessionId) {
      const { [currentSessionId]: _drop, ...rest } = get().streams;
      set({ streams: rest, isStreaming: false });
    }
  },

  addPendingUploadFiles: async (files: File[]) => {
    if (!files.length) return;
    let sessionId = get().currentSessionId;
    if (!sessionId) {
      sessionId = await get().createSession();
    }

    // 先以 uploading 状态插入预览卡片（本地 key，与后端 filename 无关），逐个上传
    const entries: PendingUpload[] = files.map((f) => ({
      key: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      displayName: f.name,
      status: 'uploading',
    }));
    set((s) => ({ pendingUploads: [...s.pendingUploads, ...entries] }));

    try {
      const results = await chatApi.uploadAgentFiles(sessionId, files);
      set((s) => ({
        pendingUploads: s.pendingUploads.map((u, _i) => {
          const idx = entries.findIndex((e) => e.key === u.key);
          if (idx === -1) return u;
          const result = results[idx];
          if (!result) {
            return { ...u, status: 'error', error: '上传失败' };
          }
          return { ...u, status: 'ready', filename: result.filename };
        }),
      }));
    } catch (err) {
      console.error('Upload failed:', err);
      const failedKeys = new Set(entries.map((e) => e.key));
      set((s) => ({
        pendingUploads: s.pendingUploads.map((u) =>
          failedKeys.has(u.key) ? { ...u, status: 'error', error: '上传失败，请重试' } : u,
        ),
      }));
    }
  },

  removePendingUpload: (key: string) => {
    set((s) => ({ pendingUploads: s.pendingUploads.filter((u) => u.key !== key) }));
  },
}));

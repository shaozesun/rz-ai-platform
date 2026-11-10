import { create } from 'zustand';
import type { Session, Message, Todo } from '../types';
import * as chatApi from '../api/chat';
import * as agentVideoApi from '../api/agentVideo';
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

// 内置直答（PLATFORM_INTRO）只服务"这功能怎么用"这类咨询；**祈使句一律交后端**。
// 实测踩坑：「生成视频」被 `key.includes(trimmed)` 判成 '如何生成视频？' 而截走
// （用户以为点了开关没生效），「帮我生成PPT视频」也被关键词重合度截走过。
// 判据收敛为：消息得像"提问"才走直答——带疑问词或问号；否则（祈使/陈述）交给 agent。
const QUESTION_HINTS = /(怎么|如何|支持|什么|哪些|能不能|可以吗|介绍|用处|用途|有啥|有什么)/;

function looksLikeQuestion(text: string): boolean {
  return QUESTION_HINTS.test(text) || /[?？]\s*$/.test(text);
}

function findIntro(text: string): string | undefined {
  const trimmed = text.trim();
  if (PLATFORM_INTRO[trimmed]) return PLATFORM_INTRO[trimmed];   // 精确命中（键都是疑问句）
  // 不是提问（如「生成视频」「做个讲解视频」「把这份 PPT 做成视频」）→ 交给后端，让它真去做
  if (!looksLikeQuestion(trimmed)) return undefined;
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

/** 按 message_id 覆盖或追加某会话的消息（讲解视频的**进度占位消息**靠它原地更新，
 *  不能靠"最后一条"——用户随时可能在看别的会话或在打字）。 */
function upsertSessionMessage(sessionId: string, msg: Message): void {
  const s = useChatStore.getState();
  const base = s.streamMessages[sessionId]
    ?? (s.currentSessionId === sessionId ? s.messages : []);
  const buf = [...base];
  const i = buf.findIndex((m) => m.message_id === msg.message_id);
  if (i >= 0) buf[i] = msg;
  else buf.push(msg);
  const patch: Partial<ChatState> = { streamMessages: { ...s.streamMessages, [sessionId]: buf } };
  if (s.currentSessionId === sessionId) patch.messages = buf;
  useChatStore.setState(patch);
}

/** 讲解视频的助手消息 id：同一任务重复投递时保持稳定（进度→视频卡片是同一条消息） */
function videoMessageId(taskId: string): string {
  return `ai-video-${taskId}`;
}

/** 视频卡片名：源名去后缀 + .mp4（文本任务用标题） */
function videoName(sourceName: string): string {
  const stem = (sourceName || '视频生成').replace(/\.(pptx?|PPTX?)$/, '');
  return `${stem}.mp4`;
}

function makeTitle(text: string) {
  return text.replace(/\s+/g, ' ').trim().slice(0, 30) + (text.length > 30 ? '...' : '');
}

/** 会话列表每页条数（对齐 DeerFlow 的 INFINITE_THREADS_PAGE_SIZE = 50） */
const SESSIONS_PAGE_SIZE = 50;

function startAgentStream(
  get: () => ChatState,
  set: (partial: Partial<ChatState>) => void,
  sessionId: string,
  text: string,
  groupId: string,
  options?: chatApi.AgentStreamOptions,
): AbortController {
  // 任务清单：write_todos 每次提交完整列表 → 直接覆盖本会话的清单（后端已规范化）。
  // 只存 store、不写进消息正文——清单活在输入框上方的托盘里，不占对话流。
  const merged: chatApi.AgentStreamOptions = {
    ...options,
    onUsage: (usage) => set({ contextUsage: usage }),
    onTodos: (todos) =>
      set({ sessionTodos: { ...get().sessionTodos, [sessionId]: todos } }),
    // 图表数据累积覆盖本会话的映射；`<chart group="…">` 随后从它取数
    onChartData: (chartData) =>
      set({ sessionChartData: { ...get().sessionChartData, [sessionId]: chartData } }),
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

export type TaskMode = 'normal' | 'full_analysis' | 'research';
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
// 其它不由 artifacts 前缀承载、但同样权威的产物 URL。对话窗口的讲解视频走独立路由，
// 若不放行会被下面的反查规则改写成 artifacts 路径（`/{task_id}/video` → 404）。
const AUTHORITATIVE_URL_PREFIXES = [ARTIFACT_URL_PREFIX, '/api/v1/chat/agent-video/'];

// LLM 有时会编造 <file url>（加域名/假签名等），这里把它还原成权威相对路径。
// 幂等：已是权威前缀则原样返回；还原不了（路径里缺 dataset_id）则原样，交给显式失败。
export function normalizeArtifactUrl(
  url: string,
  sessionId: string | null | undefined,
): string {
  if (!url || AUTHORITATIVE_URL_PREFIXES.some((p) => url.startsWith(p))) return url;
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
  /** 后端报告的真实会话总数（> sessions.length 说明还有下一页可翻） */
  sessionsTotal: number;
  /** 正在拉下一页会话（侧栏滚动触发的加载态，防重复请求） */
  loadingMoreSessions: boolean;
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
  /** 联网搜索开关：只放开联网工具，不注入调研规范（规范属「联网调研」任务模式） */
  webSearch: boolean;
  /** 当前会话的上下文用量（本轮真实输入 token / 窗口 / 压缩阈值），长会话提醒新开对话用 */
  contextUsage: chatApi.ContextUsage | null;
  artifacts: ArtifactItem[];
  activeArtifactUrl: string | null;
  panelOpen: boolean;
  /** 计划模式任务清单：session_id → 清单（流式实时更新，刷新后由消息里的 todos 还原） */
  sessionTodos: Record<string, Todo[]>;
  /**
   * 图表数据：session_id → `{组名: rows}`（流式实时更新，刷新后由消息里的 chart_data 还原）。
   *
   * 正文里只写 `<chart type="…" group="组名" …/>`，**数据不写在正文** ——
   * 数据由服务端从 `analysis_exec` 注入（正文写 JSON 会被引号截断、图直接崩）。
   */
  sessionChartData: Record<string, Record<string, unknown>>;
  sessionPanelOpen: boolean;
  pendingUploads: PendingUpload[];
  /** 铃铛通知跳转待办：切到对应会话后由 ChatArea 滚动到目标回答并清除 */
  pendingJump: { sessionId: string; messageId?: string } | null;
  /** 讲解视频任务的轮询定时器（task_id → timer）：任务在后台跑，前端自动盯进度并交付卡片 */
  videoPollers: Record<string, ReturnType<typeof setInterval>>;

  loadSessions: () => Promise<void>;
  /** 侧栏滚到底时追加下一页（对齐 DeerFlow 的无限滚动） */
  loadMoreSessions: () => Promise<void>;
  createSession: () => Promise<string>;
  newSession: () => void;
  selectSession: (id: string) => Promise<void>;
  deleteSession: (id: string) => Promise<void>;
  pinSession: (id: string, pinned: boolean) => Promise<void>;
  renameSession: (id: string, title: string) => void;
  deleteMessage: (sessionId: string, messageId: string) => Promise<void>;
  retryFailed: () => Promise<void>;
  sendMessage: (text: string, groupId?: string) => Promise<void>;
  setToolStatus: (name: string, message: string) => void;
  clearToolStatus: () => void;
  /** 切换登录账号时全量复位：清空上个账号的会话/消息/活动流 */
  resetForUser: () => void;
  setTaskMode: (mode: TaskMode) => void;
  setInteractionMode: (mode: InteractionMode) => void;
  setWebSearch: (on: boolean) => void;
  /** 盯住本会话的讲解视频任务：进度写进占位消息，完成后替换为视频卡片 */
  watchVideoTasks: (sessionId: string) => Promise<void>;
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
  sessionsTotal: 0,
  loadingMoreSessions: false,
  streams: {},
  streamMessages: {},
  toolStatus: {},
  taskMode: 'normal',
  interactionMode: 'trust',
  webSearch: false,
  contextUsage: null,
  artifacts: [],
  activeArtifactUrl: null,
  panelOpen: false,
  sessionTodos: {},
  sessionChartData: {},
  sessionPanelOpen: false,
  pendingUploads: [],
  pendingJump: null,
  videoPollers: {},

  loadSessions: async () => {
    const epoch = chatEpoch;
    try {
      const { sessions: remote, total } = await chatApi.getSessions(SESSIONS_PAGE_SIZE, 0);
      if (epoch !== chatEpoch) return; // 期间切换过账号，丢弃上个账号迟到的结果
      const existing = get().sessions;
      const firstPageIds = new Set(remote.map((s) => s.session_id));
      // Preserve locally-updated titles that haven't been persisted yet
      const merged = remote.map((s) => {
        const old = existing.find((e) => e.session_id === s.session_id);
        if (old && old.title !== '新对话' && s.title === '新对话') {
          return { ...s, title: old.title };
        }
        return s;
      });
      // 保留用户已经翻出来的后续页（每轮对话结束都会调 loadSessions，不能把翻出来的历史收回去）
      const tail = existing.filter((s) => !firstPageIds.has(s.session_id));
      set({
        sessions: [...merged, ...tail],
        sessionsTotal: total,
      });
    } catch {
      // Silently fail - user may not have sessions yet
    }
  },

  loadMoreSessions: async () => {
    const { sessions, sessionsTotal, loadingMoreSessions } = get();
    // 已知到头 / 正在加载 → 不重复请求（scroll 事件会高频触发）
    if (loadingMoreSessions || (sessionsTotal > 0 && sessions.length >= sessionsTotal)) return;
    // 没有更多可翻时先按"未知总数"处理：total 为 0 说明还没成功拉过列表
    if (!sessions.length && sessionsTotal === 0) return;
    set({ loadingMoreSessions: true });
    try {
      const { sessions: page, total } = await chatApi.getSessions(
        SESSIONS_PAGE_SIZE,
        sessions.length,
      );
      const seen = new Set(get().sessions.map((s) => s.session_id));
      const appended = page.filter((s) => !seen.has(s.session_id));
      set((s) => ({
        sessions: [...s.sessions, ...appended],
        sessionsTotal: total,
        loadingMoreSessions: false,
      }));
    } catch {
      set({ loadingMoreSessions: false });
    }
  },

  createSession: async () => {
    const session = await chatApi.createSession();
    set((s) => ({ sessions: [session, ...s.sessions], currentSessionId: session.session_id }));
    return session.session_id;
  },

  newSession: () => {
    // 不中断任何后台流：切到空白视图即可，旧流 chunk 写进其所属会话的 streamMessages，不污染新视图
    set({ currentSessionId: null, messages: [], isStreaming: false, isLoadingMessages: false, artifacts: [], activeArtifactUrl: null, panelOpen: false, contextUsage: null });
  },

  resetForUser: () => {
    // 切换登录账号：中止上个账号遗留的活动流，再全量清空其会话/消息，避免串号
    chatEpoch += 1; // 使上账号迟到的 loadSessions 结果作废
    sessionLoadSeq += 1; // 作废在途的 selectSession 历史消息加载
    if (sessionLoadAbort) sessionLoadAbort.abort();
    Object.values(get().streams).forEach((c) => c.abort());
    Object.values(get().videoPollers).forEach((timer) => clearInterval(timer));
    set({
      sessions: [],
      sessionsTotal: 0,
      loadingMoreSessions: false,
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
      sessionPanelOpen: false,
      pendingUploads: [],
      contextUsage: null,
      videoPollers: {},
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
      set({ messages: stashed, artifacts, activeArtifactUrl: artifacts[0]?.url ?? null });
      // 流已结束 → 缓冲可释放；仍活动 → 保留供 appendToSession 继续写
      if (!stillStreaming) {
        const { [id]: _drop, ...rest } = get().streamMessages;
        set({ streamMessages: rest });
      }
      return;
    }
    // 无内存缓冲：正常从后端加载。不先清空消息（避免空白闪现 + 旧图表 unmount），
    // 置加载态，加载完成整体替换。
    set({ isLoadingMessages: true, artifacts: [], activeArtifactUrl: null });
    try {
      const { messages, usage } = await chatApi.getMessages(id, abort.signal);
      if (seq !== sessionLoadSeq) return; // 已切换到别的会话，过期响应丢弃
      const artifacts = collectArtifacts(messages, id);
      // 计划模式任务清单：取最后一条带 todos 的 assistant 消息还原（刷新后托盘仍在）
      const lastTodos = [...messages].reverse().find((m) => m.todos?.length)?.todos;
      // 图表数据：同样从最后一条带 chart_data 的 assistant 消息还原（刷新后图仍在）
      const lastChartData = [...messages].reverse().find((m) => m.chart_data)?.chart_data;
      set((s) => ({
        messages, artifacts, activeArtifactUrl: artifacts[0]?.url ?? null,
        isLoadingMessages: false, contextUsage: usage,
        sessionChartData: lastChartData
          ? { ...s.sessionChartData, [id]: lastChartData }
          : s.sessionChartData,
        sessionTodos: lastTodos
          ? { ...s.sessionTodos, [id]: lastTodos }
          : s.sessionTodos,
      }));
      // 讲解视频任务存在服务端（对话消息不落库）：切回会话时补进度或已完成的视频卡片
      void get().watchVideoTasks(id);
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

    // 用户消息**先落库**（不等流跑完）：流被中断/取消、或压根没跑到 agent 时，
    // 至少这一轮的提问留在历史里，不会只剩一个带标题的空壳会话。
    // 失败只记日志、不阻断开流（后端 save_messages 会在流结束时补插一次，见其幂等判断）。
    try {
      await chatApi.appendUserMessage(
        sessionId,
        text,
        uploadedFilenames.length ? uploadedFilenames : null,
      );
    } catch (err) {
      console.warn('用户消息预落库失败（不影响本轮回答）', err);
    }

    // 会话标题本地乐观改一下（立刻好看）；**不再发 PATCH**——标题已由后端在落库用户
    // 消息时确定（`streaming.py::_autotitle_session`）。两个写入者会互相打架，而且
    // 前端那次是 fire-and-forget，失败就静默停在占位名「新对话」。
    const current = get().sessions.find((s) => s.session_id === sessionId);
    if (current && current.title === '新对话') {
      const newTitle = makeTitle(text);
      set((s) => ({
        sessions: s.sessions.map((ses) =>
          ses.session_id === sessionId ? { ...ses, title: newTitle } : ses,
        ),
      }));
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
          webSearch: get().webSearch,
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
          // 本轮若建了讲解视频任务，立刻开始盯进度（工具已返回、任务在后台跑）
          void get().watchVideoTasks(sessionId);
        },
        (err) => handleStreamError(sessionId, err),
        groupId,
      );

    set((s) => ({
      streams: { ...s.streams, [sessionId]: controller },
      isStreaming: s.currentSessionId === sessionId,
    }));
  },

  setToolStatus: (name, message) => {
    set((s) => ({ toolStatus: { ...s.toolStatus, [name]: message } }));
  },

  clearToolStatus: () => set({ toolStatus: {} }),

  setTaskMode: (mode) => set({ taskMode: mode }),
  setInteractionMode: (mode) => set({ interactionMode: mode }),
  setWebSearch: (on) => set({ webSearch: on }),

  watchVideoTasks: async (sessionId) => {
    // 这里是**兜底通道**：正常情况下 video_create 会等到视频生成完再返回，模型直接在回复里
    // 输出 `<file>` 卡片（对应关系明确）。只有工具等超时（"还在后台跑"）时才需要前端盯着补卡片。
    // 所以：回复里已经出现过某个任务链接的，这里**不再补投**（否则同一个视频会出现两张卡片）。
    const delivered = (taskId: string): boolean => {
      const s = useChatStore.getState();
      const msgs = s.streamMessages[sessionId]
        ?? (s.currentSessionId === sessionId ? s.messages : []);
      return msgs.some((m) => typeof m.content === 'string'
        && m.content.includes(`/agent-video/${taskId}/video`));
    };
    const render = (tasks: agentVideoApi.AgentVideoTask[]) => {
      const running: string[] = [];
      for (const t of tasks) {
        if (delivered(t.task_id)) continue;   // 模型回复里已经带过这个视频了
        const msgId = videoMessageId(t.task_id);
        const base = {
          message_id: msgId,
          session_id: sessionId,
          role: 'assistant' as const,
          created_at: t.created_at || new Date().toISOString(),
        };
        if (t.status === 'success' && t.video_url) {
          upsertSessionMessage(sessionId, {
            ...base,
            content: `<file url="${t.video_url}" name="${videoName(t.source_name)}"/>`,
          });
        } else if (t.status === 'failed') {
          upsertSessionMessage(sessionId, {
            ...base,
            content: `⚠️ 视频生成失败：${t.error || '未知错误'}`,
          });
        } else {
          upsertSessionMessage(sessionId, {
            ...base,
            content: `正在生成视频：${t.progress_text || t.status}（${t.progress}%）`,
          });
          running.push(t.task_id);
        }
      }
      return running;
    };

    // 同一会话已有定时器就只做一次立即渲染（每轮结束、切回会话都会调到这里）；
    // 定时器按 sessionId 存，多会话并发时各盯各的。
    if (get().videoPollers[sessionId]) {
      try {
        render(await agentVideoApi.listAgentVideoTasks(sessionId));
      } catch { /* 拉不到就不打扰 */ }
      return;
    }

    let tasks: agentVideoApi.AgentVideoTask[] = [];
    try {
      tasks = await agentVideoApi.listAgentVideoTasks(sessionId);
    } catch {
      return;
    }
    let running = render(tasks);
    if (running.length === 0) return;   // 没有在跑的任务：不轮询

    const timer = setInterval(async () => {
      try {
        running = render(await agentVideoApi.listAgentVideoTasks(sessionId));
        if (running.length === 0) {
          clearInterval(timer);
          set((st) => {
            const rest = { ...st.videoPollers };
            delete rest[sessionId];
            return { videoPollers: rest };
          });
        }
      } catch { /* 单次失败继续下个 tick */ }
    }, 3000);
    set((st) => ({ videoPollers: { ...st.videoPollers, [sessionId]: timer } }));
  },



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
          // 注意：后端被拒时返回的是 {filename, error}（**带 filename**），早期版本没看 error，
          // 于是"超过大小上限"的文件也显示成已上传，随后 agent 查不到文件（实测踩过）。
          if (!result || result.error) {
            return { ...u, status: 'error', error: result?.error || '上传失败' };
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

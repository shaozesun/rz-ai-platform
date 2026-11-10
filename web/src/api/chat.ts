import client, { getAccessToken, refreshAccessToken } from './client';
import type { Session, Message, Todo } from '../types';

// 流式请求用裸 fetch（SSE 需 ReadableStream，axios 浏览器端不便流式），绕过了 axios
// 的 401 拦截器。这里统一走 refreshAccessToken 复用刷新单飞队列：401 时刷新一次重试，
// 刷新失败由 refreshAccessToken 登出跳转并返回 null，原 401 交上层 onError 兜底。
async function fetchWithAuth(url: string, init: RequestInit = {}): Promise<Response> {
  const headers = { ...(init.headers || {}), Authorization: `Bearer ${getAccessToken()}` };
  let res = await fetch(url, { ...init, headers });
  if (res.status === 401) {
    const newToken = await refreshAccessToken();
    if (newToken) {
      res = await fetch(url, { ...init, headers: { ...headers, Authorization: `Bearer ${newToken}` } });
    }
  }
  return res;
}

/** 拉一页会话（后端按 pinned desc, updated_at desc 排；total 是真实总数，用于判断还有没有下一页） */
export async function getSessions(
  limit = 100,
  offset = 0,
): Promise<{ sessions: Session[]; total: number }> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  const { data } = await client.get(`/chat/sessions?${params}`);
  const list = data.sessions ?? data.data ?? [];
  const sessions = list.map((s: { session_id: string; session_name: string; created_at: string; updated_at: string; pinned?: boolean; message_count?: number }) => ({
    session_id: s.session_id,
    title: s.session_name || '新对话',
    created_at: s.created_at,
    updated_at: s.updated_at,
    message_count: s.message_count ?? 0,
    pinned: !!s.pinned,
  }));
  return { sessions, total: data.total ?? sessions.length };
}

export async function createSession(title?: string): Promise<Session> {
  const params = new URLSearchParams();
  if (title) params.set('title', title);
  const { data } = await client.post(`/chat/sessions?${params}`);
  const s = data.session ?? data;
  return {
    session_id: s.session_id,
    title: s.session_name || '新对话',
    created_at: s.created_at,
    updated_at: s.updated_at,
    message_count: 0,
    pinned: false,
  };
}

export async function deleteSession(sessionId: string): Promise<void> {
  await client.delete(`/chat/sessions/${sessionId}`);
}

export async function updateSession(
  sessionId: string,
  updates: { title?: string; pinned?: boolean },
): Promise<void> {
  await client.patch(`/chat/sessions/${sessionId}`, updates);
}

/** 会话上下文用量（打开历史会话时用；与 getMessages 同一次请求带出） */
export interface ContextUsage {
  input_tokens: number;
  window: number | null;
  compact_trigger: number | null;
}

export interface SessionMessages {
  messages: Message[];
  usage: ContextUsage | null;
}

/** 上下文用量：取最后一条带 context_tokens 的 assistant 消息（后端只在 done 时写） */
function pickUsage(
  list: { context_tokens?: number }[],
  data: { context_window?: number; compact_trigger_tokens?: number },
): ContextUsage | null {
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i]?.context_tokens) {
      return {
        input_tokens: list[i].context_tokens!,
        window: data.context_window ?? null,
        compact_trigger: data.compact_trigger_tokens ?? null,
      };
    }
  }
  return null;
}

export async function getMessages(
  sessionId: string,
  signal?: AbortSignal,
): Promise<SessionMessages> {
  const { data } = await client.get(`/chat/sessions/${sessionId}/messages`, { signal });
  const list = data.messages ?? data.data ?? [];
  const messages = list.map((m: { message_id?: string; role: string; content: string; created_at?: string; uploaded_filenames?: string[]; context_tokens?: number; todos?: Todo[] }, i: number) => ({
    message_id: m.message_id || `${sessionId}-${i}`,
    session_id: sessionId,
    role: m.role,
    content: m.content,
    created_at: m.created_at || new Date().toISOString(),
    uploaded_filenames: m.uploaded_filenames,
    context_tokens: m.context_tokens,
    todos: m.todos,
  }));
  return { messages, usage: pickUsage(list, data) };
}

export async function deleteMessage(sessionId: string, messageId: string): Promise<void> {
  await client.delete(`/chat/sessions/${sessionId}/messages/${messageId}`);
}

/** 开流前把用户消息先落库：流被取消/没跑到 agent 时，提问也留在历史里（后端幂等去重） */
export async function appendUserMessage(
  sessionId: string,
  content: string,
  uploadedFilenames: string[] | null,
): Promise<void> {
  await client.post(`/chat/sessions/${sessionId}/messages`, {
    content,
    uploaded_filenames: uploadedFilenames,
  });
}

// SSE 流式对话
export function chatStream(
  sessionId: string,
  text: string,
  onChunk: (text: string) => void,
  onDone: () => void,
  onError: (err: Error) => void,
  groupId: string = 'default',
): AbortController {
  const controller = new AbortController();

  const params = new URLSearchParams({ session_id: sessionId, group_id: groupId });
  const url = `/api/v1/chat?${params}`;

  fetchWithAuth(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body?.getReader();
      if (!reader) {
        throw new Error('No stream reader');
      }
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const payload = line.slice(6);
            if (payload === '[DONE]') {
              onDone();
              return;
            }
            try {
              const parsed = JSON.parse(payload);
              if (parsed.content) {
                onChunk(parsed.content);
              }
            } catch {
              onChunk(payload);
            }
          }
        }
      }
      onDone();
    })
    .catch((err) => {
      if (err.name !== 'AbortError') {
        onError(err);
      }
    });

  return controller;
}

export interface AgentStreamOptions {
  interactionMode?: string;
  taskMode?: string;
  /** 联网搜索开关：只放开 web__search / web__fetch，不注入调研规范 */
  webSearch?: boolean;
  /** 计划模式的任务清单更新（write_todos 每次提交完整列表） */
  onTodos?: (todos: Todo[]) => void;
  /**
   * 图表数据更新（服务端从 `analysis_exec` 注入，累积下发）。
   * 正文里的 `<chart group="组名"/>` 靠它取数 —— 模型不再在正文写 JSON。
   */
  onChartData?: (chartData: Record<string, unknown>) => void;
  uploadedFilenames?: string[] | null;
  /** 本轮结束时的上下文用量（done 事件携带），用于显示「上下文 X%」 */
  onUsage?: (usage: ContextUsage) => void;
}

export interface UploadedFileInfo {
  /** 后端被拒时只回 filename + error（文件未落盘），此时其它字段缺失 */
  error?: string | null;
  filename: string;
  size: number;
  extension: string;
  outline: string[];
  extract_error: string | null;
}

// 上传文件到 Agent 对话（会话级持久化，不跨会话共享）。返回的 filename 供下一条
// 消息的 AgentStreamOptions.uploadedFilenames 引用，触发后端 <current_uploads> 注入。
export async function uploadAgentFiles(
  sessionId: string,
  files: File[],
): Promise<UploadedFileInfo[]> {
  const form = new FormData();
  for (const file of files) {
    form.append('files', file);
  }
  const params = new URLSearchParams({ session_id: sessionId });
  const { data } = await client.post(`/chat/agent/uploads?${params}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data.files ?? [];
}

// Agent 流式对话
export function agentStream(
  sessionId: string,
  text: string,
  onChunk: (text: string) => void,
  onToolCall: (name: string, args: Record<string, unknown>) => void,
  onToolResult: (name: string, result: string) => void,
  onStatus: (tool: string, message: string) => void,
  onDone: () => void,
  onError: (err: Error) => void,
  groupId: string = 'default',
  options?: AgentStreamOptions,
): AbortController {
  const controller = new AbortController();

  const params = new URLSearchParams({ session_id: sessionId, group_id: groupId });
  const url = `/api/v1/chat/agent?${params}`;

  fetchWithAuth(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      message: text,
      interaction_mode: options?.interactionMode ?? 'trust',
      task_mode: options?.taskMode ?? 'normal',
      web_search: options?.webSearch ?? false,
      uploaded_filenames: options?.uploadedFilenames ?? null,
    }),
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const reader = res.body?.getReader();
      if (!reader) throw new Error('No stream reader');
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const payload = line.slice(6);
          try {
            const parsed = JSON.parse(payload);
            const event = parsed.event;
            const data = parsed.data || {};
            if (event === 'text') {
              onChunk(data.content || '');
            } else if (event === 'tool_call') {
              onToolCall(data.name || '', data.args || {});
            } else if (event === 'tool_result') {
              onToolResult(data.name || '', data.result || '');
              // 图表数据（累积下发）—— 不能靠 result 预览，它被截断到 2000 字符
              if (data.chart_data) options?.onChartData?.(data.chart_data as Record<string, unknown>);
            } else if (event === 'status') {
              onStatus(data.tool || '', data.message || '');
            } else if (event === 'todos') {
              options?.onTodos?.((data.todos || []) as Todo[]);
            } else if (event === 'error') {
              onChunk(`\n\n> ⚠️ ${data.message || '未知错误'}\n\n`);
            } else if (event === 'done') {
              if (data.usage) options?.onUsage?.(data.usage as ContextUsage);
              onDone();
              return;
            }
          } catch {
            // 忽略解析失败的行
          }
        }
      }
      onDone();
    })
    .catch((err) => {
      if (err.name !== 'AbortError') onError(err);
    });

  return controller;
}

// 非 RAG 纯对话
export function chatNoRagStream(
  sessionId: string,
  text: string,
  onChunk: (text: string) => void,
  onDone: () => void,
  onError: (err: Error) => void,
): AbortController {
  const controller = new AbortController();

  const params = new URLSearchParams({ session_id: sessionId });
  const url = `/api/v1/chat/no-rag?${params}`;

  fetchWithAuth(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
    signal: controller.signal,
  })
    .then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const reader = res.body?.getReader();
      if (!reader) throw new Error('No stream reader');
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const payload = line.slice(6);
            if (payload === '[DONE]') { onDone(); return; }
            try {
              const parsed = JSON.parse(payload);
              if (parsed.content) onChunk(parsed.content);
            } catch { onChunk(payload); }
          }
        }
      }
      onDone();
    })
    .catch((err) => {
      if (err.name !== 'AbortError') onError(err);
    });

  return controller;
}

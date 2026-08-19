import client, { getAccessToken, refreshAccessToken } from './client';
import type { Session, Message, ExecutionPlan, InterviewAnswers, InterviewQuestion } from '../types';

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

export async function getSessions(): Promise<Session[]> {
  const { data } = await client.get('/chat/sessions');
  const list = data.sessions ?? data.data ?? [];
  return list.map((s: { session_id: string; session_name: string; created_at: string; updated_at: string }) => ({
    session_id: s.session_id,
    title: s.session_name || '新对话',
    created_at: s.created_at,
    updated_at: s.updated_at,
    message_count: 0,
  }));
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
  };
}

export async function deleteSession(sessionId: string): Promise<void> {
  await client.delete(`/chat/sessions/${sessionId}`);
}

export async function updateSession(sessionId: string, title: string): Promise<void> {
  await client.patch(`/chat/sessions/${sessionId}`, { title });
}

export async function getMessages(sessionId: string): Promise<Message[]> {
  const { data } = await client.get(`/chat/sessions/${sessionId}/messages`);
  const list = data.messages ?? data.data ?? [];
  return list.map((m: { role: string; content: string; created_at?: string }, i: number) => ({
    message_id: `${sessionId}-${i}`,
    session_id: sessionId,
    role: m.role,
    content: m.content,
    created_at: m.created_at || new Date().toISOString(),
  }));
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
  planConfirmed?: boolean;
  plan?: ExecutionPlan | null;
  onPlan?: (plan: ExecutionPlan) => void;
  interviewAnswers?: InterviewAnswers | null;
  interviewAction?: string | null;
  onInterview?: (questions: InterviewQuestion[]) => void;
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
      plan_confirmed: options?.planConfirmed ?? false,
      plan: options?.plan ?? null,
      interview_answers: options?.interviewAnswers ?? null,
      interview_action: options?.interviewAction ?? null,
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
            } else if (event === 'status') {
              onStatus(data.tool || '', data.message || '');
            } else if (event === 'interview') {
              options?.onInterview?.(data.questions || []);
            } else if (event === 'plan') {
              options?.onPlan?.(data as ExecutionPlan);
            } else if (event === 'error') {
              onChunk(`\n\n> ⚠️ ${data.message || '未知错误'}\n\n`);
            } else if (event === 'done') {
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

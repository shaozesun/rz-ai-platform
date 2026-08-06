import client, { getAccessToken } from './client';
import type { Session, Message } from '../types';

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
  const token = getAccessToken();

  const params = new URLSearchParams({ session_id: sessionId, group_id: groupId });
  const url = `/api/v1/chat?${params}`;

  fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
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

// Agent 流式对话
export function agentStream(
  sessionId: string,
  text: string,
  onChunk: (text: string) => void,
  onToolCall: (name: string, args: Record<string, unknown>) => void,
  onToolResult: (name: string, result: string) => void,
  onDone: () => void,
  onError: (err: Error) => void,
  groupId: string = 'default',
): AbortController {
  const controller = new AbortController();
  const token = getAccessToken();

  const params = new URLSearchParams({ session_id: sessionId, group_id: groupId });
  const url = `/api/v1/chat/agent?${params}`;

  fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ message: text }),
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
  const token = getAccessToken();

  const params = new URLSearchParams({ session_id: sessionId });
  const url = `/api/v1/chat/no-rag?${params}`;

  fetch(url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${token}`,
    },
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

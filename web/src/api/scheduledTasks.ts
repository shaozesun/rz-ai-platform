import client from './client';
import type { Message } from '../types';

export interface ScheduledTask {
  task_id: string;
  name: string;
  description: string;
  schedule_type: 'once' | 'interval' | 'cron';
  once_at: string | null;
  interval_seconds: number | null;
  cron_expr: string | null;
  prompt: string;
  task_mode: string;
  thread_strategy: 'new' | 'reuse';
  enabled: boolean;
  next_run_at: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ScheduledTaskRun {
  run_id: string;
  task_id: string;
  thread_id: string;
  status: 'pending' | 'running' | 'success' | 'failed';
  triggered_at: string;
  final_reply: string;
  artifact_urls: string[];
  error: string;
  duration_ms: number;
  created_at: string;
}

export interface ScheduledTaskNotification {
  notification_id: string;
  user_id: string;
  task_id: string;
  task_name: string;
  run_id: string;
  status: 'success' | 'failed';
  title: string;
  body: string;
  created_at: string;
}

export interface ScheduledTaskInput {
  name: string;
  description?: string;
  schedule_type: 'once' | 'interval' | 'cron';
  once_at?: string | null;
  interval_seconds?: number | null;
  cron_expr?: string | null;
  prompt: string;
  task_mode?: string;
  thread_strategy?: 'new' | 'reuse';
  enabled?: boolean;
}

async function unwrap<T>(resp: { data: { data: T } }): Promise<T> {
  return resp.data.data;
}

export async function listTasks(): Promise<ScheduledTask[]> {
  const resp = await client.get('/scheduled-tasks');
  return unwrap<ScheduledTask[]>(resp);
}

export async function createTask(input: ScheduledTaskInput): Promise<ScheduledTask> {
  const resp = await client.post('/scheduled-tasks', input);
  return unwrap<ScheduledTask>(resp);
}

export async function updateTask(
  taskId: string,
  input: Partial<ScheduledTaskInput>,
): Promise<ScheduledTask> {
  const resp = await client.patch(`/scheduled-tasks/${taskId}`, input);
  return unwrap<ScheduledTask>(resp);
}

export async function deleteTask(taskId: string): Promise<void> {
  await client.delete(`/scheduled-tasks/${taskId}`);
}

export async function triggerTask(taskId: string): Promise<void> {
  await client.post(`/scheduled-tasks/${taskId}/trigger`);
}

export async function listRuns(taskId: string): Promise<ScheduledTaskRun[]> {
  const resp = await client.get(`/scheduled-tasks/${taskId}/runs`);
  return unwrap<ScheduledTaskRun[]>(resp);
}

export async function listNotifications(): Promise<ScheduledTaskNotification[]> {
  const resp = await client.get('/scheduled-tasks/notifications');
  return unwrap<ScheduledTaskNotification[]>(resp);
}

export async function getRunMessages(taskId: string, runId: string): Promise<Message[]> {
  const resp = await client.get(`/scheduled-tasks/${taskId}/runs/${runId}/messages`);
  return resp.data.messages ?? [];
}

export function artifactDownloadUrl(taskId: string, runId: string, filename: string): string {
  return `/api/v1/scheduled-tasks/${taskId}/runs/${runId}/artifacts/${filename}`;
}

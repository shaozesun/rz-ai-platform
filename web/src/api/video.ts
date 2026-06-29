import client from './client';
import type { VideoTaskParams } from '../types';

export async function createVideoTask(
  file: File,
  params: VideoTaskParams,
): Promise<{ ok: boolean; data: { task_id: string } }> {
  const form = new FormData();
  form.append('file', file);
  form.append('aspect_ratio', params.aspect_ratio);
  form.append('resolution', params.resolution);
  form.append('voice', params.voice);
  form.append('subtitle_style', params.subtitle_style);
  const { data } = await client.post('/video/tasks', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 30000,
  });
  return data;
}

export async function listVideoTasks() {
  const { data } = await client.get('/video/tasks');
  return data;
}

export async function getVideoTask(taskId: string) {
  const { data } = await client.get(`/video/tasks/${taskId}`);
  return data;
}

export async function deleteVideoTask(taskId: string) {
  const { data } = await client.delete(`/video/tasks/${taskId}`);
  return data;
}

export function getVideoUrl(taskId: string): string {
  return `/api/v1/video/tasks/${taskId}/video`;
}

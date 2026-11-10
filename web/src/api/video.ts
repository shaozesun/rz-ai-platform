import client from './client';
import type { VideoTaskParams } from '../types';

/**
 * 单文件体积上限（MB）。必须与 deploy/nginx/nginx.conf 中 location /api/ 的
 * client_max_body_size 保持一致，否则超限会在 nginx 层被 413 拦掉、后端收不到。
 */
export const MAX_UPLOAD_MB = 200;

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
  form.append('speech_language', params.speech_language);
  form.append('subtitle_language', params.subtitle_language);
  const { data } = await client.post('/video/tasks', form, {
    headers: { 'Content-Type': 'multipart/form-data' },
    // 上传耗时随文件体积增长，不能沿用全局 30s（百 MB 文件会被直接 abort）。
    // 上限是 MAX_UPLOAD_MB=200，180s 已覆盖到 1.1MB/s，对实测 3-4MB/s 有约 2 倍余量。
    timeout: 180000,
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

/**
 * 对话窗口「讲解视频」（agent_video）API。
 *
 * 与表单页视频（`api/video.ts`，走 /video/tasks）是两条独立链路：任务在 `agent_video_tasks`
 * 集合里，只服务对话场景（开关 → 模型调工具建任务 → 前端自动盯进度并交付卡片）。
 */
import client from './client';

export interface AgentVideoTask {
  task_id: string;
  status: string;
  progress: number;
  progress_text: string;
  source_name: string;
  page_count: number;
  duration: number;
  error: string;
  /** 完成后才有值：/api/v1/chat/agent-video/{task_id}/video */
  video_url: string;
  created_at?: string;
}

/** 列表直接给数组，与后端 {ok, tasks} 形状对齐 */
export async function listAgentVideoTasks(sessionId: string): Promise<AgentVideoTask[]> {
  const params = new URLSearchParams({ session_id: sessionId });
  const { data } = await client.get(`/chat/agent-video?${params}`);
  return (data.tasks ?? []) as AgentVideoTask[];
}

export async function getAgentVideoTask(taskId: string): Promise<AgentVideoTask> {
  const { data } = await client.get(`/chat/agent-video/${taskId}`);
  return data as AgentVideoTask;
}

/** 是否仍在进行中（用于判断要不要继续轮询） */
export function isRunning(status: string): boolean {
  return status !== 'success' && status !== 'failed';
}

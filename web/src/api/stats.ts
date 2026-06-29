import client from './client';

export interface OverviewData {
  api_call_count: number;
  compute_usage: number;
  user_count: number;
  session_count: number;
  video_count: number;
  group_count: number;
  risk_check_count: number;
  fire_safety_count: number;
}

export interface ActivityItem {
  type: 'video';
  text: string;
  time: string;
}

const OVERVIEW_DEFAULTS: OverviewData = {
  api_call_count: 0,
  compute_usage: 0,
  user_count: 0,
  session_count: 0,
  video_count: 0,
  group_count: 0,
  risk_check_count: 0,
  fire_safety_count: 0,
};

export interface TrendItem {
  day: string;
  date: string;
  对话: number;
  视频: number;
  隐患识别: number;
  消防配置: number;
}

export async function getOverview(): Promise<OverviewData> {
  const { data } = await client.get('/stats/overview');
  return { ...OVERVIEW_DEFAULTS, ...(data.data ?? {}) };
}

export async function getTrend(): Promise<TrendItem[]> {
  const { data } = await client.get('/stats/trend');
  return data.data ?? [];
}

export async function getActivities(limit = 8): Promise<ActivityItem[]> {
  const { data } = await client.get('/stats/activity', { params: { limit } });
  return data.activities ?? [];
}

export async function submitFeedback(content: string): Promise<boolean> {
  const { data } = await client.post('/feedback', { content });
  return data.ok === true;
}

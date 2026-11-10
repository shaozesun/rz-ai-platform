import client from './client';

/** always = 常驻可见；on_demand = 经 tool_search 按需检索；mode_gated = 仅特定任务模式可用 */
export type ToolVisibility = 'always' | 'on_demand' | 'mode_gated';

export interface ToolItem {
  /** 工具名，如 mgmt__query_my_info / analysis_exec */
  name: string;
  title: string;
  /** 后端自动截取的描述首句（≤60 字），供页面展示 */
  summary: string;
  description: string;
  category_label: string;
  domain_label: string;
  /** 调用该工具所需的额外权限（空 = 沿用门面权限） */
  required_perm: string;
  visibility: ToolVisibility;
  mode_gated: boolean;
}

export interface ToolGroup {
  key: string;
  label: string;
  description: string;
  /** 内置工具组的配置开关（常开组为空串） */
  switch?: string;
  tools: ToolItem[];
}

async function unwrap<T>(resp: { data: { data: T } }): Promise<T> {
  return resp.data.data;
}

export async function listTools(): Promise<ToolGroup[]> {
  const resp = await client.get('/tools');
  const data = await unwrap<{ groups: ToolGroup[] }>(resp);
  return data.groups ?? [];
}

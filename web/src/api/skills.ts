import client from './client';

export interface AgentSkill {
  /** 技能名（小写字母/数字/下划线），也是能力 id 的第二段 */
  name: string;
  title: string;
  description: string;
  category: string;
  domain: string;
  kind: string;
  /** 空串 = 官方内置；非空 = 自建技能，值即归属用户 id */
  owner: string;
  /** 自建技能可停用/删除；官方内置只读 */
  editable: boolean;
  enabled: boolean;
  /** 概述：description 首句 */
  overview: string;
  /** 使用场景：frontmatter 的 when_to_use，缺失时回退 description 后续句 */
  when_to_use: string;
  /** 附带文档（references/ 下的 md） */
  references: SkillReference[];
}

export interface SkillReference {
  /** references/<stem>.md 的 stem */
  type: string;
  name: string;
  description: string;
}

async function unwrap<T>(resp: { data: { data: T } }): Promise<T> {
  return resp.data.data;
}

export async function listSkills(): Promise<AgentSkill[]> {
  const resp = await client.get('/skills');
  const data = await unwrap<{ skills: AgentSkill[] }>(resp);
  return data.skills ?? [];
}

export async function setSkillEnabled(
  name: string,
  enabled: boolean,
): Promise<{ name: string; enabled: boolean }> {
  const resp = await client.patch(`/skills/${encodeURIComponent(name)}/enabled`, { enabled });
  return unwrap<{ name: string; enabled: boolean }>(resp);
}

export async function deleteSkill(name: string): Promise<void> {
  await client.delete(`/skills/${encodeURIComponent(name)}`);
}

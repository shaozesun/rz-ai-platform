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
}

export interface SkillReference {
  /** references/<stem>.md 的 stem */
  type: string;
  name: string;
  description: string;
}

export interface SkillDetail extends AgentSkill {
  /** SKILL.md 原文 */
  skill_md: string;
  references: SkillReference[];
}

async function unwrap<T>(resp: { data: { data: T } }): Promise<T> {
  return resp.data.data;
}

export async function listSkills(): Promise<AgentSkill[]> {
  const resp = await client.get('/skills');
  const data = await unwrap<{ skills: AgentSkill[] }>(resp);
  return data.skills ?? [];
}

export async function getSkill(name: string): Promise<SkillDetail> {
  const resp = await client.get(`/skills/${encodeURIComponent(name)}`);
  return unwrap<SkillDetail>(resp);
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

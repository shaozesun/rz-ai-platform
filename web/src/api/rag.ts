import client from './client';

export async function uploadFile(
  file: File,
  groupId: string = 'default',
): Promise<{ ok: boolean; name: string; size: number; chunks: number; msg?: string }> {
  const form = new FormData();
  form.append('file', file);
  const { data } = await client.post(`/upload?group_id=${groupId}`, form, {
    headers: { 'Content-Type': 'multipart/form-data' },
  });
  return data;
}

export async function deleteFile(
  name: string,
  groupId: string = 'default',
): Promise<{ ok: boolean; vectors_deleted: number; file_deleted: boolean }> {
  const { data } = await client.post(`/delete?group_id=${groupId}`, { name });
  return data;
}

export async function getVectorStoreInfo(
  groupId: string = 'default',
  source?: string,
): Promise<{ ok: boolean; data: unknown }> {
  const params = new URLSearchParams({ group_id: groupId });
  if (source) params.set('source', source);
  const { data } = await client.get(`/vector-store-info?${params}`);
  return data;
}

export async function retrieveDocuments(
  query: string,
  k: number = 10,
  groupId: string = 'default',
): Promise<{ ok: boolean; documents: { content: string; metadata: Record<string, unknown> }[] }> {
  const { data } = await client.post(`/retrieve?group_id=${groupId}`, { query, k });
  return data;
}

export async function getGroups(): Promise<{ ok: boolean; groups: { group_id: string; name: string; created_at: string | null }[] }> {
  const { data } = await client.get('/groups');
  return data;
}

export async function createGroup(name: string): Promise<{ ok: boolean; group: { group_id: string; name: string; created_at: string } }> {
  const { data } = await client.post('/groups', { name });
  return data;
}

export async function getStats(): Promise<{ ok: boolean; total_docs: number; total_chunks: number }> {
  const { data } = await client.get('/stats');
  return data;
}

export async function deleteGroupApi(groupId: string): Promise<{ ok: boolean }> {
  const { data } = await client.delete(`/groups/${groupId}`);
  return data;
}

import client from './client';

export async function getFeishuStatus() {
  const { data } = await client.get('/feishu/status');
  return data.data as { lark_bound: boolean; lark_name: string };
}

export async function connectFeishu() {
  const { data } = await client.post('/feishu/connect');
  return data.data as { code: string; instruction: string };
}

export async function unbindFeishu() {
  const { data } = await client.post('/feishu/unbind');
  return data;
}

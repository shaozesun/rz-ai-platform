import client from './client';

export async function getWechatStatus() {
  const { data } = await client.get('/wechat/status');
  return data.data as { wechat_bound: boolean; wechat_name: string };
}

export async function connectWechat() {
  const { data } = await client.post('/wechat/connect');
  return data.data as { code: string; instruction: string };
}

export async function unbindWechat() {
  const { data } = await client.post('/wechat/unbind');
  return data;
}

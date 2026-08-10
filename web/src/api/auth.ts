import client, { setAuth, clearAuth } from './client';

export async function fetchCaptcha() {
  const { data } = await client.get('/auth/captcha');
  return data.data as { captcha_id: string; image_base64: string };
}

export async function login(
  phone: string, password: string,
  captchaId: string, captchaCode: string, turnstileToken: string,
) {
  const { data } = await client.post('/auth/login', {
    phone, password,
    captcha_id: captchaId, captcha_code: captchaCode, turnstile_token: turnstileToken,
  });
  return data;
}

export async function register(
  phone: string, password: string,
  captchaId: string, captchaCode: string, turnstileToken: string,
) {
  const { data } = await client.post('/auth/register', {
    phone, password,
    captcha_id: captchaId, captcha_code: captchaCode, turnstile_token: turnstileToken,
  });
  return data;
}

export async function refreshToken() {
  const { data } = await client.post('/auth/refresh', {
    refresh_token: sessionStorage.getItem('refresh_token'),
  });
  if (data.data) {
    const d = data.data;
    setAuth(d.access_token, d.refresh_token);
  }
  return data;
}

export async function logout(): Promise<void> {
  const refreshToken = sessionStorage.getItem('refresh_token');
  if (refreshToken) {
    try {
      await client.post('/auth/logout', { refresh_token: refreshToken });
    } catch {
      // Ignore errors
    }
  }
  clearAuth();
}

export async function getMe() {
  const { data } = await client.get('/auth/me');
  return data;
}

export async function applyRoles(roles: string[], reason: string, permissions: string[] = []) {
  const { data } = await client.post('/auth/apply', { roles, permissions, reason });
  return data;
}

export async function resetPassword(
  phone: string, new_password: string,
  captchaId: string, captchaCode: string, turnstileToken: string,
) {
  const { data } = await client.post('/auth/reset-password', {
    phone, new_password,
    captcha_id: captchaId, captcha_code: captchaCode, turnstile_token: turnstileToken,
  });
  return data;
}

export async function updateProfile(updates: {
  name?: string;
  phone?: string;
  email?: string | null;
  company?: string | null;
  avatar?: string;
}) {
  const { data } = await client.put('/auth/profile', updates);
  return data;
}

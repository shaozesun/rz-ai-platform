import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios';

const API_BASE = '/api/v1';

const client = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

let isRefreshing = false;
let pendingQueue: ((token: string) => void)[] = [];
let refreshTimer: ReturnType<typeof setTimeout> | null = null;

const storage = sessionStorage;

function getAccessToken(): string | null {
  return storage.getItem('access_token');
}

function getRefreshToken(): string | null {
  return storage.getItem('refresh_token');
}

function clearAuth(): void {
  storage.removeItem('access_token');
  storage.removeItem('refresh_token');
  storage.removeItem('user');
  storage.removeItem('permissions');
}

function processQueue(token: string): void {
  pendingQueue.forEach((cb) => cb(token));
  pendingQueue = [];
}

function rejectQueue(_error: unknown): void {
  pendingQueue.forEach((cb) => cb('')); // trigger reject via empty token
  pendingQueue = [];
}

function decodeJwtExp(token: string): number {
  try {
    const payload = JSON.parse(atob(token.split('.')[1]));
    return payload.exp || 0;
  } catch {
    return 0;
  }
}

function scheduleRefresh(token: string): void {
  clearRefreshTimer();
  const exp = decodeJwtExp(token);
  if (!exp) return;
  const expiresIn = exp * 1000 - Date.now();
  const refreshIn = expiresIn - 5 * 60 * 1000; // 过期前 5 分钟刷新
  if (refreshIn <= 0) return; // 已经快过期了，让拦截器处理
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    doRefresh().catch(() => {});
  }, refreshIn);
}

function clearRefreshTimer(): void {
  if (refreshTimer) {
    clearTimeout(refreshTimer);
    refreshTimer = null;
  }
}

function setAuth(access: string, refresh: string): void {
  storage.setItem('access_token', access);
  storage.setItem('refresh_token', refresh);
  scheduleRefresh(access);
}

async function doRefresh(): Promise<string | null> {
  const refreshToken = getRefreshToken();
  if (!refreshToken) return null;

  const { data } = await axios.post(`${API_BASE}/auth/refresh`, {
    refresh_token: refreshToken,
  });

  const { access_token, refresh_token } = data.data;
  setAuth(access_token, refresh_token);
  return access_token;
}

// Request interceptor - attach token
client.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = getAccessToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Response interceptor - handle 401 auto-refresh
client.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as InternalAxiosRequestConfig & {
      _retry?: boolean;
    };

    if (error.response?.status !== 401 || originalRequest._retry) {
      return Promise.reject(error);
    }

    // Skip refresh for auth endpoints that would cause loops
    const url = originalRequest.url || '';
    const skipList = ['/auth/login', '/auth/refresh', '/auth/logout'];
    if (skipList.some((path) => url.includes(path))) {
      return Promise.reject(error);
    }

    if (isRefreshing) {
      return new Promise((resolve, reject) => {
        pendingQueue.push((token: string) => {
          if (!token) { reject(error); return; }
          originalRequest.headers.Authorization = `Bearer ${token}`;
          resolve(client(originalRequest));
        });
      });
    }

    isRefreshing = true;
    originalRequest._retry = true;

    try {
      const newToken = await doRefresh();
      if (!newToken) {
        throw new Error('No refresh token');
      }

      processQueue(newToken);
      originalRequest.headers.Authorization = `Bearer ${newToken}`;
      return client(originalRequest);
    } catch {
      clearRefreshTimer();
      clearAuth();
      rejectQueue(error);
      // 使用 replace 避免用户点后退按钮回到报错页面
      window.location.replace('/login');
      return Promise.reject(error);
    } finally {
      isRefreshing = false;
    }
  },
);

function initTokenRefresh(): void {
  const token = getAccessToken();
  if (token) {
    scheduleRefresh(token);
  }
}

export { client, setAuth, clearAuth, getAccessToken, initTokenRefresh };
export default client;

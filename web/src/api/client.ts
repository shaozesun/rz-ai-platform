import axios, { AxiosError, type InternalAxiosRequestConfig } from 'axios';

const API_BASE = '/api/v1';

const client = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

let isRefreshing = false;
let pendingQueue: (() => void)[] = [];

const storage = sessionStorage;

function getAccessToken(): string | null {
  return storage.getItem('access_token');
}

function getRefreshToken(): string | null {
  return storage.getItem('refresh_token');
}

function setAuth(access: string, refresh: string): void {
  storage.setItem('access_token', access);
  storage.setItem('refresh_token', refresh);
}

function clearAuth(): void {
  storage.removeItem('access_token');
  storage.removeItem('refresh_token');
  storage.removeItem('user');
  storage.removeItem('permissions');
}

function processQueue(): void {
  pendingQueue.forEach((cb) => cb());
  pendingQueue = [];
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
      return new Promise((resolve) => {
        pendingQueue.push(() => {
          originalRequest.headers.Authorization = `Bearer ${getAccessToken()}`;
          resolve(client(originalRequest));
        });
      });
    }

    isRefreshing = true;
    originalRequest._retry = true;

    try {
      const refreshToken = getRefreshToken();
      if (!refreshToken) {
        throw new Error('No refresh token');
      }

      const { data } = await axios.post(`${API_BASE}/auth/refresh`, {
        refresh_token: refreshToken,
      });

      const { access_token, refresh_token } = data.data;
      setAuth(access_token, refresh_token);

      processQueue();

      originalRequest.headers.Authorization = `Bearer ${access_token}`;
      return client(originalRequest);
    } catch {
      clearAuth();
      pendingQueue = [];
      window.location.href = '/login';
      return Promise.reject(error);
    } finally {
      isRefreshing = false;
    }
  },
);

export { client, setAuth, clearAuth, getAccessToken };
export default client;

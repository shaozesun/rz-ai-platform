import { create } from 'zustand';
import type { User } from '../types';
import { getMe } from '../api/auth';
import { initTokenRefresh } from '../api/client';

const storage = sessionStorage;

interface AuthState {
  accessToken: string | null;
  refreshToken: string | null;
  user: User | null;
  permissions: string[];
  isAuthenticated: boolean;
  initialized: boolean;

  setAuth: (access: string, refresh: string, user: User, permissions: string[]) => void;
  updateUser: (user: User) => void;
  loadFromStorage: () => void;
  clearAuth: () => void;
  hasPermission: (perm: string) => boolean;
  hasAnyPermission: (...perms: string[]) => boolean;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  accessToken: null,
  refreshToken: null,
  user: null,
  permissions: [],
  isAuthenticated: false,
  initialized: false,

  setAuth: (access, refresh, user, permissions) => {
    storage.setItem('access_token', access);
    storage.setItem('refresh_token', refresh);
    storage.setItem('user', JSON.stringify(user));
    storage.setItem('permissions', JSON.stringify(permissions));
    set({
      accessToken: access,
      refreshToken: refresh,
      user,
      permissions,
      isAuthenticated: true,
      initialized: true,
    });
  },

  updateUser: (user) => {
    storage.setItem('user', JSON.stringify(user));
    set({ user });
  },

  loadFromStorage: async () => {
    const access = storage.getItem('access_token');
    const refresh = storage.getItem('refresh_token');
    const userStr = storage.getItem('user');
    const permsStr = storage.getItem('permissions');

    if (access && userStr) {
      try {
        const user = JSON.parse(userStr) as User;
        const permissions = permsStr ? JSON.parse(permsStr) : [];
        initTokenRefresh(); // 启动主动续期定时器
        set({
          accessToken: access,
          refreshToken: refresh,
          user,
          permissions,
          isAuthenticated: true,
          initialized: true,
        });

        // 从后端拉取最新权限，避免缓存过期
        try {
          const res = await getMe();
          if (res.ok && res.data) {
            const fresh = res.data;
            storage.setItem('user', JSON.stringify(fresh));
            storage.setItem('permissions', JSON.stringify(fresh.permissions || []));
            set({
              user: fresh,
              permissions: fresh.permissions || [],
            });
          }
        } catch {
          // 静默失败，使用缓存中的权限
        }
      } catch {
        get().clearAuth();
        set({ initialized: true });
      }
    } else {
      set({ initialized: true });
    }
  },

  clearAuth: () => {
    storage.removeItem('access_token');
    storage.removeItem('refresh_token');
    storage.removeItem('user');
    storage.removeItem('permissions');
    set({
      accessToken: null,
      refreshToken: null,
      user: null,
      permissions: [],
      isAuthenticated: false,
    });
  },

  hasPermission: (perm: string) => {
    return get().permissions.includes(perm);
  },

  hasAnyPermission: (...perms: string[]) => {
    return perms.some((p) => get().permissions.includes(p));
  },
}));

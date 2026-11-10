const THEME_KEY = 'rz_theme';

export type Theme = 'light' | 'dark' | 'black';

/** 默认主题：浅色。用户没主动选过（或清了本地存储）时用它。
 *  已显式选过 dark/black 的用户不受影响——这里只是「没得选时的兜底」，不做强制迁移。 */
const DEFAULT_THEME: Theme = 'light';

export function getTheme(): Theme {
  const t = localStorage.getItem(THEME_KEY);
  if (t === 'light' || t === 'dark' || t === 'black') return t;
  return DEFAULT_THEME;
}

export function applyTheme(theme: Theme): void {
  const el = document.documentElement;
  el.classList.toggle('dark', theme === 'dark' || theme === 'black');
  el.classList.toggle('black', theme === 'black');
  localStorage.setItem(THEME_KEY, theme);
}

export function initTheme(): void {
  applyTheme(getTheme());
}

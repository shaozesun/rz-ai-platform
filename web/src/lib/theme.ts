const THEME_KEY = 'rz_theme';

export type Theme = 'light' | 'dark' | 'black';

export function getTheme(): Theme {
  const t = localStorage.getItem(THEME_KEY);
  if (t === 'light' || t === 'dark' || t === 'black') return t;
  return 'black';
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

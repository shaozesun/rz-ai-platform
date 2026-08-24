const THEME_KEY = 'rz_theme';

export type Theme = 'light' | 'dark';

export function getTheme(): Theme {
  return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light';
}

export function applyTheme(theme: Theme): void {
  document.documentElement.classList.toggle('dark', theme === 'dark');
  localStorage.setItem(THEME_KEY, theme);
}

export function initTheme(): void {
  document.documentElement.classList.toggle('dark', getTheme() === 'dark');
}

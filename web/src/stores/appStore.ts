import { create } from 'zustand';

// Responsive breakpoints: mobile < 768, tablet 768-1024, pc > 1024
export type Breakpoint = 'mobile' | 'tablet' | 'pc';

interface AppState {
  sidebarOpen: boolean;
  breakpoint: Breakpoint;
  toggleSidebar: () => void;
  setSidebarOpen: (open: boolean) => void;
  setBreakpoint: (bp: Breakpoint) => void;
}

export const useAppStore = create<AppState>((set) => ({
  sidebarOpen: true,
  breakpoint: 'pc',
  toggleSidebar: () => set((s) => ({ sidebarOpen: !s.sidebarOpen })),
  setSidebarOpen: (open) => set({ sidebarOpen: open }),
  setBreakpoint: (bp) => set({
    breakpoint: bp,
    sidebarOpen: bp === 'pc', // Auto-open on PC
  }),
}));

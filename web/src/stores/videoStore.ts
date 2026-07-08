import { create } from 'zustand';
import type { VideoTask, VideoTaskParams } from '../types';
import { createVideoTask, listVideoTasks, getVideoTask, deleteVideoTask } from '../api/video';

function isActive(s: string) {
  return s !== 'success' && s !== 'failed';
}

interface VideoState {
  tasks: VideoTask[];
  loading: boolean;

  loadTasks: () => Promise<void>;
  pollActiveTasks: () => Promise<void>;
  submitTask: (file: File, params: VideoTaskParams) => Promise<string>;
  deleteTask: (taskId: string) => Promise<void>;
}

export const useVideoStore = create<VideoState>((set, get) => ({
  tasks: [],
  loading: false,

  loadTasks: async () => {
    set({ loading: true });
    try {
      const res = await listVideoTasks();
      set({ tasks: res.data || [] });
    } finally {
      set({ loading: false });
    }
  },

  pollActiveTasks: async () => {
    const { tasks } = get();
    const activeIds = tasks.filter((t) => isActive(t.status)).map((t) => t.task_id);
    if (activeIds.length === 0) return;

    const updates = await Promise.all(
      activeIds.map(async (id) => {
        try {
          const res = await getVideoTask(id);
          return res.data as VideoTask;
        } catch {
          return null;
        }
      }),
    );

    set((state) => {
      const newTasks = [...state.tasks];
      for (const update of updates) {
        if (!update) continue;
        const idx = newTasks.findIndex((t) => t.task_id === update.task_id);
        if (idx !== -1) {
          newTasks[idx] = update;
        }
      }
      return { tasks: newTasks };
    });
  },

  submitTask: async (file, params) => {
    const res = await createVideoTask(file, params);
    const taskId = res.data.task_id;
    const optimistic: VideoTask = {
      task_id: taskId,
      original_filename: file.name,
      params,
      status: 'pending',
      progress: 0,
      progress_text: '',
      created_at: new Date().toISOString(),
    };
    set((state) => ({ tasks: [optimistic, ...state.tasks] }));
    return taskId;
  },

  deleteTask: async (taskId: string) => {
    const res = await deleteVideoTask(taskId);
    if (res.ok) {
      set((state) => ({ tasks: state.tasks.filter((t) => t.task_id !== taskId) }));
    }
  },
}));

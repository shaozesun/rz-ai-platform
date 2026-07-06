import { create } from 'zustand';
import type { VideoTask, VideoTaskParams } from '../types';
import { createVideoTask, listVideoTasks, deleteVideoTask } from '../api/video';

interface VideoState {
  tasks: VideoTask[];
  loading: boolean;

  loadTasks: () => Promise<void>;
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

  submitTask: async (file, params) => {
    const res = await createVideoTask(file, params);
    const taskId = res.data.task_id;
    // Refresh list after short delay to see new task
    setTimeout(() => get().loadTasks(), 500);
    return taskId;
  },

  deleteTask: async (taskId: string) => {
    await deleteVideoTask(taskId);
    set((state) => ({ tasks: state.tasks.filter((t) => t.task_id !== taskId) }));
  },
}));

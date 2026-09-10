// 任务完成提醒：长任务（对话/Agent/看板）完成且用户不在当前页时，走站内通知（顶部铃铛）。
// 浏览器原生 Notification 仅 HTTPS/localhost 可用，HTTP 下静默失败；故站内通知为主，
// HTTPS 已授权场景保留系统通知。

import { useNotificationStore } from '../stores/notificationStore';

export const BROWSER_NOTIFY_KEY = 'rz_browser_notify_enabled';

/** 通知跳转目标（点了铃铛里该条 → 切到对话页对应会话并定位回答；或跳 href 通用路径） */
export interface NotifyTarget {
  sessionId?: string;
  messageId?: string;
  href?: string;
}

/** 站内通知：推入顶部铃铛（不依赖安全上下文，HTTP 可用）。 */
export function pushInAppNotification(
  title: string,
  body?: string,
  target?: NotifyTarget,
): void {
  useNotificationStore.getState().push({
    id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    title,
    body,
    createdAt: Date.now(),
    ...target,
  });
}

/** 标题闪烁：后台完成时改标签标题，回到页面后恢复。 */
function flashTitle(title: string): void {
  const original = document.title;
  document.title = `${title} · ${original}`;
  const restore = () => {
    document.title = original;
    window.removeEventListener('focus', restore);
    document.removeEventListener('visibilitychange', restore);
  };
  window.addEventListener('focus', restore);
  document.addEventListener('visibilitychange', restore);
}

export function isNotificationSupported(): boolean {
  return typeof Notification !== 'undefined';
}

export function requestPermission(): Promise<NotificationPermission> {
  if (!isNotificationSupported()) return Promise.resolve('denied');
  return Notification.requestPermission();
}

/** 低层发送：仅支持 + 已授权守卫；点击通知关闭并聚焦回窗口。 */
export function showNotification(title: string, body?: string): void {
  if (!isNotificationSupported() || Notification.permission !== 'granted') return;
  try {
    const n = new Notification(title, { body, icon: '/logo.png' });
    n.onclick = () => {
      n.close();
      window.focus();
    };
  } catch {
    // 某些环境/浏览器禁止构造 Notification，静默忽略
  }
}

/** 任务完成提醒：开关 + 前台不打扰 + 站内推送（HTTPS 已授权时叠加系统通知）。 */
export function notifyTurnFinished(text?: string, target?: NotifyTarget): void {
  if (localStorage.getItem(BROWSER_NOTIFY_KEY) === 'false') return;
  if (!document.hidden && document.hasFocus()) return; // 用户就在当前页，不打扰
  const trimmed = text ? text.replace(/\s+/g, ' ').trim().slice(0, 60) : '';
  const title = '对话生成完成';
  const body = trimmed ? `「${trimmed}」的回复已生成，点击查看` : 'AI 回复已生成完毕，点击查看';
  pushInAppNotification(title, body, target);
  if (document.hidden) flashTitle(`🔔 ${title}`);
  if (isNotificationSupported() && Notification.permission === 'granted') {
    showNotification(title, body);
  }
}

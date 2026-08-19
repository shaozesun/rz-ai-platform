// 浏览器通知工具：长任务完成且用户切到其它标签页时发系统通知。
// 参考 deerflow（frontend/src/core/notification/hooks.ts）：在流 onFinish 里
// 判断 document.hidden || !document.hasFocus() 才发，受设置开关控制。

export const BROWSER_NOTIFY_KEY = 'rz_browser_notify_enabled';

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
    const n = new Notification(title, { body, icon: '/logo.jpg' });
    n.onclick = () => {
      n.close();
      window.focus();
    };
  } catch {
    // 某些环境/浏览器禁止构造 Notification，静默忽略
  }
}

/** 任务完成通知：三重守卫（支持 + 授权 + 开关）且仅当用户不在当前页时发。 */
export function notifyTurnFinished(text?: string): void {
  if (!isNotificationSupported()) return;
  if (Notification.permission !== 'granted') return;
  if (localStorage.getItem(BROWSER_NOTIFY_KEY) === 'false') return;
  if (!document.hidden && document.hasFocus()) return; // 用户就在当前页，不打扰
  const trimmed = text ? text.replace(/\s+/g, ' ').trim().slice(0, 60) : '';
  showNotification(
    '对话生成完成',
    trimmed ? `「${trimmed}」的回复已生成，点击查看` : 'AI 回复已生成完毕，点击查看',
  );
}

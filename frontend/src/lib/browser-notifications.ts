/**
 * Browser notifications (#149): thin wrapper around the Notification API, so components
 * and tests do not touch the global directly.
 *
 * Privacy: a notification shows only what the server returned for it (sender and category;
 * the subject only if the user turned it on). It stays on this device and is replaced, not
 * stacked, when the same mail is announced again (`tag`).
 */

export type NotificationAccess = NotificationPermission | "unsupported";

export function notificationAccess(): NotificationAccess {
  if (typeof window === "undefined" || !("Notification" in window)) return "unsupported";
  return Notification.permission;
}

/** Asks the browser for permission (must run in a click handler). */
export async function requestNotificationAccess(): Promise<NotificationAccess> {
  if (notificationAccess() === "unsupported") return "unsupported";
  try {
    return await Notification.requestPermission();
  } catch {
    return Notification.permission;
  }
}

/** True if the user is looking at this tab: the app updates itself, no notification. */
export function pageIsInUse(): boolean {
  return document.visibilityState === "visible" && document.hasFocus();
}

export interface MailNotificationText {
  /** Sender, or a neutral text if unknown. */
  title: string;
  /** Category, and the subject if the user chose to see it. */
  body: string;
}

export function mailNotificationText({
  sender,
  category,
  subject,
  unknownSender,
}: {
  sender: string | null;
  category: string | null;
  subject: string | null;
  unknownSender: string;
}): MailNotificationText {
  const parts = [category, subject].filter((part): part is string => !!part?.trim());
  return { title: sender?.trim() || unknownSender, body: parts.join(" · ") };
}

export interface ShowNotificationOptions extends MailNotificationText {
  /** Same tag: a later notification replaces the earlier one (e.g. one per mail). */
  tag: string;
  /** Play the browser's notification sound. Off by default. */
  sound: boolean;
  onClick?: () => void;
}

export function showNotification({
  title,
  body,
  tag,
  sound,
  onClick,
}: ShowNotificationOptions): Notification | undefined {
  if (notificationAccess() !== "granted") return undefined;
  try {
    const notification = new Notification(title, {
      body,
      tag,
      silent: !sound,
      icon: "/icons/icon-192.png",
    });
    notification.onclick = () => {
      window.focus();
      onClick?.();
      notification.close();
    };
    return notification;
  } catch {
    // Some mobile browsers only allow notifications through a service worker.
    return undefined;
  }
}

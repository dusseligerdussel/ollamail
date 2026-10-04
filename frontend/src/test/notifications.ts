import { vi } from "vitest";

import type { MailNotification, NotificationSettings } from "@/api/notifications";

import { json, problem } from "./fetch";

export function testNotificationSettings(
  overrides: Partial<NotificationSettings> = {},
): NotificationSettings {
  return {
    available: true,
    enabled: false,
    category_ids: [],
    show_subject: false,
    sound: false,
    ...overrides,
  };
}

/** In-memory `/api/notifications/*`; notifications by message ID (synthetic data). */
export function notificationsApi({
  settings = testNotificationSettings(),
  messages = {} as Record<string, MailNotification>,
} = {}) {
  const updates: unknown[] = [];
  let current = settings;

  async function handle(request: Request): Promise<Response | undefined> {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /api/notifications/settings") return json(current);
    if (route === "PUT /api/notifications/settings") {
      const body = (await request.json()) as Partial<NotificationSettings>;
      updates.push(body);
      current = { ...current, ...body };
      return json(current);
    }
    const message = /^GET \/api\/notifications\/messages\/([^/]+)$/.exec(route);
    if (message?.[1]) {
      const found = messages[message[1]];
      return found ? json(found) : problem(404);
    }
    return undefined;
  }

  return { handle, updates, current: () => current };
}

export interface FakeNotificationInstance {
  title: string;
  options: NotificationOptions;
  onclick: (() => void) | null;
  close: () => void;
}

/**
 * Stand-in for the browser's `Notification` (jsdom has none). `permission` is what the
 * browser currently allows, `answer` what it answers to `requestPermission()`.
 */
export function stubNotifications({
  permission = "default" as NotificationPermission,
  answer = "granted" as NotificationPermission,
} = {}) {
  const shown: FakeNotificationInstance[] = [];
  const requestPermission = vi.fn(async () => {
    FakeNotification.permission = answer;
    return answer;
  });
  class FakeNotification implements FakeNotificationInstance {
    static permission: NotificationPermission = permission;
    static requestPermission = requestPermission;
    onclick: (() => void) | null = null;
    close = vi.fn();

    constructor(
      readonly title: string,
      readonly options: NotificationOptions = {},
    ) {
      shown.push(this);
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
  return { shown, requestPermission };
}

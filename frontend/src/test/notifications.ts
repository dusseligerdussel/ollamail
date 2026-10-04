import { vi } from "vitest";

import type {
  MailNotification,
  NotificationSettings,
  PushDevice,
  WebPush,
} from "@/api/notifications";

import { base64UrlToBytes } from "@/lib/web-push";

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

/** A VAPID public key in the format the server sends (65 bytes, synthetic). */
export const TEST_VAPID_KEY = `B${"A".repeat(86)}`;

export function testPushDevice(overrides: Partial<PushDevice> = {}): PushDevice {
  return {
    id: "0199e000-0000-7000-8000-00000000d001",
    browser: "Firefox",
    os: "Linux",
    mobile: false,
    push_service: "updates.push.services.mozilla.com",
    created_at: "2026-10-01T08:00:00Z",
    last_sent_at: null,
    ...overrides,
  };
}

/** In-memory `/api/notifications/*`; notifications by message ID (synthetic data). */
export function notificationsApi({
  settings = testNotificationSettings(),
  messages = {} as Record<string, MailNotification>,
  webPush = { available: false, public_key: null, devices: [] } as WebPush,
} = {}) {
  const updates: unknown[] = [];
  const registered: unknown[] = [];
  const removed: string[] = [];
  let current = settings;
  let push = webPush;

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
    if (route === "GET /api/notifications/push") return json(push);
    if (route === "POST /api/notifications/push/devices") {
      registered.push(await request.json());
      const device = testPushDevice({ id: "0199e000-0000-7000-8000-00000000d0ff" });
      if (!push.devices.some((d) => d.id === device.id)) {
        push = { ...push, devices: [...push.devices, device] };
      }
      return json(device, { status: 201 });
    }
    const device = /^DELETE \/api\/notifications\/push\/devices\/([^/]+)$/.exec(route);
    if (device?.[1]) {
      const id = device[1];
      if (!push.devices.some((d) => d.id === id)) return problem(404);
      removed.push(id);
      push = { ...push, devices: push.devices.filter((d) => d.id !== id) };
      return new Response(null, { status: 204 });
    }
    const message = /^GET \/api\/notifications\/messages\/([^/]+)$/.exec(route);
    if (message?.[1]) {
      const found = messages[message[1]];
      return found ? json(found) : problem(404);
    }
    return undefined;
  }

  return { handle, updates, registered, removed, current: () => current };
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

/**
 * Stand-in for the service worker registration and its `PushManager` (jsdom has neither).
 * `subscribed`: this browser already has a subscription made with `TEST_VAPID_KEY`.
 */
export function stubPushManager({ subscribed = false } = {}) {
  const messages: unknown[] = [];
  const key = base64UrlToBytes(TEST_VAPID_KEY).buffer;
  const subscription = {
    endpoint: "https://updates.push.services.mozilla.com/wpush/v2/test-device",
    options: { applicationServerKey: key },
    toJSON: () => ({ keys: { p256dh: "BTestP256dh", auth: "TestAuth" } }),
    unsubscribe: vi.fn(async () => {
      current = null;
      return true;
    }),
  };
  let current: typeof subscription | null = subscribed ? subscription : null;
  const pushManager = {
    getSubscription: vi.fn(async () => current),
    subscribe: vi.fn(async () => {
      current = subscription;
      return subscription;
    }),
  };
  const registration = { pushManager, active: { postMessage: (m: unknown) => messages.push(m) } };
  vi.stubGlobal("PushManager", class {});
  Object.defineProperty(navigator, "serviceWorker", {
    configurable: true,
    value: { getRegistration: vi.fn(async () => registration) },
  });
  return { pushManager, subscription, messages };
}

export function unstubPushManager() {
  Reflect.deleteProperty(navigator, "serviceWorker");
}

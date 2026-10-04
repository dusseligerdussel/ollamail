import type { Page, Route } from "@playwright/test";

// Notification settings and one announced mail, mocked in the browser (synthetic data).

export const NOTIFIED_MESSAGE_ID = "0199e000-0000-7000-8000-00000000a001";
const MAILBOX_ID = "0199e000-0000-7000-8000-00000000b001";

export const notificationCategories = [
  ["important", "Important"],
  ["action_required", "Action required"],
  ["waiting_for", "Waiting for"],
  ["info", "Info"],
  ["newsletter", "Newsletter"],
].map(([key, name], index) => ({
  id: `0199e000-0000-7000-8000-00000000c00${index + 1}`,
  name,
  description: `Description of ${name}`,
  builtin_key: key,
  scope: "organization",
  hidden: false,
  position: index,
}));

export interface PushDeviceMock {
  id: string;
  browser: string | null;
  os: string | null;
  mobile: boolean;
  push_service: string;
  created_at: string;
  last_sent_at: string | null;
}

/** A VAPID public key in the format the server sends (65 bytes, synthetic). */
export const VAPID_KEY = `B${"A".repeat(86)}`;
export const THIS_DEVICE_ID = "0199e000-0000-7000-8000-00000000d0ff";

export const otherDevice: PushDeviceMock = {
  id: "0199e000-0000-7000-8000-00000000d001",
  browser: "Chrome",
  os: "Android",
  mobile: true,
  push_service: "fcm.googleapis.com",
  created_at: "2026-10-01T08:00:00Z",
  last_sent_at: null,
};

export interface MockNotifications {
  enabled?: boolean;
  available?: boolean;
  showSubject?: boolean;
  /** Category IDs the user opted in to. */
  categoryIds?: string[];
  /** Web Push switched on by the administrator, with these devices. */
  webPush?: { devices: PushDeviceMock[] };
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, json: body });
}

export async function mockNotifications(
  page: Page,
  {
    enabled = false,
    available = true,
    showSubject = false,
    categoryIds = [],
    webPush,
  }: MockNotifications = {},
) {
  let push = {
    available: !!webPush,
    public_key: webPush ? VAPID_KEY : null,
    devices: webPush?.devices ?? [],
  };
  const registered: unknown[] = [];
  const removed: string[] = [];
  let settings = {
    available,
    enabled,
    category_ids: categoryIds,
    show_subject: showSubject,
    sound: false,
  };
  const updates: unknown[] = [];
  await page.route(
    (url) =>
      url.pathname.startsWith("/api/notifications/") || url.pathname === "/api/triage/categories",
    (route) => {
      const request = route.request();
      const key = `${request.method()} ${new URL(request.url()).pathname}`;
      if (key === "GET /api/triage/categories") return json(route, notificationCategories);
      if (key === "GET /api/notifications/settings") return json(route, settings);
      if (key === "PUT /api/notifications/settings") {
        const body = request.postDataJSON() as Partial<typeof settings>;
        updates.push(body);
        settings = { ...settings, ...body };
        return json(route, settings);
      }
      if (key === "GET /api/notifications/push") return json(route, push);
      if (key === "POST /api/notifications/push/devices") {
        registered.push(request.postDataJSON());
        const device: PushDeviceMock = {
          id: THIS_DEVICE_ID,
          browser: "Chrome",
          os: "Linux",
          mobile: false,
          push_service: "fcm.googleapis.com",
          created_at: "2026-10-04T09:00:00Z",
          last_sent_at: null,
        };
        push = {
          ...push,
          devices: [...push.devices.filter((d) => d.id !== device.id), device],
        };
        return json(route, device, 201);
      }
      const remove = /^DELETE \/api\/notifications\/push\/devices\/(.+)$/.exec(key);
      if (remove?.[1]) {
        const id = remove[1];
        removed.push(id);
        push = { ...push, devices: push.devices.filter((d) => d.id !== id) };
        return route.fulfill({ status: 204 });
      }
      if (key === `GET /api/notifications/messages/${NOTIFIED_MESSAGE_ID}`) {
        return json(route, {
          message_id: NOTIFIED_MESSAGE_ID,
          mailbox_id: MAILBOX_ID,
          sender: "Erika Musterfrau",
          category: {
            id: notificationCategories[1]?.id,
            name: "Action required",
            builtin_key: "action_required",
          },
          subject: settings.show_subject ? "Angebot bis Freitag" : null,
          sound: settings.sound,
        });
      }
      return json(route, { status: 404 }, 404);
    },
  );
  return { updates, registered, removed };
}

/**
 * Stand-in for the service worker registration and its push subscription: a real one
 * needs the browser vendor's push service. Call before `page.goto`.
 */
export async function mockPushManager(page: Page) {
  await page.addInitScript(() => {
    let subscription: PushSubscription | null = null;
    const pushManager = {
      getSubscription: async () => subscription,
      subscribe: async (options: PushSubscriptionOptionsInit) => {
        subscription = {
          endpoint: "https://fcm.googleapis.com/fcm/send/e2e-device",
          options: { applicationServerKey: options.applicationServerKey },
          toJSON: () => ({ keys: { p256dh: "BE2eP256dh", auth: "E2eAuth" } }),
          unsubscribe: async () => {
            subscription = null;
            return true;
          },
        } as unknown as PushSubscription;
        return subscription;
      },
    };
    const registration = { pushManager, active: { postMessage: () => {} } };
    Object.assign(window, { PushManager: class {} });
    navigator.serviceWorker.getRegistration = async () =>
      registration as unknown as ServiceWorkerRegistration;
  });
}

/** Server events: one `notification.message` for `NOTIFIED_MESSAGE_ID`, then the stream ends. */
export async function mockNotificationEvent(page: Page) {
  const event = {
    type: "notification.message",
    ids: { message_id: NOTIFIED_MESSAGE_ID, mailbox_id: MAILBOX_ID },
    status: null,
  };
  let sent = false;
  await page.route(
    (url) => url.pathname === "/api/events",
    (route) => {
      // The browser reconnects after the stream ends; announce the mail only once.
      const body = sent
        ? "retry: 60000\n\n"
        : `retry: 60000\n\nevent: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
      sent = true;
      return route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
        body,
      });
    },
  );
}

/**
 * Records notifications instead of showing them and lets the page look unattended (another
 * window has the focus), so the app announces mails. Call before `page.goto`.
 */
export async function recordNotifications(page: Page) {
  await page.addInitScript(() => {
    const shown: { title: string; body?: string; silent?: boolean | null; tag?: string }[] = [];
    Object.assign(window, { __notifications: shown });
    class RecordingNotification {
      static permission: NotificationPermission = "granted";
      static requestPermission = async () => "granted" as const;
      onclick: (() => void) | null = null;
      constructor(title: string, options: NotificationOptions = {}) {
        shown.push({ title, body: options.body, silent: options.silent, tag: options.tag });
      }
      close() {}
    }
    Object.assign(window, { Notification: RecordingNotification });
    document.hasFocus = () => false;
  });
}

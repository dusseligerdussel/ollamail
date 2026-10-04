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

export interface MockNotifications {
  enabled?: boolean;
  available?: boolean;
  showSubject?: boolean;
  /** Category IDs the user opted in to. */
  categoryIds?: string[];
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
  }: MockNotifications = {},
) {
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
  return { updates };
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

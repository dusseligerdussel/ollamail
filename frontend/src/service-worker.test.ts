import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { runInNewContext } from "node:vm";

import { describe, expect, it, vi } from "vitest";

// public/sw.js runs in a simulated service worker scope: events, clients, caches and fetch
// are stand-ins, so the push handling can be checked without a browser (synthetic data).

const SOURCE = readFileSync(resolve(__dirname, "../public/sw.js"), "utf8");
const MESSAGE_ID = "0199e000-0000-7000-8000-00000000a001";

interface FakeClient {
  url: string;
  visibilityState: "visible" | "hidden";
  focused: boolean;
  focus: () => Promise<void>;
  navigate: (url: string) => Promise<void>;
}

function client(overrides: Partial<FakeClient> = {}): FakeClient {
  return {
    url: "http://localhost/inbox",
    visibilityState: "hidden",
    focused: false,
    focus: vi.fn(async () => {}),
    navigate: vi.fn(async () => {}),
    ...overrides,
  };
}

function serviceWorker({
  windows = [] as FakeClient[],
  response = new Response(null, { status: 401 }) as Response | (() => Promise<Response>),
} = {}) {
  const listeners = new Map<string, (event: unknown) => void>();
  const stored = new Map<string, Response>();
  const shown: { title: string; options: NotificationOptions & { data?: unknown } }[] = [];
  const openWindow = vi.fn(async () => null);
  const fetch = vi.fn(async () => (typeof response === "function" ? response() : response));
  const caches = {
    open: async () => ({
      put: async (key: string, value: Response) => void stored.set(key, value),
      addAll: async () => {},
    }),
    match: async (key: string) => stored.get(key)?.clone(),
    keys: async () => [],
    delete: async () => true,
  };
  const self = {
    location: new URL("http://localhost/"),
    addEventListener: (type: string, listener: (event: unknown) => void) =>
      listeners.set(type, listener),
    registration: {
      showNotification: async (title: string, options: NotificationOptions) =>
        void shown.push({ title, options }),
    },
    clients: { matchAll: async () => windows, openWindow, claim: async () => {} },
    skipWaiting: async () => {},
  };
  runInNewContext(SOURCE, {
    self,
    caches,
    fetch,
    URL,
    Response,
    JSON,
    Promise,
    encodeURIComponent,
  });

  async function dispatch(type: string, event: Record<string, unknown>) {
    let pending: Promise<unknown> = Promise.resolve();
    listeners.get(type)?.({ ...event, waitUntil: (p: Promise<unknown>) => (pending = p) });
    await pending;
  }

  return {
    shown,
    fetch,
    openWindow,
    push: (payload: unknown) => dispatch("push", { data: { json: () => payload } }),
    message: (data: unknown) => dispatch("message", { data }),
    click: (notification: { data?: unknown }) =>
      dispatch("notificationclick", { notification: { ...notification, close: vi.fn() } }),
  };
}

const push = { type: "notification.message", message_id: MESSAGE_ID, mailbox_id: "m" };

function content(overrides: Record<string, unknown> = {}) {
  return new Response(
    JSON.stringify({
      message_id: MESSAGE_ID,
      sender: "Erika Musterfrau",
      category: { id: "c", name: "Important", builtin_key: "important" },
      subject: null,
      sound: false,
      ...overrides,
    }),
    { status: 200 },
  );
}

describe("service worker: web push", () => {
  it("fetches the content and shows it in the user's language, silently", async () => {
    const sw = serviceWorker({ response: () => Promise.resolve(content()) });
    await sw.message({
      type: "ollamail:notification-strings",
      strings: {
        unknownSender: "Neue Mail",
        fallbackTitle: "ollamail",
        fallbackBody: "Neue Mail.",
        categories: { important: "Wichtig" },
      },
    });

    await sw.push(push);

    expect(sw.fetch).toHaveBeenCalledWith(
      `/api/notifications/messages/${MESSAGE_ID}`,
      expect.objectContaining({ credentials: "same-origin" }),
    );
    expect(sw.shown).toEqual([
      {
        title: "Erika Musterfrau",
        options: expect.objectContaining({
          body: "Wichtig",
          silent: true,
          tag: `ollamail-message-${MESSAGE_ID}`,
          data: { url: `/inbox?message=${MESSAGE_ID}` },
        }),
      },
    ]);
  });

  it("shows the subject and plays the sound only when the server says so", async () => {
    const sw = serviceWorker({
      response: () => Promise.resolve(content({ subject: "Angebot", sound: true, sender: null })),
    });
    await sw.push(push);
    expect(sw.shown[0]?.title).toBe("Unknown sender");
    expect(sw.shown[0]?.options).toMatchObject({ body: "Important · Angebot", silent: false });
  });

  it("shows a neutral text without details when the content cannot be loaded", async () => {
    const sw = serviceWorker();
    await sw.push(push);
    expect(sw.shown).toHaveLength(1);
    expect(sw.shown[0]?.title).toBe("ollamail");
    expect(sw.shown[0]?.options.body).toBe("New mail. Open ollamail to see it.");
  });

  it("stays quiet while the app is in use", async () => {
    const sw = serviceWorker({ windows: [client({ visibilityState: "visible", focused: true })] });
    await sw.push(push);
    expect(sw.shown).toEqual([]);
    expect(sw.fetch).not.toHaveBeenCalled();
  });

  it("shows it with a tab in the background (the tab uses the same tag)", async () => {
    const sw = serviceWorker({ windows: [client()], response: () => Promise.resolve(content()) });
    await sw.push(push);
    expect(sw.shown[0]?.options.tag).toBe(`ollamail-message-${MESSAGE_ID}`);
  });

  it("ignores pushes it does not know", async () => {
    const sw = serviceWorker();
    await sw.push({ type: "something.else" });
    await sw.push(null);
    expect(sw.shown).toEqual([]);
  });

  it("a click focuses an open tab on the mail, or opens one", async () => {
    const open = client();
    const withTab = serviceWorker({ windows: [open] });
    await withTab.click({ data: { url: `/inbox?message=${MESSAGE_ID}` } });
    expect(open.focus).toHaveBeenCalled();
    expect(open.navigate).toHaveBeenCalledWith(`/inbox?message=${MESSAGE_ID}`);

    const withoutTab = serviceWorker();
    await withoutTab.click({ data: { url: `/inbox?message=${MESSAGE_ID}` } });
    expect(withoutTab.openWindow).toHaveBeenCalledWith(`/inbox?message=${MESSAGE_ID}`);

    // Only paths of the app itself.
    await withoutTab.click({ data: { url: "https://example.org/" } });
    await withoutTab.click({ data: { url: "//example.org/" } });
    expect(withoutTab.openWindow).toHaveBeenCalledTimes(1);
  });
});

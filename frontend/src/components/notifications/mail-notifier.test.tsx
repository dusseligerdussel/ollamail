import { act, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MailNotification } from "@/api/notifications";
import { backend, mockFetch, testUser } from "@/test/fetch";
import {
  notificationsApi,
  stubNotifications,
  testNotificationSettings,
} from "@/test/notifications";
import { renderApp } from "@/test/render-app";
import { categoryId } from "@/test/triage";

class MockEventSource {
  static last: MockEventSource | undefined;
  readonly listeners = new Map<string, Set<(event: Event) => void>>();

  constructor(readonly url: string) {
    MockEventSource.last = this;
  }

  addEventListener(type: string, listener: (event: Event) => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)?.add(listener);
  }

  close() {}

  emit(type: string, data: unknown) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }
}

const MESSAGE_ID = "0199e000-0000-7000-8000-00000000a001";

function content(overrides: Partial<MailNotification> = {}): MailNotification {
  return {
    message_id: MESSAGE_ID,
    mailbox_id: "0199e000-0000-7000-8000-00000000b001",
    sender: "Erika Musterfrau",
    category: { id: categoryId(1), name: "Important", builtin_key: "important" },
    subject: null,
    sound: false,
    ...overrides,
  };
}

function announce() {
  act(() =>
    MockEventSource.last?.emit("notification.message", {
      type: "notification.message",
      ids: { message_id: MESSAGE_ID, mailbox_id: "0199e000-0000-7000-8000-00000000b001" },
      status: null,
    }),
  );
}

function setup(options: Parameters<typeof notificationsApi>[0] = {}) {
  const api = notificationsApi({
    settings: testNotificationSettings({ enabled: true, category_ids: [categoryId(1)] }),
    messages: { [MESSAGE_ID]: content() },
    ...options,
  });
  const fallback = backend({ user: testUser });
  mockFetch(async (request) => (await api.handle(request)) ?? fallback(request.clone()));
  return api;
}

let focused: boolean;

beforeEach(() => {
  vi.stubGlobal("EventSource", MockEventSource);
  focused = false;
  vi.spyOn(document, "hasFocus").mockImplementation(() => focused);
  vi.spyOn(window, "focus").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("MailNotifier", () => {
  it("shows sender and category, silently, and opens the mail on click", async () => {
    setup();
    const browser = stubNotifications({ permission: "granted" });
    const { router } = await renderApp("/settings");

    announce();

    await waitFor(() => expect(browser.shown).toHaveLength(1));
    const [shown] = browser.shown;
    expect(shown?.title).toBe("Erika Musterfrau");
    expect(shown?.options).toMatchObject({
      body: "Important",
      silent: true,
      tag: `ollamail-message-${MESSAGE_ID}`,
    });
    act(() => shown?.onclick?.());
    await waitFor(() => expect(router.state.location.pathname).toBe("/inbox"));
    expect(router.state.location.search).toMatchObject({ message: MESSAGE_ID });
  });

  it("adds the subject and sound only when the server says so", async () => {
    setup({ messages: { [MESSAGE_ID]: content({ subject: "Contract renewal", sound: true }) } });
    const browser = stubNotifications({ permission: "granted" });
    await renderApp("/settings");

    announce();

    await waitFor(() => expect(browser.shown).toHaveLength(1));
    expect(browser.shown[0]?.options).toMatchObject({
      body: "Important · Contract renewal",
      silent: false,
    });
  });

  it("stays quiet while the app is in use or without the browser's permission", async () => {
    setup();
    const browser = stubNotifications({ permission: "granted" });
    await renderApp("/settings");
    focused = true;
    announce();

    focused = false;
    const denied = stubNotifications({ permission: "denied" });
    announce();
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(browser.shown).toEqual([]);
    expect(denied.shown).toEqual([]);
  });

  it("shows nothing when notifications are switched off in the settings", async () => {
    setup({ settings: testNotificationSettings({ enabled: false }) });
    const browser = stubNotifications({ permission: "granted" });
    await renderApp("/settings");

    announce();
    await new Promise((resolve) => setTimeout(resolve, 50));

    expect(browser.shown).toEqual([]);
  });
});

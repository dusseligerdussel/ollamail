import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { forgetPushDevice } from "@/api/notifications";

import { backend, mockFetch, testUser } from "@/test/fetch";
import {
  notificationsApi,
  stubNotifications,
  stubPushManager,
  TEST_VAPID_KEY,
  testNotificationSettings,
  testPushDevice,
  unstubPushManager,
} from "@/test/notifications";
import { renderApp } from "@/test/render-app";
import { categoryId, triageApi } from "@/test/triage";

function setup(options: Parameters<typeof notificationsApi>[0] = {}) {
  const api = notificationsApi(options);
  const triage = triageApi();
  const fallback = backend({ user: testUser });
  mockFetch(async (request) => {
    const clone = request.clone();
    return (await api.handle(request)) ?? (await triage.handle(clone)) ?? fallback(request.clone());
  });
  return api;
}

describe("notification settings", () => {
  it("is linked from the settings", async () => {
    setup();
    await renderApp("/settings");

    expect(screen.getByRole("link", { name: /set up notifications/i })).toHaveAttribute(
      "href",
      "/settings/notifications",
    );
  });

  it("says when the administrator switched notifications off", async () => {
    setup({ settings: testNotificationSettings({ available: false }) });
    stubNotifications();
    await renderApp("/settings/notifications");

    expect(await screen.findByText("Notifications not enabled")).toBeInTheDocument();
    expect(screen.queryByRole("switch")).not.toBeInTheDocument();
  });

  it("is off by default and asks the browser before switching on", async () => {
    const api = setup();
    const browser = stubNotifications({ permission: "default", answer: "granted" });
    const user = userEvent.setup();
    await renderApp("/settings/notifications");

    const enabled = await screen.findByRole("switch", { name: "Notify me about new mail" });
    expect(enabled).not.toBeChecked();
    // Subject and sound are off by default and cannot change while notifications are off.
    expect(screen.getByRole("switch", { name: "Show subject" })).not.toBeChecked();
    expect(screen.getByRole("switch", { name: "Sound" })).toBeDisabled();
    expect(await screen.findByRole("checkbox", { name: "Important" })).toBeDisabled();

    await user.click(enabled);

    expect(browser.requestPermission).toHaveBeenCalledOnce();
    // The first opt-in selects "Important" and "Action required".
    await waitFor(() =>
      expect(api.updates).toEqual([
        { enabled: true, category_ids: [categoryId(1), categoryId(2)] },
      ]),
    );
    expect(await screen.findByRole("checkbox", { name: "Important" })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "Waiting for" })).not.toBeChecked();
    expect(screen.getByText("Notifications are allowed.")).toBeInTheDocument();
  });

  it("stays off if the browser refuses", async () => {
    const api = setup();
    stubNotifications({ permission: "default", answer: "denied" });
    const user = userEvent.setup();
    await renderApp("/settings/notifications");

    await user.click(await screen.findByRole("switch", { name: "Notify me about new mail" }));

    expect(await screen.findByText(/browser blocks notifications/i)).toBeInTheDocument();
    expect(api.updates).toEqual([]);
    expect(screen.getByRole("switch", { name: "Notify me about new mail" })).not.toBeChecked();
  });

  it("changes categories, subject and sound", async () => {
    const api = setup({
      settings: testNotificationSettings({ enabled: true, category_ids: [categoryId(1)] }),
    });
    stubNotifications({ permission: "granted" });
    const user = userEvent.setup();
    await renderApp("/settings/notifications");

    await user.click(await screen.findByRole("checkbox", { name: "Waiting for" }));
    await waitFor(() =>
      expect(api.updates).toEqual([{ category_ids: [categoryId(1), categoryId(3)] }]),
    );
    await user.click(screen.getByRole("switch", { name: "Show subject" }));
    await user.click(screen.getByRole("switch", { name: "Sound" }));

    await waitFor(() =>
      expect(api.updates).toEqual([
        { category_ids: [categoryId(1), categoryId(3)] },
        { show_subject: true },
        { sound: true },
      ]),
    );
  });

  it("sends a silent test notification without the subject by default", async () => {
    setup({ settings: testNotificationSettings({ enabled: true, category_ids: [categoryId(1)] }) });
    const browser = stubNotifications({ permission: "granted" });
    const user = userEvent.setup();
    await renderApp("/settings/notifications");

    await user.click(await screen.findByRole("button", { name: "Send test" }));

    expect(browser.shown).toHaveLength(1);
    expect(browser.shown[0]?.title).toBe("Jane Example");
    expect(browser.shown[0]?.options).toMatchObject({ body: "Important", silent: true });
  });

  it("explains when the browser cannot show notifications", async () => {
    setup();
    await renderApp("/settings/notifications");

    expect(
      await screen.findByText("This browser does not support notifications."),
    ).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: "Notify me about new mail" })).toBeDisabled();
  });
});

describe("web push", () => {
  afterEach(() => unstubPushManager());

  const available = (devices = [testPushDevice()]) => ({
    available: true,
    public_key: TEST_VAPID_KEY,
    devices,
  });

  it("is hidden while the administrator has not switched it on", async () => {
    setup({ settings: testNotificationSettings({ enabled: true }) });
    stubNotifications({ permission: "granted" });
    stubPushManager();
    await renderApp("/settings/notifications");

    await screen.findByRole("switch", { name: "Notify me about new mail" });
    expect(screen.queryByText("Without an open tab")).not.toBeInTheDocument();
  });

  it("names the push service and subscribes this device", async () => {
    const api = setup({
      settings: testNotificationSettings({ enabled: true }),
      webPush: available([]),
    });
    stubNotifications({ permission: "granted" });
    const push = stubPushManager();
    const user = userEvent.setup();
    await renderApp("/settings/notifications");

    expect(await screen.findByText(/Chrome: Google, Firefox: Mozilla/)).toBeInTheDocument();
    expect(screen.getByText("No device set up yet.")).toBeInTheDocument();
    const toggle = await screen.findByRole("switch", { name: "Notify me without an open tab" });
    await waitFor(() => expect(toggle).toBeEnabled());
    expect(toggle).not.toBeChecked();

    await user.click(toggle);

    await waitFor(() => expect(toggle).toBeChecked());
    expect(push.pushManager.subscribe).toHaveBeenCalledWith(
      expect.objectContaining({ userVisibleOnly: true }),
    );
    expect(api.registered).toEqual([
      {
        endpoint: push.subscription.endpoint,
        keys: { p256dh: "BTestP256dh", auth: "TestAuth" },
      },
    ]);
    expect(await screen.findByText("This device")).toBeInTheDocument();
  });

  it("cannot be switched on while notifications are off", async () => {
    setup({ webPush: available([]) });
    stubNotifications({ permission: "granted" });
    stubPushManager();
    await renderApp("/settings/notifications");

    const toggle = await screen.findByRole("switch", { name: "Notify me without an open tab" });
    await waitFor(() => expect(screen.getByText("No device set up yet.")).toBeInTheDocument());
    expect(toggle).toBeDisabled();
  });

  it("lists the devices and removes one", async () => {
    const other = testPushDevice({
      id: "0199e000-0000-7000-8000-00000000d002",
      browser: "Chrome",
      os: "Android",
      mobile: true,
      push_service: "fcm.googleapis.com",
    });
    const api = setup({
      settings: testNotificationSettings({ enabled: true }),
      webPush: available([testPushDevice(), other]),
    });
    stubNotifications({ permission: "granted" });
    stubPushManager();
    const user = userEvent.setup();
    await renderApp("/settings/notifications");

    const devices = await screen.findByRole("list", { name: "Devices" });
    expect(devices).toHaveTextContent("Firefox on Linux");
    expect(devices).toHaveTextContent("via fcm.googleapis.com");

    await user.click(screen.getByRole("button", { name: "Remove Chrome on Android" }));

    await waitFor(() => expect(api.removed).toEqual([other.id]));
    await waitFor(() => expect(screen.queryByText("Chrome on Android")).not.toBeInTheDocument());
  });

  it("refreshes the registration of a subscribed browser and switches it off", async () => {
    const api = setup({
      settings: testNotificationSettings({ enabled: true }),
      webPush: available([]),
    });
    stubNotifications({ permission: "granted" });
    const push = stubPushManager({ subscribed: true });
    const user = userEvent.setup();
    await renderApp("/settings/notifications");

    const toggle = await screen.findByRole("switch", { name: "Notify me without an open tab" });
    await waitFor(() => expect(toggle).toBeChecked());
    expect(api.registered).toHaveLength(1);

    await user.click(toggle);

    await waitFor(() => expect(toggle).not.toBeChecked());
    expect(push.subscription.unsubscribe).toHaveBeenCalled();
    expect(api.removed).toEqual(["0199e000-0000-7000-8000-00000000d0ff"]);
  });

  it("forgets this device when signing out", async () => {
    const api = setup({ webPush: available([testPushDevice()]) });
    const push = stubPushManager({ subscribed: true });
    window.localStorage.setItem("ollamail.pushDeviceId", testPushDevice().id);

    await forgetPushDevice();

    expect(api.removed).toEqual([testPushDevice().id]);
    expect(push.subscription.unsubscribe).toHaveBeenCalled();
    expect(window.localStorage.getItem("ollamail.pushDeviceId")).toBeNull();
  });
});

import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { backend, mockFetch, testUser } from "@/test/fetch";
import {
  notificationsApi,
  stubNotifications,
  testNotificationSettings,
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

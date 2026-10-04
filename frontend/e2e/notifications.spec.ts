import { expect, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockApi } from "./mock-api";
import {
  mockNotificationEvent,
  mockNotifications,
  mockPushManager,
  NOTIFIED_MESSAGE_ID,
  notificationCategories,
  otherDevice,
  recordNotifications,
  THIS_DEVICE_ID,
} from "./mock-notifications";

// Runs without a backend: notification settings and server events are mocked in the browser.

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test("opt in: the browser asks first, important categories are preselected", async ({
  page,
  context,
}) => {
  await context.grantPermissions(["notifications"]);
  const { updates } = await mockNotifications(page);
  await page.goto("/settings");
  await page.getByRole("link", { name: /Set up notifications/ }).click();

  const enabled = page.getByRole("switch", { name: "Notify me about new mail" });
  await expect(enabled).not.toBeChecked();
  await expect(page.getByRole("switch", { name: "Show subject" })).not.toBeChecked();
  await expect(page.getByRole("switch", { name: "Sound" })).not.toBeChecked();
  await enabled.click();

  await expect(enabled).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Important" })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Action required" })).toBeChecked();
  await expect(page.getByRole("checkbox", { name: "Newsletter" })).not.toBeChecked();
  await expect(page.getByText("Notifications are allowed.")).toBeVisible();
  expect(updates).toEqual([
    {
      enabled: true,
      category_ids: [notificationCategories[0]?.id, notificationCategories[1]?.id],
    },
  ]);
  await expectNoA11yViolations(page);
});

test("a new important mail is announced with sender and category only", async ({ page }) => {
  await recordNotifications(page);
  await mockNotifications(page, {
    enabled: true,
    categoryIds: [notificationCategories[1]?.id ?? ""],
  });
  await mockNotificationEvent(page);
  await page.goto("/settings");

  await expect
    .poll(() =>
      page.evaluate(() => (window as unknown as { __notifications: unknown[] }).__notifications),
    )
    .toEqual([
      {
        title: "Erika Musterfrau",
        body: "Action required",
        silent: true,
        tag: `ollamail-message-${NOTIFIED_MESSAGE_ID}`,
      },
    ]);
});

test("switched off by the administrator", async ({ page }) => {
  await mockNotifications(page, { available: false });
  await page.goto("/settings/notifications");

  await expect(page.getByText("Notifications not enabled")).toBeVisible();
  await expect(page.getByRole("switch")).toHaveCount(0);
});

test("web push: opt in on this device, list devices, remove them", async ({ page, context }) => {
  await context.grantPermissions(["notifications"]);
  const { registered, removed } = await mockNotifications(page, {
    enabled: true,
    categoryIds: [notificationCategories[0]?.id ?? ""],
    webPush: { devices: [otherDevice] },
  });
  await mockPushManager(page);
  await page.goto("/settings/notifications");

  // The push service is named before switching on.
  await expect(page.getByText(/Chrome: Google, Firefox: Mozilla/)).toBeVisible();
  const devices = page.getByRole("list", { name: "Devices" });
  await expect(devices.getByRole("listitem")).toHaveCount(1);
  await expect(devices).toContainText("Chrome on Android");

  const toggle = page.getByRole("switch", { name: "Notify me without an open tab" });
  await expect(toggle).toBeEnabled();
  await toggle.click();

  await expect(toggle).toBeChecked();
  await expect(devices.getByRole("listitem")).toHaveCount(2);
  await expect(devices.getByRole("listitem").first()).toContainText("This device");
  expect(registered).toEqual([
    {
      endpoint: "https://fcm.googleapis.com/fcm/send/e2e-device",
      keys: { p256dh: "BE2eP256dh", auth: "E2eAuth" },
    },
  ]);
  await expectNoA11yViolations(page);

  await page.getByRole("button", { name: "Remove Chrome on Android" }).click();
  await expect(devices.getByRole("listitem")).toHaveCount(1);

  await toggle.click();
  await expect(toggle).not.toBeChecked();
  await expect(page.getByText("No device set up yet.")).toBeVisible();
  expect(removed).toEqual([otherDevice.id, THIS_DEVICE_ID]);
});

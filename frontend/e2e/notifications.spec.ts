import { expect, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockApi } from "./mock-api";
import {
  mockNotificationEvent,
  mockNotifications,
  NOTIFIED_MESSAGE_ID,
  notificationCategories,
  recordNotifications,
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

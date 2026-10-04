import { expect, test } from "@playwright/test";

import { mockAdmin } from "./mock-admin";
import { mockApi } from "./mock-api";
import { mockDigest } from "./mock-digest";
import { mockMail } from "./mock-mail";
import { mockSearch, QUESTION } from "./mock-search";

// Runs without a backend: the API is mocked (synthetic data).
// Error states with a next action, notices on narrow screens, segmented controls and the
// search answer in the detail pane (#193).

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await mockMail(page);
});

test("a list that failed to load loads again with Try again", async ({ page }) => {
  let failing = true;
  await page.route(
    (url) => url.pathname === "/api/messages",
    (route) =>
      failing
        ? route.fulfill({
            status: 404,
            contentType: "application/problem+json",
            json: { type: "about:blank", title: "Not Found", status: 404 },
          })
        : route.fallback(),
  );

  await page.goto("/inbox");
  const error = page.getByRole("alert");
  await expect(error).toBeVisible();
  failing = false;
  await error.getByRole("button", { name: "Try again" }).click();

  await expect(error).toBeHidden();
  await expect(
    page.getByRole("list", { name: "Messages" }).getByRole("link").first(),
  ).toBeVisible();
});

test("mobile: notices fold into one line that expands", async ({ page }) => {
  await mockAdmin(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/inbox");

  // Cloud AI and a mailbox that cannot sync (everyone), the missing model (admins).
  const notices = page.getByRole("complementary", { name: "Notices" });
  const toggle = notices.getByRole("button", { name: "3 notices" });
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(notices.getByText("Cloud AI active")).toBeHidden();
  const collapsed = await notices.boundingBox();
  expect(collapsed?.height).toBeLessThanOrEqual(32);

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  await expect(notices.getByText("Cloud AI active")).toBeVisible();
  await expect(notices.getByText("Mailbox not syncing")).toBeVisible();
  await expect(notices.getByText("Model missing")).toBeVisible();
});

test("the cloud notice can be hidden for the session", async ({ page }) => {
  await mockAdmin(page);
  await page.goto("/inbox");

  const cloud = page.getByRole("complementary", { name: "Cloud AI active" });
  await expect(cloud).toContainText("Example Cloud receives mail content for: Digest");
  await cloud.getByRole("button", { name: "Hide cloud notice" }).click();
  await expect(cloud).toBeHidden();
  // Warnings cannot be hidden.
  await expect(page.getByRole("complementary", { name: "Model missing" })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("complementary", { name: "Model missing" })).toBeVisible();
  await expect(cloud).toBeHidden();
});

test("the selected options of a segmented control are outlined", async ({ page }) => {
  await mockDigest(page);
  await page.goto("/digest/settings");

  const weekdays = page.getByRole("group", { name: "Days" });
  const on = weekdays.locator("[aria-pressed=true]").first();
  const off = weekdays.locator("[aria-pressed=false]").first();
  await expect(on).toBeVisible();
  await expect(off).toHaveCSS("border-top-color", "rgba(0, 0, 0, 0)");
  const onBorder = await on.evaluate((element) => getComputedStyle(element).borderTopColor);
  expect(onBorder).not.toBe("rgba(0, 0, 0, 0)");
});

test("side by side, the answer is shown in the detail pane until a mail is opened", async ({
  page,
}) => {
  await mockSearch(page);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/search");
  const searchbox = page.getByRole("searchbox", { name: "Search terms or question" });
  await searchbox.fill(QUESTION);
  await searchbox.press("Enter");

  const answer = page.getByRole("article", { name: QUESTION });
  await expect(answer.getByRole("region", { name: "Sources" })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Answers" })).toBeVisible();
  await expect(page.getByText("No message selected")).toBeHidden();
  const wide = await answer.boundingBox();
  expect(wide?.width).toBeGreaterThan(640);

  await answer.getByRole("button", { name: "Open source 1" }).click();
  await expect(page).toHaveURL(/message=/);
  // The mail takes the detail pane; the answer stays visible in the list.
  await expect(answer).toBeVisible();
  await expect(page.getByRole("heading", { level: 2, name: "Answers" })).toBeHidden();
});

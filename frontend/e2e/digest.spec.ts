import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { mockApi } from "./mock-api";
import { FEED_URL, mockDigest } from "./mock-digest";
import { mockMail } from "./mock-mail";

test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await mockMail(page);
});

async function expectNoA11yViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(results.violations.map((violation) => violation.id)).toEqual([]);
}

test("shows the current digest with player and transcript links", async ({ page }) => {
  await mockDigest(page);
  await page.goto("/digest");

  await expect(page.getByRole("heading", { name: "Current" })).toBeVisible();
  await expect(page.getByRole("region", { name: "Playback" })).toBeVisible();
  const transcript = page.getByRole("region", { name: "Transcript" });
  await transcript.getByRole("link", { name: "Open mail 2" }).click();
  await expect(page).toHaveURL(/\/inbox\?message=/);
});

test("creates, copies and replaces the feed URL", async ({ page }) => {
  await mockDigest(page);
  await page.goto("/digest/settings");

  await page.getByRole("button", { name: "Create feed URL" }).click();
  await expect(page.getByRole("textbox", { name: "Feed URL" })).toHaveValue(FEED_URL);
  await expect(page.getByRole("img", { name: "QR code of the feed URL" })).toBeVisible();

  await page.getByRole("button", { name: "Create new URL" }).click();
  await expect(page.getByRole("alert")).toContainText("stops working immediately");
});

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 390, height: 844 },
  ]) {
    test(`digest pages have no axe violations (${colorScheme}, ${viewport.width}px)`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      await page.emulateMedia({ colorScheme });
      await mockDigest(page);

      await page.goto("/digest");
      await expect(page.getByRole("heading", { name: "Current" })).toBeVisible();
      await expectNoA11yViolations(page);

      await page.goto(`/digest?digest=${"0192d000-0000-7000-8000-000000000000"}`);
      await expect(page.getByRole("region", { name: "Transcript" })).toBeVisible();
      await expectNoA11yViolations(page);

      await page.goto("/digest/settings");
      await page.getByRole("button", { name: "Create feed URL" }).click();
      await expect(page.getByRole("img", { name: "QR code of the feed URL" })).toBeVisible();
      await expectNoA11yViolations(page);
      const overflow = await page.evaluate(
        () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
      );
      expect(overflow).toBe(0);
    });
  }
}

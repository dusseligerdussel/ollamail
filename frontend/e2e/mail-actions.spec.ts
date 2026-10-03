import { expect, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockApi } from "./mock-api";
import { messageId, mockMail } from "./mock-mail";

// Mail actions (#148) without a backend: API and mail data are mocked in the browser.
test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await mockMail(page, { messages: 20 });
});

test("archive with e, undo from the toast, flag with s", async ({ page }) => {
  await page.goto(`/inbox?message=${messageId(0)}`);
  const list = page.getByRole("list", { name: "Messages" });
  const first = list.locator(`a[href$="message=${messageId(0)}"]`);
  await expect(first).toBeVisible();
  await expect(page.getByRole("button", { name: "Archive" })).toBeVisible();

  await page.keyboard.press("s");
  await expect(page.getByRole("button", { name: "Flag", pressed: true })).toBeVisible();
  await expect(first.getByRole("img", { name: "Flagged" })).toBeVisible();

  await page.keyboard.press("e");
  // The next message opens, the archived one leaves the list.
  await expect(page).toHaveURL(new RegExp(`message=${messageId(1)}`));
  await expect(first).toBeHidden();
  const toast = page.getByText("Archived", { exact: true });
  await expect(toast).toBeVisible();

  await page.getByRole("button", { name: "Undo" }).click();
  await expect(first).toBeVisible();
});

test("move via the menu and trash via the command palette", async ({ page }) => {
  await page.goto(`/inbox?message=${messageId(0)}`);
  const list = page.getByRole("list", { name: "Messages" });
  await expect(page.getByRole("button", { name: "Move", exact: true })).toBeVisible();

  await page.keyboard.press("v");
  const menu = page.getByRole("menu");
  await expect(menu.getByRole("menuitem", { name: "Inbox" })).toBeVisible();
  await expectNoA11yViolations(page);
  await menu.getByRole("menuitem", { name: "Archiv" }).click();
  await expect(page.getByText("Moved", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`message=${messageId(1)}`));

  await page.keyboard.press("ControlOrMeta+k");
  await page.getByRole("option", { name: "Move to trash" }).click();
  await expect(page.getByText("Moved to trash", { exact: true })).toBeVisible();
  await expect(page).toHaveURL(new RegExp(`message=${messageId(2)}`));
  await expect(list.getByRole("listitem")).toHaveCount(18);
});

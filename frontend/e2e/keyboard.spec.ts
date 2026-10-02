import { expect, type Page, test } from "@playwright/test";

import { mockApi } from "./mock-api";
import { mockDigest } from "./mock-digest";
import { mockMail, NOW } from "./mock-mail";
import { mockTodos } from "./mock-todos";

// Keyboard operation and focus handling across the app (#123, WCAG 2.1.1, 2.4.3, 2.4.7, 4.1.3).
// Runs without a backend: the API is mocked in the browser (synthetic data).
test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(NOW);
  await mockApi(page);
  await mockMail(page);
  await mockDigest(page);
});

async function shortcutOverview(page: Page) {
  await page.keyboard.press("?");
  const overview = page.getByRole("dialog", { name: /Keyboard shortcuts|Tastenkürzel/ });
  await expect(overview).toBeVisible();
  const descriptions = await overview.locator("dt").allTextContents();
  await page.keyboard.press("Escape");
  await expect(overview).toBeHidden();
  return descriptions;
}

test("the skip link moves focus to the main content", async ({ page }) => {
  await page.goto("/tasks");
  await expect(page.getByRole("heading", { level: 1, name: "Tasks" })).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("link", { name: "Skip to content" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.locator("#main")).toBeFocused();
  // The next Tab starts in the content, not in the navigation.
  await page.keyboard.press("Tab");
  expect(
    await page.evaluate(() => document.querySelector("#main")?.contains(document.activeElement)),
  ).toBe(true);
});

test("Tab leaves the virtualised mail list; arrows and j/k move the focus inside", async ({
  page,
}) => {
  await page.goto("/inbox");
  const list = page.getByRole("list", { name: "Messages" });
  await expect(list.getByRole("link").first()).toBeVisible();

  // Exactly one row is in the tab order.
  await list.getByRole("link").first().focus();
  const rows = list.getByRole("link");
  expect(
    await rows.evaluateAll((links) => links.filter((link) => link.tabIndex === 0).length),
  ).toBe(1);

  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowDown");
  await expect(list.locator('[data-message-index="2"]')).toBeFocused();
  await page.keyboard.press("k");
  await expect(list.locator('[data-message-index="1"]')).toBeFocused();
  await page.keyboard.press("j");
  await expect(list.locator('[data-message-index="2"]')).toBeFocused();

  // Tab leaves the list, Shift+Tab comes back to the same row.
  await page.keyboard.press("Tab");
  expect(await page.evaluate(() => !!document.activeElement?.closest("[data-message-index]"))).toBe(
    false,
  );
  await page.keyboard.press("Shift+Tab");
  await expect(list.locator('[data-message-index="2"]')).toBeFocused();

  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 2, name: /.+/ }).first()).toBeVisible();
  await expect(page).toHaveURL(/message=/);
});

test("e marks the active task as done", async ({ page }) => {
  const { store } = await mockTodos(page);
  const done = () => [...store.values()].filter((todo) => todo.status === "done").length;
  const before = done();
  await page.goto("/tasks");
  await expect(page.getByRole("region", { name: /^Overdue/ })).toBeVisible();
  await page.keyboard.press("j");
  await page.keyboard.press("e");
  await expect.poll(done).toBe(before + 1);
  // `e` never reopens: pressed on a done task it does nothing (`x` toggles).
});

test("shortcuts do not fire in text fields", async ({ page }) => {
  await mockTodos(page);
  await page.goto("/tasks");
  const input = page.getByRole("textbox", { name: "New task" });
  await input.focus();
  await page.keyboard.type("jkexd?gi");
  await expect(input).toHaveValue("jkexd?gi");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page).toHaveURL(/\/tasks$/);
});

test("the shortcut overview lists the shortcuts of each page, translated", async ({ page }) => {
  await mockTodos(page);
  const pages = [
    ["/inbox", ["Next item", "Previous item", "Open item", "Command menu"]],
    ["/tasks", ["Mark task as done", "New task", "Change due date"]],
  ] as const;
  for (const [path, expected] of pages) {
    await page.goto(path);
    await expect(
      page.getByRole("main").getByRole("link").or(page.getByRole("region")).first(),
    ).toBeVisible();
    // Actions on an item are listed once an item is active.
    await page.keyboard.press("j");
    const descriptions = await shortcutOverview(page);
    for (const text of expected) expect(descriptions, path).toContain(text);
  }

  // German: every entry is translated (no raw keys such as `tasks.shortcuts.done`).
  await page.evaluate(() => window.localStorage.setItem("ollamail.language", "de"));
  for (const [path] of pages) {
    await page.goto(path);
    await expect(
      page.getByRole("main").getByRole("link").or(page.getByRole("region")).first(),
    ).toBeVisible();
    await page.keyboard.press("j");
    const descriptions = await shortcutOverview(page);
    expect(descriptions, path).toContain(
      path === "/tasks" ? "Aufgabe erledigen" : "Nächster Eintrag",
    );
    for (const description of descriptions) {
      expect(description, path).not.toMatch(/^[\w-]+(\.[\w-]+)+$/);
    }
  }
});

test("dialogs and sheets trap the focus and return it to their trigger", async ({ page }) => {
  await page.goto("/inbox");
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();

  const triggers = [
    { trigger: page.getByRole("button", { name: /^Command menu/ }), dialog: "Command menu" },
    {
      trigger: page.getByRole("button", { name: /^Keyboard shortcuts/ }),
      dialog: "Keyboard shortcuts",
    },
  ];
  for (const { trigger, dialog } of triggers) {
    await trigger.focus();
    await page.keyboard.press("Enter");
    const opened = page.getByRole("dialog", { name: dialog });
    await expect(opened).toBeVisible();
    for (let step = 0; step < 8; step++) {
      await page.keyboard.press("Tab");
      expect(
        await opened.evaluate((element) => element.contains(document.activeElement)),
        `${dialog}: focus left the dialog`,
      ).toBe(true);
    }
    await page.keyboard.press("Escape");
    await expect(opened).toBeHidden();
    await expect(trigger).toBeFocused();
  }

  // A confirmation dialog on the settings page.
  await page.goto("/settings");
  const remove = page.getByRole("button", { name: "Delete account…" });
  await remove.focus();
  await page.keyboard.press("Enter");
  const confirm = page.getByRole("dialog");
  await expect(confirm).toBeVisible();
  expect(await confirm.evaluate((element) => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(remove).toBeFocused();
});

test("every page has one h1, no skipped heading levels and its own title", async ({ page }) => {
  await mockTodos(page);
  for (const [path, title] of [
    ["/inbox", "Inbox – ollamail"],
    ["/tasks", "Tasks – ollamail"],
    ["/digest", "Digest – ollamail"],
    ["/search", "Search – ollamail"],
    ["/settings", "Settings – ollamail"],
    ["/admin", "Admin – ollamail"],
  ] as const) {
    await page.goto(path);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
    await expect(page).toHaveTitle(title);
    const levels = await page.evaluate(() =>
      [...document.querySelectorAll("h1, h2, h3, h4, h5, h6")]
        .filter((heading) => (heading as HTMLElement).offsetParent !== null)
        .map((heading) => Number(heading.tagName[1])),
    );
    expect(
      levels.filter((level) => level === 1),
      path,
    ).toHaveLength(1);
    levels.forEach((level, index) => {
      expect(level - (levels[index - 1] ?? 0), `${path}: ${levels.join(",")}`).toBeLessThanOrEqual(
        1,
      );
    });
  }
});

test("status changes are announced through live regions", async ({ page }) => {
  await page.goto("/settings/mailboxes");
  await expect(page.getByRole("heading", { level: 1, name: "Mailboxes" })).toBeVisible();
  // The sync status of each mailbox (updated live through server events).
  await expect(
    page
      .getByRole("status")
      .filter({ hasText: /Up to date|Syncing|Importing|Paused|Error/ })
      .first(),
  ).toBeVisible();
  // Toasts: sonner's region is announced politely.
  await expect(page.locator("section[aria-live='polite']")).toBeAttached();
});

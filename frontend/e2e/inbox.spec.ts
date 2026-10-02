import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { mockApi } from "./mock-api";
import { messageId, mockMail } from "./mock-mail";

// Runs without a backend: API and mail data are mocked in the browser (synthetic data).
test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

async function expectNoA11yViolations(page: Page) {
  // The mail itself is third-party content in a script-less sandbox, where axe cannot run.
  const results = await new AxeBuilder({ page })
    .exclude("iframe")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const summary = results.violations.map(
    (violation) =>
      `${violation.impact}: ${violation.id} – ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
  expect(summary).toEqual([]);
}

async function overflow(page: Page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

test("10,000 messages scroll smoothly", async ({ page }) => {
  await mockMail(page, { messages: 10_000 });
  await page.goto("/inbox");
  const list = page.getByRole("list", { name: "Messages" });
  await expect(list.getByRole("listitem").first()).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
  await expect(page.getByText("10,000", { exact: true })).toBeVisible();

  // Scroll through the whole list in steps and record frame times meanwhile.
  const result = await page.evaluate(async () => {
    const scroller = document.querySelector<HTMLElement>("[data-testid=message-list]");
    if (!scroller) throw new Error("no list");
    const frames: number[] = [];
    let last = performance.now();
    let running = true;
    const tick = (now: number) => {
      frames.push(now - last);
      last = now;
      if (running) requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
    const maxRows = { value: 0 };
    for (let step = 0; step < 400; step += 1) {
      scroller.scrollTop += scroller.clientHeight;
      await new Promise((resolve) => requestAnimationFrame(resolve));
      maxRows.value = Math.max(maxRows.value, scroller.querySelectorAll("li").length);
      if (scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1) {
        // Wait for the next page, then keep going.
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    running = false;
    const sorted = [...frames].sort((a, b) => a - b);
    return {
      rows: maxRows.value,
      p95: sorted[Math.floor(sorted.length * 0.95)] ?? 0,
      scrollHeight: scroller.scrollHeight,
    };
  });
  // Only the visible rows (plus overscan) are in the DOM.
  expect(result.rows).toBeLessThan(80);
  // The list is as tall as all 10,000 rows.
  expect(result.scrollHeight).toBeGreaterThanOrEqual(10_000 * 36);
  // Typically ~16 ms; generous for slow CI machines.
  expect(result.p95).toBeLessThan(100);

  // The end of the list loads and shows the oldest message.
  await page.evaluate(() => {
    const scroller = document.querySelector<HTMLElement>("[data-testid=message-list]");
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
  });
  await expect(list.locator('li[aria-posinset="10000"] a')).toBeVisible({ timeout: 15_000 });
});

test("keyboard: j/k, Enter, u and Esc", async ({ page }) => {
  await mockMail(page);
  await page.goto("/inbox");
  await expect(page.getByRole("list", { name: "Messages" })).toBeVisible();

  await page.keyboard.press("j");
  await page.keyboard.press("j");
  await page.keyboard.press("k");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`message=${messageId(0)}`));
  await expect(
    page.getByRole("heading", { level: 1, name: "Abstimmung Quartalsplanung" }),
  ).toBeVisible();
  // Opening marked it read; u marks it unread again.
  await expect(page.getByRole("button", { name: "Mark as unread" })).toBeVisible();
  await page.keyboard.press("u");
  await expect(page.getByRole("button", { name: "Mark as read" })).toBeVisible();

  await page.keyboard.press("j");
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`message=${messageId(1)}`));
  await page.keyboard.press("Escape");
  await expect(page).not.toHaveURL(/message=/);

  // Actions are in the command palette.
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.getByRole("option", { name: "Show only unread messages" })).toBeVisible();
});

test("external images load only after a click", async ({ page }) => {
  await mockMail(page);
  const external: string[] = [];
  page.on("request", (request) => {
    if (!request.url().startsWith("http://localhost")) external.push(request.url());
  });
  await page.goto(`/inbox?message=${messageId(12)}`);
  await expect(
    page.getByText("3 external images were blocked to protect your privacy."),
  ).toBeVisible();
  const frame = page.frameLocator("iframe");
  await expect(frame.getByText("Herbstaktion im Fahrradladen")).toBeVisible();
  // Scripts never run inside the mail.
  expect(await page.locator("iframe").getAttribute("sandbox")).not.toContain("allow-scripts");

  await page.getByRole("button", { name: "Load images" }).click();
  await expect(page.getByText(/external images were blocked/)).toHaveCount(0);
  expect(external).toEqual([]);
});

test("mobile: list and message are stacked", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockMail(page);
  await page.goto("/inbox");
  await page.getByRole("list", { name: "Messages" }).getByRole("link").first().click();
  await expect(page.getByRole("button", { name: "Back to the list" })).toBeVisible();
  await expect(page.getByRole("list", { name: "Messages" })).toHaveCount(0);
  expect(await overflow(page)).toBe(0);
  await page.getByRole("button", { name: "Back to the list" }).click();
  await expect(page.getByRole("list", { name: "Messages" })).toBeVisible();
});

test("add an IMAP mailbox with autodiscovery and connection test", async ({ page }) => {
  await mockMail(page, { oauth: ["gmail"] });
  await page.goto("/settings/mailboxes/new");
  await expect(page.getByRole("radio", { name: /Google/ })).toBeVisible();
  await expect(page.getByRole("radio", { name: /Microsoft 365/ })).toHaveCount(0);

  await page.getByLabel("Email address").fill("erika@firma.example");
  await page.getByLabel("Password", { exact: true }).fill("wrong");
  await page.getByLabel("Password", { exact: true }).press("Tab");
  await expect(page.getByLabel("IMAP server")).toHaveValue("imap.example.org");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText("Sign-in failed. Check the user name and password.")).toBeVisible();

  await page.getByLabel("Password", { exact: true }).fill("secret");
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText("Connection works, 4 folders found.")).toBeVisible();
  await page.getByRole("button", { name: "Add mailbox" }).click();
  await expect(page).toHaveURL(/\/settings\/mailboxes$/);
});

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 360, height: 740 },
  ]) {
    test(`mail views have no axe violations (${colorScheme}, ${viewport.width}px)`, async ({
      page,
    }) => {
      test.setTimeout(120_000);
      await page.emulateMedia({ colorScheme });
      await page.setViewportSize(viewport);
      await mockMail(page, { oauth: ["gmail", "graph"] });
      for (const path of [
        "/inbox",
        `/inbox?message=${messageId(12)}`,
        `/inbox?message=${messageId(6)}`,
        "/settings/mailboxes",
        "/settings/mailboxes/new",
      ]) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.waitForLoadState("networkidle");
        await expectNoA11yViolations(page);
        expect(await overflow(page), path).toBe(0);
      }

      await page.getByRole("button", { name: "Add mailbox" }).click();
      await expect(page.getByText("Enter an email address.")).toBeVisible();
      await expectNoA11yViolations(page);

      await page.goto("/settings/mailboxes");
      // (Open Radix menus hide the page with aria-hidden while the skip link stays focusable;
      // axe flags that for every menu of the app, so the open menu itself is not checked.)
      await page.getByRole("button", { name: "Actions for Arbeit" }).click();
      await page.getByRole("menuitem", { name: "Folders" }).click();
      await expect(page.getByRole("dialog", { name: "Folders" })).toBeVisible();
      await expect(page.getByRole("menu")).toHaveCount(0);
      await page.waitForTimeout(300); // sheet transition
      await expectNoA11yViolations(page);
    });
  }
}

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { mockApi } from "./mock-api";
import { mockMail, NOW } from "./mock-mail";
import { mockTodos, offerMessage, todoId } from "./mock-todos";

// Runs without a backend: API, mails and tasks are mocked in the browser (synthetic data).
test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime(NOW);
  await mockApi(page);
  await mockMail(page);
});

async function expectNoA11yViolations(page: Page) {
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

function group(page: Page, name: string) {
  return page.getByRole("region", { name: new RegExp(`^${name}`) });
}

test("check off a task, change its date and jump to its mail", async ({ page }) => {
  const { store } = await mockTodos(page);
  await page.goto("/tasks");
  await expect(group(page, "Overdue")).toBeVisible();

  // Check off: the task moves to "Done" and the server has it done.
  const title = "Termin für Quartalsplanung bestätigen";
  await page.getByRole("checkbox", { name: `Mark “${title}” as done` }).click();
  await expect(group(page, "Done").getByRole("button", { name: title, exact: true })).toBeVisible();
  await expect(group(page, "Today").getByRole("button", { name: title, exact: true })).toHaveCount(
    0,
  );
  await expect.poll(() => store.get(todoId(3))?.status).toBe("done");

  // Change the date: tomorrow moves the overdue task to "Upcoming".
  const offer = "Anmerkungen zum Angebot einarbeiten";
  await group(page, "Overdue").getByRole("button", { name: /^Due / }).first().click();
  await page
    .getByRole("dialog", { name: "Due date" })
    .getByRole("button", { name: /^Tomorrow/ })
    .click();
  await expect(
    group(page, "Upcoming").getByRole("button", { name: offer, exact: true }),
  ).toBeVisible();
  await expect.poll(() => store.get(todoId(1))?.due_date).toBe("2026-10-03");

  // Jump to the source mail.
  await page.getByRole("link", { name: `Open message of “${offer}”` }).click();
  await expect(page).toHaveURL(new RegExp(`/inbox\\?message=${offerMessage}`));
  await expect(page.getByRole("heading", { level: 1, name: "Entwurf Angebot" })).toBeVisible();
  // The mail lists its task.
  const tasks = page.getByRole("region", { name: "Tasks from this message" });
  await expect(tasks.getByRole("button", { name: offer, exact: true })).toBeVisible();
});

test("keyboard: n, j/k, x, d, Enter and o", async ({ page }) => {
  const { store } = await mockTodos(page);
  await page.goto("/tasks");
  await expect(group(page, "Overdue")).toBeVisible();

  await page.keyboard.press("n");
  await expect(page.getByRole("textbox", { name: "New task" })).toBeFocused();
  await page.keyboard.type("Blumen gießen");
  await page.keyboard.press("Enter");
  await expect(
    group(page, "No date").getByRole("button", { name: "Blumen gießen", exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");

  await page.keyboard.press("j");
  await expect(
    page.getByRole("button", { name: "Anmerkungen zum Angebot einarbeiten", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("j");
  await page.keyboard.press("x");
  await expect.poll(() => store.get(todoId(2))?.status).toBe("done");

  // Enter on the focused title edits it (the whole title is selected).
  await page.keyboard.press("k");
  await page.keyboard.press("Enter");
  await page.keyboard.type("Angebot überarbeiten");
  await page.keyboard.press("Enter");
  await expect.poll(() => store.get(todoId(1))?.title).toBe("Angebot überarbeiten");

  await page.keyboard.press("d");
  const picker = page.getByRole("dialog", { name: "Due date" });
  await expect(picker.getByRole("button", { name: /^Today/ })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(picker).toHaveCount(0);
  await expect.poll(() => store.get(todoId(1))?.due_date).toBe("2026-10-02");

  await page.keyboard.press("o");
  await expect(page).toHaveURL(new RegExp(`message=${offerMessage}`));
});

test("empty state", async ({ page }) => {
  await mockTodos(page, { todos: false });
  await page.goto("/tasks");
  await expect(page.getByRole("heading", { name: "No tasks" })).toBeVisible();
  await expect(page.getByRole("textbox", { name: "New task" })).toBeVisible();
});

test("mobile: tasks fit the screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockTodos(page);
  await page.goto("/tasks");
  await expect(group(page, "Overdue")).toBeVisible();
  expect(await overflow(page)).toBe(0);
  // Row actions are visible without hover on touch screens; here at least reachable.
  await expect(page.getByRole("button", { name: "Dismiss “Fahrradlicht kaufen”" })).toBeAttached();
});

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 360, height: 740 },
  ]) {
    test(`task views have no axe violations (${colorScheme}, ${viewport.width}px)`, async ({
      page,
    }) => {
      test.setTimeout(120_000);
      await page.emulateMedia({ colorScheme });
      await page.setViewportSize(viewport);
      await mockTodos(page);
      for (const path of ["/tasks", `/inbox?message=${offerMessage}`]) {
        await page.goto(path);
        await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
        await page.waitForLoadState("networkidle");
        await expectNoA11yViolations(page);
        expect(await overflow(page), path).toBe(0);
      }

      await page.goto("/tasks");
      await group(page, "Today").getByRole("button", { name: /^Due / }).first().click();
      await expect(page.getByRole("dialog", { name: "Due date" })).toBeVisible();
      await expectNoA11yViolations(page);
    });
  }
}

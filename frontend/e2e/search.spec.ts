import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { mockApi } from "./mock-api";
import { mockMail } from "./mock-mail";
import {
  conversationId,
  mockSearch,
  NO_EVIDENCE_QUESTION,
  QUESTION,
  searchMessageId,
} from "./mock-search";

// Runs without a backend: API, mails and the answer stream are mocked (synthetic data).
test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await mockMail(page);
});

async function expectNoA11yViolations(page: Page) {
  const results = await new AxeBuilder({ page })
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

const searchbox = (page: Page) => page.getByRole("searchbox", { name: "Search terms or question" });

test("keyboard only: search, filter, open a hit at the matching passage", async ({ page }) => {
  const { searches } = await mockSearch(page);
  await page.goto("/inbox");
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();

  // `/` opens the search with the cursor in the field.
  await page.keyboard.press("/");
  await expect(page.getByRole("heading", { level: 1, name: "Search" })).toBeVisible();
  await expect(searchbox(page)).toBeFocused();
  await page.keyboard.type("rechnung");
  await page.keyboard.press("Enter");

  const hits = page.getByRole("list", { name: "Results" });
  await expect(hits.getByRole("listitem")).toHaveCount(4);
  await expect(hits.getByRole("listitem").first().locator("mark").first()).toHaveText("Rechnung");
  await expect(hits.getByText("Belege-2026.pdf")).toBeVisible();
  // The receipt is a scan: its text was recognised (OCR).
  await expect(hits.getByRole("listitem").nth(3).getByText("(OCR)")).toBeVisible();

  // Filter chip by keyboard: Tab to "Date", open, choose.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Date" })).toBeFocused();
  await page.keyboard.press("Enter");
  await page.getByRole("menuitemradio", { name: "Last 30 days" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Date: Last 30 days" })).toBeVisible();
  await expect.poll(() => searches.length).toBe(2);
  expect(searches[1]).toMatchObject({ query: "rechnung", filters: { since: expect.any(String) } });

  // Sender chip (after the clear button of the date chip): edited inline; Enter applies it.
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Remove filter “Date”" })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(page.getByRole("button", { name: "Sender" })).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("textbox", { name: "Sender" })).toBeFocused();
  await page.keyboard.type("billing");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Sender: billing" })).toBeVisible();
  await expect.poll(() => searches.length).toBe(3);
  expect(searches[2]).toMatchObject({ filters: { sender: "billing" } });
  await expect(hits.getByRole("listitem")).toHaveCount(4);

  // j / o open the first hit (Enter would act on the focused chip); the passage is marked.
  await page.keyboard.press("j");
  await page.keyboard.press("o");
  await expect(page).toHaveURL(new RegExp(`message=${searchMessageId(0)}`));
  const mark = page.locator("article mark");
  await expect(mark).toContainText("zahlbar innerhalb von 14 Tagen");
  await expect(mark).toBeInViewport();
  // The query never appears in the URL.
  expect(page.url()).not.toContain("rechnung");

  await page.keyboard.press("Escape");
  await expect(page).not.toHaveURL(/message=/);
});

test("streams an answer with numbered sources; a citation opens the mail", async ({ page }) => {
  const { asks } = await mockSearch(page, { step: 40 });
  await page.goto("/search");
  await searchbox(page).fill(QUESTION);
  await searchbox(page).press("Enter");

  const answer = page.getByRole("article", { name: QUESTION });
  // While the answer streams it can be cancelled; the text grows piece by piece.
  await expect(answer.getByRole("button", { name: /Cancel/ })).toBeVisible();
  await expect(answer).toContainText("Die Rechnung");
  await expect(answer).toContainText("Verwendungszweck anzugeben");
  await expect(answer.getByRole("button", { name: /Cancel/ })).toHaveCount(0);
  expect(asks).toEqual([{ question: QUESTION, conversation_id: null, filters: {} }]);
  await expect(page).toHaveURL(new RegExp(`conversation=${conversationId(100)}`));

  const sources = answer.getByRole("region", { name: "Sources" });
  await expect(sources.getByRole("listitem")).toHaveCount(2);
  await expect(sources.getByRole("listitem").first()).toContainText("Rechnung 2026-1042");

  await answer.getByRole("button", { name: "Open source 2" }).click();
  await expect(page).toHaveURL(new RegExp(`message=${searchMessageId(1)}`));
  await expect(page.locator("article mark")).toContainText("offene Rechnung 2026-1042");
});

test("cancel stops the stream", async ({ page }) => {
  await mockSearch(page, { answer: "hang" });
  await page.goto("/search");
  await searchbox(page).fill(QUESTION);
  await searchbox(page).press("Enter");
  await expect(page.getByRole("button", { name: /Cancel/ })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByText("Cancelled. The answer was not saved.")).toBeVisible();
  await expect(page.getByRole("button", { name: /Cancel/ })).toHaveCount(0);
});

test("says when the mails do not back an answer", async ({ page }) => {
  await mockSearch(page, { answer: "no_evidence" });
  await page.goto("/search");
  await searchbox(page).fill(NO_EVIDENCE_QUESTION);
  await searchbox(page).press("Enter");
  await expect(page.getByText("Not enough evidence")).toBeVisible();
  await expect(page.getByRole("region", { name: "Sources" })).toHaveCount(0);
});

test("history: reopen and delete conversations", async ({ page }) => {
  await mockSearch(page);
  await page.goto("/search");
  const history = page.getByRole("list", { name: "History" });
  await expect(history.getByRole("listitem")).toHaveCount(4);

  await history.getByRole("link", { name: /Wer vertritt Lena/ }).click();
  await expect(page.getByRole("article", { name: "Wer vertritt Lena im Oktober?" })).toBeVisible();
  await page.getByRole("button", { name: "New search" }).click();

  await page.getByRole("button", { name: "Delete “Wer vertritt Lena im Oktober?”" }).click();
  await expect(history.getByRole("listitem")).toHaveCount(3);
  await page.getByRole("button", { name: "Clear history" }).click();
  await page.getByRole("button", { name: "Clear history" }).click();
  await expect(page.getByText("Search your mail")).toBeVisible();
});

test("mobile: hits and message are stacked", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockSearch(page);
  await page.goto("/search");
  await searchbox(page).fill("rechnung");
  await searchbox(page).press("Enter");
  await page.getByRole("list", { name: "Results" }).getByRole("link").first().click();
  await expect(page.getByRole("button", { name: "Back to the list" })).toBeVisible();
  await expect(page.locator("article mark")).toBeVisible();
  expect(await overflow(page)).toBe(0);
  await page.getByRole("button", { name: "Back to the list" }).click();
  await expect(page.getByRole("list", { name: "Results" })).toBeVisible();
});

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 360, height: 780 },
  ]) {
    test(`search views have no axe violations (${colorScheme}, ${viewport.width}px)`, async ({
      page,
    }) => {
      await page.emulateMedia({ colorScheme });
      await page.setViewportSize(viewport);
      await mockSearch(page, { step: 5 });
      await page.goto("/search");
      await expect(page.getByRole("list", { name: "History" })).toBeVisible();
      await expectNoA11yViolations(page);

      await searchbox(page).fill("rechnung");
      await searchbox(page).press("Enter");
      await expect(page.getByRole("list", { name: "Results" })).toBeVisible();
      await expectNoA11yViolations(page);

      await searchbox(page).fill(QUESTION);
      await searchbox(page).press("Enter");
      await expect(page.getByRole("region", { name: "Sources" })).toBeVisible();
      await expectNoA11yViolations(page);
      expect(await overflow(page)).toBe(0);
    });
  }
}

import { randomUUID } from "node:crypto";

import { expect, test } from "@playwright/test";

import { appendMessages, csrfHeaders, imap, signIn } from "./stack";

/**
 * Tasks and search against the real stack (API, worker, IMAP test server; no mocks and no
 * LLM). Same requirements as `mailbox.spec.ts`:
 *
 *   E2E_API=1 E2E_IMAP=1 E2E_SETUP_TOKEN=<token> pnpm e2e e2e/fullstack.spec.ts
 */
test.skip(!process.env.E2E_API || !process.env.E2E_IMAP, "needs backend, worker and IMAP server");

test("create a task and check it off", async ({ page }) => {
  const title = `Reply to the offer ${randomUUID().slice(0, 8)}`;
  await signIn(page);

  await page.goto("/tasks");
  const input = page.getByRole("textbox", { name: "New task" });
  await input.fill(title);
  await input.press("Enter");
  const checkbox = page.getByRole("checkbox", { name: `Mark “${title}” as done` });
  await expect(checkbox).not.toBeChecked();

  await checkbox.click();
  const done = page.getByRole("region", { name: /^Done/ });
  await expect(done.getByRole("button", { name: title, exact: true })).toBeVisible();

  // Stored on the server: still done after a reload.
  await page.reload();
  await expect(
    page.getByRole("region", { name: /^Done/ }).getByRole("button", { name: title, exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("checkbox", { name: `Mark “${title}” as done` })).toHaveCount(0);
});

test("a synced mail is found by the full-text search", async ({ page }) => {
  test.setTimeout(120_000);
  const run = randomUUID().slice(0, 8);
  const address = `e2e-search-${run}@example.org`;
  // A term that only this run's mail contains; the database may hold mails of earlier runs.
  const term = `delivery${run}`;
  const subject = `Delivery note ${run}`;
  await appendMessages(address, [
    { subject, body: `Please find the reference ${term} for the delivery on Friday.` },
    { subject: `Unrelated ${run}`, body: "Nothing to see here." },
  ]);
  await signIn(page);

  // Added through the API; the form itself is covered by mailbox.spec.ts.
  const created = await page.request.post("/api/mailboxes", {
    headers: await csrfHeaders(page),
    data: {
      type: "imap",
      address,
      provider_settings: {
        host: imap.host,
        port: imap.port,
        security: "tls",
        verify_certificate: false,
      },
      credentials: { password: imap.password },
    },
  });
  expect(created.status()).toBe(201);

  // Sync and indexing run in the worker: wait until the API finds the mail. (Repeating the
  // search in the UI would not help, it keeps results of the same query for 30 s.)
  await expect
    .poll(
      async () => {
        const response = await page.request.post("/api/search", {
          headers: await csrfHeaders(page),
          data: { query: term },
        });
        expect(response.status()).toBe(200);
        return (await response.json()).hits.length;
      },
      { timeout: 90_000 },
    )
    .toBe(1);

  await page.goto("/search");
  const searchbox = page.getByRole("searchbox", { name: "Search terms or question" });
  await searchbox.fill(term);
  await searchbox.press("Enter");
  const hits = page.getByRole("list", { name: "Results" });
  await expect(hits.getByRole("listitem")).toHaveCount(1);
  await expect(hits.getByText(subject)).toBeVisible();

  await hits.getByText(subject).click();
  await expect(page.getByRole("heading", { level: 1, name: subject })).toBeVisible();
  await expect(page.getByRole("article").getByText(term)).toBeVisible();
});

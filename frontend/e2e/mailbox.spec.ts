import { randomUUID } from "node:crypto";

import { expect, test } from "@playwright/test";

import { appendMessages, imap, signIn } from "./stack";

/**
 * Add a mailbox against the IMAP test server and see its mails in the inbox (real API, worker
 * and IMAP server, no mocks). Needs:
 *
 * - backend behind the dev server's `/api` proxy with `OLLAMAIL_AUTH_COOKIE_SECURE=false` and
 *   `OLLAMAIL_MAIL_ALLOW_INSECURE_CONNECTIONS=true` (the test server's certificate is self-signed),
 * - a running worker (`python -m app.worker`) that syncs the mailbox,
 * - the IMAP test server of the backend tests (`backend/tests/mail/imap_server.py`): any user,
 *   password `ollamail-test`.
 *
 *   E2E_API=1 E2E_IMAP=1 E2E_SETUP_TOKEN=<token> pnpm e2e e2e/mailbox.spec.ts
 *
 * On an empty database the first admin is created with the setup token; otherwise
 * `E2E_EMAIL`/`E2E_PASSWORD` sign in (default: the admin of `auth.spec.ts`).
 */
test.skip(!process.env.E2E_API || !process.env.E2E_IMAP, "needs backend, worker and IMAP server");

test("add a mailbox on the IMAP test server → its mails appear in the inbox", async ({ page }) => {
  test.setTimeout(120_000);
  const run = randomUUID().slice(0, 8);
  const address = `e2e-${run}@example.org`;
  // Unique per run: the database may already hold mailboxes of earlier runs.
  const subjects = ["Quarterly planning", "Invoice 2026-1042", "Maintenance on Saturday"].map(
    (subject) => `${subject} ${run}`,
  );
  await appendMessages(
    address,
    subjects.map((subject) => ({ subject })),
  );
  await signIn(page);

  await page.goto("/settings/mailboxes/new");
  await page.getByLabel("Email address").fill(address);
  await page.getByLabel("Password", { exact: true }).fill(imap.password);
  // Leaving the address field starts autodiscovery; wait for its guess before overriding it.
  await expect(page.getByText("Settings guessed from the domain.")).toBeVisible();
  await page.getByLabel("IMAP server").fill(imap.host);
  await page.getByLabel("Port").fill(String(imap.port));
  await page.getByLabel("Accept any server certificate").check();
  await page.getByRole("button", { name: "Test connection" }).click();
  await expect(page.getByText(/Connection works, \d+ folders? found\./)).toBeVisible();
  await page.getByRole("button", { name: "Add mailbox" }).click();

  await expect(page).toHaveURL(/\/settings\/mailboxes$/);
  const list = page.getByRole("list", { name: "Mailboxes" });
  const row = list.getByRole("listitem").filter({ hasText: address });
  await expect(row).toBeVisible();
  // The status follows the sync live (SSE) until the import is done.
  await expect(row.getByText(/Up to date · 3 messages/)).toBeVisible({ timeout: 60_000 });

  await page.goto("/inbox");
  const messages = page.getByRole("list", { name: "Messages" });
  for (const subject of subjects) {
    await expect(messages.getByText(subject)).toBeVisible();
  }
  await messages.getByText(subjects[1] ?? "").click();
  await expect(page.getByRole("heading", { level: 2, name: subjects[1] })).toBeVisible();
  await expect(page.getByRole("article").getByText("Synthetic body 1.")).toBeVisible();

  // Removing hides the mails at once; the worker deletes them in the background (#147) and
  // the row disappears when it is done.
  await page.goto("/settings/mailboxes");
  await row.getByRole("button", { name: `Actions for ${address}` }).click();
  await page.getByRole("menuitem", { name: "Remove" }).click();
  await page
    .getByRole("dialog", { name: "Remove mailbox?" })
    .getByRole("button", { name: "Remove" })
    .click();
  await expect(page.getByText("Removing mailbox (3 messages)")).toBeVisible();
  await expect(row).toHaveCount(0, { timeout: 60_000 });
  await page.goto("/inbox");
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  for (const subject of subjects) {
    await expect(page.getByText(subject)).toHaveCount(0);
  }
});

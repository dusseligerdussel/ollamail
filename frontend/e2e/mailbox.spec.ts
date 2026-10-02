import { randomUUID } from "node:crypto";
import tls from "node:tls";

import { expect, type Page, test } from "@playwright/test";

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

const imap = {
  host: process.env.E2E_IMAP_HOST ?? "localhost",
  port: Number(process.env.E2E_IMAP_PORT ?? 31993),
  password: process.env.E2E_IMAP_PASSWORD ?? "ollamail-test",
};
const user = {
  name: "E2E Admin",
  email: process.env.E2E_EMAIL ?? "e2e-admin@example.org",
  password: process.env.E2E_PASSWORD ?? "e2e admin password 1",
};

/** Puts synthetic messages into a fresh test account (IMAP APPEND over TLS). */
async function appendMessages(address: string, subjects: string[]) {
  const socket = tls.connect({ host: imap.host, port: imap.port, rejectUnauthorized: false });
  let buffer = "";
  socket.setEncoding("utf8");
  socket.on("data", (chunk) => {
    buffer += chunk;
  });
  const waitFor = async (pattern: RegExp) => {
    for (let i = 0; i < 200; i += 1) {
      if (pattern.test(buffer)) return;
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    throw new Error(`IMAP: no ${pattern} in ${buffer.slice(-200)}`);
  };
  await waitFor(/^\* OK/m);
  socket.write(`a1 LOGIN "${address}" "${imap.password}"\r\n`);
  await waitFor(/^a1 OK/m);
  for (const [index, subject] of subjects.entries()) {
    const message = [
      "From: Jonas Beispiel <jonas@example.org>",
      `To: ${address}`,
      `Subject: ${subject}`,
      `Message-ID: <e2e-${index}-${randomUUID()}@example.org>`,
      `Date: ${new Date(Date.now() - index * 60_000).toUTCString()}`,
      "Content-Type: text/plain; charset=utf-8",
      "",
      `Synthetic body ${index}.`,
      "",
    ].join("\r\n");
    const tag = `b${index}`;
    socket.write(`${tag} APPEND INBOX {${Buffer.byteLength(message)}}\r\n`);
    await waitFor(/^\+/m);
    buffer = "";
    socket.write(`${message}\r\n`);
    await waitFor(new RegExp(`^${tag} OK`, "m"));
  }
  socket.write("z LOGOUT\r\n");
  socket.end();
}

async function signIn(page: Page) {
  const status = await (await page.request.get("/api/setup/status")).json();
  if (!status.initialized) {
    await page.goto("/setup");
    await page.getByLabel("Name").fill(user.name);
    await page.getByLabel("E-mail address").fill(user.email);
    await page.getByLabel("Password").fill(user.password);
    await page.getByLabel("Setup code").fill(process.env.E2E_SETUP_TOKEN ?? "");
    await page.getByRole("button", { name: "Create administrator" }).click();
    await page.getByRole("link", { name: "Continue to inbox" }).click();
  } else {
    await page.goto("/login");
    await page.getByLabel("E-mail address").fill(user.email);
    await page.getByLabel("Password").fill(user.password);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
}

test("add a mailbox on the IMAP test server → its mails appear in the inbox", async ({ page }) => {
  test.setTimeout(120_000);
  const run = randomUUID().slice(0, 8);
  const address = `e2e-${run}@example.org`;
  // Unique per run: the database may already hold mailboxes of earlier runs.
  const subjects = ["Quarterly planning", "Invoice 2026-1042", "Maintenance on Saturday"].map(
    (subject) => `${subject} ${run}`,
  );
  await appendMessages(address, subjects);
  await signIn(page);

  await page.goto("/settings/mailboxes/new");
  await page.getByLabel("Email address").fill(address);
  await page.getByLabel("Password", { exact: true }).fill(imap.password);
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
  await expect(page.getByRole("heading", { level: 1, name: subjects[1] })).toBeVisible();
  await expect(page.getByRole("article").getByText("Synthetic body 1.")).toBeVisible();
});

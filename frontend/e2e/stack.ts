import { randomUUID } from "node:crypto";
import tls from "node:tls";

import { expect, type Page } from "@playwright/test";

/**
 * Helpers for the specs against the real stack (API, worker, IMAP test server; no mocks).
 * Only synthetic data: `example.org` addresses, generated subjects and bodies.
 */

export const imap = {
  host: process.env.E2E_IMAP_HOST ?? "localhost",
  port: Number(process.env.E2E_IMAP_PORT ?? 31993),
  password: process.env.E2E_IMAP_PASSWORD ?? "ollamail-test",
};

/** The first admin, created by `auth.spec.ts` (or by `signIn` on an empty database). */
export const admin = {
  name: "E2E Admin",
  email: process.env.E2E_EMAIL ?? "e2e-admin@example.org",
  password: process.env.E2E_PASSWORD ?? "e2e admin password 1",
};

export interface TestMessage {
  subject: string;
  body?: string;
}

/** Puts synthetic messages into a fresh test account (IMAP APPEND over TLS). */
export async function appendMessages(address: string, messages: TestMessage[]) {
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
  for (const [index, { subject, body }] of messages.entries()) {
    const message = [
      "From: Jonas Beispiel <jonas@example.org>",
      `To: ${address}`,
      `Subject: ${subject}`,
      `Message-ID: <e2e-${index}-${randomUUID()}@example.org>`,
      `Date: ${new Date(Date.now() - index * 60_000).toUTCString()}`,
      "Content-Type: text/plain; charset=utf-8",
      "",
      body ?? `Synthetic body ${index}.`,
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

/** Signs in as `admin`; on an empty database the admin is created with the setup token. */
export async function signIn(page: Page) {
  const status = await (await page.request.get("/api/setup/status")).json();
  if (!status.initialized) {
    await page.goto("/setup");
    await page.getByLabel("Name").fill(admin.name);
    await page.getByLabel("Email address").fill(admin.email);
    await page.getByLabel("Password").fill(admin.password);
    await page.getByLabel("Setup code").fill(process.env.E2E_SETUP_TOKEN ?? "");
    await page.getByRole("button", { name: "Create administrator" }).click();
    await page.getByRole("link", { name: "Continue to inbox" }).click();
  } else {
    await page.goto("/login");
    await page.getByLabel("Email address").fill(admin.email);
    await page.getByLabel("Password").fill(admin.password);
    await page.getByRole("button", { name: "Sign in" }).click();
  }
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
}

/** Headers for state-changing API calls with the page's session (CSRF double submit). */
export async function csrfHeaders(page: Page) {
  const csrf = (await page.context().cookies()).find((c) => c.name === "ollamail_csrf");
  return { "X-CSRF-Token": csrf?.value ?? "" };
}

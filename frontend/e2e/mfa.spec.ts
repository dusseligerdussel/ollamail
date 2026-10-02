import { createHmac, randomBytes } from "node:crypto";

import {
  type APIRequestContext,
  type BrowserContext,
  expect,
  type Page,
  test,
} from "@playwright/test";

/**
 * Sign-in with an authenticator app (TOTP) against the real API (no mocks): set it up under
 * Settings → Security, sign out, sign in with password and code. Needs a running backend
 * behind the dev server's `/api` proxy, as `auth.spec.ts`:
 *
 *   E2E_API=1 E2E_SETUP_TOKEN=<OLLAMAIL_SETUP_TOKEN> pnpm e2e e2e/mfa.spec.ts
 *
 * Uses the admin of `auth.spec.ts` (and sets up the instance if it is still empty) to create
 * a fresh user, so it can run after it and repeatedly.
 */
test.skip(!process.env.E2E_API, "needs a backend (E2E_API=1)");

const admin = { email: "e2e-admin@example.org", password: "e2e admin password 1" };

/** RFC 6238 code (SHA-1, 6 digits, 30 s) for a base32 secret, `offset` steps from now. */
function totp(secret: string, offset = 0): string {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    bits += alphabet.indexOf(char).toString(2).padStart(5, "0");
  }
  const key = Buffer.from(bits.match(/.{8}/g)?.map((byte) => Number.parseInt(byte, 2)) ?? []);
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000) + offset));
  const hmac = createHmac("sha1", key).update(counter).digest();
  const start = (hmac[hmac.length - 1] ?? 0) & 0xf;
  const value = (hmac.readUInt32BE(start) & 0x7fffffff) % 1_000_000;
  return value.toString().padStart(6, "0");
}

async function csrfHeaders(context: BrowserContext, request: APIRequestContext) {
  let token = (await context.cookies()).find((c) => c.name === "ollamail_csrf")?.value;
  if (!token) {
    await request.get("/api/auth/providers");
    token = (await context.cookies()).find((c) => c.name === "ollamail_csrf")?.value;
  }
  return { "X-CSRF-Token": token ?? "" };
}

/** A new local user, created by the e2e admin through the API. */
async function createUser(page: Page) {
  const { request } = page;
  const context = page.context();
  const status = await (await request.get("/api/setup/status")).json();
  if (!status.initialized) {
    const created = await request.post("/api/setup", {
      headers: await csrfHeaders(context, request),
      data: {
        setup_token: process.env.E2E_SETUP_TOKEN ?? "",
        email: admin.email,
        display_name: "E2E Admin",
        password: admin.password,
      },
    });
    expect(created.status()).toBe(201);
  } else {
    const login = await request.post("/api/auth/login", {
      headers: await csrfHeaders(context, request),
      data: admin,
    });
    expect(login.status(), "the e2e admin signs in without a second factor").toBe(200);
  }
  const member = {
    email: `e2e-totp-${randomBytes(4).toString("hex")}@example.org`,
    password: `e2e totp password ${randomBytes(4).toString("hex")}`,
  };
  const created = await request.post("/api/users", {
    headers: await csrfHeaders(context, request),
    data: { ...member, display_name: "E2E TOTP" },
  });
  expect(created.status()).toBe(201);
  await request.post("/api/auth/logout", { headers: await csrfHeaders(context, request) });
  await context.clearCookies();
  return member;
}

async function enterPassword(page: Page, email: string, password: string) {
  await page.getByLabel("E-mail address").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

test("set up an authenticator app and sign in with password and code", async ({ page }) => {
  const member = await createUser(page);

  await page.goto("/settings/security");
  await expect(page).toHaveURL(/\/login\?redirect=/);
  await enterPassword(page, member.email, member.password);
  await expect(page.getByRole("heading", { level: 1, name: "Security" })).toBeVisible();

  await page.getByRole("button", { name: "Set up" }).click();
  const sheet = page.getByRole("dialog", { name: "Set up authenticator app" });
  await expect(sheet.getByRole("img", { name: "QR code for the authenticator app" })).toBeVisible();
  const secret = await sheet.getByRole("textbox", { name: "Key for manual entry" }).inputValue();
  await sheet.getByLabel("Code", { exact: true }).fill(totp(secret));
  await sheet.getByRole("button", { name: "Activate" }).click();

  const codes = page.getByRole("dialog", { name: "Recovery codes" });
  await expect(codes.getByRole("listitem")).toHaveCount(10);
  await codes.getByRole("button", { name: "Done, codes saved" }).click();
  await expect(page.getByText("Two-factor authentication is on.")).toBeVisible();

  await page.getByRole("button", { name: "Account" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Sign in to ollamail" })).toBeVisible();

  // The password alone does not sign in.
  await enterPassword(page, member.email, member.password);
  await expect(
    page.getByRole("heading", { level: 1, name: "Two-factor authentication" }),
  ).toBeVisible();
  const me = await page.request.get("/api/auth/me");
  expect(me.status()).toBe(401);

  const code = page.getByLabel("Code from your authenticator app");
  await code.fill("000000");
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("alert")).toHaveText("The code is not correct. Try again.");

  // The next time step: the current one was used for the set-up (no replay).
  await code.fill(totp(secret, 1));
  await page.getByRole("button", { name: "Verify" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
});

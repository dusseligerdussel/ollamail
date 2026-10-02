import { expect, type Page, test } from "@playwright/test";

/**
 * Setup, sign-in and guards against the real API (no mocks). Needs a running backend with an
 * empty database behind the dev server's `/api` proxy:
 *
 *   E2E_API=1 E2E_SETUP_TOKEN=<OLLAMAIL_SETUP_TOKEN> pnpm e2e e2e/auth.spec.ts
 *
 * The backend must allow cookies over plain HTTP (`OLLAMAIL_AUTH_COOKIE_SECURE=false`).
 */
test.skip(!process.env.E2E_API, "needs a backend with an empty database (E2E_API=1)");
test.describe.configure({ mode: "serial" });

const admin = {
  name: "E2E Admin",
  email: "e2e-admin@example.org",
  password: "e2e admin password 1",
};
const member = {
  name: "E2E Member",
  email: "e2e-member@example.org",
  password: "e2e member password 1",
};

async function signIn(page: Page, email: string, password: string) {
  await page.getByLabel("E-mail address").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}

async function signOut(page: Page) {
  await page.getByRole("button", { name: "Account" }).click();
  await page.getByRole("menuitem", { name: "Sign out" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Sign in to ollamail" })).toBeVisible();
}

test("fresh instance → setup → admin signed in → logout → login", async ({ page }) => {
  const status = await page.request.get("/api/setup/status");
  expect(await status.json(), "the database must be empty").toEqual({ initialized: false });

  await page.goto("/");
  await expect(page).toHaveURL(/\/setup$/);
  await page.getByLabel("Name").fill(admin.name);
  await page.getByLabel("E-mail address").fill(admin.email);
  await page.getByLabel("Password").fill(admin.password);

  // A wrong setup code is rejected by the server.
  await page.getByLabel("Setup code").fill("wrong-code");
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(page.getByText("The setup code is incorrect.")).toBeVisible();

  await page.getByLabel("Setup code").fill(process.env.E2E_SETUP_TOKEN ?? "");
  await page.getByRole("button", { name: "Create administrator" }).click();
  await expect(
    page.getByRole("heading", { level: 1, name: "Administrator created" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Continue to inbox" }).click();

  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(nav.getByRole("link", { name: "Admin" })).toBeVisible();

  // The session survives a reload; the setup is no longer reachable.
  await page.goto("/setup");
  await expect(page).toHaveURL(/\/inbox$/);

  await signOut(page);
  await page.goto("/settings");
  await expect(page).toHaveURL(/\/login\?redirect=%2Fsettings$/);

  await signIn(page, admin.email, "not the password");
  await expect(page.getByRole("alert")).toHaveText("E-mail address or password is incorrect.");

  await signIn(page, admin.email, admin.password);
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  const sessions = page.getByRole("region", { name: "Active sessions" });
  await expect(sessions.getByText("This device")).toBeVisible();
});

test("non-admins see no admin navigation and get a 403 page", async ({ page }) => {
  await page.goto("/login");
  await signIn(page, admin.email, admin.password);
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();

  // Create a regular user through the admin API (same session cookies, CSRF header).
  const csrf = (await page.context().cookies()).find((c) => c.name === "ollamail_csrf");
  const created = await page.request.post("/api/users", {
    headers: { "X-CSRF-Token": csrf?.value ?? "" },
    data: { email: member.email, display_name: member.name, password: member.password },
  });
  expect(created.status()).toBe(201);
  await signOut(page);

  await signIn(page, member.email, member.password);
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
  const nav = page.getByRole("navigation", { name: "Main navigation" });
  await expect(nav.getByRole("link", { name: "Settings" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Admin" })).toHaveCount(0);

  await page.goto("/admin");
  await expect(page.getByRole("heading", { level: 1, name: "No access" })).toBeVisible();
  // The API enforces it as well.
  expect((await page.request.get("/api/users")).status()).toBe(403);
});

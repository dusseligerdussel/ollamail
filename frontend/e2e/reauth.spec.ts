import { expect, type Page, type Route, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { adminUserId, mockAdmin, sharedMailboxIds } from "./mock-admin";
import { mockApi } from "./mock-api";

/**
 * Confirmation before sensitive actions (#144) and critical admin actions (#190): the
 * server answers 403 `reauth-required`, the re-auth sheet confirms and the action runs
 * again. Runs without a backend (mocked API).
 */

const REAUTH_REQUIRED = {
  type: "urn:ollamail:problem:reauth-required",
  title: "Forbidden",
  status: 403,
  detail: "Confirm that it is you to continue.",
  reauth_minutes: 10,
};

const OPTIONS = {
  methods: ["password", "webauthn", "totp", "signin"],
  provider_display_name: null,
  login_path: null,
  reauth_minutes: 10,
  valid_until: null,
};

/**
 * `sensitive` ("DELETE /api/auth/mfa/totp") answers 403 until the password
 * "correct horse battery" was confirmed; returns the requests it saw.
 */
async function mockReauth(page: Page, sensitive: string, success: (route: Route) => unknown) {
  const seen: string[] = [];
  let confirmed = false;
  await page.route(
    (url) => url.pathname.startsWith("/api/"),
    async (route) => {
      const request = route.request();
      const key = `${request.method()} ${new URL(request.url()).pathname}`;
      if (key === "GET /api/auth/reauth") return route.fulfill({ json: OPTIONS });
      if (key === "POST /api/auth/reauth") {
        seen.push(key);
        if (request.postDataJSON()?.password === "correct horse battery") {
          confirmed = true;
          return route.fulfill({
            json: { authenticated_at: "2026-10-03T10:00:00Z", valid_until: "2026-10-03T10:10:00Z" },
          });
        }
        return route.fulfill({
          status: 400,
          contentType: "application/problem+json",
          json: { type: "urn:ollamail:problem:reauth-invalid", title: "Bad Request", status: 400 },
        });
      }
      if (key === sensitive) {
        seen.push(key);
        if (!confirmed) {
          return route.fulfill({
            status: 403,
            contentType: "application/problem+json",
            json: REAUTH_REQUIRED,
          });
        }
        return success(route);
      }
      return route.fallback();
    },
  );
  return seen;
}

test("removing the authenticator app asks for the password first", async ({ page }) => {
  await mockApi(page);
  const seen = await mockReauth(page, "DELETE /api/auth/mfa/totp", (route) =>
    route.fulfill({ status: 204 }),
  );
  await page.goto("/settings/security");

  await page.getByRole("button", { name: "Remove", exact: true }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await expect(sheet).toBeVisible();
  await expect(sheet).toContainText("It stays valid for 10 minutes.");
  await expect(sheet.getByLabel("Password")).toBeFocused();
  await expect(sheet.getByRole("navigation", { name: "Other ways to confirm" })).toContainText(
    "Use a passkey",
  );
  await expectNoA11yViolations(page);

  await sheet.getByLabel("Password").fill("wrong password");
  await sheet.getByRole("button", { name: "Confirm" }).click();
  await expect(sheet.getByRole("alert")).toHaveText("The password is not correct.");

  await sheet.getByLabel("Password").fill("correct horse battery");
  await sheet.getByLabel("Password").press("Enter");

  await expect(sheet).toBeHidden();
  await expect(page.getByText("Authenticator app removed.")).toBeVisible();
  expect(seen).toEqual([
    "DELETE /api/auth/mfa/totp",
    "POST /api/auth/reauth",
    "POST /api/auth/reauth",
    "DELETE /api/auth/mfa/totp",
  ]);
});

test("Escape cancels the confirmation without an error", async ({ page }) => {
  await mockApi(page);
  const seen = await mockReauth(page, "POST /api/privacy/exports", (route) =>
    route.fulfill({ status: 202, json: {} }),
  );
  await page.goto("/settings");

  const request = page.getByRole("button", { name: "Request export" });
  await request.click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await expect(sheet).toBeVisible();
  await page.keyboard.press("Escape");

  await expect(sheet).toBeHidden();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(request).toBeEnabled();
  expect(seen).toEqual(["POST /api/privacy/exports"]);
});

test("deleting the account confirms on top of the dialog", async ({ page }) => {
  await mockApi(page);
  const seen = await mockReauth(page, "DELETE /api/privacy/account", (route) =>
    route.fulfill({ status: 204 }),
  );
  await page.goto("/settings");

  await page.getByRole("button", { name: "Delete account…" }).click();
  const dialog = page.getByRole("dialog", { name: "Delete your account permanently?" });
  await dialog.getByRole("textbox").fill("admin@example.org");
  await dialog.getByRole("button", { name: "Delete account", exact: true }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await sheet.getByLabel("Password").fill("correct horse battery");
  await sheet.getByRole("button", { name: "Confirm" }).click();

  await expect(page).toHaveURL(/\/login$/);
  expect(seen.filter((key) => key === "DELETE /api/privacy/account")).toHaveLength(2);
});

test("making a user an administrator asks for a confirmation first", async ({ page }) => {
  await mockApi(page);
  await mockAdmin(page);
  const path = `PATCH /api/users/${adminUserId(1)}`;
  const seen = await mockReauth(page, path, (route) =>
    route.fulfill({ json: { id: adminUserId(1), role: "admin" } }),
  );
  await page.goto("/admin/users");

  await page.getByRole("button", { name: "Actions for Jonas Beispiel" }).click();
  await page.getByRole("menuitem", { name: "Make administrator" }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await expect(sheet).toBeVisible();
  await expect(sheet.getByLabel("Password")).toBeFocused();
  await expectNoA11yViolations(page);
  await sheet.getByLabel("Password").fill("correct horse battery");
  await sheet.getByRole("button", { name: "Confirm" }).click();

  await expect(sheet).toBeHidden();
  await expect(page.getByText("User updated")).toBeVisible();
  expect(seen).toEqual([path, "POST /api/auth/reauth", path]);
});

test("cancelling the confirmation creates no SCIM token and shows no error", async ({ page }) => {
  await mockApi(page);
  await mockAdmin(page);
  const seen = await mockReauth(page, "POST /api/admin/scim/tokens", (route) =>
    route.fulfill({ status: 201, json: {} }),
  );
  await page.goto("/admin/scim");

  await page.getByLabel("Name").fill("Entra ID");
  await page.getByRole("button", { name: "Create token" }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await expect(sheet).toBeVisible();
  await sheet.getByRole("button", { name: "Cancel" }).click();

  await expect(sheet).toBeHidden();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByLabel("Name")).toHaveValue("Entra ID");
  expect(seen).toEqual(["POST /api/admin/scim/tokens"]);
});

test("adding an AI provider confirms and then saves", async ({ page }) => {
  await mockApi(page);
  await mockAdmin(page);
  const seen = await mockReauth(page, "POST /api/admin/ai/providers", (route) =>
    route.fulfill({ status: 201, json: {} }),
  );
  await page.goto("/admin/ai");

  await page.getByRole("button", { name: "Add provider" }).click();
  const form = page.getByRole("dialog", { name: "Add provider" });
  await form.getByLabel("Name", { exact: true }).fill("relay");
  await form.getByLabel("Display name").fill("Relay");
  await form.getByLabel("URL", { exact: true }).fill("https://llm.example.org/v1");
  await form.getByRole("button", { name: "Save" }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await sheet.getByLabel("Password").fill("correct horse battery");
  await sheet.getByRole("button", { name: "Confirm" }).click();

  await expect(sheet).toBeHidden();
  expect(seen).toEqual([
    "POST /api/admin/ai/providers",
    "POST /api/auth/reauth",
    "POST /api/admin/ai/providers",
  ]);
});

test("testing a stored AI key against a new URL confirms first (#219)", async ({ page }) => {
  await mockApi(page);
  await mockAdmin(page);
  const seen = await mockReauth(page, "POST /api/admin/ai/providers/test", (route) =>
    route.fulfill({ json: { ok: true, models: ["m1"], error: null, duration_ms: 12 } }),
  );
  await page.goto("/admin/ai");

  await page.getByRole("button", { name: "Edit: Example Cloud" }).click();
  const form = page.getByRole("dialog", { name: "Edit provider" });
  await form.getByLabel("URL", { exact: true }).fill("https://relay.example.org/v1");
  await expect(form).toContainText("URL or type changed: enter the API key again.");
  await form.getByRole("button", { name: "Test connection" }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await sheet.getByLabel("Password").fill("correct horse battery");
  await sheet.getByRole("button", { name: "Confirm" }).click();

  await expect(sheet).toBeHidden();
  await expect(form.getByRole("status")).toContainText("Connected");
  expect(seen).toEqual([
    "POST /api/admin/ai/providers/test",
    "POST /api/auth/reauth",
    "POST /api/admin/ai/providers/test",
  ]);
});

test("saving the role mapping confirms and then saves (#206)", async ({ page }) => {
  await mockApi(page);
  await mockAdmin(page);
  const seen = await mockReauth(page, "PUT /api/admin/auth/role-mapping", (route) =>
    route.fulfill({ json: route.request().postDataJSON() }),
  );
  await page.goto("/admin/role-mapping");

  await page.getByRole("button", { name: "Save", exact: true }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await expect(sheet).toBeVisible();
  await sheet.getByLabel("Password").fill("correct horse battery");
  await sheet.getByRole("button", { name: "Confirm" }).click();

  await expect(sheet).toBeHidden();
  await expect(page.getByText("Role mapping saved")).toBeVisible();
  expect(seen).toEqual([
    "PUT /api/admin/auth/role-mapping",
    "POST /api/auth/reauth",
    "PUT /api/admin/auth/role-mapping",
  ]);
});

test("cancelling the confirmation keeps shared mailbox access unsaved, without an error (#206)", async ({
  page,
}) => {
  await mockApi(page);
  await mockAdmin(page);
  const path = `PUT /api/admin/shared-mailboxes/${sharedMailboxIds.support}/assignments`;
  const seen = await mockReauth(page, path, (route) => route.fulfill({ json: {} }));
  await page.goto(`/admin/shared-mailboxes/${sharedMailboxIds.support}`);

  await page.getByRole("checkbox", { name: /Test Admin/ }).click();
  await page.getByRole("button", { name: "Save access" }).click();
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await expect(sheet).toBeVisible();
  await sheet.getByRole("button", { name: "Cancel" }).click();

  await expect(sheet).toBeHidden();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByText("Unsaved changes")).toBeVisible();
  await expect(page.getByRole("button", { name: "Save access" })).toBeEnabled();
  expect(seen).toEqual([path]);
});

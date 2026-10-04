import { expect, type Page, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockApi } from "./mock-api";

/**
 * Setup and sign-in over plain http:// (#142): browsers drop the Secure cookies there, the
 * server answers every POST with a CSRF 403. Runs without a backend (mocked API).
 */

/** What the backend answers when the CSRF cookie never arrived. */
async function rejectPostsAsCsrf(page: Page, path: string) {
  await page.route(`**${path}`, (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 403,
          contentType: "application/problem+json",
          json: {
            type: "about:blank",
            title: "Forbidden",
            status: 403,
            detail: "CSRF token missing or invalid.",
            error_code: "csrf_failed",
          },
        })
      : route.fallback(),
  );
}

/** The dev server runs on localhost, a secure context; pretend to be http://<lan-ip>. */
async function insecureContext(page: Page) {
  await page.addInitScript(() => {
    Object.defineProperty(window, "isSecureContext", { value: false });
  });
}

test("setup: a CSRF rejection is explained instead of blaming the setup code", async ({ page }) => {
  await mockApi(page, { initialized: false, role: null });
  await rejectPostsAsCsrf(page, "/api/setup");
  await page.goto("/setup");
  await expect(page.getByRole("heading", { level: 1, name: "Set up ollamail" })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);

  await page.getByLabel("Name").fill("Test Admin");
  await page.getByLabel("Email address").fill("admin@example.org");
  await page.getByLabel("Password").fill("correct horse battery");
  await page.getByLabel("Setup code").fill("SETUPCODE");
  await page.getByRole("button", { name: "Create administrator" }).click();

  const alert = page.getByRole("alert");
  await expect(alert).toContainText("The browser did not send the security cookie");
  await expect(alert).toContainText("OLLAMAIL_AUTH_COOKIE_SECURE=false");
  await expect(page.getByText("The setup code is incorrect.")).toHaveCount(0);
  await expect(page.getByLabel("Setup code")).not.toHaveAttribute("aria-invalid");
  await expectNoA11yViolations(page);
});

test("login: a CSRF rejection is explained", async ({ page }) => {
  await mockApi(page, { role: null });
  await rejectPostsAsCsrf(page, "/api/auth/login");
  await page.goto("/login");

  await page.getByLabel("Email address").fill("admin@example.org");
  await page.getByLabel("Password").fill("correct horse battery");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  await expect(page.getByRole("alert")).toHaveCount(1);
  await expect(page.getByRole("alert")).toContainText(
    "The browser did not send the security cookie",
  );
});

test("setup and login warn up front outside a secure context", async ({ page }) => {
  await insecureContext(page);
  for (const [path, initialized] of [
    ["/setup", false],
    ["/login", true],
  ] as const) {
    await test.step(path, async () => {
      await page.unrouteAll();
      await mockApi(page, { initialized, role: null });
      await page.goto(path);
      const alert = page.getByRole("alert");
      await expect(alert).toContainText("Unencrypted connection (HTTP)");
      await expect(alert.getByRole("link", { name: /HTTPS and test operation/ })).toHaveAttribute(
        "href",
        /docs\/OPERATIONS\.md#26-http-ohne-tls-testbetrieb$/,
      );
      await expectNoA11yViolations(page);
    });
  }
});

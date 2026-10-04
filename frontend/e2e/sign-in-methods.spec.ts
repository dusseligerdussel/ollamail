import { expect, type Page, type Route, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockApi } from "./mock-api";
import { mockMail } from "./mock-mail";

// Unlinking a sign-in that was linked by e-mail address (#216): from the link notice to the
// sign-in methods, the confirmation (with re-authentication), the block and lifting it.
const notice = {
  id: "00000000-0000-4000-8000-0000000000c1",
  provider: "oidc:corp",
  provider_name: "Corporate SSO",
  created_at: "2026-10-04T08:15:00Z",
};

const local = {
  id: "00000000-0000-4000-8000-0000000000d1",
  provider: "local",
  provider_name: null,
  created_at: "2026-01-01T08:00:00Z",
  last_used_at: "2026-10-04T09:30:00Z",
  current: true,
  unlink_refusal: "local",
};

const linked = {
  id: "00000000-0000-4000-8000-0000000000d2",
  provider: "oidc:corp",
  provider_name: "Corporate SSO",
  created_at: "2026-10-04T08:15:00Z",
  last_used_at: "2026-10-04T08:20:00Z",
  current: false,
  unlink_refusal: null,
};

const block = {
  id: "00000000-0000-4000-8000-0000000000e1",
  provider: "oidc:corp",
  provider_name: "Corporate SSO",
  created_at: "2026-10-04T10:00:00Z",
};

const REAUTH_REQUIRED = {
  type: "urn:ollamail:problem:reauth-required",
  title: "Forbidden",
  status: 403,
  detail: "Confirm that it is you to continue.",
  reauth_minutes: 10,
};

/** A linked account; unlinking and lifting the block need a confirmation with the password. */
async function mockLinkedAccount(page: Page) {
  await mockApi(page);
  await mockMail(page);
  const state = {
    identities: [local, linked],
    blocks: [] as (typeof block)[],
    notices: [notice],
    confirmed: false,
    seen: [] as string[],
  };
  await page.route("**/api/auth/link-notices", (route) => route.fulfill({ json: state.notices }));
  await page.route("**/api/auth/identities", (route) => route.fulfill({ json: state.identities }));
  await page.route("**/api/auth/link-blocks", (route) => route.fulfill({ json: state.blocks }));
  await page.route("**/api/auth/reauth", (route) => {
    if (route.request().method() === "GET") {
      return route.fulfill({
        json: {
          methods: ["password", "signin"],
          provider_display_name: null,
          login_path: null,
          reauth_minutes: 10,
          valid_until: null,
        },
      });
    }
    state.confirmed = true;
    return route.fulfill({
      json: { authenticated_at: "2026-10-04T10:00:00Z", valid_until: "2026-10-04T10:10:00Z" },
    });
  });
  const sensitive = (onConfirmed: () => void) => (route: Route) => {
    state.seen.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
    if (!state.confirmed) {
      return route.fulfill({
        status: 403,
        contentType: "application/problem+json",
        json: REAUTH_REQUIRED,
      });
    }
    onConfirmed();
    return route.fulfill({ status: 204 });
  };
  await page.route(
    `**/api/auth/identities/${linked.id}`,
    sensitive(() => {
      state.identities = [local];
      state.blocks = [block];
      state.notices = [];
    }),
  );
  await page.route(
    `**/api/auth/link-blocks/${block.id}`,
    sensitive(() => {
      state.blocks = [];
    }),
  );
  return state;
}

test("unlinks a sign-in linked by e-mail address and blocks relinking", async ({ page }) => {
  const state = await mockLinkedAccount(page);
  await page.goto("/inbox");

  const bar = page.getByRole("complementary", { name: "New sign-in linked" });
  await bar.getByRole("link", { name: "Unlink sign-in" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  const section = page.getByRole("region", { name: "Sign-in methods" });
  await expect(section.getByRole("listitem").filter({ hasText: "Local account" })).toContainText(
    "This session",
  );

  await section.getByRole("button", { name: "Unlink Corporate SSO" }).click();
  const dialog = page.getByRole("dialog", { name: "Unlink Corporate SSO?" });
  await expect(dialog).toContainText("All sessions that signed in with Corporate SSO end");
  await expectNoA11yViolations(page);
  await dialog.getByRole("button", { name: "Unlink", exact: true }).click();

  // The server asks for a confirmation first; the unlink runs again afterwards.
  const sheet = page.getByRole("dialog", { name: "Confirm it is you" });
  await sheet.getByLabel("Password").fill("correct horse battery");
  await sheet.getByRole("button", { name: "Confirm" }).click();

  await expect(dialog).toBeHidden();
  const blocked = section.getByRole("listitem").filter({ hasText: "Corporate SSO" });
  await expect(blocked).toContainText("Blocked");
  await expect(blocked).toContainText("Not linked to your account again automatically.");
  await expect(bar).toBeHidden();
  expect(state.seen).toEqual([
    `DELETE /api/auth/identities/${linked.id}`,
    `DELETE /api/auth/identities/${linked.id}`,
  ]);

  await blocked.getByRole("button", { name: "Lift block for Corporate SSO" }).click();
  await expect(blocked).toBeHidden();
  expect(state.seen.at(-1)).toBe(`DELETE /api/auth/link-blocks/${block.id}`);
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the confirmation fits the screen", async ({ page }) => {
    await mockLinkedAccount(page);
    await page.goto("/settings");

    const section = page.getByRole("region", { name: "Sign-in methods" });
    await section.getByRole("button", { name: "Unlink Corporate SSO" }).click();
    const dialog = page.getByRole("dialog", { name: "Unlink Corporate SSO?" });
    await expect(dialog.getByRole("button", { name: "Unlink", exact: true })).toBeInViewport();
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeInViewport();
  });
});

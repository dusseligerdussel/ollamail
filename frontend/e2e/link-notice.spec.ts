import { expect, type Page, test } from "@playwright/test";

import { mockApi } from "./mock-api";
import { mockMail } from "./mock-mail";

// A sign-in linked to the account by e-mail address (#208): the notice in the shell, the
// sessions list naming the sign-in method, and dismissing the notice.
const notice = {
  id: "00000000-0000-4000-8000-0000000000c1",
  provider: "oidc:corp",
  provider_name: "Corporate SSO",
  created_at: "2026-10-04T08:15:00Z",
};

const sessions = [
  {
    id: "00000000-0000-4000-8000-0000000000a1",
    provider: "local",
    provider_name: null,
    created_at: "2026-10-01T08:00:00Z",
    last_seen_at: "2026-10-04T09:30:00Z",
    expires_at: "2026-10-15T08:00:00Z",
    user_agent: "Mozilla/5.0 (X11; Linux x86_64) Chrome/140.0 Safari/537.36",
    current: true,
  },
  {
    id: "00000000-0000-4000-8000-0000000000a2",
    provider: "oidc:corp",
    provider_name: "Corporate SSO",
    created_at: "2026-10-04T08:15:00Z",
    last_seen_at: "2026-10-04T08:20:00Z",
    expires_at: "2026-10-15T08:15:00Z",
    user_agent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    current: false,
  },
];

async function mockLinkedAccount(page: Page) {
  await mockApi(page);
  await mockMail(page);
  const state = { notices: [notice], dismissed: [] as string[] };
  await page.route("**/api/auth/link-notices", (route) => route.fulfill({ json: state.notices }));
  await page.route("**/api/auth/link-notices/*", (route) => {
    state.dismissed.push(new URL(route.request().url()).pathname.split("/").at(-1) ?? "");
    state.notices = [];
    return route.fulfill({ status: 204 });
  });
  await page.route("**/api/auth/sessions", (route) => route.fulfill({ json: sessions }));
  return state;
}

test("shows a linked sign-in and leads to the sessions", async ({ page }) => {
  await mockLinkedAccount(page);
  await page.goto("/inbox");

  const bar = page.getByRole("complementary", { name: "New sign-in linked" });
  await expect(bar).toContainText("a sign-in through Corporate SSO was linked to your account");

  await bar.getByRole("link", { name: "Review sessions" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();
  const list = page.getByRole("region", { name: "Active sessions" });
  await expect(list.getByRole("listitem").filter({ hasText: "Chrome on Windows" })).toContainText(
    "Sign-in: Corporate SSO",
  );
  await expect(list.getByRole("listitem").filter({ hasText: "This device" })).toContainText(
    "Sign-in: Local account",
  );
});

test("dismissing confirms the link", async ({ page }) => {
  const state = await mockLinkedAccount(page);
  await page.goto("/inbox");

  const bar = page.getByRole("complementary", { name: "New sign-in linked" });
  await bar.getByRole("button", { name: "That was me – dismiss notice" }).click();

  await expect(bar).toBeHidden();
  expect(state.dismissed).toEqual([notice.id]);
  await page.reload();
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
  await expect(bar).toBeHidden();
});

test.describe("on a phone", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("the notice folds into the notices line", async ({ page }) => {
    await mockLinkedAccount(page);
    await page.goto("/inbox");

    // The mocked mailboxes add a sync notice, so the line sums up both.
    const notices = page.getByRole("complementary", { name: "Notices" });
    await notices.getByRole("button", { name: "2 notices" }).click();
    await expect(notices).toContainText("a sign-in through Corporate SSO was linked");
    await expect(notices.getByRole("link", { name: "Review sessions" })).toBeVisible();
  });
});

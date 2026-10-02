import { expect, type Page } from "@playwright/test";

import { mockAdmin, sharedMailboxIds } from "./mock-admin";
import { type MockApi, mockApi } from "./mock-api";
import { mockDigest } from "./mock-digest";
import { mockDrafts, overviewDrafts } from "./mock-drafts";
import { mockMail, NOW } from "./mock-mail";
import { mockSearch } from "./mock-search";
import { mockTodos } from "./mock-todos";

// Every route of the app and its main states, for the page-wide checks (axe, reflow; #123).
// Runs without a backend: the API is mocked in the browser.

/** Every route of the app shell (signed-in admin). */
export const routes = [
  "/inbox",
  "/tasks",
  "/digest",
  "/digest/settings",
  "/search",
  "/drafts",
  "/settings",
  "/settings/security",
  "/settings/categories",
  "/settings/mailboxes",
  "/settings/mailboxes/new",
  "/settings/task-export",
  "/admin",
  "/admin/ai",
  "/admin/audit",
  "/admin/categories",
  "/admin/retention",
  "/admin/role-mapping",
  "/admin/scim",
  "/admin/shared-mailboxes",
  "/admin/shared-mailboxes/new",
  `/admin/shared-mailboxes/${sharedMailboxIds.support}`,
  "/admin/sign-in",
  "/admin/users",
  "/does-not-exist",
];

/** Pages outside the app shell and pages for other session states: path → mocked session. */
export const publicRoutes: [string, MockApi][] = [
  ["/login", { role: null }],
  ["/setup", { initialized: false, role: null }],
  ["/invite", { role: null }],
  // 403 page
  ["/admin", { role: "user" }],
];

export type State = "data" | "empty" | "error" | "loading";

export async function mockState(page: Page, state: State) {
  await page.clock.setFixedTime(NOW);
  await mockApi(page);
  if (state === "data" || state === "empty") {
    const empty = state === "empty";
    await mockMail(page, { mailboxes: !empty, triage: !empty });
    await mockDigest(page, { digests: empty ? 0 : 8 });
    await mockTodos(page, { todos: !empty });
    await mockSearch(page, { history: !empty });
    await mockDrafts(page, { drafts: empty ? [] : overviewDrafts() });
    await mockAdmin(page, { empty });
    return;
  }
  // Session and setup stay answered (the route guard needs them); everything else fails or
  // never answers.
  const session = ["/api/setup/status", "/api/auth/me", "/api/auth/providers"];
  await page.route(
    (url) => url.pathname.startsWith("/api/") && !session.includes(url.pathname),
    (route) =>
      state === "error"
        ? route.fulfill({
            status: 500,
            contentType: "application/problem+json",
            json: { type: "about:blank", title: "Internal Server Error", status: 500 },
          })
        : undefined, // never answered: skeletons stay
  );
}

export async function openPage(page: Page, path: string, state: State) {
  await page.goto(path);
  await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
  if (state !== "loading") await page.waitForLoadState("networkidle");
}

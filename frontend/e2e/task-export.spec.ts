import { expect, type Page, type Route, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockApi } from "./mock-api";

// Runs without a backend: the export API and the Microsoft sign-in are mocked in the browser
// (synthetic account and lists).

const LISTS = [
  { id: "AQMkADAw-tasks", name: "Tasks" },
  { id: "AQMkADAw-work", name: "Work" },
];

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, json: body });
}

async function mockTodoExport(page: Page) {
  let target: Record<string, unknown> | null = null;
  let signedIn = false;
  const saved: unknown[] = [];

  // Microsoft's sign-in page: consent is given at once, back to the settings.
  await page.route("https://login.example.com/**", (route) => {
    signedIn = true;
    const origin = new URL(page.url()).origin;
    return route.fulfill({
      status: 302,
      headers: { location: `${origin}/settings/task-export?mstodo=connected` },
    });
  });
  await page.route(
    (url) => url.pathname.startsWith("/api/todo-export"),
    (route) => {
      const request = route.request();
      const key = `${request.method()} ${new URL(request.url()).pathname}`;
      switch (key) {
        case "GET /api/todo-export":
          return json(route, { available_sinks: ["caldav", "mstodo"], target });
        case "POST /api/todo-export/mstodo/connect":
          return json(route, { authorization_url: "https://login.example.com/authorize?state=s" });
        case "POST /api/todo-export/mstodo/lists":
          return signedIn
            ? json(route, { account: "erika@example.com", lists: LISTS })
            : json(route, { status: 409, error_code: "mstodo_not_connected" }, 409);
        case "PUT /api/todo-export/mstodo": {
          const body = request.postDataJSON() as { list_id: string; mode: string };
          saved.push(body);
          target = {
            sink: "mstodo",
            url: "",
            username: "erika@example.com",
            has_password: false,
            list_id: body.list_id,
            list_name: LISTS.find((list) => list.id === body.list_id)?.name ?? "",
            mode: body.mode,
            active: true,
            last_sync_at: null,
            last_error: null,
            counts: { synced: 0, pending: 0, error: 0, removed: 0 },
            created_at: "2026-10-01T08:00:00Z",
          };
          return json(route, { available_sinks: ["caldav", "mstodo"], target });
        }
        default:
          return json(route, { status: 404 }, 404);
      }
    },
  );
  return { saved };
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test("connect Microsoft To Do: sign in, pick a list, turn on", async ({ page }) => {
  const { saved } = await mockTodoExport(page);
  await page.goto("/settings/task-export");

  await page.getByRole("radio", { name: /Microsoft To Do/ }).click();
  await expect(page.getByLabel("Server URL")).toHaveCount(0);
  await expect(page.getByText(/to Microsoft To Do\./)).toBeVisible();
  await page.getByRole("button", { name: "Sign in with Microsoft" }).click();

  // Back from Microsoft: the account's lists, no form for server or password.
  await expect(page.getByText("Signed in as erika@example.com")).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/task-export$/);
  await page.getByLabel("List", { exact: true }).selectOption("AQMkADAw-work");
  await expectNoA11yViolations(page);
  await page.getByRole("button", { name: "Turn on export" }).click();

  await expect(page.getByText("Task export turned on")).toBeVisible();
  const connection = page.getByRole("region", { name: "Connection" });
  await expect(connection.getByText("Microsoft To Do")).toBeVisible();
  await expect(connection.getByText("Work")).toBeVisible();
  await expect(connection.getByText("Server URL")).toHaveCount(0);
  expect(saved).toEqual([{ list_id: "AQMkADAw-work", mode: "auto" }]);
});

test("a cancelled Microsoft sign-in is reported", async ({ page }) => {
  await mockTodoExport(page);
  await page.goto("/settings/task-export?mstodo=error&reason=consent_denied");

  await expect(page.getByText("Microsoft sign-in failed")).toBeVisible();
  await expect(
    page.getByText("The sign-in was cancelled or access was not allowed."),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/task-export$/);
});

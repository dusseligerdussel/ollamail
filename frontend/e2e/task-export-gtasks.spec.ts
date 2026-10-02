import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { mockApi } from "./mock-api";

// Runs without a backend: the export API and Google's consent page are mocked in the
// browser. Lists and IDs are invented.
const GOOGLE = "https://accounts.google.test/o/oauth2/v2/auth";

interface Target {
  sink: "gtasks";
  url: string;
  username: string;
  has_password: boolean;
  list_id: string;
  list_name: string;
  mode: "auto" | "manual";
  active: boolean;
  last_sync_at: string | null;
  last_error: string | null;
  counts: { synced: number; pending: number; error: number; removed: number };
  created_at: string;
}

const LISTS = [
  { id: "gl-default", name: "My Tasks" },
  { id: "gl-work", name: "Work" },
];

async function mockExport(page: Page) {
  let target: Target | null = null;
  const requests: { method: string; path: string; body: unknown }[] = [];
  let mode: Target["mode"] = "auto";
  const settings = () => ({ available_sinks: ["caldav", "gtasks"], target });

  await page.route(
    (url) => url.pathname.startsWith("/api/todo-export"),
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const body = request.postDataJSON() as Record<string, unknown> | null;
      if (request.method() !== "GET") requests.push({ method: request.method(), path, body });
      const key = `${request.method()} ${path}`;
      if (key === "GET /api/todo-export") return route.fulfill({ json: settings() });
      if (key === "GET /api/todo-export/lists") return route.fulfill({ json: LISTS });
      if (key === "POST /api/todo-export/gtasks/oauth/start") {
        mode = (body?.mode as Target["mode"]) ?? "auto";
        return route.fulfill({ json: { authorization_url: `${GOOGLE}?state=s` } });
      }
      if (key === "PATCH /api/todo-export" && target) {
        const list = LISTS.find((item) => item.id === body?.list_id);
        if (list) target = { ...target, list_id: list.id, list_name: list.name };
        return route.fulfill({ json: settings() });
      }
      return route.fulfill({ status: 404, json: { status: 404 } });
    },
  );
  // Google agrees and sends the browser back; the backend's callback is not involved here.
  await page.route(
    (url) => url.href.startsWith(GOOGLE),
    (route) => {
      target = {
        sink: "gtasks",
        url: "",
        username: "",
        has_password: false,
        list_id: "gl-default",
        list_name: "My Tasks",
        mode,
        active: true,
        last_sync_at: null,
        last_error: null,
        counts: { synced: 0, pending: 0, error: 0, removed: 0 },
        created_at: "2026-10-02T08:00:00Z",
      };
      return route.fulfill({
        status: 303,
        headers: { Location: new URL("/settings/task-export?gtasks=connected", page.url()).href },
      });
    },
  );
  return { requests };
}

async function expectNoA11yViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    // Stacked toasts fade out on purpose (app-wide sonner behaviour, not this page).
    .exclude("[data-sonner-toaster]")
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(
    results.violations.map(
      (violation) =>
        `${violation.id}: ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
    ),
  ).toEqual([]);
}

test.beforeEach(async ({ page }) => {
  await mockApi(page);
});

test("connect Google Tasks and pick another list", async ({ page }) => {
  const { requests } = await mockExport(page);
  await page.goto("/settings/task-export");

  await page.getByRole("radio", { name: /Google Tasks/ }).click();
  await expect(page.getByLabel("Server URL")).toHaveCount(0);
  await expect(page.getByText(/to Google Tasks\.$/)).toBeVisible();
  await expectNoA11yViolations(page);
  await page.getByRole("radio", { name: /Manual/ }).click();
  await page.getByRole("button", { name: "Connect with Google" }).click();

  // Back from Google: confirmed, the URL is clean, the default list is used.
  await expect(page.getByText("Google Tasks connected")).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/task-export$/);
  const connection = page.getByRole("region", { name: "Connection" });
  const list = connection.getByRole("combobox", { name: "List" });
  await expect(list).toHaveValue("gl-default");
  await expect(page.getByRole("radio", { name: /Manual/ })).toBeChecked();

  await list.selectOption("gl-work");
  await expect(page.getByText("List changed")).toBeVisible();
  await expect(list).toHaveValue("gl-work");
  expect(requests).toEqual([
    { method: "POST", path: "/api/todo-export/gtasks/oauth/start", body: { mode: "manual" } },
    { method: "PATCH", path: "/api/todo-export", body: { list_id: "gl-work" } },
  ]);
  await expectNoA11yViolations(page);
});

test("a cancelled sign-in is explained", async ({ page }) => {
  await mockExport(page);
  await page.goto("/settings/task-export?gtasks_error=consent_denied");

  await expect(page.getByText("Google Tasks was not connected")).toBeVisible();
  await expect(
    page.getByText("The sign-in was cancelled or access was not allowed."),
  ).toBeVisible();
  await expect(page).toHaveURL(/\/settings\/task-export$/);
});

test("fits a phone screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockExport(page);
  await page.goto("/settings/task-export");
  await page.getByRole("radio", { name: /Google Tasks/ }).click();

  await expect(page.getByRole("button", { name: "Connect with Google" })).toBeVisible();
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBe(0);
});

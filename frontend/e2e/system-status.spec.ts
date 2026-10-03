import { expect, test } from "@playwright/test";

import { mockAdmin, processingMailboxIds, systemModels } from "./mock-admin";
import { mockApi } from "./mock-api";
import { mockMail } from "./mock-mail";

// Runs without a backend: the API is mocked (signed-in admin unless a test says otherwise).
// Notices in the app shell and the system status on the admin overview (#139).

test("admins see a missing model in the shell and download it on the overview", async ({
  page,
}) => {
  await mockApi(page);
  await mockMail(page);
  await mockAdmin(page);
  let pulled = false;
  await page.route("**/api/admin/system/models", (route) =>
    route.fulfill({
      json: systemModels({ embeddings: pulled ? "installed" : "missing" }, null),
    }),
  );
  await page.route("**/api/admin/system/models/pull", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ endpoint: "default", model: "bge-m3" });
    pulled = true;
    await route.fulfill({
      status: 202,
      json: {
        status: "queued",
        completed: 0,
        total: null,
        error_code: null,
        updated_at: "2026-10-02T09:00:00Z",
      },
    });
  });

  await page.goto("/inbox");
  const notice = page.getByRole("complementary", { name: "Model missing" });
  await expect(notice).toContainText("bge-m3 is not installed");
  await notice.getByRole("link", { name: "Show details" }).click();

  await expect(page).toHaveURL(/\/admin$/);
  // The overview shows the details instead of the notice.
  await expect(notice).toBeHidden();
  const checklist = page.getByRole("region", { name: "Getting started" });
  await expect(checklist.getByRole("listitem").first()).toContainText(
    "Language models ready (open)",
  );
  const models = page.getByRole("region", { name: "Language models" });
  await models.getByRole("button", { name: "Download" }).click();

  await expect(page.getByText("Download started")).toBeVisible();
  await expect(models.getByRole("button", { name: "Download" })).toBeHidden();
  await expect(checklist.getByRole("listitem").first()).toContainText(
    "Language models ready (done)",
  );
});

test("admins see an unreachable LLM endpoint", async ({ page }) => {
  await mockApi(page);
  await mockMail(page);
  await mockAdmin(page);
  await page.route("**/api/admin/system/models", (route) =>
    route.fulfill({ json: systemModels({ triage: "unreachable", todos: "unreachable" }) }),
  );

  await page.goto("/tasks");

  await expect(
    page.getByRole("complementary", { name: "Language model not reachable" }),
  ).toContainText("Endpoint default does not respond.");
});

test("users see a mailbox that cannot sync and no model status", async ({ page }) => {
  await mockApi(page, { role: "user" });
  await mockMail(page);
  const requests: string[] = [];
  page.on("request", (request) => requests.push(new URL(request.url()).pathname));
  await page.route("**/api/mailboxes", (route) =>
    route.fulfill({
      json: [
        {
          id: "0192f300-0000-7000-8000-000000000001",
          type: "imap",
          display_name: "Arbeit",
          address: "erika@example.org",
          is_shared: false,
          permissions: ["act", "manage", "read", "send", "sync"],
          provider_settings: {},
          has_credentials: true,
          sync_enabled: true,
          sync_settings: {
            initial_sync_days: null,
            excluded_roles: ["trash", "junk"],
            excluded_folders: [],
            poll_interval_seconds: null,
          },
          status: {
            phase: "error",
            last_synced_at: "2026-10-01T09:00:00Z",
            last_error: "authentication_failed",
            sync_queued: false,
            folders_total: 2,
            folders_imported: 2,
            folders_failed: 0,
            message_count: 3,
          },
          created_at: "2026-09-01T08:00:00Z",
          updated_at: "2026-09-01T08:00:00Z",
        },
      ],
    }),
  );

  await page.goto("/inbox");
  const notice = page.getByRole("complementary", { name: "Mailbox not syncing" });
  await expect(notice).toContainText("Arbeit: Sign-in failed.");
  await notice.getByRole("link", { name: "Reconnect" }).click();

  await expect(page).toHaveURL(/\/settings\/mailboxes$/);
  // The mailbox settings show the error next to the mailbox instead.
  await expect(notice).toBeHidden();
  expect(requests).not.toContain("/api/admin/system/models");
});

test("admins retry the failed processing of a mailbox", async ({ page }) => {
  await mockApi(page);
  await mockMail(page);
  await mockAdmin(page);
  let retried = false;
  await page.route(
    `**/api/admin/system/mailboxes/${processingMailboxIds.work}/retry-failed`,
    (route) => {
      retried = true;
      return route.fulfill({ json: { queued: 3 } });
    },
  );

  await page.goto("/admin");
  const processing = page.getByRole("region", { name: "Processing per mailbox" });
  const work = processing.getByRole("listitem").filter({ hasText: "Erika Muster" });
  await expect(work).toContainText("13 pending");
  await expect(work).toContainText("3 failed");
  await work.getByRole("button", { name: "Retry failed" }).click();

  await expect(page.getByText("3 mails queued again")).toBeVisible();
  expect(retried).toBe(true);
});

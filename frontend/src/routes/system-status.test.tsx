import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import type { Mailbox } from "@/api/mail";
import type { ModelStatus, SystemOverview } from "@/api/system";
import i18n from "@/i18n";
import { backend, json, mockFetch, testUser } from "@/test/fetch";
import { testMailbox } from "@/test/mail";
import { renderApp } from "@/test/render-app";
import { testModels, testOverview } from "@/test/system";

interface Options {
  user?: typeof testUser;
  mailboxes?: Mailbox[];
  models?: ModelStatus[];
  overview?: SystemOverview;
}

function mockSystemApi({
  user,
  mailboxes = [testMailbox()],
  models = testModels(),
  overview = testOverview(),
}: Options = {}) {
  const requests: string[] = [];
  const api = backend(user ? { user } : {});
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    requests.push(route);
    switch (route) {
      case "GET /api/ai/status":
        return json({ cloud: [] });
      case "GET /api/mailboxes":
        return json(mailboxes);
      case "GET /api/admin/system/models":
        return json(models);
      case "GET /api/admin/system/overview":
        return json(overview);
      case "POST /api/admin/system/models/pull":
        return json(
          {
            status: "queued",
            completed: 0,
            total: null,
            error_code: null,
            updated_at: "2026-10-02T09:00:00Z",
          },
          { status: 202 },
        );
      case "POST /api/admin/system/mailboxes/0199b000-0000-7000-8000-0000000000a1/retry-failed":
        return json({ queued: 3 });
      case "GET /api/messages":
        return json({ items: [], next_cursor: null });
      default:
        return api(request);
    }
  });
  return requests;
}

const missingEmbeddings = testModels({
  embeddings: { state: "missing", can_pull: true },
});

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

describe("shell notices", () => {
  it("tells admins about a missing model", async () => {
    mockSystemApi({ models: missingEmbeddings });
    await renderApp("/settings");

    const notice = await screen.findByRole("complementary", { name: "Model missing" });
    expect(notice).toHaveTextContent("bge-m3 is not installed");
    expect(within(notice).getByRole("link", { name: "Show details" })).toHaveAttribute(
      "href",
      "/admin",
    );
  });

  it("prefers an unreachable endpoint over missing models", async () => {
    mockSystemApi({
      models: testModels({
        triage: { state: "unreachable" },
        embeddings: { state: "missing", can_pull: true },
      }),
    });
    await renderApp("/settings");

    const notice = await screen.findByRole("complementary", {
      name: "Language model not reachable",
    });
    expect(notice).toHaveTextContent("Endpoint default does not respond.");
  });

  it("never asks non-admins for the model status", async () => {
    const requests = mockSystemApi({ user: testUser, models: missingEmbeddings });
    await renderApp("/settings");

    await waitFor(() => expect(requests).toContain("GET /api/mailboxes"));
    expect(requests).not.toContain("GET /api/admin/system/models");
    expect(screen.queryByRole("complementary", { name: "Model missing" })).not.toBeInTheDocument();
  });

  it("shows every user a mailbox that cannot sync", async () => {
    mockSystemApi({
      user: testUser,
      mailboxes: [
        testMailbox({
          status: { ...testMailbox().status, phase: "error", last_error: "authentication_failed" },
        }),
      ],
    });
    await renderApp("/settings");

    const notice = await screen.findByRole("complementary", { name: "Mailbox not syncing" });
    expect(notice).toHaveTextContent("Arbeit: Sign-in failed.");
    expect(within(notice).getByRole("link", { name: "Reconnect" })).toHaveAttribute(
      "href",
      "/settings/mailboxes",
    );
  });

  it("ignores shared mailboxes and healthy ones", async () => {
    mockSystemApi({
      user: testUser,
      mailboxes: [
        testMailbox(),
        testMailbox({
          id: "0199b000-0000-7000-8000-000000000002",
          is_shared: true,
          status: { ...testMailbox().status, phase: "error", last_error: "connection_failed" },
        }),
      ],
    });
    await renderApp("/settings");

    await screen.findByRole("heading", { level: 1 });
    expect(
      screen.queryByRole("complementary", { name: "Mailbox not syncing" }),
    ).not.toBeInTheDocument();
  });
});

describe("admin overview", () => {
  it("shows the getting-started checklist", async () => {
    mockSystemApi({ models: missingEmbeddings });
    await renderApp("/admin");

    const checklist = await screen.findByRole("region", { name: "Getting started" });
    const items = within(checklist).getAllByRole("listitem");
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining("Language models ready (open)"),
      expect.stringContaining("Mailbox connected (done)"),
      expect.stringContaining("Daily digest turned on (open)"),
      expect.stringContaining("Public URL set (open)"),
    ]);
    expect(within(checklist).getByRole("link", { name: /daily digest/ })).toHaveAttribute(
      "href",
      "/digest/settings",
    );
    expect(checklist).toHaveTextContent("OLLAMAIL_AUTH_PUBLIC_URL");
  });

  it("downloads a missing model", async () => {
    const requests = mockSystemApi({ models: missingEmbeddings });
    await renderApp("/admin");

    const models = await screen.findByRole("region", { name: "Language models" });
    await userEvent.click(await within(models).findByRole("button", { name: "Download" }));

    await waitFor(() => expect(requests).toContain("POST /api/admin/system/models/pull"));
    expect(await screen.findByText("Download started")).toBeInTheDocument();
  });

  it("shows download progress", async () => {
    mockSystemApi({
      models: testModels({
        embeddings: {
          state: "missing",
          can_pull: true,
          pull: {
            status: "running",
            completed: 600_000_000,
            total: 1_200_000_000,
            error_code: null,
            updated_at: "2026-10-02T09:00:00Z",
          },
        },
      }),
    });
    await renderApp("/admin");

    expect(await screen.findByText("Downloading 50% of 1.2 GB")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download" })).not.toBeInTheDocument();
  });

  it("lists processing per mailbox and retries failed steps", async () => {
    const requests = mockSystemApi();
    await renderApp("/admin");

    const processing = await screen.findByRole("region", { name: "Processing per mailbox" });
    const [work, support] = await within(processing).findAllByRole("listitem");
    expect(work).toHaveTextContent("Erika Muster");
    expect(work).toHaveTextContent("5 pending");
    expect(work).toHaveTextContent("3 failed");
    expect(support).toHaveTextContent("Support");
    expect(support).toHaveTextContent(
      "Shared mailbox (Microsoft 365) · Sync error: Sign-in failed.",
    );
    expect(work).toHaveTextContent("IMAP · Up to date");
    expect(within(support as HTMLElement).queryByRole("button")).not.toBeInTheDocument();

    await userEvent.click(
      within(work as HTMLElement).getByRole("button", { name: "Retry failed" }),
    );

    await waitFor(() =>
      expect(requests).toContain(
        "POST /api/admin/system/mailboxes/0199b000-0000-7000-8000-0000000000a1/retry-failed",
      ),
    );
    expect(await screen.findByText("3 mails queued again")).toBeInTheDocument();
  });
});

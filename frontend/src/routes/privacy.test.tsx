import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { pageNavigation } from "@/api/auth";
import type { DataExport, RetentionSettings } from "@/api/privacy";
import i18n from "@/i18n";
import { backend, json, mockFetch, problem, testUser } from "@/test/fetch";
import { renderApp } from "@/test/render-app";

const readyExport: DataExport = {
  id: "00000000-0000-4000-8000-0000000000e1",
  status: "ready",
  error_code: null,
  size: 1_572_864,
  created_at: "2026-10-01T08:00:00Z",
  finished_at: "2026-10-01T08:01:00Z",
  expires_at: "2026-10-02T08:01:00Z",
};

const retention: RetentionSettings = {
  values: {
    mail_days: 0,
    attachment_days: 0,
    search_index_days: 0,
    rag_history_days: 90,
    digest_days: 30,
    audit_days: 730,
  },
  defaults: {
    mail_days: 0,
    attachment_days: 0,
    search_index_days: 0,
    rag_history_days: 90,
    digest_days: 30,
    audit_days: 365,
  },
  overridden: ["audit_days"],
  initial_sync_days: 90,
  last_run: {
    finished_at: "2026-10-02T03:07:00Z",
    mails: 12,
    attachments: 3,
    search_chunks: 40,
    threads: 2,
    audit_events: 5,
  },
};

interface Captured {
  method: string;
  path: string;
  body: unknown;
}

function mockPrivacyApi({
  exports = [] as DataExport[],
  selfDelete = true,
  user = undefined as typeof testUser | undefined,
  deleteAccount = () => new Response(null, { status: 204 }) as Response,
} = {}) {
  const requests: Captured[] = [];
  const api = backend(user ? { user } : {});
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const body = request.method === "GET" ? undefined : await request.clone().text();
    requests.push({
      method: request.method,
      path: pathname,
      body: body ? JSON.parse(body) : undefined,
    });
    switch (`${request.method} ${pathname}`) {
      case "GET /api/privacy/account":
        return json({ self_delete_enabled: selfDelete, export_expiry_hours: 48 });
      case "GET /api/privacy/exports":
        return json(exports);
      case "POST /api/privacy/exports": {
        const created: DataExport = {
          ...readyExport,
          id: "00000000-0000-4000-8000-0000000000e2",
          status: "pending",
          size: null,
          finished_at: null,
        };
        exports = [created, ...exports];
        return json(created, { status: 202 });
      }
      case `DELETE /api/privacy/exports/${readyExport.id}`:
        exports = exports.filter((item) => item.id !== readyExport.id);
        return new Response(null, { status: 204 });
      case "DELETE /api/privacy/account":
        return deleteAccount();
      case "GET /api/admin/privacy/retention":
        return json(retention);
      case "PATCH /api/admin/privacy/retention":
        return json({ ...retention, values: { ...retention.values, mail_days: 365 } });
      default:
        return api(request);
    }
  });
  return requests;
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("settings: data export", () => {
  it("requests an export and shows its progress", async () => {
    const user = userEvent.setup();
    const requests = mockPrivacyApi();
    await renderApp("/settings");

    expect(await screen.findByText(/download link is valid for 48 hours/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Request export" }));

    const list = await screen.findByRole("list", { name: "Exports" });
    expect(await within(list).findByText("Being prepared…")).toBeInTheDocument();
    expect(requests.filter((r) => r.method === "POST").map((r) => r.path)).toEqual([
      "/api/privacy/exports",
    ]);
    // One export at a time.
    expect(screen.getByRole("button", { name: "Request export" })).toBeDisabled();
  });

  it("offers a ready export for download until it expires", async () => {
    const user = userEvent.setup();
    const requests = mockPrivacyApi({ exports: [readyExport] });
    await renderApp("/settings");

    const list = await screen.findByRole("list", { name: "Exports" });
    expect(list).toHaveTextContent("1.5 MB · available until");
    const link = within(list).getByRole("link", { name: "Download" });
    expect(link).toHaveAttribute("href", `/api/privacy/exports/${readyExport.id}/download`);

    await user.click(within(list).getByRole("button", { name: /^Delete Export of/ }));
    await waitFor(() =>
      expect(screen.queryByRole("list", { name: "Exports" })).not.toBeInTheDocument(),
    );
    expect(requests.some((r) => r.method === "DELETE")).toBe(true);
  });
});

describe("settings: delete account", () => {
  it("needs the account's email address and signs out afterwards", async () => {
    const user = userEvent.setup();
    const assign = vi.spyOn(pageNavigation, "assign").mockImplementation(() => {});
    const requests = mockPrivacyApi();
    await renderApp("/settings");

    await user.click(await screen.findByRole("button", { name: "Delete account…" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete your account permanently?" });
    const confirm = within(dialog).getByRole("button", { name: "Delete account" });
    expect(confirm).toBeDisabled();

    const input = within(dialog).getByLabelText(/enter your email address admin@example.org/);
    await user.type(input, "someone@example.org");
    expect(confirm).toBeDisabled();
    await user.clear(input);
    await user.type(input, "Admin@Example.org");
    expect(confirm).toBeEnabled();
    await user.click(confirm);

    await waitFor(() => expect(assign).toHaveBeenCalledWith("/login"));
    const deletion = requests.find(
      (r) => r.path === "/api/privacy/account" && r.method === "DELETE",
    );
    expect(deletion?.body).toEqual({ confirm_email: "Admin@Example.org" });
  });

  it("explains why the last admin cannot delete their account", async () => {
    const user = userEvent.setup();
    const assign = vi.spyOn(pageNavigation, "assign").mockImplementation(() => {});
    mockPrivacyApi({
      deleteAccount: () => problem(409, { type: "urn:ollamail:problem:last-admin" }),
    });
    await renderApp("/settings");

    await user.click(await screen.findByRole("button", { name: "Delete account…" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByRole("textbox"), "admin@example.org");
    await user.click(within(dialog).getByRole("button", { name: "Delete account" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "You are the last administrator.",
    );
    expect(assign).not.toHaveBeenCalled();
  });

  it("is not offered when an admin deletes accounts", async () => {
    mockPrivacyApi({ selfDelete: false, user: testUser });
    await renderApp("/settings");

    expect(await screen.findByText(/Accounts are deleted by an administrator/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete account…" })).not.toBeInTheDocument();
  });
});

describe("admin: retention", () => {
  it("is linked from the admin page", async () => {
    mockPrivacyApi();
    await renderApp("/admin");
    expect(screen.getByRole("link", { name: /Retention/ })).toHaveAttribute(
      "href",
      "/admin/retention",
    );
  });

  it("is not shown to non-admins", async () => {
    mockPrivacyApi({ user: testUser });
    await renderApp("/admin/retention");
    expect(screen.getByRole("heading", { level: 1, name: "No access" })).toBeInTheDocument();
  });

  it("shows the periods with defaults and the last run", async () => {
    mockPrivacyApi();
    await renderApp("/admin/retention");

    expect(await screen.findByLabelText("Audit log")).toHaveValue(730);
    expect(screen.getByLabelText("Mails")).toHaveValue(0);
    expect(screen.getByText(/Default: 365 days/)).toBeInTheDocument();
    expect(screen.getAllByText("Default: keep forever")).toHaveLength(3);
    expect(screen.getByText("Custom")).toBeInTheDocument();
    expect(
      screen.getByText(/12 mails, 3 attachments, 40 search index entries and 5 audit log/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("saves only changed fields and warns about short mail retention", async () => {
    const user = userEvent.setup();
    const requests = mockPrivacyApi();
    await renderApp("/admin/retention");

    const mails = await screen.findByLabelText("Mails");
    fireEvent.change(mails, { target: { value: "30" } });
    expect(mails).toHaveValue(30);
    expect(screen.getByRole("status")).toHaveTextContent("Shorter than the initial import");
    fireEvent.change(mails, { target: { value: "365" } });
    expect(screen.queryByText(/Shorter than the initial import/)).not.toBeInTheDocument();

    const digests = screen.getByLabelText("Digests");
    fireEvent.change(digests, { target: { value: "0" } });
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    fireEvent.change(digests, { target: { value: "30" } });

    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(requests.find((r) => r.method === "PATCH")?.body).toEqual({ mail_days: 365 }),
    );

    await user.click(screen.getByRole("button", { name: "Use default" }));
    await waitFor(() =>
      expect(requests.filter((r) => r.method === "PATCH").at(-1)?.body).toEqual({
        audit_days: null,
      }),
    );
  });
});

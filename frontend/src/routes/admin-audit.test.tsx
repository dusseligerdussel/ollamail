import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { AuditEvent } from "@/api/audit";
import type { CurrentUser } from "@/hooks/use-current-user";
import i18n from "@/i18n";
import { json, mockFetch, problem } from "@/test/fetch";
import { setViewportWidth } from "@/test/media";
import { renderApp } from "@/test/render-app";

const currentUser = vi.hoisted(() => ({ value: { id: "test", isAdmin: true } as CurrentUser }));
vi.mock("@/hooks/use-current-user", () => ({ useCurrentUser: () => currentUser.value }));

const USER_ID = "0199a0b4-0000-7000-8000-000000000001";
const MAILBOX_ID = "0199a0b4-1111-7000-8000-000000000002";

function event(overrides: Partial<AuditEvent>): AuditEvent {
  return {
    id: 1,
    occurred_at: "2026-10-01T08:30:00Z",
    action: "auth.login_succeeded",
    actor_kind: "user",
    actor_id: USER_ID,
    actor_name: "Erika Muster",
    target_type: null,
    target_id: null,
    target_name: null,
    details: {},
    ...overrides,
  };
}

const events: AuditEvent[] = [
  event({
    id: 4,
    action: "crypto.keys_rotated",
    actor_kind: "system",
    actor_id: null,
    actor_name: null,
    details: { rotated: 3 },
  }),
  event({
    id: 3,
    action: "mailbox.deleted",
    actor_name: null,
    target_type: "mailbox",
    target_id: MAILBOX_ID,
  }),
  event({
    id: 2,
    action: "auth.login_failed",
    actor_kind: "anonymous",
    actor_id: null,
    actor_name: null,
    details: { reason: "invalid_credentials" },
  }),
  event({ id: 1, details: { provider: "local" } }),
];

/** Serves `GET /api/audit/events` from `pages` (by `before` cursor) and records the queries. */
function mockAuditApi(pages: Record<string, { items: AuditEvent[]; next_before: number | null }>) {
  const queries: URLSearchParams[] = [];
  mockFetch((request) => {
    const url = new URL(request.url);
    if (url.pathname !== "/api/audit/events") return problem(404);
    queries.push(url.searchParams);
    return json(pages[url.searchParams.get("before") ?? ""] ?? { items: [], next_before: null });
  });
  return queries;
}

beforeEach(async () => {
  currentUser.value = { id: "test", isAdmin: true };
  await i18n.changeLanguage("en");
});

describe("audit log", () => {
  it("is not shown to non-admins", async () => {
    currentUser.value = { id: "test", isAdmin: false };
    await renderApp("/admin/audit");
    expect(screen.getByRole("heading", { name: "Page not found" })).toBeInTheDocument();
  });

  it("lists events with readable actors and targets", async () => {
    mockAuditApi({ "": { items: events, next_before: null } });
    await renderApp("/admin/audit");

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell")[1]?.textContent)).toEqual([
      "Keys rotated",
      "Mailbox removed",
      "Sign-in failed",
      "Signed in",
    ]);
    expect(within(rows[0] as HTMLElement).getByText("System")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByText("rotated: 3")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("Deleted user")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("Mailbox 0199a0b4")).toBeInTheDocument();
    expect(within(rows[2] as HTMLElement).getByText("Not signed in")).toBeInTheDocument();
    expect(within(rows[3] as HTMLElement).getByText("Erika Muster")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load older entries" })).not.toBeInTheDocument();
  });

  it("uses a stacked list on narrow screens", async () => {
    setViewportWidth(390);
    mockAuditApi({ "": { items: events, next_before: null } });
    await renderApp("/admin/audit");

    const list = await screen.findByRole("list", { name: "Audit log" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(4);
    expect(items[1]).toHaveTextContent("Deleted user → Mailbox 0199a0b4");
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("filters by event and date and exports with the same filters", async () => {
    const user = userEvent.setup();
    const queries = mockAuditApi({ "": { items: events, next_before: null } });
    const { router } = await renderApp("/admin/audit");
    await screen.findByRole("table");

    await user.selectOptions(screen.getByLabelText("Event"), "Sign-in failed");
    await waitFor(() => expect(queries.at(-1)?.get("action")).toBe("auth.login_failed"));
    expect(router.state.location.search).toEqual({ action: "auth.login_failed" });

    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-09-01" } });
    await waitFor(() => expect(queries.at(-1)?.get("since")).not.toBeNull());
    expect(new Date(queries.at(-1)?.get("since") ?? "").getDate()).toBe(1);

    const exportLink = screen.getByRole("link", { name: "Export CSV" });
    const href = new URL(exportLink.getAttribute("href") ?? "", "https://test");
    expect(href.pathname).toBe("/api/audit/events/export");
    expect(href.searchParams.get("action")).toBe("auth.login_failed");
    expect(href.searchParams.get("since")).toBe(queries.at(-1)?.get("since"));

    await user.click(screen.getByRole("button", { name: "Reset filters" }));
    await waitFor(() => expect(router.state.location.search).toEqual({}));
  });

  it("loads older entries", async () => {
    const user = userEvent.setup();
    const queries = mockAuditApi({
      "": { items: events.slice(0, 2), next_before: 3 },
      "3": { items: events.slice(2), next_before: null },
    });
    await renderApp("/admin/audit");

    await user.click(await screen.findByRole("button", { name: "Load older entries" }));

    await waitFor(() => expect(screen.getAllByRole("row")).toHaveLength(5));
    expect(queries.at(-1)?.get("before")).toBe("3");
    expect(screen.queryByRole("button", { name: "Load older entries" })).not.toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    mockAuditApi({});
    await renderApp("/admin/audit");

    expect(await screen.findByRole("heading", { name: "No entries" })).toBeInTheDocument();
  });

  it("shows API errors inline", async () => {
    mockFetch(() => problem(403, { request_id: "req-7" }));
    await renderApp("/admin/audit");

    const alert = await screen.findByRole("alert", {}, { timeout: 5000 });
    expect(alert).toHaveTextContent("You do not have permission to do this.");
  });
});

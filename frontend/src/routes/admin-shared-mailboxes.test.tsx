import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { AdminUser } from "@/api/admin-auth";
import type { SharedMailbox } from "@/api/shared-mailboxes";
import { backend, json, mockFetch, testAdmin, testUser } from "@/test/fetch";
import { testMailbox } from "@/test/mail";
import { renderApp } from "@/test/render-app";

const MAILBOX_ID = "0199b000-0000-7000-8000-0000000000aa";

function sharedMailbox(overrides: Partial<SharedMailbox> = {}): SharedMailbox {
  return {
    ...testMailbox({
      id: MAILBOX_ID,
      display_name: "Support",
      address: "support@example.org",
      is_shared: true,
    }),
    assignments: [],
    reader_count: 0,
    ...overrides,
  } as SharedMailbox;
}

const users: AdminUser[] = [testAdmin, testUser].map((user) => ({
  ...user,
  providers: ["local"],
  invitation_pending: false,
  active_sessions: 0,
}));

/** Admin API for shared mailboxes; records the bodies of assignment updates. */
function mockSharedApi(mailboxes: SharedMailbox[]) {
  const assignments: unknown[] = [];
  const base = backend();
  mockFetch(async (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    if (route === "GET /api/admin/shared-mailboxes") return json(mailboxes);
    if (route === `GET /api/admin/shared-mailboxes/${MAILBOX_ID}`) return json(mailboxes[0]);
    if (route === `PUT /api/admin/shared-mailboxes/${MAILBOX_ID}/assignments`) {
      const body = await request.json();
      assignments.push(body);
      return json(mailboxes[0]);
    }
    if (route === "GET /api/users") return json(users);
    if (route.startsWith("GET /api/admin/auth/") || route === "GET /api/auth/ldap/directories") {
      return json([]);
    }
    return base(request);
  });
  return { assignments };
}

describe("admin: shared mailboxes", () => {
  it("lists shared mailboxes with their access", async () => {
    mockSharedApi([sharedMailbox({ reader_count: 3, assignments: [] })]);
    await renderApp("/admin/shared-mailboxes");
    const list = await screen.findByRole("list", { name: "Shared mailboxes" });
    const row = within(list).getByRole("link", { name: /Support/ });
    expect(row).toHaveAttribute("href", `/admin/shared-mailboxes/${MAILBOX_ID}`);
    expect(within(row).getByText("Not assigned yet")).toBeInTheDocument();
    expect(screen.getByText(/do not see their mails/)).toBeInTheDocument();
  });

  it("assigns people and groups", async () => {
    const { assignments } = mockSharedApi([sharedMailbox()]);
    await renderApp(`/admin/shared-mailboxes/${MAILBOX_ID}`);
    const save = await screen.findByRole("button", { name: "Save access" });
    expect(save).toBeDisabled();

    await userEvent.click(await screen.findByRole("checkbox", { name: /Test User/ }));
    fireEvent.change(screen.getByLabelText("Group"), { target: { value: "Support-Team" } });
    await userEvent.click(screen.getByRole("button", { name: "Add group" }));
    expect(
      within(screen.getByRole("list", { name: "Groups" })).getByText("Support-Team"),
    ).toBeInTheDocument();

    await userEvent.click(save);
    await waitFor(() =>
      expect(assignments).toEqual([
        { users: [testUser.id], groups: [{ group: "Support-Team", provider: null }] },
      ]),
    );
  });

  it("is only for admins", async () => {
    mockFetch(backend({ user: testUser }));
    await renderApp("/admin/shared-mailboxes");
    expect(screen.queryByRole("list", { name: "Shared mailboxes" })).not.toBeInTheDocument();
  });
});

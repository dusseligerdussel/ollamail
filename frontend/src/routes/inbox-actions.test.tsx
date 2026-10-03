import { fireEvent, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { MessageSummary } from "@/api/mail";
import { backend, json, mockFetch } from "@/test/fetch";
import { MAILBOX_ID, messageId, testMailbox, testMessage, testThread } from "@/test/mail";
import { renderApp } from "@/test/render-app";

const INBOX = "0199b000-0000-7000-8000-0000000000f1";
const ARCHIVE = "0199b000-0000-7000-8000-0000000000f2";
const PROJECTS = "0199b000-0000-7000-8000-0000000000f3";

function folder(id: string, name: string, role: string | null) {
  return {
    id,
    remote_id: name,
    name,
    kind: "folder",
    role,
    sync_enabled: true,
    synced: true,
    excluded_by_role: false,
    import_pending: false,
    last_error: null,
    last_synced_at: null,
    message_count: 0,
  };
}

/**
 * A mail server in memory: actions take a message out of the inbox (or back), `PATCH` records
 * flags. Answers like `POST /messages/{id}/actions`.
 */
function mockMailServer(messages: MessageSummary[], { failWith }: { failWith?: string } = {}) {
  const inbox = new Map(messages.map((message) => [message.id, message]));
  const actions: { id: string; body: unknown }[] = [];
  const patches: { id: string; body: unknown }[] = [];
  const base = backend();
  mockFetch(async (request) => {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /api/mailboxes") return json([testMailbox()]);
    if (route === `GET /api/mailboxes/${MAILBOX_ID}/folders`) {
      return json([
        folder(INBOX, "INBOX", "inbox"),
        folder(ARCHIVE, "Archive", "archive"),
        folder(PROJECTS, "Projects", null),
      ]);
    }
    if (route === "GET /api/messages") {
      const items = [...inbox.values()];
      return json({ items, total: items.length, next_cursor: null });
    }
    const thread = /^GET \/api\/messages\/([^/]+)\/thread$/.exec(route);
    if (thread?.[1]) return json(testThread(Number(thread[1].slice(-2))));
    const action = /^POST \/api\/messages\/([^/]+)\/actions$/.exec(route);
    if (action?.[1]) {
      const id = action[1];
      const body = (await request.json()) as { action: string; folder_id: string | null };
      actions.push({ id, body });
      if (failWith) {
        return json({ title: "Conflict", status: 409, error_code: failWith }, { status: 409 });
      }
      const message = messages.find((item) => item.id === id) as MessageSummary;
      const target =
        body.action === "archive" ? ARCHIVE : body.action === "move" ? body.folder_id : null;
      if (target === INBOX) inbox.set(id, message);
      else inbox.delete(id);
      return json({ message, folder_ids: [target], undo_folder_id: INBOX });
    }
    const patch = /^PATCH \/api\/messages\/([^/]+)$/.exec(route);
    if (patch?.[1]) {
      const body = (await request.json()) as { flagged?: boolean };
      patches.push({ id: patch[1], body });
      const message = inbox.get(patch[1]) as MessageSummary;
      const updated = { ...message, flagged: body.flagged ?? message.flagged };
      inbox.set(patch[1], updated);
      return json(updated);
    }
    return base(request);
  });
  return { actions, patches };
}

// jsdom has no layout; the virtualised list needs a viewport height to render rows.
const offsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 720 });
});
afterAll(() => {
  if (offsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", offsetHeight);
});

describe("inbox actions (#148)", () => {
  it("archives with e, opens the next message and undoes from the toast", async () => {
    const { actions } = mockMailServer([testMessage(1), testMessage(2)]);
    const { router } = await renderApp(`/inbox?message=${messageId(1)}`);
    await screen.findByRole("button", { name: "Archive" });

    fireEvent.keyDown(document.body, { key: "e" });

    await waitFor(() =>
      expect(actions).toEqual([
        { id: messageId(1), body: expect.objectContaining({ action: "archive" }) },
      ]),
    );
    await waitFor(() => expect(screen.queryByText("Sender 1")).not.toBeInTheDocument());
    expect(router.state.location.search).toMatchObject({ message: messageId(2) });

    // Keyboard: sonner's swipe handling needs pointer capture, which jsdom lacks.
    const undo = await screen.findByRole("button", { name: "Undo" });
    undo.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() =>
      expect(actions.at(-1)).toEqual({
        id: messageId(1),
        body: { action: "move", folder_id: INBOX },
      }),
    );
    expect(await screen.findByText("Sender 1")).toBeInTheDocument();
  });

  it("moves into a folder from the menu and flags with s", async () => {
    const { actions, patches } = mockMailServer([testMessage(1), testMessage(2)]);
    await renderApp(`/inbox?message=${messageId(1)}`);
    await screen.findByRole("button", { name: "Archive" });

    fireEvent.keyDown(document.body, { key: "s" });
    await waitFor(() => expect(patches).toEqual([{ id: messageId(1), body: { flagged: true } }]));
    expect(await screen.findByRole("button", { name: "Flag", pressed: true })).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "v" });
    await userEvent.click(await screen.findByRole("menuitem", { name: "Projects" }));
    await waitFor(() =>
      expect(actions).toEqual([
        { id: messageId(1), body: { action: "move", folder_id: PROJECTS } },
      ]),
    );
  });

  it("brings the message back and explains when the server refuses", async () => {
    mockMailServer([testMessage(1)], { failWith: "no_archive_folder" });
    await renderApp(`/inbox?message=${messageId(1)}`);
    await userEvent.click(await screen.findByRole("button", { name: "Archive" }));

    expect(await screen.findByText("This mailbox has no archive folder.")).toBeInTheDocument();
    expect(await screen.findByText("Sender 1")).toBeInTheDocument();
  });

  it("offers no actions in read-only shared mailboxes", async () => {
    mockMailServer([testMessage(1)]);
    const base = backend();
    mockFetch(async (request) => {
      const url = new URL(request.url);
      if (`${request.method} ${url.pathname}` === "GET /api/mailboxes") {
        return json([testMailbox({ is_shared: true, permissions: ["read"] })]);
      }
      if (url.pathname === "/api/messages") {
        return json({ items: [testMessage(1)], total: 1, next_cursor: null });
      }
      if (url.pathname.endsWith("/thread")) return json(testThread(1));
      return base(request);
    });
    await renderApp(`/inbox?message=${messageId(1)}`);
    expect(await screen.findByText("Shared mailbox, read only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });
});

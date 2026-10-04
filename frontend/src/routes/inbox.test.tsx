import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import type { MessageBody, MessageSummary, Thread } from "@/api/mail";
import { backend, json, mockFetch, problem } from "@/test/fetch";
import { messageId, testDetail, testMailbox, testMessage, testThread } from "@/test/mail";
import { setViewportWidth } from "@/test/media";

const SHARED_ID = "0199b000-0000-7000-8000-000000000002";

import { renderApp } from "@/test/render-app";

interface MailBackend {
  mailboxes?: ReturnType<typeof testMailbox>[];
  messages?: MessageSummary[];
  threads?: Record<string, Thread>;
  /** Bodies of collapsed thread messages (`GET /messages/{id}/body`); `null`: 500. */
  bodies?: Record<string, MessageBody | null>;
}

/** Mail endpoints on top of the default backend; records PATCH bodies and list queries. */
function mockMailApi({
  mailboxes = [testMailbox()],
  messages = [],
  threads = {},
  bodies = {},
}: MailBackend = {}) {
  const patches: { id: string; body: unknown }[] = [];
  const bodyRequests: string[] = [];
  const queries: URLSearchParams[] = [];
  const base = backend();
  const fetchMock = mockFetch(async (request) => {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /api/mailboxes") return json(mailboxes);
    if (route === "GET /api/messages") {
      queries.push(url.searchParams);
      const unreadOnly = url.searchParams.get("unread") === "true";
      const items = messages.filter((message) => !unreadOnly || message.unread);
      return json({ items, total: items.length, next_cursor: null });
    }
    const thread = /^GET \/api\/messages\/([^/]+)\/thread$/.exec(route);
    if (thread?.[1] && threads[thread[1]]) return json(threads[thread[1]]);
    const body = /^GET \/api\/messages\/([^/]+)\/body$/.exec(route);
    if (body?.[1] && body[1] in bodies) {
      bodyRequests.push(body[1]);
      const found = bodies[body[1]];
      return found ? json(found) : problem(500);
    }
    const patch = /^PATCH \/api\/messages\/([^/]+)$/.exec(route);
    if (patch?.[1]) {
      const body = await request.json();
      patches.push({ id: patch[1], body });
      const message = messages.find((item) => item.id === patch[1]);
      return json({ ...message, unread: !(body as { seen: boolean }).seen });
    }
    return base(request);
  });
  return { patches, queries, bodyRequests, fetchMock };
}

// jsdom has no layout; the virtualised list needs a viewport height to render rows.
const offsetHeight = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight");
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", { configurable: true, value: 720 });
});
afterAll(() => {
  if (offsetHeight) Object.defineProperty(HTMLElement.prototype, "offsetHeight", offsetHeight);
});

describe("inbox", () => {
  it("asks to add a mailbox when none is connected", async () => {
    mockMailApi({ mailboxes: [] });
    await renderApp("/inbox");
    expect(await screen.findByText("No mail account connected")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Add mailbox" })).toHaveAttribute(
      "href",
      "/settings/mailboxes/new",
    );
    // Nothing to select, so the detail pane stays empty instead of explaining j/k.
    expect(screen.queryByText("No message selected")).not.toBeInTheDocument();
  });

  it("explains the keyboard navigation while nothing is selected", async () => {
    mockMailApi({ messages: [testMessage(1)] });
    await renderApp("/inbox");
    expect(await screen.findByText("No message selected")).toBeInTheDocument();
  });

  it("lists messages with sender, subject and unread state", async () => {
    mockMailApi({
      messages: [
        testMessage(1, { unread: true, has_attachments: true }),
        testMessage(2, { subject: "", sender: null }),
      ],
    });
    await renderApp("/inbox");
    const list = await screen.findByRole("list", { name: "Messages" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(rows[0] as HTMLElement).getByText("Sender 1")).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByRole("img", { name: "Unread" })).toBeInTheDocument();
    expect(within(rows[0] as HTMLElement).getByLabelText("Has attachments")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("(No subject)")).toBeInTheDocument();
    expect(within(rows[1] as HTMLElement).getByText("Unknown sender")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "Inbox" }).parentElement,
    ).toHaveTextContent("2");
  });

  it("filters unread messages", async () => {
    const { queries } = mockMailApi({
      messages: [testMessage(1, { unread: true }), testMessage(2)],
    });
    await renderApp("/inbox");
    await screen.findByText("Sender 2");
    await userEvent.click(screen.getByRole("button", { name: "Unread" }));
    await waitFor(() => expect(screen.queryByText("Sender 2")).not.toBeInTheDocument());
    expect(queries.at(-1)?.get("unread")).toBe("true");
  });

  it("opens a message, marks it read and toggles unread with u", async () => {
    const id = messageId(1);
    const { patches } = mockMailApi({
      messages: [testMessage(1, { unread: true }), testMessage(2)],
      threads: { [id]: testThread(1, { unread: true, text: "Hello Erika" }) },
    });
    const { router } = await renderApp("/inbox");
    await screen.findByText("Sender 1");

    // j selects the first row, Enter opens it.
    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "Enter" });
    expect(await screen.findByText("Hello Erika")).toBeInTheDocument();
    expect(router.state.location.search).toMatchObject({ message: id });
    await waitFor(() => expect(patches).toEqual([{ id, body: { seen: true } }]));

    fireEvent.keyDown(document.body, { key: "u" });
    await waitFor(() => expect(patches.at(-1)).toEqual({ id, body: { seen: false } }));
    expect(await screen.findByRole("button", { name: "Mark as read" })).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(router.state.location.search).not.toHaveProperty("message"));
  });

  it("loads the body of an older message when it is expanded", async () => {
    const id = messageId(2);
    const older = messageId(1);
    const thread: Thread = {
      thread_id: "0199c100-0000-7000-8000-000000000001",
      mailbox_id: testMailbox().id,
      subject: "Subject 1",
      messages: [
        testDetail(1, { snippet: "Earlier snippet", body: null }),
        testDetail(2, { text: "Newest body" }),
      ],
    };
    const { bodyRequests } = mockMailApi({
      messages: [testMessage(2)],
      threads: { [id]: thread },
      bodies: { [older]: { html: null, blocked_images: 0, text: "Earlier body" } },
    });
    await renderApp(`/inbox?message=${id}`);
    expect(await screen.findByText("Newest body")).toBeInTheDocument();
    // Collapsed: only the snippet, nothing loaded yet.
    expect(screen.getByText("Earlier snippet")).toBeInTheDocument();
    expect(bodyRequests).toEqual([]);

    await userEvent.click(screen.getByRole("button", { name: /Sender 1/ }));
    expect(await screen.findByText("Earlier body")).toBeInTheDocument();
    expect(bodyRequests).toEqual([older]);
  });

  it("offers a retry when the body of an expanded message fails to load", async () => {
    const id = messageId(2);
    const older = messageId(1);
    const bodies: Record<string, MessageBody | null> = { [older]: null };
    mockMailApi({
      messages: [testMessage(2)],
      threads: {
        [id]: {
          thread_id: "0199c100-0000-7000-8000-000000000001",
          mailbox_id: testMailbox().id,
          subject: "Subject 1",
          messages: [testDetail(1, { body: null }), testDetail(2)],
        },
      },
      bodies,
    });
    await renderApp(`/inbox?message=${id}`);
    await userEvent.click(await screen.findByRole("button", { name: /Sender 1/ }));
    const retry = await screen.findByRole("button", { name: "Try again" });
    bodies[older] = { html: null, blocked_images: 0, text: "Earlier body" };
    await userEvent.click(retry);
    expect(await screen.findByText("Earlier body")).toBeInTheDocument();
  });

  it("shows HTML in a sandboxed frame and blocks external images until asked", async () => {
    const id = messageId(3);
    mockMailApi({
      messages: [testMessage(3)],
      threads: {
        [id]: testThread(3, {
          body: { html: "<p>Figures</p>", blocked_images: 2, text: "Figures" },
        }),
      },
    });
    await renderApp(`/inbox?message=${id}`);
    const frame = await screen.findByTitle("Content of the message “Subject 3”");
    expect(frame).toHaveAttribute(
      "sandbox",
      "allow-same-origin allow-popups allow-popups-to-escape-sandbox",
    );
    expect(frame.getAttribute("sandbox")).not.toContain("allow-scripts");
    const document = frame.getAttribute("srcdoc") ?? "";
    expect(document).toContain("<p>Figures</p>");
    expect(document).toContain("img-src 'self' data:;");
    expect(
      screen.getByText("2 external images were blocked to protect your privacy."),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Load images" })).toBeInTheDocument();
  });

  it("links attachments for download", async () => {
    const id = messageId(4);
    mockMailApi({
      messages: [testMessage(4)],
      threads: {
        [id]: testThread(4, {
          attachments: [
            {
              id: "0199d000-0000-7000-8000-000000000001",
              filename: "figures.pdf",
              content_type: "application/pdf",
              size: 2048,
              is_inline: false,
            },
            {
              id: "0199d000-0000-7000-8000-000000000002",
              filename: "logo.png",
              content_type: "image/png",
              size: 10,
              is_inline: true,
            },
          ],
        }),
      },
    });
    await renderApp(`/inbox?message=${id}`);
    const link = await screen.findByRole("link", { name: /figures\.pdf/ });
    expect(link).toHaveAttribute(
      "href",
      `/api/messages/${id}/attachments/0199d000-0000-7000-8000-000000000001`,
    );
    expect(screen.queryByText("logo.png")).not.toBeInTheDocument();
  });

  it("stacks list and message on narrow screens", async () => {
    setViewportWidth(390);
    const id = messageId(5);
    mockMailApi({
      messages: [testMessage(5)],
      threads: { [id]: testThread(5, { text: "Mobile body" }) },
    });
    const { router } = await renderApp(`/inbox?message=${id}`);
    expect(await screen.findByText("Mobile body")).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Messages" })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Back to the list" }));
    expect(await screen.findByRole("list", { name: "Messages" })).toBeInTheDocument();
    expect(router.state.location.search).not.toHaveProperty("message");
  });

  it("lists shared mailboxes separately and keeps them read only", async () => {
    const shared = testMailbox({
      id: SHARED_ID,
      display_name: "Support",
      address: "support@example.org",
      is_shared: true,
      permissions: ["read"],
    });
    const id = messageId(7);
    const { patches } = mockMailApi({
      mailboxes: [testMailbox(), shared],
      messages: [testMessage(7, { unread: true, mailbox_id: SHARED_ID })],
      threads: {
        [id]: {
          ...testThread(7, {
            unread: true,
            text: "Customer question",
            mailbox_id: SHARED_ID,
          }),
          mailbox_id: SHARED_ID,
        },
      },
    });
    await renderApp(`/inbox?mailbox=${SHARED_ID}&message=${id}`);

    // Own section in the navigation, the inbox titled with the mailbox.
    const section = await screen.findByRole("region", { name: "Shared mailboxes" });
    expect(within(section).getByRole("link", { name: "Support" })).toHaveAttribute(
      "href",
      `/inbox?mailbox=${SHARED_ID}`,
    );
    expect(screen.getByRole("heading", { level: 1, name: "Support" })).toBeInTheDocument();
    // Only the shared mailbox is the current page, not the inbox as a whole.
    expect(within(section).getByRole("link", { name: "Support" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    expect(screen.getAllByRole("link", { name: "Inbox" })[0]).not.toHaveAttribute("aria-current");
    expect(
      within(screen.getByRole("combobox", { name: "Mailbox" })).getByRole("group", {
        name: "Shared mailboxes",
      }),
    ).toBeInTheDocument();

    // Opening does not mark it read; the read state cannot be toggled.
    expect(await screen.findByText("Customer question")).toBeInTheDocument();
    expect(screen.getByText("Shared mailbox, read only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark as read" })).not.toBeInTheDocument();
    fireEvent.keyDown(document.body, { key: "u" });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(patches).toEqual([]);
  });
});

import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/api/errors";
import { isUnchangedSuggestion, sendErrorKey } from "@/lib/reply-draft";
import { type DraftsBackend, draftsApi, testDraft } from "@/test/drafts";
import { backend, json, mockFetch } from "@/test/fetch";
import { messageId, testMailbox, testThread } from "@/test/mail";
import { renderApp } from "@/test/render-app";

const opened = messageId(3);

/** Mail and drafts endpoints on top of the default backend. */
function mockReplyApi(options: DraftsBackend & { shared?: boolean } = {}) {
  const drafts = draftsApi(options);
  const base = backend();
  mockFetch(async (request) => {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    const handled = await drafts.handle(request);
    if (handled) return handled;
    if (route === "GET /api/mailboxes") {
      return json([testMailbox(options.shared ? { is_shared: true, permissions: ["read"] } : {})]);
    }
    if (route === "GET /api/messages") return json({ items: [], total: 0, next_cursor: null });
    if (route === `GET /api/messages/${opened}/thread`) return json(testThread(3));
    if (route.startsWith("PATCH /api/messages/")) return json({});
    return base(request);
  });
  return drafts;
}

const routes = (drafts: ReturnType<typeof draftsApi>) => drafts.requests.map((r) => r.route);

/** Focuses the element directly: in jsdom a pointer click also lands on the resize handle. */
async function typeInto(field: HTMLElement, text: string) {
  act(() => field.focus());
  await userEvent.keyboard(text);
}

async function openThread() {
  await renderApp(`/inbox?message=${opened}`);
  await screen.findByRole("article", { name: "Message from Sender 3" });
}

function editor() {
  return screen.getByRole("region", { name: "Reply" });
}

describe("reply drafts in the thread", () => {
  it("r starts a reply, the text is saved automatically and sent with Send", async () => {
    const drafts = mockReplyApi();
    await openThread();
    await screen.findByRole("button", { name: /^Reply(R)?$/ });

    await userEvent.keyboard("r");
    const body = await screen.findByRole("textbox", { name: "Reply text" });
    await waitFor(() => expect(body).toHaveFocus());
    expect(drafts.requests).toContainEqual({
      route: "POST /api/drafts",
      body: { message_id: opened, reply_all: false, body: "" },
    });
    expect(within(editor()).getByText("Sender 3 <sender3@example.org>")).toBeInTheDocument();

    await userEvent.keyboard("Hallo, passt gut.");
    await waitFor(
      () =>
        expect(drafts.requests).toContainEqual({
          route: expect.stringMatching(/^PATCH \/api\/drafts\//),
          body: { body: "Hallo, passt gut." },
        }),
      { timeout: 3000 },
    );
    await waitFor(
      () =>
        expect(
          within(editor())
            .getAllByRole("status")
            .map((e) => e.textContent),
        ).toContain("Saved"),
      { timeout: 3000 },
    );

    // ⌘Enter / Ctrl+Enter in the field sends (no confirmation: the text was written by hand).
    fireEvent.keyDown(body, { key: "Enter", ctrlKey: true });
    expect((await screen.findAllByText("Reply sent"))[0]).toBeInTheDocument();
    expect(routes(drafts)).toContainEqual(expect.stringMatching(/^POST \/api\/drafts\/.+\/send$/));
    expect(screen.queryByRole("region", { name: "Reply" })).not.toBeInTheDocument();
    // Typing and the autosave delay take a few seconds in jsdom.
  }, 15_000);

  it("a suggested draft streams into the editor; sending it unchanged asks first", async () => {
    const drafts = mockReplyApi({ generated: "Hallo Sender,\n\ndanke, ich komme gerne." });
    await openThread();

    await userEvent.click(await screen.findByRole("button", { name: /^Reply(R)?$/ }));
    await userEvent.click(await within(editor()).findByRole("button", { name: "Suggest draft" }));
    const instruction = await screen.findByRole("textbox", {
      name: "Short instruction (optional)",
    });
    await typeInto(instruction, "kurz zusagen{Enter}");

    const body = screen.getByRole("textbox", { name: "Reply text" });
    await waitFor(() => expect(body).toHaveValue("Hallo Sender,\n\ndanke, ich komme gerne."));
    expect(drafts.requests).toContainEqual({
      route: "POST /api/drafts/generate",
      body: expect.objectContaining({ message_id: opened, instruction: "kurz zusagen" }),
    });

    await userEvent.click(within(editor()).getByRole("button", { name: /^Send/ }));
    expect(
      await screen.findByText("The suggested draft is unchanged. Please review it before sending."),
    ).toBeInTheDocument();
    expect(routes(drafts)).not.toContainEqual(expect.stringMatching(/\/send$/));

    await userEvent.click(screen.getByRole("button", { name: "Send anyway" }));
    expect((await screen.findAllByText("Reply sent"))[0]).toBeInTheDocument();
    expect(routes(drafts)).toContainEqual(expect.stringMatching(/\/send$/));
  });

  it("an edited suggestion is sent without the extra confirmation", async () => {
    const drafts = mockReplyApi({ generated: "Danke, passt." });
    await openThread();
    await userEvent.click(await screen.findByRole("button", { name: /^Reply(R)?$/ }));
    await userEvent.click(await within(editor()).findByRole("button", { name: "Suggest draft" }));
    await typeInto(await screen.findByRole("textbox", { name: /Short instruction/ }), "{Enter}");
    const body = screen.getByRole("textbox", { name: "Reply text" });
    await waitFor(() => expect(body).toHaveValue("Danke, passt."));

    await typeInto(body, " Bis Montag.");
    await userEvent.click(within(editor()).getByRole("button", { name: /^Send/ }));
    expect((await screen.findAllByText("Reply sent"))[0]).toBeInTheDocument();
    expect(drafts.requests).toContainEqual({
      route: expect.stringMatching(/^PATCH /),
      body: { body: "Danke, passt. Bis Montag." },
    });
  });

  it("shows a failed generation and keeps the previous text", async () => {
    mockReplyApi({ generateError: "llm_unavailable" });
    await openThread();
    await userEvent.click(await screen.findByRole("button", { name: /^Reply(R)?$/ }));
    const body = await screen.findByRole("textbox", { name: "Reply text" });
    await typeInto(body, "Mein Text");
    await userEvent.click(within(editor()).getByRole("button", { name: "Suggest draft" }));
    expect(screen.getByText("The current text will be replaced.")).toBeInTheDocument();
    await typeInto(screen.getByRole("textbox", { name: /Short instruction/ }), "{Enter}");

    const alert = await within(editor()).findByRole("alert");
    expect(alert).toHaveTextContent("No draft created");
    expect(alert).toHaveTextContent("The language model cannot be reached right now.");
    expect(body).toHaveValue("Mein Text");
  });

  it("reports a timeout separately from an outage", async () => {
    mockReplyApi({ generateError: "llm_timeout" });
    await openThread();
    await userEvent.click(await screen.findByRole("button", { name: /^Reply(R)?$/ }));
    const body = await screen.findByRole("textbox", { name: "Reply text" });
    await typeInto(body, "Mein Text");
    await userEvent.click(within(editor()).getByRole("button", { name: "Suggest draft" }));
    await typeInto(screen.getByRole("textbox", { name: /Short instruction/ }), "{Enter}");

    const alert = await within(editor()).findByRole("alert");
    expect(alert).toHaveTextContent("No draft created");
    expect(alert).toHaveTextContent(
      "The language model took too long. Please try again or write the reply yourself.",
    );
    expect(alert).not.toHaveTextContent("cannot be reached");
    expect(body).toHaveValue("Mein Text");
  });

  it("shows why sending failed and keeps the draft open", async () => {
    mockReplyApi({ sendError: [502, "recipients_refused"] });
    await openThread();
    await userEvent.keyboard("r");
    const body = await screen.findByRole("textbox", { name: "Reply text" });
    await typeInto(body, "Hallo");
    await userEvent.click(within(editor()).getByRole("button", { name: /^Send/ }));

    const alert = await within(editor()).findByRole("alert");
    expect(alert).toHaveTextContent("Not sent");
    expect(alert).toHaveTextContent("The mail server refused one or more recipients.");
    expect(body).toHaveValue("Hallo");
  });

  it("a reply all lists the copies; a stored draft opens with the thread", async () => {
    mockReplyApi({
      drafts: [testDraft(1, { message_id: opened, body: "Angefangen", reply_all: true })],
    });
    await openThread();
    const body = await screen.findByRole("textbox", { name: "Reply text" });
    expect(body).toHaveValue("Angefangen");
    expect(within(editor()).getByRole("radio", { name: "Reply all" })).toBeChecked();

    await userEvent.click(within(editor()).getByRole("radio", { name: "Reply" }));
    await waitFor(() =>
      expect(within(editor()).getByRole("radio", { name: "Reply" })).toBeChecked(),
    );
  });

  it("discarding closes the editor", async () => {
    const drafts = mockReplyApi({ drafts: [testDraft(1, { message_id: opened, body: "Text" })] });
    await openThread();
    await screen.findByRole("textbox", { name: "Reply text" });
    await userEvent.click(within(editor()).getByRole("button", { name: "Discard draft" }));
    expect((await screen.findAllByText("Draft discarded"))[0]).toBeInTheDocument();
    expect(routes(drafts)).toContainEqual(expect.stringMatching(/\/discard$/));
    expect(await screen.findByRole("button", { name: /^Reply(R)?$/ })).toBeInTheDocument();
  });

  it("offers no reply in a read-only (shared) mailbox", async () => {
    mockReplyApi({ shared: true });
    await openThread();
    expect(screen.queryByRole("button", { name: /^Reply(R)?$/ })).not.toBeInTheDocument();
    await userEvent.keyboard("r");
    expect(screen.queryByRole("textbox", { name: "Reply text" })).not.toBeInTheDocument();
  });
});

describe("drafts overview", () => {
  it("lists open drafts with a link to the mail", async () => {
    mockReplyApi({
      drafts: [
        testDraft(1, { message_id: opened, body: "Danke für die Einladung\nmehr Text" }),
        testDraft(2, { subject: "Re: Alte Mail", body: "" }),
      ],
    });
    await renderApp("/drafts");
    const list = await screen.findByRole("list", { name: "Drafts" });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0] as HTMLElement).getByRole("link")).toHaveAttribute(
      "href",
      `/inbox?message=${opened}`,
    );
    expect(items[0]).toHaveTextContent("Danke für die Einladung");
    expect(items[1]).toHaveTextContent("(No text)");
    expect(items[1]).toHaveTextContent("The mail you replied to was deleted");
  });

  it("shows an empty state", async () => {
    mockReplyApi();
    await renderApp("/drafts");
    expect(await screen.findByText("No drafts")).toBeInTheDocument();
  });

  it("discards a draft from the list", async () => {
    const drafts = mockReplyApi({
      drafts: [testDraft(1, { message_id: opened, body: "Text" })],
    });
    await renderApp("/drafts");
    await userEvent.click(
      await screen.findByRole("button", { name: "Discard draft “Re: Subject 3”" }),
    );
    expect(await screen.findByText("No drafts")).toBeInTheDocument();
    expect(routes(drafts)).toContainEqual(expect.stringMatching(/\/discard$/));
  });
});

describe("reply draft helpers", () => {
  it("treats only the untouched suggestion as unchanged", () => {
    expect(isUnchangedSuggestion("Hallo", undefined)).toBe(false);
    expect(isUnchangedSuggestion("Hallo\n", "Hallo")).toBe(true);
    expect(isUnchangedSuggestion("Hallo!", "Hallo")).toBe(false);
  });

  it("maps send failures to messages", () => {
    expect(sendErrorKey(new ApiError(403, { status: 403, error_code: "read_only" }))).toBe(
      "read_only",
    );
    expect(sendErrorKey(new ApiError(502, { status: 502, error_code: "imap_bad" }))).toBe(
      "rejected",
    );
    expect(sendErrorKey(new ApiError(503, { status: 503, error_code: "connection_failed" }))).toBe(
      "unreachable",
    );
    expect(sendErrorKey(new ApiError(409, { status: 409, error_code: "oauth_missing" }))).toBe(
      "not_configured",
    );
    expect(sendErrorKey(new ApiError(0))).toBeUndefined();
  });
});

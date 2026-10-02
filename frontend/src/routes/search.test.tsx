import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Thread } from "@/api/mail";
import type { AnswerEvent, ConversationSummary, SearchHit } from "@/api/search";
import { backend, json, mockFetch, problem } from "@/test/fetch";
import { MAILBOX_ID, messageId, testMailbox, testThread } from "@/test/mail";
import { renderApp } from "@/test/render-app";

const CONVERSATION = "0199e000-0000-7000-8000-000000000001";

function hit(index: number, overrides: Partial<SearchHit> = {}): SearchHit {
  return {
    message_id: messageId(index),
    mailbox_id: MAILBOX_ID,
    thread_id: null,
    subject: `Rechnung ${index}`,
    sender: { name: `Sender ${index}`, address: `sender${index}@example.org` },
    date: "2026-10-01T08:00:00Z",
    source: "body",
    attachment_id: null,
    attachment_filename: null,
    excerpt: `Anbei die Rechnung für September, Nummer ${index}.`,
    score: 0.03,
    ...overrides,
  };
}

const sources = [
  {
    number: 1,
    message_id: messageId(1),
    mailbox_id: MAILBOX_ID,
    attachment_id: null,
    source: "body",
    heading: "From: Sender 1 <sender1@example.org>\nDate: 2026-10-01\nSubject: Rechnung 1",
    snippet: "Zahlbar innerhalb von 14 Tagen.",
  },
];

function sse(events: AnswerEvent[], { hold = false } = {}) {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const event of events) {
        controller.enqueue(
          encoder.encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`),
        );
      }
      if (!hold) controller.close();
    },
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

const start: AnswerEvent = {
  type: "start",
  conversation_id: CONVERSATION,
  question_id: "0199e000-0000-7000-8000-0000000000a1",
  answer_id: "0199e000-0000-7000-8000-0000000000a2",
};

interface SearchBackend {
  hits?: SearchHit[];
  answer?: () => Response;
  conversations?: ConversationSummary[];
  threads?: Record<string, Thread>;
}

function mockSearchApi({
  hits = [],
  answer = () => problem(500),
  conversations = [],
  threads = {},
}: SearchBackend = {}) {
  const searches: unknown[] = [];
  const asks: unknown[] = [];
  const deletes: string[] = [];
  let stored = [...conversations];
  const base = backend();
  mockFetch(async (request) => {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (route === "GET /api/mailboxes") return json([testMailbox()]);
    if (route === "GET /api/triage/categories") return json([]);
    if (route === "POST /api/search") {
      searches.push(await request.json());
      return json({ hits });
    }
    if (route === "POST /api/rag/ask") {
      asks.push(await request.json());
      return answer();
    }
    if (route === "GET /api/rag/conversations") return json(stored);
    if (route === "DELETE /api/rag/conversations") {
      deletes.push("all");
      stored = [];
      return new Response(null, { status: 204 });
    }
    const conversation = /^(GET|DELETE) \/api\/rag\/conversations\/([^/]+)$/.exec(route);
    if (conversation?.[1] === "DELETE" && conversation[2]) {
      deletes.push(conversation[2]);
      stored = stored.filter((item) => item.id !== conversation[2]);
      return new Response(null, { status: 204 });
    }
    if (conversation?.[1] === "GET") return problem(404);
    const thread = /^GET \/api\/messages\/([^/]+)\/thread$/.exec(route);
    if (thread?.[1] && threads[thread[1]]) return json(threads[thread[1]]);
    return base(request);
  });
  return { searches, asks, deletes };
}

/**
 * Types into the search field. Focus is set directly: in jsdom (no layout) a click inside the
 * resizable panels counts as a click on the column handle, which takes the focus.
 */
async function type(text: string) {
  const input = await screen.findByRole("searchbox", { name: "Search terms or question" });
  input.focus();
  await userEvent.keyboard(text);
  return input;
}

describe("search", () => {
  it("shows hits with marked terms and applies filter chips", async () => {
    const { searches } = mockSearchApi({ hits: [hit(1), hit(2)] });
    await renderApp("/search");
    await type("rechnung{Enter}");

    const list = await screen.findByRole("list", { name: "Results" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    const marks = (rows[0] as HTMLElement).querySelectorAll("mark");
    expect([...marks].map((mark) => mark.textContent)).toEqual(["Rechnung", "Rechnung"]);
    expect(searches).toEqual([{ query: "rechnung", filters: {}, limit: 30 }]);

    // Period chip: last 30 days.
    // Keyboard only (also the way the chips work without a mouse).
    screen.getByRole("button", { name: "Date" }).focus();
    await userEvent.keyboard("{Enter}");
    const option = await screen.findByRole("menuitemradio", { name: "Last 30 days" });
    option.focus();
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(searches).toHaveLength(2));
    expect(searches[1]).toMatchObject({ filters: { since: expect.any(String) } });
    expect(screen.getByRole("button", { name: "Date: Last 30 days" })).toBeInTheDocument();

    // Sender chip is edited inline.
    await userEvent.click(screen.getByRole("button", { name: "Sender" }));
    const sender = await screen.findByRole("textbox", { name: "Sender" });
    sender.focus();
    await userEvent.keyboard("lena{Enter}");
    await waitFor(() => expect(searches).toHaveLength(3));
    expect(searches[2]).toMatchObject({ filters: { sender: "lena" } });

    await userEvent.click(screen.getByRole("button", { name: "Remove filter “Sender”" }));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: "Sender: lena" })).not.toBeInTheDocument(),
    );
  });

  it("marks hits from scanned attachments as OCR", async () => {
    mockSearchApi({
      hits: [
        hit(1, {
          source: "attachment_ocr",
          attachment_id: "0199e000-0000-7000-8000-0000000000b1",
          attachment_filename: "scan-rechnung.pdf",
        }),
        hit(2, {
          source: "attachment",
          attachment_id: "0199e000-0000-7000-8000-0000000000b2",
          attachment_filename: "rechnung.pdf",
        }),
      ],
    });
    await renderApp("/search");
    await type("rechnung{Enter}");

    const list = await screen.findByRole("list", { name: "Results" });
    const [scan, text] = within(list).getAllByRole("listitem") as HTMLElement[];
    expect(within(scan as HTMLElement).getByText("scan-rechnung.pdf")).toBeInTheDocument();
    expect(within(scan as HTMLElement).getByText("(OCR)")).toHaveAttribute(
      "title",
      "Text recognised from a scan (OCR), may contain reading errors",
    );
    expect(within(text as HTMLElement).getByText("rechnung.pdf")).toBeInTheDocument();
    expect(within(text as HTMLElement).queryByText("(OCR)")).not.toBeInTheDocument();
  });

  it("opens a hit with the keyboard at the matching passage", async () => {
    const id = messageId(1);
    mockSearchApi({
      hits: [hit(1, { excerpt: "die Rechnung für September liegt bei" })],
      threads: {
        [id]: testThread(1, {
          text: "Hallo Erika,\n\ndie Rechnung für September liegt bei.\n\nJonas",
        }),
      },
    });
    const { router } = await renderApp("/search");
    const input = await type("rechnung{Enter}");
    await screen.findByRole("list", { name: "Results" });
    input.blur();

    fireEvent.keyDown(document.body, { key: "j" });
    fireEvent.keyDown(document.body, { key: "Enter" });
    await waitFor(() => expect(router.state.location.search).toMatchObject({ message: id }));
    const mark = await screen.findByText("die Rechnung für September liegt bei", {
      selector: "mark",
    });
    expect(mark).toBeInTheDocument();

    fireEvent.keyDown(document.body, { key: "Escape" });
    await waitFor(() => expect(router.state.location.search).not.toHaveProperty("message"));
  });

  it("streams an answer with numbered sources and opens a source", async () => {
    const id = messageId(1);
    const { asks } = mockSearchApi({
      threads: { [id]: testThread(1, { text: "Zahlbar innerhalb von 14 Tagen." }) },
      answer: () =>
        sse([
          start,
          { type: "filters", filters: { extracted: [] } },
          { type: "sources", sources },
          { type: "token", text: "Innerhalb von 14 Tagen " },
          { type: "token", text: "[1]." },
          { type: "done", status: "answered", citations: [1], ttft_ms: 120 },
        ]),
    });
    const { router } = await renderApp("/search");
    await type("Bis wann ist die Rechnung zu zahlen?{Enter}");

    const answer = await screen.findByRole("article", {
      name: "Bis wann ist die Rechnung zu zahlen?",
    });
    await within(answer).findByText(/Innerhalb von 14 Tagen/);
    expect(asks).toEqual([
      { question: "Bis wann ist die Rechnung zu zahlen?", conversation_id: null, filters: {} },
    ]);
    await waitFor(() =>
      expect(router.state.location.search).toMatchObject({ conversation: CONVERSATION }),
    );
    const list = within(answer).getByRole("region", { name: "Sources" });
    expect(within(list).getByText("Sender 1")).toBeInTheDocument();

    await userEvent.click(within(answer).getByRole("button", { name: "Open source 1" }));
    await waitFor(() => expect(router.state.location.search).toMatchObject({ message: id }));
    expect(
      await screen.findByText("Zahlbar innerhalb von 14 Tagen.", { selector: "mark" }),
    ).toBeInTheDocument();
  });

  it("says when the mail has no evidence", async () => {
    mockSearchApi({
      answer: () =>
        sse([
          start,
          { type: "sources", sources: [] },
          { type: "token", text: "Dazu habe ich nichts gefunden." },
          { type: "done", status: "no_evidence", citations: [], ttft_ms: 80 },
        ]),
    });
    await renderApp("/search");
    await type("Wann ist das Sommerfest?{Enter}");
    expect(await screen.findByText("Not enough evidence")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Sources" })).not.toBeInTheDocument();
  });

  it("cancels a running answer with Escape", async () => {
    mockSearchApi({
      answer: () =>
        sse([start, { type: "sources", sources }, { type: "token", text: "Teil" }], { hold: true }),
    });
    await renderApp("/search");
    await type("Was steht in der Rechnung?{Enter}");
    expect(await screen.findByRole("button", { name: /Cancel/ })).toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("searchbox"), { key: "Escape" });
    expect(await screen.findByText("Cancelled. The answer was not saved.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Cancel/ })).not.toBeInTheDocument();
  });

  it("reports a model outage from the stream", async () => {
    mockSearchApi({ answer: () => sse([start, { type: "error", code: "llm_unavailable" }]) });
    await renderApp("/search");
    await type("Wer kommt morgen?{Enter}");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The language model is not reachable right now.",
    );
  });

  it("lists the history and deletes entries", async () => {
    const { deletes } = mockSearchApi({
      conversations: [
        {
          id: CONVERSATION,
          title: "Wann kommt die Rechnung?",
          created_at: "2026-10-01T08:00:00Z",
          updated_at: "2026-10-01T08:00:00Z",
        },
        {
          id: "0199e000-0000-7000-8000-000000000002",
          title: "Wer vertritt Lena?",
          created_at: "2026-09-30T08:00:00Z",
          updated_at: "2026-09-30T08:00:00Z",
        },
      ],
    });
    await renderApp("/search");
    expect(await screen.findByRole("link", { name: /Wann kommt die Rechnung/ })).toHaveAttribute(
      "href",
      `/search?conversation=${CONVERSATION}`,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Delete “Wann kommt die Rechnung?”" }),
    );
    await waitFor(() => expect(deletes).toEqual([CONVERSATION]));
    await waitFor(() =>
      expect(
        screen.queryByRole("link", { name: /Wann kommt die Rechnung/ }),
      ).not.toBeInTheDocument(),
    );

    await userEvent.click(screen.getByRole("button", { name: "Clear history" }));
    expect(screen.getByText("Delete all questions and answers?")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Clear history" }));
    await waitFor(() => expect(deletes).toEqual([CONVERSATION, "all"]));
    expect(await screen.findByText("Search your mail")).toBeInTheDocument();
  });
});

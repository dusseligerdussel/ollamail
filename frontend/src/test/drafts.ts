import type { Draft, DraftEvent } from "@/api/drafts";

import { json, problem } from "./fetch";
import { MAILBOX_ID } from "./mail";

/** Synthetic reply drafts for component tests (invented names, example addresses). */
export function draftId(index: number) {
  return `0199e000-0000-7000-8000-${String(index).padStart(12, "0")}`;
}

export function testDraft(index: number, overrides: Partial<Draft> = {}): Draft {
  return {
    id: draftId(index),
    message_id: null,
    mailbox_id: MAILBOX_ID,
    thread_id: null,
    status: "draft",
    reply_all: false,
    to: [{ name: "Sender 3", address: "sender3@example.org" }],
    cc: [],
    subject: "Re: Subject 3",
    body: "",
    quote_original: true,
    instruction: null,
    language: null,
    model: null,
    created_at: "2026-10-02T08:00:00Z",
    updated_at: "2026-10-02T08:30:00Z",
    sent_at: null,
    can_send: true,
    ...overrides,
  };
}

/** A `text/event-stream` response with the given events, one chunk each. */
export function eventStream(events: DraftEvent[]) {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const event of events) {
        controller.enqueue(
          encoder.encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`),
        );
      }
      controller.close();
    },
  });
  return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
}

export interface DraftsBackend {
  drafts?: Draft[];
  /** Events of `POST /drafts/generate`; `done` gets the stored draft. */
  generated?: string;
  /** `error` event instead of a text. */
  generateError?: string;
  /** `POST /drafts/{id}/send` fails with this status and `error_code`. */
  sendError?: [number, string];
}

/** In-memory drafts API; records the requests that change data. */
export function draftsApi({
  drafts = [],
  generated,
  generateError,
  sendError,
}: DraftsBackend = {}) {
  const store = new Map(drafts.map((draft) => [draft.id, draft]));
  const requests: { route: string; body?: Record<string, unknown> }[] = [];
  let created = 0;

  async function handle(request: Request): Promise<Response | undefined> {
    const url = new URL(request.url);
    const route = `${request.method} ${url.pathname}`;
    if (!url.pathname.startsWith("/api/drafts")) return undefined;
    const body =
      request.method === "GET" || request.method === "DELETE"
        ? undefined
        : ((await request.json().catch(() => ({}))) as Record<string, unknown>);
    requests.push({ route, body });

    if (route === "GET /api/drafts") {
      const message = url.searchParams.get("message_id");
      return json(
        [...store.values()].filter(
          (draft) => draft.status === "draft" && (!message || draft.message_id === message),
        ),
      );
    }
    if (route === "POST /api/drafts") {
      created += 1;
      const draft = testDraft(100 + created, {
        message_id: String(body?.message_id),
        reply_all: Boolean(body?.reply_all),
        cc: body?.reply_all ? [{ name: null, address: "team@example.org" }] : [],
      });
      store.set(draft.id, draft);
      return json(draft, { status: 201 });
    }
    if (route === "POST /api/drafts/generate") {
      const draft = store.get(String(body?.draft_id));
      if (!draft) return problem(404);
      if (generateError) {
        return eventStream([
          { type: "start", draft_id: draft.id },
          { type: "error", code: generateError },
        ]);
      }
      const text = generated ?? "";
      const stored = { ...draft, body: text, model: "test-model" };
      store.set(draft.id, stored);
      return eventStream([
        { type: "start", draft_id: draft.id },
        ...(text.match(/\S+\s*/g) ?? []).map((part) => ({ type: "token" as const, text: part })),
        { type: "done", draft: stored, ttft_ms: 120 },
      ]);
    }
    const match = /^(\w+) \/api\/drafts\/([^/]+)(\/\w+)?$/.exec(route);
    const draft = match?.[2] ? store.get(match[2]) : undefined;
    if (!match || !draft) return problem(404);
    const [, method, , action] = match;
    if (method === "PATCH" && !action) {
      const changed = { ...draft, ...body } as Draft;
      if (body && "reply_all" in body) {
        changed.cc = body.reply_all ? [{ name: null, address: "team@example.org" }] : [];
      }
      store.set(draft.id, changed);
      return json(changed);
    }
    if (method === "POST" && action === "/send") {
      if (sendError) return problem(sendError[0], { error_code: sendError[1] });
      const sent = { ...draft, status: "sent" as const, sent_at: "2026-10-02T09:00:00Z" };
      store.set(draft.id, sent);
      return json(sent);
    }
    if (method === "POST" && action === "/discard") {
      const discarded = { ...draft, status: "discarded" as const };
      store.set(draft.id, discarded);
      return json(discarded);
    }
    if (method === "DELETE" && !action) {
      store.delete(draft.id);
      return new Response(null, { status: 204 });
    }
    return problem(404);
  }

  return { store, requests, handle };
}

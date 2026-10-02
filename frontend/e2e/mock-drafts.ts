import type { Page, Route } from "@playwright/test";

import { mailboxIds, messageId, NOW } from "./mock-mail";

/**
 * In-browser mock of the reply drafts API with synthetic data (invented names, `example.*`
 * addresses). Register after `mockApi` and `mockMail`.
 *
 * The generation stream (`POST /api/drafts/generate`) is produced inside the page by a
 * wrapped `fetch`, so it really arrives piece by piece (Playwright can only fulfil whole
 * responses).
 */
export interface MockDrafts {
  /** Text of the suggested draft. */
  suggestion?: string;
  /** `error` event with this code instead of a text. */
  generateError?: string;
  /** Delay between stream events in ms. */
  step?: number;
  /** Stop after this many events and keep the stream open (screenshots while writing). */
  pauseAfter?: number;
  /** `POST /api/drafts/{id}/send` fails with this status and `error_code`. */
  sendError?: [number, string];
  /** Open drafts that exist already. */
  drafts?: StoredDraft[];
}

type StoredDraft = ReturnType<typeof draft>;

/**
 * The mail of the inbox mock that the specs answer (plain text: axe cannot look into the
 * sandboxed frame of HTML mails).
 */
export const answered = messageId(1);

export const SUGGESTION =
  "Hallo Lena,\n\ndanke für den Hinweis. Ich verschiebe das Update des Ticketsystems auf Montag früh und sage dem Team Bescheid.\n\nViele Grüße\nErika";

const id = (index: number) => `0193a000-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;

export function draft(index: number, overrides: Record<string, unknown> = {}) {
  return {
    id: id(index),
    message_id: answered as string | null,
    mailbox_id: mailboxIds.work,
    thread_id: null,
    status: "draft" as "draft" | "sent" | "discarded",
    reply_all: false,
    to: [{ name: "Lena Muster", address: "lena.muster@example.com" }],
    cc: [] as { name: string | null; address: string }[],
    subject: "Re: Wartungsfenster am Samstag",
    body: "",
    quote_original: true,
    instruction: null as string | null,
    language: "de",
    model: null as string | null,
    created_at: NOW.toISOString(),
    updated_at: NOW.toISOString(),
    sent_at: null as string | null,
    can_send: true,
    ...overrides,
  };
}

/** Open drafts for the overview, of the mails in the inbox mock. */
export function overviewDrafts() {
  const hour = 3_600_000;
  return [
    draft(1, {
      body: "Hallo Lena,\n\nSamstag passt, ich gebe dem Support vorher Bescheid.",
      updated_at: new Date(NOW.getTime() - hour).toISOString(),
    }),
    draft(2, {
      message_id: messageId(3),
      to: [{ name: "Rechnungsstelle", address: "billing@example.net" }],
      subject: "Re: Rechnung 2026-1042",
      body: "Guten Tag,\n\ndie Rechnung ist angewiesen.",
      updated_at: new Date(NOW.getTime() - 5 * hour).toISOString(),
    }),
    draft(3, {
      message_id: null,
      to: [{ name: "Paul Platzhalter", address: "paul@example.com" }],
      subject: "Re: Protokoll Teamrunde",
      body: "Danke für das Protokoll.",
      updated_at: new Date(NOW.getTime() - 50 * hour).toISOString(),
    }),
  ];
}

function words(text: string) {
  return text.match(/\S+\s*/g) ?? [];
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, json: body });
}

function problem(route: Route, status: number, extra: Record<string, unknown> = {}) {
  return route.fulfill({
    status,
    contentType: "application/problem+json",
    json: { type: "about:blank", title: `HTTP ${status}`, status, ...extra },
  });
}

export async function mockDrafts(
  page: Page,
  {
    suggestion = SUGGESTION,
    generateError,
    step = 25,
    pauseAfter,
    sendError,
    drafts = [],
  }: MockDrafts = {},
) {
  const store = new Map(drafts.map((item) => [item.id, item]));
  const requests: { route: string; body?: Record<string, unknown> }[] = [];
  let created = 0;

  // Events of one generation, computed here (with the store) and streamed by the page.
  await page.exposeFunction("__ollamailGenerate", (raw: string) => {
    const body = JSON.parse(raw) as Record<string, unknown>;
    requests.push({ route: "POST /api/drafts/generate", body });
    const current = store.get(String(body.draft_id));
    if (!current) return [];
    const start = { type: "start", draft_id: current.id };
    if (generateError) return [start, { type: "error", code: generateError }];
    const stored = { ...current, body: suggestion, model: "llama3.1:8b" };
    store.set(stored.id, stored);
    return [
      start,
      ...words(suggestion).map((text) => ({ type: "token", text })),
      { type: "done", draft: stored, ttft_ms: 640 },
    ];
  });
  await page.addInitScript(
    ({ step, pauseAfter }) => {
      const original = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const request = input instanceof Request ? input : new Request(input, init);
        if (request.method !== "POST" || new URL(request.url).pathname !== "/api/drafts/generate") {
          return original(input, init);
        }
        const generate = (
          window as unknown as { __ollamailGenerate: (body: string) => Promise<unknown[]> }
        ).__ollamailGenerate;
        const events = await generate(await request.clone().text());
        const encoder = new TextEncoder();
        const signal = request.signal;
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            let index = 0;
            let timer: ReturnType<typeof setTimeout> | undefined;
            const limit = pauseAfter ?? events.length;
            const next = () => {
              if (signal.aborted) return;
              if (index >= limit) {
                // Paused streams stay open (writing state, cancel).
                if (pauseAfter === undefined) controller.close();
                return;
              }
              const event = events[index] as { type: string };
              index += 1;
              controller.enqueue(
                encoder.encode(`event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`),
              );
              timer = setTimeout(next, step);
            };
            signal.addEventListener("abort", () => {
              clearTimeout(timer);
              controller.error(new DOMException("Aborted", "AbortError"));
            });
            next();
          },
        });
        return new Response(body, { headers: { "Content-Type": "text/event-stream" } });
      };
    },
    { step, pauseAfter },
  );

  await page.route(
    (url) => url.pathname.startsWith("/api/drafts"),
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname.replace(/^\/api/, "");
      const method = request.method();
      const body =
        method === "POST" || method === "PATCH"
          ? ((request.postDataJSON() as Record<string, unknown> | null) ?? undefined)
          : undefined;
      requests.push({ route: `${method} ${path}`, body });

      if (method === "GET" && path === "/drafts") {
        const message = url.searchParams.get("message_id");
        return json(
          route,
          [...store.values()]
            .filter((item) => item.status === "draft" && (!message || item.message_id === message))
            .sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
        );
      }
      if (method === "POST" && path === "/drafts") {
        created += 1;
        const replyAll = Boolean(body?.reply_all);
        const item = draft(100 + created, {
          message_id: String(body?.message_id),
          reply_all: replyAll,
          cc: replyAll ? [{ name: null, address: "team@example.org" }] : [],
        });
        store.set(item.id, item);
        return json(route, item, 201);
      }
      const match = /^\/drafts\/([^/]+)(\/\w+)?$/.exec(path);
      const current = match?.[1] ? store.get(match[1]) : undefined;
      if (!match || !current) return problem(route, 404);
      const action = match[2];
      if (method === "PATCH" && !action) {
        const changed = { ...current, ...body, updated_at: new Date().toISOString() };
        if (body && "reply_all" in body) {
          changed.cc = body.reply_all ? [{ name: null, address: "team@example.org" }] : [];
        }
        store.set(current.id, changed as StoredDraft);
        return json(route, changed);
      }
      if (method === "POST" && action === "/send") {
        if (sendError) return problem(route, sendError[0], { error_code: sendError[1] });
        const sent = { ...current, status: "sent" as const, sent_at: new Date().toISOString() };
        store.set(current.id, sent);
        return json(route, sent);
      }
      if (method === "POST" && action === "/discard") {
        const discarded = { ...current, status: "discarded" as const };
        store.set(current.id, discarded);
        return json(route, discarded);
      }
      if (method === "DELETE" && !action) {
        store.delete(current.id);
        return route.fulfill({ status: 204 });
      }
      return problem(route, 404);
    },
  );
  return { store, requests };
}

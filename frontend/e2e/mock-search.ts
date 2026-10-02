import type { Page, Route } from "@playwright/test";

import { mailboxIds, NOW } from "./mock-mail";

/**
 * In-browser mock of the search and "ask your inbox" API with synthetic data (invented
 * names, `example.*` addresses). Register after `mockApi` and `mockMail`.
 *
 * The answer stream (`POST /api/rag/ask`) is produced inside the page by a wrapped `fetch`,
 * so it really arrives piece by piece (Playwright can only fulfil whole responses).
 */
export type AnswerScenario = "answered" | "no_evidence" | "error" | "hang";

export interface MockSearch {
  answer?: AnswerScenario;
  /** Delay between stream events in ms. */
  step?: number;
  /** Stop after this many events and keep the stream open (screenshots while writing). */
  pauseAfter?: number;
  /** Stored conversations in the history. */
  history?: boolean;
  /** `POST /api/search` fails. */
  searchError?: boolean;
  /** `POST /api/search` never answers (skeleton). */
  searchHang?: boolean;
  /** The answer also cites the scanned attachment (source "attachment_ocr"). */
  ocrSource?: boolean;
}

const id = (prefix: string, index: number) =>
  `${prefix}-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;

export const searchMessageId = (index: number) => id("0193b000", index);
export const conversationId = (index: number) => id("0193e000", index);

const day = (days: number, hours = 9) =>
  new Date(NOW.getTime() - days * 86_400_000 + (hours - 9) * 3_600_000).toISOString();

interface SyntheticMail {
  subject: string;
  sender: [string, string];
  date: string;
  text: string;
  excerpt: string;
  attachment?: string;
  /** The attachment is a scan; its text was recognised (OCR). */
  ocr?: boolean;
}

export const searchMails: SyntheticMail[] = [
  {
    subject: "Rechnung 2026-1042",
    sender: ["Rechnungsstelle", "billing@example.net"],
    date: day(2),
    text: "Hallo Erika,\n\nanbei die Rechnung 2026-1042 für September. Die Rechnung ist zahlbar innerhalb von 14 Tagen, also bis zum 15. Oktober.\n\nBitte gib die Rechnungsnummer im Verwendungszweck an.\n\nViele Grüße\nRechnungsstelle",
    excerpt:
      "anbei die Rechnung 2026-1042 für September. Die Rechnung ist zahlbar innerhalb von 14 Tagen, also bis zum 15. Oktober.",
  },
  {
    subject: "Zahlungserinnerung",
    sender: ["Rechnungsstelle", "billing@example.net"],
    date: day(0, 8),
    text: "Hallo Erika,\n\nwir möchten freundlich an die offene Rechnung 2026-1042 erinnern. Bitte gib bei der Zahlung die Rechnungsnummer im Verwendungszweck an.\n\nViele Grüße\nRechnungsstelle",
    excerpt:
      "wir möchten freundlich an die offene Rechnung 2026-1042 erinnern. Bitte gib bei der Zahlung die Rechnungsnummer im Verwendungszweck an.",
  },
  {
    subject: "Re: Entwurf Angebot Nordlicht",
    sender: ["Lena Muster", "lena.muster@example.com"],
    date: day(6),
    text: "Hallo Erika,\n\ndanke für den Entwurf. Die Rechnung für die erste Projektphase stellen wir nach Abnahme, die zweite Rate zum Projektende.\n\nLena",
    excerpt:
      "Die Rechnung für die erste Projektphase stellen wir nach Abnahme, die zweite Rate zum Projektende.",
  },
  {
    subject: "Unterlagen Steuererklärung",
    sender: ["Jonas Beispiel", "jonas@example.org"],
    date: day(23),
    text: "Hi Erika,\n\nanbei die gesammelten Belege. Die Handwerker-Rechnung ist im PDF ganz hinten.\n\nJonas",
    excerpt:
      "… Seite 4: Rechnung Malerbetrieb Pinsel & Rolle, Betrag 1.240,00 EUR, bezahlt am 3. September …",
    attachment: "Belege-2026.pdf",
    ocr: true,
  },
];

const hits = searchMails.map((mail, index) => ({
  message_id: searchMessageId(index),
  mailbox_id: index === 3 ? mailboxIds.private : mailboxIds.work,
  thread_id: null,
  subject: mail.subject,
  sender: { name: mail.sender[0], address: mail.sender[1] },
  date: mail.date,
  source: mail.attachment ? (mail.ocr ? "attachment_ocr" : "attachment") : "body",
  attachment_id: mail.attachment ? id("0193d000", index) : null,
  attachment_filename: mail.attachment ?? null,
  excerpt: mail.excerpt,
  score: 0.033 - index * 0.002,
}));

function thread(index: number) {
  const mail = searchMails[index] as SyntheticMail;
  const message = {
    id: searchMessageId(index),
    mailbox_id: hits[index]?.mailbox_id ?? mailboxIds.work,
    thread_id: null,
    subject: mail.subject,
    sender: { name: mail.sender[0], address: mail.sender[1] },
    snippet: mail.excerpt,
    date: mail.date,
    unread: false,
    flagged: false,
    has_attachments: !!mail.attachment,
    to: [{ name: "Erika Mustermann", address: "erika@example.org" }],
    cc: [],
    reply_to: [],
    sent_at: mail.date,
    text: mail.text,
    body: { html: null, blocked_images: 0 },
    attachments: mail.attachment
      ? [
          {
            id: id("0193d000", index),
            filename: mail.attachment,
            content_type: "application/pdf",
            size: 412_388,
            is_inline: false,
          },
        ]
      : [],
  };
  return {
    thread_id: null,
    mailbox_id: message.mailbox_id,
    subject: mail.subject,
    messages: [message],
  };
}

const heading = (index: number) => {
  const mail = searchMails[index] as SyntheticMail;
  return `From: ${mail.sender[0]} <${mail.sender[1]}>\nDate: ${mail.date.slice(0, 10)}\nSubject: ${mail.subject}`;
};

export const QUESTION = "Bis wann muss die Rechnung 2026-1042 bezahlt werden?";
export const NO_EVIDENCE_QUESTION = "Wann findet das Sommerfest statt?";

const sources = [0, 1, 2].map((index) => ({
  number: index + 1,
  message_id: searchMessageId(index),
  mailbox_id: mailboxIds.work,
  attachment_id: null,
  source: "body",
  heading: heading(index),
  snippet: searchMails[index]?.excerpt ?? "",
}));

const ocrSource = {
  number: 4,
  message_id: searchMessageId(3),
  mailbox_id: mailboxIds.private,
  attachment_id: id("0193d000", 3),
  source: "attachment_ocr",
  heading: `${heading(3)}\nAttachment: Belege-2026.pdf`,
  snippet: searchMails[3]?.excerpt ?? "",
};

const ANSWER_OCR =
  " Die Handwerker-Rechnung über 1.240,00 EUR ist laut Beleg am 3. September bezahlt worden [4].";

const ANSWER =
  "Die Rechnung 2026-1042 für September ist innerhalb von 14 Tagen zu zahlen, also bis zum 15. Oktober [1]. Die Rechnungsstelle hat heute daran erinnert und bittet darum, die Rechnungsnummer im Verwendungszweck anzugeben [2].";

function words(text: string) {
  return text.match(/\S+\s*/g) ?? [];
}

function streamEvents(scenario: AnswerScenario, ocr = false) {
  const start = {
    type: "start",
    conversation_id: conversationId(100),
    question_id: conversationId(101),
    answer_id: conversationId(102),
  };
  const filters = { type: "filters", filters: { extracted: [] } };
  switch (scenario) {
    case "answered":
      return [
        start,
        filters,
        { type: "sources", sources: ocr ? [...sources, ocrSource] : sources },
        ...words(ocr ? ANSWER + ANSWER_OCR : ANSWER).map((text) => ({ type: "token", text })),
        { type: "done", status: "answered", citations: ocr ? [1, 2, 4] : [1, 2], ttft_ms: 840 },
      ];
    case "no_evidence":
      return [
        start,
        filters,
        { type: "sources", sources: [] },
        ...words("In deinen Mails habe ich dazu keine Angaben gefunden.").map((text) => ({
          type: "token",
          text,
        })),
        { type: "done", status: "no_evidence", citations: [], ttft_ms: 610 },
      ];
    case "error":
      return [start, filters, { type: "error", code: "llm_unavailable" }];
    case "hang":
      return [start, filters, { type: "sources", sources }];
  }
}

const history = [
  ["Bis wann muss die Rechnung 2026-1042 bezahlt werden?", 0],
  ["Wer vertritt Lena im Oktober?", 1],
  ["Was wurde in der Teamrunde zur Quartalsplanung beschlossen?", 3],
  ["Wann ist das Wartungsfenster für das Ticketsystem?", 9],
] as const;

function conversations() {
  return history.map(([title, days], index) => ({
    id: conversationId(index + 1),
    title,
    created_at: day(days, 10),
    updated_at: day(days, 10),
  }));
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, json: body });
}

export async function mockSearch(
  page: Page,
  {
    answer = "answered",
    step = 30,
    pauseAfter,
    history: withHistory = true,
    searchError = false,
    searchHang = false,
    ocrSource: withOcrSource = false,
  }: MockSearch = {},
) {
  let stored = withHistory ? conversations() : [];
  let answered = false;
  const asks: unknown[] = [];
  const searches: unknown[] = [];

  await page.exposeFunction("__ollamailAsk", (body: string) => {
    asks.push(JSON.parse(body));
    if (answer === "answered") answered = true;
  });
  await page.addInitScript(
    ({ events, step, pauseAfter }) => {
      const original = window.fetch.bind(window);
      window.fetch = async (input, init) => {
        const request = input instanceof Request ? input : new Request(input, init);
        if (request.method !== "POST" || new URL(request.url).pathname !== "/api/rag/ask") {
          return original(input, init);
        }
        const report = (window as unknown as { __ollamailAsk: (body: string) => Promise<void> })
          .__ollamailAsk;
        await report(await request.clone().text());
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
    {
      events: streamEvents(answer, withOcrSource),
      step,
      pauseAfter: answer === "hang" ? streamEvents("hang").length : pauseAfter,
    },
  );

  await page.route(
    (url) =>
      url.pathname === "/api/search" ||
      url.pathname.startsWith("/api/rag/") ||
      url.pathname === "/api/triage/categories" ||
      /^\/api\/messages\/0193b000-[^/]+\/thread$/.test(url.pathname),
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname.replace(/^\/api/, "");
      const method = request.method();

      if (path === "/triage/categories") {
        return json(
          route,
          [
            ["Wichtig", "important"],
            ["Rechnungen", "invoices"],
            ["Newsletter", "newsletter"],
          ].map(([name, key], index) => ({
            id: id("0193c000", index),
            name,
            description: "",
            builtin_key: key,
            scope: "organization",
            hidden: false,
            position: index,
          })),
        );
      }
      if (method === "POST" && path === "/search") {
        if (searchHang) return;
        searches.push(request.postDataJSON());
        if (searchError) {
          return route.fulfill({
            status: 503,
            contentType: "application/problem+json",
            json: { type: "about:blank", title: "Service Unavailable", status: 503 },
          });
        }
        const { query } = request.postDataJSON() as { query: string };
        const found = /rechnung|zahl|frage|bezahlt/i.test(query) ? hits : [];
        return json(route, { hits: found });
      }
      const threadMatch = /^\/messages\/(0193b000-[^/]+)\/thread$/.exec(path);
      if (threadMatch) {
        const index = hits.findIndex((hit) => hit.message_id === threadMatch[1]);
        return json(route, thread(Math.max(index, 0)));
      }
      if (method === "GET" && path === "/rag/conversations") {
        const list = answered
          ? [
              {
                id: conversationId(100),
                title: QUESTION,
                created_at: NOW.toISOString(),
                updated_at: NOW.toISOString(),
              },
              ...stored,
            ]
          : stored;
        return json(route, list);
      }
      if (method === "DELETE" && path === "/rag/conversations") {
        stored = [];
        answered = false;
        return route.fulfill({ status: 204 });
      }
      const conversation = /^\/rag\/conversations\/([^/]+)$/.exec(path);
      if (conversation?.[1]) {
        const conversationKey = conversation[1];
        if (method === "DELETE") {
          stored = stored.filter((item) => item.id !== conversationKey);
          return route.fulfill({ status: 204 });
        }
        const summary = stored.find((item) => item.id === conversationKey);
        if ((answered && conversationKey === conversationId(100)) || summary) {
          const title = summary?.title ?? QUESTION;
          return json(route, {
            id: conversationKey,
            title,
            created_at: summary?.created_at ?? NOW.toISOString(),
            updated_at: summary?.updated_at ?? NOW.toISOString(),
            messages: [
              {
                id: id("0193f000", 1),
                role: "user",
                content: title,
                created_at: NOW.toISOString(),
                filters: { extracted: [] },
                status: null,
                citations: [],
              },
              {
                id:
                  conversationKey === conversationId(100) ? conversationId(102) : id("0193f000", 2),
                role: "assistant",
                content: withOcrSource ? ANSWER + ANSWER_OCR : ANSWER,
                created_at: NOW.toISOString(),
                filters: null,
                status: "answered",
                citations: withOcrSource
                  ? [...sources.slice(0, 2), ocrSource]
                  : sources.slice(0, 2),
              },
            ],
          });
        }
        return route.fulfill({
          status: 404,
          contentType: "application/problem+json",
          json: { type: "about:blank", title: "Not Found", status: 404 },
        });
      }
      return route.fallback();
    },
  );
  return { asks, searches };
}

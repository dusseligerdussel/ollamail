import type { Page, Route } from "@playwright/test";

import { messageId, NOW } from "./mock-mail";

/**
 * In-browser mock of the todo API with synthetic tasks (invented content). Dates are relative
 * to `NOW` of `mock-mail.ts`; install the clock (`page.clock.setFixedTime(NOW)`) so "today"
 * matches. Register after `mockApi`.
 */
export interface MockTodos {
  /** `false`: no tasks at all (empty state). */
  todos?: boolean;
}

export function todoId(index: number) {
  return `0192e000-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;
}

function day(offset: number) {
  const date = new Date(NOW);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

type MockTodo = Record<string, unknown> & { id: string; status: string; due_date: string | null };

function todo(index: number, fields: Record<string, unknown>): MockTodo {
  return {
    id: todoId(index),
    title: `Task ${index}`,
    description: null,
    due_date: null,
    priority: "normal",
    status: "open",
    is_manual: false,
    is_edited: false,
    confidence: 0.86,
    done_suggested: false,
    mailbox_id: null,
    message_id: null,
    thread_id: null,
    external_refs: {},
    created_at: "2026-09-28T08:00:00Z",
    updated_at: "2026-09-28T08:00:00Z",
    completed_at: null,
    ...fields,
  };
}

/** Message 6 of the mail mock is the reply "Re: Entwurf Angebot". */
export const offerMessage = messageId(6);

export function sampleTodos() {
  return [
    todo(1, {
      title: "Anmerkungen zum Angebot einarbeiten",
      due_date: day(-2),
      message_id: offerMessage,
      description: "Abschnitt 3: Preise und Laufzeit",
    }),
    todo(2, { title: "Rechnung 2026-1042 bezahlen", due_date: day(-1), message_id: messageId(3) }),
    todo(3, {
      title: "Termin für Quartalsplanung bestätigen",
      due_date: day(0),
      message_id: messageId(0),
    }),
    todo(4, { title: "Raum für Teamrunde buchen", due_date: day(0), is_manual: true }),
    todo(5, {
      title: "Offene Punkte aus dem Protokoll klären",
      due_date: day(1),
      message_id: messageId(7),
    }),
    todo(6, {
      title: "Urlaubsübergabe an Lena vorbereiten",
      due_date: day(6),
      message_id: messageId(2),
    }),
    todo(7, {
      title: "Zugangsdaten Testumgebung prüfen",
      due_date: day(19),
      message_id: messageId(5),
    }),
    todo(8, { title: "Fahrradlicht kaufen", is_manual: true }),
    todo(9, { title: "Ablage aufräumen", is_manual: true, description: "Ordner Verträge zuerst" }),
    todo(10, {
      title: "Wartungsfenster im Kalender eintragen",
      status: "done",
      due_date: day(-1),
      completed_at: new Date(NOW.getTime() - 3_600_000).toISOString(),
      message_id: messageId(1),
    }),
    todo(11, {
      title: "Folien für Montag schicken",
      status: "done",
      completed_at: new Date(NOW.getTime() - 26 * 3_600_000).toISOString(),
    }),
  ];
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, json: body });
}

export async function mockTodos(page: Page, { todos = true }: MockTodos = {}) {
  const store = new Map((todos ? sampleTodos() : []).map((item) => [item.id, item]));
  let created = 100;

  await page.route(
    (url) => url.pathname.startsWith("/api/todos"),
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const method = request.method();
      if (method === "GET" && url.pathname === "/api/todos") {
        const statuses = url.searchParams.getAll("status");
        const message = url.searchParams.get("message_id");
        const items = [...store.values()]
          .filter((item) => statuses.length === 0 || statuses.includes(item.status))
          .filter((item) => !message || item.message_id === message)
          .sort((a, b) => (a.due_date ?? "9999").localeCompare(b.due_date ?? "9999"));
        return json(route, items);
      }
      if (method === "POST" && url.pathname === "/api/todos") {
        const body = request.postDataJSON() as Record<string, unknown>;
        created += 1;
        const item = todo(created, { ...body, is_manual: true });
        store.set(item.id, item);
        return json(route, item, 201);
      }
      const id = url.pathname.split("/").at(-1) ?? "";
      const current = store.get(id);
      if (method === "PATCH" && current) {
        const body = request.postDataJSON() as Record<string, unknown>;
        const updated = {
          ...current,
          ...body,
          ...(body.status === "done" && { completed_at: new Date(NOW).toISOString() }),
        };
        store.set(id, updated);
        return json(route, updated);
      }
      return route.fulfill({
        status: 404,
        contentType: "application/problem+json",
        json: { type: "about:blank", title: "Not Found", status: 404 },
      });
    },
  );
  return { store };
}

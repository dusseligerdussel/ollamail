import type { Page, Route } from "@playwright/test";

import { mailboxIds, messageId } from "./mock-mail";

/**
 * In-browser mock of the digest API with synthetic data (invented content). Register after
 * `mockApi` and `mockMail`; later routes take precedence.
 */
export interface MockDigest {
  /** Number of digests (newest first); 0 shows the empty state. */
  digests?: number;
  /** Status of the newest digest. */
  status?: "pending" | "summarizing" | "synthesizing" | "ready" | "failed";
  /** Whether the podcast feed is enabled. */
  feedActive?: boolean;
  /** Served as audio of every digest (e.g. a short generated Opus file). */
  audio?: Buffer;
  /** Requests never answer (skeletons stay visible). */
  hang?: boolean;
}

export const FEED_URL = "https://mail.example.org/api/feeds/TESTTOKEN-not-a-real-secret.xml";

export function digestId(index: number) {
  return `0192d000-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;
}

const MONTH = "Oktober";
const WEEKDAYS = ["Freitag", "Donnerstag", "Mittwoch", "Dienstag", "Montag", "Sonntag", "Samstag"];

const SCRIPT = (day: number, weekday: string) => `# Digest vom ${weekday}, ${day}. ${MONTH} 2026

Dein Überblick am ${weekday}, dem ${day}. ${MONTH} 2026. Seit dem letzten Digest sind 12 neue Mails eingegangen, drei davon sind wichtig.

Die Rechnungsstelle erinnert an die Rechnung 2026-1042, sie ist bis zum 16. Oktober zu bezahlen [1]. Jonas Beispiel möchte die Quartalsplanung am Donnerstag kurz abstimmen und bittet um einen Terminvorschlag [2]. Lena Muster hat zwei Anmerkungen zum Angebotsentwurf, vor allem zu Abschnitt 3 [3].

Das Projektteam Nordlicht hat das Protokoll der Teamrunde verschickt; offene Punkte sollen bis Freitag erledigt sein [4, 5]. Am Samstag ist das Ticketsystem zwischen 6 und 8 Uhr wegen Wartung nicht erreichbar [6].

Außerdem gibt es 4 weitere Mails, unter anderem von Mira Testfrau und Paul Platzhalter. Dazu kommen 2 Newsletter, unter anderem vom Fahrradladen.

Neue Aufgaben: Rechnung 2026-1042 bezahlen und Termin für die Quartalsplanung vorschlagen.

Heute fällig: Anmerkungen zum Angebot einarbeiten.

Das war dein Digest.
`;

function digest(index: number, status: MockDigest["status"]) {
  const day = 2 - index > 0 ? 2 - index : 30 + (2 - index);
  const month = 2 - index > 0 ? 10 : 9;
  const weekday = WEEKDAYS[index % WEEKDAYS.length] ?? "Freitag";
  const iso = `2026-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  const ready = status === "ready";
  return {
    id: digestId(index),
    trigger: index === 3 ? "manual" : "scheduled",
    status,
    error_code: status === "failed" ? "tts_voice_not_available" : null,
    title: `Digest vom ${weekday}, ${day}. ${month === 10 ? MONTH : "September"} 2026`,
    language: "de",
    length: "normal",
    period_start: `${iso}T05:00:00Z`,
    period_end: `${iso}T05:00:00Z`,
    scheduled_for: `${iso}T05:00:00Z`,
    message_count: 12 - (index % 5),
    todo_count: index % 3,
    duration_seconds: ready ? 214 - index * 9 : null,
    audio_formats: ready ? ["opus", "mp3"] : [],
    created_at: `${iso}T05:00:00Z`,
    generated_at: ready || status === "failed" ? `${iso}T05:03:00Z` : null,
    script: ready || status === "synthesizing" ? SCRIPT(day, weekday) : null,
    references: [1, 2, 3, 4, 5, 6].map((ref) => ({
      ref,
      message_id: messageId(ref),
      mailbox_id: mailboxIds.work,
    })),
    voice: "de_DE-thorsten-medium",
    model: "llama3.2:3b",
  };
}

export async function mockDigest(
  page: Page,
  { digests = 8, status = "ready", feedActive = false, audio, hang = false }: MockDigest = {},
) {
  const items = Array.from({ length: digests }, (_, index) =>
    digest(index, index === 0 ? status : "ready"),
  );
  const settings = {
    enabled: true,
    delivery_time: "07:00:00",
    timezone: null,
    effective_timezone: "Europe/Berlin",
    weekdays: [0, 1, 2, 3, 4],
    language: null,
    effective_language: "de",
    voice: null,
    length: "normal",
    mailbox_ids: null,
    next_run_at: "2026-10-05T05:00:00Z",
    feed: feedActive
      ? { active: true, created_at: "2026-09-14T18:20:00Z" }
      : { active: false, created_at: null },
  };
  const voices = [
    { id: "de_DE-kerstin-low", language: "de", default: false, installed: true },
    { id: "de_DE-thorsten-medium", language: "de", default: true, installed: true },
    { id: "en_US-ljspeech-medium", language: "en", default: true, installed: false },
  ];

  const fulfill = (route: Route, json: unknown, status = 200) =>
    hang ? undefined : route.fulfill({ status, json });

  await page.route(
    (url) => url.pathname.startsWith("/api/digests"),
    async (route) => {
      const request = route.request();
      const path = new URL(request.url()).pathname;
      const key = `${request.method()} ${path}`;
      if (key === "GET /api/digests") return fulfill(route, items);
      if (key === "POST /api/digests") return fulfill(route, digest(99, "pending"), 202);
      if (key === "GET /api/digests/settings") return fulfill(route, settings);
      if (key === "PATCH /api/digests/settings") {
        Object.assign(settings, request.postDataJSON());
        return fulfill(route, settings);
      }
      if (key === "GET /api/digests/voices") return fulfill(route, voices);
      if (key === "POST /api/digests/feed") {
        settings.feed = { active: true, created_at: "2026-10-02T09:30:00Z" };
        return fulfill(route, { feed_url: FEED_URL, created_at: settings.feed.created_at }, 201);
      }
      if (key === "DELETE /api/digests/feed") {
        settings.feed = { active: false, created_at: null };
        return route.fulfill({ status: 204 });
      }
      const audioMatch = /^GET \/api\/digests\/([^/]+)\/audio\.(opus|mp3)$/.exec(key);
      if (audioMatch) {
        if (!audio) return route.fulfill({ status: 404 });
        return route.fulfill({ body: audio, contentType: "audio/ogg" });
      }
      const detail = /^GET \/api\/digests\/([^/]+)$/.exec(key);
      const found = detail && items.find((item) => item.id === detail[1]);
      if (found) return fulfill(route, found);
      return route.fulfill({
        status: 404,
        contentType: "application/problem+json",
        json: { type: "about:blank", title: "HTTP 404", status: 404 },
      });
    },
  );
}

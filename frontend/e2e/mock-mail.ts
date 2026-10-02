import type { Page, Route } from "@playwright/test";

/**
 * In-browser mock of the mailbox and mail API with synthetic data (invented names,
 * `example.org` addresses). Register after `mockApi`; later routes take precedence.
 */
export interface MockMail {
  /** Number of inbox messages (the list is generated, newest first). */
  messages?: number;
  /** `false`: no mailbox connected yet. */
  mailboxes?: boolean;
  /** OAuth providers reported as configured. */
  oauth?: ("gmail" | "graph")[];
  /** Delay of list and thread responses in ms (loading states). */
  delay?: number;
  /** Requests never answer (skeletons stay visible). */
  hang?: boolean;
}

export const NOW = new Date("2026-10-02T09:30:00Z");

export const mailboxIds = {
  work: "0192a000-0000-7000-8000-000000000001",
  private: "0192a000-0000-7000-8000-000000000002",
};

const folderIds = {
  inbox: "0192a000-0000-7000-8000-0000000000f1",
  archive: "0192a000-0000-7000-8000-0000000000f2",
  sent: "0192a000-0000-7000-8000-0000000000f3",
  trash: "0192a000-0000-7000-8000-0000000000f4",
};

const people = [
  ["Jonas Beispiel", "jonas@example.org"],
  ["Lena Muster", "lena.muster@example.com"],
  ["Projektteam Nordlicht", "team@nordlicht.example"],
  ["Rechnungsstelle", "billing@example.net"],
  ["Mira Testfrau", "mira@example.org"],
  ["Paul Platzhalter", "paul@example.com"],
  ["Newsletter Fahrradladen", "news@fahrrad.example"],
  ["IT-Service", "it@example.org"],
] as const;

const subjects = [
  ["Abstimmung Quartalsplanung", "Können wir uns am Donnerstag kurz zur Planung für Q4 abstimmen?"],
  ["Rechnung 2026-1042", "Anbei die Rechnung für September. Zahlbar innerhalb von 14 Tagen."],
  ["Re: Entwurf Angebot", "Danke für den Entwurf, ich habe zwei Anmerkungen zu Abschnitt 3."],
  [
    "Wartungsfenster am Samstag",
    "Am Samstag zwischen 6 und 8 Uhr ist das Ticketsystem nicht erreichbar.",
  ],
  ["Herbstaktion: 20 % auf Zubehör", "Nur bis Sonntag: Lichter, Schlösser und Taschen im Angebot."],
  ["Protokoll Teamrunde", "Hier das Protokoll von heute. Offene Punkte bitte bis Freitag."],
  ["Urlaubsvertretung", "Ich bin vom 10. bis 21. Oktober nicht im Büro, Vertretung hat Lena."],
  [
    "Fwd: Zugangsdaten Testumgebung",
    "Die Zugangsdaten für die Testumgebung findest du im Passwortmanager.",
  ],
] as const;

export function messageId(index: number) {
  return `0192b000-0000-7000-8000-${index.toString(16).padStart(12, "0")}`;
}

function summary(index: number) {
  const [name, address] = people[index % people.length] ?? people[0];
  const [subject, snippet] = subjects[(index * 3) % subjects.length] ?? subjects[0];
  const date = new Date(NOW.getTime() - index * 47 * 60_000 - (index > 5 ? 86_400_000 : 0));
  return {
    id: messageId(index),
    mailbox_id: index % 3 === 2 ? mailboxIds.private : mailboxIds.work,
    thread_id: `0192c000-0000-7000-8000-${index.toString(16).padStart(12, "0")}`,
    subject,
    sender: { name, address },
    snippet,
    date: date.toISOString(),
    unread: index < 4 || index % 7 === 3,
    flagged: false,
    has_attachments: index % 4 === 1,
  };
}

const newsletterHtml = `
<table width="100%" cellpadding="0" cellspacing="0" style="max-width:560px">
  <tr><td style="padding:8px 0"><img src="" alt=""></td></tr>
  <tr><td style="font-size:20px;font-weight:600;padding:8px 0">Herbstaktion im Fahrradladen</td></tr>
  <tr><td style="padding:4px 0;color:#3f3f46">Nur bis Sonntag: 20 % auf Lichter, Schlösser und Taschen.
    Die Aktion gilt im Laden und im Onlineshop.</td></tr>
  <tr><td style="padding:12px 0"><a href="https://fahrrad.example/aktion">Zur Aktion</a></td></tr>
  <tr><td style="padding:12px 0;font-size:12px;color:#71717a">Du erhältst diese Nachricht, weil du den
    Newsletter abonniert hast. <a href="https://fahrrad.example/abmelden">Abmelden</a></td></tr>
</table>`;

function detail(index: number, overrides: Record<string, unknown> = {}) {
  const base = summary(index);
  const html = index % 2 === 0;
  return {
    ...base,
    to: [{ name: "Erika Mustermann", address: "erika@example.org" }],
    cc: index % 3 === 0 ? [{ name: null, address: "team@example.org" }] : [],
    reply_to: [],
    sent_at: base.date,
    text: `${base.snippet}\n\nViele Grüße\n${base.sender.name}`,
    body: html
      ? {
          html: `<p>Hallo Erika,</p><p>${base.snippet}</p><p>Viele Grüße<br>${base.sender.name}</p>`,
          blocked_images: 0,
        }
      : { html: null, blocked_images: 0 },
    attachments: base.has_attachments
      ? [
          {
            id: "0192d000-0000-7000-8000-000000000001",
            filename: "Rechnung-2026-1042.pdf",
            content_type: "application/pdf",
            size: 48_213,
            is_inline: false,
          },
        ]
      : [],
    ...overrides,
  };
}

/** The newsletter (index 4) shows blocked images; index 2 is a reply in a thread. */
function thread(index: number) {
  const opened = summary(index);
  if (opened.subject.startsWith("Herbstaktion")) {
    return {
      thread_id: opened.thread_id,
      mailbox_id: opened.mailbox_id,
      subject: opened.subject,
      messages: [
        detail(index, {
          body: { html: newsletterHtml, blocked_images: 3 },
          text: "Herbstaktion im Fahrradladen",
        }),
      ],
    };
  }
  if (opened.subject.startsWith("Re:")) {
    const earlier = {
      ...detail(index, {
        id: messageId(90_000 + index),
        subject: "Entwurf Angebot",
        sender: { name: "Erika Mustermann", address: "erika@example.org" },
        to: [opened.sender],
        snippet: "Anbei der Entwurf für das Angebot, Rückmeldung gerne bis Mittwoch.",
        text: "Hallo,\n\nanbei der Entwurf für das Angebot, Rückmeldung gerne bis Mittwoch.\n\nErika",
        body: { html: null, blocked_images: 0 },
        unread: false,
        date: new Date(new Date(opened.date).getTime() - 26 * 3_600_000).toISOString(),
      }),
    };
    return {
      thread_id: opened.thread_id,
      mailbox_id: opened.mailbox_id,
      subject: "Entwurf Angebot",
      messages: [earlier, detail(index)],
    };
  }
  return {
    thread_id: opened.thread_id,
    mailbox_id: opened.mailbox_id,
    subject: opened.subject,
    messages: [detail(index)],
  };
}

function status(overrides: Record<string, unknown> = {}) {
  return {
    phase: "idle",
    last_synced_at: new Date(NOW.getTime() - 3 * 60_000).toISOString(),
    last_error: null,
    sync_queued: false,
    folders_total: 3,
    folders_imported: 3,
    folders_failed: 0,
    message_count: 1284,
    ...overrides,
  };
}

function mailbox(id: string, name: string, address: string, mailboxStatus: object) {
  return {
    id,
    type: "imap",
    display_name: name,
    address,
    is_shared: false,
    // Own mailboxes grant everything (shared mailboxes only what was assigned).
    permissions: ["read", "sync", "manage", "act"],
    provider_settings: { host: "imap.example.org", port: 993, security: "tls" },
    has_credentials: true,
    sync_enabled: true,
    sync_settings: {},
    status: mailboxStatus,
    created_at: "2026-09-01T08:00:00Z",
    updated_at: "2026-09-01T08:00:00Z",
  };
}

export const mailboxes = [
  mailbox(mailboxIds.work, "Arbeit", "erika@example.org", status()),
  mailbox(
    mailboxIds.private,
    "Privat",
    "erika.mustermann@posteo.example",
    status({
      phase: "importing",
      last_synced_at: null,
      folders_total: 4,
      folders_imported: 1,
      message_count: 312,
    }),
  ),
  {
    ...mailbox(
      "0192a000-0000-7000-8000-000000000003",
      "Verein",
      "kasse@verein.example",
      status({ phase: "error", last_error: "authentication_failed", message_count: 57 }),
    ),
  },
];

const folders = [
  ["inbox", "INBOX", "inbox", 812],
  ["archive", "Archiv", "archive", 402],
  ["sent", "Gesendet", "sent", 70],
  ["trash", "Papierkorb", "trash", 0],
] as const;

function folderList() {
  return folders.map(([key, name, role, count]) => ({
    id: folderIds[key],
    remote_id: name,
    name,
    kind: "folder",
    role,
    sync_enabled: role !== "trash",
    excluded_by_role: role === "trash",
    synced: role !== "trash",
    last_synced_at: NOW.toISOString(),
    last_error: null,
    import_pending: role === "archive",
    message_count: count,
  }));
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, json: body });
}

export async function mockMail(
  page: Page,
  {
    messages = 240,
    mailboxes: hasMailboxes = true,
    oauth = [],
    delay = 0,
    hang = false,
  }: MockMail = {},
) {
  const read = new Set<string>();
  const unread = new Set<string>();
  const wait = () => (delay ? new Promise((resolve) => setTimeout(resolve, delay)) : undefined);

  await page.route(
    (url) => url.pathname.startsWith("/api/mailboxes") || url.pathname.startsWith("/api/messages"),
    async (route) => {
      if (hang) return;
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname.replace(/^\/api/, "");
      const method = request.method();

      if (method === "GET" && path === "/mailboxes")
        return json(route, hasMailboxes ? mailboxes : []);
      if (method === "GET" && path === "/mailboxes/providers") {
        return json(route, [
          { type: "imap", connect: "credentials", oauth_start_path: null },
          ...oauth.map((type) => ({
            type,
            connect: "oauth",
            oauth_start_path: type === "gmail" ? "/mail/gmail/oauth/start" : "/mail/graph/connect",
          })),
        ]);
      }
      if (method === "POST" && path === "/mailboxes/autodiscover") {
        return json(route, {
          suggestions: [
            {
              type: "imap",
              provider_settings: { host: "imap.example.org", port: 993, security: "tls" },
              source: "guess",
              hints: ["guessed"],
            },
          ],
        });
      }
      if (method === "POST" && path === "/mailboxes/test") {
        const body = request.postDataJSON() as { credentials?: { password?: string } };
        return json(
          route,
          body.credentials?.password === "wrong"
            ? { ok: false, error: "authentication_failed", folders: [] }
            : { ok: true, error: null, folders: folderList() },
        );
      }
      if (method === "POST" && path === "/mailboxes") return json(route, mailboxes[0], 201);
      if (/^\/mailboxes\/[^/]+\/folders$/.test(path)) return json(route, folderList());

      if (method === "GET" && path === "/messages") {
        await wait();
        const limit = Number(url.searchParams.get("limit") ?? 100);
        const offset = Number(url.searchParams.get("cursor") ?? 0);
        const onlyUnread = url.searchParams.get("unread") === "true";
        const mailboxFilter = url.searchParams.get("mailbox_id");
        let all = Array.from({ length: messages }, (_, index) => summary(index)).map((item) => ({
          ...item,
          unread: unread.has(item.id) || (item.unread && !read.has(item.id)),
        }));
        if (mailboxFilter) all = all.filter((item) => item.mailbox_id === mailboxFilter);
        if (onlyUnread) all = all.filter((item) => item.unread);
        const items = all.slice(offset, offset + limit);
        return json(route, {
          items,
          total: all.length,
          next_cursor: offset + limit < all.length ? String(offset + limit) : null,
        });
      }
      const threadMatch = path.match(/^\/messages\/([^/]+)\/thread$/);
      if (threadMatch) {
        await wait();
        const index = Number.parseInt(threadMatch[1]?.split("-").at(-1) ?? "0", 16);
        const data = thread(index);
        for (const message of data.messages) {
          if (read.has(message.id)) message.unread = false;
          if (unread.has(message.id)) message.unread = true;
        }
        return json(route, data);
      }
      const bodyMatch = path.match(/^\/messages\/([^/]+)\/body$/);
      if (bodyMatch) {
        return json(route, {
          html: newsletterHtml.replace(
            '<img src="" alt="">',
            '<span style="display:inline-block;width:120px;height:32px;background:#e4e4e7"></span>',
          ),
          blocked_images: 0,
        });
      }
      const patchMatch = path.match(/^\/messages\/([^/]+)$/);
      if (patchMatch && method === "PATCH") {
        const id = patchMatch[1] ?? "";
        const seen = (request.postDataJSON() as { seen: boolean }).seen;
        if (seen) {
          read.add(id);
          unread.delete(id);
        } else {
          unread.add(id);
          read.delete(id);
        }
        const index = Number.parseInt(id.split("-").at(-1) ?? "0", 16);
        return json(route, { ...summary(index), unread: !seen });
      }
      return route.fulfill({
        status: 404,
        contentType: "application/problem+json",
        json: { type: "about:blank", title: "Not Found", status: 404 },
      });
    },
  );
}

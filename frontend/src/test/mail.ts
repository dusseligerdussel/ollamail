import type { Mailbox, MessageDetail, MessageSummary, Thread } from "@/api/mail";

/** Synthetic mail data for component tests (invented names, example addresses). */
export const MAILBOX_ID = "0199b000-0000-7000-8000-000000000001";

export function testMailbox(overrides: Partial<Mailbox> = {}): Mailbox {
  return {
    id: MAILBOX_ID,
    type: "imap",
    display_name: "Arbeit",
    address: "erika@example.org",
    is_shared: false,
    provider_settings: { host: "imap.example.org", port: 993, security: "tls" },
    has_credentials: true,
    sync_enabled: true,
    sync_settings: {
      initial_sync_days: null,
      excluded_roles: ["trash", "junk"],
      excluded_folders: [],
      poll_interval_seconds: null,
    },
    status: {
      phase: "idle",
      last_synced_at: "2026-10-02T09:00:00Z",
      last_error: null,
      sync_queued: false,
      folders_total: 2,
      folders_imported: 2,
      folders_failed: 0,
      message_count: 3,
    },
    created_at: "2026-09-01T08:00:00Z",
    updated_at: "2026-09-01T08:00:00Z",
    ...overrides,
  } as Mailbox;
}

export function messageId(index: number) {
  return `0199c000-0000-7000-8000-${String(index).padStart(12, "0")}`;
}

export function testMessage(
  index: number,
  overrides: Partial<MessageSummary> = {},
): MessageSummary {
  return {
    id: messageId(index),
    mailbox_id: MAILBOX_ID,
    thread_id: null,
    subject: `Subject ${index}`,
    sender: { name: `Sender ${index}`, address: `sender${index}@example.org` },
    snippet: `Snippet ${index}`,
    date: "2026-10-02T08:00:00Z",
    unread: false,
    flagged: false,
    has_attachments: false,
    ...overrides,
  };
}

export function testDetail(index: number, overrides: Partial<MessageDetail> = {}): MessageDetail {
  return {
    ...testMessage(index),
    to: [{ name: "Erika Mustermann", address: "erika@example.org" }],
    cc: [],
    reply_to: [],
    sent_at: "2026-10-02T08:00:00Z",
    text: `Body ${index}`,
    body: { html: null, blocked_images: 0 },
    attachments: [],
    ...overrides,
  };
}

export function testThread(index: number, overrides: Partial<MessageDetail> = {}): Thread {
  const message = testDetail(index, overrides);
  return {
    thread_id: null,
    mailbox_id: MAILBOX_ID,
    subject: message.subject,
    messages: [message],
  };
}

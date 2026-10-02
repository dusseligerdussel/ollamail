import type { Digest, DigestSettings, DigestVoice } from "@/api/digest";
import { MAILBOX_ID, messageId } from "@/test/mail";

/** Synthetic digest data for component tests (invented content, no real mails). */
export function digestId(index: number) {
  return `0199d000-0000-7000-8000-${String(index).padStart(12, "0")}`;
}

export function testDigest(index: number, overrides: Partial<Digest> = {}): Digest {
  return {
    id: digestId(index),
    trigger: "scheduled",
    status: "ready",
    error_code: null,
    title: `Digest ${index}`,
    language: "en",
    length: "normal",
    period_start: "2026-10-01T05:00:00Z",
    period_end: "2026-10-02T05:00:00Z",
    scheduled_for: "2026-10-02T05:00:00Z",
    message_count: 3,
    todo_count: 1,
    duration_seconds: 125,
    audio_formats: ["opus", "mp3"],
    created_at: "2026-10-02T05:00:00Z",
    generated_at: "2026-10-02T05:02:00Z",
    script: `# Digest ${index}\n\nGood morning. You have 3 new mails.\n\nThe invoice is due on Monday [1]. The team meeting moved to 10 am [2].\n\nThat's your digest.\n`,
    references: [
      { ref: 1, message_id: messageId(1), mailbox_id: MAILBOX_ID },
      { ref: 2, message_id: messageId(2), mailbox_id: MAILBOX_ID },
    ],
    voice: "en_US-ljspeech-medium",
    model: "llama3.2",
    ...overrides,
  };
}

export function testDigestSettings(overrides: Partial<DigestSettings> = {}): DigestSettings {
  return {
    enabled: false,
    delivery_time: "07:00:00",
    timezone: null,
    effective_timezone: "UTC",
    weekdays: [0, 1, 2, 3, 4, 5, 6],
    language: null,
    effective_language: "en",
    voice: null,
    length: "normal",
    mailbox_ids: null,
    next_run_at: null,
    feed: { active: false, created_at: null },
    ...overrides,
  };
}

export const testVoices: DigestVoice[] = [
  { id: "de_DE-thorsten-medium", language: "de", default: true, installed: true },
  { id: "en_US-amy-low", language: "en", default: false, installed: true },
  { id: "en_US-ljspeech-medium", language: "en", default: true, installed: true },
];

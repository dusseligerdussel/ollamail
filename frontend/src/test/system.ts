import type { ModelStatus, SystemOverview } from "@/api/system";

/** Synthetic system status for admin tests (invented names, no mail content). */
export function testModels(
  overrides: Partial<Record<ModelStatus["task"], Partial<ModelStatus>>> = {},
) {
  const tasks: ModelStatus["task"][] = [
    "triage",
    "todos",
    "digest",
    "rag_chat",
    "reply_draft",
    "embeddings",
  ];
  return tasks.map(
    (task): ModelStatus => ({
      task,
      endpoint: "default",
      provider: "ollama",
      model: task === "embeddings" ? "bge-m3" : "qwen2.5:3b",
      state: "installed",
      can_pull: false,
      pull: null,
      ...overrides[task],
    }),
  );
}

export function testOverview(overrides: Partial<SystemOverview> = {}): SystemOverview {
  return {
    mailbox_count: 2,
    digest_enabled: false,
    digest_scheduler_enabled: true,
    public_url_set: false,
    mailboxes: [
      {
        id: "0199b000-0000-7000-8000-0000000000a1",
        type: "imap",
        display_name: null,
        is_shared: false,
        owner_name: "Erika Muster",
        sync_phase: "idle",
        sync_error: null,
        processing_enabled: true,
        pending: 4,
        running: 1,
        failed: 3,
      },
      {
        id: "0199b000-0000-7000-8000-0000000000a2",
        type: "graph",
        display_name: "Support",
        is_shared: true,
        owner_name: null,
        sync_phase: "error",
        sync_error: "authentication_failed",
        processing_enabled: true,
        pending: 0,
        running: 0,
        failed: 0,
      },
    ],
    ...overrides,
  };
}

import { queryOptions } from "@tanstack/react-query";

import { readServerSentEvents } from "@/lib/sse";

import { api, unwrap } from "./client";
import { ApiError } from "./errors";
import type { components, operations } from "./schema.gen";

export type Draft = components["schemas"]["DraftRead"];
export type DraftUpdate = components["schemas"]["DraftUpdate"];
export type DraftGenerate = components["schemas"]["DraftGenerate"];
/** One event of the generation stream (`POST /drafts/generate`). */
export type DraftEvent =
  operations["drafts_generate_draft"]["responses"][200]["content"]["text/event-stream"];

/**
 * Query keys. Drafts live under `["drafts"]`, not `["message", …]`: mail events must not
 * reload a draft while it is being edited.
 */
export const draftKeys = {
  all: ["drafts"] as const,
  open: ["drafts", "open"] as const,
  message: (messageId: string) => ["drafts", "message", messageId] as const,
};

export const DRAFTS_LIMIT = 200;

/** Open drafts, most recently changed first (overview). */
export const openDraftsQueryOptions = queryOptions({
  queryKey: draftKeys.open,
  queryFn: ({ signal }) =>
    unwrap(
      api.GET("/drafts", { params: { query: { status: "draft", limit: DRAFTS_LIMIT } }, signal }),
    ),
});

/** Open drafts replying to one mail (the newest is continued in the thread). */
export function messageDraftsQueryOptions(messageId: string) {
  return queryOptions({
    queryKey: draftKeys.message(messageId),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/drafts", {
          params: { query: { message_id: messageId, status: "draft", limit: 1 } },
          signal,
        }),
      ),
  });
}

export function createDraft(messageId: string, replyAll: boolean) {
  return unwrap(
    api.POST("/drafts", { body: { message_id: messageId, reply_all: replyAll, body: "" } }),
  );
}

export function updateDraft(draftId: string, body: DraftUpdate) {
  return unwrap(api.PATCH("/drafts/{draft_id}", { params: { path: { draft_id: draftId } }, body }));
}

export function sendDraft(draftId: string) {
  return unwrap(api.POST("/drafts/{draft_id}/send", { params: { path: { draft_id: draftId } } }));
}

export function discardDraft(draftId: string) {
  return unwrap(
    api.POST("/drafts/{draft_id}/discard", { params: { path: { draft_id: draftId } } }),
  );
}

export function deleteDraft(draftId: string) {
  return unwrap(api.DELETE("/drafts/{draft_id}", { params: { path: { draft_id: draftId } } }));
}

/**
 * Generates the text of a draft and yields the events of the stream. Aborting `signal`
 * stops it (the server then stores nothing). Request errors throw an `ApiError`.
 */
export async function* generateDraft(
  body: DraftGenerate,
  signal: AbortSignal,
): AsyncGenerator<DraftEvent> {
  const stream = await unwrap(api.POST("/drafts/generate", { body, parseAs: "stream", signal }));
  if (!stream) throw new ApiError(0);
  for await (const { data } of readServerSentEvents(stream as ReadableStream<Uint8Array>)) {
    yield JSON.parse(data) as DraftEvent;
  }
}

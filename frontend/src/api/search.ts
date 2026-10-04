import { queryOptions } from "@tanstack/react-query";

import { readServerSentEvents } from "@/lib/sse";

import { api, unwrap } from "./client";
import { ApiError } from "./errors";
import type { components, operations } from "./schema.gen";

export type SearchFilterParams = components["schemas"]["SearchFilterParams"];
export type SearchHit = components["schemas"]["SearchHitRead"];
export type SearchResults = components["schemas"]["SearchResults"];
export type ConversationSummary = components["schemas"]["ConversationSummary"];
export type Conversation = components["schemas"]["ConversationRead"];
export type ConversationMessage = components["schemas"]["ConversationMessage"];
export type Citation = components["schemas"]["CitationRead"];
export type AskRequest = components["schemas"]["AskRequest"];
/** One event of the answer stream (`POST /rag/ask`). */
export type AnswerEvent = operations["rag_ask"]["responses"][200]["content"]["text/event-stream"];
export type AnswerSource = Extract<AnswerEvent, { type: "sources" }>["sources"][number];
export type AnswerStatus = Extract<AnswerEvent, { type: "done" }>["status"];

/**
 * Query keys. Hits start with `message`, so mail events without a rule of their own refresh them
 * (not `message.processed`/`message.updated`, #223: a rerun embeds the query again); the
 * conversations of "ask your inbox" live under `rag`.
 */
export const searchKeys = {
  hits: (query: string, filters: SearchFilterParams) =>
    ["message", "search", query, filters] as const,
  conversations: ["rag", "conversations"] as const,
  conversation: (id: string) => ["rag", "conversations", id] as const,
};

export const SEARCH_LIMIT = 30;

export function searchQueryOptions(query: string, filters: SearchFilterParams) {
  return queryOptions({
    queryKey: searchKeys.hits(query, filters),
    // POST: the query stays out of URLs and access logs.
    queryFn: ({ signal }) =>
      unwrap(api.POST("/search", { body: { query, filters, limit: SEARCH_LIMIT }, signal })),
    staleTime: 30_000,
  });
}

export const conversationsQueryOptions = queryOptions({
  queryKey: searchKeys.conversations,
  queryFn: ({ signal }) =>
    unwrap(api.GET("/rag/conversations", { params: { query: { limit: 100 } }, signal })),
});

export function conversationQueryOptions(id: string) {
  return queryOptions({
    queryKey: searchKeys.conversation(id),
    queryFn: ({ signal }) =>
      unwrap(
        api.GET("/rag/conversations/{conversation_id}", {
          params: { path: { conversation_id: id } },
          signal,
        }),
      ),
  });
}

export function deleteConversation(id: string) {
  return unwrap(
    api.DELETE("/rag/conversations/{conversation_id}", {
      params: { path: { conversation_id: id } },
    }),
  );
}

export function deleteAllConversations() {
  return unwrap(api.DELETE("/rag/conversations"));
}

/**
 * Asks a question and yields the events of the streamed answer. Aborting `signal` stops
 * the stream (the server then stores nothing). Request errors throw an `ApiError`.
 */
export async function* askQuestion(
  body: AskRequest,
  signal: AbortSignal,
): AsyncGenerator<AnswerEvent> {
  const stream = await unwrap(api.POST("/rag/ask", { body, parseAs: "stream", signal }));
  if (!stream) throw new ApiError(0);
  for await (const { data } of readServerSentEvents(stream as ReadableStream<Uint8Array>)) {
    yield JSON.parse(data) as AnswerEvent;
  }
}

import { type QueryClient, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";

import { API_BASE_PATH } from "@/api/client";

export const EVENTS_URL = `${API_BASE_PATH}/events`;

/**
 * A server event from `GET /api/events` (#7). The SSE `data` field is a JSON object with the
 * event `type` (e.g. `"message.synced"`) and only IDs and status values, never content.
 * The type can also be sent as the SSE `event` name.
 */
export interface ServerEvent {
  type: string;
  [field: string]: unknown;
}

/** Query keys to invalidate for an event. */
export type InvalidationRule = (event: ServerEvent) => QueryKey[];

/**
 * Event types that need more than the default. By default an event `<resource>.<action>`
 * invalidates all queries whose key starts with `[<resource>]`, e.g. `message.synced` →
 * `["message", …]`. Feature modules add entries here, keyed by event type.
 */
export const invalidationRules: Record<string, InvalidationRule> = {
  // Sync progress changes the mailbox status and brings new mails into the inbox.
  "mailbox.sync": () => [["mailbox"], ["message", "list"], ["message", "triage", "inbox"]],
  // A removed or reconfigured mailbox changes which mails are listed.
  "mailbox.changed": () => [["mailbox"], ["message"]],
  // A message got its category (#21): labels and the inbox by category, not the threads.
  "message.triaged": () => [["message", "triage"]],
};

export function defaultInvalidation(event: ServerEvent): QueryKey[] {
  const resource = event.type.split(".")[0];
  return resource ? [[resource]] : [];
}

export function parseServerEvent(message: MessageEvent<unknown>): ServerEvent | undefined {
  if (typeof message.data !== "string") return undefined;
  let payload: unknown;
  try {
    payload = JSON.parse(message.data);
  } catch {
    return undefined;
  }
  if (typeof payload !== "object" || payload === null || Array.isArray(payload)) return undefined;
  const record = payload as Record<string, unknown>;
  const type = typeof record.type === "string" ? record.type : message.type;
  if (!type || type === "message") return undefined;
  return { ...record, type };
}

export function applyServerEvent(
  queryClient: QueryClient,
  event: ServerEvent,
  rules: Record<string, InvalidationRule> = invalidationRules,
) {
  const rule = Object.hasOwn(rules, event.type) ? rules[event.type] : undefined;
  for (const queryKey of (rule ?? defaultInvalidation)(event)) {
    void queryClient.invalidateQueries({ queryKey });
  }
}

export interface UseEventsOptions {
  url?: string;
  enabled?: boolean;
  rules?: Record<string, InvalidationRule>;
}

/**
 * Subscribes to server events and turns them into TanStack Query invalidations.
 *
 * The browser reconnects on its own after network errors. Events missed while disconnected
 * are not replayed, so after a reconnect all queries are refetched.
 */
export function useEvents({
  url = EVENTS_URL,
  enabled = true,
  rules = invalidationRules,
}: UseEventsOptions = {}) {
  const queryClient = useQueryClient();

  useEffect(() => {
    if (!enabled || typeof EventSource === "undefined") return;

    const source = new EventSource(url, { withCredentials: true });
    let disconnected = false;

    const onMessage = (message: MessageEvent<unknown>) => {
      const event = parseServerEvent(message);
      if (event) applyServerEvent(queryClient, event, rules);
    };
    const onOpen = () => {
      if (disconnected) void queryClient.invalidateQueries();
      disconnected = false;
    };
    const onError = () => {
      disconnected = true;
    };

    source.addEventListener("message", onMessage);
    source.addEventListener("open", onOpen);
    source.addEventListener("error", onError);
    // Named SSE events (`event: <type>`) only reach listeners registered for that name.
    for (const type of Object.keys(rules)) source.addEventListener(type, onMessage);

    return () => {
      source.close();
    };
  }, [queryClient, url, enabled, rules]);
}

/** Mount once inside the app shell. */
export function EventsListener() {
  useEvents();
  return null;
}

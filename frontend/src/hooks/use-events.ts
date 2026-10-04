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
  // A message is processed (#140): its tasks and labels may be new, the search finds it. Not
  // the threads and lists: processing changes neither, and during an import every message
  // sends this event.
  "message.processed": () => [
    ["message", "todos"],
    ["message", "triage"],
    ["message", "search"],
  ],
  // A new mail to announce (#149): shown as a browser notification, no data to reload.
  "notification.message": () => [],
};

export type ServerEventListener = (event: ServerEvent) => void;

const eventListeners = new Set<ServerEventListener>();

/**
 * Receive every server event of the open stream (e.g. to show a notification). Events are
 * only delivered while `useEvents` is mounted. Returns the function to unsubscribe.
 */
export function subscribeServerEvents(listener: ServerEventListener): () => void {
  eventListeners.add(listener);
  return () => {
    eventListeners.delete(listener);
  };
}

function emitServerEvent(event: ServerEvent) {
  for (const listener of eventListeners) listener(event);
}

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

export function invalidationsFor(
  event: ServerEvent,
  rules: Record<string, InvalidationRule> = invalidationRules,
): QueryKey[] {
  const rule = Object.hasOwn(rules, event.type) ? rules[event.type] : undefined;
  return (rule ?? defaultInvalidation)(event);
}

/** Quiet time after the last event before a batch of invalidations is applied. */
export const INVALIDATION_DEBOUNCE_MS = 2_000;
/** Longest an event waits while events keep coming (e.g. during an import). */
export const INVALIDATION_MAX_WAIT_MS = 5_000;

function startsWith(key: QueryKey, prefix: QueryKey) {
  return (
    prefix.length <= key.length &&
    prefix.every((part, index) => JSON.stringify(part) === JSON.stringify(key[index]))
  );
}

/**
 * Bundles invalidations (#140): an event after a quiet period applies at once; events that
 * follow are collected and applied together once no event came for `debounceMs`, or at the
 * latest `maxWaitMs` after the batch started. Each key is invalidated once per batch, and
 * not at all if a shorter key of the same batch covers it. So an import of thousands of
 * messages refetches the lists every few seconds instead of once per message.
 */
export function createInvalidationBatcher(
  queryClient: QueryClient,
  { debounceMs = INVALIDATION_DEBOUNCE_MS, maxWaitMs = INVALIDATION_MAX_WAIT_MS } = {},
) {
  const pending = new Map<string, QueryKey>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  // Start of the current batch; `undefined` while quiet.
  let batchStart: number | undefined;

  const invalidate = (keys: QueryKey[]) => {
    for (const queryKey of keys) {
      if (keys.some((other) => other.length < queryKey.length && startsWith(queryKey, other)))
        continue;
      void queryClient.invalidateQueries({ queryKey });
    }
  };
  const flush = () => {
    timer = undefined;
    if (pending.size === 0) {
      batchStart = undefined;
      return;
    }
    const keys = [...pending.values()];
    pending.clear();
    invalidate(keys);
    batchStart = Date.now();
    timer = setTimeout(flush, debounceMs);
  };

  return {
    add(keys: QueryKey[]) {
      if (keys.length === 0) return;
      if (batchStart === undefined) {
        batchStart = Date.now();
        invalidate([...new Map(keys.map((key) => [JSON.stringify(key), key])).values()]);
        timer = setTimeout(flush, debounceMs);
        return;
      }
      for (const key of keys) pending.set(JSON.stringify(key), key);
      clearTimeout(timer);
      const left = batchStart + maxWaitMs - Date.now();
      timer = setTimeout(flush, Math.max(0, Math.min(debounceMs, left)));
    },
    /** Drops what is pending (everything is refetched anyway, or nobody listens anymore). */
    cancel() {
      clearTimeout(timer);
      timer = undefined;
      pending.clear();
      batchStart = undefined;
    },
  };
}

export interface UseEventsOptions {
  url?: string;
  enabled?: boolean;
  rules?: Record<string, InvalidationRule>;
}

/**
 * Subscribes to server events and turns them into TanStack Query invalidations, bundled by
 * `createInvalidationBatcher`.
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
    const batcher = createInvalidationBatcher(queryClient);
    let disconnected = false;

    const onMessage = (message: MessageEvent<unknown>) => {
      const event = parseServerEvent(message);
      if (!event) return;
      batcher.add(invalidationsFor(event, rules));
      emitServerEvent(event);
    };
    const onOpen = () => {
      if (disconnected) {
        batcher.cancel();
        void queryClient.invalidateQueries();
      }
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
      batcher.cancel();
    };
  }, [queryClient, url, enabled, rules]);
}

/** Mount once inside the app shell. */
export function EventsListener() {
  useEvents();
  return null;
}

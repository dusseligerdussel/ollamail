import {
  type InfiniteData,
  type Query,
  type QueryClient,
  type QueryKey,
  useQueryClient,
} from "@tanstack/react-query";
import { useEffect } from "react";

import { API_BASE_PATH } from "@/api/client";
import { patchMessage, removeFromLists } from "@/components/mail/message-cache";

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

/**
 * A query key to invalidate. `firstPage` (#188) drops all but the first page of the infinite
 * queries under the key before they are refetched, so a list scrolled through many pages
 * refetches one page, not all of them; further pages load again while scrolling.
 * `minAgeMs` (#223) refetches a query only once its data is that old; younger queries are
 * refetched when they reach that age, so a burst of events reloads them at most that often.
 */
export type Invalidation =
  | QueryKey
  | { queryKey: QueryKey; firstPage?: boolean; minAgeMs?: number };

/** Queries to invalidate for an event. */
export type InvalidationRule = (event: ServerEvent) => Invalidation[];

/** Invalidate `queryKey`, refetching only the first page of its infinite queries. */
export function firstPage(queryKey: QueryKey): Invalidation {
  return { queryKey, firstPage: true };
}

/**
 * How often the inbox by category is reloaded at most while messages are triaged (#223): its
 * first page also counts the messages per category, and during an import every message is
 * triaged.
 */
export const TRIAGE_REFRESH_MS = 30_000;

// Inbox lists (by date and by category): long, paged, and changed by every import batch.
const LIST_BY_DATE = firstPage(["message", "list"]);
const LIST_BY_CATEGORY = firstPage(["message", "triage", "inbox"]);
const LIST_BY_CATEGORY_THROTTLED: Invalidation = {
  queryKey: ["message", "triage", "inbox"],
  firstPage: true,
  minAgeMs: TRIAGE_REFRESH_MS,
};

/** ID of the message an event is about (`ids.message_id`), if any. */
export function eventMessageId(event: ServerEvent): string | undefined {
  const ids = event.ids;
  if (typeof ids !== "object" || ids === null) return undefined;
  const id = (ids as Record<string, unknown>).message_id;
  return typeof id === "string" ? id : undefined;
}

// `message.updated` (#223): what changed (`status`) and how the cached message changes.
const STATE_CHANGES: Record<string, { unread: boolean } | { flagged: boolean }> = {
  seen: { unread: false },
  unseen: { unread: true },
  flagged: { flagged: true },
  unflagged: { flagged: false },
};
const MOVES = new Set(["archive", "move", "trash"]);

/**
 * Event types that need more than the default. By default an event `<resource>.<action>`
 * invalidates all queries whose key starts with `[<resource>]`, e.g. `message.synced` →
 * `["message", …]`. Feature modules add entries here, keyed by event type.
 */
export const invalidationRules: Record<string, InvalidationRule> = {
  // Sync progress (one event per import batch) changes the mailbox status only; the lists
  // follow once the sync is done or failed (#188), with their first page.
  "mailbox.sync": (event) =>
    event.status === "progress" ? [["mailbox"]] : [["mailbox"], LIST_BY_DATE, LIST_BY_CATEGORY],
  // A removed or reconfigured mailbox changes which mails are listed.
  "mailbox.changed": () => [["mailbox"], ["message"]],
  // A message got its category (#21): labels and the inbox by category, not the threads. The
  // inbox by category at most every `TRIAGE_REFRESH_MS` (#223).
  "message.triaged": () => [["message", "triage", "result"], LIST_BY_CATEGORY_THROTTLED],
  // A message is processed (#140): its tasks and labels may be new. Not the threads and the
  // list by date: processing changes neither, and during an import every message sends this
  // event. Not the search either (#223): rerunning it embeds the query again; a search
  // started later finds the message.
  "message.processed": () => [
    ["message", "todos"],
    ["message", "triage", "result"],
    LIST_BY_CATEGORY_THROTTLED,
  ],
  // A message was marked read/unread, flagged/unflagged (patched in the cache, see
  // `cacheUpdates`) or moved (#223). Opening a mail sends this, so neither the thread nor the
  // search is reloaded. Lists filtered by read state may gain or lose the message; moved
  // messages leave the lists and may enter the one of their new folder.
  "message.updated": (event) => {
    const status = typeof event.status === "string" ? event.status : "";
    if (status === "seen" || status === "unseen") {
      return [
        ["message", "list", { unread: true }],
        ["message", "list", { unread: false }],
        ["message", "triage", "inbox", { unread: true }],
        ["message", "triage", "inbox", { unread: false }],
      ];
    }
    if (Object.hasOwn(STATE_CHANGES, status)) return [];
    if (MOVES.has(status)) {
      return [
        ["message", "list"],
        ["message", "triage", "inbox"],
      ];
    }
    return [["message"]];
  },
  // A new mail to announce (#149): shown as a browser notification, no data to reload.
  "notification.message": () => [],
};

/**
 * Cache changes applied at once for an event, before (and instead of most of) its
 * invalidations: the event carries all that changed, so nothing needs to be refetched.
 */
export type CacheUpdate = (queryClient: QueryClient, event: ServerEvent) => void;

export const cacheUpdates: Record<string, CacheUpdate> = {
  "message.updated": (queryClient, event) => {
    const messageId = eventMessageId(event);
    const status = typeof event.status === "string" ? event.status : "";
    if (!messageId) return;
    const change = Object.hasOwn(STATE_CHANGES, status) ? STATE_CHANGES[status] : undefined;
    if (change) patchMessage(queryClient, messageId, change);
    else if (MOVES.has(status)) removeFromLists(queryClient, messageId);
  },
};

export function applyCacheUpdate(
  queryClient: QueryClient,
  event: ServerEvent,
  updates: Record<string, CacheUpdate> = cacheUpdates,
) {
  const update = Object.hasOwn(updates, event.type) ? updates[event.type] : undefined;
  update?.(queryClient, event);
}

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

export function defaultInvalidation(event: ServerEvent): Invalidation[] {
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
): Invalidation[] {
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

interface PendingInvalidation {
  queryKey: QueryKey;
  firstPage: boolean;
  /** 0: refetch now, whatever the age of the data. */
  minAgeMs: number;
}

function pendingInvalidation(item: Invalidation): PendingInvalidation {
  if (Array.isArray(item)) return { queryKey: item, firstPage: false, minAgeMs: 0 };
  const options = item as Exclude<Invalidation, QueryKey>;
  return {
    queryKey: options.queryKey,
    firstPage: options.firstPage ?? false,
    minAgeMs: options.minAgeMs ?? 0,
  };
}

type QueryFilter = (query: Query) => boolean;

function isInfiniteData(data: unknown): data is InfiniteData<unknown, unknown> {
  return (
    typeof data === "object" &&
    data !== null &&
    Array.isArray((data as Partial<InfiniteData<unknown>>).pages) &&
    Array.isArray((data as Partial<InfiniteData<unknown>>).pageParams)
  );
}

/** Drops all but the first page of the infinite queries under `queryKey`. */
export function keepFirstPage(
  queryClient: QueryClient,
  queryKey: QueryKey,
  predicate: QueryFilter = () => true,
) {
  for (const query of queryClient.getQueryCache().findAll({ queryKey, predicate })) {
    const data = query.state.data;
    if (!isInfiniteData(data) || data.pages.length <= 1) continue;
    queryClient.setQueryData(query.queryKey, {
      pages: data.pages.slice(0, 1),
      pageParams: data.pageParams.slice(0, 1),
    });
  }
}

/**
 * Bundles invalidations (#140): an event after a quiet period applies at once; events that
 * follow are collected and applied together once no event came for `debounceMs`, or at the
 * latest `maxWaitMs` after the batch started. Each key is invalidated once per batch, and
 * not at all if a shorter key of the same batch covers it. So an import of thousands of
 * messages refetches the lists every few seconds instead of once per message. A key keeps
 * `firstPage` (and `minAgeMs`) only if every invalidation it stands for asked for it.
 * Queries too young for `minAgeMs` are invalidated once they are old enough (#223).
 */
export function createInvalidationBatcher(
  queryClient: QueryClient,
  { debounceMs = INVALIDATION_DEBOUNCE_MS, maxWaitMs = INVALIDATION_MAX_WAIT_MS } = {},
) {
  const pending = new Map<string, PendingInvalidation>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  // Start of the current batch; `undefined` while quiet.
  let batchStart: number | undefined;
  // Invalidations waiting for their queries to reach `minAgeMs`, and when the next is due.
  const deferred = new Map<string, PendingInvalidation>();
  let deferredTimer: ReturnType<typeof setTimeout> | undefined;
  let deferredDue = Number.POSITIVE_INFINITY;

  const merge = (known: PendingInvalidation | undefined, next: PendingInvalidation) =>
    known
      ? {
          ...known,
          firstPage: known.firstPage && next.firstPage,
          minAgeMs: Math.min(known.minAgeMs, next.minAgeMs),
        }
      : next;
  const collect = (target: Map<string, PendingInvalidation>, items: Invalidation[]) => {
    for (const item of items) {
      const next = pendingInvalidation(item);
      const id = JSON.stringify(next.queryKey);
      target.set(id, merge(target.get(id), next));
    }
  };
  const runDeferred = () => {
    deferredTimer = undefined;
    deferredDue = Number.POSITIVE_INFINITY;
    const entries = [...deferred.values()];
    deferred.clear();
    invalidate(entries);
  };
  const defer = (entry: PendingInvalidation, due: number) => {
    const id = JSON.stringify(entry.queryKey);
    deferred.set(id, merge(deferred.get(id), entry));
    if (due >= deferredDue) return;
    clearTimeout(deferredTimer);
    deferredDue = due;
    deferredTimer = setTimeout(runDeferred, Math.max(0, due - Date.now()));
  };
  const invalidate = (entries: PendingInvalidation[]) => {
    const covers = (outer: PendingInvalidation, inner: PendingInvalidation) =>
      outer.queryKey.length < inner.queryKey.length && startsWith(inner.queryKey, outer.queryKey);
    for (const entry of entries) {
      if (entries.some((other) => covers(other, entry) && other.minAgeMs === 0)) continue;
      const firstPage = entries.every((other) => !covers(entry, other) || other.firstPage);
      let predicate: QueryFilter | undefined;
      if (entry.minAgeMs > 0) {
        const now = Date.now();
        const old = new Set<Query>();
        let due = Number.POSITIVE_INFINITY;
        for (const query of queryClient.getQueryCache().findAll({ queryKey: entry.queryKey })) {
          const updatedAt = query.state.dataUpdatedAt;
          if (now - updatedAt >= entry.minAgeMs) old.add(query);
          else due = Math.min(due, updatedAt + entry.minAgeMs);
        }
        if (due < Number.POSITIVE_INFINITY) defer(entry, due);
        // Decided before `keepFirstPage`, which renews the data of the queries it trims.
        predicate = (query) => old.has(query);
      }
      if (entry.firstPage && firstPage) keepFirstPage(queryClient, entry.queryKey, predicate);
      void queryClient.invalidateQueries(
        predicate ? { queryKey: entry.queryKey, predicate } : { queryKey: entry.queryKey },
      );
    }
  };
  const flush = () => {
    timer = undefined;
    if (pending.size === 0) {
      batchStart = undefined;
      return;
    }
    const entries = [...pending.values()];
    pending.clear();
    invalidate(entries);
    batchStart = Date.now();
    timer = setTimeout(flush, debounceMs);
  };

  return {
    add(items: Invalidation[]) {
      if (items.length === 0) return;
      if (batchStart === undefined) {
        batchStart = Date.now();
        const now = new Map<string, PendingInvalidation>();
        collect(now, items);
        invalidate([...now.values()]);
        timer = setTimeout(flush, debounceMs);
        return;
      }
      collect(pending, items);
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
      clearTimeout(deferredTimer);
      deferredTimer = undefined;
      deferredDue = Number.POSITIVE_INFINITY;
      deferred.clear();
    },
  };
}

export interface UseEventsOptions {
  url?: string;
  enabled?: boolean;
  rules?: Record<string, InvalidationRule>;
  updates?: Record<string, CacheUpdate>;
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
  updates = cacheUpdates,
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
      applyCacheUpdate(queryClient, event, updates);
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
    const named = new Set([...Object.keys(rules), ...Object.keys(updates)]);
    for (const type of named) source.addEventListener(type, onMessage);

    return () => {
      source.close();
      batcher.cancel();
    };
  }, [queryClient, url, enabled, rules, updates]);
}

/** Mount once inside the app shell. */
export function EventsListener() {
  useEvents();
  return null;
}

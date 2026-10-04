import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import {
  INVALIDATION_DEBOUNCE_MS,
  INVALIDATION_MAX_WAIT_MS,
  type InvalidationRule,
  invalidationsFor,
  parseServerEvent,
  subscribeServerEvents,
  TRIAGE_REFRESH_MS,
  useEvents,
} from "./use-events";

class MockEventSource {
  static instances: MockEventSource[] = [];
  readonly listeners = new Map<string, Set<(event: Event) => void>>();
  closed = false;

  constructor(
    readonly url: string,
    readonly init?: EventSourceInit,
  ) {
    MockEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: Event) => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)?.add(listener);
  }

  close() {
    this.closed = true;
  }

  emit(type: string, data?: unknown) {
    const event =
      data === undefined
        ? new Event(type)
        : new MessageEvent(type, { data: typeof data === "string" ? data : JSON.stringify(data) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
  }

  static get last() {
    const source = MockEventSource.instances.at(-1);
    if (!source) throw new Error("no EventSource created");
    return source;
  }
}

let queryClient: QueryClient;
let invalidate: MockInstance<QueryClient["invalidateQueries"]>;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  MockEventSource.instances = [];
  vi.stubGlobal("EventSource", MockEventSource);
  queryClient = new QueryClient();
  invalidate = vi.spyOn(queryClient, "invalidateQueries");
});

afterEach(() => {
  queryClient.clear();
  vi.useRealTimers();
});

describe("useEvents", () => {
  it("connects to /api/events with credentials and closes on unmount", () => {
    const { unmount } = renderHook(() => useEvents(), { wrapper });

    expect(MockEventSource.last.url).toBe("/api/events");
    expect(MockEventSource.last.init).toEqual({ withCredentials: true });

    unmount();
    expect(MockEventSource.last.closed).toBe(true);
  });

  it("invalidates queries of the event's resource by default", () => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "message.synced", message_id: "m1" });

    expect(invalidate).toHaveBeenCalledExactlyOnceWith({ queryKey: ["message"] });
  });

  it("uses a registered rule for its event type", () => {
    const rules: Record<string, InvalidationRule> = {
      "mailbox.sync_finished": (event) => [["mailbox", event.mailbox_id], ["message"]],
    };
    renderHook(() => useEvents({ rules }), { wrapper });

    MockEventSource.last.emit("message", { type: "mailbox.sync_finished", mailbox_id: "b1" });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mailbox", "b1"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["message"] });
    expect(invalidate).toHaveBeenCalledTimes(2);
  });

  it("refreshes only the mailbox status while a sync makes progress", () => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "mailbox.sync", status: "progress" });

    expect(invalidate).toHaveBeenCalledExactlyOnceWith({ queryKey: ["mailbox"] });
  });

  it.each(["done", "failed"])("refreshes the inbox once a sync is %s", (status) => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "mailbox.sync", status });

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["mailbox"],
      ["message", "list"],
      ["message", "triage", "inbox"],
    ]);
  });

  it("refreshes categories, not threads, when a message is triaged", () => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "message.triaged", message_id: "m1" });

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["message", "triage", "result"],
      ["message", "triage", "inbox"],
    ]);
  });

  it("refreshes tasks and labels, not threads, lists or search, when a message is processed", () => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "message.processed", message_id: "m1" });

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["message", "todos"],
      ["message", "triage", "result"],
      ["message", "triage", "inbox"],
    ]);
  });

  it("patches a message marked read instead of reloading threads, lists or search", () => {
    const item = (id: string) => ({ id, unread: true, flagged: false });
    const list = { pages: [{ items: [item("m1"), item("m2")] }], pageParams: [undefined] };
    queryClient.setQueryData(["message", "list", {}], list);
    queryClient.setQueryData(["message", "triage", "inbox", { category: "all" }], list);
    queryClient.setQueryData(["message", "thread", "m1"], { messages: [item("m1")] });
    queryClient.setQueryData(["message", "search", "q", {}], { hits: [] });
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", {
      type: "message.updated",
      ids: { message_id: "m1", mailbox_id: "b1" },
      status: "seen",
    });

    const read = { pages: [{ items: [{ ...item("m1"), unread: false }, item("m2")] }] };
    expect(queryClient.getQueryData(["message", "list", {}])).toMatchObject(read);
    expect(
      queryClient.getQueryData(["message", "triage", "inbox", { category: "all" }]),
    ).toMatchObject(read);
    expect(queryClient.getQueryData(["message", "thread", "m1"])).toEqual({
      messages: [{ ...item("m1"), unread: false }],
    });
    // Only the lists filtered by read state, which may lose or gain the message.
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["message", "list", { unread: true }],
      ["message", "list", { unread: false }],
      ["message", "triage", "inbox", { unread: true }],
      ["message", "triage", "inbox", { unread: false }],
    ]);
    expect(queryClient.getQueryState(["message", "list", {}])?.isInvalidated).toBe(false);
    expect(queryClient.getQueryState(["message", "thread", "m1"])?.isInvalidated).toBe(false);
    expect(queryClient.getQueryState(["message", "search", "q", {}])?.isInvalidated).toBe(false);
  });

  it("patches a flagged message without reloading anything", () => {
    queryClient.setQueryData(["message", "thread", "m1"], {
      messages: [{ id: "m1", flagged: false }],
    });
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message.updated", {
      type: "message.updated",
      ids: { message_id: "m1" },
      status: "flagged",
    });

    expect(queryClient.getQueryData(["message", "thread", "m1"])).toEqual({
      messages: [{ id: "m1", flagged: true }],
    });
    expect(invalidate).not.toHaveBeenCalled();
  });

  it.each(["archive", "move", "trash"])("removes a message from the lists on %s", (status) => {
    const list = {
      pages: [{ items: [{ id: "m1" }, { id: "m2" }], total: 2 }],
      pageParams: [undefined],
    };
    queryClient.setQueryData(["message", "list", {}], list);
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", {
      type: "message.updated",
      ids: { message_id: "m1" },
      status,
    });

    expect(queryClient.getQueryData(["message", "list", {}])).toMatchObject({
      pages: [{ items: [{ id: "m2" }], total: 1 }],
    });
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["message", "list"],
      ["message", "triage", "inbox"],
    ]);
  });

  it("reloads the inbox by category at most every TRIAGE_REFRESH_MS while messages are triaged", () => {
    vi.useFakeTimers();
    const key = ["message", "triage", "inbox", { category: "all" }];
    const data = {
      pages: [{ items: [1], groups: [] }, { items: [2] }],
      pageParams: [undefined, "c1"],
    };
    queryClient.setQueryData(key, data);
    const isInvalidated = () => queryClient.getQueryState(key)?.isInvalidated;
    renderHook(() => useEvents(), { wrapper });
    const emit = () => MockEventSource.last.emit("message", { type: "message.triaged" });

    // Fresh data stays, pages included, while events keep coming.
    for (let elapsed = 0; elapsed < TRIAGE_REFRESH_MS - 1_000; elapsed += 1_000) {
      emit();
      vi.advanceTimersByTime(1_000);
    }
    expect(isInvalidated()).toBe(false);
    expect(queryClient.getQueryData(key)).toEqual(data);

    // Once old enough, it is reloaded (first page only), even without another event.
    vi.advanceTimersByTime(1_000);
    expect(isInvalidated()).toBe(true);
    expect(queryClient.getQueryData(key)).toEqual({
      pages: data.pages.slice(0, 1),
      pageParams: data.pageParams.slice(0, 1),
    });
  });

  it("reloads old data of the inbox by category at once when a message is triaged", () => {
    vi.useFakeTimers();
    const key = ["message", "triage", "inbox", { category: "all" }];
    queryClient.setQueryData(key, { pages: [{ items: [] }], pageParams: [undefined] });
    vi.advanceTimersByTime(TRIAGE_REFRESH_MS);
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "message.triaged" });

    expect(queryClient.getQueryState(key)?.isInvalidated).toBe(true);
  });

  it("drops deferred invalidations on unmount", () => {
    vi.useFakeTimers();
    const key = ["message", "triage", "inbox", { category: "all" }];
    queryClient.setQueryData(key, { pages: [{ items: [] }], pageParams: [undefined] });
    const { unmount } = renderHook(() => useEvents(), { wrapper });
    MockEventSource.last.emit("message", { type: "message.triaged" });

    unmount();
    vi.advanceTimersByTime(TRIAGE_REFRESH_MS);

    expect(queryClient.getQueryState(key)?.isInvalidated).toBe(false);
  });

  it("keeps only the first page of a long list before refetching it", () => {
    const pages = (count: number) => ({
      pages: Array.from({ length: count }, (_, index) => ({ items: [index] })),
      pageParams: Array.from({ length: count }, (_, index) => (index ? `c${index}` : undefined)),
    });
    queryClient.setQueryData(["message", "list", { unread: true }], pages(10));
    queryClient.setQueryData(["message", "triage", "inbox", {}], pages(3));
    queryClient.setQueryData(["message", "thread", "m1"], { messages: [] });
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "mailbox.sync", status: "done" });

    expect(queryClient.getQueryData(["message", "list", { unread: true }])).toEqual(pages(1));
    expect(queryClient.getQueryData(["message", "triage", "inbox", {}])).toEqual(pages(1));
    expect(queryClient.getQueryData(["message", "thread", "m1"])).toEqual({ messages: [] });
  });

  it("keeps all pages when another event of the batch reloads the list in full", () => {
    vi.useFakeTimers();
    const data = {
      pages: [{ items: [1] }, { items: [2] }],
      pageParams: [undefined, "c1"],
    };
    queryClient.setQueryData(["message", "list", {}], data);
    renderHook(() => useEvents(), { wrapper });
    const emit = (event: object) => MockEventSource.last.emit("message", event);

    emit({ type: "message.triaged" });
    emit({ type: "mailbox.sync", status: "done" });
    emit({ type: "message.updated", message_id: "m1" });
    vi.advanceTimersByTime(INVALIDATION_DEBOUNCE_MS);

    expect(queryClient.getQueryData(["message", "list", {}])).toEqual(data);
  });

  it("bundles the invalidations of an event burst", () => {
    vi.useFakeTimers();
    renderHook(() => useEvents(), { wrapper });
    const emit = (type: string) => MockEventSource.last.emit("message", { type });

    // The first event after a quiet period applies at once.
    emit("message.processed");
    expect(invalidate).toHaveBeenCalledTimes(3);
    invalidate.mockClear();

    // Events that follow wait until no event came for a while, and each key runs once.
    for (let index = 0; index < 50; index++) emit("message.processed");
    emit("mailbox.sync");
    vi.advanceTimersByTime(INVALIDATION_DEBOUNCE_MS - 1);
    expect(invalidate).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["message", "todos"],
      ["message", "triage", "result"],
      ["message", "triage", "inbox"],
      ["mailbox"],
      ["message", "list"],
    ]);
    invalidate.mockClear();

    // A shorter key covers the longer ones of the same batch.
    emit("message.triaged");
    emit("mailbox.changed");
    vi.advanceTimersByTime(INVALIDATION_DEBOUNCE_MS);
    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["mailbox"],
      ["message"],
    ]);
  });

  it("applies a batch at the latest after the maximum wait while events keep coming", () => {
    vi.useFakeTimers();
    renderHook(() => useEvents(), { wrapper });
    const emit = () => MockEventSource.last.emit("message", { type: "message.triaged" });

    emit();
    invalidate.mockClear();
    for (let elapsed = 0; elapsed < INVALIDATION_MAX_WAIT_MS; elapsed += 500) {
      vi.advanceTimersByTime(500);
      emit();
    }

    expect(invalidate.mock.calls.map(([filters]) => filters?.queryKey)).toEqual([
      ["message", "triage", "result"],
      ["message", "triage", "inbox"],
    ]);
  });

  it("drops pending invalidations on unmount", () => {
    vi.useFakeTimers();
    const { unmount } = renderHook(() => useEvents(), { wrapper });
    MockEventSource.last.emit("message", { type: "message.triaged" });
    MockEventSource.last.emit("message", { type: "message.triaged" });
    invalidate.mockClear();

    unmount();
    vi.advanceTimersByTime(INVALIDATION_MAX_WAIT_MS);

    expect(invalidate).not.toHaveBeenCalled();
  });

  it("passes notification events to subscribers without reloading anything", () => {
    const received: unknown[] = [];
    const unsubscribe = subscribeServerEvents((event) => received.push(event));
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("notification.message", {
      type: "notification.message",
      ids: { message_id: "m1", mailbox_id: "b1" },
    });
    unsubscribe();
    MockEventSource.last.emit("message", { type: "notification.message" });

    expect(received).toEqual([
      { type: "notification.message", ids: { message_id: "m1", mailbox_id: "b1" } },
    ]);
    expect(invalidate).not.toHaveBeenCalled();
  });

  it("receives named SSE events for registered types", () => {
    const rules: Record<string, InvalidationRule> = { "todo.created": () => [["todo"]] };
    renderHook(() => useEvents({ rules }), { wrapper });

    MockEventSource.last.emit("todo.created", { todo_id: "t1" });

    expect(invalidate).toHaveBeenCalledExactlyOnceWith({ queryKey: ["todo"] });
  });

  it("ignores malformed events", () => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", "not json");
    MockEventSource.last.emit("message", [1, 2]);
    MockEventSource.last.emit("message", { status: "no type" });

    expect(invalidate).not.toHaveBeenCalled();
  });

  it("refetches everything after a reconnect, not on the first connect", () => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("open");
    expect(invalidate).not.toHaveBeenCalled();

    MockEventSource.last.emit("error");
    MockEventSource.last.emit("open");
    expect(invalidate).toHaveBeenCalledExactlyOnceWith();
  });

  it("does nothing when disabled or unsupported", () => {
    renderHook(() => useEvents({ enabled: false }), { wrapper });
    expect(MockEventSource.instances).toHaveLength(0);

    vi.stubGlobal("EventSource", undefined);
    renderHook(() => useEvents(), { wrapper });
    expect(MockEventSource.instances).toHaveLength(0);
  });
});

describe("invalidationsFor", () => {
  const keys = (event: { type: string; status?: string }) =>
    invalidationsFor(event).map((item) => ("queryKey" in item ? item.queryKey : item));

  it.each([
    [{ type: "message.synced" }, [["message"]]],
    [{ type: "mailbox.sync", status: "progress" }, [["mailbox"]]],
    [
      { type: "mailbox.sync", status: "done" },
      [["mailbox"], ["message", "list"], ["message", "triage", "inbox"]],
    ],
    [{ type: "mailbox.changed" }, [["mailbox"], ["message"]]],
    [
      { type: "message.triaged" },
      [
        ["message", "triage", "result"],
        ["message", "triage", "inbox"],
      ],
    ],
    [
      { type: "message.processed" },
      [
        ["message", "todos"],
        ["message", "triage", "result"],
        ["message", "triage", "inbox"],
      ],
    ],
    [
      { type: "message.updated", status: "seen" },
      [
        ["message", "list", { unread: true }],
        ["message", "list", { unread: false }],
        ["message", "triage", "inbox", { unread: true }],
        ["message", "triage", "inbox", { unread: false }],
      ],
    ],
    [{ type: "message.updated", status: "unflagged" }, []],
    [
      { type: "message.updated", status: "archive" },
      [
        ["message", "list"],
        ["message", "triage", "inbox"],
      ],
    ],
    [{ type: "message.updated", status: "unknown" }, [["message"]]],
    [{ type: "notification.message" }, []],
  ])("%o → %j", (event, expected) => {
    expect(keys(event)).toEqual(expected);
  });

  it("never reloads the search or the threads for message events", () => {
    const statuses = ["seen", "unseen", "flagged", "unflagged", "archive", "move", "trash"];
    const events = [
      { type: "message.triaged" },
      { type: "message.processed" },
      ...statuses.map((status) => ({ type: "message.updated", status })),
    ];
    for (const event of events) {
      for (const key of keys(event)) {
        expect(key.slice(0, 2)).not.toEqual(["message", "search"]);
        expect(key.slice(0, 2)).not.toEqual(["message", "thread"]);
        expect(key).not.toEqual(["message"]);
      }
    }
  });

  it("throttles the inbox by category, not the labels, for triage events", () => {
    for (const type of ["message.triaged", "message.processed"]) {
      expect(invalidationsFor({ type })).toContainEqual({
        queryKey: ["message", "triage", "inbox"],
        firstPage: true,
        minAgeMs: TRIAGE_REFRESH_MS,
      });
      expect(invalidationsFor({ type })).toContainEqual(["message", "triage", "result"]);
    }
  });
});

describe("parseServerEvent", () => {
  it("prefers the type from the payload over the SSE event name", () => {
    const message = new MessageEvent("x.y", { data: JSON.stringify({ type: "a.b", id: 1 }) });
    expect(parseServerEvent(message)).toEqual({ type: "a.b", id: 1 });
  });
});

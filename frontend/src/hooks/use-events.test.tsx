import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, type MockInstance, vi } from "vitest";

import { type InvalidationRule, parseServerEvent, useEvents } from "./use-events";

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

  it("refreshes the inbox while a mailbox syncs", () => {
    renderHook(() => useEvents(), { wrapper });

    MockEventSource.last.emit("message", { type: "mailbox.sync", status: "progress" });

    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["mailbox"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["message", "list"] });
    expect(invalidate).toHaveBeenCalledTimes(2);
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

describe("parseServerEvent", () => {
  it("prefers the type from the payload over the SSE event name", () => {
    const message = new MessageEvent("x.y", { data: JSON.stringify({ type: "a.b", id: 1 }) });
    expect(parseServerEvent(message)).toEqual({ type: "a.b", id: 1 });
  });
});

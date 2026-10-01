import { vi } from "vitest";

export type FetchHandler = (request: Request) => Response | Promise<Response>;

export function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { "Content-Type": "application/json", ...init.headers },
  });
}

export function problem(status: number, extra: Record<string, unknown> = {}) {
  return json(
    { type: "about:blank", title: `HTTP ${status}`, status, ...extra },
    { status, headers: { "Content-Type": "application/problem+json" } },
  );
}

/** Default backend for component tests: healthy API, everything else 404. */
export const defaultHandler: FetchHandler = (request) =>
  new URL(request.url).pathname === "/api/healthz" ? json({ status: "ok" }) : problem(404);

/** Replaces the global `fetch` (the API client looks it up per request). */
export function mockFetch(handler: FetchHandler = defaultHandler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

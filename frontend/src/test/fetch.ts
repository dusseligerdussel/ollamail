import { vi } from "vitest";

import type { AuthProviders, AuthSession, User } from "@/api/auth";

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

export const testAdmin: User = {
  id: "00000000-0000-4000-8000-000000000001",
  email: "admin@example.org",
  display_name: "Test Admin",
  role: "admin",
  language: "en",
  timezone: "UTC",
  is_active: true,
  created_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
};

export const testUser: User = {
  ...testAdmin,
  id: "00000000-0000-4000-8000-000000000002",
  email: "user@example.org",
  display_name: "Test User",
  role: "user",
};

export const testSession: AuthSession = {
  id: "00000000-0000-4000-8000-0000000000a1",
  provider: "local",
  created_at: "2026-01-01T08:00:00Z",
  last_seen_at: "2026-01-02T09:30:00Z",
  expires_at: "2026-01-15T08:00:00Z",
  user_agent: "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0",
  current: true,
};

export interface TestBackend {
  initialized?: boolean;
  /** Signed-in user, `null` without a session. */
  user?: User | null;
  providers?: AuthProviders;
}

/**
 * Handler for a set-up instance: `/api/healthz`, setup status, the signed-in user (an admin by
 * default), sign-in options, sessions and the account's privacy options; everything else 404.
 */
export function backend({
  initialized = true,
  user = testAdmin,
  providers = { local_login: true, local_registration: false, passkey_login: false, providers: [] },
}: TestBackend = {}): FetchHandler {
  return (request) => {
    const { pathname } = new URL(request.url);
    const route = `${request.method} ${pathname}`;
    switch (route) {
      case "GET /api/healthz":
        return json({ status: "ok" });
      case "GET /api/setup/status":
        return json({ initialized });
      case "GET /api/auth/me":
        return user ? json(user) : problem(401);
      case "GET /api/auth/providers":
        return json(providers);
      case "GET /api/auth/sessions":
        return user ? json([testSession]) : problem(401);
      case "GET /api/auth/link-notices":
        return user ? json([]) : problem(401);
      case "GET /api/privacy/account":
        return user ? json({ self_delete_enabled: true, export_expiry_hours: 24 }) : problem(401);
      case "GET /api/privacy/exports":
        return user ? json([]) : problem(401);
      default:
        return problem(404);
    }
  };
}

/** Default backend for component tests: set-up instance, an admin is signed in. */
export const defaultHandler: FetchHandler = backend();

/** Replaces the global `fetch` (the API client looks it up per request). */
export function mockFetch(handler: FetchHandler = defaultHandler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) =>
    handler(input instanceof Request ? input : new Request(input, init)),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

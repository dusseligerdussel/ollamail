import createClient, { type Middleware } from "openapi-fetch";

import { ApiError, isProblemDetails, parseRetryAfter } from "./errors";
import type { paths } from "./schema.gen";

/** All API requests go through the same origin; the proxy strips this prefix. */
export const API_BASE_PATH = "/api";

/**
 * Double-submit CSRF protection: the backend sets this cookie and expects its value in the
 * header on state-changing requests.
 */
export const CSRF_COOKIE = "ollamail_csrf";
export const CSRF_HEADER = "X-CSRF-Token";

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export const LOGIN_PATH = "/login";

export function readCookie(name: string): string | undefined {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(";")) {
    const cookie = part.trim();
    if (cookie.startsWith(prefix)) return decodeURIComponent(cookie.slice(prefix.length));
  }
  return undefined;
}

/** Full page navigation to the login page; remembers where the user was. */
export function redirectToLogin() {
  const { pathname, search, hash } = window.location;
  if (pathname === LOGIN_PATH) return;
  const params = new URLSearchParams({ redirect: `${pathname}${search}${hash}` });
  window.location.assign(`${LOGIN_PATH}?${params}`);
}

export interface ApiClientOptions {
  baseUrl?: string;
  /** Defaults to the global `fetch`, looked up per request (so tests can stub it). */
  fetch?: (request: Request) => Promise<Response>;
  /** Called for every 401 response. */
  onUnauthorized?: () => void;
}

export function createApiClient({
  baseUrl = `${window.location.origin}${API_BASE_PATH}`,
  fetch = (request) => globalThis.fetch(request),
  onUnauthorized = redirectToLogin,
}: ApiClientOptions = {}) {
  const client = createClient<paths>({ baseUrl, fetch, credentials: "include" });

  const middleware: Middleware = {
    onRequest({ request }) {
      if (!SAFE_METHODS.has(request.method)) {
        const token = readCookie(CSRF_COOKIE);
        if (token) request.headers.set(CSRF_HEADER, token);
      }
      return request;
    },
    onResponse({ response }) {
      if (response.status === 401) onUnauthorized();
      return response;
    },
  };
  client.use(middleware);
  return client;
}

/** The app-wide typed API client. Paths and payloads come from `schema.gen.ts`. */
export const api = createApiClient();

interface FetchResult<T> {
  data?: T;
  error?: unknown;
  response: Response;
}

/**
 * Resolves to the response data or throws an `ApiError`, so that TanStack Query's error
 * handling (see `query-client.ts`) applies:
 *
 * ```ts
 * queryFn: ({ signal }) => unwrap(api.GET("/healthz", { signal }))
 * ```
 */
export async function unwrap<T>(request: Promise<FetchResult<T>>): Promise<T> {
  let result: FetchResult<T>;
  try {
    result = await request;
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(0, undefined, { cause: error });
  }
  const { data, error, response } = result;
  if (!response.ok) {
    throw new ApiError(response.status, isProblemDetails(error) ? error : undefined, {
      retryAfter: parseRetryAfter(response.headers.get("Retry-After")),
    });
  }
  return data as T;
}

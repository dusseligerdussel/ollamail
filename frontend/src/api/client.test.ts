import { afterEach, describe, expect, it, vi } from "vitest";

import { json, mockFetch, problem } from "@/test/fetch";

import { CSRF_HEADER, createApiClient, readCookie, unwrap } from "./client";
import { ApiError } from "./errors";

function setCookie(value: string) {
  // biome-ignore lint/suspicious/noDocumentCookie: test setup for the CSRF cookie
  document.cookie = value;
}

afterEach(() => {
  setCookie("ollamail_csrf=; expires=Thu, 01 Jan 1970 00:00:00 GMT");
});

describe("createApiClient", () => {
  it("sends typed requests below /api with credentials", async () => {
    const fetchMock = mockFetch(() => json({ status: "ok" }));
    const api = createApiClient();

    const data = await unwrap(api.GET("/healthz"));

    expect(data.status).toBe("ok");
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(new URL(request.url).pathname).toBe("/api/healthz");
    expect(request.credentials).toBe("include");
  });

  it("adds the CSRF header to state-changing requests only", async () => {
    setCookie("ollamail_csrf=token%20value");
    const requests: Request[] = [];
    const api = createApiClient({
      fetch: async (request) => {
        requests.push(request);
        return json({});
      },
    });

    await api.GET("/healthz");
    // No mutating endpoint exists yet; the middleware does not depend on the path.
    await (api.POST as (path: string) => Promise<unknown>)("/anything");

    expect(requests[0]?.headers.get(CSRF_HEADER)).toBeNull();
    expect(requests[1]?.headers.get(CSRF_HEADER)).toBe("token value");
  });

  it("calls onUnauthorized for 401 responses", async () => {
    const onUnauthorized = vi.fn();
    const api = createApiClient({ fetch: async () => problem(401), onUnauthorized });

    await expect(unwrap(api.GET("/healthz"))).rejects.toMatchObject({ status: 401 });
    expect(onUnauthorized).toHaveBeenCalledOnce();
  });
});

describe("unwrap", () => {
  it("throws an ApiError with the Problem Details", async () => {
    const api = createApiClient({
      fetch: async () => problem(409, { detail: "changed", request_id: "req-1" }),
    });

    const error = await unwrap(api.GET("/healthz")).catch((e: unknown) => e);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, requestId: "req-1", isClientError: true });
    expect((error as ApiError).problem?.detail).toBe("changed");
  });

  it("ignores error bodies that are not Problem Details", async () => {
    const api = createApiClient({
      fetch: async () => new Response("Bad gateway", { status: 502 }),
    });

    const error = (await unwrap(api.GET("/healthz")).catch((e: unknown) => e)) as ApiError;

    expect(error.status).toBe(502);
    expect(error.problem).toBeUndefined();
  });

  it("turns network failures into status 0", async () => {
    const api = createApiClient({
      fetch: async () => {
        throw new TypeError("Failed to fetch");
      },
    });

    await expect(unwrap(api.GET("/healthz"))).rejects.toMatchObject({ status: 0 });
  });

  it("passes aborts through", async () => {
    const api = createApiClient({
      fetch: async () => {
        throw new DOMException("aborted", "AbortError");
      },
    });

    await expect(unwrap(api.GET("/healthz"))).rejects.toMatchObject({ name: "AbortError" });
  });
});

describe("readCookie", () => {
  it("finds a cookie among others", () => {
    setCookie("other=1");
    setCookie("ollamail_csrf=abc");
    expect(readCookie("ollamail_csrf")).toBe("abc");
    expect(readCookie("missing")).toBeUndefined();
  });
});

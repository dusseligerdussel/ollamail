import i18n from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import "@/i18n";

import {
  ApiError,
  describeApiError,
  isCsrfError,
  isLlmBusy,
  isProblemDetails,
  parseRetryAfter,
} from "./errors";

beforeAll(async () => {
  await i18n.changeLanguage("en");
});

describe("describeApiError", () => {
  it.each([
    [0, "The server is not reachable. Check your connection."],
    [403, "You do not have permission to do this."],
    [422, "Some entries are invalid."],
    [500, "A server error occurred. Please try again later."],
    [502, "A server error occurred. Please try again later."],
    [418, "Something went wrong."],
  ])("maps status %i to a localized message", (status, message) => {
    expect(describeApiError(new ApiError(status), i18n.t).title).toBe(message);
  });

  it("never shows the English server text and adds the request ID", () => {
    const error = new ApiError(404, {
      title: "Not Found",
      status: 404,
      detail: "internal detail",
      request_id: "abc123",
    });

    expect(describeApiError(error, i18n.t)).toEqual({
      title: "The requested item was not found.",
      description: "Request ID: abc123",
    });
  });

  it("explains a refusal because of too many parallel AI requests", () => {
    const busy = new ApiError(429, {
      title: "Too Many Requests",
      status: 429,
      error_code: "llm_busy",
    });

    expect(describeApiError(busy, i18n.t).title).toBe(
      "You already have several answers or drafts in progress. Wait for one to finish, then try again.",
    );
    expect(describeApiError(new ApiError(429), i18n.t).title).toBe(
      "Too many requests. Please wait a moment.",
    );
  });

  it("handles errors that are not API errors", () => {
    expect(describeApiError(new Error("boom"), i18n.t)).toEqual({
      title: "Something went wrong.",
    });
  });

  it("is translated", async () => {
    await i18n.changeLanguage("de");
    expect(describeApiError(new ApiError(0), i18n.t).title).toBe(
      "Der Server ist nicht erreichbar. Prüfe deine Verbindung.",
    );
    await i18n.changeLanguage("en");
  });
});

describe("isProblemDetails", () => {
  it("accepts problem objects only", () => {
    expect(isProblemDetails({ title: "Conflict", status: 409 })).toBe(true);
    expect(isProblemDetails("Bad gateway")).toBe(false);
    expect(isProblemDetails(null)).toBe(false);
    expect(isProblemDetails({ foo: 1 })).toBe(false);
  });
});

describe("isCsrfError", () => {
  it("recognises the backend's CSRF rejection only", () => {
    expect(isCsrfError(new ApiError(403, { status: 403, error_code: "csrf_failed" }))).toBe(true);
    expect(isCsrfError(new ApiError(403, { status: 403, detail: "invalid" }))).toBe(false);
    expect(isCsrfError(new ApiError(401, { status: 401, error_code: "csrf_failed" }))).toBe(false);
    expect(isCsrfError(new Error("csrf_failed"))).toBe(false);
  });
});

describe("parseRetryAfter", () => {
  it("reads seconds and HTTP dates, ignores anything else", () => {
    const now = Date.parse("2026-10-04T12:00:00Z");
    expect(parseRetryAfter("5", now)).toBe(5);
    expect(parseRetryAfter("Sun, 04 Oct 2026 12:00:30 GMT", now)).toBe(30);
    expect(parseRetryAfter("Sun, 04 Oct 2026 11:00:00 GMT", now)).toBe(0);
    expect(parseRetryAfter("soon", now)).toBeUndefined();
    expect(parseRetryAfter(null, now)).toBeUndefined();
  });
});

describe("isLlmBusy", () => {
  it("is only true for 429 llm_busy", () => {
    expect(isLlmBusy(new ApiError(429, { status: 429, error_code: "llm_busy" }))).toBe(true);
    expect(isLlmBusy(new ApiError(429))).toBe(false);
    expect(isLlmBusy(new Error("busy"))).toBe(false);
  });
});

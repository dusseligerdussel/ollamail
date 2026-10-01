import i18n from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import "@/i18n";

import { ApiError, describeApiError, isProblemDetails } from "./errors";

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

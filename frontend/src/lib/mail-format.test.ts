import { describe, expect, it, vi } from "vitest";

import { addressFull, addressName, formatListDate, formatSize } from "./mail-format";

describe("mail formatting", () => {
  const now = new Date("2026-10-02T15:00:00Z");

  it("shows the time today, day and month this year, else the full date", () => {
    expect(formatListDate("2026-10-02T09:30:00Z", "en-US", "UTC", now)).toBe("9:30 AM");
    expect(formatListDate("2026-09-28T09:30:00Z", "en-US", "UTC", now)).toBe("Sep 28");
    expect(formatListDate("2025-12-24T09:30:00Z", "en-US", "UTC", now)).toBe("Dec 24, 2025");
    // The user's time zone decides what "today" is.
    expect(formatListDate("2026-10-01T23:30:00Z", "de-DE", "Europe/Berlin", now)).toBe("01:30");
  });

  it("creates date formatters only once per locale and time zone (#111)", () => {
    const created = vi.spyOn(Intl, "DateTimeFormat");
    try {
      for (let day = 1; day <= 28; day++) {
        formatListDate(
          `2026-09-${String(day).padStart(2, "0")}T09:30:00Z`,
          "en-GB",
          "Asia/Tokyo",
          now,
        );
      }
      // Day and year comparison plus the day-and-month style; none of them per row.
      expect(created.mock.calls.length).toBeLessThanOrEqual(3);
      expect(formatListDate("2026-09-28T09:30:00Z", "en-GB", "Asia/Tokyo", now)).toBe("28 Sept");
    } finally {
      created.mockRestore();
    }
  });

  it("formats sizes and addresses", () => {
    expect(formatSize(512, "en")).toBe("512 byte");
    expect(formatSize(48_213, "en")).toBe("47.1 kB");
    expect(formatSize(5 * 1024 * 1024, "de")).toBe("5 MB");
    expect(addressName({ name: "Erika", address: "erika@example.org" })).toBe("Erika");
    expect(addressName({ name: null, address: "erika@example.org" })).toBe("erika@example.org");
    expect(addressName(null)).toBe("");
    expect(addressFull({ name: "Erika", address: "erika@example.org" })).toBe(
      "Erika <erika@example.org>",
    );
  });
});

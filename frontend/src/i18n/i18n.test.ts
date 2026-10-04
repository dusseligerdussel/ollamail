import { describe, expect, it } from "vitest";

import de from "./locales/de.json";
import en from "./locales/en.json";

function keysOf(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) return [prefix];
  return Object.entries(value).flatMap(([key, child]) =>
    keysOf(child, prefix ? `${prefix}.${key}` : key),
  );
}

describe("i18n resources", () => {
  it("defines the same keys for every language", () => {
    expect(keysOf(de).sort()).toEqual(keysOf(en).sort());
  });
});

import { describe, expect, it } from "vitest";

import { resources } from "./index";

function keysOf(value: unknown, prefix = ""): string[] {
  if (typeof value !== "object" || value === null) return [prefix];
  return Object.entries(value).flatMap(([key, child]) =>
    keysOf(child, prefix ? `${prefix}.${key}` : key),
  );
}

describe("i18n resources", () => {
  it("defines the same keys for every language", () => {
    const en = keysOf(resources.en.translation).sort();
    const de = keysOf(resources.de.translation).sort();
    expect(de).toEqual(en);
  });
});

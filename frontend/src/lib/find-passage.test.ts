import { describe, expect, it } from "vitest";

import { findPassage, findPassageRange } from "./find-passage";

const TEXT = `Hallo Erika,

die Rechnung für September liegt bei.
Sie ist zahlbar innerhalb von 14 Tagen nach Erhalt.

Viele Grüße
Jonas`;

describe("findPassage", () => {
  it("finds a passage regardless of whitespace, case and ellipses", () => {
    const found = findPassage(
      TEXT,
      "… DIE Rechnung für September liegt bei. Sie ist zahlbar innerhalb von 14 Tagen …",
    );
    expect(found).toBeDefined();
    const [start, end] = found ?? [0, 0];
    expect(TEXT.slice(start, end)).toBe(
      "die Rechnung für September liegt bei.\nSie ist zahlbar innerhalb von 14 Tagen",
    );
  });

  it("finds a passage whose start differs (chunk boundary)", () => {
    const found = findPassage(
      TEXT,
      "xyz uvw liegt bei. Sie ist zahlbar innerhalb von 14 Tagen nach",
    );
    expect(found && TEXT.slice(...found)).toBe(
      "liegt bei.\nSie ist zahlbar innerhalb von 14 Tagen nach",
    );
  });

  it("returns nothing for unknown text", () => {
    expect(findPassage(TEXT, "völlig andere Worte stehen hier drin")).toBeUndefined();
    expect(findPassage(TEXT, "  ")).toBeUndefined();
  });
});

describe("findPassageRange", () => {
  it("spans text nodes of several elements", () => {
    const root = document.createElement("div");
    root.innerHTML =
      "<p>Hallo Erika,</p><p>die <b>Rechnung</b> für September</p><p>liegt bei.</p><style>p{}</style>";
    const range = findPassageRange(root, "die Rechnung für September liegt bei.");
    expect(range?.toString()).toBe("die Rechnung für Septemberliegt bei.");
    expect(range?.startContainer.textContent).toBe("die ");
  });
});

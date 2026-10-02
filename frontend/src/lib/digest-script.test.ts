import { describe, expect, it } from "vitest";

import { parseScript } from "./digest-script";

describe("parseScript", () => {
  it("splits headings and paragraphs and extracts references", () => {
    const script =
      "# Digest for Friday\n\nGood morning. 3 new mails.\n\nThe invoice is due Monday [1]. " +
      "Two meetings moved [2, 3].\nSee you.\n";

    expect(parseScript(script)).toEqual([
      { kind: "heading", text: "Digest for Friday" },
      { kind: "paragraph", parts: [{ text: "Good morning. 3 new mails." }] },
      {
        kind: "paragraph",
        parts: [
          { text: "The invoice is due Monday" },
          { refs: [1] },
          { text: ". Two meetings moved" },
          { refs: [2, 3] },
          { text: ". See you." },
        ],
      },
    ]);
  });

  it("keeps markup as plain text", () => {
    expect(parseScript("<b>bold</b> [x]")).toEqual([
      { kind: "paragraph", parts: [{ text: "<b>bold</b> [x]" }] },
    ]);
  });
});

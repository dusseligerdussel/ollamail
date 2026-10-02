import { describe, expect, it } from "vitest";

import {
  answerSegments,
  citedNumbers,
  highlightSegments,
  isQuestion,
  parseSourceHeading,
  queryTerms,
} from "./search-text";

describe("queryTerms", () => {
  it("keeps words, drops operators, short words and duplicates, longest first", () => {
    expect(queryTerms('Rechnung OR "Angebot" -x rechnung')).toEqual(["rechnung", "angebot"]);
    expect(queryTerms("Bis wann muss die Rechnung bezahlt werden?")).toEqual([
      "rechnung",
      "bezahlt",
    ]);
  });
});

describe("highlightSegments", () => {
  it("marks words starting with a term, case-insensitively", () => {
    expect(highlightSegments("Zwei Rechnungen, eine Vorrechnung", ["rechnung"])).toEqual([
      { text: "Zwei ", match: false },
      { text: "Rechnungen", match: true },
      { text: ", eine Vorrechnung", match: false },
    ]);
  });

  it("treats terms as text, not as patterns", () => {
    expect(highlightSegments("a+b (c)", ["b"])).toEqual([
      { text: "a+", match: false },
      { text: "b", match: true },
      { text: " (c)", match: false },
    ]);
    expect(highlightSegments("text", [])).toEqual([{ text: "text", match: false }]);
  });
});

describe("answer citations", () => {
  it("splits markers from text", () => {
    expect(answerSegments("Gate B12 [1], at 09:40 [1][2].")).toEqual([
      { text: "Gate B12 " },
      { citation: 1 },
      { text: ", at 09:40 " },
      { citation: 1 },
      { citation: 2 },
      { text: "." },
    ]);
    expect(citedNumbers("a [2] b [1] c [2]")).toEqual([2, 1]);
  });
});

describe("isQuestion", () => {
  it.each([
    ["Wann kommt die Rechnung?", true],
    ["wann ist das Teammeeting", true],
    ["What did Lena say about the offer", true],
    ["Rechnung September", false],
    ["wie", false],
    ["Angebot Nordlicht", false],
  ])("%s → %s", (query, expected) => {
    expect(isQuestion(query)).toBe(expected);
  });
});

describe("parseSourceHeading", () => {
  it("reads sender name, date, subject and attachment", () => {
    expect(
      parseSourceHeading(
        "From: Lena Muster <lena@example.com>\nDate: 2026-09-14\nSubject: Angebot\nAttachment: a.pdf",
      ),
    ).toEqual({
      sender: "Lena Muster",
      date: "2026-09-14",
      subject: "Angebot",
      attachment: "a.pdf",
    });
    expect(parseSourceHeading("From: it@example.org")).toEqual({ sender: "it@example.org" });
  });
});

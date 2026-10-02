/** Text helpers of the search page: query terms, highlighting, citations, question detection. */

const WORD = /[\p{L}\p{N}]{2,}/gu;
// Search operators (websearch_to_tsquery) and frequent short words of questions, which
// would mark half of every excerpt.
const SKIPPED = new Set(
  (
    "or and not the a an of to in on at for is are was were be do does did what when where " +
    "who why how which my me i you it this that with from by about " +
    "der die das den dem des ein eine einen einem einer und oder nicht ist sind war wird " +
    "werden wurde muss soll kann im in am an auf aus bei bis mit von vom zu zum zur für " +
    "über wer wen wem was wann wo wie warum welche welcher welches ich mir mich du dir es " +
    "er sie wir ihr uns mein meine dein deine hat habe hatte gibt es"
  ).split(" "),
);

/** Words of a query worth highlighting, longest first (so longer matches win). */
export function queryTerms(query: string): string[] {
  const terms = new Set<string>();
  for (const match of query.toLocaleLowerCase().matchAll(WORD)) {
    if (!SKIPPED.has(match[0])) terms.add(match[0]);
  }
  return [...terms].sort((a, b) => b.length - a.length);
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export interface TextSegment {
  text: string;
  match: boolean;
}

/**
 * Splits `text` into plain and matching parts. A term matches at the start of a word and
 * extends to the end of that word, so "rechnung" also marks "Rechnungen".
 */
export function highlightSegments(text: string, terms: readonly string[]): TextSegment[] {
  if (terms.length === 0 || !text) return text ? [{ text, match: false }] : [];
  const pattern = new RegExp(
    `(?<![\\p{L}\\p{N}])(?:${terms.map(escapeRegExp).join("|")})[\\p{L}\\p{N}]*`,
    "giu",
  );
  const segments: TextSegment[] = [];
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const start = match.index;
    if (start > last) segments.push({ text: text.slice(last, start), match: false });
    segments.push({ text: match[0], match: true });
    last = start + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last), match: false });
  return segments;
}

export type AnswerSegment = { text: string } | { citation: number };

const CITATION = /\[(\d{1,3})\]/g;

/** Splits an answer into text and citation markers `[n]`. */
export function answerSegments(text: string): AnswerSegment[] {
  const segments: AnswerSegment[] = [];
  let last = 0;
  for (const match of text.matchAll(CITATION)) {
    if (match.index > last) segments.push({ text: text.slice(last, match.index) });
    segments.push({ citation: Number(match[1]) });
    last = match.index + match[0].length;
  }
  if (last < text.length) segments.push({ text: text.slice(last) });
  return segments;
}

/** Numbers cited in `text`, in order of first citation. */
export function citedNumbers(text: string): number[] {
  const numbers = new Set<number>();
  for (const match of text.matchAll(CITATION)) numbers.add(Number(match[1]));
  return [...numbers];
}

const QUESTION_WORDS = new Set([
  // German
  "wer",
  "wen",
  "wem",
  "wessen",
  "was",
  "wann",
  "wo",
  "wohin",
  "woher",
  "warum",
  "weshalb",
  "wieso",
  "wie",
  "welche",
  "welcher",
  "welches",
  "welchen",
  "welchem",
  "gibt",
  "habe",
  "hat",
  "hatte",
  "ist",
  "sind",
  "kann",
  "soll",
  "muss",
  "fasse",
  "zeige",
  // English
  "who",
  "whom",
  "whose",
  "what",
  "when",
  "where",
  "why",
  "how",
  "which",
  "did",
  "do",
  "does",
  "is",
  "are",
  "was",
  "were",
  "have",
  "has",
  "can",
  "should",
  "summarize",
  "summarise",
  "list",
]);

/**
 * Whether a query reads as a question (answered with sources) rather than keywords (hit
 * list): it ends with "?" or starts with a question word and has at least three words.
 */
export function isQuestion(query: string): boolean {
  const text = query.trim();
  if (text.endsWith("?")) return true;
  const words = text.toLocaleLowerCase().split(/\s+/);
  return words.length >= 3 && QUESTION_WORDS.has(words[0] ?? "");
}

export interface SourceHeading {
  sender?: string;
  date?: string;
  subject?: string;
  attachment?: string;
}

/** Fields of a source heading (`From: …`, `Date: …`, `Subject: …`, `Attachment: …`). */
export function parseSourceHeading(heading: string): SourceHeading {
  const result: SourceHeading = {};
  for (const line of heading.split("\n")) {
    const colon = line.indexOf(": ");
    if (colon < 0) continue;
    const value = line.slice(colon + 2).trim();
    switch (line.slice(0, colon)) {
      case "From":
        // "Name <address>" → "Name".
        result.sender = value.replace(/\s*<[^>]*>$/, "") || value;
        break;
      case "Date":
        result.date = value;
        break;
      case "Subject":
        result.subject = value;
        break;
      case "Attachment":
        result.attachment = value;
        break;
    }
  }
  return result;
}

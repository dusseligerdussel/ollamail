/** A piece of a paragraph: text or the mails (`[n]`, `[n, m]`) a sentence is based on. */
export type ScriptPart = { text: string } | { refs: number[] };

export type ScriptBlock =
  | { kind: "heading"; text: string }
  | { kind: "paragraph"; parts: ScriptPart[] };

const HEADING = /^#{1,6}\s+(.*)$/;
const REFERENCE = /\s*\[(\d+(?:\s*,\s*\d+)*)\]/g;

function parts(text: string): ScriptPart[] {
  const result: ScriptPart[] = [];
  let last = 0;
  for (const match of text.matchAll(REFERENCE)) {
    if (match.index > last) result.push({ text: text.slice(last, match.index) });
    const refs = (match[1] ?? "").split(",").map((ref) => Number(ref.trim()));
    result.push({ refs });
    last = match.index + match[0].length;
  }
  if (last < text.length) result.push({ text: text.slice(last) });
  return result;
}

/**
 * Splits the digest script (Markdown with `[n]` references, see `backend/app/digest/script.py`)
 * into headings and paragraphs. Only this subset is interpreted; everything else stays text,
 * so nothing from the script is ever rendered as HTML.
 */
export function parseScript(script: string): ScriptBlock[] {
  const blocks: ScriptBlock[] = [];
  for (const chunk of script.split(/\n\s*\n/)) {
    const lines = chunk
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    let paragraph: string[] = [];
    const flush = () => {
      if (paragraph.length) blocks.push({ kind: "paragraph", parts: parts(paragraph.join(" ")) });
      paragraph = [];
    };
    for (const line of lines) {
      const heading = HEADING.exec(line);
      if (heading) {
        flush();
        blocks.push({ kind: "heading", text: heading[1] ?? "" });
      } else {
        paragraph.push(line);
      }
    }
    flush();
  }
  return blocks;
}

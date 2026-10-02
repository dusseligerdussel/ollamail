/**
 * Finds a passage (a search excerpt or a cited snippet) in the text of a mail, so the mail
 * can be opened at the relevant place. Whitespace and case are ignored; the passage may be
 * shortened with "…" and may differ at its edges (chunk boundaries), so it is located by
 * a few words from its start and, if possible, its end.
 */

interface Normalized {
  text: string;
  /** Index in the original text of each character of `text`. */
  map: number[];
}

function normalize(text: string): Normalized {
  let out = "";
  const map: number[] = [];
  let space = true;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index] ?? "";
    if (/\s/.test(char)) {
      if (!space) {
        out += " ";
        map.push(index);
        space = true;
      }
      continue;
    }
    out += char.toLocaleLowerCase();
    map.push(index);
    space = false;
  }
  return { text: out, map };
}

const PROBE_WORDS = 6;

function words(passage: string) {
  return normalize(passage.replaceAll("…", " ")).text.split(" ").filter(Boolean);
}

/** `[start, end)` of the passage in `text`, or `undefined` if it does not occur. */
export function findPassage(text: string, passage: string): [number, number] | undefined {
  const parts = words(passage);
  if (parts.length === 0) return undefined;
  const haystack = normalize(text);
  const size = Math.min(PROBE_WORDS, parts.length);

  // The first window of words that occurs marks the start.
  let start = -1;
  let firstWindow = 0;
  for (let offset = 0; offset + size <= parts.length; offset += 1) {
    const probe = parts.slice(offset, offset + size).join(" ");
    start = haystack.text.indexOf(probe);
    if (start >= 0) {
      firstWindow = offset;
      break;
    }
  }
  if (start < 0) return undefined;
  let end = start + parts.slice(firstWindow, firstWindow + size).join(" ").length;

  // Extend to the last window of words found after the start, within the passage length.
  const limit = start + Math.ceil(parts.join(" ").length * 1.5);
  for (let offset = parts.length - size; offset > firstWindow; offset -= 1) {
    const probe = parts.slice(offset, offset + size).join(" ");
    const position = haystack.text.indexOf(probe, start);
    if (position >= 0 && position + probe.length <= limit) {
      end = position + probe.length;
      break;
    }
  }
  const from = haystack.map[start] ?? 0;
  const to = (haystack.map[end - 1] ?? from) + 1;
  return [from, to];
}

/** A DOM range of the passage within `root`, built from its text nodes. */
export function findPassageRange(root: Node, passage: string): Range | undefined {
  const document = root.ownerDocument ?? (root as Document);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  const starts: number[] = [];
  let text = "";
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const parent = node.parentElement?.tagName;
    if (parent === "STYLE" || parent === "SCRIPT" || parent === "TITLE") continue;
    nodes.push(node as Text);
    starts.push(text.length);
    // A space between nodes, so words of neighbouring blocks do not run together.
    text += `${node.nodeValue ?? ""} `;
  }
  const found = findPassage(text, passage);
  if (!found) return undefined;
  const locate = (position: number) => {
    let index = starts.length - 1;
    while (index > 0 && (starts[index] ?? 0) > position) index -= 1;
    const node = nodes[index] as Text;
    const offset = Math.min(position - (starts[index] ?? 0), node.length);
    return { node, offset };
  };
  const from = locate(found[0]);
  const to = locate(found[1]);
  const range = document.createRange();
  range.setStart(from.node, from.offset);
  range.setEnd(to.node, to.offset);
  return range;
}

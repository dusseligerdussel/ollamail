/**
 * Test double for `window.matchMedia` that evaluates `min-width` / `max-width` against a
 * configurable viewport width and `prefers-color-scheme` against a configurable preference.
 */
type Listener = (event: MediaQueryListEvent) => void;

let viewportWidth = 1440;
let prefersDark = false;
const lists = new Set<{ query: string; listeners: Set<Listener>; matches: boolean }>();

function evaluate(query: string): boolean {
  return query.split(/\s+and\s+/).every((part) => {
    const min = /min-width:\s*(\d+)px/.exec(part);
    if (min) return viewportWidth >= Number(min[1]);
    const max = /max-width:\s*(\d+)px/.exec(part);
    if (max) return viewportWidth <= Number(max[1]);
    const scheme = /prefers-color-scheme:\s*(dark|light)/.exec(part);
    if (scheme) return (scheme[1] === "dark") === prefersDark;
    return false;
  });
}

function notify() {
  for (const list of lists) {
    const matches = evaluate(list.query);
    if (matches === list.matches) continue;
    list.matches = matches;
    for (const listener of list.listeners) {
      listener({ matches, media: list.query } as MediaQueryListEvent);
    }
  }
}

window.matchMedia = (query: string) => {
  const entry = { query, listeners: new Set<Listener>(), matches: evaluate(query) };
  lists.add(entry);
  return {
    media: query,
    get matches() {
      return evaluate(query);
    },
    onchange: null,
    addEventListener: (_: string, listener: Listener) => entry.listeners.add(listener),
    removeEventListener: (_: string, listener: Listener) => entry.listeners.delete(listener),
    addListener: (listener: Listener) => entry.listeners.add(listener),
    removeListener: (listener: Listener) => entry.listeners.delete(listener),
    dispatchEvent: () => true,
  } as unknown as MediaQueryList;
};

export function setViewportWidth(width: number) {
  viewportWidth = width;
  notify();
}

export function setPrefersDark(value: boolean) {
  prefersDark = value;
  notify();
}

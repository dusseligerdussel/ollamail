import { describe, expect, it, vi } from "vitest";

import { createRegistry } from "./registry";
import {
  createShortcutDispatcher,
  formatKeys,
  matchesCombo,
  parseKeys,
  type Shortcut,
} from "./shortcuts";

function key(key: string, init: KeyboardEventInit = {}, target?: HTMLElement) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...init });
  if (target) Object.defineProperty(event, "target", { value: target });
  return event;
}

function setup(options: { now?: () => number } = {}) {
  const registry = createRegistry<Shortcut>();
  const dispatch = createShortcutDispatcher(registry, { isMac: false, ...options });
  const add = (keys: string, extra: Partial<Shortcut> = {}) => {
    const handler = vi.fn();
    registry.register({ id: keys, keys, description: keys, group: "general", handler, ...extra });
    return handler;
  };
  return { registry, dispatch, add };
}

describe("parseKeys", () => {
  it("parses combos and sequences", () => {
    expect(parseKeys("mod+k")).toEqual([{ key: "k", mod: true, alt: false, shift: false }]);
    expect(parseKeys("g i").map((combo) => combo.key)).toEqual(["g", "i"]);
    expect(parseKeys("shift+Esc")).toEqual([
      { key: "escape", mod: false, alt: false, shift: true },
    ]);
  });
});

describe("matchesCombo", () => {
  const [modK] = parseKeys("mod+k");
  const [j] = parseKeys("j");
  const [question] = parseKeys("?");

  it("maps mod to Ctrl on other platforms and ⌘ on macOS", () => {
    if (!modK) throw new Error("parse failed");
    expect(matchesCombo(modK, key("k", { ctrlKey: true }), false)).toBe(true);
    expect(matchesCombo(modK, key("k", { metaKey: true }), false)).toBe(false);
    expect(matchesCombo(modK, key("k", { metaKey: true }), true)).toBe(true);
  });

  it("distinguishes shifted letters but not symbols", () => {
    if (!j || !question) throw new Error("parse failed");
    expect(matchesCombo(j, key("j"), false)).toBe(true);
    expect(matchesCombo(j, key("J", { shiftKey: true }), false)).toBe(false);
    expect(matchesCombo(question, key("?", { shiftKey: true }), false)).toBe(true);
  });
});

describe("shortcut dispatcher", () => {
  it("runs the matching shortcut and prevents the default action", () => {
    const { dispatch, add } = setup();
    const next = add("j");
    const previous = add("k");
    const event = key("j");
    dispatch(event);
    expect(next).toHaveBeenCalledOnce();
    expect(previous).not.toHaveBeenCalled();
    expect(event.defaultPrevented).toBe(true);
  });

  it("ignores keys typed into text fields unless the shortcut allows it", () => {
    const { dispatch, add } = setup();
    const next = add("j");
    const palette = add("mod+k", { allowInInput: true });
    const input = document.createElement("input");
    dispatch(key("j", {}, input));
    dispatch(key("k", { ctrlKey: true }, input));
    expect(next).not.toHaveBeenCalled();
    expect(palette).toHaveBeenCalledOnce();
  });

  it("supports sequences within the timeout", () => {
    let time = 0;
    const { dispatch, add } = setup({ now: () => time });
    const goInbox = add("g i");
    const single = add("i");

    dispatch(key("g"));
    time = 500;
    dispatch(key("i"));
    expect(goInbox).toHaveBeenCalledOnce();
    expect(single).not.toHaveBeenCalled();

    dispatch(key("g"));
    time = 2000;
    dispatch(key("i"));
    expect(goInbox).toHaveBeenCalledOnce();
    expect(single).toHaveBeenCalledOnce();
  });

  it("uses the latest registration and stops after unregistering", () => {
    const { registry, dispatch } = setup();
    const first = vi.fn();
    const second = vi.fn();
    const base = { id: "list.next", keys: "j", description: "", group: "list" } as const;
    const unregisterFirst = registry.register({ ...base, handler: first });
    const unregisterSecond = registry.register({ ...base, handler: second });

    // Unregistering a replaced shortcut must not remove its replacement.
    unregisterFirst();
    dispatch(key("j"));
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledOnce();

    unregisterSecond();
    dispatch(key("j"));
    expect(second).toHaveBeenCalledOnce();
    expect(registry.getSnapshot()).toEqual([]);
  });
});

describe("formatKeys", () => {
  it("uses platform specific labels", () => {
    expect(formatKeys("mod+k", true)).toEqual([["⌘", "K"]]);
    expect(formatKeys("mod+k", false)).toEqual([["Ctrl", "K"]]);
    expect(formatKeys("g i", false)).toEqual([["G"], ["I"]]);
  });
});

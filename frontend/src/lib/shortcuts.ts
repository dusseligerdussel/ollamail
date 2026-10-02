import type { Registry } from "./registry";

export const shortcutGroups = ["general", "navigation", "list"] as const;
export type ShortcutGroup = (typeof shortcutGroups)[number];

export interface Shortcut {
  /** Unique id; registering the same id again replaces the previous shortcut. */
  id: string;
  /**
   * Key binding. Combos join modifiers with `+` (`mod+k`, `shift+e`), sequences separate combos
   * with a space (`g i`). `mod` is ⌘ on macOS and Ctrl elsewhere.
   */
  keys: string;
  /** Translated description for the shortcut overview. */
  description: string;
  group: ShortcutGroup;
  handler: (event: KeyboardEvent) => void;
  /** Also fire while focus is in a text field or dialog (e.g. for `mod+k`). */
  allowInInput?: boolean;
  /** Do not list the shortcut in the overview. */
  hidden?: boolean;
}

export interface KeyCombo {
  key: string;
  mod: boolean;
  alt: boolean;
  shift: boolean;
}

const keyAliases: Record<string, string> = {
  esc: "escape",
  return: "enter",
  space: " ",
  up: "arrowup",
  down: "arrowdown",
  left: "arrowleft",
  right: "arrowright",
};

export function parseKeys(keys: string): KeyCombo[] {
  return keys
    .trim()
    .split(/\s+/)
    .map((combo) => {
      // A trailing "+" is the plus key itself (e.g. "shift++").
      const parts = combo.endsWith("++")
        ? [...combo.slice(0, -2).split("+"), "+"]
        : combo.split("+");
      const rawKey = (parts.pop() ?? "").toLowerCase();
      const modifiers = new Set(parts.map((part) => part.toLowerCase()));
      return {
        key: keyAliases[rawKey] ?? rawKey,
        mod: modifiers.has("mod"),
        alt: modifiers.has("alt"),
        shift: modifiers.has("shift"),
      };
    });
}

export function isMacPlatform(): boolean {
  return typeof navigator !== "undefined" && /Mac|iPhone|iPad|iPod/i.test(navigator.platform);
}

/** Symbols such as `?` already imply Shift on most layouts, so Shift is ignored for them. */
function shiftIsImplicit(key: string) {
  return key.length === 1 && !/[a-z0-9]/.test(key);
}

export function matchesCombo(combo: KeyCombo, event: KeyboardEvent, isMac: boolean): boolean {
  const modPressed = isMac ? event.metaKey : event.ctrlKey;
  const otherModPressed = isMac ? event.ctrlKey : event.metaKey;
  if (event.key.toLowerCase() !== combo.key) return false;
  if (modPressed !== combo.mod || otherModPressed) return false;
  if (event.altKey !== combo.alt) return false;
  return shiftIsImplicit(combo.key) || event.shiftKey === combo.shift;
}

const modifierKeys = new Set(["Shift", "Control", "Alt", "Meta"]);

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  return ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName);
}

const activationKeys = new Set(["enter", " "]);
const interactiveSelector =
  'a[href], button, summary, [role="button"], [role="link"], [role="checkbox"], [role="switch"], [role="radio"], [role="tab"], [role="menuitem"], [role="option"]';

/** Enter and Space on a focused control activate it; shortcuts must not take them away. */
function isActivation(shortcut: Shortcut, event: KeyboardEvent): boolean {
  if (event.ctrlKey || event.metaKey || event.altKey) return false;
  const [first, ...rest] = parseKeys(shortcut.keys);
  return (
    rest.length === 0 &&
    !!first &&
    activationKeys.has(first.key) &&
    event.target instanceof Element &&
    event.target.closest(interactiveSelector) !== null
  );
}

function isInsideDialog(target: EventTarget | null): boolean {
  return (
    target instanceof Element && target.closest('[role="dialog"], [role="alertdialog"]') !== null
  );
}

interface DispatcherOptions {
  isMac?: boolean;
  /** Maximum delay between the keys of a sequence such as `g i`. */
  sequenceTimeoutMs?: number;
  now?: () => number;
}

/** Creates a `keydown` listener that runs the matching shortcut from the registry. */
export function createShortcutDispatcher(
  registry: Pick<Registry<Shortcut>, "getSnapshot">,
  {
    isMac = isMacPlatform(),
    sequenceTimeoutMs = 1000,
    now = () => Date.now(),
  }: DispatcherOptions = {},
) {
  let pending: { event: KeyboardEvent; at: number } | null = null;

  return (event: KeyboardEvent) => {
    if (event.defaultPrevented || event.isComposing || modifierKeys.has(event.key)) return;

    const restricted = isEditableTarget(event.target) || isInsideDialog(event.target);
    const candidates = registry
      .getSnapshot()
      .filter((shortcut) => !restricted || shortcut.allowInInput)
      .filter((shortcut) => !isActivation(shortcut, event));

    const previous = pending && now() - pending.at <= sequenceTimeoutMs ? pending.event : null;
    pending = null;

    const fire = (shortcut: Shortcut) => {
      event.preventDefault();
      shortcut.handler(event);
    };

    if (previous) {
      for (const shortcut of candidates) {
        const [first, second, ...rest] = parseKeys(shortcut.keys);
        if (first && second && rest.length === 0 && matchesCombo(first, previous, isMac)) {
          if (matchesCombo(second, event, isMac)) return fire(shortcut);
        }
      }
    }

    let startsSequence = false;
    for (const shortcut of candidates) {
      const combos = parseKeys(shortcut.keys);
      const [first] = combos;
      if (!first || !matchesCombo(first, event, isMac)) continue;
      if (combos.length === 1) return fire(shortcut);
      startsSequence = true;
    }
    if (startsSequence) pending = { event, at: now() };
  };
}

const macSymbols: Record<string, string> = { mod: "⌘", shift: "⇧", alt: "⌥" };
const otherLabels: Record<string, string> = { mod: "Ctrl", shift: "Shift", alt: "Alt" };
const keyLabels: Record<string, string> = {
  escape: "Esc",
  enter: "↵",
  " ": "Space",
  arrowup: "↑",
  arrowdown: "↓",
  arrowleft: "←",
  arrowright: "→",
};

/** Splits a binding into display labels: one array of keys per step of a sequence. */
export function formatKeys(keys: string, isMac = isMacPlatform()): string[][] {
  const labels = isMac ? macSymbols : otherLabels;
  return parseKeys(keys).map((combo) => {
    const parts: string[] = [];
    if (combo.mod) parts.push(labels.mod ?? "");
    if (combo.alt) parts.push(labels.alt ?? "");
    if (combo.shift) parts.push(labels.shift ?? "");
    parts.push(keyLabels[combo.key] ?? combo.key.toUpperCase());
    return parts;
  });
}

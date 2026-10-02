import { readdirSync, readFileSync } from "node:fs";
import { resolve as resolvePath } from "node:path";

import { describe, expect, it } from "vitest";

// Read from disk (tests run in `frontend/`): the test setup does not process CSS imports.
const css = readFileSync(resolvePath("src/index.css"), "utf8");

// Contrast of the design tokens in both themes (WCAG 1.4.3 text 4.5:1, 1.4.11 controls 3:1;
// #123). axe checks rendered text; this also covers combinations a page may not show yet.

function tokens(selector: string): Record<string, string> {
  const start = css.indexOf(`${selector} {`);
  const body = css.slice(start, css.indexOf("\n}", start));
  return Object.fromEntries(
    [...body.matchAll(/--([\w-]+):\s*([^;]+);/g)].map(([, name, value]) => [name, value?.trim()]),
  );
}

const light = tokens(":root");
const themes = { light, dark: { ...light, ...tokens(".dark") } };

function resolve(theme: Record<string, string>, name: string): string {
  let value = theme[name];
  while (value?.startsWith("var(--")) value = theme[value.slice(6, -1)];
  if (!value) throw new Error(`unknown token --${name}`);
  return value;
}

/** Gamma-encoded sRGB channels (0–1) of an opaque `oklch(L C H)` colour, clipped to the gamut. */
function srgb(color: string): number[] {
  const match = color.match(/^oklch\(([\d.]+) ([\d.]+) ([\d.]+)\)$/);
  if (!match) throw new Error(`not an opaque oklch() colour: ${color}`);
  const [lightness, chroma, hue] = match.slice(1).map(Number) as [number, number, number];
  const a = chroma * Math.cos((hue * Math.PI) / 180);
  const b = chroma * Math.sin((hue * Math.PI) / 180);
  const l = (lightness + 0.3963377774 * a + 0.2158037573 * b) ** 3;
  const m = (lightness - 0.1055613458 * a - 0.0638541728 * b) ** 3;
  const s = (lightness - 0.0894841775 * a - 1.291485548 * b) ** 3;
  return [
    4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
    -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
    -0.0041960863 * l - 0.7034186147 * m + 1.707614701 * s,
  ].map((linear) => {
    const channel = Math.min(1, Math.max(0, linear));
    return channel <= 0.0031308 ? 12.92 * channel : 1.055 * channel ** (1 / 2.4) - 0.055;
  });
}

function luminance(rgb: number[]): number {
  const [red, green, blue] = rgb.map((channel) =>
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
  ) as [number, number, number];
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue;
}

function ratio(x: number[], y: number[]) {
  const [a, b] = [luminance(x), luminance(y)];
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

function contrast(theme: Record<string, string>, foreground: string, background: string) {
  return ratio(srgb(resolve(theme, foreground)), srgb(resolve(theme, background)));
}

const surfaces = ["background", "card", "popover", "muted", "accent", "sidebar"];

/** [foreground, background, minimum ratio] */
const pairs: [string, string, number][] = [
  ["foreground", "background", 4.5],
  ["card-foreground", "card", 4.5],
  ["popover-foreground", "popover", 4.5],
  ["primary-foreground", "primary", 4.5],
  ["secondary-foreground", "secondary", 4.5],
  ["accent-foreground", "accent", 4.5],
  ["sidebar-foreground", "sidebar", 4.5],
  ["sidebar-accent-foreground", "sidebar-accent", 4.5],
  ["sidebar-primary-foreground", "sidebar-primary", 4.5],
  // Secondary text and error messages appear on every surface.
  ...surfaces.map((surface): [string, string, number] => ["muted-foreground", surface, 4.5]),
  ...surfaces.map((surface): [string, string, number] => ["destructive", surface, 4.5]),
  // Non-text: focus ring, unread dot / active marker, boundaries of form controls.
  ...surfaces.map((surface): [string, string, number] => ["ring", surface, 3]),
  ...surfaces.map((surface): [string, string, number] => ["brand", surface, 3]),
  ...surfaces.map((surface): [string, string, number] => ["input", surface, 3]),
];

describe.each(Object.entries(themes))("design tokens (%s)", (_, theme) => {
  it.each(pairs)("%s on %s has at least %d:1", (foreground, background, minimum) => {
    expect(contrast(theme, foreground, background)).toBeGreaterThanOrEqual(minimum);
  });
});

// Focus rings are drawn at 80 % opacity (`ring-ring/80`) and must still reach 3:1.
const FOCUS_RING_ALPHA = 0.8;

describe.each(Object.entries(themes))("focus ring (%s)", (_, theme) => {
  it.each(surfaces)("has at least 3:1 on %s", (surface) => {
    const ring = srgb(resolve(theme, "ring"));
    const background = srgb(resolve(theme, surface));
    const blended = ring.map(
      (channel, index) =>
        FOCUS_RING_ALPHA * channel + (1 - FOCUS_RING_ALPHA) * (background[index] ?? 0),
    );
    expect(ratio(blended, background)).toBeGreaterThanOrEqual(3);
  });
});

it("no component draws the focus ring fainter than that", () => {
  const faint = readdirSync(resolvePath("src"), { recursive: true, encoding: "utf8" })
    .filter((file) => file.endsWith(".tsx"))
    .flatMap((file) =>
      [...readFileSync(resolvePath("src", file), "utf8").matchAll(/(?:ring|outline)-ring\/(\d+)/g)]
        .filter(([, alpha]) => Number(alpha) < FOCUS_RING_ALPHA * 100)
        .map(([match]) => `${file}: ${match}`),
    );
  expect(faint).toEqual([]);
});

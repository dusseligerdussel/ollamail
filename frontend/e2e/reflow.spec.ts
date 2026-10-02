import { expect, type Page, test } from "@playwright/test";

import { mockState, openPage, routes } from "./app-states";

// Reflow, zoom, text spacing and reduced motion for every route (#123, WCAG 1.4.4, 1.4.10,
// 1.4.12, 2.3.3). Runs without a backend: the API is mocked in the browser.

/** Horizontal overflow of the page in CSS px (0: nothing to scroll sideways). */
function overflow(page: Page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

/** Elements whose text is cut off without a way to read it (no `title`, no ellipsis). */
function clippedText(page: Page) {
  return page.evaluate(() =>
    [...document.querySelectorAll<HTMLElement>("main *")]
      .filter((element) => {
        if (!element.checkVisibility() || element.childElementCount > 0) return false;
        if (!element.textContent?.trim()) return false;
        const style = getComputedStyle(element);
        if (style.textOverflow === "ellipsis" || element.closest("[title]")) return false;
        // Visually hidden text for screen readers (`sr-only`) is 1 px wide on purpose.
        if (element.getBoundingClientRect().width <= 1) return false;
        const hidden = style.overflowX === "hidden" || style.overflowX === "clip";
        return hidden && element.scrollWidth > element.clientWidth + 1;
      })
      .map((element) => element.textContent?.trim().slice(0, 40)),
  );
}

test.describe("320 CSS px (reflow)", () => {
  test.use({ viewport: { width: 320, height: 640 } });

  test("every route fits without horizontal scrolling", async ({ page }) => {
    test.setTimeout(180_000);
    await mockState(page, "data");
    for (const path of routes) {
      await test.step(path, async () => {
        await openPage(page, path, "data");
        expect.soft(await overflow(page), path).toBe(0);
      });
    }
  });
});

test.describe("200 % zoom", () => {
  // A 1280 × 800 window at 200 % zoom lays out like a 640 × 400 one.
  test.use({ viewport: { width: 640, height: 400 } });

  test("every route fits, and the navigation and page content stay reachable", async ({ page }) => {
    test.setTimeout(180_000);
    await mockState(page, "data");
    for (const path of routes) {
      await test.step(path, async () => {
        await openPage(page, path, "data");
        expect.soft(await overflow(page), path).toBe(0);
        // The navigation is in reach (bottom bar or sidebar).
        await expect.soft(page.getByRole("navigation").first()).toBeInViewport();
      });
    }
  });
});

test.describe("text spacing (WCAG 1.4.12)", () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test("larger line, letter and word spacing does not cut off text", async ({ page }) => {
    test.setTimeout(180_000);
    await mockState(page, "data");
    for (const path of routes) {
      await test.step(path, async () => {
        await openPage(page, path, "data");
        await page.addStyleTag({
          content: `* { line-height: 1.5 !important; letter-spacing: 0.12em !important;
            word-spacing: 0.16em !important; } p { margin-bottom: 2em !important; }`,
        });
        expect.soft(await overflow(page), path).toBe(0);
        expect.soft(await clippedText(page), path).toEqual([]);
      });
    }
  });
});

test("reduced motion turns off transitions and animations", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await mockState(page, "data");
  await openPage(page, "/inbox", "data");
  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByRole("dialog", { name: "Command menu" });
  await expect(palette).toBeVisible();
  // At most a few milliseconds per animation (the global rule shortens them to 0.01 ms).
  const durations = await page.evaluate(() =>
    document
      .getAnimations()
      .filter((animation) => animation.timeline instanceof DocumentTimeline)
      .map((animation) => Number(animation.effect?.getComputedTiming().duration ?? 0)),
  );
  for (const duration of durations) expect(duration).toBeLessThanOrEqual(1);
});

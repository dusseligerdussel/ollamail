import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

/** WCAG 2.2 level A and AA rules (docs/DESIGN.md, #123). */
export const wcagTags = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"];

/**
 * Runs axe on the page and fails with one line per violation (impact, rule, elements).
 * Mail HTML is third-party content in a script-less sandbox, where axe cannot run, so iframes
 * are always left out; `exclude` adds further selectors. A soft assertion: a test that checks
 * several pages reports the violations of all of them.
 */
export async function expectNoA11yViolations(page: Page, exclude: string[] = []) {
  await finishAnimations(page);
  let builder = new AxeBuilder({ page }).exclude("iframe").withTags(wcagTags);
  for (const selector of exclude) builder = builder.exclude(selector);
  const results = await builder.analyze();
  const summary = results.violations.map(
    (violation) =>
      `${violation.impact}: ${violation.id} – ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
  expect.soft(summary, new URL(page.url()).pathname).toEqual([]);
}

/**
 * Waits until running animations and transitions have ended: axe measures colours, and a menu
 * that is still fading in or a hover transition yields false contrast violations. Skeletons pulse
 * forever and scroll-driven fades follow the scroll position, so both are left out.
 */
export async function finishAnimations(page: Page) {
  await page.evaluate(() =>
    Promise.all(
      document
        .getAnimations()
        .filter(
          (animation) =>
            animation.timeline instanceof DocumentTimeline &&
            animation.effect?.getComputedTiming().iterations !== Infinity,
        )
        .map((animation) => animation.finished.catch(() => undefined)),
    ),
  );
}

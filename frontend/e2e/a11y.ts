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
  let builder = new AxeBuilder({ page }).exclude("iframe").withTags(wcagTags);
  for (const selector of exclude) builder = builder.exclude(selector);
  const results = await builder.analyze();
  const summary = results.violations.map(
    (violation) =>
      `${violation.impact}: ${violation.id} – ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
  expect.soft(summary, new URL(page.url()).pathname).toEqual([]);
}

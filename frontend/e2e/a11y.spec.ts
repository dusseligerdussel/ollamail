import { expect, type Page, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockState, openPage, publicRoutes, routes, type State } from "./app-states";
import { mockApi } from "./mock-api";

// axe (WCAG 2.2 AA) for every route and its main states (`app-states.ts`), in light and dark,
// on desktop and phone (#123). Feature specs check their own flows (open mail, answer
// stream, …); this one makes sure no page is left out.

const viewports = [
  { width: 1440, height: 900 },
  { width: 390, height: 844 },
];

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of viewports) {
    test.describe(`${colorScheme}, ${viewport.width}px`, () => {
      test.use({ colorScheme, viewport });

      // Error and loading states share their components (InlineError, skeletons) across pages,
      // so two combinations cover them: light on desktop and dark on the phone.
      const states: State[] =
        (colorScheme === "light") === viewport.width > 768
          ? ["data", "empty", "error", "loading"]
          : ["data", "empty"];
      for (const state of states) {
        test(`every route (${state}) has no axe violations`, async ({ page }) => {
          test.setTimeout(240_000);
          await mockState(page, state);
          for (const path of routes) {
            await test.step(path, async () => {
              await openPage(page, path, state);
              await expectNoA11yViolations(page);
            });
          }
        });
      }

      test("sign-in, setup, invitation and 403 pages have no axe violations", async ({ page }) => {
        for (const [path, api] of publicRoutes) {
          await test.step(path, async () => {
            await page.unrouteAll();
            await mockApi(page, api);
            await openPage(page, path, "data");
            expect(new URL(page.url()).pathname).toBe(path);
            await expectNoA11yViolations(page);
          });
        }
      });

      test("dialogs, sheets and the command palette have no axe violations", async ({ page }) => {
        test.setTimeout(120_000);
        await mockState(page, "data");

        // Path, what opens the overlay (a key or a button in the page).
        const overlays: [string, string][] = [
          ["/inbox", "ControlOrMeta+k"],
          ["/settings", "Delete account…"],
          ["/admin/users", "Invite user"],
          ["/admin/sign-in", "Add provider"],
          ["/admin/ai", "Add provider"],
          ["/admin/ai", "Edit: Example Cloud"],
        ];
        // The shortcut overview exists only with a keyboard (wide screens).
        if (viewport.width >= 768) overlays.push(["/inbox", "?"]);
        for (const [path, opener] of overlays) {
          await test.step(`${path}: ${opener}`, async () => {
            await openPage(page, path, "data");
            if (opener.length <= 1 || opener.includes("+")) await page.keyboard.press(opener);
            else await clickButton(opener)(page);
            const dialog = page.getByRole("dialog");
            await expect(dialog).toHaveAccessibleName(/\S/);
            await expect(dialog).toBeVisible();
            await expect(dialog).not.toHaveAttribute("data-state", "closed");
            // Sheets slide in; axe measures colours, so wait until the transition has ended.
            await dialog.evaluate((element) =>
              Promise.all(
                element
                  .getAnimations({ subtree: true })
                  // Skeletons pulse forever, scroll-driven fades follow the scroll position.
                  .filter(
                    (animation) =>
                      animation.timeline instanceof DocumentTimeline &&
                      animation.effect?.getComputedTiming().iterations !== Infinity,
                  )
                  .map((animation) => animation.finished),
              ),
            );
            await expectNoA11yViolations(page);
          });
        }

        await test.step("/admin/users: menu", async () => {
          await openPage(page, "/admin/users", "data");
          await clickButton("Actions for Jonas Beispiel")(page);
          await expect(page.getByRole("menu")).toBeVisible();
          await expectNoA11yViolations(page);
        });
      });
    });
  }
}

function clickButton(name: string) {
  return (page: Page) => page.getByRole("main").getByRole("button", { name, exact: true }).click();
}

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page, test } from "@playwright/test";

import { type MockApi, mockApi } from "./mock-api";
import { mockMail } from "./mock-mail";

const pages = ["/inbox", "/tasks", "/digest", "/search", "/settings", "/admin"] as const;

// Pages that need a different session state: path → mocked API.
const statePages: [string, MockApi][] = [
  ["/login", { role: null }],
  ["/setup", { initialized: false, role: null }],
  // 403 page
  ["/admin", { role: "user" }],
];

// Runs without a backend: the API is mocked (signed-in admin unless a test says otherwise).
test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await mockMail(page);
});

async function expectNoA11yViolations(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  const summary = results.violations.map(
    (violation) =>
      `${violation.impact}: ${violation.id} – ${violation.nodes.map((node) => node.target.join(" ")).join(", ")}`,
  );
  expect(summary).toEqual([]);
}

test("loads without requests to external origins", async ({ page, baseURL }) => {
  const appOrigin = new URL(baseURL ?? "").origin;
  const externalRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol.startsWith("http") && url.origin !== appOrigin) {
      externalRequests.push(url.origin);
    }
  });

  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();
  expect(externalRequests).toEqual([]);
});

test("applies the stored theme before the app script runs", async ({ page }) => {
  await page.addInitScript(() => window.localStorage.setItem("ollamail.theme", "dark"));
  // Without the app bundle only the blocking theme script can set the class.
  await page.route("**/src/main.tsx*", (route) => route.abort());
  await page.goto("/inbox");
  await expect(page.locator("html")).toHaveClass(/\bdark\b/);
  expect(await page.evaluate(() => document.documentElement.style.colorScheme)).toBe("dark");
});

test("is operable with the keyboard", async ({ page }) => {
  await page.goto("/inbox");
  await expect(page.getByRole("heading", { level: 1, name: "Inbox" })).toBeVisible();

  await page.keyboard.press("Tab");
  const skipLink = page.getByRole("link", { name: "Skip to content" });
  await expect(skipLink).toBeFocused();
  await expect(skipLink).toBeVisible();

  await page.keyboard.press("ControlOrMeta+k");
  await page.getByRole("combobox").fill("digest");
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { level: 1, name: "Digest" })).toBeVisible();

  await page.keyboard.press("g");
  await page.keyboard.press("s");
  await expect(page.getByRole("heading", { level: 1, name: "Settings" })).toBeVisible();

  await page.keyboard.press("?");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
});

for (const colorScheme of ["light", "dark"] as const) {
  for (const viewport of [
    { width: 1440, height: 900 },
    { width: 360, height: 740 },
  ]) {
    test.describe(`${colorScheme}, ${viewport.width}px`, () => {
      test.use({ colorScheme, viewport });

      test("pages have no axe violations", async ({ page }) => {
        for (const path of pages) {
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
          await expectNoA11yViolations(page);
        }
      });

      test("sign-in, setup and 403 pages have no axe violations", async ({ page }) => {
        for (const [path, api] of statePages) {
          await page.unrouteAll();
          await mockApi(page, api);
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
          expect(new URL(page.url()).pathname).toBe(path);
          await expectNoA11yViolations(page);
          const overflow = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
          );
          expect(overflow, path).toBe(0);
        }
      });

      test("layout does not overflow horizontally", async ({ page }) => {
        for (const path of pages) {
          await page.goto(path);
          await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
          const overflow = await page.evaluate(
            () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
          );
          expect(overflow, path).toBe(0);
        }
      });
    });
  }
}

test("overlays have no axe violations", async ({ page }) => {
  await page.goto("/inbox");
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();

  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.getByRole("dialog", { name: "Command menu" })).toBeVisible();
  await expectNoA11yViolations(page);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await page.keyboard.press("?");
  await expect(page.getByRole("dialog", { name: "Keyboard shortcuts" })).toBeVisible();
  await expectNoA11yViolations(page);
});

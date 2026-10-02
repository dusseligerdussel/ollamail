import { expect, type Page, test } from "@playwright/test";

import { expectNoA11yViolations } from "./a11y";
import { mockApi } from "./mock-api";
import { answered, mockDrafts, overviewDrafts, SUGGESTION } from "./mock-drafts";
import { mockMail } from "./mock-mail";

// Runs without a backend: API, mails, drafts and the generation stream are mocked
// (synthetic data).
test.beforeEach(async ({ page }) => {
  await mockApi(page);
  await mockMail(page);
});

async function overflow(page: Page) {
  return page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
}

const editor = (page: Page) => page.getByRole("region", { name: "Reply" });
const replyText = (page: Page) => page.getByRole("textbox", { name: "Reply text" });

async function openMail(page: Page) {
  await page.goto(`/inbox?message=${answered}`);
  await expect(page.getByRole("article", { name: "Message from Lena Muster" })).toBeVisible();
}

test("suggest a draft (streamed), edit it and send it", async ({ page }) => {
  const { requests } = await mockDrafts(page);
  await openMail(page);

  // `r` opens the editor below the thread with the cursor in the text.
  await page.keyboard.press("r");
  await expect(replyText(page)).toBeFocused();
  await expect(editor(page).getByText("Lena Muster <lena.muster@example.com>")).toBeVisible();

  await editor(page).getByRole("button", { name: "Suggest draft" }).click();
  const instruction = page.getByRole("textbox", { name: "Short instruction (optional)" });
  await expect(instruction).toBeFocused();
  await instruction.fill("auf Montag verschieben");
  await instruction.press("Enter");

  // The text arrives piece by piece; the field is read-only meanwhile.
  await expect(editor(page).getByText("Writing draft …")).toBeVisible();
  await expect(replyText(page)).toHaveValue(SUGGESTION);
  await expect(replyText(page)).toBeFocused();
  expect(requests).toContainEqual({
    route: "POST /api/drafts/generate",
    body: expect.objectContaining({ message_id: answered, instruction: "auf Montag verschieben" }),
  });

  // Edit: the change is saved automatically.
  await replyText(page).press("ControlOrMeta+End");
  await page.keyboard.type("\nPS: Die Ankündigung geht heute raus.");
  await expect(editor(page).getByText("Saved")).toBeVisible();
  const edited = `${SUGGESTION}\nPS: Die Ankündigung geht heute raus.`;
  expect(requests).toContainEqual({
    route: expect.stringMatching(/^PATCH \/drafts\//),
    body: { body: edited },
  });

  // ⌘Enter / Ctrl+Enter sends an edited draft right away.
  await page.keyboard.press("ControlOrMeta+Enter");
  await expect(page.getByText("Reply sent")).toBeVisible();
  await expect(editor(page)).toHaveCount(0);
  expect(requests.map((request) => request.route)).toContainEqual(
    expect.stringMatching(/^POST \/drafts\/.+\/send$/),
  );
  await expect(page.getByRole("button", { name: /^Reply/ }).first()).toBeVisible();
});

test("an unchanged suggestion needs a second confirmation before sending", async ({ page }) => {
  const { requests } = await mockDrafts(page);
  await openMail(page);
  await page.getByRole("button", { name: /^Reply all/ }).click();
  await expect(editor(page).getByText("team@example.org")).toBeVisible();
  await expect(editor(page).getByRole("radio", { name: "Reply all" })).toBeChecked();

  await editor(page).getByRole("button", { name: "Suggest draft" }).click();
  await page.getByRole("button", { name: "Suggest", exact: true }).click();
  await expect(replyText(page)).toHaveValue(SUGGESTION);

  await editor(page).getByRole("button", { name: /^Send/ }).click();
  await expect(
    page.getByText("The suggested draft is unchanged. Please review it before sending."),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Send anyway" })).toBeFocused();
  expect(requests.map((request) => request.route)).not.toContainEqual(
    expect.stringMatching(/\/send$/),
  );

  await page.getByRole("button", { name: "Send anyway" }).click();
  await expect(page.getByText("Reply sent")).toBeVisible();
});

test("cancel a running suggestion with Esc", async ({ page }) => {
  await mockDrafts(page, { pauseAfter: 6 });
  await openMail(page);
  await page.keyboard.press("r");
  await expect(replyText(page)).toBeFocused();
  await page.keyboard.type("Mein Anfang");
  await editor(page).getByRole("button", { name: "Suggest draft" }).click();
  await page.keyboard.press("Enter");
  await expect(replyText(page)).toHaveValue(/^Hallo Lena/);

  await replyText(page).press("Escape");
  await expect(replyText(page)).toHaveValue("Mein Anfang");
  await expect(editor(page).getByText("Writing draft …")).toHaveCount(0);
  // Esc only stopped the suggestion; the mail stays open.
  await expect(page.getByRole("article", { name: "Message from Lena Muster" })).toBeVisible();
});

test("a failed send is explained and the draft stays", async ({ page }) => {
  await mockDrafts(page, { sendError: [502, "recipients_refused"] });
  await openMail(page);
  await page.keyboard.press("r");
  await expect(replyText(page)).toBeFocused();
  await page.keyboard.type("Danke, ist notiert.");
  await page.keyboard.press("ControlOrMeta+Enter");

  const alert = editor(page).getByRole("alert");
  await expect(alert).toContainText("Not sent");
  await expect(alert).toContainText("The mail server refused one or more recipients.");
  await expect(replyText(page)).toHaveValue("Danke, ist notiert.");
});

test("the drafts overview opens a draft in its thread", async ({ page }) => {
  await mockDrafts(page, { drafts: overviewDrafts() });
  await page.goto("/drafts");
  const list = page.getByRole("list", { name: "Drafts" });
  await expect(list.getByRole("listitem")).toHaveCount(3);
  await expect(list.getByText("The mail you replied to was deleted")).toBeVisible();

  await page.keyboard.press("j");
  await page.keyboard.press("o");
  await expect(page).toHaveURL(new RegExp(`/inbox\\?message=${answered}`));
  await expect(replyText(page)).toHaveValue(/Samstag passt, ich gebe dem Support/);
});

for (const colorScheme of ["light", "dark"] as const) {
  test.describe(`${colorScheme} theme`, () => {
    test.use({ colorScheme });

    test("editor and overview have no axe violations", async ({ page }) => {
      await mockDrafts(page, { drafts: overviewDrafts() });
      await openMail(page);
      await expect(replyText(page)).toBeVisible();
      await page.waitForLoadState("networkidle");
      await expectNoA11yViolations(page);

      await page.goto("/drafts");
      await expect(page.getByRole("list", { name: "Drafts" })).toBeVisible();
      await expectNoA11yViolations(page);
    });
  });
}

test("mobile: the editor fits the screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockDrafts(page);
  await openMail(page);
  await page.getByRole("button", { name: /^Reply all/ }).click();
  await editor(page).getByRole("button", { name: "Suggest draft" }).click();
  await page.getByRole("button", { name: "Suggest", exact: true }).click();
  await expect(replyText(page)).toHaveValue(SUGGESTION);
  expect(await overflow(page)).toBe(0);

  await page.goto("/drafts");
  await expect(page.getByRole("heading", { level: 1, name: "Drafts" })).toBeVisible();
  expect(await overflow(page)).toBe(0);
});

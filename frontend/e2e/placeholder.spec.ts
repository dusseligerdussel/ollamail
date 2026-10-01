import { expect, test } from "@playwright/test";

test("shows the placeholder without requests to external origins", async ({ page, baseURL }) => {
  const appOrigin = new URL(baseURL ?? "").origin;
  const externalRequests: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.protocol.startsWith("http") && url.origin !== appOrigin) {
      externalRequests.push(url.origin);
    }
  });

  await page.goto("/", { waitUntil: "networkidle" });

  await expect(page.getByRole("heading", { name: "ollamail" })).toBeVisible();
  expect(externalRequests).toEqual([]);
});

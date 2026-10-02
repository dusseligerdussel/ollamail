import { defineConfig, devices } from "@playwright/test";

const port = 4173;
const baseURL = `http://localhost:${port}`;

const chromium = {
  ...devices["Desktop Chrome"],
  launchOptions: {
    // Allows using a preinstalled browser instead of `playwright install`.
    executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH || undefined,
  },
};

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  // No retries: a test that only passes on a second attempt hides a bug or a race.
  retries: 0,
  reporter: process.env.CI ? [["github"], ["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [
    // Against the real API (E2E_API=1) the setup flow needs an empty database, so it runs
    // before everything else. Without E2E_API it skips itself.
    { name: "setup", testMatch: "auth.spec.ts", use: chromium },
    {
      name: "chromium",
      testIgnore: "auth.spec.ts",
      grepInvert: /@perf/,
      dependencies: ["setup"],
      use: chromium,
    },
    // Frame-time measurements are only meaningful without parallel tests competing for the CPU.
    { name: "perf", grep: /@perf/, dependencies: ["chromium"], use: chromium },
  ],
  webServer: {
    // E2E_PREVIEW=1 serves the production build (run `pnpm build` first) instead of the
    // dev server; both proxy `/api` to the backend on port 8000.
    command: process.env.E2E_PREVIEW
      ? `pnpm preview --port ${port} --strictPort`
      : `pnpm dev --port ${port} --strictPort`,
    url: baseURL,
    reuseExistingServer: !process.env.CI,
  },
});

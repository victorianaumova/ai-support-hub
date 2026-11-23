import { defineConfig, devices } from "@playwright/test";

/** E2E against the e2e compose stack (`make test-e2e`): the API serves the built app, fake
 * models answer. The scenarios share one database, so they run in order, one at a time. */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  use: {
    // localhost (not 127.0.0.1): browsers treat it as a secure context, so the `Secure` session
    // cookies work over plain HTTP here.
    baseURL: process.env.E2E_URL ?? "http://localhost:8100",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});

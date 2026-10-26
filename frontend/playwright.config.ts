import { defineConfig, devices } from "@playwright/test";

import { FRONTEND_PORT } from "./e2e/stack";

/**
 * End-to-end tests (`pnpm e2e`) against a throwaway Docker Compose stack that
 * global-setup.ts starts and global-teardown.ts deletes (D44). First time only:
 * `pnpm exec playwright install chromium`.
 */
export default defineConfig({
  testDir: "./e2e",
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  // One user, one stack: tests run one at a time.
  workers: 1,
  fullyParallel: false,
  reporter: "list",
  use: {
    // localhost: browsers treat it as secure, so the Secure session cookie works over http.
    baseURL: `http://localhost:${FRONTEND_PORT}`,
    trace: "retain-on-failure",
  },
  projects: [
    // First, while e2e-bot is running: the chart watches its live candles.
    { name: "market", testMatch: "market.spec.ts", use: { ...devices["Desktop Chrome"] } },
    // Then the rest, the kill switch among them, which stops the bot.
    {
      name: "chromium",
      testIgnore: "market.spec.ts",
      dependencies: ["market"],
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});

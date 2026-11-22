import { defineConfig, devices } from "@playwright/test";

/**
 * Browser tests run against a stack that is already up (`docker compose up`); they do not start one.
 *
 *   BASE_URL     the stack's one origin, default http://localhost:8080
 *   MAILPIT_URL  the mail catcher's address, default http://localhost:8025 (the sign-in code is read from it)
 *
 * They create accounts and write cost-ledger rows, so point them at a throwaway stack, never at one that holds your own
 * data (the README shows how to start one on other ports and a separate database).
 */
export default defineConfig({
  testDir: "./tests/e2e",
  // The specs share one stack and one mailbox, so they run one after another.
  workers: 1,
  fullyParallel: false,
  // A test that fails without a code change is fixed or deleted, never retried silently (FRONTEND_UI 11).
  retries: 0,
  reporter: process.env.CI ? [["github"], ["list"]] : "list",
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});

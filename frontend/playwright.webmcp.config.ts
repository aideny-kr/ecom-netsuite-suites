import { defineConfig } from "@playwright/test";

// Run against a locally served build; all API responses in the suite are fixtures.
// Keep separate from the existing backend-seeded e2e suite and its browser version.
export default defineConfig({
  testDir: "./tests",
  testMatch: "webmcp.spec.ts",
  timeout: 30_000,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: process.env.BASE_URL || "http://localhost:3004",
    channel: "chrome",
    launchOptions: { args: ["--enable-features=WebMCP"] },
    trace: "retain-on-failure",
  },
});

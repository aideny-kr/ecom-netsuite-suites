import { defineConfig } from "@playwright/test";

/**
 * Live Electron e2e for the rich-pipe `data_table` render (B0 visual smoke,
 * automated). Serves the built static renderer (`renderer/out/`, which carries
 * the strict packaged CSP + injected hashes) and the test points the Electron
 * dev renderer at it via `SUITE_STUDIO_RENDERER_URL`.
 *
 * The test drives the real agent end-to-end, so it needs a resolvable Anthropic
 * credential (Claude Code Keychain / OAuth, per ADR-008/009) and makes a real,
 * subscription-billed call. It is therefore gated behind `RUN_DESKTOP_E2E=1`
 * and skips otherwise — it never runs unbidden in CI or `npm test` (vitest).
 */
export default defineConfig({
  testDir: "./e2e",
  testMatch: /.*\.e2e\.ts/,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 120_000,
  reporter: [["list"]],
  webServer: {
    // Serve the pre-built static export; Electron (dev mode) loads it via
    // SUITE_STUDIO_RENDERER_URL so we exercise the packaged CSP + hashes.
    command: "python3 -m http.server 3123 --directory renderer/out",
    url: "http://127.0.0.1:3123/index.html",
    reuseExistingServer: true,
    timeout: 30_000,
  },
});

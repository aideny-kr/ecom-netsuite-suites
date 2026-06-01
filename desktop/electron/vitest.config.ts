import { defineConfig } from "vitest/config";

// Vitest config for the Electron B0 spike (/goal #5).
//
// Tests for `sidecar.ts` and `main.ts` run in the default node environment.
// Renderer tests opt into jsdom per-file via `// @vitest-environment jsdom`.
// Electron itself is mocked out — we do NOT boot a real Electron window in
// CI; that's the operator's `npm start` smoke (gate #8).
export default defineConfig({
  test: {
    environment: "node",
    include: ["tests/**/*.test.ts"],
    globals: false,
    typecheck: {
      enabled: false,
    },
  },
});

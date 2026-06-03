import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

/**
 * E2E smoke for the frontend stale-bundle self-recovery banner.
 *
 * Bug (deploy.md #7): after a manual FE deploy, an already-open tab runs the
 * OLD bundle against the NEW container → ChunkLoadError + "Failed to find
 * Server Action" 500s → a page that renders but is "static, no interaction."
 *
 * Fix: `useVersionCheck` polls the same-origin `/version` route (the deployed
 * container's build id) and compares it to the bundle's inlined `BUILD_ID`.
 * On a mismatch, `NewVersionBanner` (mounted in the root layout) surfaces a
 * non-blocking "A new version is available." banner with a Refresh button.
 *
 * Strategy: route-intercept the same-origin /version route to return a build
 * id that differs from the running bundle's, then assert the banner + Refresh
 * appear and that
 * Refresh reloads. The chunk-error AUTO-reload path is covered by unit tests
 * (jsdom) — it is not reliably reproducible in a real browser e2e.
 *
 * REQUIREMENT (server start, NOT this spec): the app under test MUST be served
 * with a fixed non-"dev" `NEXT_PUBLIC_BUILD_ID`. When `BUILD_ID === "dev"` the
 * version check is a deliberate no-op (no local false positives), so the banner
 * could never render and this spec would assert something impossible.
 *   - CI: the `e2e-smoke` job sets `NEXT_PUBLIC_BUILD_ID=e2e-baseline` on the
 *     Playwright step (see `.github/workflows/ci.yml`).
 *   - Local/manual: start the frontend with the same env, e.g.
 *     `NEXT_PUBLIC_BUILD_ID=e2e-baseline npm run build && \
 *      NEXT_PUBLIC_BUILD_ID=e2e-baseline npm run start`.
 * playwright.config.ts has no `webServer` block (the stack is started out of
 * band against BASE_URL), so the env is set at the server start, not here.
 *
 * The intercept below returns a DIFFERENT id (`SKEW_BUILD_ID`) than the served
 * `e2e-baseline`, producing the deploy-skew the banner needs.
 */

const SKEW_BUILD_ID = "e2e-skew-newer-build";

test.describe("Frontend stale-bundle self-recovery — version banner", () => {
  test.beforeEach(async ({ page }) => {
    // Always return a "newer" build id from the deployed /version route.
    await page.route("**/version", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        headers: { "Cache-Control": "no-store" },
        body: JSON.stringify({ buildId: SKEW_BUILD_ID }),
      });
    });
    await registerAndLogin(page);
  });

  test("surfaces the new-version banner and Refresh reloads the page", async ({
    page,
  }) => {
    const banner = page.getByText(/A new version is available/i);
    await expect(banner).toBeVisible({ timeout: 10_000 });

    const refresh = page.getByRole("button", { name: /refresh/i });
    await expect(refresh).toBeVisible();

    // Clicking Refresh triggers a full reload (navigation).
    await Promise.all([page.waitForLoadState("load"), refresh.click()]);

    // After reload the skew persists (same intercept) so the banner returns.
    await expect(page.getByText(/A new version is available/i)).toBeVisible({
      timeout: 10_000,
    });
  });

  test("dismiss hides the banner without reloading", async ({ page }) => {
    const banner = page.getByText(/A new version is available/i);
    await expect(banner).toBeVisible({ timeout: 10_000 });

    await page.getByRole("button", { name: /dismiss/i }).click();
    await expect(banner).toBeHidden();
  });
});

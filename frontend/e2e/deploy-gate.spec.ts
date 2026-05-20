/**
 * E2E coverage for the two-step gated sandbox deploy UX.
 *
 * Strategy:
 * - Skip the login form entirely. Mint a JWT for ayi@frame.work via the
 *   backend test endpoint and inject it into localStorage. (The unit
 *   tests cover the auth flow; here we're testing the deploy gate.)
 * - Reset the test changeset's deploy-token state between tests by
 *   POSTing through the backend (delete in-flight tokens + unconsume
 *   the last confirmed run so the gate can re-fire).
 * - Use data-testid attributes for stable selectors.
 *
 * Required env:
 *   E2E_BASE_URL          — frontend base (default http://localhost:3001)
 *   E2E_API_URL           — backend base (default http://localhost:8001)
 *   E2E_ACCESS_TOKEN      — pre-minted JWT for the test user
 *   E2E_CHANGESET_ID      — approved changeset with passing gates
 *
 * Run locally:
 *   E2E_BASE_URL=http://localhost:3001 \
 *   E2E_API_URL=http://localhost:8001 \
 *   E2E_ACCESS_TOKEN=$(...mint...) \
 *   E2E_CHANGESET_ID=24b693cd-f1f2-4b18-a2c9-9e7afd84f017 \
 *   npx playwright test e2e/deploy-gate.spec.ts --project=chromium
 */

import { execSync } from "node:child_process";
import path from "node:path";
import { test, expect, type Page } from "@playwright/test";

const FRONTEND = process.env.E2E_BASE_URL || "http://localhost:3001";
const API = process.env.E2E_API_URL || "http://localhost:8001";
const TOKEN = process.env.E2E_ACCESS_TOKEN || "";
const CHANGESET_ID = process.env.E2E_CHANGESET_ID || "";
const WORKSPACE_NAME = process.env.E2E_WORKSPACE_NAME || "NetSuite Scripts";
const SANDBOX_TARGET = process.env.E2E_SANDBOX_TARGET || "6738075-sb1";

test.skip(
  !TOKEN || !CHANGESET_ID,
  "Set E2E_ACCESS_TOKEN + E2E_CHANGESET_ID to run the deploy-gate E2E suite. " +
    "See file header for mint instructions.",
);

async function injectToken(page: Page) {
  // Visit a same-origin page first so localStorage is writable.
  await page.goto(`${FRONTEND}/login`);
  await page.evaluate((t) => {
    localStorage.setItem("access_token", t);
    document.cookie = `access_token=${t}; path=/; max-age=604800; samesite=lax`;
  }, TOKEN);
}

function resetDeployGateState() {
  // Shell out to the Python helper that clears in-flight tokens + queued
  // runs for the test changeset, so each test starts from a known
  // deploy-eligible state. Worktree-only: it loads the backend's
  // settings (engine + .env) so we don't have to plumb DSNs through the
  // frontend test config.
  const repoRoot = path.resolve(process.cwd(), "..");
  const backendDir = path.join(repoRoot, "backend");
  const venvPython = path.join(backendDir, ".venv", "bin", "python");
  execSync(
    `${venvPython} scripts/e2e_deploy_gate_reset.py ${CHANGESET_ID}`,
    { cwd: backendDir, env: process.env, stdio: "inherit" },
  );
}

async function openTestChangeset(page: Page) {
  await page.goto(`${FRONTEND}/workspace`);

  // Pick the workspace by display name.
  const selector = page.getByRole("combobox").filter({ hasText: /Select workspace|NetSuite Scripts/ });
  await selector.click();
  await page.getByRole("option", { name: WORKSPACE_NAME }).click();

  // Expand the row for the approved test changeset by finding the
  // "approved" badge nearest a row, then clicking it. Use the title
  // text the seed script set.
  const csRow = page.getByRole("button", {
    name: /Add Sakura mainboards, tulip-refresh.*approved/i,
  });
  await csRow.waitFor({ timeout: 10_000 });
  await csRow.click();

  // Fill the sandbox target — the input has no default value.
  await page.getByPlaceholder(SANDBOX_TARGET).fill(SANDBOX_TARGET);
}

test.describe("Deploy-gate UX", () => {
  test.beforeEach(async ({ page }) => {
    resetDeployGateState();
    await injectToken(page);
  });

  test("preview renders the confirmation card with manifest + gates", async ({ page }) => {
    await openTestChangeset(page);
    await page.getByRole("button", { name: /Deploy Sandbox/ }).click();

    const card = page.getByTestId("deploy-confirmation-card");
    await expect(card).toBeVisible({ timeout: 10_000 });

    // Header shows the sandbox target.
    await expect(card).toContainText(/Deploy \d+ files? to/);
    await expect(card).toContainText(SANDBOX_TARGET);

    // Gates row has at least one of the seeded passing states.
    await expect(card).toContainText("validate:");
    await expect(card).toContainText("tests:");
    await expect(card).toContainText("assertions:");

    // File manifest is present.
    await expect(card.locator("ul li").first()).toBeVisible();

    // Confirm + Cancel buttons rendered.
    await expect(page.getByTestId("deploy-confirm-button")).toBeVisible();
    await expect(page.getByTestId("deploy-cancel-button")).toBeVisible();
  });

  test("show-more expands the file list", async ({ page }) => {
    await openTestChangeset(page);
    await page.getByRole("button", { name: /Deploy Sandbox/ }).click();

    const card = page.getByTestId("deploy-confirmation-card");
    await expect(card).toBeVisible({ timeout: 10_000 });

    const moreButton = page.getByTestId("deploy-show-more-files");
    if (await moreButton.isVisible()) {
      const initialCount = await card.locator("ul li").count();
      await moreButton.click();
      // After expansion the list grows.
      const expandedCount = await card.locator("ul li").count();
      expect(expandedCount).toBeGreaterThan(initialCount);
      // The "show N more" button is gone.
      await expect(moreButton).toHaveCount(0);
    } else {
      test.skip(true, "Manifest has ≤5 files; nothing to expand.");
    }
  });

  test("cancel hides the card without queuing a run", async ({ page }) => {
    await openTestChangeset(page);
    await page.getByRole("button", { name: /Deploy Sandbox/ }).click();

    const card = page.getByTestId("deploy-confirmation-card");
    await expect(card).toBeVisible({ timeout: 10_000 });

    // Capture network: cancel should NOT POST to /confirm.
    let confirmCalled = false;
    page.on("request", (req) => {
      if (req.url().includes("/deploy-sandbox/confirm") && req.method() === "POST") {
        confirmCalled = true;
      }
    });

    await page.getByTestId("deploy-cancel-button").click();

    // Card disappears.
    await expect(card).toHaveCount(0);

    // Brief settle window so a stray POST would land.
    await page.waitForTimeout(500);
    expect(confirmCalled).toBe(false);
  });

  test("confirm queues a deploy_sandbox WorkspaceRun", async ({ page }) => {
    await openTestChangeset(page);
    await page.getByRole("button", { name: /Deploy Sandbox/ }).click();

    const card = page.getByTestId("deploy-confirmation-card");
    await expect(card).toBeVisible({ timeout: 10_000 });

    // Watch the confirm response to assert success + grab the run_id.
    const confirmRespPromise = page.waitForResponse(
      (resp) =>
        resp.url().includes("/deploy-sandbox/confirm") &&
        resp.request().method() === "POST",
    );
    await page.getByTestId("deploy-confirm-button").click();
    const confirmResp = await confirmRespPromise;
    expect(confirmResp.status()).toBe(202);

    const body = await confirmResp.json();
    expect(body.run_type).toBe("deploy_sandbox");
    expect(body.status).toMatch(/queued|running/);

    // The card should clear from the UI.
    await expect(card).toHaveCount(0, { timeout: 5_000 });
  });

  test("deploy button is disabled while a preview card is open", async ({ page }) => {
    await openTestChangeset(page);

    // First click — mints a preview, opens the card.
    const deployButton = page.getByRole("button", { name: /Deploy Sandbox/ });
    await deployButton.click();
    await expect(page.getByTestId("deploy-confirmation-card")).toBeVisible({
      timeout: 10_000,
    });

    // UI in-flight protection: the Deploy Sandbox button is disabled
    // while the card is open. This is the UI layer of codex P1 #3 — the
    // backend partial-unique constraint is the safety net (covered by
    // test_13_concurrent_preview_rejected_with_existing_jti).
    await expect(deployButton).toBeDisabled();
  });
});

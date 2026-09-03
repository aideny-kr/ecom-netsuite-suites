/**
 * E2E coverage for the Celigo flow-pages surface (Task 18), against a real
 * seeded tenant — the integrations tile, the flow page's canvas + navigator
 * + inspector + script drawer, and the same "10 open · 1 root cause"
 * cross-surface fact `cross-surface-counts.test.tsx` pins in the unit suite,
 * this time served over the real HTTP API from a real Postgres row set.
 *
 * Required env:
 *   CELIGO_E2E     — set (to anything) to un-skip this file
 *   E2E_EMAIL      — the seeded user's email
 *   E2E_PASSWORD   — the seeded user's password
 *   BASE_URL       — frontend base (playwright.config.ts default: http://localhost:3002)
 *
 * Seed first, then run:
 *   1. docker compose up -d postgres   (or use whatever Postgres --database-url below points at)
 *   2. cd backend && .venv/bin/python -m scripts.seed_celigo_e2e \
 *        --tenant-slug e2e-celigo \
 *        --database-url postgresql+asyncpg://postgres:postgres@localhost:5432/ecom_netsuite_flowpages
 *      (prints the seeded user's email + password on first run — reuse those
 *      for E2E_EMAIL/E2E_PASSWORD, or pass --email/--password explicitly)
 *   3. Point the backend AND frontend under test at that same database
 *      (DATABASE_URL / DATABASE_URL_DIRECT), then:
 *        CELIGO_E2E=1 BASE_URL=http://localhost:3002 \
 *        E2E_EMAIL=e2e-celigo@example.com E2E_PASSWORD='CeligoE2E-Passw0rd!' \
 *        npx playwright test e2e/celigo-flow-pages.spec.ts
 *
 * The seed script is idempotent — re-running it (e.g. before every gate run)
 * finds the same tenant/integration/flow/signature and changes nothing, so
 * this spec always sees the same 10-open/1-signature world.
 */

import { test, expect } from "@playwright/test";

test.skip(!process.env.CELIGO_E2E, "needs a seeded tenant — see file header");

const EMAIL = process.env.E2E_EMAIL ?? "";
const PASSWORD = process.env.E2E_PASSWORD ?? "";

test.describe("Celigo flow pages", () => {
  test("integrations tile → flow page → step inspector → script drawer, all agreeing on 10 open · 1 root cause", async ({
    page,
  }) => {
    // 1. Log in as the seeded user (auth.spec.ts's own login flow).
    await page.goto("/login");
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: "Sign in" }).click();
    await page.waitForURL("**/dashboard", { timeout: 15_000 });

    // 2. The Celigo surface, deep-linked straight to the integrations tiles.
    await page.goto("/workspace?surface=celigo");
    await expect(page.getByTestId("celigo-surface-host")).toBeVisible();

    const tile = page.getByText("Solidus + NetSuite").locator("xpath=ancestor::button[1]");
    await expect(tile.getByText("10 open · 1 root cause")).toBeVisible();

    // 3. Click through to the integration, then to the erroring flow (a
    //    clickable table ROW, not a button — the flows table's own click
    //    handler lives on the `<tr>`).
    await tile.click();
    await page.getByRole("row", { name: /New Sales Order to NetSuite - Multi-Subsidiary/ }).click();
    await expect(page).toHaveURL(/surface=celigo/);

    // 4. The flow page: header pill agrees with the tile, ten bubbles, the
    //    Framework Intl lane label.
    await expect(page.getByText("10 open · 1 root cause")).toBeVisible();
    await expect(page.locator('[data-testid^="step-bubble-"]')).toHaveCount(10);
    await expect(page.getByText(/Framework Intl/)).toBeVisible();

    // 5. The lookup bubble carrying the errors (the ONLY bubble with an open-
    //    error badge — Framework Inc's own lookup has none) → its Errors tab.
    const erroringBubble = page.locator('[data-testid^="step-bubble-"]').filter({ hasText: "10 open" });
    await expect(erroringBubble).toHaveCount(1);
    await erroringBubble.click();
    await page.getByRole("tab", { name: /^Errors/ }).click();
    await expect(page.getByRole("tab", { name: "Errors 10" })).toBeVisible();

    // 6. A hook chip on the Framework Intl lane's final step → Scripts tab →
    //    "Open source" → the script drawer, carrying the N2 banner verbatim.
    //    ("Add New Sales Order (BV)" is unique to that branch; the Framework
    //    Inc branch carries its own "(Inc)"-suffixed sibling.)
    const salesOrderBubble = page.locator('[data-testid^="step-bubble-"]').filter({
      hasText: "Add New Sales Order (BV)",
    });
    await salesOrderBubble.getByText(/^HK preMap/).click();
    await expect(page.getByRole("tab", { name: /^Scripts/, selected: true })).toBeVisible();
    await page.getByRole("button", { name: "Open source →" }).first().click();

    const drawer = page.getByRole("dialog", { name: "Script source" });
    await expect(drawer).toBeVisible();
    await expect(
      drawer.getByText(
        "Customer-authored JavaScript, shown to you only. Never run here, never sent to the assistant.",
      ),
    ).toBeVisible();

    // 7. Escape closes the drawer.
    await page.keyboard.press("Escape");
    await expect(drawer).not.toBeVisible();
  });
});

import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Settings — Pricing config", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("Pricing config (FX/VAT/rounding) is reachable on /settings", async ({ page }) => {
    await page.goto("/settings");
    // The pricing config UI used to live ONLY in the removed Pricing-agent workspace
    // (the agents endpoint returns [] since v2.0). It now has a home in /settings.
    // A freshly-registered org owner is admin, so the admin sections render; the
    // pricing config auto-seeds its 16-currency default on first GET.
    await expect(page.getByText(/USD Base Rate/i)).toBeVisible();
    await expect(page.getByText(/Add Currency/i)).toBeVisible();
  });
});

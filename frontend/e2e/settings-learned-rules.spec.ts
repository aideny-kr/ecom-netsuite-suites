import { test, expect } from "@playwright/test";

import { registerAndLogin } from "./helpers";

test.describe("Settings — Learned Rules", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("section renders with an empty state for a fresh tenant", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Learned Rules" })).toBeVisible();
    await expect(page.getByRole("button", { name: /add rule/i })).toBeVisible();
    await expect(page.getByText(/no learned rules yet/i)).toBeVisible();
  });

  test("admin can add, deactivate, then delete a learned rule", async ({ page }) => {
    await page.goto("/settings");
    const ruleText = `E2E rule ${Date.now()}`;

    // --- add ---
    await page.getByRole("button", { name: /add rule/i }).click();
    await page.getByPlaceholder(/describe the rule/i).fill(ruleText);
    await page.getByPlaceholder(/category/i).fill("term_definition");
    await page.getByRole("button", { name: /^save$/i }).click();
    await expect(page.getByText(ruleText)).toBeVisible({ timeout: 10_000 });

    const row = page.locator("tr", { hasText: ruleText });

    // --- deactivate (toggle is_active) ---
    await row.getByRole("button", { name: "Active" }).click();
    await expect(row.getByRole("button", { name: "Inactive" })).toBeVisible({ timeout: 10_000 });

    // --- delete (with confirmation) ---
    await row.getByRole("button", { name: /delete/i }).click();
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await dialog.getByRole("button", { name: /^delete$/i }).click();
    await expect(page.getByText(ruleText)).toHaveCount(0, { timeout: 10_000 });
  });
});

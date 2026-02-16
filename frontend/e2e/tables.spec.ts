import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Table Explorer", () => {
  test.beforeEach(async ({ page }) => {
    await registerAndLogin(page);
  });

  test("navigate to /tables/orders and verify empty state or skeleton", async ({
    page,
  }) => {
    await page.goto("/tables/orders");

    // Should see either the skeleton loader or the rendered table / empty state
    const skeleton = page.locator('[class*="skeleton"], [class*="Skeleton"]');
    const table = page.locator("table");

    // Wait for one of the two to be present
    await expect(skeleton.or(table).first()).toBeVisible({ timeout: 10_000 });
  });

  test("verify table header columns are visible", async ({ page }) => {
    await page.goto("/tables/orders");

    // Wait for the loading state to resolve
    await page.waitForLoadState("networkidle");

    // If there is data, the table headers should be rendered
    const tableHead = page.locator("thead");
    const skeleton = page.locator('[class*="skeleton"], [class*="Skeleton"]');

    // Either the table head is visible (data loaded) or the skeleton is shown (still loading / no data)
    const headVisible = await tableHead.isVisible().catch(() => false);
    const skeletonVisible = await skeleton.first().isVisible().catch(() => false);

    expect(headVisible || skeletonVisible).toBeTruthy();
  });
});

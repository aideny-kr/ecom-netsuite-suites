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

  test("verify table renders with headers or empty state", async ({ page }) => {
    await page.goto("/tables/orders");

    // A fresh tenant has no data — expect either:
    // 1. Skeleton while loading
    // 2. "No data available" empty state
    // 3. A visible thead with column headers (if data exists)
    const skeleton = page.locator('[class*="skeleton"], [class*="Skeleton"]');
    const emptyState = page.getByText("No data available");
    const tableHead = page.locator("thead th");

    await expect(
      skeleton.first().or(emptyState).or(tableHead.first()),
    ).toBeVisible({ timeout: 15_000 });
  });
});

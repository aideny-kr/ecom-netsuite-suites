import { test, expect } from "@playwright/test";
import { registerAndLogin } from "./helpers";

test.describe("Sidebar Navigation", () => {
  let tenant: Awaited<ReturnType<typeof registerAndLogin>>;

  test.beforeEach(async ({ page }) => {
    tenant = await registerAndLogin(page);
  });

  test("sidebar shows the company name", async ({ page }) => {
    await expect(page.getByText(tenant.name)).toBeVisible();
  });

  test("click Dashboard link and verify URL", async ({ page }) => {
    await page.getByRole("link", { name: "Dashboard" }).click();
    await expect(page).toHaveURL(/\/dashboard/);
  });

  test("click Connections link and verify URL", async ({ page }) => {
    // Use exact match to target the sidebar link, not the dashboard quick-link card
    await page
      .getByRole("link", { name: "Connections", exact: true })
      .click();
    await expect(page).toHaveURL(/\/connections/);
  });

  test("expand Tables section and click Orders", async ({ page }) => {
    // Click the Tables toggle button to expand
    await page.getByRole("button", { name: "Tables" }).click();

    // Target the sidebar Orders link specifically (exact match avoids dashboard card)
    const ordersLink = page.getByRole("link", { name: "Orders", exact: true });
    await expect(ordersLink).toBeVisible({ timeout: 5_000 });

    await ordersLink.click();
    await expect(page).toHaveURL(/\/tables\/orders/);
  });

  test("click Audit Log link if present", async ({ page }) => {
    const auditLink = page.getByRole("link", { name: "Audit Log" });
    const isVisible = await auditLink.isVisible().catch(() => false);

    if (isVisible) {
      await auditLink.click();
      await expect(page).toHaveURL(/\/audit/);
    }
  });
});

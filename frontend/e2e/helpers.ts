import { type Page, expect } from "@playwright/test";

/**
 * Generate unique tenant registration data using a random suffix.
 */
export function generateUniqueTenant() {
  const id = Math.random().toString(36).substring(2, 10);
  return {
    name: `Test Org ${id}`,
    slug: `test-org-${id}`,
    email: `user-${id}@example.com`,
    password: `Password1!${id}`,
    fullName: `E2E User ${id}`,
  };
}

/**
 * Register a new tenant and land on the dashboard.
 * Returns the credentials used so tests can re-login.
 */
export async function registerAndLogin(page: Page) {
  const tenant = generateUniqueTenant();

  await page.goto("/register");
  await page.getByLabel("Organization").fill(tenant.name);
  await page.getByLabel("Slug").fill(tenant.slug);
  await page.getByLabel("Full Name").fill(tenant.fullName);
  await page.getByLabel("Email").fill(tenant.email);
  await page.getByLabel("Password").fill(tenant.password);
  await page.getByRole("button", { name: "Create account" }).click();

  await page.waitForURL("**/dashboard", { timeout: 15_000 });
  await expect(page).toHaveURL(/\/dashboard/);

  return tenant;
}

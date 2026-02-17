import { test, expect } from "@playwright/test";
import { generateUniqueTenant, registerAndLogin } from "./helpers";

test.describe("Authentication", () => {
  test("register a new tenant and redirect to dashboard", async ({ page }) => {
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
  });

  test("login with registered credentials and redirect to dashboard", async ({
    page,
  }) => {
    // First register a user so we have valid credentials
    const tenant = await registerAndLogin(page);

    // Log out by navigating to login page directly
    await page.goto("/login");

    await page.getByLabel("Email").fill(tenant.email);
    await page.getByLabel("Password").fill(tenant.password);
    await page.getByRole("button", { name: "Sign in" }).click();

    await page.waitForURL("**/dashboard", { timeout: 15_000 });
    await expect(page).toHaveURL(/\/dashboard/);
  });

  test("login with wrong password shows error message", async ({ page }) => {
    await page.goto("/login");

    await page.getByLabel("Email").fill("nobody@e2etest.local");
    await page.getByLabel("Password").fill("WrongPassword123!");
    await page.getByRole("button", { name: "Sign in" }).click();

    // The toast title should display a failure message
    await expect(
      page.getByText("Login failed", { exact: true }),
    ).toBeVisible({ timeout: 10_000 });
  });
});
